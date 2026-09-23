"""Method templates: load, validate, render, and what the wire format requires of a hole."""

import tomllib

import pytest

from clockwork import method
from clockwork.acq import loop
from clockwork.method import template
from test_method import SAMPLE

# `SAMPLE` (test_method.py) with holes cut where a knob reaches: the table's cycle count and
# its two time points on box1, the wait in box2's compression table. Rendered at its defaults
# it is SAMPLE string for string, which is the anchoring test every template gets.
TEMPLATE = """\
template_schema = 1
renders = 2

start = [["box2", "TARBTRG"], ["box1", "TBLSTRT"]]
reset = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]

[knobs]
pulse_ms = { default = 1.5, min = 0.5, max = 5.0, unit = "ms", description = "line A high" }
cycles = { default = 10, min = 1, max = 100, unit = "", description = "table repeats" }
wait_ms = { default = 10.0, min = 1.0, max = 50.0, unit = "ms", description = "compression wait" }

[labels]
sample = { required = true, description = "what was sprayed" }

[constants]
tick_us = 100.0
on_tick = 10

[derive]
off_tick = "on_tick + round(pulse_ms * 1000 / tick_us)"

[marks]
off = { ms = "off_tick * tick_us / 1000", description = "line A falls" }

[metadata]
name = "smoke-test"
created = 2026-09-06
description = "A minimal two-box method for tests."

[acquisition]
frames = 1
scans = 100
accumulations = 10
repetition_mode = "per_repetition"
keep_raw = true
file_stem = "smoke-test"

[[boxes]]
name = "box1"
port = "COM3"
setup = ["STBLCLK,EXT"]
load = ["STBLDAT;25:[A:{cycles},{on_tick}:A:1,{off_tick}:A:0,100:];"]
arm = ["SMOD,TBL"]

[[boxes]]
name = "box2"
port = "COM4"
setup = ["SWFREQ,1,15000"]
load = ["STWCTBL,J22[HRsm2CD5m2ND{wait_ms}r]10"]
arm = []
"""

LABELS = {"sample": "polyalanine"}


def loads(text: str = TEMPLATE, **replacements: str) -> template.Template:
    for old, new in replacements.items():
        assert old in text, old
        text = text.replace(old, new)
    return template.loads_template(text)


def problems_of(exc_info) -> str:
    return "\n".join(exc_info.value.problems)


# --- the anchor -------------------------------------------------------------------------


def test_renders_at_defaults_to_the_method_it_came_from():
    t = loads()
    rendered = template.render(t, labels=LABELS)
    plain = method.loads(SAMPLE)
    assert rendered.method == plain
    assert method.stamp(rendered.method)["method_hash"] == method.stamp(plain)["method_hash"]
    assert rendered.knobs == {"pulse_ms": 1.5, "cycles": 10, "wait_ms": 10.0}
    assert rendered.derived == {"off_tick": 25}
    assert rendered.labels == LABELS
    assert rendered.tick_us == 100.0
    assert rendered.template_hash == t.hash
    assert rendered.template_text == TEMPLATE


def test_the_loops_refusals_and_cautions_do_not_change_on_a_rendered_method():
    rendered = template.render(loads(), labels=LABELS).method
    plain = method.loads(SAMPLE)
    assert loop.refusals(rendered) == loop.refusals(plain)
    assert loop.cautions(rendered) == loop.cautions(plain)


def test_turning_a_knob_moves_every_hole_it_reaches():
    rendered = template.render(loads(), {"pulse_ms": 3.0, "cycles": 4, "wait_ms": 12.5}, LABELS)
    assert rendered.method.boxes[0].load == ("STBLDAT;25:[A:4,10:A:1,40:A:0,100:];",)
    assert rendered.method.boxes[1].load == ("STWCTBL,J22[HRsm2CD5m2ND12.5r]10",)
    assert rendered.derived == {"off_tick": 40}


def test_a_mark_is_milliseconds_and_an_expected_scan():
    rendered = template.render(loads(), labels=LABELS)
    (mark,) = rendered.marks
    assert mark.name == "off" and mark.ms == 2.5 and mark.scan == 25
    assert mark.description == "line A falls"


def test_the_hash_does_not_depend_on_line_endings():
    assert loads(TEMPLATE.replace("\n", "\r\n")).hash == loads().hash


# --- knobs and labels -------------------------------------------------------------------


def test_a_knob_outside_its_range_is_refused_naming_the_range():
    with pytest.raises(template.TemplateError) as exc_info:
        template.render(loads(), {"pulse_ms": 7.5}, LABELS)
    assert "knob 'pulse_ms' = 7.5 ms is outside the range" in problems_of(exc_info)
    assert "0.5 to 5 ms" in problems_of(exc_info)


def test_the_range_is_inclusive_at_both_ends():
    t = loads()
    template.render(t, {"pulse_ms": 0.5}, LABELS)
    template.render(t, {"pulse_ms": 5.0}, LABELS)


def test_an_unknown_knob_is_refused_listing_the_real_ones():
    with pytest.raises(template.TemplateError) as exc_info:
        template.render(loads(), {"duration_ms": 200.0}, LABELS)
    assert "no such knob (it has cycles, pulse_ms, wait_ms)" in problems_of(exc_info)


def test_an_integer_knob_takes_whole_numbers_only():
    t = loads()
    assert t.knob("cycles").integer and not t.knob("pulse_ms").integer
    with pytest.raises(template.TemplateError) as exc_info:
        template.render(t, {"cycles": 2.5}, LABELS)
    assert "takes whole numbers, got 2.5" in problems_of(exc_info)
    rendered = template.render(t, {"cycles": 3.0}, LABELS)
    assert rendered.knobs["cycles"] == 3 and rendered.method.boxes[0].load[0].startswith(
        "STBLDAT;25:[A:3,")


def test_a_default_outside_its_own_range_is_refused_at_load():
    with pytest.raises(template.TemplateError) as exc_info:
        loads(**{"default = 1.5, min = 0.5": "default = 0.1, min = 0.5"})
    assert "the default 0.1 is not within min 0.5 to max 5.0" in problems_of(exc_info)


def test_a_required_label_must_be_given_and_renders_nothing():
    t = loads()
    with pytest.raises(template.TemplateError) as exc_info:
        template.render(t)
    assert "label 'sample': required, and not given" in problems_of(exc_info)
    with pytest.raises(template.TemplateError):
        template.render(t, labels={"sample": "   "})
    with pytest.raises(template.TemplateError) as exc_info:
        template.render(t, labels={"sample": "x", "operator": "y"})
    assert "label 'operator': this template has no such label" in problems_of(exc_info)
    assert (template.render(t, labels={"sample": "a"}).method
            == template.render(t, labels={"sample": "b"}).method)


def test_a_derivation_may_not_use_a_label():
    with pytest.raises(template.TemplateError) as exc_info:
        loads(**{'off_tick = "on_tick + round(pulse_ms * 1000 / tick_us)"':
                 'off_tick = "on_tick + sample"'})
    assert "'sample' is a label, and labels render nothing" in problems_of(exc_info)


# --- the vocabulary ---------------------------------------------------------------------


def test_parentheses_group_and_precedence_is_the_usual_one():
    t = loads(**{'off_tick = "on_tick + round(pulse_ms * 1000 / tick_us)"':
                 'off_tick = "(on_tick + 2) * 3 - 1"\nplain = "on_tick + 2 * 3 - 1"'})
    rendered = template.render(t, labels=LABELS)
    assert rendered.derived == {"off_tick": 35, "plain": 15}


def test_round_is_to_nearest_with_halves_away_from_zero():
    assert template.round_half_away(2.5) == 3
    assert template.round_half_away(-2.5) == -3
    assert template.round_half_away(2.4999) == 2
    assert template.round_half_away(7) == 7


def test_whole_numbers_stay_whole_and_division_gives_a_fraction():
    t = loads(**{'off_tick = "on_tick + round(pulse_ms * 1000 / tick_us)"':
                 'off_tick = "on_tick + 15"\nhalf = "on_tick / 5"'})
    derived = template.render(t, labels=LABELS).derived
    assert derived["off_tick"] == 25 and isinstance(derived["off_tick"], int)
    assert derived["half"] == 2.0 and isinstance(derived["half"], float)


def test_derivations_may_be_written_in_any_order():
    t = loads(**{'off_tick = "on_tick + round(pulse_ms * 1000 / tick_us)"':
                 'off_tick = "on_tick + width_ticks"\n'
                 'width_ticks = "round(pulse_ms * 1000 / tick_us)"'})
    assert template.render(t, labels=LABELS).method == method.loads(SAMPLE)


def test_a_cycle_is_refused_naming_the_derivations_on_it():
    with pytest.raises(template.TemplateError) as exc_info:
        loads(**{'off_tick = "on_tick + round(pulse_ms * 1000 / tick_us)"':
                 'off_tick = "back + 1"\nback = "off_tick + 1"'})
    assert "uses itself" in problems_of(exc_info)
    assert "off_tick -> back -> off_tick" in problems_of(exc_info)


@pytest.mark.parametrize("expression, complaint", [
    ('"on_tick ^ 2"', "not part of the vocabulary"),
    ('"floor(pulse_ms)"', "round() is the only one"),
    ('"round"', "needs an argument"),
    ('"on_tick + nowhere"', "no knob, constant or derivation is named 'nowhere'"),
    ('"(on_tick + 1"', "expected ')'"),
    ('""', "is empty"),
])
def test_an_expression_outside_the_vocabulary_is_refused_at_load(expression, complaint):
    with pytest.raises(template.TemplateError) as exc_info:
        loads(**{'"on_tick + round(pulse_ms * 1000 / tick_us)"': expression})
    assert complaint in problems_of(exc_info)


def test_division_by_zero_is_refused():
    with pytest.raises(template.TemplateError) as exc_info:
        loads(**{'off_tick = "on_tick + round(pulse_ms * 1000 / tick_us)"':
                 'off_tick = "round(on_tick / (pulse_ms - 1.5))"'})
    assert "divides by zero" in problems_of(exc_info)


# --- how a hole is written --------------------------------------------------------------


@pytest.mark.parametrize("value, written", [
    (208, "208"), (208.0, "208"), (208.00000000000003, "208"), (37.5, "37.5"),
    (16.7628, "16.7628"), (16.76284, "16.7628"), (-0.0, "0"), (0.00001, "0"), (-20, "-20"),
])
def test_a_number_is_written_in_its_shortest_form_at_four_decimals(value, written):
    assert template.format_number(value) == written


def test_a_time_point_count_that_is_not_whole_is_refused_asking_for_round():
    t = loads(**{'off_tick = "on_tick + round(pulse_ms * 1000 / tick_us)"':
                 'off_tick = "on_tick + pulse_ms * 1000 / tick_us"'})
    with pytest.raises(template.TemplateError) as exc_info:
        template.render(t, {"pulse_ms": 1.55}, LABELS)
    text = problems_of(exc_info)
    assert "{off_tick} is a time point's count, in ticks and must be a whole number" in text
    assert "comes to 25.5; round() its derivation" in text


def test_a_negative_count_is_refused_because_it_would_be_dynamic():
    with pytest.raises(template.TemplateError) as exc_info:
        loads(**{"on_tick = 10": "on_tick = -5"})
    assert "a negative count makes the time point dynamic" in problems_of(exc_info)


def test_a_cycle_count_must_be_whole_and_a_loop_count_at_least_one():
    whole, fractional = ("cycles = { default = 10, min = 1, max = 100",
                         "cycles = { default = 10.5, min = 1.0, max = 100.0")
    with pytest.raises(template.TemplateError) as exc_info:
        loads(**{whole: fractional})
    assert "{cycles} is a table's cycle count and must be a whole number" in problems_of(exc_info)
    with pytest.raises(template.TemplateError) as exc_info:
        loads(**{"[HRsm2CD5m2ND{wait_ms}r]10": "[HRsm2CD5m2ND{wait_ms}r]{loops}",
                 "[constants]": "[constants]\nloops = 0"})
    assert "{loops} is a compression table's loop count and comes to 0" in problems_of(exc_info)


def test_a_compression_table_number_may_not_be_negative():
    t = loads(**{"wait_ms = { default = 10.0, min = 1.0": "wait_ms = { default = 10.0, min = -1.0"})
    with pytest.raises(template.TemplateError) as exc_info:
        template.render(t, {"wait_ms": -1.0}, LABELS)
    assert "the mini-language's numbers start with a digit" in problems_of(exc_info)


def test_a_hole_outside_a_command_string_or_with_arithmetic_inside_is_refused():
    with pytest.raises(template.TemplateError) as exc_info:
        loads(**{'description = "A minimal two-box method for tests."':
                 'description = "pulse {pulse_ms} ms"'})
    assert "holes are filled only in the boxes' setup, load and arm" in problems_of(exc_info)
    with pytest.raises(template.TemplateError) as exc_info:
        loads(**{"{off_tick}:A:0": "{on_tick + 15}:A:0"})
    assert "is not a hole; a hole is a bare name in braces" in problems_of(exc_info)
    with pytest.raises(template.TemplateError) as exc_info:
        loads(**{"100:];": "100:];}"})
    assert "a brace that is not part of a hole" in problems_of(exc_info)


# --- the document -----------------------------------------------------------------------


def test_a_template_is_not_a_method_and_the_method_loader_says_so():
    assert template.is_template(tomllib.loads(TEMPLATE))
    assert not template.is_template(tomllib.loads(SAMPLE))
    with pytest.raises(method.MethodError) as exc_info:
        method.loads(TEMPLATE)
    assert "this document is a method template, not a method" in str(exc_info.value)


@pytest.mark.parametrize("old, new, complaint", [
    ("template_schema = 1", "template_schema = 2", "template_schema: expected 1, got 2"),
    ("renders = 2", "renders = 1", "renders: expected 2"),
    ("renders = 2", "renders = 2\nschema_version = 2", "schema_version: a template states"),
    ("[constants]", "[extra]\nx = 1\n\n[constants]", "extra: not a key of template schema 1"),
    ("[marks]", "[marks]\nstray = { ms = \"on_tick\", colour = \"red\" }\n[unused]",
     "marks.stray.colour: not a key"),
])
def test_the_document_is_checked_against_the_schema(old, new, complaint):
    with pytest.raises(template.TemplateError) as exc_info:
        loads(**{old: new})
    assert complaint in problems_of(exc_info)


def test_a_template_with_marks_declares_the_pusher_period():
    with pytest.raises(template.TemplateError) as exc_info:
        loads(**{"tick_us = 100.0": "period_us = 100.0",
                 'off = { ms = "off_tick * tick_us / 1000"':
                 'off = { ms = "off_tick * period_us / 1000"',
                 "round(pulse_ms * 1000 / tick_us)": "round(pulse_ms * 1000 / period_us)"})
    assert "a template with marks declares tick_us" in problems_of(exc_info)


def test_every_problem_is_reported_not_just_the_first():
    with pytest.raises(template.TemplateError) as exc_info:
        loads(**{"template_schema = 1": "template_schema = 9", "renders = 2": "renders = 9"})
    assert len(exc_info.value.problems) == 2


@pytest.mark.parametrize("name, word", [
    ("duration_ms", "DurationMs"), ("bias_hold_v", "BiasHoldV"), ("sample", "Sample"),
    ("release", "Release"), ("V_bias", "VBias"), ("pulseMs", "PulseMs"), ("a__b_", "AB"),
])
def test_a_name_is_stamped_split_on_underscores_and_capitalized(name, word):
    assert template.camel_case(name) == word


def test_two_names_one_file_would_stamp_alike_are_refused_at_load():
    # A knob and a label stamp under different prefixes, so across kinds is no clash.
    loads(**{"[labels]": "[labels]\npulseMs = { description = \"no clash\" }"})
    with pytest.raises(template.TemplateError) as exc_info:
        loads(**{"cycles = {": "pulseMs = { default = 1, min = 0, max = 2, unit = \"\" }"
                               "\ncycles = {"})
    assert "'pulseMs' and 'pulse_ms' would be stamped" in problems_of(exc_info)


def test_a_render_carries_its_template_for_the_units_and_descriptions():
    t = loads()
    rendered = template.render(t, labels=LABELS)
    assert rendered.template is t
    assert rendered.template.knob("pulse_ms").unit == "ms"


def test_load_template_reads_a_file_and_normalizes_its_line_endings(tmp_path):
    path = tmp_path / "t.toml"
    path.write_bytes(TEMPLATE.replace("\n", "\r\n").encode("utf-8"))
    t = template.load_template(str(path))
    assert t.text == TEMPLATE and t.name == "smoke-test"
    assert t.defaults() == {"pulse_ms": 1.5, "cycles": 10, "wait_ms": 10.0}
    with pytest.raises(KeyError):
        t.knob("duration_ms")
