"""The standing envelope and the run record, as pure functions: one test per refusal,
the cold-start rules over read-backs built by hand, and the record's document (lab
record, task 71). The same checks driven through the tools over `--fake` are in
`test_mcp.py`."""

from __future__ import annotations

import dataclasses
import json

import pytest

from clockwork import envelope, record
from clockwork.envelope import (
    HAND_WRITTEN,
    NO_LIMITS,
    LimitsError,
    check,
    cold_start,
    combine,
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


def limits_text() -> str:
    return f"""\
schema_version = 2
description = "a test instrument"

[allow]
boxes = ["box1"]

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
    assert entry.origin == "standing" and LIMITS.project == ""
    assert LIMITS.against([TEMPLATE_OBJECT]) == []


def test_a_bad_limits_document_reports_every_problem_at_once():
    with pytest.raises(LimitsError) as caught:
        envelope.loads("""\
schema_version = 3
cold_start = "sometimes"
[templates.x]
hash = "abc"
[templates.x.knobs]
k = { min = 3, max = 1 }
""")
    problems = caught.value.problems
    joined = " | ".join(problems)
    for expected in ("schema_version must be 2", "cold_start", "[allow] is required",
                     "hash must be at least", "min 3 above max 1"):
        assert expected in joined, expected


def test_a_schema_1_file_is_refused_with_why_its_budget_and_switch_went():
    old = limits_text().replace("schema_version = 2", "schema_version = 1").replace(
        'boxes = ["box1"]\n', 'boxes = ["box1"]\nhand_written = true\n\n[budget]\n'
        "max_runs = 3\nmax_replicates_per_run = 2\nmax_hours = 8\n")
    with pytest.raises(LimitsError) as caught:
        envelope.loads(old)
    [budget, version, switch] = caught.value.problems
    assert version.startswith("schema_version is 1, the shape with a [budget]")
    assert switch == envelope.RETIRED["hand_written"]
    assert budget == envelope.RETIRED["budget"]


def test_limits_narrow_a_template_and_never_widen_it():
    wide = envelope.loads(limits_text().replace("min = 200, max = 450", "min = 50")
                          .replace("level_v = 5.0", "level_v = 30.0\nghost = 1"))
    problems = wide.entry_for(TEMPLATE_OBJECT.hash).problems_against(TEMPLATE_OBJECT)
    assert any("below the template's own 100" in line for line in problems)
    assert any("fix level_v at 30.0, outside" in line for line in problems)
    assert any("'ghost' the template does not have" in line for line in problems)
    refused = check(render(b_ticks=300).method, render(b_ticks=300), wide, real=True)
    assert len(refused) == 1 and "cannot be applied" in refused[0]


# --- the check, one refusal each -------------------------------------------------------


def test_a_render_inside_the_limits_is_refused_nothing():
    rendered = render(b_ticks=300)
    assert check(rendered.method, rendered, LIMITS, real=True) == []


def test_no_limits_refuses_the_instrument_and_nothing_in_a_rehearsal():
    rendered = render()
    assert check(rendered.method, rendered, None, real=True) == [NO_LIMITS]
    assert check(rendered.method, rendered, None, real=False) == []
    assert check(None, None, None, real=True) == [NO_LIMITS]
    assert check(None, None, LIMITS, real=True) == []


def test_a_template_the_limits_do_not_list_is_refused():
    other = template_module.loads_template(TEMPLATE.replace("envelope-test", "other"))
    rendered = template_module.render(other, {}, {})
    [refusal] = check(rendered.method, rendered, LIMITS, real=True)
    assert "other" in refusal and "is not in the limits in force" in refusal


def test_a_knob_outside_its_range_is_refused():
    rendered = render(b_ticks=500)
    [refusal] = check(rendered.method, rendered, LIMITS, real=True)
    assert refusal.startswith("b_ticks = 500 ticks is outside the standing limits")
    assert "200 to 450 ticks" in refusal


def test_a_fixed_knob_moved_is_refused():
    rendered = render(b_ticks=300, level_v=6.0)
    [refusal] = check(rendered.method, rendered, LIMITS, real=True)
    assert "level_v is fixed at 5.0 V" in refusal and "asked for 6.0" in refusal


LINE_TEMPLATE = TEMPLATE.replace(
    'level_v = { default = 5.0, min = 0.0, max = 20.0, unit = "V", description = "a level" }',
    'compress_order = { default = 4, min = 1, max = 64, unit = "", description = "CRIMP order" }\n'
    'squeeze_ms = { default = 16, min = 1, max = 100, unit = "ms", description = "line high" }\n'
    'reverse_ms = { default = 12.0, min = 1.0, max = 100.0, unit = "ms", description = "play" }',
).replace(
    'setup = ["STBLCLK,EXT", "SDCB,2,{level_v}"]\n'
    'load = ["STBLDAT;0:[A:1,0:A:1:B:1,{b_ticks}:B:0,532:A:0,533:];"]',
    'setup = ["SARBCPEX,3,TRUE", "SALTPLY,1,{reverse_ms}"]\n'
    'load = ["SARBCTBL,J3{compress_order}[HRc{squeeze_ms}n1C]1"]',
)
LINE_OBJECT = template_module.loads_template(LINE_TEMPLATE)


def line_limits(fixed: str = "squeeze_ms = 16") -> envelope.Limits:
    return envelope.loads(limits_text().replace(
        "b_ticks = { min = 200, max = 450 }",
        "compress_order = { min = 1, max = 16 }\nreverse_ms = { min = 8.0, max = 16.0 }",
    ).replace("level_v = 5.0", fixed).replace(TEMPLATE_OBJECT.hash[:12], LINE_OBJECT.hash[:12]))


def test_the_compress_line_knobs_are_bounded_and_fixed_like_any_other():
    limits = line_limits()
    inside = template_module.render(LINE_OBJECT, {"compress_order": 4, "b_ticks": 300}, {})
    assert check(inside.method, inside, limits, real=True) == []
    order = template_module.render(LINE_OBJECT, {"compress_order": 64, "b_ticks": 300}, {})
    [refusal] = check(order.method, order, limits, real=True)
    assert "compress_order = 64 is outside the standing" in refusal and "1 to 16" in refusal
    window = template_module.render(LINE_OBJECT, {"squeeze_ms": 20, "b_ticks": 300}, {})
    [refusal] = check(window.method, window, limits, real=True)
    assert "squeeze_ms is fixed at 16 ms" in refusal and "asked for 20" in refusal


def test_a_whole_millisecond_knob_cannot_be_fixed_at_a_fraction():
    [problem] = line_limits("squeeze_ms = 16.5").against([LINE_OBJECT])
    assert "fix squeeze_ms at 16.5 and it takes whole values" in problem


def test_a_box_the_limits_do_not_allow_is_refused():
    rendered = render(b_ticks=300)
    moved = dataclasses.replace(rendered.method, boxes=(
        *rendered.method.boxes, BoxMethod(name="box9", port="COM9")))
    [refusal] = check(moved, rendered, LIMITS, real=True)
    assert "do not allow box9" in refusal


BOUNDED = """
[electrodes]
max_v = 100.0
nets = [
  { name = "LEVEL", dc = { box = "box1", channel = 2 } },
  { name = "GROUND", volts = 0.0 },
]

[[rf_drive]]
box = "box1"
channel = 1
max_pct = 50.0
"""
"""The potentials bounded: what admits a hand-written method against the instrument."""


def test_a_hand_written_method_is_refused_where_the_potentials_are_not_bounded():
    method = render(b_ticks=300).method
    assert not LIMITS.hand_written
    assert check(method, None, LIMITS, real=True) == [HAND_WRITTEN]
    assert check(method, None, LIMITS, real=False) == []


def test_bounded_potentials_let_a_hand_written_method_through_held_to_them():
    limits = envelope.loads(limits_text() + BOUNDED)
    assert limits.hand_written
    method = render(b_ticks=500).method
    # b_ticks 500 is outside the template entry's range: a hand-written method is not
    # held to any template's knobs, only to the boxes and the potentials.
    assert check(method, None, limits, real=True) == []
    renamed = dataclasses.replace(method, boxes=(dataclasses.replace(
        method.boxes[0], name="box9"),))
    [boxed] = check(renamed, None, limits, real=True)
    assert "do not allow box9" in boxed
    tight = envelope.loads(limits_text() + BOUNDED.replace("max_v = 100.0", "max_v = 10.0"))
    [spread] = check(render(level_v=20.0).method, None, tight, real=True)
    assert spread.startswith("LEVEL reaches 20 V and GROUND 0 V")


def test_allow_hand_written_is_refused_with_why_it_went():
    with pytest.raises(LimitsError) as caught:
        envelope.loads(limits_text().replace('boxes = ["box1"]\n',
                                             'boxes = ["box1"]\nhand_written = true\n'))
    assert caught.value.problems == [envelope.RETIRED["hand_written"]]


# --- the caps ---------------------------------------------------------------------------

CAPS = """
[caps]
level_v = { max = 10.0 }
b_ticks = { min = 150 }
"""


def test_a_cap_refuses_a_render_its_entry_leaves_open():
    limits = envelope.loads(limits_text().replace("[templates.line-b.fixed]\nlevel_v = 5.0\n",
                                                  "") + CAPS)
    assert [cap.name for cap in limits.caps] == ["level_v", "b_ticks"]
    inside = render(b_ticks=300, level_v=10.0)
    assert check(inside.method, inside, limits, real=True) == []
    rendered = render(b_ticks=300, level_v=12.0)
    [refusal] = check(rendered.method, rendered, limits, real=True)
    assert refusal == ("level_v = 12.0 V is outside the standing cap on level_v, at most "
                       "10.0 V, which holds for every template")
    # A knob its entry already refuses is not refused twice.
    [once] = check(render(b_ticks=120).method, render(b_ticks=120), limits, real=True)
    assert "outside the standing limits for envelope-test" in once


def test_an_entry_past_a_cap_is_a_problem_in_the_limits():
    with pytest.raises(LimitsError) as caught:
        envelope.loads(limits_text().replace("level_v = 5.0", "level_v = 15.0")
                       + CAPS.replace("b_ticks = { min = 150 }", "b_ticks = { min = 250 }"))
    assert caught.value.problems == [
        "limits for template 'line-b' fix level_v at 15.0, outside the standing cap of at "
        "most 10.0",
        "limits for template 'line-b' put b_ticks's minimum at 200, below the standing cap "
        "of 250"]


# --- a project's limits ------------------------------------------------------------------

OTHER = template_module.loads_template(TEMPLATE.replace("envelope-test", "project-own"))


def project_text(body: str) -> str:
    return f'schema_version = 2\ndescription = "a project"\n{body}'


def narrowing(knobs: str = "", fixed: str = "", extra: str = "") -> str:
    text = f'[templates.mine]\nhash = "{TEMPLATE_OBJECT.hash[:12]}"\n{extra}'
    if knobs:
        text += f"[templates.mine.knobs]\n{knobs}\n"
    if fixed:
        text += f"[templates.mine.fixed]\n{fixed}\n"
    return project_text(text)


def test_a_project_admits_its_own_template_without_touching_the_standing_file():
    rendered = template_module.render(OTHER, {"b_ticks": 300}, {})
    [absent] = check(rendered.method, rendered, LIMITS, real=True)
    assert "is not in the limits in force" in absent
    project = envelope.loads(project_text(
        f'[templates.own]\nhash = "{OTHER.hash[:12]}"\n'
        "[templates.own.knobs]\nb_ticks = { max = 400 }\n"), "p.toml", project=True)
    combined = combine(LIMITS, project)
    assert combined.project == "p.toml" and combined.project_description == "a project"
    assert combined.boxes == LIMITS.boxes
    assert combined.entry_for(OTHER.hash).origin == "project"
    assert check(rendered.method, rendered, combined, real=True) == []
    over = template_module.render(OTHER, {"b_ticks": 450}, {})
    [refusal] = check(over.method, over, combined, real=True)
    assert refusal.startswith("b_ticks = 450 ticks is outside the project's limits for "
                              "project-own, at most 400 ticks")
    # The standing entry is untouched.
    assert check(render(b_ticks=300).method, render(b_ticks=300), combined, real=True) == []


def test_a_project_narrows_a_standing_entry_and_keeps_what_it_leaves_open():
    combined = combine(LIMITS, envelope.loads(
        narrowing("b_ticks = { max = 300 }"), project=True))
    entry = combined.entry_for(TEMPLATE_OBJECT.hash)
    assert entry.origin == "project" and entry.key == "mine"
    assert [(limit.min, limit.max) for limit in entry.knobs] == [(200, 300)]
    assert entry.fixed == (("level_v", 5.0),)
    [refusal] = check(render(b_ticks=350).method, render(b_ticks=350), combined, real=True)
    assert "outside the project's limits" in refusal and "200 to 300 ticks" in refusal
    fixed = combine(LIMITS, envelope.loads(narrowing(fixed="b_ticks = 250"), project=True))
    assert dict(fixed.entry_for(TEMPLATE_OBJECT.hash).fixed) == {"level_v": 5.0,
                                                                  "b_ticks": 250}
    assert fixed.entry_for(TEMPLATE_OBJECT.hash).knobs == ()


def test_a_project_that_would_widen_the_standing_limits_is_refused_whole():
    with pytest.raises(LimitsError) as caught:
        combine(LIMITS, envelope.loads(narrowing(
            "b_ticks = { min = 100, max = 500 }", "", 'cold_start = "caution"\n'),
            project=True))
    assert caught.value.problems == [
        "the project's limits for template 'mine' set cold_start 'caution', looser than "
        "the standing 'refuse'",
        "the project's limits for template 'mine' put b_ticks's minimum at 100, below the "
        "standing 200: a project narrows and never widens",
        "the project's limits for template 'mine' put b_ticks's maximum at 500, above the "
        "standing 450: a project narrows and never widens"]
    with pytest.raises(LimitsError) as caught:
        combine(LIMITS, envelope.loads(narrowing("level_v = { max = 6.0 }"), project=True))
    assert "bound level_v, which the standing limits fix at 5.0" in caught.value.problems[0]
    with pytest.raises(LimitsError) as caught:
        combine(LIMITS, envelope.loads(narrowing(fixed="level_v = 6.0"), project=True))
    assert caught.value.problems == [
        "the project's limits for template 'mine' fix level_v at 6.0 and the standing "
        "limits fix it at 5.0"]
    with pytest.raises(LimitsError) as caught:
        combine(LIMITS, envelope.loads(narrowing(fixed="b_ticks = 500"), project=True))
    assert "fix b_ticks at 500, outside the standing 200 to 450" in caught.value.problems[0]


def test_a_project_is_held_to_the_standing_caps():
    capped = envelope.loads(limits_text() + CAPS)
    with pytest.raises(LimitsError) as caught:
        combine(capped, envelope.loads(project_text(
            f'[templates.own]\nhash = "{OTHER.hash[:12]}"\n'
            "[templates.own.fixed]\nlevel_v = 12.0\n"), project=True))
    assert caught.value.problems == [
        "the project's limits for template 'own' fix level_v at 12.0, outside the standing "
        "cap of at most 10.0"]


def test_a_project_file_carries_nothing_of_the_instruments():
    with pytest.raises(LimitsError) as caught:
        envelope.loads(project_text(
            'cold_start = "strict"\n[allow]\nboxes = ["box9"]\n[caps]\nk = { max = 1 }\n'
            "[budget]\nmax_runs = 9\n[electrodes]\nmax_v = 1\n"), project=True)
    problems = caught.value.problems
    assert envelope.RETIRED["budget"] in problems
    carried = [line for line in problems if "a project's limits may not carry it" in line]
    assert [line.split(" ")[0] for line in carried] == [
        "cold_start", "allow", "caps", "electrodes"]
    assert len(problems) == 5


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
