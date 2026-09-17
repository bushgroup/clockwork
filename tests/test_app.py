"""The window, driven against the stand-ins the way a trainee drives it.

Four kinds of test, in the order they cost. The Qt-free halves -- what a run is called
and how a file is handed to mainspring -- need no window at all and are asserted
directly. The panes are a widget over `clockwork.method.text` and are checked for the one
property that matters: a method rendered into panes and read back is the same method. The
run log is checked for the two judgements it makes, collapsing the ten lines a clean
golden run emits and surfacing a frame that ended on silence. And the whole window is
driven end to end over `FakeBox` and `FakeConsole` for a run with a replicate.

**What the end-to-end tests prove, and what they do not.** They prove this window's
plumbing: that a method reaches `send_phases`, that a series names two files, that Stop
ends one, that a refusal greys Acquire out. They prove nothing about a MIPS box or a
digitizer -- the stand-ins have opinions of their own, documented in `clockwork.acq.fake`
and `clockwork.mips.transport` -- and a green run here is not evidence about hardware.

`QT_QPA_PLATFORM=offscreen` is set before Qt is imported so the suite runs with no
display; `pytest-qt`'s `qtbot` owns the application and the event loop.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

pytest.importorskip("pytestqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402

from clockwork import instrument as instrument_module  # noqa: E402
from clockwork import method as method_module  # noqa: E402
from clockwork.acq import FrameEnded, Warned  # noqa: E402
from clockwork.acq.loop import (  # noqa: E402
    WHEN_ARMED,
    FrameRecord,
    declared_differences,
)
from clockwork.app import naming  # noqa: E402
from clockwork.app.boxstate import (  # noqa: E402
    AGREES,
    DIFFERS,
    FOUND,
    Reading,
    state_table,
)
from clockwork.app.launch import open_data_file, open_with  # noqa: E402
from clockwork.app.panes import MARGIN_TAGS, BoxPane  # noqa: E402
from clockwork.app.runlog import RunPanel, is_left_as_found  # noqa: E402
from clockwork.app.settings import Settings  # noqa: E402
from clockwork.app.window import MainWindow  # noqa: E402
from clockwork.method.text import render_pane  # noqa: E402
from clockwork.mips import (  # noqa: E402
    Box,
    FakeBox,
    discover,
    read_sequencer,
    read_state,
)

SCANS = 32
"""Two of the fake console's 16-scan spectrum periods, so a fold has something to
add. The instrument's methods are 5000 and 20000; nothing under test here depends on
the size of a frame."""

ACCUMULATIONS = 2
BOX = "box1"


# --- fixtures ------------------------------------------------------------------------


def per_repetition_table(scans: int) -> str:
    """The sequencer's table for a `per_repetition` frame, in the shape the loop's own
    consistency check expects: DIOA and DIOB raised at tick 0, DIOB dropped at 500, DIOA
    dropped a console batch past the last counted scan."""
    return (f"STBLDAT;0:[A:1,0:A:1:B:1,500:B:0,"
            f"{method_module.enable_fall_tick(scans)}:A:0,"
            f"{method_module.table_period(scans)}:];")


def make_method(
    *,
    scans: int = SCANS,
    accumulations: int = ACCUMULATIONS,
    stem: str = "260917_ZZ_001",
    load: list[str] | None = None,
) -> method_module.Method:
    return method_module.from_dict({
        "schema_version": 2,
        "metadata": {"name": "window test", "created": dt.date(2026, 9, 17)},
        "acquisition": {
            "frames": 1,
            "scans": scans,
            "accumulations": accumulations,
            "file_stem": stem,
            "repetition_mode": "per_repetition",
            "keep_raw": True,
            "enable": {"box": BOX, "channel": "A"},
        },
        "boxes": [{
            "name": BOX,
            "port": "COM3",
            "setup": ["STBLCLK,EXT", "STBLTRG,POS"],
            "load": load if load is not None else [per_repetition_table(scans)],
            "arm": ["SMOD,TBL"],
        }],
        "start": [[BOX, "TBLSTRT"]],
        "reset": [[BOX, "SMOD,LOC"], [BOX, "SMOD,TBL"]],
    })


@pytest.fixture
def scratch_settings(tmp_path, monkeypatch):
    """A `QSettings` of this test's own, so a suite run never touches a real install's.

    `QSettings` is per user and per machine, and a test that wrote through the window's
    own organisation and application names would remember a temporary directory as a
    trainee's output directory on the machine the suite ran on.
    """
    path = str(tmp_path / "settings.ini")
    backing = QSettings(path, QSettings.Format.IniFormat)
    monkeypatch.setattr("clockwork.app.window.Settings",
                        lambda: Settings(backing))
    return backing


def make_instrument(directory) -> str:
    """An instrument document beside the test, with the one field acquiring needs.

    `offset_v` is sent to the console and stamped into the file as one number, so
    `prepare_console` refuses a document without it by name and the window greys Acquire
    out before the button is pressed. The calibration is there so the run has a mass
    axis, which is the status line the window shows beside the picker.
    """
    path = str(directory / "instrument.toml")
    instrument_module.save(instrument_module.from_dict({
        "schema_version": 1,
        "instrument": {"name": "window test"},
        "calibration": {"slope": 0.738088, "intercept": 0.056167},
        "vertical": {"full_scale_v": 0.5, "offset_v": 0.251, "inverted": False},
    }), path)
    return path


@pytest.fixture
def window(qtbot, scratch_settings, tmp_path):
    """A window over the stand-ins, with its directory, initials and document filled."""
    made = MainWindow(fake=True)
    qtbot.addWidget(made)
    made.output_dir.setText(str(tmp_path))
    made.initials.setText("ZZ")
    made._initials_changed()
    made._load_instrument(make_instrument(tmp_path))
    yield made
    made.worker.shutdown()
    made.worker.wait(10_000)


def load_into(window, tmp_path, method: method_module.Method) -> str:
    """Save a method beside the test and open it in the window, boxes and all."""
    path = str(tmp_path / "method.toml")
    method_module.save(method, path)
    window._load_method(path)
    return path


def until(qtbot, predicate, timeout: int = 60_000) -> None:
    # `waitUntil` insists on a real bool, and the predicates below are written the way
    # they read -- `window.worker.boxes and idle(window)` is a dict, not False.
    qtbot.waitUntil(lambda: bool(predicate()), timeout=timeout)


def idle(window) -> bool:
    return window._job is None


def ready_to_acquire(window, qtbot, tmp_path, method=None) -> None:
    """A window taken through the trainee's own order: open, find, send, start.

    Acquire expects the boxes to be loaded and armed already, so `send_phases` comes
    first here exactly as it does on the instrument; the window greys Acquire out until
    it has, which is the assertion in `test_acquire_is_greyed_out_until_the_boxes_are
    _armed`.
    """
    load_into(window, tmp_path, method if method is not None else make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    window.send(setup=True)
    until(qtbot, lambda: idle(window) and window._armed)
    window.start_console()
    until(qtbot, lambda: window.worker.console is not None
          and window.worker.console.alive and idle(window))


# --- naming, with no window in sight --------------------------------------------------


def test_next_stem_is_one_past_the_highest_number_already_there(tmp_path):
    for name in ("260825_BK_025.uimf", "260825_BK_037.summed.uimf",
                 "260904_BK_094.uimf", "260904_BK_094.sent.txt"):
        (tmp_path / name).write_text("", encoding="utf-8")
    assert naming.next_stem(tmp_path, "BK", dt.date(2026, 9, 17)) == "260917_BK_095"


def test_the_counter_does_not_reset_per_day_and_ignores_other_initials(tmp_path):
    (tmp_path / "260101_BK_012.uimf").write_text("", encoding="utf-8")
    (tmp_path / "260917_QQ_400.uimf").write_text("", encoding="utf-8")
    assert naming.next_stem(tmp_path, "BK", dt.date(2026, 9, 17)) == "260917_BK_013"


def test_a_directory_that_is_not_there_yet_starts_at_one(tmp_path):
    assert naming.next_stem(tmp_path / "nope", "BK",
                            dt.date(2026, 9, 17)) == "260917_BK_001"


def test_a_stem_reserved_this_session_moves_the_counter_before_it_exists(tmp_path):
    first = naming.next_stem(tmp_path, "BK", dt.date(2026, 9, 17))
    second = naming.next_stem(tmp_path, "BK", dt.date(2026, 9, 17), taken=[first])
    assert (first, second) == ("260917_BK_001", "260917_BK_002")


def test_initials_are_cleaned_so_a_stem_can_always_be_parsed_back():
    assert naming.clean_initials("m_b 2!") == "MB2"
    assert naming.parse_stem(naming.stem("m_b 2!", 7, dt.date(2026, 9, 17))) == (
        "260917", "MB2", 7)


def test_a_name_in_another_shape_is_not_a_stem():
    assert naming.parse_stem("Tables_BradykininCLOCK.txt") is None
    assert naming.parse_stem("bradykinin_clock-20260915-145701.uimf") is None


# --- handing a file to another program ------------------------------------------------


def test_open_with_refuses_a_file_that_is_not_there(tmp_path):
    result = open_with("notepad.exe", str(tmp_path / "absent.uimf"))
    assert not result and "no file" in result.problem


def test_open_data_file_names_both_attempts_when_both_fail(tmp_path, monkeypatch):
    missing = str(tmp_path / "absent.uimf")
    result = open_data_file(missing, configured="nothing-here.exe")
    assert not result
    assert "no file" in result.problem


# --- the panes ------------------------------------------------------------------------


def test_a_method_rendered_into_a_pane_reads_back_as_the_same_method(qtbot):
    method = make_method()
    entry = method.boxes[0]
    pane = BoxPane(entry.name)
    qtbot.addWidget(pane)
    pane.set_text(render_pane(entry, method.start, method.reset))
    result = pane.result
    assert result.box_method(entry.port) == entry
    assert result.start == method.start
    assert result.reset == method.reset


def test_the_margin_tags_every_line_and_marks_prose_as_not_sent(qtbot):
    pane = BoxPane(BOX)
    qtbot.addWidget(pane)
    pane.set_text("STBLCLK,EXT\n\n# a note\nSMOD,TBL\nThis line is a sentence.\n")
    tags = {line.number: line.tag for line in pane.result.lines}
    assert tags[1] == "setup"
    assert tags[3] == "comment"
    assert tags[4] == "arm"
    assert tags[5] == "unplaced"
    assert MARGIN_TAGS["unplaced"] == "not sent"


def test_a_pane_holds_only_what_was_typed(qtbot):
    """The margin is a reading of the text and never an edit of it: a document that
    round-trips to TOML must not gain a tag clockwork decided on."""
    pane = BoxPane(BOX)
    qtbot.addWidget(pane)
    text = "STBLCLK,EXT\nSMOD,TBL\n"
    pane.set_text(text)
    assert pane.text() == text


# --- the run log ----------------------------------------------------------------------


def test_left_as_found_is_recognised_and_the_lines_beside_it_are_not():
    assert is_left_as_found("auklet SWFDIR is left as found on modules 1: FWD, 2: REV")
    assert not is_left_as_found(
        "auklet DC bias 1 was declared 12.00 V and reads back 11.20 V")
    assert not is_left_as_found(
        "auklet DC bias monitors were not compared: the readback was taken with the "
        "table TBLRDY, where they do not convert")


def test_ten_left_as_found_lines_collapse_to_one_row_per_box(qtbot):
    panel = RunPanel()
    qtbot.addWidget(panel)
    for index in range(5):
        panel.show(Warned(f"auklet SETTING{index} is left as found on module 1: x"))
    for index in range(5):
        panel.show(Warned(f"bufflehead SETTING{index} is left as found on module 1: x"))
    assert panel.log.topLevelItemCount() == 2
    assert panel.log.topLevelItem(0).childCount() == 5
    assert "auklet: 5 settings left as found" in panel.log.topLevelItem(0).text(1)


def test_a_declared_versus_read_line_is_never_grouped_away(qtbot):
    panel = RunPanel()
    qtbot.addWidget(panel)
    panel.show(Warned("auklet SWFDIR is left as found on module 1: FWD"))
    panel.show(Warned("auklet DC bias 1 was declared 12.00 V and reads back 11.20 V"))
    panel.show(Warned("auklet DC bias monitors were not compared: the readback was "
                      "taken with the table TBLRDY, where they do not convert"))
    assert panel.log.topLevelItemCount() == 3


def test_a_frame_that_ended_on_silence_is_surfaced_and_one_that_counted_out_is_not(qtbot):
    panel = RunPanel()
    qtbot.addWidget(panel)
    counted = FrameRecord(method_frame=1, repetition=1, frame_number=1,
                          outcome="acquired", scans_published=SCANS,
                          ended_by="counted")
    silent = FrameRecord(method_frame=1, repetition=2, frame_number=2,
                         outcome="acquired", scans_published=SCANS,
                         ended_by="silence")
    panel.show(FrameEnded(counted))
    assert panel.log.topLevelItemCount() == 0
    panel.show(FrameEnded(silent))
    assert panel.log.topLevelItemCount() == 1
    assert "silence" in panel.log.topLevelItem(0).text(1)


# --- the state panel ------------------------------------------------------------------


def rack_box() -> Box:
    """A stand-in with a small DC bias bank, one head and two ARB modules.

    Two modules rather than four so a section is short enough to read in an assertion,
    and a monitor error so the setpoint and the monitor stay two different numbers
    (`clockwork.mips.transport`). Module 2 is left at `REV`, which is the setting the
    CLOCK method left behind and the detection-response method could not see
    (lab record, task 40).
    """
    fake = FakeBox(name="MIPS-A", version="1.243t", dcb_channels=4, rf_channels=1,
                   arb_modules=2)
    fake.dc_bias = [12.0, -70.0, 0.0, 5.0]
    fake.dc_bias_error = -0.03
    fake.rf[1].update({"SRFFRQ": "943000", "SRFDRV": "50.00"})
    fake.arb[2]["SWFDIR"] = "REV"
    return Box(transport=fake, name=BOX)


def in_table_mode(box: Box) -> Box:
    """Put the stand-in in table mode without loading a table into it.

    `SMOD,TBL` is refused with no table loaded, and what these tests need is the
    consequence rather than the command: the 100 ms service task stops, so the monitor
    array freezes wherever it was and `GTBLSTA` stops answering `IDLE` (§8.2).
    """
    box.transport.mode = "TBL"
    box.transport.status = "READY"
    return box


def declaring(**kwargs) -> method_module.BoxMethod:
    """A method entry for `BOX`, with whatever setup and declarations a test needs."""
    return method_module.BoxMethod(name=BOX, port="COM3", **kwargs)


def rows_of(table, title: str) -> dict:
    section = next(part for part in table.sections if part.title == title)
    return {row.label: row for row in section.rows}


def test_a_setting_the_method_does_not_name_is_marked_left_as_found():
    """The mark the panel exists for: `SWFDIR` REV on module 2, named by nothing.

    The run log already says *that* something was left as found; the panel is where a
    trainee sees *what*, which is the difference the instrument day of 2026-09-15 could
    not tell between two files.
    """
    state = read_state(rack_box())
    table = state_table(Reading(state=state, when="on demand"),
                        declaring(setup=("SWFDIR,1,FWD",)))
    arb = rows_of(table, "ARB")
    assert arb["module 1 direction"].mark == AGREES
    assert arb["module 2 direction"].value == "REV"
    assert arb["module 2 direction"].mark == FOUND


def test_a_declared_setting_the_box_disagrees_with_is_marked_and_counted():
    state = read_state(rack_box())
    table = state_table(Reading(state=state, when="on demand"),
                        declaring(setup=("SWFDIR,2,FWD",)))
    row = rows_of(table, "ARB")["module 2 direction"]
    assert row.mark == DIFFERS and row.declared == "FWD" and row.value == "REV"
    assert table.differing == (row,)


def test_the_panel_marks_a_dc_bias_row_exactly_where_the_loop_would_warn():
    """The assertion that keeps the panel and the run log from contradicting.

    `declared_differences` emits the `Warned` lines a trainee reads in the log; the
    panel's marks are the same judgement against the same tolerances, and a window that
    marked a row as declared while warning about it in the log would be worse than one
    that showed neither.
    """
    state = read_state(rack_box())
    entry = declaring(dc_bias=((1, 12.0), (2, -60.0)))
    rows = rows_of(state_table(Reading(state=state), entry), "DC bias")
    warned = declared_differences(entry, state)
    assert rows["channel 1"].mark == AGREES
    assert rows["channel 2"].mark == DIFFERS
    assert any("DC bias 2 was declared -60.00 V" in line for line in warned)
    assert not any("DC bias 1 was declared" in line for line in warned)


def test_a_monitor_read_in_table_mode_is_not_shown_as_a_number():
    """Task 43: the monitors stop converting in table mode and the array freezes.

    What the panel must not do is print the frozen number beside the setpoint, where it
    reads as a measurement of the output.
    """
    box = rack_box()
    local = read_state(box)
    assert "monitors" in rows_of(state_table(Reading(state=local)),
                                 "DC bias")["channel 1"].note
    table = state_table(Reading(state=read_state(in_table_mode(box)), when="on demand"))
    section = next(part for part in table.sections if part.title == "DC bias")
    assert rows_of(table, "DC bias")["channel 1"].note == "not converting in table mode"
    assert "does not run in table mode" in section.note


def test_gtblfrq_is_not_shown_as_a_frequency_under_an_external_clock():
    """Wire format §4: `TableFreq()` prints an uninitialised local under `EXT`.

    No getter reports the clock source, so the method's own `STBLCLK` is the only thing
    that says which of the two a number is.
    """
    state = read_state(rack_box())
    external = state_table(Reading(state=state), declaring(setup=("STBLCLK,EXT",)))
    engine = next(part for part in external.sections if part.title == "Table engine")
    assert rows_of(external, "Table engine")["clock"].value == "external, EXT"
    assert "uninitialised local" in engine.note
    internal = state_table(Reading(state=state), declaring(setup=("STBLCLK,42000000",)))
    assert "Hz internal" in rows_of(internal, "Table engine")["clock"].value


def test_a_sequencer_reading_updates_the_table_engine_and_claims_nothing_else():
    """Step 4 of task 51: the armed reading is two getters and must not look like forty.

    `read_sequencer` is what the loop takes once a box is armed, because a whole-state
    reading there reports monitors that have stopped converting. The panel keeps the
    earlier reading's rows and says how old they are.
    """
    box = rack_box()
    whole = read_state(box)
    reading = Reading(state=whole, when="after setup, before load",
                      sequencer=read_sequencer(in_table_mode(box)),
                      sequencer_when="armed")
    table = state_table(reading)
    assert "two getters" in table.caption and "after setup, before load" in table.caption
    assert rows_of(table, "Table engine")["status"].value == "READY"
    # The DC bias rows are the earlier reading's, and still carry its monitors.
    assert "monitors" in rows_of(table, "DC bias")["channel 1"].note


def test_a_box_nothing_has_been_read_off_has_a_caption_and_no_rows():
    table = state_table(Reading())
    assert table.empty and table.sections == () and table.caption == "not read yet"


def test_the_panel_starts_shut_and_opens_the_section_that_disagrees(qtbot):
    pane = BoxPane(BOX)
    qtbot.addWidget(pane)
    assert not pane.state.opened
    pane.show_state(Reading(state=read_state(rack_box()), when="on demand"),
                    declaring(setup=("SWFDIR,2,FWD",)))
    assert "1 setting disagrees" in pane.state.summary.text()
    sections = {pane.state.tree.topLevelItem(index).text(0):
                pane.state.tree.topLevelItem(index)
                for index in range(pane.state.tree.topLevelItemCount())}
    assert sections["ARB"].isExpanded()
    assert not sections["DC bias"].isExpanded()


def test_a_section_opened_by_hand_survives_the_next_re_mark(qtbot):
    """Every keystroke in the pane re-marks the rows, which rebuilds the tree.

    A trainee typing the line a row asked for must not watch the section they opened to
    read it shut itself under them.
    """
    pane = BoxPane(BOX)
    qtbot.addWidget(pane)
    pane.show_state(Reading(state=read_state(rack_box()), when="on demand"), declaring())
    opened = next(pane.state.tree.topLevelItem(index)
                  for index in range(pane.state.tree.topLevelItemCount())
                  if pane.state.tree.topLevelItem(index).text(0) == "RF")
    assert not opened.isExpanded()
    opened.setExpanded(True)
    pane.show_method(declaring(setup=("SWFDIR,2,REV",)))
    again = next(pane.state.tree.topLevelItem(index)
                 for index in range(pane.state.tree.topLevelItemCount())
                 if pane.state.tree.topLevelItem(index).text(0) == "RF")
    assert again.isExpanded()


def test_editing_a_pane_remarks_the_panel_without_a_new_reading(qtbot):
    """A trainee who types the line the panel said was missing should watch the mark
    change, not wait five seconds for a readback to confirm what the pane decides."""
    pane = BoxPane(BOX)
    qtbot.addWidget(pane)
    pane.show_state(Reading(state=read_state(rack_box()), when="on demand"), declaring())
    before = state_table(pane.state.reading, pane.state.method)
    assert rows_of(before, "ARB")["module 2 direction"].mark == FOUND
    pane.show_method(declaring(setup=("SWFDIR,2,REV",)))
    after = state_table(pane.state.reading, pane.state.method)
    assert rows_of(after, "ARB")["module 2 direction"].mark == AGREES


# --- discovery ------------------------------------------------------------------------


def test_discovery_keeps_the_boxes_that_answered_and_reports_the_ports_that_did_not():
    def opener(port, **_):
        if port == "COM9":
            raise OSError("could not open port COM9")
        return Box(transport=FakeBox(), name=port)

    found = discover(ports=["COM3", "COM9"], opener=opener)
    assert list(found.boxes) and found.silent == (("COM9", "could not open port COM9"),)
    entry = found.found[0]
    assert entry.port == "COM3" and entry.box is not None
    found.close()


def test_two_boxes_with_one_name_are_reported_and_neither_is_addressable():
    found = discover(ports=["COM3", "COM4"],
                     opener=lambda port, **_: Box(transport=FakeBox(), name=port))
    assert found.boxes == {} if len(found.unusable) == 2 else found.boxes
    assert len(found.unusable) == 2
    found.close()


# --- the whole window -----------------------------------------------------------------


def test_a_method_opens_into_panes_and_the_start_order_is_shown(window, tmp_path, qtbot):
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: BOX in window.panes and idle(window))
    assert window.panes[BOX].text().startswith("STBLCLK,EXT")
    assert "TBLSTRT" in window.start_order.text()


def test_a_method_that_contradicts_itself_greys_acquire_out_and_says_why(
        window, tmp_path, qtbot):
    """`refusals` is the window's whole judgement about a method: a loop count that
    disagrees with `accumulations` folds the wrong pushes together, and the remedy is a
    sentence rather than a greyed button with no explanation."""
    wrong = per_repetition_table(SCANS).replace("A:1,", "A:7,", 1)
    load_into(window, tmp_path, make_method(load=[wrong]))
    until(qtbot, lambda: BOX in window.panes and idle(window))
    window.panes[BOX].reparse()
    window._refresh_actions()
    assert not window.acquire_button.isEnabled()
    assert "Acquire is not available" in window.problems.text()


def test_acquire_is_greyed_out_until_the_boxes_are_armed_with_this_method(
        window, tmp_path, qtbot):
    """`run_acquisition` starts boxes it expects to be loaded and armed already. A
    method never sent, and a method edited since it was sent, both leave the box holding
    something other than what is on screen -- and both would show up as `TBLSTRT`
    refused for "not in table mode", three frames in, rather than as a sentence."""
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    assert not window.acquire_button.isEnabled()
    assert "have not been loaded and armed" in window.problems.text()

    window.send(setup=True)
    until(qtbot, lambda: idle(window) and window._armed)
    assert window.acquire_button.isEnabled(), window.problems.text()

    # Into the `setup` block, which is what a send puts on the wire. A changed `reset`
    # would not invalidate the arming and should not: it is walked at replicate time,
    # not at arm time, and `wire_fingerprint` covers the three phases a send delivers.
    window.panes[BOX].set_text("SDCB,1,12.0\n\n" + window.panes[BOX].text())
    window._refresh_actions()
    assert not window.acquire_button.isEnabled()
    assert "changed since the boxes were armed" in window.problems.text()


def test_a_send_fills_the_state_panels_without_a_second_readback(
        window, tmp_path, qtbot):
    """Decision 6's "refreshed after every send", done out of the send's own readings.

    `send_phases` already reads every box between `setup` and `load`, where the box is
    local and its DC bias monitors are still converting. Queuing a `read_state` after
    the send would cost five seconds to produce a worse reading -- the box is armed by
    then, and the monitor half of it is a frozen array (task 43). So the panel is fed
    from the `StateRead` events the send reports, and the two-getter armed reading
    lands as an overlay that says so.
    """
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    assert window.panes[BOX].state.reading.state is None

    window.send(setup=True)
    until(qtbot, lambda: idle(window) and window._armed)
    window._drain()

    reading = window.panes[BOX].state.reading
    assert reading.state is not None and reading.state.dc_bias_setpoints
    assert "after setup, before load" in reading.when
    assert reading.sequencer is not None and WHEN_ARMED in reading.sequencer_when
    # Two getters and not forty: the armed reading knows the table engine and nothing
    # else, and the caption has to say so beside rows it did not take.
    assert not reading.sequencer.dc_bias_setpoints
    assert "two getters" in window.panes[BOX].state.caption.text()


def test_the_panel_button_reads_only_its_own_box(window, tmp_path, qtbot):
    """One box's reading costs about forty round trips, so the button reads one box.

    The Run menu's entry reads the rack; this is the per-pane refresh of step 1, and
    a trainee who has just changed one box's front panel should not pay for three.
    """
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    window.panes[BOX].state.refresh.click()
    until(qtbot, lambda: idle(window) and window.panes[BOX].state.reading.state)
    assert "on demand" in window.panes[BOX].state.reading.when
    assert window.readings[BOX].state.identity


def test_a_whole_run_with_a_replicate_names_two_files_and_folds_both(
        window, tmp_path, qtbot):
    ready_to_acquire(window, qtbot, tmp_path)
    assert window.acquire_button.isEnabled(), window.problems.text()

    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    window.replicates.setValue(2)
    window.acquire()
    until(qtbot, lambda: len(runs) >= 2 and idle(window), timeout=180_000)

    stems = [os.path.basename(run.raw_path) for run in runs]
    assert stems == ["260917_ZZ_001.uimf", "260917_ZZ_002.uimf"] or len(set(stems)) == 2
    assert runs[0].replicate is False and runs[1].replicate is True
    for run in runs:
        assert run.complete, run.text
        assert os.path.isfile(run.summed_path)
    # Both log files, named for the stem, beside the data (`notes/architecture.md`).
    written = set(os.listdir(tmp_path))
    for run in runs:
        stem = os.path.splitext(os.path.basename(run.raw_path))[0]
        assert f"{stem}.sent.txt" in written
        assert any(name.startswith(stem) and name.endswith(".transcript.log")
                   for name in written)


def test_the_setup_send_and_the_acquisition_share_one_send_log(window, tmp_path, qtbot):
    """The setup, load and arm strings are the ones the lab troubleshoots from, and
    they go before any file exists to name them. Send setup opens the log; an
    acquisition under the same stem continues it rather than overwriting it."""
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    stem = window.stem.text()
    window.send(setup=True)
    until(qtbot, lambda: idle(window) and window.worker.snapshot is not None)
    log = tmp_path / f"{stem}.sent.txt"
    assert log.is_file()
    after_setup = log.read_text(encoding="utf-8")
    assert "STBLCLK,EXT" in after_setup

    window.start_console()
    until(qtbot, lambda: window.worker.console is not None
          and window.worker.console.alive and idle(window))
    assert window.stem.text() == stem, "the stem did not move before the acquisition"
    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    window.replicates.setValue(1)
    window.acquire()
    until(qtbot, lambda: runs and idle(window), timeout=180_000)
    whole = log.read_text(encoding="utf-8")
    assert whole.startswith(after_setup[:200])
    assert "TBLSTRT" in whole


def test_stop_ends_a_series_after_the_current_repetition(window, tmp_path, qtbot):
    ready_to_acquire(window, qtbot, tmp_path, make_method(accumulations=8))
    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    window.replicates.setValue(3)
    window.acquire()
    qtbot.waitUntil(lambda: window.run_panel.progress.repetition >= 1, timeout=120_000)
    window.stop()
    until(qtbot, lambda: idle(window), timeout=180_000)

    assert len(runs) == 1, "the series ended rather than going on to the replicates"
    run = runs[0]
    assert run.stopped_early
    # A stopped run still folds the method frame it was in and still closes its files:
    # what it leaves on disk is a short experiment, not a broken one.
    assert os.path.isfile(run.summed_path)


def test_no_instrument_document_greys_acquire_out_but_not_send(
        qtbot, scratch_settings, tmp_path):
    """The offset is sent to the console and stamped into the file as one number, so a
    run without one cannot be prepared -- but the boxes need no document, and a trainee
    setting an instrument up should still be able to send to them."""
    made = MainWindow(fake=True)
    qtbot.addWidget(made)
    try:
        made.output_dir.setText(str(tmp_path))
        made.initials.setText("ZZ")
        made._initials_changed()
        load_into(made, tmp_path, make_method())
        until(qtbot, lambda: BOX in made.panes and idle(made))
        assert not made.acquire_button.isEnabled()
        assert "no channel offset" in made.problems.text()
        assert made.setup_button.isEnabled()
        made._load_instrument(make_instrument(tmp_path))
        made._refresh_actions()
        assert "no channel offset" not in made.problems.text()
    finally:
        made.worker.shutdown()
        made.worker.wait(10_000)


def test_a_box_the_method_names_that_nothing_answered_for_is_still_editable(
        window, tmp_path, qtbot):
    """A trainee editing tomorrow's method should not need the instrument switched on."""
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: BOX in window.panes and idle(window))
    window._discovered(type("Empty", (), {"found": ()})())
    assert BOX in window.panes
    assert window.panes[BOX].status.text() == "off or absent"
    assert window.panes[BOX].text().startswith("STBLCLK,EXT")
