"""The standing envelope and the run record, as pure functions: one test per refusal,
the cold-start rules over read-backs built by hand, and the record's document (lab
record, task 71). The same checks driven through the tools over `--fake` are in
`test_mcp.py`."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json

import pytest

from clockwork import envelope, record
from clockwork.envelope import (
    HAND_WRITTEN,
    NO_LIMITS,
    Ledger,
    LimitsError,
    check,
    cold_start,
    judge,
)
from clockwork.method import BoxMethod, RfChannel
from clockwork.method import template as template_module
from clockwork.mips import BoxState

TEMPLATE = """\
template_schema = 1
renders = 2
start = [["box1", "TBLSTRT"]]
reset = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]

[knobs]
b_ticks = { default = 500, min = 100, max = 520, unit = "ticks", description = "line B high" }
level_v = { default = 5.0, min = 0.0, max = 20.0, unit = "V", description = "a level" }

[metadata]
name = "envelope-test"
created = 2026-09-24

[acquisition]
frames = 1
scans = 32
accumulations = 2
repetition_mode = "per_repetition"
keep_raw = true
file_stem = "envelope-test"
enable = { box = "box1", channel = "A" }

[[boxes]]
name = "box1"
port = "COM3"
setup = ["STBLCLK,EXT", "SDCB,2,{level_v}"]
load = ["STBLDAT;0:[A:1,0:A:1:B:1,{b_ticks}:B:0,532:A:0,533:];"]
arm = ["SMOD,TBL"]
"""

TEMPLATE_OBJECT = template_module.loads_template(TEMPLATE)


def limits_text(**budget: object) -> str:
    spend = {"max_runs": 3, "max_replicates_per_run": 2, "max_hours": 8, **budget}
    return f"""\
schema_version = 1
description = "a test instrument"

[allow]
boxes = ["box1"]

[budget]
{chr(10).join(f"{key} = {value}" for key, value in spend.items())}

[templates.line-b]
hash = "{TEMPLATE_OBJECT.hash[:12]}"
name = "envelope-test"

[templates.line-b.knobs]
b_ticks = {{ min = 200, max = 450 }}

[templates.line-b.fixed]
level_v = 5.0
"""


LIMITS = envelope.loads(limits_text())


def render(**knobs: float):
    return template_module.render(TEMPLATE_OBJECT, knobs, {})


# --- the document -------------------------------------------------------------------


def test_a_limits_document_loads_and_names_its_template_by_hash():
    entry = LIMITS.entry_for(TEMPLATE_OBJECT.hash)
    assert entry is not None and entry.key == "line-b"
    assert entry.fixed == (("level_v", 5.0),)
    assert LIMITS.cold_start == "refuse" and LIMITS.boxes == ("box1",)
    assert LIMITS.budget == envelope.Budget(3, 2, 8.0)
    assert LIMITS.against([TEMPLATE_OBJECT]) == []


def test_a_bad_limits_document_reports_every_problem_at_once():
    with pytest.raises(LimitsError) as caught:
        envelope.loads("""\
schema_version = 2
cold_start = "sometimes"
[budget]
max_runs = 0
max_replicates_per_run = 2
[templates.x]
hash = "abc"
[templates.x.knobs]
k = { min = 3, max = 1 }
""")
    problems = caught.value.problems
    joined = " | ".join(problems)
    for expected in ("schema_version", "cold_start", "[allow] is required", "max_runs",
                     "max_hours", "hash must be at least", "min 3 above max 1"):
        assert expected in joined, expected


def test_limits_narrow_a_template_and_never_widen_it():
    wide = envelope.loads(limits_text().replace("min = 200, max = 450", "min = 50")
                          .replace("level_v = 5.0", "level_v = 30.0\nghost = 1"))
    problems = wide.entry_for(TEMPLATE_OBJECT.hash).problems_against(TEMPLATE_OBJECT)
    assert any("below the template's own 100" in line for line in problems)
    assert any("fix level_v at 30.0, outside" in line for line in problems)
    assert any("'ghost' the template does not have" in line for line in problems)
    refused = check(render(b_ticks=300).method, render(b_ticks=300), wide, Ledger(),
                    real=True)
    assert len(refused) == 1 and "cannot be applied" in refused[0]


# --- the check, one refusal each -------------------------------------------------------


def test_a_render_inside_the_limits_is_refused_nothing():
    rendered = render(b_ticks=300)
    assert check(rendered.method, rendered, LIMITS, Ledger(), real=True, replicates=2) == []


def test_no_limits_refuses_the_instrument_and_nothing_in_a_rehearsal():
    rendered = render()
    assert check(rendered.method, rendered, None, Ledger(), real=True) == [NO_LIMITS]
    assert check(rendered.method, rendered, None, Ledger(), real=False) == []
    assert check(None, None, None, Ledger(), real=True) == [NO_LIMITS]


def test_a_template_the_limits_do_not_list_is_refused():
    other = template_module.loads_template(TEMPLATE.replace("envelope-test", "other"))
    rendered = template_module.render(other, {}, {})
    [refusal] = check(rendered.method, rendered, LIMITS, Ledger(), real=True)
    assert "other" in refusal and "is not in the standing limits" in refusal


def test_a_knob_outside_its_range_is_refused():
    rendered = render(b_ticks=500)
    [refusal] = check(rendered.method, rendered, LIMITS, Ledger(), real=True)
    assert refusal.startswith("b_ticks = 500 ticks is outside the standing limits")
    assert "200 to 450 ticks" in refusal


def test_a_fixed_knob_moved_is_refused():
    rendered = render(b_ticks=300, level_v=6.0)
    [refusal] = check(rendered.method, rendered, LIMITS, Ledger(), real=True)
    assert "level_v is fixed at 5.0 V" in refusal and "asked for 6.0" in refusal


def test_a_box_the_limits_do_not_allow_is_refused():
    rendered = render(b_ticks=300)
    moved = dataclasses.replace(rendered.method, boxes=(
        *rendered.method.boxes, BoxMethod(name="box9", port="COM9")))
    [refusal] = check(moved, rendered, LIMITS, Ledger(), real=True)
    assert "do not allow box9" in refusal


def test_the_budget_refuses_run_n_plus_one_too_many_replicates_and_too_long():
    rendered = render(b_ticks=300)
    began = dt.datetime(2026, 9, 24, 8, 0)
    assert check(rendered.method, rendered, LIMITS, Ledger(runs=2), real=True) == []
    [runs] = check(rendered.method, rendered, LIMITS, Ledger(runs=3), real=True)
    assert "made its 3 acquisitions" in runs and "restarting clockwork serve" in runs
    [many] = check(rendered.method, rendered, LIMITS, Ledger(), real=True, replicates=3)
    assert "3 replicates is more than the 2" in many
    late = Ledger(started=began, now=began + dt.timedelta(hours=8, minutes=6))
    [hours] = check(rendered.method, rendered, LIMITS, late, real=True)
    assert "started 8.1 h ago" in hours
    assert check(None, None, LIMITS, Ledger(runs=3), real=True) == [runs]


def test_a_hand_written_method_is_refused_against_the_instrument_only():
    method = render(b_ticks=300).method
    assert check(method, None, LIMITS, Ledger(), real=True) == [HAND_WRITTEN]
    assert check(method, None, LIMITS, Ledger(), real=False) == []


# --- the cold-start check --------------------------------------------------------------


def state(name: str, **values: str) -> BoxState:
    return BoxState(name=name, values={key.replace("_", ","): value
                                       for key, value in values.items()})


def box(name: str = "box1", **fields: object) -> BoxMethod:
    return BoxMethod(name=name, port="COM3", **fields)


def one(method_box: BoxMethod, read: BoxState, **options: bool):
    method = dataclasses.replace(render().method, boxes=(method_box,))
    return cold_start(method, [read], **options)


def test_an_undeclared_dc_bias_channel_is_a_finding_at_zero_volts_too():
    read = state("box1", GDCBALL="99.00,0.00,0.00")
    declared = box(dc_bias=((1, 99.0), (3, 0.0)))
    [finding] = one(declared, read)
    assert finding.setting == "dc_bias" and finding.depends
    assert finding.text == ("box1 DC bias channel is not declared by the method and holds "
                            "2: 0.00 V")
    assert one(box(setup=("SDCB,2,0",), dc_bias=((1, 99.0), (3, 0.0))), read) == []
    assert one(box(setup=("SDCBALL,1,2,3",)), read) == []
    [both] = one(box(), read)
    assert "channels are not declared" in both.text and "1: 99.00 V" in both.text


def test_an_rf_head_counts_only_when_it_is_on():
    phantom = state("box1", GRFALL="1000000,0.00,0.00,0.00,1000000,0.00,0.00,0.00")
    assert one(box(), phantom) == []
    on = state("box1", GRFALL="943000,50.00,0,0,804000,0.00,0,0")
    [finding] = one(box(), on)
    assert finding.setting == "rf" and finding.depends
    assert finding.text == ("box1 RF 1 is on, 943000 Hz at 50.00% drive, and the method "
                            "does not declare it")
    assert one(box(rf=(RfChannel(channel=1, drive_pct=50.0),)), on) == []
    assert one(box(setup=("SRFDRV,1,50",)), on) == []


def test_the_arb_setup_block_refuses_and_the_rest_of_a_module_cautions():
    read = state("box2", GWFREQ_1="10019", GWFVRNG_1="10.00", GWFDIR_1="FWD",
                 GWFDIR_2="REV", GWFREQ_2="15000", GWFVRNG_2="15.00")
    declared = box("box2", setup=("SWFREQ,2,15000", "SWFVRNG,2,15", "SWFDIR,1,FWD"))
    found = {finding.setting: finding for finding in one(declared, read)}
    assert set(found) == {"GWFREQ", "GWFVRNG", "GWFDIR"}
    assert found["GWFREQ"].depends and found["GWFVRNG"].depends
    assert not found["GWFDIR"].depends
    assert found["GWFDIR"].text == ("box2 WFDIR is left as found on module 2: REV, and "
                                    "the method declares it on module 1")


def test_a_box_not_read_back_is_a_finding():
    [finding] = cold_start(render().method, [])
    assert finding.setting == "unread" and finding.depends


def test_after_a_send_a_declared_value_the_box_does_not_hold_is_a_finding():
    read = state("box1", GDCBALL="99.00,5.00", GDCBALLV="99.00,9.00", GTBLSTA="IDLE")
    declared = box(dc_bias=((1, 50.0), (2, 5.0)))
    assert one(declared, read) == []
    found = one(declared, read, declared=True)
    assert [(finding.depends, finding.text) for finding in found] == [
        (True, "box1 DC bias 1 was declared 50.00 V and reads back 99.00 V"),
        (False, "box1 DC bias 2 is set to 5.00 V and monitors 9.00 V"),
    ]


def test_judge_splits_findings_by_mode():
    findings = [envelope.Finding("b", "dc_bias", "depends", True),
                envelope.Finding("b", "GWFDIR", "rest", False)]
    assert judge(findings, "refuse") == (["depends"], ["rest"])
    assert judge(findings, "strict") == (["depends", "rest"], [])
    assert judge(findings, "caution") == ([], ["depends", "rest"])
    per_template = envelope.loads(limits_text().replace(
        'name = "envelope-test"', 'name = "envelope-test"\ncold_start = "strict"'))
    assert per_template.mode_for(render()) == "strict"
    assert per_template.mode_for(None) == "refuse"


def test_the_default_limits_sit_beside_the_instrument_document(tmp_path):
    instrument = tmp_path / "slim.instrument.toml"
    assert envelope.default_path(str(instrument)) == str(tmp_path / envelope.LIMITS_NAME)
    assert envelope.default_path("") == ""


# --- the run record --------------------------------------------------------------------


def test_a_run_record_is_one_document_per_request_found_again_by_its_id(tmp_path):
    first = record.RunRecord.open(str(tmp_path), "260924_ZZ_001", request_id="r-1",
                                  text="run it once", initials="ZZ")
    first.append("plans", {"text": "one run at the defaults"})
    first.add_file({"stem": "260924_ZZ_001", "complete": False})
    first.add_file({"stem": "260924_ZZ_001", "complete": True})
    again = record.RunRecord.open(str(tmp_path), "260924_ZZ_009", request_id="r-1",
                                  text="run it once")
    assert again.path == first.path == str(tmp_path / "260924_ZZ_001.request.json")
    other = record.RunRecord.open(str(tmp_path), "260924_ZZ_001", request_id="r-2",
                                  text="something else")
    assert other.path != first.path and "r-2" in other.path
    data = json.loads((tmp_path / "260924_ZZ_001.request.json").read_text(encoding="utf-8"))
    assert data["request"] == {**data["request"], "id": "r-1", "text": "run it once",
                               "source": "agent", "initials": "ZZ"}
    assert [entry["text"] for entry in data["plans"]] == ["one run at the defaults"]
    assert [entry["complete"] for entry in data["files"]] == [True]
    assert record.find(str(tmp_path), "r-2") == other.path
    assert b"\r\n" not in (tmp_path / "260924_ZZ_001.request.json").read_bytes()
    with pytest.raises(ValueError):
        first.append("gossip", {})
    with pytest.raises(ValueError):
        record.RunRecord.open(str(tmp_path), "x", request_id="r-3", text="", source="bot")
