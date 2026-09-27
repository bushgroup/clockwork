"""A series plan and the `series` tool: the plan expanded, shuffled and counted before
anything is sent, refused whole, acquired by the owner as one job, and every file stamped
with its planned index, its executed position and the seed (lab record, task 91).

The tool is driven over a `--fake` owner in process, as `test_mcp.py` drives `acquire`;
the fake files are small (32 scans, two accumulations), so a series of eight takes seconds.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from clockwork import envelope
from clockwork import series as series_module
from clockwork.acq.uimf import SUMMED_SUFFIX
from clockwork.mcp import Toolbox, ToolFailure
from clockwork.mcp.cli import verbs
from clockwork.mcp.server import SIMULATED
from clockwork.method import template as template_module
from clockwork.owner import RemoteOwner, SeriesJob, SeriesResult, wire
from clockwork.summary import summarize
from test_daemon import Daemon
from test_mcp import TEMPLATE, fake_owner, followed  # noqa: F401 -- a fixture, imported

TWO_KNOBS = TEMPLATE.replace(
    'b_ticks = { default = 500',
    'b_on = { default = 1, min = 1, max = 50, unit = "ticks", description = "line B rises" }\n'
    'b_ticks = { default = 500',
).replace("0:A:1:B:1,{b_ticks}:B:0", "0:A:1,{b_on}:B:1,{b_ticks}:B:0")

LABELS = {"sample": "test mixture"}
REQUEST = "Scan line B's fall across three values with the defaults either side, please"

GRID = """\
plan_schema = 1
description = "b_on against b_ticks"
template = "two-knob.toml"
labels = { sample = "test mixture" }
replicates = 2
references = ["start", "end"]
seed = 12345

[grid]
b_on = [1, 20]
b_ticks = [300, 400, 450]
"""


@pytest.fixture
def two_knobs():
    return template_module.loads_template(TWO_KNOBS)


@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "library"
    folder.mkdir()
    (folder / "two-knob.toml").write_text(TWO_KNOBS, encoding="utf-8")
    return str(folder)


def plan_text(*, middle: str, replicates: int = 1, references: str = '["start", "end"]',
              extra: str = "") -> str:
    return (f'plan_schema = 1\ntemplate = "two-knob.toml"\nlabels = {{ sample = "test '
            f'mixture" }}\nreplicates = {replicates}\nreferences = {references}\n{extra}'
            f"\n{middle}")


def limits(library: str, *, max_runs: int, low: int = 100, high: int = 520) -> envelope.Limits:
    loaded = template_module.load_template(os.path.join(library, "two-knob.toml"))
    return envelope.loads(
        'schema_version = 1\ncold_start = "caution"\n[allow]\nboxes = ["box1"]\n'
        f"[budget]\nmax_runs = {max_runs}\nmax_replicates_per_run = 2\nmax_hours = 8\n"
        f'[templates.two]\nhash = "{loaded.hash[:12]}"\n'
        f"[templates.two.knobs]\nb_ticks = {{ min = {low}, max = {high} }}\n")


def summed_stamps(output: str) -> list[dict]:
    """Every summed file's clockwork stamps, by position acquired."""
    found = [summarize(os.path.join(output, name))["clockwork"]
             for name in os.listdir(output) if name.endswith(SUMMED_SUFFIX)]
    return sorted(found, key=lambda stamps: stamps["ClockworkSeriesPosition"])


def record_of(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# --- the plan -----------------------------------------------------------------------


def test_a_grid_of_two_by_three_with_two_references_and_two_replicates_is_16_files(
        two_knobs):
    plan = series_module.loads(GRID)
    points = series_module.expand(plan, two_knobs)
    assert [point.index for point in points] == list(range(1, 9))
    assert [point.reference for point in points] == ["start"] + [""] * 6 + ["end"]
    assert [point.knobs for point in points[1:7]] == [
        {"b_on": 1, "b_ticks": 300}, {"b_on": 1, "b_ticks": 400}, {"b_on": 1, "b_ticks": 450},
        {"b_on": 20, "b_ticks": 300}, {"b_on": 20, "b_ticks": 400},
        {"b_on": 20, "b_ticks": 450}]
    assert points[0].knobs == points[-1].knobs == {}
    assert series_module.files(points) == 16
    assert series_module.acquisitions(points) == 8
    assert series_module.describe(points[4]) == "point 5 (b_on = 20, b_ticks = 300)"
    assert series_module.describe(points[0]) == "the start reference (point 1, the defaults)"


def test_one_seed_gives_one_order_with_the_references_left_at_the_ends(two_knobs):
    points = series_module.expand(series_module.loads(GRID), two_knobs)
    first = [point.index for point in series_module.order(points, 12345)]
    assert first == [point.index for point in series_module.order(points, 12345)]
    assert first[0] == 1 and first[-1] == 8
    assert sorted(first[1:-1]) == list(range(2, 8))
    assert first != list(range(1, 9)), "this seed happens to shuffle"
    assert [point.index for point in series_module.order(points, None)] == list(range(1, 9))
    for _ in range(20):
        assert 0 <= series_module.draw_seed() < 2**31


def test_coupled_points_take_the_defaults_for_what_they_do_not_name(two_knobs):
    plan = series_module.loads(plan_text(
        references="[]", extra="shuffle = false",
        middle="[[points]]\nb_on = 5\nb_ticks = 300\n\n[[points]]\nb_ticks = 450\n\n"
               "[[points]]\n"))
    points = series_module.expand(plan, two_knobs)
    assert [point.knobs for point in points] == [{"b_on": 5, "b_ticks": 300},
                                                 {"b_ticks": 450}, {}]
    assert plan.seed is None and not plan.shuffle
    assert series_module.describe(points[2]) == "point 3 (the defaults)"


@pytest.mark.parametrize(("text", "said"), [
    (plan_text(middle="[grid]\nb_ticks = [300]", extra="colour = 3"),
     "colour is not a key of a series plan"),
    (plan_text(middle="[grid]\nb_ticks = [300]\n[[points]]\nb_ticks = 400"),
     "one of the two"),
    (plan_text(middle=""), "one of the two"),
    (plan_text(middle="[grid]\nb_ticks = [300, 300]"), "grid.b_ticks repeats a value"),
    (plan_text(middle="[grid]\nb_ticks = []"), "grid.b_ticks must be a list of numbers"),
    (plan_text(middle="[grid]\nb_ticks = [300]", references='["middle"]'),
     "references must be a list"),
    (plan_text(middle="[grid]\nb_ticks = [300]", extra="shuffle = false\nseed = 4"),
     "a seed orders a shuffle"),
    (plan_text(middle="[grid]\nb_ticks = [300]", extra=f"seed = {2**31}"),
     "seed must be a whole number from 0 to 2147483647"),
    (plan_text(middle="[grid]\nb_ticks = [300]", replicates=0), "replicates counts files"),
    (plan_text(middle="[grid]\nb_ticks = [300]").replace("plan_schema = 1", "plan_schema = 2"),
     "plan_schema must be 1"),
    ("plan_schema = 1\n[grid\n", "not valid TOML"),
])
def test_a_plan_is_refused_for_anything_the_format_does_not_define(text, said):
    with pytest.raises(series_module.PlanError, match=said):
        series_module.loads(text)


def test_a_knob_the_template_does_not_have_is_refused_by_name(two_knobs):
    plan = series_module.loads(plan_text(middle="[grid]\nb_offf = [3]"))
    with pytest.raises(series_module.PlanError, match="no knob called b_offf"):
        series_module.expand(plan, two_knobs)


def test_the_series_job_and_its_result_cross_the_wire():
    for cls in (SeriesJob, SeriesResult):
        example = wire.example(cls)
        assert wire.loads(wire.dumps(example)) == example


def test_the_series_verb_takes_its_plan_bare():
    [verb] = [entry for entry in verbs() if entry.name == "series"]
    assert {flag.parameter for flag in verb.flags} == {
        "request", "initials", "plan", "conditions", "setup", "request_id"}


# --- refused whole ------------------------------------------------------------------


def test_a_point_outside_the_limits_or_the_budget_refuses_the_whole_plan(fake_owner,  # noqa: F811
                                                                           library, tmp_path):
    output = str(tmp_path / "runs")
    narrow = Toolbox(fake_owner, library=library, output=output, instrument=SIMULATED,
                     limits=limits(library, max_runs=5, high=450))
    asked = {"request": REQUEST, "initials": "zz"}
    issued = fake_owner.status().issued
    with pytest.raises(ToolFailure) as caught:
        narrow.call("series", {**asked, "plan": plan_text(
            replicates=3, middle="[grid]\nb_ticks = [300, 500]")})
    said = str(caught.value)
    assert said.startswith("the series was refused whole and nothing was sent")
    assert "the start reference (point 1, the defaults): b_ticks = 500" in said
    assert "point 3 (b_ticks = 500): b_ticks = 500" in said
    assert "point 2 (b_ticks = 300): 3 replicates is more than the 2" in said
    assert fake_owner.status().issued == issued, "nothing reached the owner"

    toolbox = Toolbox(fake_owner, library=library, output=output, instrument=SIMULATED,
                      limits=limits(library, max_runs=5))
    with pytest.raises(ToolFailure, match=r"the series is 8 acquisitions \(16 files\) and "
                                          r"this daemon session's budget has 5 of 5 left"):
        toolbox.call("series", {**asked, "plan": GRID})
    assert fake_owner.status().issued == issued

    toolbox.call("discover_boxes", {"template": "two-knob.toml", "labels": LABELS})
    small = plan_text(references="[]", middle="[grid]\nb_ticks = [300, 450]")
    started = toolbox.call("series", {**asked, "plan": small})
    assert (started["acquisitions"], started["files"]) == (2, 2)
    assert len(followed(toolbox, started["job"])["runs"]) == 2
    assert toolbox.call("status")["budget"]["acquisitions_made"] == 2
    with pytest.raises(ToolFailure, match="4 acquisitions .* has 3 of 5 left"):
        toolbox.call("series", {**asked, "plan": plan_text(
            middle="[grid]\nb_ticks = [300, 450]")})


# --- acquired -----------------------------------------------------------------------


def test_a_series_runs_in_a_daemon_as_one_job(library, tmp_path):
    made = Daemon(tmp_path)
    client = RemoteOwner(made.endpoint, timeout=10)
    try:
        toolbox = Toolbox(client, library=library, output=str(tmp_path),
                          instrument=SIMULATED)
        toolbox.call("discover_boxes", {"template": "two-knob.toml", "labels": LABELS})
        deadline = time.monotonic() + 20
        while "ready" not in toolbox.call("status", {})["console"]:
            assert time.monotonic() < deadline
            time.sleep(0.1)
        started = toolbox.call("series", {"request": REQUEST, "initials": "zz",
                                          "plan": plan_text(middle="[grid]\nb_ticks = [300]")})
        ended = followed(toolbox, started["job"])
        assert ended["result"]["points_done"] == 3 and len(ended["runs"]) == 3
        assert [entry["ClockworkSeriesIndex"] for entry in summed_stamps(str(tmp_path))] == (
            started["order"])
    finally:
        made.stop(client)
        client.close()


def test_every_file_of_a_shuffled_series_says_where_it_sat_and_what_shuffled_it(
        fake_owner, library, tmp_path, two_knobs):  # noqa: F811
    output = str(tmp_path / "runs")
    toolbox = Toolbox(fake_owner, library=library, output=output, instrument=SIMULATED)
    toolbox.call("discover_boxes", {"template": "two-knob.toml", "labels": LABELS})
    text = plan_text(replicates=2, extra="seed = 12345",
                     middle="[grid]\nb_ticks = [300, 400, 450]")
    started = toolbox.call("series", {"request": REQUEST, "initials": "zz", "plan": text,
                                      "conditions": "a rehearsal"})
    points = series_module.expand(series_module.loads(text), two_knobs)
    expected = [point.index for point in series_module.order(points, 12345)]
    assert started["order"] == expected and started["seed"] == 12345
    assert (started["points"], started["files"], started["acquisitions"]) == (5, 10, 5)

    ended = followed(toolbox, started["job"], timeout=240)
    assert ended["result"]["points_done"] == 5 and ended["result"]["stopped"] is None
    assert ended["series"]["order"] == expected
    assert len(ended["runs"]) == 10 and all(run["complete"] for run in ended["runs"])

    stamps = summed_stamps(output)
    assert [entry["ClockworkSeriesPosition"] for entry in stamps] == list(range(1, 11))
    assert [entry["ClockworkSeriesIndex"] for entry in stamps] == [
        index for index in expected for _ in range(2)]
    assert {entry["ClockworkSeriesSeed"] for entry in stamps} == {12345}
    assert {entry["ClockworkSeriesId"] for entry in stamps} == {started["request_id"]}
    knobs = {point.index: point.knobs.get("b_ticks", 500) for point in points}
    assert [entry["ClockworkKnobBTicks"] for entry in stamps] == [
        knobs[entry["ClockworkSeriesIndex"]] for entry in stamps]

    toolbox.recorder(started["job"]).join(60)
    record = record_of(started["record"])
    [plan] = record["plans"]
    assert (plan["seed"], plan["order"], plan["files"]) == (12345, expected, 10)
    assert plan["plan"] == text and len(plan["points"]) == 5
    assert len(record["files"]) == 10

    rows = toolbox.call("manifest")["rows"]
    assert sorted((row["series_position"], row["series_index"], row["series_seed"])
                  for row in rows) == [
        (position, index, 12345)
        for position, index in enumerate((index for index in expected for _ in range(2)),
                                         start=1)]


def test_a_point_the_boxes_already_hold_is_acquired_without_a_send(fake_owner, library,  # noqa: F811
                                                                    tmp_path):
    output = str(tmp_path / "runs")
    toolbox = Toolbox(fake_owner, library=library, output=output, instrument=SIMULATED)
    toolbox.call("discover_boxes", {"template": "two-knob.toml", "labels": LABELS})
    started = toolbox.call("series", {"request": REQUEST, "initials": "zz", "plan": plan_text(
        references="[]", extra="shuffle = false",
        middle="[[points]]\nb_ticks = 300\n[[points]]\nb_ticks = 300\n"
               "[[points]]\nb_ticks = 450\n")})
    assert started["seed"] is None and started["order"] == [1, 2, 3]
    events, last = [], 0
    while True:
        answer = toolbox.call("progress", {"job": started["job"], "after": last, "wait_s": 5})
        events += answer["events"]
        last = answer["last"]
        if answer["done"]:
            break
    assert [event["sends"] for event in events if event["kind"] == "PointStarted"] == [
        True, False, True]
    assert not any("ClockworkSeriesSeed" in stamps for stamps in summed_stamps(output))
    sent = sorted(name for name in os.listdir(output) if name.endswith(".sent.txt"))
    assert len(sent) == 3, "each file has its send log"
    with open(os.path.join(output, sent[1]), encoding="utf-8") as handle:
        assert "this is a replicate" in handle.read(), "the second says nothing was sent"


def test_a_stop_ends_the_series_and_the_record_says_where(fake_owner, library,  # noqa: F811
                                                          tmp_path):
    output = str(tmp_path / "runs")
    toolbox = Toolbox(fake_owner, library=library, output=output, instrument=SIMULATED)
    toolbox.call("discover_boxes", {"template": "two-knob.toml", "labels": LABELS})
    started = toolbox.call("series", {"request": REQUEST, "initials": "zz", "plan": plan_text(
        middle="[grid]\nb_ticks = [200, 300, 400, 450]")})
    deadline = time.monotonic() + 120
    while True:
        assert time.monotonic() < deadline
        answer = toolbox.call("progress", {"job": started["job"], "wait_s": 5})
        if any(event["kind"] == "RunDone" for event in answer["events"]):
            break
    assert toolbox.call("stop", {"reason": "the test has what it needs"})["stopping"]
    ended = followed(toolbox, started["job"])
    result = ended["result"]
    assert 1 <= result["points_done"] < 6 and result["stopped"]
    assert len(ended["runs"]) >= result["points_done"]
    positions = [entry["ClockworkSeriesPosition"] for entry in summed_stamps(output)]
    assert positions == list(range(1, len(positions) + 1))

    toolbox.recorder(started["job"]).join(60)
    notes = [note["text"] for note in record_of(started["record"])["notes"]]
    assert any(note.startswith(f"series job {started['job']} stopped after "
                               f"{result['points_done']} of 6 points") for note in notes), notes
