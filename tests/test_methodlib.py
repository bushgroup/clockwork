"""The method library: scanning a directory, and the two diffs (lab record, task 54).

The two golden methods are the integration fixture -- real documents that differ in
their repetition mode, their per-box strings and what they declare -- and skip in a
public clone the same way `test_method_text.py`'s do. The unit-level behaviour of
`method_diff`'s line alignment is checked against small methods built in this file, so
it does not depend on what the golden documents happen to contain.
"""

from __future__ import annotations

import datetime
import os

import pytest

import clockwork
from clockwork import method as method_module
from clockwork.app.boxstate import AGREES, DIFFERS, FOUND, Reading
from clockwork.app.methodlib import (
    LibraryEntry,
    instrument_diff,
    method_diff,
    open_entry,
    scan_library,
)
from clockwork.method import BoxMethod, Metadata, Method, RfChannel
from clockwork.mips import Box, FakeBox, read_state

BOX = "box1"


def make_method(name: str = "m", **overrides) -> Method:
    boxes = overrides.pop("boxes", (
        BoxMethod(name=BOX, port="COM3", setup=("STBLCLK,EXT",),
                  load=("STBLDAT;...", "SARBCTBL,1"), arm=("SMOD,TBL",)),
    ))
    acquisition = overrides.pop("acquisition", method_module.Acquisition(
        frames=1, scans=100, accumulations=10, file_stem=name))
    start = overrides.pop("start", (method_module.Step(BOX, "TBLSTRT"),))
    return Method(
        metadata=Metadata(name=name, created=datetime.date(2026, 9, 6)),
        acquisition=acquisition,
        boxes=boxes,
        start=start,
        **overrides,
    )


def golden_directory() -> str:
    directory = clockwork.lab_dir("golden")
    if directory is None:
        pytest.skip("the golden experiments are lab material and this is a public clone")
    return directory


# --- scan_library ----------------------------------------------------------------------


def test_an_empty_or_missing_directory_is_an_empty_library(tmp_path):
    assert scan_library("") == []
    assert scan_library(str(tmp_path / "nowhere")) == []


def test_scan_finds_a_method_nested_in_its_own_directory(tmp_path):
    nested = tmp_path / "an-experiment"
    nested.mkdir()
    method_module.save(make_method("nested-one"), str(nested / "method.toml"))
    entries = scan_library(str(tmp_path))
    assert [entry.name for entry in entries] == ["nested-one"]
    assert entries[0].hash
    assert entries[0].created == datetime.date(2026, 9, 6)
    assert entries[0].ok


def test_a_document_that_will_not_parse_is_listed_with_its_problem(tmp_path):
    bad = tmp_path / "broken.toml"
    bad.write_text("schema_version = 2\n", encoding="utf-8")
    entries = scan_library(str(tmp_path))
    assert len(entries) == 1
    assert not entries[0].ok
    assert entries[0].problem
    assert entries[0].hash == ""
    with pytest.raises(method_module.MethodError):
        open_entry(entries[0])


def test_two_methods_with_the_same_stamp_hash_the_same():
    a = make_method("same")
    b = make_method("same")
    entry_a = LibraryEntry(path="a", hash=method_module.stamp(a)["method_hash"][:12])
    entry_b = LibraryEntry(path="b", hash=method_module.stamp(b)["method_hash"][:12])
    assert entry_a.hash == entry_b.hash


@pytest.mark.parametrize("name", ["bradykinin-clock", "detection-response"])
def test_the_golden_library_lists_both_experiments(name):
    directory = golden_directory()
    entries = scan_library(directory)
    names = [entry.name for entry in entries]
    assert f"golden-{name}" in names


# --- method_diff -------------------------------------------------------------------------


def test_a_method_diffed_against_itself_is_identical():
    m = make_method()
    diff = method_diff(m, m)
    assert diff.identical
    assert all(not f.differs for f in diff.acquisition)
    assert all(row.kind == "equal" for box in diff.boxes for row in box.phases["setup"])


def test_a_changed_line_is_reported_changed_not_as_a_remove_and_an_add():
    a = make_method(boxes=(BoxMethod(name=BOX, port="COM3", load=("STBLDAT;A;",)),))
    b = make_method(boxes=(BoxMethod(name=BOX, port="COM3", load=("STBLDAT;B;",)),))
    diff = method_diff(a, b)
    rows = diff.boxes[0].phases["load"]
    assert [row.kind for row in rows] == ["changed"]
    assert rows[0].a == "STBLDAT;A;"
    assert rows[0].b == "STBLDAT;B;"


def test_an_added_and_a_removed_line_are_told_apart():
    a = make_method(boxes=(BoxMethod(name=BOX, port="COM3", setup=("ONE", "TWO")),))
    b = make_method(boxes=(BoxMethod(name=BOX, port="COM3", setup=("ONE", "THREE")),))
    diff = method_diff(a, b)
    rows = diff.boxes[0].phases["setup"]
    kinds = {(row.kind, row.a, row.b) for row in rows}
    assert ("equal", "ONE", "ONE") in kinds
    assert ("changed", "TWO", "THREE") in kinds


def test_a_box_only_in_one_method_is_reported_as_such_not_dropped():
    a = make_method(boxes=(
        BoxMethod(name=BOX, port="COM3", load=("A",)),
        BoxMethod(name="box2", port="COM4", load=("B",)),
    ))
    b = make_method(boxes=(BoxMethod(name=BOX, port="COM3", load=("A",)),))
    diff = method_diff(a, b)
    box2 = next(box for box in diff.boxes if box.name == "box2")
    assert box2.in_a and not box2.in_b
    assert box2.phases["load"] == (method_diff(a, b).boxes[1].phases["load"][0],)
    assert box2.phases["load"][0].kind == "removed"


def test_acquisition_settings_are_compared_field_by_field():
    a = make_method(acquisition=method_module.Acquisition(
        frames=1, scans=100, accumulations=10, file_stem="a",
        repetition_mode="per_repetition"))
    b = make_method(acquisition=method_module.Acquisition(
        frames=1, scans=200, accumulations=10, file_stem="b",
        repetition_mode="single_frame"))
    diff = method_diff(a, b)
    by_label = {f.label: f for f in diff.acquisition}
    assert by_label["scans"].differs
    assert by_label["frames"].differs is False
    assert by_label["repetition_mode"].a == "per_repetition"
    assert by_label["repetition_mode"].b == "single_frame"


def test_declared_dc_bias_and_rf_are_compared_side_by_side():
    a = make_method(boxes=(BoxMethod(
        name=BOX, port="COM3", dc_bias=((1, 5.0), (2, -3.0)),
        rf=(RfChannel(channel=1, frequency_hz=900_000),)),))
    b = make_method(boxes=(BoxMethod(
        name=BOX, port="COM3", dc_bias=((1, 5.0), (2, -1.0), (3, 0.0)),
        rf=(RfChannel(channel=1, frequency_hz=950_000),)),))
    diff = method_diff(a, b)
    by_label = {f.label: f for f in diff.boxes[0].declared}
    assert by_label["DC bias 1"].differs is False
    assert by_label["DC bias 2"].differs
    assert by_label["DC bias 3"].a == ""
    assert by_label["DC bias 3"].b == "0.00 V"
    assert by_label["RF 1 frequency hz"].differs


def test_start_and_reset_are_diffed_as_method_level_sequences():
    a = make_method(start=(method_module.Step(BOX, "TBLSTRT"),))
    b = make_method(start=(method_module.Step(BOX, "TARBTRG"), method_module.Step(BOX, "TBLSTRT")))
    diff = method_diff(a, b)
    assert [row.kind for row in diff.start] == ["added", "equal"]


@pytest.mark.parametrize("name", ["bradykinin-clock"])
def test_the_two_golden_methods_differ_on_repetition_mode(name):
    directory = golden_directory()
    a = method_module.load(os.path.join(directory, "bradykinin-clock", "method.toml"))
    b = method_module.load(os.path.join(directory, "detection-response", "method.toml"))
    diff = method_diff(a, b)
    assert not diff.identical
    by_label = {f.label: f for f in diff.acquisition}
    assert by_label["repetition_mode"].differs
    assert {box.name for box in diff.boxes} == {"auklet", "bufflehead", "cormorant"}


# --- instrument_diff ---------------------------------------------------------------------


def rack_box() -> Box:
    fake = FakeBox(name="MIPS-A", version="1.243t", dcb_channels=2, rf_channels=0,
                   arb_modules=0)
    fake.dc_bias = [5.0, -1.0]
    return Box(transport=fake, name=BOX)


def test_instrument_diff_marks_every_box_the_method_names():
    state = read_state(rack_box())
    picked = make_method(boxes=(
        BoxMethod(name=BOX, port="COM3", dc_bias=((1, 5.0), (2, 0.0))),
    ))
    tables = instrument_diff(picked, {BOX: Reading(state=state, when="on demand")})
    assert set(tables) == {BOX}
    rows = {row.label: row for section in tables[BOX].sections for row in section.rows}
    assert rows["channel 1"].mark == AGREES
    assert rows["channel 2"].mark == DIFFERS


def test_instrument_diff_reads_not_read_yet_for_a_box_with_no_reading():
    picked = make_method(boxes=(BoxMethod(name=BOX, port="COM3"),))
    tables = instrument_diff(picked, {})
    assert tables[BOX].empty
    assert tables[BOX].caption == "not read yet"


def test_instrument_diff_leaves_an_undeclared_setting_marked_left_as_found():
    state = read_state(rack_box())
    picked = make_method(boxes=(BoxMethod(name=BOX, port="COM3"),))
    tables = instrument_diff(picked, {BOX: Reading(state=state, when="on demand")})
    rows = {row.label: row for section in tables[BOX].sections for row in section.rows}
    assert rows["channel 1"].mark == FOUND
