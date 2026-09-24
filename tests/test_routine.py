"""Instrument routines: the format, the judgement, the read-back against a document, the
per-push ion events they measure, and a whole routine through the tools over the stand-ins
(lab record, task 76).

The stand-in console's per-push spectrum is three separate one-bin events in fifteen pushes
of every sixteen, so a run of it has exactly 2.8125 events per push and 0.9375 of its pushes
occupied: numbers a fixture routine can hold to a hundredth.
"""

from __future__ import annotations

import json
import os

import pytest

from clockwork import envelope, routine, summary
from clockwork.mcp import Toolbox, ToolFailure
from clockwork.mcp.cli import is_verb
from clockwork.mcp.server import SIMULATED
from clockwork.method import template as template_module
from clockwork.mips import Box, FakeBox, read_state
from clockwork.owner import LocalOwner, StartConsole
from clockwork.record import RECORD_SUFFIX

TEMPLATE = """\
template_schema = 1
renders = 2
start = [["box1", "TBLSTRT"]]
reset = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]

[knobs]
b_ticks = { default = 500, min = 100, max = 520, unit = "ticks", description = "line B high" }

[labels]
sample = { required = true, description = "what was sprayed" }

[metadata]
name = "routine-test"
created = 2026-09-24

[acquisition]
frames = 1
scans = 32
accumulations = 2
repetition_mode = "per_repetition"
keep_raw = true
file_stem = "routine-test"
enable = { box = "box1", channel = "A" }

[[boxes]]
name = "box1"
port = "COM3"
setup = ["STBLCLK,EXT"]
load = ["STBLDAT;0:[A:1,0:A:1:B:1,{b_ticks}:B:0,532:A:0,533:];"]
arm = ["SMOD,TBL"]

[boxes.dc_bias]
16 = 5.0
"""

EVENTS_PER_PUSH = 3 * 15 / 16

CHECK = f"""\
routine_schema = 1

[routine]
name = "fixture-check"
description = "one stand-in run, judged on its events"

[acquire]
template = "line-b.toml"
labels = {{ sample = "nothing" }}
replicates = 2

[[measure]]
name = "events"
function = "ion_events"
file = "raw"

[[measure]]
name = "summary"
function = "summarize"

[[criterion]]
name = "beam"
value = "events.events_per_push"
at_least = 0.5
unmet = "no beam"

[[criterion]]
name = "events per push"
value = "events.events_per_push"
reference = "golden"
golden = {EVENTS_PER_PUSH}
factor = 1.01
source = "the stand-in console's invented spectrum"

[[criterion]]
name = "replicates agree"
value = "events.events_per_push"
reference = "pair"
factor = 1.01

[[criterion]]
name = "height against the last pass"
value = "events.height.p50"
reference = "last-passing"
factor = 1.05

[[criterion]]
name = "counts"
value = "summary.total_counts"
at_least = 1
judge = false

[report]
show = ["events.occupancy", "events.width_bins.mode"]
"""

AUDIT = """\
routine_schema = 1

[routine]
name = "fixture-audit"
description = "the boxes against the template's stack"
unattended = true

[[audit]]
name = "stack"
document = "line-b.toml"
settings = ["dc_bias"]

[[criterion]]
name = "stack held"
value = "stack.differences"
at_most = 0
"""


@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "library"
    folder.mkdir()
    (folder / "line-b.toml").write_text(TEMPLATE, encoding="utf-8")
    routines = tmp_path / "routines"
    routines.mkdir()
    (routines / "fixture-check.toml").write_text(CHECK, encoding="utf-8")
    (routines / "fixture-audit.toml").write_text(AUDIT, encoding="utf-8")
    return str(folder)


@pytest.fixture
def fake_owner():
    owner = LocalOwner(fake=True, program="clockwork routine test").start()
    owner.submit(StartConsole())
    yield owner
    owner.shutdown()
    owner.join(30)


def toolbox(owner, library: str, output: str, **extra) -> Toolbox:
    return Toolbox(owner, library=library, output=output, instrument=SIMULATED, **extra)


# -- the format ----------------------------------------------------------------------


def test_a_routine_loads_and_says_what_it_judges() -> None:
    loaded = routine.loads(CHECK)
    assert loaded.name == "fixture-check" and not loaded.unattended
    assert loaded.acquire is not None and loaded.acquire.replicates == 2
    assert [item.function for item in loaded.measures] == ["ion_events", "summarize"]
    assert loaded.criteria[0].describe() == "at least 0.5"
    assert loaded.criteria[1].describe() == f"within 1.01x of {EVENTS_PER_PUSH:g}"
    assert routine.loads(AUDIT).unattended


def test_every_problem_in_a_routine_is_listed_at_once() -> None:
    text = """\
routine_schema = 2
[routine]
[acquire]
template = "x.toml"
replicates = 0
[[measure]]
name = "events"
function = "occupancy"
[[criterion]]
value = "nothing.here"
reference = "pair"
[[criterion]]
value = "events.x"
reference = "golden"
factor = 0.5
"""
    with pytest.raises(routine.RoutineError) as caught:
        routine.loads(text)
    problems = "\n".join(caught.value.problems)
    for expected in ("routine_schema", "routine.name: required", "acquire.replicates",
                     "'occupancy' is not one of", "names no measure or audit",
                     "needs at least two replicates", "golden reference needs its number",
                     "factor of at least 1"):
        assert expected in problems, expected


def test_a_routine_acquires_or_audits_and_not_both() -> None:
    both = CHECK + '\n[[audit]]\nname = "stack"\ndocument = "line-b.toml"\n'
    with pytest.raises(routine.RoutineError, match="one of the two"):
        routine.loads(both)


def test_the_routine_directory_is_beside_the_library(tmp_path) -> None:
    assert routine.default_directory(str(tmp_path / "golden")) == str(tmp_path / "routines")
    assert routine.default_directory("") == ""


def test_request_words_make_each_run_its_own_request() -> None:
    import datetime as dt

    words = routine.request_words(routine.loads(CHECK), dt.datetime(2026, 9, 24, 14, 5, 3))
    assert words == ("routine fixture-check: one stand-in run, judged on its events "
                     "(run 2026-09-24 14:05:03)")


# -- the judgement -------------------------------------------------------------------


def results(*values: float) -> list[dict]:
    return [{"events": {"events_per_push": value, "height": {"p50": 7.0}, "occupancy": 0.9,
                        "width_bins": {"mode": 1}},
             "summary": {"total_counts": 10}} for value in values]


def test_a_run_inside_every_criterion_passes_and_the_last_pass_is_skipped() -> None:
    judged = routine.judge(routine.loads(CHECK), results(EVENTS_PER_PUSH, EVENTS_PER_PUSH))
    assert judged["verdict"] == "pass", judged
    last = next(entry for entry in judged["criteria"] if entry["reference"] == "last-passing")
    assert last["met"] is None and last["why"].startswith("skipped")
    assert judged["values"]["events.occupancy"] == [0.9, 0.9]


def test_an_unmet_reason_is_the_verdict_and_outranks_a_failure() -> None:
    judged = routine.judge(routine.loads(CHECK), results(0.1, 0.1))
    assert judged["verdict"] == "could not judge" and judged["reason"] == "no beam"


def test_outside_the_golden_factor_or_the_pair_spread_fails() -> None:
    judged = routine.judge(routine.loads(CHECK), results(EVENTS_PER_PUSH, 3.5))
    assert judged["verdict"] == "fail"
    failed = {entry["name"] for entry in judged["criteria"] if entry["met"] is False}
    assert failed == {"events per push", "replicates agree"}


def test_the_last_passing_run_is_compared_when_there_is_one() -> None:
    loaded = routine.loads(CHECK)
    earlier = {"values": {"events.height.p50": [8.0, 8.0]}, "request_id": "earlier"}
    judged = routine.judge(loaded, results(EVENTS_PER_PUSH, EVENTS_PER_PUSH), last=earlier)
    entry = next(item for item in judged["criteria"] if item["reference"] == "last-passing")
    assert entry["met"] is False and entry["reference_value"] == 8.0
    assert judged["verdict"] == "fail"


def test_a_number_missing_from_a_result_cannot_be_judged() -> None:
    broken = results(EVENTS_PER_PUSH, EVENTS_PER_PUSH)
    broken[0]["events"] = {"problem": "the run left no raw file"}
    judged = routine.judge(routine.loads(CHECK), broken)
    assert judged["verdict"] == "could not judge"
    assert "the run left no raw file" in judged["reason"]


def test_a_comparison_shown_only_never_decides() -> None:
    quiet = results(EVENTS_PER_PUSH, EVENTS_PER_PUSH)
    for result in quiet:
        result["summary"]["total_counts"] = 0
    judged = routine.judge(routine.loads(CHECK), quiet)
    assert judged["verdict"] == "pass"
    assert "shown only" in routine.report_text(routine.loads(CHECK), judged)


def test_last_passing_finds_the_newest_pass_in_the_run_records(tmp_path) -> None:
    def record(name: str, entries: list[dict]) -> None:
        (tmp_path / f"{name}{RECORD_SUFFIX}").write_text(json.dumps(
            {"request": {"id": name}, "routines": entries}), encoding="utf-8")

    record("a", [{"routine": "fixture-check", "verdict": "pass", "time": "2026-09-24T10:00",
                  "values": {"x": [1]}}])
    record("b", [{"routine": "fixture-check", "verdict": "fail", "time": "2026-09-24T12:00"}])
    record("c", [{"routine": "fixture-check", "verdict": "pass", "time": "2026-09-24T11:00",
                  "values": {"x": [2]}}])
    found = routine.last_passing(str(tmp_path), "fixture-check")
    assert found["values"] == {"x": [2]} and found["request_id"] == "c"
    assert routine.last_passing(str(tmp_path), "fixture-check", before="c")["request_id"] == "a"


# -- reading the boxes back ----------------------------------------------------------


def test_an_audit_lists_each_declared_setting_the_box_does_not_hold() -> None:
    rendered = template_module.render(template_module.loads_template(TEMPLATE), {},
                                      {"sample": "x"})
    box = Box(transport=FakeBox(rf_channels=2), name="box1")
    try:
        before = read_state(box)
        box.command("SDCB,16,5.00")
        after = read_state(box)
    finally:
        box.close()
    found = routine.audit(rendered.method, [before])
    assert found["differences"] == 1 and found["compared"] == 1
    assert found["rows"] == [{"box": "box1", "setting": "dc_bias", "index": 16,
                              "declared": 5.0, "held": 0.0}]
    assert routine.audit(rendered.method, [after])["differences"] == 0
    missing = routine.audit(rendered.method, [])
    assert missing["unread"] == ["box1"] and missing["differences"] == 1
    assert routine.audit(rendered.method, [before], settings=("arb",))["compared"] == 0


# -- the ion events a routine measures -----------------------------------------------


def test_ion_events_counts_the_stand_in_spectrum_exactly(fake_owner, library, tmp_path) -> None:
    box = toolbox(fake_owner, library, str(tmp_path / "runs"))
    report = box.call("run_routine", {"name": "fixture-check", "initials": "rt"})
    raw = report["files"][0]["raw_path"]
    found = summary.ion_events(raw)
    assert found["file"] == "raw" and found["pushes"] == 64
    assert found["events_per_push"] == EVENTS_PER_PUSH
    assert found["occupancy"] == 15 / 16
    assert found["width_bins"]["mode"] == 1 and found["railed_events"] == 0
    assert found["height_mv"]["max"] == pytest.approx(found["height"]["max"] * 0.5e3 / 65536)
    with pytest.raises(summary.SummaryError, match="sums 2 pushes per row"):
        summary.ion_events(report["files"][0]["summed_path"])


# -- whole routines through the tools ------------------------------------------------


def test_a_routine_runs_through_arm_and_acquire_to_a_verdict(fake_owner, library,
                                                             tmp_path) -> None:
    output = str(tmp_path / "runs")
    box = toolbox(fake_owner, library, output)
    listed = box.call("list_routines")["routines"]
    assert {entry["name"] for entry in listed} == {"fixture-check", "fixture-audit"}
    heard: list[str] = []
    box.narrate = heard.append
    first = box.call("run_routine", {"name": "fixture-check", "initials": "rt"})
    assert first["verdict"] == "pass", first["text"]
    assert len(first["files"]) == 2 and first["text"].startswith("fixture-check: pass")
    assert any(line.startswith("arming") for line in heard)
    with open(first["record"], encoding="utf-8") as handle:
        written = json.load(handle)
    assert written["routines"][0]["verdict"] == "pass"
    assert written["plans"][0]["text"].startswith("routine fixture-check")
    assert len(written["acquisitions"]) == 1
    stamped = summary.summarize(first["files"][1]["summed_path"])["clockwork"]
    assert stamped["ClockworkSeriesId"] == first["request_id"]
    assert stamped["ClockworkSeriesIndex"] == 2

    second = box.call("run_routine", {"name": "fixture-check", "initials": "rt"})
    assert second["request_id"] != first["request_id"]
    compared = next(entry for entry in second["criteria"]
                    if entry["reference"] == "last-passing")
    assert compared["met"] is True and compared["reference_request"] == first["request_id"]
    with open(os.path.join(output, "mcp-calls.log"), encoding="utf-8") as handle:
        tools = [json.loads(line)["tool"] for line in handle]
    assert tools.count("acquire") == 2 and tools.count("run_routine") == 2


def test_an_audit_routine_reads_the_boxes_back_and_acquires_nothing(fake_owner, library,
                                                                   tmp_path) -> None:
    output = str(tmp_path / "runs")
    box = toolbox(fake_owner, library, output)
    cold = box.call("run_routine", {"name": "fixture-audit", "initials": "rt"})
    assert cold["verdict"] == "fail" and cold["audit"]["stack"]["differences"] == 1
    assert cold["job"] is None and cold["files"] == []
    box.call("run_routine", {"name": "fixture-check", "initials": "rt"})
    warm = box.call("run_routine", {"name": "fixture-audit", "initials": "rt"})
    assert warm["verdict"] == "pass", warm["text"]
    assert not any(name.endswith(".uimf") and "audit" in name for name in os.listdir(output))


def test_a_refused_arm_is_a_routine_that_could_not_judge(fake_owner, library,
                                                         tmp_path) -> None:
    output = str(tmp_path / "runs")
    limits = envelope.loads('schema_version = 1\n[allow]\nboxes = ["box1"]\n[budget]\n'
                            "max_runs = 5\nmax_replicates_per_run = 2\nmax_hours = 8\n")
    box = toolbox(fake_owner, library, output, limits=limits)
    report = box.call("run_routine", {"name": "fixture-check", "initials": "rt"})
    assert report["verdict"] == "could not judge"
    assert report["reason"].startswith("the arm was refused")
    assert "not in the standing limits" in report["reason"]
    with open(report["record"], encoding="utf-8") as handle:
        assert json.load(handle)["routines"][0]["verdict"] == "could not judge"


def test_an_unknown_routine_names_the_ones_there_are(fake_owner, library, tmp_path) -> None:
    box = toolbox(fake_owner, library, str(tmp_path / "runs"))
    with pytest.raises(ToolFailure, match="the routines are fixture-audit, fixture-check"):
        box.call("run_routine", {"name": "beam-check", "initials": "rt"})


def test_the_command_line_knows_routine_as_a_verb() -> None:
    assert is_verb("run-routine") and is_verb("routine")
