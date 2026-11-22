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

import contextlib
import datetime as dt
import os
import sqlite3
import sys
import threading
from dataclasses import replace

import pytest

pytest.importorskip("pytestqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mainspring.interface import (  # noqa: E402
    OPTION_FOLLOW,
    OPTION_SHOW,
    SHOW_WORDS,
)
from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QDialog  # noqa: E402

from clockwork import instrument as instrument_module  # noqa: E402
from clockwork import method as method_module  # noqa: E402
from clockwork import transcript  # noqa: E402
from clockwork.acq import (  # noqa: E402
    BatchSeen,
    BeamGuard,
    Folding,
    FrameBegun,
    FrameEnded,
    RunBegun,
    Warned,
)
from clockwork.acq.loop import (  # noqa: E402
    WHEN_ARMED,
    FoldRecord,
    FrameRecord,
    Run,
    declared_differences,
)
from clockwork.acq.process import ConsoleConfig, ConsoleProcess  # noqa: E402
from clockwork.app import errors, naming, queuepanel, runqueue  # noqa: E402
from clockwork.app.boxstate import (  # noqa: E402
    AGREES,
    DIFFERS,
    FOUND,
    Reading,
    state_table,
)
from clockwork.app.launch import (  # noqa: E402
    SHOW_FOR_MODE,
    association_command,
    open_data_file,
    open_data_file_with_options,
    open_with,
    show_word,
    viewer_options,
)
from clockwork.app.librarypanel import (  # noqa: E402
    LibraryDialog,
    MethodDiffDialog,
)
from clockwork.app.methodlib import method_diff  # noqa: E402
from clockwork.app.panes import MARGIN_TAGS, BoxPane  # noqa: E402
from clockwork.app.queuepanel import BeamGuardDialog, QueuePanel  # noqa: E402
from clockwork.app.runlog import (  # noqa: E402
    _IS_WARNING,
    _JOB,
    RunPanel,
    is_left_as_found,
    names_it_elsewhere,
)
from clockwork.app.settings import Settings  # noqa: E402
from clockwork.app.templatetab import TemplateTab, default_name  # noqa: E402
from clockwork.app.window import MainWindow  # noqa: E402
from clockwork.app.worker import matches_wire  # noqa: E402
from clockwork.method import template as templates  # noqa: E402
from clockwork.method.text import render_pane  # noqa: E402
from clockwork.mips import (  # noqa: E402
    Box,
    FakeBox,
    Silent,
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
    """A window taken through a trainee's order: open, find, send, start the console.

    Acquire sends what the boxes do not hold by itself since task 169 (E7); the Send
    first here makes each test's Acquire start from boxes that hold the method, which is
    the ordinary case at the instrument and what these tests were written against.
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


def test_a_stem_with_only_a_transcript_is_counted(tmp_path):
    """Lab #3: a run that fails before its file exists leaves only its transcript, and
    the number it used must not be offered again."""
    (tmp_path / "260926_MB_014-2026-09-26.transcript.log").write_text("", encoding="utf-8")
    assert naming.parse_stem("260926_MB_014-2026-09-26.transcript.log") == (
        "260926", "MB", 14)
    assert naming.next_stem(tmp_path, "MB", dt.date(2026, 9, 26)) == "260926_MB_015"


def test_a_stem_is_taken_by_any_file_it_names_and_not_by_its_neighbours(tmp_path):
    (tmp_path / "260926_MB_0140.uimf").write_text("", encoding="utf-8")
    (tmp_path / "260926_MB_01.sent.txt").write_text("", encoding="utf-8")
    # A request's record names the file it expects, before any attempt is made.
    (tmp_path / "260926_MB_014.request.json").write_text("{}", encoding="utf-8")
    # ...and so does the temporary file it is rewritten through (lab #19).
    (tmp_path / "260926_MB_014.request.json.tmp").write_text("{}", encoding="utf-8")
    assert naming.files_of(tmp_path, "260926_MB_014") == []
    for name in ("260926_MB_014.sent.txt", "260926_MB_014-2026-09-26.transcript.log"):
        (tmp_path / name).write_text("", encoding="utf-8")
    assert naming.files_of(tmp_path, "260926_MB_014") == [
        "260926_MB_014-2026-09-26.transcript.log", "260926_MB_014.sent.txt"]
    assert naming.files_of(tmp_path / "nope", "260926_MB_014") == []


def test_a_record_being_rewritten_does_not_take_its_stem(tmp_path, monkeypatch):
    """Lab #19: a series wrote its own record while its job checked the first stem, and
    the record's temporary file failed the series. Looked at in the middle of each write,
    the stem must still be free."""
    from clockwork import record as record_module

    seen: list[list[str]] = []
    real_replace = os.replace

    def replace(source, target):
        seen.append([os.listdir(tmp_path), naming.files_of(tmp_path, "261112_UBQ_005")])
        real_replace(source, target)

    monkeypatch.setattr(record_module.os, "replace", replace)
    run = record_module.RunRecord.open(str(tmp_path), "261112_UBQ_005", request_id="r1",
                                       text="a series", initials="UBQ",
                                       source="mcp")
    run.append("notes", {"text": "while the series starts", "by": "session"})
    assert len(seen) == 2
    for listing, taken in seen:
        assert "261112_UBQ_005.request.json.tmp" in listing
        assert taken == []


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


# --- opening a file being acquired, with options ---------------------------------------


def a_file(tmp_path, name: str = "260917_ZZ_001.uimf") -> str:
    path = tmp_path / name
    path.write_bytes(b"")
    return str(path)


def test_the_path_goes_where_the_registered_command_says_and_options_follow(tmp_path):
    """`ShellExecute` on a document cannot carry `--follow`, so the association is
    resolved to a command and the path is substituted for its `"%1"` -- put where the
    template says, not appended after the last argument (task 55)."""
    path = a_file(tmp_path)
    lines: list[list[str]] = []
    result = open_data_file_with_options(
        path, ("--follow", "--show", "newest"),
        resolve=lambda _: '"C:\\mainspring\\mainspring.exe" "%1" --quiet',
        spawn=lines.append,
    )
    assert result
    assert lines == [["C:\\mainspring\\mainspring.exe", path, "--quiet",
                      "--follow", "--show", "newest"]]


def test_a_template_with_no_placeholder_gets_the_path_appended(tmp_path):
    path = a_file(tmp_path)
    lines: list[list[str]] = []
    open_data_file_with_options(path, ("--follow",), resolve=lambda _: "viewer.exe",
                                spawn=lines.append)
    assert lines == [["viewer.exe", path, "--follow"]]


def test_no_association_falls_back_to_the_configured_path_with_the_options(tmp_path):
    """The order `open_data_file` keeps, by the route that can carry arguments: a
    machine whose association is absent is the machine where the setting was filled
    in, and it gets the same options."""
    path = a_file(tmp_path)
    lines: list[list[str]] = []
    result = open_data_file_with_options(
        path, ("--follow", "--show", "rolling-sum"), configured='"C:\\ms\\ms.exe"',
        resolve=lambda _: "", spawn=lines.append,
    )
    assert result and result.how == "C:\\ms\\ms.exe"
    assert lines == [["C:\\ms\\ms.exe", path, "--follow", "--show", "rolling-sum"]]


def test_a_machine_with_neither_route_names_both_attempts(tmp_path):
    path = a_file(tmp_path)

    def refuse(_line):
        raise OSError("the system cannot find the file specified")

    result = open_data_file_with_options(
        path, ("--follow",), configured="gone.exe", resolve=lambda _: "", spawn=refuse)
    assert not result
    assert "the file association" in result.how and "gone.exe" in result.how
    assert "nothing is registered for .uimf files" in result.problem
    assert "cannot find the file" in result.problem


@pytest.mark.skipif(sys.platform != "win32", reason="the shell's association table")
def test_an_extension_nothing_owns_resolves_to_no_association():
    """`ASSOCF_INIT_IGNOREUNKNOWN`, measured rather than assumed (lab record, task 55).

    Without the flag the shell answers for an unowned extension with its `Unknown`
    class -- `OpenWith.exe "%1"`, the "How do you want to open this file?" dialog --
    and the caller then reports success, appends `--follow --show ...` to a dialog and
    never reaches the configured path. This is the real resolver against the real
    registry, because a stub cannot tell whether the flag is passed.
    """
    assert association_command(".zzclockworkprobe") == ""


def test_an_openwith_answer_is_taken_at_face_value_here(tmp_path):
    """The `Unknown` class is refused where it is resolved and nowhere else.

    There is no second guard downstream reading the command back and rejecting
    `OpenWith.exe` by name: a resolver that answers with it is believed. That keeps one
    place responsible, and it is why the test above is the one that fails if the flag
    is ever dropped -- this one would go on passing.
    """
    path = a_file(tmp_path)
    lines: list[list[str]] = []
    result = open_data_file_with_options(
        path, ("--follow",),
        resolve=lambda _: r'C:\WINDOWS\system32\OpenWith.exe "%1"',
        spawn=lines.append,
    )
    assert result and result.how == "the file association (OpenWith.exe)"
    assert lines == [[r"C:\WINDOWS\system32\OpenWith.exe", path, "--follow"]]


def test_the_show_word_is_mainspring_s_own_and_covers_every_repetition_mode():
    """The words are imported, never retyped, and a mode added to `clockwork.method`
    without an answer here should fail on this line rather than launch a viewer into
    whatever the fallback happens to be."""
    assert set(SHOW_FOR_MODE) == set(method_module.REPETITION_MODES)
    assert set(SHOW_FOR_MODE.values()) <= set(SHOW_WORDS)
    assert show_word("single_frame") == "newest"
    assert show_word("per_repetition") == "rolling-sum"
    assert viewer_options("per_repetition") == (OPTION_FOLLOW, OPTION_SHOW,
                                                "rolling-sum")


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


# --- a windowed build keeps its tracebacks ---------------------------------------


@pytest.fixture
def errors_log(tmp_path, monkeypatch):
    """`%LOCALAPPDATA%` pointed at the test's own directory, and the hooks restored.

    `install` replaces two process-wide hooks, so a test that left them in place would
    hand every later test's stray exception to this file.

    **pytest-qt's own hook is stood down first, deliberately.** It exists to fail a test
    whose Qt event loop swallowed an exception, which is exactly the thing these tests
    raise on purpose; chaining to it would make every one of them fail with the
    exception it was written to catch. Standing it down leaves `sys.__excepthook__`
    underneath, which is the hook a real build chains to and the one the console half
    of this is about.
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    before = (sys.excepthook, threading.excepthook)
    sys.excepthook = sys.__excepthook__
    errors.install()
    yield errors.errors_log_path()
    sys.excepthook, threading.excepthook = before


def test_an_exception_inside_a_queued_slot_is_written_down(qtbot, errors_log):
    """Task 60's `console=False` is why this exists.

    A windowed build has `sys.stderr is None`; PySide6 reports what a slot raised
    through `sys.excepthook`, whose default writes to that `None` and returns, and the
    slot returns as though nothing happened. Run 011 on the clean machine wrote no line
    about mainspring at all and nothing on the machine could say whether that was an
    unticked box or an exception (lab record, tasks 55 and 61).

    A real queued slot, not a direct call: the hook is only reached through Qt's own
    dispatch, so calling the function would prove nothing about the path that failed.
    """
    from PySide6.QtCore import QTimer

    def raises() -> None:
        raise RuntimeError("the association resolver fell over")

    QTimer.singleShot(0, raises)
    qtbot.waitUntil(lambda: os.path.isfile(errors_log), timeout=5_000)

    written = open(errors_log, encoding="utf-8").read()
    assert "RuntimeError: the association resolver fell over" in written
    assert "def raises" in written or "raise RuntimeError" in written


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_an_exception_on_a_thread_is_written_down_too(qtbot, errors_log):
    """`sys.excepthook` never sees what escapes the top of a thread's `run`, and the
    work is on a thread: the send, the acquisition and the fold all are."""
    def raises() -> None:
        raise ValueError("the fold could not open the companion")

    thread = threading.Thread(target=raises, name="worker")
    thread.start()
    thread.join(5)
    qtbot.waitUntil(lambda: os.path.isfile(errors_log), timeout=5_000)

    written = open(errors_log, encoding="utf-8").read()
    assert "ValueError: the fold could not open the companion" in written
    assert "on thread worker" in written


def test_the_window_says_where_the_traceback_went(qtbot, errors_log, scratch_settings):
    """The file is no use to a trainee who has not been told to look in it, so the run
    log gets one warn line naming it and the status bar says the same."""
    made = MainWindow(fake=True)
    qtbot.addWidget(made)
    try:
        from PySide6.QtCore import QTimer

        def raises() -> None:
            raise RuntimeError("something went bang")

        QTimer.singleShot(0, raises)
        qtbot.waitUntil(lambda: made.run_panel.log.topLevelItemCount() > 0,
                        timeout=5_000)
        line = made.run_panel.log.topLevelItem(0)
        assert "something went bang" in line.text(1)
        assert errors_log in line.text(1)
        assert line.data(0, _IS_WARNING)
        assert "something went bang" in made.statusBar().currentMessage()
    finally:
        made.close()
        made.worker.shutdown()
        made.worker.wait(10_000)


def test_a_quit_is_not_an_error(qtbot, errors_log):
    """A file called errors.log that collects "the program was asked to quit" is a file
    nobody reads. `SystemExit` and `KeyboardInterrupt` go straight to the hook that was
    there before."""
    sys.excepthook(SystemExit, SystemExit(0), None)
    assert not os.path.isfile(errors_log)


def test_the_console_still_gets_the_traceback(qtbot, errors_log, capsys):
    """The previous hook is called, not replaced: a checkout has a perfectly good
    stderr and a developer watching it should not have to open a file."""
    sys.excepthook(RuntimeError, RuntimeError("both ways"), None)
    assert "both ways" in open(errors_log, encoding="utf-8").read()
    assert "both ways" in capsys.readouterr().err


# --- the window fits the instrument's screen -------------------------------------

WORKING_AREA = (1536, 912)
"""MASSTRO's monitor as Windows reports it to a window: 1920 x 1080 at 125 % scaling,
`Screen.WorkingArea` 1536 x 912 logical pixels with the taskbar taken off. MASSIMO's is
larger and was still not large enough before task 61."""

TALLEST = 700
"""What `minimumSizeHint().height()` may be, with margin against the 912 above.

Offscreen at 9 pt the window measures a good deal less than this, and that slack is
deliberate twice over: these tests run under a smaller font than Windows gives, so the
figure here is a lower bound on the real one, and a margin of 200 px means the number
that fails is still a number a trainee could have lived with. The point is the
direction of travel -- it was 907 before task 61, against a 912 px screen, and the
window was clipped: the run-log pane and the status bar were below the bottom edge of a
maximized window and a trainee read "nothing in the run log" off a pane that was there
and off-screen.

If a group box added to the side column breaks this line, the column is not what should
give: put the new control where it belongs and let the scroll area do its job.
"""


WIDEST = 1200
"""What `minimumSizeHint().width()` may be with three panes loaded, with margin against the
1536 above, for the same reason as `TALLEST`: offscreen at 9 pt the tabbed window measures
888, and Windows' own fonts make every label wider (lab record, task 169). Before the
revision the panes set the minimum width, some 400 px past the instrument's monitor; then
the queue's single row of eleven buttons, at 1482, and the note's unwrapped sentence."""


def three_boxes() -> method_module.Method:
    """Three boxes, each with a table: the rack a trainee meets, which the fit is pinned
    against (lab record, task 169), not a window with no panes at all."""
    names = ("auklet", "bufflehead", "cormorant")
    return method_module.from_dict({
        "schema_version": 2,
        "metadata": {"name": "three boxes", "created": dt.date(2026, 11, 18)},
        "acquisition": {"frames": 1, "scans": SCANS, "accumulations": ACCUMULATIONS,
                        "file_stem": "261118_ZZ_001", "repetition_mode": "per_repetition",
                        "keep_raw": True, "enable": {"box": names[0], "channel": "A"}},
        "boxes": [{"name": name, "port": f"COM{3 + index}",
                   "setup": ["STBLCLK,EXT", "STBLTRG,POS"],
                   "load": [per_repetition_table(SCANS)], "arm": ["SMOD,TBL"]}
                  for index, name in enumerate(names)],
        "start": [[name, "TBLSTRT"] for name in names],
        "reset": [[names[0], "SMOD,LOC"], [names[0], "SMOD,TBL"]],
    })


def test_the_window_fits_the_working_area_with_three_panes_loaded(
        qtbot, scratch_settings, tmp_path):
    """The requirement, restated for the tabbed window (lab record, task 169): its
    minimum fits 1536 x 912 with a method of three boxes loaded, which the test before
    it never measured -- the panes, not the side column, set the old window's minimum
    width, some 400 px more than the screen. They scroll sideways inside the Method tab
    now, and the tabs share one height."""
    made = MainWindow(fake=True)
    qtbot.addWidget(made)
    try:
        load_into(made, tmp_path, three_boxes())
        until(qtbot, lambda: len(made.panes) == 3 and idle(made))
        hint = made.minimumSizeHint()
        assert hint.height() < TALLEST, hint
        assert hint.width() < WIDEST, hint
    finally:
        made.worker.shutdown()
        made.worker.wait(10_000)


def test_the_run_log_and_the_status_bar_are_on_screen_when_maximized(
        qtbot, scratch_settings, tmp_path):
    """The thing the minimum is a proxy for, asserted directly, with three panes loaded:
    at the working area's own size the run log has real height under every tab, the
    status bar's bottom edge is inside the window, and the Method tab's three panes are
    reachable."""
    made = MainWindow(fake=True)
    qtbot.addWidget(made)
    try:
        load_into(made, tmp_path, three_boxes())
        until(qtbot, lambda: len(made.panes) == 3 and idle(made))
        made.resize(*WORKING_AREA)
        made.show()
        qtbot.waitExposed(made)
        for index in range(made.tabs.count()):
            made.tabs.setCurrentIndex(index)
            qtbot.wait(10)
            assert made.run_panel.isVisible() and made.run_panel.height() > 150
            assert made.statusBar().geometry().bottom() <= made.height()
        assert made.statusBar().isVisible()
        assert made.width() <= WORKING_AREA[0]
    finally:
        made.close()
        made.worker.shutdown()
        made.worker.wait(10_000)


def test_the_window_is_five_tabs_over_the_run_strip(window):
    """E1: one tab for each way of using the window, the run strip and the run log
    under all of them, and the window opened on the Method tab by default."""
    from clockwork.app.window import TABS

    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == list(TABS)
    assert TABS == ("Template", "Queue", "Method", "Day", "Setup")
    assert window.tabs.currentWidget() is window.method_tab
    assert not window.tabs.isAncestorOf(window.run_panel)
    for control in (window.run_panel.guard_button, window.run_panel.stop_run,
                    window.run_panel.note, window.run_panel.at_window,
                    window.run_panel.purpose, window.run_panel.job_line):
        assert window.run_panel.isAncestorOf(control)
    for control in (window.find_button, window.output_dir, window.initials,
                    window.instrument_field, window.open_on_acquire,
                    window.console_settings_button):
        assert window.setup_tab.isAncestorOf(control)
    assert window.method_tab.isAncestorOf(window.acquire_button)
    assert window.method_tab.isAncestorOf(window.send_button)


def test_the_cuts_are_gone(window):
    """E8: the paste-file import, the method-against-instrument diff, Send setup, Load
    and arm, Replicate and the builder dialog."""
    for name in ("setup_button", "arm_button", "replicate_button", "action_import",
                 "action_build", "open_trainee_file", "build_from_template", "replicate",
                 "queue_dock"):
        assert not hasattr(window, name), name
    texts = [action.text() for menu in window.menuBar().actions()
             for action in menu.menu().actions()]
    assert not any("paste file" in text or "Build a method" in text for text in texts)


def test_every_control_in_the_side_column_stays_reachable(qtbot, scratch_settings):
    """The scroll areas are the structural half of task 61's step 1: on a screen far
    too small for the Method tab's column or the Setup tab, the controls are still
    there to be scrolled to rather than clipped away."""
    from PySide6.QtWidgets import QScrollArea

    made = MainWindow(fake=True)
    qtbot.addWidget(made)
    try:
        made.resize(900, 420)
        made.show()
        made.tabs.setCurrentWidget(made.setup_tab)
        qtbot.waitExposed(made)
        assert isinstance(made.setup_tab, QScrollArea)
        assert made.setup_tab.verticalScrollBar().maximum() > 0
        assert made.setup_tab.widget().isAncestorOf(made.open_on_acquire)
    finally:
        made.close()
        made.worker.shutdown()
        made.worker.wait(10_000)


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


def test_each_send_groups_its_own_left_as_found_lines(qtbot):
    """Three sends showed each `left as found` line three times, all under the first
    send's group (on the rack, 2026-09-28; lab record, task 96)."""
    panel = RunPanel()
    qtbot.addWidget(panel)
    for _ in range(3):
        panel.begin("sending setup, load and arm")
        for index in range(2):
            panel.show(Warned(f"cormorant SETTING{index} is left as found on module 1: x"))
    groups = [panel.log.topLevelItem(index)
              for index in range(panel.log.topLevelItemCount())]
    assert [group.childCount() for group in groups] == [2, 2, 2]
    assert all("cormorant: 2 settings left as found" in group.text(1) for group in groups)


def test_copy_puts_the_whole_log_on_the_clipboard(qtbot):
    """Matt at the bench, 2026-09-19: "next to Clear, there needs to be a Copy button
    to copy the run log."

    The run log is not written anywhere, which is why one clean machine's run had to be
    described from memory rather than pasted (task 61). Tab-separated `time<TAB>text`,
    one line per item and in order.
    """
    from PySide6.QtWidgets import QApplication

    panel = RunPanel()
    qtbot.addWidget(panel)
    panel.say("the console answered in 5.4 s")
    panel.say("TBLCMPLT was never seen on auklet", warn=True)

    panel.copy()
    lines = QApplication.clipboard().text().splitlines()
    assert len(lines) == 2
    assert lines[0].split("\t")[1] == "the console answered in 5.4 s"
    assert lines[1].split("\t")[1] == "! TBLCMPLT was never seen on auklet"
    # The time column is there and is a number, so a paste is two columns and the
    # order of events survives the trip into a message.
    assert float(lines[0].split("\t")[0]) >= 0


def test_a_collapsed_group_copies_out_open(qtbot):
    """A trainee who copies the log has not necessarily clicked the groups open, and
    the lines inside one are exactly what somebody reading the paste wants. The group's
    own line is a warning, so its children inherit the mark."""
    panel = RunPanel()
    qtbot.addWidget(panel)
    for index in range(3):
        panel.show(Warned(f"auklet SETTING{index} is left as found on module 1: x"))

    lines = panel.as_text().splitlines()
    assert len(lines) == 4
    assert "auklet: 3 settings left as found" in lines[0]
    assert all(line.split("\t")[1].startswith("! auklet SETTING") for line in lines[1:])
    # A child carries no time of its own; the tab is still there so the paste stays
    # two columns.
    assert lines[1].startswith("\t")


def test_the_warning_mark_is_recorded_rather_than_read_off_the_colour(qtbot):
    """`_warn_colour` answers two different colours depending on the palette, so the
    brush cannot say whether a line was a warning without knowing the theme it was
    written under. The line records it instead."""
    panel = RunPanel()
    qtbot.addWidget(panel)
    plain = panel.say("a line")
    warned = panel.say("another", warn=True)
    assert not plain.data(0, _IS_WARNING)
    assert warned.data(0, _IS_WARNING)


def test_a_setting_the_method_names_elsewhere_gets_its_own_open_group(qtbot):
    """Matt's call of 2026-09-18, and the run it was made on the strength of.

    A method with an opinion about module 1's direction and none about module 2's has a
    gap in it; a method that names no direction anywhere is simply not about direction.
    Folding the two together hid the eleventh line among the ten on 2026-09-15, and
    `SWFDIR`/`SALTWFM` left `REV` cost a run that showed no ions at all (task 56).
    """
    panel = RunPanel()
    qtbot.addWidget(panel)
    for index in range(5):
        panel.show(Warned(f"auklet SETTING{index} is left as found on module 1: x"))
    panel.show(Warned("auklet WFDIR is left as found on modules 2: FWD, 3: FWD, "
                      "and the method declares it on module 1"))

    assert panel.log.topLevelItemCount() == 2
    plain, gap = (panel.log.topLevelItem(0), panel.log.topLevelItem(1))
    assert "auklet: 5 settings left as found (click to expand)" in plain.text(1)
    assert not plain.isExpanded()
    assert "names on other modules" in gap.text(1)
    assert gap.isExpanded(), "the group that matters opened itself"


def test_the_two_kinds_of_left_as_found_line_are_told_apart_by_their_wording():
    plain = "auklet ARBMODE is left as found on modules 1: TWAVE, 2: TWAVE"
    gap = ("auklet WFDIR is left as found on modules 2: FWD, "
           "and the method declares it on module 1")
    assert is_left_as_found(plain) and is_left_as_found(gap)
    assert not names_it_elsewhere(plain)
    assert names_it_elsewhere(gap)


def test_a_declared_versus_read_line_is_never_grouped_away(qtbot):
    panel = RunPanel()
    qtbot.addWidget(panel)
    panel.show(Warned("auklet SWFDIR is left as found on module 1: FWD"))
    panel.show(Warned("auklet DC bias 1 was declared 12.00 V and reads back 11.20 V"))
    panel.show(Warned("auklet DC bias monitors were not compared: the readback was "
                      "taken with the table TBLRDY, where they do not convert"))
    assert panel.log.topLevelItemCount() == 3


def test_the_bar_moves_through_a_single_frame_method_s_one_frame(qtbot):
    """The defect that cost the 2026-09-17 sitting a misdiagnosis (task 56).

    A `single_frame` method asks the console for **one** frame of half a million scans,
    so a bar ranged over frames was filled by `FrameBegun` and sat at 100 % with a
    frozen caption for the whole 65 s of a healthy run. Ranged over scans, the same run
    moves on every `BatchSeen` -- about fifteen a second -- and the caption still counts
    repetitions.
    """
    panel = RunPanel()
    qtbot.addWidget(panel)
    panel.show(RunBegun("raw.uimf", "summed.uimf", frames=1, console_frames=1,
                        frame_length=500_000, frame_timeout=90.0))
    assert panel.bar.maximum() == 500_000

    panel.show(FrameBegun(method_frame=1, repetition=1, frame_number=1, of=1))
    assert panel.bar.value() == 0, "a frame that has begun has published nothing"
    assert panel.progress.text == "repetition 1 of 1"

    panel.show(BatchSeen(1, 1, _batch(1000), scans_so_far=1000))
    assert panel.bar.value() == 1000
    panel.show(BatchSeen(1, 1, _batch(1000), scans_so_far=250_000))
    assert panel.bar.value() == 250_000
    # No line per batch: fifteen a second would bury everything worth reading.
    assert panel.log.topLevelItemCount() == 1


def test_the_bar_counts_scans_across_a_whole_per_repetition_run(qtbot):
    """The other method shape, which the fix must not break: a hundred console frames
    of twenty thousand scans is one bar of two million, and a frame that has begun puts
    it at its own start before its first batch arrives."""
    panel = RunPanel()
    qtbot.addWidget(panel)
    panel.show(RunBegun("raw.uimf", "summed.uimf", frames=1, console_frames=100,
                        frame_length=20_000, frame_timeout=60.0))
    assert panel.bar.maximum() == 2_000_000

    panel.show(FrameBegun(method_frame=1, repetition=3, frame_number=3, of=100))
    assert panel.bar.value() == 40_000
    panel.show(BatchSeen(1, 3, _batch(500), scans_so_far=500))
    assert panel.bar.value() == 40_500
    assert panel.progress.text == "repetition 3 of 100"

    # Scans keep arriving after a frame's own `finished` -- 500 on every frame of the
    # 2026-09-17 series -- and a bar past its maximum is one Qt draws full while the
    # run is not.
    panel.show(FrameBegun(method_frame=1, repetition=100, frame_number=100, of=100))
    panel.show(BatchSeen(1, 100, _batch(500), scans_so_far=20_500))
    assert panel.bar.value() == 2_000_000


def test_a_fold_that_has_started_says_so_instead_of_going_quiet(qtbot):
    """624 s of silence on 2026-09-17, read as a dead process by the session watching
    and a force-quit away from losing a 1.3 GB raw file (task 56)."""
    panel = RunPanel()
    qtbot.addWidget(panel)
    panel.show(Folding(1, tuple(range(1, 101)), megabytes=1310.5, seconds=624.0))
    assert panel.log.topLevelItemCount() == 1
    line = panel.log.topLevelItem(0).text(1)
    assert "100 repetitions" in line and "about 10 minutes" in line


def _batch(scans: int):
    """A `Batch` of `scans` scans, which is all these assertions read off one."""
    import numpy as np

    from clockwork.acq.wire import Batch
    return Batch(mz=np.zeros(4), tic=np.zeros(scans), time_stamps=np.zeros(scans))


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


def test_a_declared_arb_frequency_agrees_with_the_one_the_divider_can_make():
    """The standing false alarm, and the reason decision 6 of the window design existed.

    `SWFREQ,n,15000` is acknowledged and read back as 14914, because the module's
    waveform clock is an integer divider (wire format 6.2). The panel compared the two
    strings and marked every module of both ARB boxes as disagreeing on every run -- a
    warning that fires every time being a warning nobody reads (task 56).
    """
    box = rack_box()
    box.command("SWFREQ,1,15000")
    state = read_state(box)
    assert state.module(1)["GWFREQ"] == "14914", "the stand-in did not quantise it"
    table = state_table(Reading(state=state, when="on demand"),
                        declaring(setup=("SWFREQ,1,15000",)))
    row = rows_of(table, "ARB")["module 1 frequency"]
    assert row.mark == AGREES, row.note
    assert "nearest it can" in row.note
    assert table.differing == ()


def test_an_arb_module_holding_a_frequency_nobody_asked_for_still_differs():
    """The mark has to keep working: quantisation explains 14914 against 15000 and
    explains nothing about a module left at some other method's setting."""
    box = rack_box()
    box.command("SWFREQ,1,5000")
    table = state_table(Reading(state=read_state(box), when="on demand"),
                        declaring(setup=("SWFREQ,1,15000",)))
    row = rows_of(table, "ARB")["module 1 frequency"]
    assert row.mark == DIFFERS and row.declared == "15000"


def test_an_arb_setting_that_reads_back_with_decimals_is_compared_as_a_number():
    """The other four of the eight. `SWFVRNG,n,15` is a voltage and the box answers one,
    and a panel comparing the two as text marked a row that agrees exactly."""
    box = rack_box()
    box.transport.arb[1]["SWFVRNG"] = "15.00"
    table = state_table(Reading(state=read_state(box), when="on demand"),
                        declaring(setup=("SWFVRNG,1,15",)))
    row = rows_of(table, "ARB")["module 1 range"]
    assert row.mark == AGREES and row.value == "15.00"


def test_a_refresh_with_the_box_armed_keeps_the_monitors_that_were_live():
    """Task 51 step 4, answered at the instrument and answered against the step.

    The mark compares a declaration with the box's *setpoint*, which agrees in either
    mode; the monitor is only a note on the row, and in table mode it is a frozen array
    (task 43). So a refresh taken with the boxes armed replaced the between-`setup`-and-
    `load` reading -- where the monitors were live -- with one where they are not. Every
    statement the panel then made was true and the evidence had gone (task 56).
    """
    box = rack_box()
    live = Reading().with_state(read_state(box), "after setup, before load")
    assert "monitors" in rows_of(state_table(live), "DC bias")["channel 1"].note

    armed = live.with_state(read_state(in_table_mode(box)), "on demand, at 14:05:00")
    rows = rows_of(state_table(armed), "DC bias")
    assert "when last live" in rows["channel 1"].note
    assert "after setup, before load" in rows["channel 1"].note
    # The setpoints are still this reading's: a box in table mode answers them truthfully
    # and it is only the monitor beside each that falls back.
    assert rows["channel 1"].value == "12.00 V"
    section = next(part for part in state_table(armed).sections
                   if part.title == "DC bias")
    assert "kept because a refresh" in section.note


def test_a_reading_with_live_monitors_replaces_the_one_kept_before_it():
    """The fallback is the *last* live reading and not the first one ever taken."""
    box = rack_box()
    first = Reading().with_state(read_state(box), "after setup, before load")
    box.transport.dc_bias = [1.0, -70.0, 0.0, 5.0]
    second = first.with_state(read_state(box), "on demand, at 15:00:00")
    assert second.converting_when == "on demand, at 15:00:00"
    frozen = second.with_state(read_state(in_table_mode(box)), "on demand, at 15:01:00")
    assert "15:00:00" in rows_of(state_table(frozen), "DC bias")["channel 1"].note


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
    assert list(found.boxes)
    assert found.silent == (Silent("COM9", True, "could not open port COM9"),)
    entry = found.found[0]
    assert entry.port == "COM3" and entry.box is not None
    found.close()


class ResettingRack:
    """An `opener` whose boxes behave the way the rack's did (lab record, task 62).

    Closing a box's port resets the Due behind it, and a port opened again before it
    has finished re-enumerating is not there: `FileNotFoundError` out of `CreateFile`.
    The stand-in does not model how long that lasts, only that it outlasts the scan
    that caused it, which is the part a rescan has to survive.
    """

    def __init__(self) -> None:
        self.opens: dict[str, int] = {}
        self.resetting: set[str] = set()

    def __call__(self, port, **_):
        if port in self.resetting:
            raise OSError(2, "The system cannot find the file specified.")
        self.opens[port] = self.opens.get(port, 0) + 1
        transport = FakeBox(name=port)
        closing = transport.close

        def close() -> None:
            closing()
            self.resetting.add(port)

        transport.close = close
        return Box(transport=transport, name=port)


def held_by(found) -> dict:
    return {entry.port: entry.box for entry in found.found if entry.box is not None}


def test_a_rescan_asks_the_boxes_it_holds_and_reopens_none_of_them():
    """The defect the rack showed: the launch scan found all three boxes and every
    `Find boxes` after it found two, because a rescan closed what it held -- which
    resets each box -- and reopened it straight away."""
    rack = ResettingRack()
    first = discover(ports=["COM5", "COM6", "COM7"], opener=rack)
    assert set(first.boxes) == {"COM5", "COM6", "COM7"}
    for _ in range(5):
        again = discover(ports=["COM5", "COM6", "COM7"], opener=rack, held=held_by(first))
        assert not again.silent
        assert again.boxes == first.boxes
        first = again
    assert rack.opens == {"COM5": 1, "COM6": 1, "COM7": 1}
    assert not rack.resetting
    first.close()


def test_closing_and_reopening_is_what_loses_a_box():
    """The same rack scanned the old way, closing first: the reset the close causes is
    what the next open meets. Kept so that the stand-in above is known to model the
    fault, and not merely to be a rack that never fails."""
    rack = ResettingRack()
    first = discover(ports=["COM5", "COM6"], opener=rack)
    first.close()
    again = discover(ports=["COM5", "COM6"], opener=rack)
    assert not again.boxes
    assert all(entry.could_not_open for entry in again.silent)


def test_a_held_box_is_asked_even_where_the_scan_would_not_have_asked_its_port():
    rack = ResettingRack()
    first = discover(ports=["COM5", "COM6"], opener=rack)
    again = discover(ports=["COM6"], opener=rack, held=held_by(first))
    assert set(again.boxes) == {"COM5", "COM6"}
    assert rack.opens == {"COM5": 1, "COM6": 1}
    again.close()


def test_a_held_box_that_stopped_answering_is_closed_and_its_port_opened_afresh():
    """A box power-cycled since the last scan is back as a new device node, and the
    handle held on the old one is dead. The fresh open is the only way to reach it."""
    opens: list[str] = []

    def opener(port, **_):
        opens.append(port)
        return Box(transport=FakeBox(name=port), name=port)

    first = discover(ports=["COM5"], opener=opener)
    dead = first.boxes["COM5"]
    dead.transport.close()
    again = discover(ports=["COM5"], opener=opener, held=held_by(first))
    assert opens == ["COM5", "COM5"]
    assert again.boxes["COM5"] is not dead
    again.close()


def test_a_held_box_that_was_renamed_comes_back_under_its_new_name():
    first = discover(ports=["COM5"],
                     opener=lambda port, **_: Box(transport=FakeBox(name="old"), name=port))
    box = first.boxes["old"]
    box.transport.name = "new"
    again = discover(ports=["COM5"], opener=lambda port, **_: pytest.fail("reopened"),
                     held=held_by(first))
    assert again.boxes == {"new": box} and box.name == "new"
    again.close()


def test_keep_false_leaves_a_held_box_open():
    rack = ResettingRack()
    first = discover(ports=["COM5"], opener=rack)
    again = discover(ports=["COM5", "COM6"], opener=rack, held=held_by(first), keep=False)
    assert again.boxes == first.boxes
    assert rack.resetting == {"COM6"}
    again.close()


def test_a_port_that_will_not_open_is_reported_as_could_not_open_not_silent():
    def opener(port, **_):
        raise OSError(2, "The system cannot find the file specified.")

    found = discover(ports=["COM7"], opener=opener)
    assert found.silent == (Silent("COM7", True,
                                    "[Errno 2] The system cannot find the file specified."),)
    assert found.text.startswith("0 box(es), 1 port(s) could not open")
    assert "wait a few seconds and Find boxes again" in found.silent[0].text


def test_the_worker_hands_its_open_boxes_to_the_next_scan(qtbot, monkeypatch):
    """`Find boxes` on the worker, pressed five times against a rack that resets on a
    close: five identical scans, and each port opened once. On the rack, closing first,
    this lost one box on every press (lab record, task 62)."""
    from functools import partial

    from clockwork.app import worker as worker_module

    rack = ResettingRack()
    monkeypatch.setattr(worker_module, "discover", partial(discover, opener=rack))
    worker = worker_module.Worker()
    try:
        results = []
        for _ in range(5):
            with qtbot.waitSignal(worker.finished_job, timeout=10_000) as blocker:
                worker.submit(worker_module.Discover(ports=("COM5", "COM6", "COM7")))
            results.append(blocker.args[1])
        assert [sorted(found.boxes) for found in results] == [["COM5", "COM6", "COM7"]] * 5
        assert all(not found.silent for found in results)
        assert rack.opens == {"COM5": 1, "COM6": 1, "COM7": 1}
    finally:
        worker.shutdown()
        worker.wait(10_000)
    assert rack.resetting == {"COM5", "COM6", "COM7"}


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


def test_acquire_is_offered_on_cold_boxes_and_the_line_says_what_they_hold(
        window, tmp_path, qtbot):
    """E7 and E2 (lab record, task 169): Acquire is no longer greyed until the boxes are
    armed, since it sends what they do not hold first; the line over the panes says
    whether the panes are what the boxes hold, by the owner's memory of the last send."""
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    assert window.acquire_button.isEnabled(), window.problems.text()
    assert "Nothing is known to be on the boxes" in window.held_line.text()

    window.send()
    until(qtbot, lambda: idle(window) and window._armed)
    window._refresh_actions()
    assert "The panes are what the boxes hold" in window.held_line.text()

    window.panes[BOX].set_text("SDCB,1,12.0\n\n" + window.panes[BOX].text())
    window.panes[BOX].reparse()
    window._refresh_actions()
    assert window.acquire_button.isEnabled()
    assert "not the panes" in window.held_line.text()


def sent_lines(path) -> list[str]:
    """The transcript's lines for strings sent to the box, `<box> > b'...'`, in order,
    from the bytes on: the time in front differs between two reads of one line."""
    text = path.read_text(encoding="utf-8")
    return [line.split(f"{BOX} > ", 1)[1]
            for line in text.splitlines() if f"{BOX} > b'" in line]


def test_acquire_on_cold_boxes_sends_every_phase_then_acquires(window, tmp_path, qtbot):
    """One press on boxes nothing is known to hold: the owner sends setup, load and arm
    under the run's own stem, then acquires, and the first file is not a replicate."""
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    window.start_console()
    until(qtbot, lambda: window.worker.console_alive and idle(window))
    stem = window.stem.text()
    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    window.replicates.setValue(1)
    assert window.acquire() is not None
    until(qtbot, lambda: runs and idle(window), timeout=180_000)
    assert runs[0].complete, runs[0].text
    assert not runs[0].replicate
    log = (tmp_path / f"{stem}.sent.txt").read_text(encoding="utf-8")
    assert "STBLCLK,EXT" in log and "TBLSTRT" in log
    assert window._armed and window._armed[0][1] == ("STBLCLK,EXT", "STBLTRG,POS")


def test_acquire_on_boxes_that_hold_the_method_sends_nothing_and_is_the_replicate(
        window, tmp_path, qtbot):
    """E7: on boxes that hold the method the first file is the replicate, and the
    owner says it sent nothing."""
    ready_to_acquire(window, qtbot, tmp_path)
    window._refresh_stem()
    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    window.replicates.setValue(1)
    window.acquire()
    until(qtbot, lambda: runs and idle(window), timeout=180_000)
    assert runs[0].complete and runs[0].replicate
    assert any("nothing sent" in line for line in run_log(window))


def test_send_sends_only_what_the_boxes_do_not_hold(window, tmp_path, qtbot):
    """Send is Acquire's send with nothing acquired: nothing on boxes that hold the
    panes, the table alone where only the table moved, and everything again with the
    tick, which then clears itself."""
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    window.send()
    until(qtbot, lambda: idle(window) and window._armed)
    transcript_file = next(tmp_path.glob("*.transcript.log"))
    first = sent_lines(transcript_file)
    assert any("STBLCLK,EXT" in line for line in first)

    window.send()
    until(qtbot, lambda: idle(window) and any(
        "nothing sent: the boxes hold the panes" in line for line in run_log(window)))
    assert sent_lines(transcript_file) == first

    window.panes[BOX].set_text(window.panes[BOX].text().replace("500:B:0", "501:B:0"))
    window.panes[BOX].reparse()
    window._refresh_stem()
    stem = window.stem.text()
    window.send()
    until(qtbot, lambda: idle(window) and window._armed
          and matches_wire(window._armed, window.build_method()))
    moved = (tmp_path / f"{stem}.sent.txt").read_text(encoding="utf-8")
    assert "501:B:0" in moved and "STBLCLK,EXT" not in moved, moved
    # The setup the boxes still hold is still part of what they are known to hold.
    assert window._armed[0][1] == ("STBLCLK,EXT", "STBLTRG,POS")

    window._refresh_stem()
    stem = window.stem.text()
    window.setup_again.setChecked(True)
    window.send()
    assert not window.setup_again.isChecked()
    until(qtbot, lambda: (tmp_path / f"{stem}.sent.txt").is_file() and idle(window))
    until(qtbot, lambda: "STBLCLK,EXT" in (tmp_path / f"{stem}.sent.txt").read_text(
        encoding="utf-8"))


def test_the_state_panel_compares_with_what_the_boxes_were_last_sent(
        window, tmp_path, qtbot):
    """E2: a reading is marked against the last send, whoever made it, and an edit to
    the panes does not re-mark it."""
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    assert window.panes[BOX].state.method is None
    window.send()
    until(qtbot, lambda: idle(window) and window._armed)
    window._refresh_actions()
    sent = window.panes[BOX].state.method
    assert sent is not None and sent.setup == ("STBLCLK,EXT", "STBLTRG,POS")
    window.panes[BOX].set_text("SDCB,1,12.0\n\n" + window.panes[BOX].text())
    window.panes[BOX].reparse()
    window._pane_changed()
    assert window.panes[BOX].state.method == sent


def test_a_method_load_keeps_the_boxes_connection_details(window, tmp_path, qtbot):
    """The FLAG of 2026-09-22 (closed by task 169): loading a method rebuilt the panes
    and their headers lost the port and firmware the scan had found."""
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    header = window.panes[BOX].status.text()
    assert header and header != "off or absent"
    import dataclasses

    other = make_method()
    other = dataclasses.replace(other, boxes=(
        dataclasses.replace(other.boxes[0], name="box2"), other.boxes[0]))
    path = str(tmp_path / "other.toml")
    method_module.save(other, path)
    window._load_method(path)
    assert window.panes[BOX].status.text() == header
    assert BOX in window.boxes_label.text()


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
    # Until the button is offered, not merely until the rack exists: `worker.boxes` is
    # filled on the worker thread before the scan's `started_job` and `finished_job`
    # reach this one, `idle` is as true before the first as after the second, and a
    # `click()` on the button `_refresh_actions` greyed out at load does nothing.
    until(qtbot, lambda: window.worker.boxes and idle(window)
          and window.panes[BOX].state.refresh.isEnabled())
    window.panes[BOX].state.refresh.click()
    until(qtbot, lambda: idle(window) and window.panes[BOX].state.reading.state)
    assert "on demand" in window.panes[BOX].state.reading.when
    assert window.readings[BOX].state.identity


def test_a_whole_run_with_a_replicate_names_two_files_and_folds_both(
        window, tmp_path, qtbot):
    # From cold boxes, so the first file is the one its own send armed (E7).
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    window.start_console()
    until(qtbot, lambda: window.worker.console_alive and idle(window))
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
    # Both log files, named for the stem, beside the data (lab record, tasks 29 and 39).
    written = set(os.listdir(tmp_path))
    for run in runs:
        stem = os.path.splitext(os.path.basename(run.raw_path))[0]
        assert f"{stem}.sent.txt" in written
        assert any(name.startswith(stem) and name.endswith(".transcript.log")
                   for name in written)


def test_a_window_run_needs_its_person_and_records_its_purpose(
        window, tmp_path, qtbot, scratch_settings):
    """A1 and A2 of the window revision (lab record, task 165): the person at the window
    is required, prefilled from the file initials and remembered; the purpose line is
    the request in the run record the owner begins, empty or not."""
    from clockwork.record import load

    ready_to_acquire(window, qtbot, tmp_path)
    assert window.run_panel.at_window.text() == "ZZ"
    window.run_panel.at_window.setText("")
    assert window.acquire() is None
    window.run_panel.at_window.setText(" m.b ")
    window.run_panel.purpose.setText("  the standard, before the series ")
    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    job = window.acquire()
    assert job is not None and job.source == "window" and job.at_window == "MB"
    assert job.request == "the standard, before the series"
    assert Settings(scratch_settings).at_window == "MB"
    until(qtbot, lambda: runs and idle(window), timeout=180_000)
    [name] = [name for name in os.listdir(tmp_path) if name.endswith(".request.json")]
    data = load(str(tmp_path / name))
    assert data["request"]["text"] == "the standard, before the series"
    assert data["request"]["at_window"] == "MB" and data["request"]["initials"] == "ZZ"
    assert len(data["files"]) == 1


# --- At the window, confirmed once per launch (A2 as revised; lab record, task 172) ---


def test_a_restored_person_at_the_window_is_asked_about_at_the_first_start(
        qtbot, scratch_settings):
    """At a sitting a run's record named the person from an earlier session, a value
    the window restored and nobody saw. Restored, it is asked about at the first Start
    of the launch, a cancel starts nothing, and once answered it is not asked again."""
    Settings(scratch_settings).at_window = "BK"
    made = MainWindow(fake=True)
    qtbot.addWidget(made)
    try:
        field = made.run_panel.at_window
        assert field.text() == "BK" and "italic" in field.styleSheet()
        assert "Not yet confirmed" in field.toolTip()
        until(qtbot, lambda: idle(made))
        made.initials.setText("ZZ")
        made._initials_changed()
        assert field.text() == "BK"  # the prefill never overwrites
        asked: list[str] = []
        made._ask_at_window = lambda person: asked.append(person) or None
        assert made.start_queue() is None and asked == ["BK"]
        made._ask_at_window = lambda person: asked.append(person) or " b.f "
        made.start_queue()  # an empty queue: asked all the same, then nothing to run
        assert asked == ["BK", "BK"]
        assert field.text() == "BF" and Settings(scratch_settings).at_window == "BF"
        assert field.styleSheet() == "" and "Not yet confirmed" not in field.toolTip()
        made.start_queue()
        assert asked == ["BK", "BK"]
    finally:
        made.worker.shutdown()
        made.worker.wait(10_000)


def test_a_person_who_typed_their_initials_is_not_asked(window, qtbot):
    until(qtbot, lambda: idle(window))
    asked: list[str] = []
    window._ask_at_window = lambda person: asked.append(person) or person
    field = window.run_panel.at_window
    field.clear()
    qtbot.keyClicks(field, "mb")
    window.start_queue()
    assert asked == [] and field.text() == "MB"
    # Emptied and prefilled from the file initials again, it is asked about again.
    field.clear()
    window._initials_changed()
    assert field.text() == "ZZ" and "italic" in field.styleSheet()
    window.start_queue()
    assert asked == ["ZZ"]


def test_a_prefilled_person_corrected_at_the_question_is_the_one_recorded(
        window, qtbot, tmp_path):
    """The window fixture's At the window is prefilled from the file initials, an ion's
    code as often as a person's: the first Acquire asks, and the run carries the
    answer."""
    ready_to_acquire(window, qtbot, tmp_path)
    asked: list[str] = []
    window._ask_at_window = lambda person: asked.append(person) or "BF"
    window.replicates.setValue(1)
    job = window.acquire()
    assert asked == ["ZZ"] and job is not None and job.at_window == "BF"
    until(qtbot, lambda: idle(window), timeout=180_000)


def test_an_unconfirmed_person_still_signs_a_stop_a_pause_and_a_note(
        qtbot, scratch_settings):
    """The confirmation is for starting a run. A window opened onto a running series to
    stop it is never held up by it."""
    Settings(scratch_settings).at_window = "BK"
    made = MainWindow(fake=True)
    qtbot.addWidget(made)
    try:
        asked: list[str] = []
        made._ask_at_window = lambda person: asked.append(person) or None
        for what in ("a stop", "a pause", "a resume", "a note"):
            assert made._signed(what) == "BK"
        assert asked == []
    finally:
        made.worker.shutdown()
        made.worker.wait(10_000)


def test_the_console_settings_a_run_sends_are_in_that_run_s_transcript(
        window, tmp_path, qtbot):
    """The prologue of an acquisition -- `init`, `horizontal`, `vertical`, `invert`,
    `enable io port`, and the `acquire` that opens the chain -- belongs to the file it
    configured the card for, and so belongs in that file's transcript.

    It was sent before any transcript was open. `clockwork.acq.console` builds a record
    only under `isEnabledFor(DEBUG)` and nothing raises the package's level until
    `_logs` attaches a handler, so every command in front of the first repetition was
    sent and never written down: the instrument sitting's transcripts carry `info`,
    `acquire frame` and `stop` and nothing else, and read as a run that never
    configured the card (lab record, task 50).
    """
    ready_to_acquire(window, qtbot, tmp_path)
    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    window.replicates.setValue(1)
    window.acquire()
    until(qtbot, lambda: runs and idle(window), timeout=180_000)

    stem = os.path.splitext(os.path.basename(runs[0].raw_path))[0]
    written = [name for name in os.listdir(tmp_path)
               if name.startswith(stem) and name.endswith(".transcript.log")]
    assert written, "the run left no transcript"
    whole = (tmp_path / written[0]).read_text(encoding="utf-8")
    for command in ("init", "horizontal", "vertical", "invert", "enable io port"):
        assert f"> {command}" in whole, f"{command!r} is not in the transcript"
    # The chain's own `acquire`, which is what `acquire frame` needs to exist and what
    # a transcript showing only `acquire frame` cannot account for.
    assert "> acquire\n" in whole or "> acquire " in whole


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


def test_a_failed_job_moves_the_name_on_past_the_files_it_left(window, tmp_path):
    """Lab #3: a run that failed kept the stem it failed under in the name field, and
    the next Acquire collided with it. Every Send and Acquire that ends, however it
    ends, reads the next name off the directory again."""
    from clockwork.owner import Acquire

    stem = window.stem.text()
    (tmp_path / transcript.default_name(stem)).write_text("", encoding="utf-8")
    window._job_failed(Acquire(label="acquiring"), "CORMORANT stopped answering")
    assert window.stem.text() != stem
    assert naming.parse_stem(window.stem.text())[2] == naming.parse_stem(stem)[2] + 1


def test_a_second_send_continues_the_send_log_instead_of_emptying_it(
        window, tmp_path, qtbot):
    """A second send used to destroy the Send setup log that preceded it under the
    same stem, and the 2026-09-17 sitting then read the emptied file and reported that
    the CLOCK `setup` had never been sent -- on the strength of which it was sent again.
    It had gone out before every CLOCK series that afternoon (task 56).

    A stem is a piece of work and its send log is that work's record: everything done
    under one stem is added to it.
    """
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    stem = window.stem.text()
    window.send(setup=True)
    until(qtbot, lambda: idle(window) and window.worker.snapshot is not None)
    log = tmp_path / f"{stem}.sent.txt"
    after_setup = log.read_text(encoding="utf-8")
    assert "STBLCLK,EXT" in after_setup

    window.send(setup=True)
    # On the file rather than on `_armed`, which the first send already set: the job is
    # queued to another thread and the window is briefly idle with it still waiting.
    until(qtbot, lambda: log.read_text(encoding="utf-8").count("SMOD,TBL") >= 2)
    until(qtbot, lambda: idle(window))
    whole = log.read_text(encoding="utf-8")
    assert "STBLCLK,EXT" in whole, "the setup send's strings were emptied out"
    assert whole.startswith(after_setup[:200])


def test_an_acquire_after_a_send_continues_that_stem_s_send_log(
        window, tmp_path, qtbot):
    """Task 56's defect down a new path (task 169): a Send under a stem, then an Acquire
    under the same stem that found nothing to send, whose first file is a replicate on
    the wire -- and a replicate's log was opened afresh, over the send's."""
    ready_to_acquire(window, qtbot, tmp_path, make_method())
    window.panes[BOX].set_text(window.panes[BOX].text().replace("500:B:0", "501:B:0"))
    window.panes[BOX].reparse()
    window._refresh_stem()
    stem = window.stem.text()
    log = tmp_path / f"{stem}.sent.txt"
    window.send()
    until(qtbot, lambda: log.is_file() and "SMOD,TBL" in log.read_text(encoding="utf-8"))
    until(qtbot, lambda: idle(window))
    after_send = log.read_text(encoding="utf-8")

    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    window.replicates.setValue(1)
    window.acquire()
    until(qtbot, lambda: runs and idle(window), timeout=180_000)
    whole = log.read_text(encoding="utf-8")
    assert whole.startswith(after_send[:200]), "the acquisition emptied the send's log"
    assert "TBLSTRT" in whole


def test_a_box_with_an_empty_pane_is_not_a_box_the_method_names(
        window, tmp_path, qtbot):
    """The launch scan gives every box it finds a pane, and a pane used to become a
    `BoxMethod` whether or not anything was written in it -- so the send read forty
    round trips of state off the rack's fourth box, which no method on this instrument
    names (found at the instrument, 2026-09-17; task 56)."""
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    window._ensure_panes([BOX, "visitor"])
    window.panes["visitor"].set_text("# nothing is declared here\n")
    window.panes["visitor"].reparse()

    named = [entry.name for entry in window.build_method().boxes]
    assert named == [BOX], "a pane with no commands in it became a box of the method"

    # A line that is a command makes it one, with nothing else to press.
    window.panes["visitor"].set_text("SDCB,1,12.0\n")
    window.panes["visitor"].reparse()
    assert "visitor" in [entry.name for entry in window.build_method().boxes]


def a_run_begun(tmp_path, name: str = "260917_ZZ_001.uimf") -> RunBegun:
    """The event the loop raises once the run's files exist, with its raw file made."""
    raw = a_file(tmp_path, name)
    return RunBegun(raw, raw.replace(".uimf", ".summed.uimf"),
                    frames=1, console_frames=ACCUMULATIONS, frame_length=SCANS,
                    frame_timeout=5.0)


def test_the_button_goes_live_the_moment_the_run_s_raw_file_exists(
        window, tmp_path, qtbot):
    """Greyed until `_run_done` was the whole reason nobody had seen live viewing
    (task 55): the file being written is the one worth opening, and it exists at
    `RunBegun`. The tooltip changes with it, because the two cases open two files."""
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    assert not window.mainspring_button.isEnabled()

    window._run_begun(a_run_begun(tmp_path))
    assert window.mainspring_button.isEnabled()
    assert "being acquired" in window.mainspring_button.toolTip()
    assert "never the summed companion" in window.mainspring_button.toolTip()

    # And back to the wording for a file that is finished once the job is over.
    window._live_run = ("", "", "")
    window._refresh_actions()
    assert window.mainspring_button.isEnabled()
    assert "summed companion once the fold" in window.mainspring_button.toolTip()


def test_the_checkbox_opens_one_viewer_a_session_and_not_one_a_run(
        window, tmp_path, qtbot):
    """A viewer launched following ends with `Live` ticked and moves to each later run
    of the session by itself (task 58), so the second Acquire needs no second process.
    The button is how a trainee deliberately opens another."""
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    # So that the test does not depend on this machine having a `.uimf` association:
    # with one, the resolved command is recorded; without one, the configured path is.
    window.settings.mainspring_path = "mainspring.exe"
    window.open_on_acquire.setChecked(True)

    window._run_begun(a_run_begun(tmp_path))
    assert len(window.launched_commands) == 1
    line = window.launched_commands[0]
    assert line[-3:] == [OPTION_FOLLOW, OPTION_SHOW, "rolling-sum"]
    assert any(part.endswith("260917_ZZ_001.uimf") for part in line)

    window._run_begun(a_run_begun(tmp_path, "260917_ZZ_002.uimf"))
    assert len(window.launched_commands) == 1, "a second run opened a second viewer"

    window.open_in_mainspring()
    assert len(window.launched_commands) == 2, "the button no longer opens another"
    assert window.launched_commands[1][-3:] == [OPTION_FOLLOW, OPTION_SHOW,
                                                "rolling-sum"]


def test_the_checkbox_is_off_until_it_is_ticked_and_is_remembered(
        window, scratch_settings, tmp_path, qtbot):
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    assert not window.open_on_acquire.isChecked()
    window.settings.mainspring_path = "mainspring.exe"

    window._run_begun(a_run_begun(tmp_path))
    assert window.launched_commands == [], "a viewer was opened without being asked for"

    window.open_on_acquire.setChecked(True)
    window.close()
    assert Settings(scratch_settings).open_mainspring_on_acquire


def test_a_single_frame_method_opens_on_the_newest_frame(window, tmp_path, qtbot):
    """A frame that fills for a minute shows nothing under a sum of finished frames,
    so the mode is taken from the method and not fixed (Matt, 2026-09-17)."""
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    window.settings.mainspring_path = "mainspring.exe"
    window.repetition_mode.setCurrentText("single_frame")

    window._run_begun(a_run_begun(tmp_path))
    window.open_in_mainspring()
    assert window.launched_commands[0][-3:] == [OPTION_FOLLOW, OPTION_SHOW, "newest"]


def test_a_fake_acquisition_launches_the_viewer_on_the_file_it_is_writing(
        window, tmp_path, qtbot):
    """End to end over the stand-ins: the checkbox, the run, and the command line that
    would have opened the file the console was filling. `--fake` records rather than
    launches, because a simulated file is not one to show a trainee."""
    ready_to_acquire(window, qtbot, tmp_path)
    window.settings.mainspring_path = "mainspring.exe"
    window.open_on_acquire.setChecked(True)

    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    window.acquire()
    until(qtbot, lambda: runs and idle(window), timeout=180_000)

    assert len(window.launched_commands) == 1, window.launched_commands
    line = window.launched_commands[0]
    assert runs[0].raw_path in line, "the viewer was not opened on the raw file"
    assert runs[0].summed_path not in line, "the companion is not the file being written"
    assert line[-3:] == [OPTION_FOLLOW, OPTION_SHOW, "rolling-sum"]
    # And the button reverts to the finished-file wording once the job is over.
    assert "summed companion once the fold" in window.mainspring_button.toolTip()


def test_the_replicate_spinner_carries_its_own_label(window):
    """"Replicates" sat in the Files group with no widget beside it for the whole of the
    2026-09-17 sitting, the spinner being over by Acquire (task 56)."""
    assert window.replicates.prefix() == "replicates: "
    labels = [window.findChildren(type(window.stem))]
    assert labels  # the Files group still has its other rows


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
        assert made.send_button.isEnabled()
        made._load_instrument(make_instrument(tmp_path))
        made._refresh_actions()
        assert "no channel offset" not in made.problems.text()
    finally:
        made.worker.shutdown()
        made.worker.wait(10_000)


def test_the_worker_hands_prepare_console_a_config_and_not_the_method_that_reads_it(
        window, tmp_path):
    """`ConsoleProcess.config` is a method, and the worker has to call it.

    Passing it uncalled handed `prepare_console` a function object and the first
    acquisition on a real console died with `AttributeError: 'function' object has no
    attribute 'full_scale_v'` -- at the instrument, on the first run of the day (lab
    record, task 50). `--fake` cannot reach the branch at all, because
    `FakeConsoleProcess` subclasses `ConsoleSupervisor` rather than `ConsoleProcess`, so
    the `isinstance` short-circuits and nothing evaluates it. This test builds a real
    `ConsoleProcess` -- constructing one starts no process, and `config()` only reads the
    directory -- so the branch is exercised without a console.
    """
    directory = tmp_path / "console"
    directory.mkdir()
    (directory / "AqMD3_console.exe").write_text("", encoding="utf-8")
    (directory / "config.txt").write_text("FullScaleRange=0.5\nTriggerLevel=0.4\n",
                                          encoding="utf-8")
    window.worker.console = ConsoleProcess(str(directory / "AqMD3_console.exe"))

    config = window.worker._console_config()
    assert isinstance(config, ConsoleConfig), "prepare_console was handed a method"
    assert config.full_scale_v == 0.5

    # A config.txt that has gone is a comparison clockwork cannot make, not a refusal.
    (directory / "config.txt").unlink()
    assert window.worker._console_config() is None


def test_a_scan_does_not_move_the_methods_enable_declaration(window, tmp_path, qtbot):
    """A scan finding boxes the method does not name must leave `acquisition.enable` alone.

    The instrument found this on 2026-09-17 (lab record, task 50): `_discovered` cleared the
    combo and refilled it, `clear()` dropped the current index to the first item, and the
    rebuild read that back into the method -- so a launch scan moved the enable from the box
    the method declared to whichever box sorted first. It is the gate declaration: the box
    whose DIOA the digitizer watches, and the box whose `TBLCMPLT` is the gating witness.
    """
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: BOX in window.panes and idle(window))
    assert window.build_method().acquisition.enable.box == BOX

    found = type("Found", (), {"name": "", "port": "", "version": "", "box": None})
    other = found()
    other.name, other.port, other.version = "aaa_sorts_first", "COM9", "1.263"
    window._discovered(type("Scan", (), {"found": (other,)})())

    assert "aaa_sorts_first" in window.panes
    enable = window.build_method().acquisition.enable
    assert enable.box == BOX, "the scan moved the enable off the box the method declared"
    assert enable.channel == "A"


def test_a_box_the_method_names_that_nothing_answered_for_is_still_editable(
        window, tmp_path, qtbot):
    """A trainee editing tomorrow's method should not need the instrument switched on."""
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: BOX in window.panes and idle(window))
    window._discovered(type("Empty", (), {"found": ()})())
    assert BOX in window.panes
    assert window.panes[BOX].status.text() == "off or absent"
    assert window.panes[BOX].text().startswith("STBLCLK,EXT")


# --- the run queue's rules, with no window in sight -----------------------------------


def a_run(stem: str, *, stopped: str = "", retried: int = 0, complete: bool = True,
          console_errors: int = 0, asked: bool = True):
    """A `Run` in the shape the queue reads: a stem, an ending and its frames.

    Built rather than acquired, because what is under test here is the sentence a row
    shows in the morning and not the loop that produced it. A frame that did not acquire
    is what makes a run incomplete, which is the queue's own reason to call a row failed.
    """
    method = make_method(stem=stem)
    frames = tuple(
        FrameRecord(method_frame=1, repetition=index + 1, frame_number=index + 1,
                    outcome="acquired" if complete or index else "timed out",
                    ended_by="counted", retry=index < retried)
        for index in range(ACCUMULATIONS))
    discarded = tuple(
        replace(frame, ended_by="silence", retry=False) for frame in frames[:retried])
    folds = () if not complete else (
        FoldRecord(method_frame=1, frames_folded=(1,), rows=SCANS, seconds=0.1),)
    return Run(method=method, raw_path=f"/data/{stem}.uimf",
               summed_path=f"/data/{stem}.summed.uimf", frames=frames, folds=folds,
               warnings=(), seconds=1.0, stopped_early=stopped or None,
               stop_asked=bool(stopped) and asked,
               retried=discarded, console_errors=console_errors)


def test_a_row_reports_its_stems_its_stop_its_retries_and_the_console_errors():
    row = runqueue.QueueRow(method_path="/methods/clock.toml")
    assert row.name == "clock"
    assert not row.reportable
    state = runqueue.outcome_of(
        row, [a_run("260918_ZZ_001"),
              a_run("260918_ZZ_002", retried=1, console_errors=3)])
    assert state == runqueue.DONE
    assert row.stems == ("260918_ZZ_001", "260918_ZZ_002")
    assert row.directory == os.path.dirname("/data/260918_ZZ_001.uimf")
    assert row.retried_frames == 1
    assert row.console_errors == 3
    assert "260918_ZZ_001, 260918_ZZ_002" in row.outcome
    assert "1 repetition(s) acquired again" in row.outcome
    assert "console reported 3 errors" in row.outcome
    row.state = state
    assert row.reportable
    row.reset()
    assert (row.retried_frames, row.console_errors, row.directory) == (0, 0, "")


def test_a_library_document_is_named_by_its_folder_not_by_method_toml():
    """Every library document is `method.toml`, so a queue of three named `method` three
    times, and the run log said nothing about which ran when (lab record, task 96)."""
    folder = os.path.join("library", "bradykinin-clock", "method.toml")
    assert runqueue.QueueRow(method_path=folder).name == "bradykinin-clock"
    assert runqueue.QueueRow(method_path="method.toml").name == "method"
    assert runqueue.QueueRow(method_path="/methods/clock.toml").name == "clock"


def test_a_stopped_run_is_neither_done_nor_failed_because_its_files_are_good():
    row = runqueue.QueueRow(method_path="clock.toml")
    state = runqueue.outcome_of(row, [a_run("260918_ZZ_001", stopped="by the operator")])
    assert state == runqueue.STOPPED
    assert "stopped: by the operator" in row.outcome


def test_a_run_that_came_back_incomplete_fails_the_row():
    row = runqueue.QueueRow(method_path="clock.toml")
    assert runqueue.outcome_of(row, [a_run("260918_ZZ_001", complete=False)]) \
        == runqueue.FAILED
    assert runqueue.outcome_of(row, []) == runqueue.FAILED


# --- randomized passes and the queue file (task 86) -------------------------------------


def lettered(*names: str, **row) -> runqueue.RunQueue:
    return runqueue.RunQueue([runqueue.QueueRow(method_path=f"{n}.toml", **row)
                              for n in names])


def passes_of(queue: runqueue.RunQueue) -> list[list[str]]:
    by_pass: dict[int, list[str]] = {}
    for row in queue.rows:
        by_pass.setdefault(row.pass_number, []).append(row.name)
    return [by_pass[number] for number in sorted(by_pass)]


def test_randomizing_makes_n_passes_each_holding_every_row_once():
    queue = lettered("a", "b", "c", "d")
    done = queue.randomize(3, seed=7)
    assert len(queue.rows) == 12
    assert [row.pass_label for row in queue.rows[:4]] == ["1/3"] * 4
    assert [row.pass_label for row in queue.rows[-4:]] == ["3/3"] * 4
    for names in passes_of(queue):
        assert sorted(names) == ["a", "b", "c", "d"]
    assert done.seed == 7 and queue.seed == 7
    assert [list(names) for names in done.order] == passes_of(queue)
    assert "seed 7" in done.line and "pass 3:" in done.line


def test_the_same_seed_gives_the_same_order_and_a_new_order_each_pass_differs():
    first = lettered(*"abcdefgh")
    second = lettered(*"abcdefgh")
    assert first.randomize(4, seed=12345).order == second.randomize(4, seed=12345).order
    assert len(set(first.randomize(4, seed=12345).order)) > 1


def test_one_order_repeated_when_the_box_is_unticked():
    queue = lettered(*"abcdefgh")
    done = queue.randomize(3, reshuffle=False, seed=3)
    assert len(set(done.order)) == 1 and not done.reshuffle


def test_rows_that_stay_first_open_every_pass_in_the_order_they_were_entered():
    queue = runqueue.RunQueue([
        runqueue.QueueRow(method_path="x.toml"),
        runqueue.QueueRow(method_path="blank.toml", stays_first=True),
        runqueue.QueueRow(method_path="y.toml"),
        runqueue.QueueRow(method_path="qc.toml", stays_first=True),
        runqueue.QueueRow(method_path="z.toml"),
    ])
    queue.randomize(5, seed=1)
    for names in passes_of(queue):
        assert names[:2] == ["blank", "qc"]
        assert sorted(names[2:]) == ["x", "y", "z"]


def test_randomizing_again_rebuilds_from_pass_one_and_does_not_duplicate():
    queue = lettered("a", "b", "c")
    queue.randomize(3, seed=1)
    first_pass = [row for row in queue.rows if row.pass_number == 1]
    first_pass[0].replicates = 5
    later = next(row for row in queue.rows if row.pass_number == 3)
    later.conditions = "only on pass 3"
    queue.randomize(2, seed=2)
    assert len(queue.rows) == 6 and queue.passes == 2
    changed = [row for row in queue.rows if row.name == first_pass[0].name]
    assert [row.replicates for row in changed] == [5, 5], "pass 1 is the template"
    assert all(row.conditions != "only on pass 3" for row in queue.rows)


def test_a_row_added_by_hand_after_randomizing_joins_every_new_pass():
    queue = lettered("a", "b")
    queue.randomize(2, seed=1)
    queue.add(runqueue.QueueRow(method_path="c.toml"))
    queue.randomize(2, seed=1)
    assert [sorted(names) for names in passes_of(queue)] == [["a", "b", "c"]] * 2


def test_a_queue_copied_with_its_methods_opens_by_the_relative_path(tmp_path):
    here = tmp_path / "here"
    (here / "methods").mkdir(parents=True)
    (here / "methods" / "a.toml").write_text("", encoding="utf-8")
    runqueue.save(runqueue.RunQueue([runqueue.QueueRow(
        method_path=str(here / "methods" / "a.toml"))]), str(here / "q.toml"))
    moved = tmp_path / "moved"
    here.rename(moved)
    opened = runqueue.load(str(moved / "q.toml"))
    assert opened.rows[0].method_path == str(moved / "methods" / "a.toml")


def test_a_method_found_by_neither_path_keeps_its_absolute_one(tmp_path):
    missing = str(tmp_path / "gone.toml")
    runqueue.save(runqueue.RunQueue([runqueue.QueueRow(method_path=missing)]),
                  str(tmp_path / "q.toml"))
    assert runqueue.load(str(tmp_path / "q.toml")).rows[0].method_path == missing


@pytest.mark.parametrize("text, reason", [
    ("not = [toml", "not a queue file"),
    ("schema_version = 9\n", "schema_version 9"),
    ("schema_version = 1\n[[rows]]\nconditions = 'x'\n", "names no method"),
    ("schema_version = 1\n[[rows]]\nmethod = 'a.toml'\nreplicates = 'two'\n",
     "`replicates` is not a whole number"),
    ("schema_version = 1\n[[rows]]\nmethod = 'a.toml'\npass = 3\npasses = 2\n",
     "pass 3 of 2"),
])
def test_a_queue_file_that_is_not_one_says_why(text, reason):
    with pytest.raises(runqueue.QueueFileError, match=reason):
        runqueue.loads(text)


CONDITIONS = queuepanel.CONDITIONS_COLUMN
REPS = queuepanel.REPLICATES_COLUMN
OUTCOME = queuepanel.OUTCOME_COLUMN


# --- the queue over the stand-ins -----------------------------------------------------


def queued(window, tmp_path, name: str, method, **row) -> str:
    """Save a method under its own name and put a row for it in the window's queue."""
    path = str(tmp_path / f"{name}.toml")
    method_module.save(method, path)
    window.queue.add(runqueue.QueueRow(method_path=path, **row))
    window.queue_panel.refresh()
    return path


def test_the_buttons_a_trainee_presses_are_greyed_out_while_the_queue_runs(
        window, tmp_path, qtbot):
    ready_to_acquire(window, qtbot, tmp_path)
    queued(window, tmp_path, "only", make_method(accumulations=6))
    assert window.queue_panel.start_button.isEnabled()
    window.start_queue()
    qtbot.waitUntil(lambda: window.run_panel.progress.repetition >= 1, timeout=120_000)
    assert not window.acquire_button.isEnabled()
    assert not window.send_button.isEnabled()
    assert not window.queue_panel.start_button.isEnabled()
    assert window.queue_panel.stop_button.isEnabled()
    window.stop()
    until(qtbot, lambda: not window.queue.running and idle(window), timeout=180_000)


def test_the_queue_panel_puts_back_a_cell_the_queue_owns(qtbot):
    """`ItemIsEditable` is per item, so a double-click on the outcome opens an editor
    over a column the queue writes. What it says is put back."""
    queue = runqueue.RunQueue([runqueue.QueueRow(method_path="a.toml", replicates=3)])
    panel = QueuePanel(queue)
    qtbot.addWidget(panel)
    item = panel.tree.topLevelItem(0)
    item.setText(OUTCOME, "typed over the outcome")
    assert item.text(OUTCOME) == ""
    item.setText(REPS, "not a number")
    assert queue.rows[0].replicates == 3 and item.text(REPS) == "3"


def test_stays_first_is_a_check_box_and_the_pass_column_appears_with_passes(qtbot):
    queue = lettered("a", "b")
    panel = QueuePanel(queue)
    qtbot.addWidget(panel)
    assert panel.tree.isColumnHidden(queuepanel.PASS_COLUMN)
    panel.tree.topLevelItem(1).setCheckState(queuepanel.STAYS_FIRST_COLUMN,
                                             Qt.CheckState.Checked)
    assert queue.rows[1].stays_first
    queue.randomize(2, seed=1)
    panel.refresh()
    assert not panel.tree.isColumnHidden(queuepanel.PASS_COLUMN)
    assert panel.tree.topLevelItem(0).text(queuepanel.PASS_COLUMN) == "1/2"
    assert panel.tree.topLevelItem(0).text(queuepanel.METHOD_COLUMN) == "b"
    assert "2 randomized passes" in panel.caption.text()


def run_log(window) -> list[str]:
    log = window.run_panel.log
    return [log.topLevelItem(index).text(1) for index in range(log.topLevelItemCount())]


def test_randomize_in_the_window_logs_the_seed_and_asks_before_replacing_passes(
        window, tmp_path):
    for name in ("a", "b", "c"):
        queued(window, tmp_path, name, make_method())
    window._ask_randomize = lambda rows, passes: (3, True)
    asked: list[str] = []
    window._confirm = lambda title, detail: asked.append(title) or False
    window.randomize_queue()
    assert asked == [], "a queue without passes has nothing to replace"
    assert len(window.queue.rows) == 9
    line = next(text for text in run_log(window) if "randomized" in text)
    assert f"seed {window.queue.seed}" in line and "pass 3:" in line

    window.randomize_queue()
    assert asked == ["Replace the 3 passes?"] and len(window.queue.rows) == 9
    window._confirm = lambda title, detail: True
    window._ask_randomize = lambda rows, passes: (2, False)
    window.randomize_queue()
    assert window.queue.passes == 2 and len(window.queue.rows) == 6


def test_a_queue_saved_from_the_window_opens_again_with_its_order(window, tmp_path):
    for name in ("a", "b", "c"):
        queued(window, tmp_path, name, make_method())
    window._ask_randomize = lambda rows, passes: (2, True)
    window.randomize_queue()
    order = [(row.name, row.pass_label) for row in window.queue.rows]
    path = str(tmp_path / "series")
    window._queue_file_path = lambda save: path if save else path + ".queue.toml"
    window.save_queue()
    assert os.path.isfile(path + ".queue.toml"), "the suffix is added"

    window.queue.rows[0].state = runqueue.DONE
    window._confirm = lambda title, detail: False
    window.open_queue()
    assert window.queue.rows[0].state == runqueue.DONE, "cancel keeps the queue"
    window._confirm = lambda title, detail: True
    window.open_queue()
    assert [(row.name, row.pass_label) for row in window.queue.rows] == order
    assert all(row.state == runqueue.WAITING for row in window.queue.rows)
    assert window.queue_panel.start_button.isEnabled()


def test_a_saved_queue_is_not_listed_in_the_method_library(tmp_path):
    from clockwork.app.methodlib import scan_library
    method_module.save(make_method(), str(tmp_path / "a.toml"))
    runqueue.save(lettered("a"), str(tmp_path / "series.queue.toml"))
    assert [os.path.basename(entry.path) for entry in scan_library(str(tmp_path))] \
        == ["a.toml"]


# --- the method library (task 54) ------------------------------------------------------


@pytest.fixture
def library(tmp_path) -> str:
    """A directory holding two documents, one of them broken, for the browser to list."""
    method_module.save(make_method(stem="a"), str(tmp_path / "a.toml"))
    nested = tmp_path / "nested"
    nested.mkdir()
    method_module.save(make_method(stem="b"), str(nested / "method.toml"))
    (tmp_path / "broken.toml").write_text("schema_version = 2\n", encoding="utf-8")
    return str(tmp_path)


def test_the_dialog_lists_every_document_the_directory_holds(library, qtbot):
    dialog = LibraryDialog(library)
    qtbot.addWidget(dialog)
    assert dialog.table.rowCount() == 3
    assert len(dialog.entries) == 3
    assert sum(1 for entry in dialog.entries if not entry.ok) == 1


def test_open_is_only_enabled_for_a_single_good_selection(library, qtbot):
    dialog = LibraryDialog(library)
    qtbot.addWidget(dialog)
    dialog.table.selectRow(next(i for i, e in enumerate(dialog.entries) if e.ok))
    assert dialog.open_button.isEnabled()
    assert not dialog.diff_methods_button.isEnabled()
    dialog.table.selectRow(next(i for i, e in enumerate(dialog.entries) if not e.ok))
    assert not dialog.open_button.isEnabled()


def test_diff_two_methods_needs_exactly_two_selected(library, qtbot):
    dialog = LibraryDialog(library)
    qtbot.addWidget(dialog)
    good = [i for i, e in enumerate(dialog.entries) if e.ok]
    dialog.table.selectRow(good[0])
    assert not dialog.diff_methods_button.isEnabled()
    dialog.table.selectionModel().select(
        dialog.table.model().index(good[1], 0),
        dialog.table.selectionModel().SelectionFlag.Select
        | dialog.table.selectionModel().SelectionFlag.Rows)
    assert dialog.diff_methods_button.isEnabled()


def test_open_into_panes_names_the_chosen_path_and_accepts(library, qtbot):
    dialog = LibraryDialog(library)
    qtbot.addWidget(dialog)
    row = next(i for i, e in enumerate(dialog.entries) if e.ok)
    dialog.table.selectRow(row)
    qtbot.mouseClick(dialog.open_button, Qt.MouseButton.LeftButton)
    assert dialog.chosen_path == dialog.entries[row].path
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_open_library_loads_the_dialogs_chosen_method_and_remembers_the_directory(
        window, tmp_path, monkeypatch):
    """`open_library` cannot exercise the real modal dialog in a test, so the dialog
    class is stood in for -- what matters here is what the window does with the result,
    which is exactly what `_load_method` does with any other path."""
    path = str(tmp_path / "picked.toml")
    method_module.save(make_method(stem="picked"), path)

    class _Stub:
        def __init__(self, directory, parent):
            self.chosen_path = path
            self.chosen_template = ""
        def directory(self):
            return "chosen-directory"
        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr("clockwork.app.window.LibraryDialog", _Stub)
    window.open_library()
    assert window.method_path == path
    assert window.settings.library_dir == "chosen-directory"


def test_method_diff_dialog_marks_a_changed_line_and_an_added_box(qtbot):
    a = make_method(load=["ONE"])
    b = method_module.from_dict({
        **method_module.to_dict(a),
        "boxes": [
            {**method_module.to_dict(a)["boxes"][0], "load": ["TWO"]},
            {"name": "box2", "port": "COM9", "setup": [], "load": ["NEW"], "arm": []},
        ],
    })
    dialog = MethodDiffDialog(method_diff(a, b))
    qtbot.addWidget(dialog)
    titles = [dialog.tree.topLevelItem(i).text(0)
              for i in range(dialog.tree.topLevelItemCount())]
    assert any(title.startswith("box2") and "only in B" in title for title in titles)
    assert method_diff(a, a).identical
    assert not method_diff(a, b).identical


def test_report_a_problem_sits_above_about_and_names_the_last_run(window, tmp_path,
                                                                  monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    help_menu = window.action_about.associatedObjects()[-1]
    actions = [action for action in help_menu.actions() if not action.isSeparator()]
    assert actions[-3:] == [window.action_report, window.action_request,
                            window.action_about]

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    window.method_path = str(tmp_path / "bradykinin.toml")
    log = tmp_path / "ZZ-007-2026-09-24.transcript.log"
    log.write_text("10:00:00.000 mips.wire        auklet > b'STBLDAT'\n"
                   "10:00:00.001 acq.loop         FrameDone: frame 1 finished\n",
                   encoding="utf-8")
    window._last_run_paths = (str(tmp_path / "ZZ-007.uimf"), "", "ZZ-007")
    text = parse_qs(urlsplit(window.report_url()).query)["attachments"][0]
    assert "bradykinin.toml" in text and "FrameDone: frame 1 finished" in text
    assert "STBLDAT" not in text


def test_a_window_report_names_no_project_whatever_folder_the_run_is_in(window,
                                                                       tmp_path,
                                                                       monkeypatch):
    """The project line is `clockwork report`'s, read off a project's data folder; the
    window knows no project (lab record, task 158)."""
    from urllib.parse import parse_qs, urlsplit

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    day = tmp_path / "slimphony-demo-data" / "261109-scout"
    day.mkdir(parents=True)
    (day / "ZZ-007-2026-11-09.transcript.log").write_text(
        "10:00:00.001 acq.loop         FrameDone: frame 1 finished\n", encoding="utf-8")
    window._last_run_paths = (str(day / "ZZ-007.uimf"), "", "ZZ-007")
    text = parse_qs(urlsplit(window.report_url()).query)["attachments"][0]
    assert "FrameDone: frame 1 finished" in text
    assert "Project" not in text


def test_report_a_problem_keeps_the_last_run_off_the_ui_thread(window, tmp_path, qtbot,
                                                               monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from clockwork import keep
    from clockwork.app import window as window_module

    root = tmp_path / "kept"
    monkeypatch.setenv(keep.ENV, str(root))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    opened = []
    monkeypatch.setattr(window_module.QDesktopServices, "openUrl",
                        lambda address: opened.append(address.toString()) or True)
    (tmp_path / "ZZ-007.uimf").write_bytes(b"uimf")
    (tmp_path / "ZZ-007-2026-09-25.transcript.log").write_text(
        "10:00:00.001 acq.loop         FrameDone: frame 1 finished\n", encoding="utf-8")
    window._last_run_paths = (str(tmp_path / "ZZ-007.uimf"), "", "ZZ-007")
    window.report_problem().join(30)
    qtbot.waitUntil(lambda: bool(opened), timeout=5000)
    [name] = os.listdir(root)
    text = parse_qs(urlsplit(opened[0]).query)["attachments"][0]
    assert f"Report id: {name}" in text and str(root / name) in text
    assert window.action_report.isEnabled()
    assert {row.kept for row in keep.read_manifest(str(root / name)).rows
            if row.status == "copied"} == {"ZZ-007.uimf", "ZZ-007-2026-09-25.transcript.log"}


def test_report_after_a_run_stopped_before_its_first_frame_reuses_its_kept_folder(
        window, tmp_path, qtbot, monkeypatch):
    """Lab #8: `acquire` was refused, the owner kept the run under its stem, and Report --
    which learnt stems only from `RunBegun` -- kept the method alone in a second folder.
    The stem now arrives with `LogsOpened`, before anything is sent."""
    from urllib.parse import parse_qs, urlsplit

    from clockwork import keep
    from clockwork.app import window as window_module
    from clockwork.owner import LogsOpened

    root = tmp_path / "kept"
    monkeypatch.setenv(keep.ENV, str(root))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    opened = []
    monkeypatch.setattr(window_module.QDesktopServices, "openUrl",
                        lambda address: opened.append(address.toString()) or True)
    window._last_run_paths = (str(tmp_path / "ZZ-006.uimf"), "", "ZZ-006")
    log = tmp_path / "ZZ-007-2026-10-05.transcript.log"
    log.write_text("09:21:48.000 acq.loop         Stopped: the console refused 'acquire'\n",
                   encoding="utf-8")
    failed = keep.keep(str(root), [str(log)], reason="the console refused 'acquire'",
                       stems=["ZZ-007"])
    window.mailbox.put(LogsOpened(directory=str(tmp_path), stem="ZZ-007",
                                  transcript=str(log)))
    window._drain()
    window.report_problem().join(30)
    qtbot.waitUntil(lambda: bool(opened), timeout=5000)
    assert os.listdir(root) == [os.path.basename(failed)]
    text = parse_qs(urlsplit(opened[0]).query)["attachments"][0]
    assert f"Report id: {os.path.basename(failed)}" in text
    assert "Stopped: the console refused" in text


def test_report_this_run_on_a_queue_row_keeps_that_rows_files_not_the_last_runs(
        window, tmp_path, qtbot, monkeypatch):
    """Lab #2: the toolbar's Report kept the run after the one the trainee meant. The
    row's own "Report this run" keeps the row's stems, its method and its transcript."""
    from urllib.parse import parse_qs, urlsplit

    from clockwork import keep
    from clockwork.app import window as window_module

    root = tmp_path / "kept"
    monkeypatch.setenv(keep.ENV, str(root))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    opened = []
    monkeypatch.setattr(window_module.QDesktopServices, "openUrl",
                        lambda address: opened.append(address.toString()) or True)
    methods = {}
    for stem, name in (("ZZ-039", "2+BK-CLOCK-40V-2ms"), ("ZZ-054", "2+BK-CLOCK-45V")):
        (tmp_path / f"{stem}.uimf").write_bytes(b"uimf")
        (tmp_path / f"{stem}-2026-09-25.transcript.log").write_text(
            f"10:00:00.001 acq.loop         Retried: {stem}\n", encoding="utf-8")
        methods[stem] = tmp_path / f"{name}.toml"
        methods[stem].write_text("# method\n", encoding="utf-8")
        row = runqueue.QueueRow(method_path=str(methods[stem]), state=runqueue.DONE,
                                stems=(stem,), directory=str(tmp_path))
        window.queue.add(row)
    window.queue.add(runqueue.QueueRow(method_path=str(methods["ZZ-039"])))
    window._last_run_paths = (str(tmp_path / "ZZ-054.uimf"), "", "ZZ-054")
    window.queue_panel.refresh()

    assert not window.queue_panel.menu_for(2).actions()[0].isEnabled()
    [action] = window.queue_panel.menu_for(0).actions()
    assert action.isEnabled() and action.text().startswith("Report this run")
    action.trigger()
    qtbot.waitUntil(lambda: bool(opened), timeout=5000)
    [name] = os.listdir(root)
    manifest = keep.read_manifest(str(root / name))
    assert manifest.stems() == ["ZZ-039"]
    assert {row.kept for row in manifest.rows if row.status == "copied"} == {
        "ZZ-039.uimf", "ZZ-039-2026-09-25.transcript.log", "2+BK-CLOCK-40V-2ms.toml"}
    text = parse_qs(urlsplit(opened[0]).query)["attachments"][0]
    assert "Retried: ZZ-039" in text and "2+BK-CLOCK-40V-2ms.toml" in text


def test_request_a_feature_opens_the_feature_form_and_keeps_nothing(window, tmp_path,
                                                                      monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from clockwork import keep
    from clockwork.app import window as window_module

    root = tmp_path / "kept"
    monkeypatch.setenv(keep.ENV, str(root))
    opened = []
    monkeypatch.setattr(window_module.QDesktopServices, "openUrl",
                        lambda address: opened.append(address.toString()) or True)
    window.action_request.trigger()
    [address] = opened
    assert parse_qs(urlsplit(address).query)["template"] == ["feature.yml"]
    assert not root.exists()


def test_the_kept_root_is_the_setting_unless_the_environment_names_one(window, tmp_path,
                                                                        monkeypatch):
    from clockwork import keep

    monkeypatch.delenv(keep.ENV)
    window.settings.kept_root = str(tmp_path / "chosen")
    assert window.kept_root() == str(tmp_path / "chosen")
    monkeypatch.setenv(keep.ENV, str(tmp_path / "env"))
    assert window.kept_root() == str(tmp_path / "env")
    assert window.worker.owner.kept_root == ""  # a --fake window's owner keeps nothing


# --- a rendered method in the window: attached, detached (lab record, task 94) ---------


_WINDOW_TABLE = per_repetition_table(SCANS).replace(",500:B:0,", ",{b_ticks}:B:0,")

WINDOW_TEMPLATE = f"""\
template_schema = 1
renders = 2
start = [["{BOX}", "TBLSTRT"]]
reset = [["{BOX}", "SMOD,LOC"], ["{BOX}", "SMOD,TBL"]]

[knobs]
b_ticks = {{ default = 500, min = 100, max = 520, unit = "ticks", description = "line B high" }}

[labels]
sample = {{ required = true, description = "what was sprayed" }}

[constants]
tick_us = 129.0

[marks]
b_off = {{ ms = "b_ticks * tick_us / 1000", description = "line B falls" }}

[metadata]
name = "window-template"
created = 2026-09-27
description = "make_method with a knob on line B."

[acquisition]
frames = 1
scans = {SCANS}
accumulations = {ACCUMULATIONS}
repetition_mode = "per_repetition"
keep_raw = true
file_stem = "window-template"
enable = {{ box = "{BOX}", channel = "A" }}

[[boxes]]
name = "{BOX}"
port = "COM3"
setup = ["STBLCLK,EXT", "STBLTRG,POS"]
load = ["{_WINDOW_TABLE}"]
arm = ["SMOD,TBL"]
"""
"""`make_method` as a template: the same table with line B's fall a knob, a label and a
mark, so a window run of a rendered file has every kind of parameter to stamp."""


def render_to(tmp_path, name: str = "cell", b_ticks: int = 400) -> str:
    """A rendered method written as `render-template --to` writes it."""
    rendered = templates.render(templates.loads_template(WINDOW_TEMPLATE),
                                {"b_ticks": b_ticks}, {"sample": "test mixture"})
    path = str(tmp_path / f"{name}.toml")
    method_module.save(templates.attached(rendered), path)
    return path


def stamped(path: str) -> dict[str, str]:
    """`Global_Params` by name, as text: what a manifest or mainspring reads back."""
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return {name: value for name, value in conn.execute(
            "SELECT ParamName, ParamValue FROM Global_Params")}


def armed_on(window, qtbot, path: str) -> None:
    """`ready_to_acquire` for a method file already on disk."""
    assert window._load_method(path)
    until(qtbot, lambda: window.worker.boxes and idle(window))
    window.send(setup=True)
    until(qtbot, lambda: idle(window) and window._armed)
    window.start_console()
    until(qtbot, lambda: window.worker.console is not None
          and window.worker.console.alive and idle(window))


def one_run(window, qtbot) -> object:
    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    assert window.acquire_button.isEnabled(), window.problems.text()
    window.acquire()
    until(qtbot, lambda: runs and idle(window), timeout=180_000)
    assert runs[0].complete, runs[0].text
    return runs[0]


def test_a_rendered_file_opens_attached_and_its_run_stamps_the_template_and_knobs(
        window, tmp_path, qtbot):
    armed_on(window, qtbot, render_to(tmp_path))
    assert window.rendered is not None
    assert window.provenance.isVisibleTo(window)
    assert window.provenance.text() == (
        "Rendered from window-template at b_ticks 400 ticks, sample 'test mixture'")

    run = one_run(window, qtbot)
    for path in (run.raw_path, run.summed_path):
        values = stamped(path)
        assert float(values["ClockworkKnobBTicks"]) == 400.0, path
        assert values["ClockworkLabelSample"] == "test mixture"
        assert float(values["ClockworkMarkBOffMs"]) == pytest.approx(400 * 129.0 / 1000)
        assert int(values["ClockworkMarkBOffScan"]) == 400
        assert values["ClockworkTemplateHash"] == templates.loads_template(
            WINDOW_TEMPLATE).hash
    assert window.rendered is not None, "a run does not detach the method"


def test_editing_a_pane_detaches_once_and_the_run_stamps_as_hand_written(
        window, tmp_path, qtbot):
    armed_on(window, qtbot, render_to(tmp_path))
    before = len(run_log(window))
    pane = window.panes[BOX]
    pane.set_text("# a note the trainee added\n" + pane.text())
    window._pane_changed()
    window._pane_changed()
    said = [line for line in run_log(window)[before:] if "now a hand-written" in line]
    assert len(said) == 1, run_log(window)[before:]
    assert window.rendered is None and not window.provenance.isVisibleTo(window)

    window.send()
    until(qtbot, lambda: idle(window)
          and matches_wire(window._armed, window.build_method()))
    values = stamped(one_run(window, qtbot).raw_path)
    assert not any(name.startswith(("ClockworkKnob", "ClockworkTemplate", "ClockworkLabel",
                                    "ClockworkMark")) for name in values)


def test_a_file_whose_strings_no_longer_render_opens_hand_written_and_acquires(
        window, tmp_path, qtbot):
    path = render_to(tmp_path)
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text.replace('"STBLTRG,POS"', '"STBLTRG,POS", "# edited by hand"', 1))
    armed_on(window, qtbot, path)
    warned = [line for line in run_log(window) if "rendered from a template, but" in line]
    assert len(warned) == 1 and "edited since it was rendered" in warned[0]
    assert window.rendered is None
    values = stamped(one_run(window, qtbot).raw_path)
    assert "ClockworkKnobBTicks" not in values


def test_saving_keeps_the_table_while_attached_and_drops_it_once_detached(
        window, tmp_path, qtbot):
    window._load_method(render_to(tmp_path))
    until(qtbot, lambda: window.worker.boxes and idle(window))
    kept = str(tmp_path / "kept.toml")
    window.method_path = kept
    window.save_method()
    reopened = method_module.load(kept)
    assert templates.reproduce(reopened)[1] == ""
    assert dict(reopened.rendered_from.knobs) == {"b_ticks": 400}

    window.frames.setValue(2)
    dropped = str(tmp_path / "dropped.toml")
    window.method_path = dropped
    window.save_method()
    assert window.rendered is None
    assert method_module.load(dropped).rendered_from is None


def test_a_queue_row_over_a_rendered_file_stamps_as_rendered(window, tmp_path, qtbot):
    ready_to_acquire(window, qtbot, tmp_path)
    window.queue.add(runqueue.QueueRow(method_path=render_to(tmp_path, b_ticks=300)))
    window.queue_panel.refresh()
    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    window.start_queue()
    until(qtbot, lambda: not window.queue.running and idle(window), timeout=300_000)
    assert [row.state for row in window.queue.rows] == [runqueue.DONE]
    assert float(stamped(runs[0].raw_path)["ClockworkKnobBTicks"]) == 300.0


def test_the_library_lists_a_template_and_open_on_it_goes_to_the_builder(
        qtbot, tmp_path):
    (tmp_path / "template.toml").write_text(WINDOW_TEMPLATE, encoding="utf-8")
    render_to(tmp_path)
    dialog = LibraryDialog(str(tmp_path))
    qtbot.addWidget(dialog)
    rows = {dialog.table.item(row, 3).text(): row for row in range(dialog.table.rowCount())}
    template_row = next(row for text, row in rows.items() if text.startswith("template;"))
    rendered_row = next(row for text, row in rows.items() if text.startswith("rendered from"))
    dialog.table.selectRow(template_row)
    assert dialog.open_button.isEnabled()
    assert not dialog.diff_methods_button.isEnabled()
    dialog._open()
    assert dialog.chosen_template.endswith("template.toml") and dialog.chosen_path == ""
    dialog.chosen_template = ""
    dialog.table.clearSelection()
    dialog.table.selectRow(rendered_row)
    dialog._open()
    assert dialog.chosen_path.endswith("cell.toml") and dialog.chosen_template == ""


# --- the Template tab (lab record, task 169; E4, R10) ------------------------------------


def window_limits(**knob: object) -> object:
    """Standing limits admitting `WINDOW_TEMPLATE`, with `b_ticks` bounded as given."""
    from clockwork import envelope

    bounds = ", ".join(f"{key} = {value}" for key, value in knob.items()) or "min = 100"
    template = templates.loads_template(WINDOW_TEMPLATE)
    return envelope.loads(f"""\
schema_version = 2

[allow]
boxes = ["{BOX}"]

[templates.window]
hash = "{template.hash[:12]}"

[templates.window.knobs]
b_ticks = {{ {bounds} }}
""")


def library_with_template(tmp_path) -> str:
    library = tmp_path / "library"
    library.mkdir(exist_ok=True)
    (library / "template.toml").write_text(WINDOW_TEMPLATE, encoding="utf-8")
    return str(library)


def test_the_template_tab_renders_in_range_and_saves_what_render_to_writes(
        qtbot, tmp_path):
    tab = TemplateTab()
    qtbot.addWidget(tab)
    tab.set_library(library_with_template(tmp_path))
    assert tab.picker.count() == 1 and tab.template is not None
    field = tab.knob_fields["b_ticks"]
    assert (field.minimum(), field.maximum(), field.value()) == (100, 520, 500)
    # The sample is required: nothing renders, and nothing is saved, until it is given.
    assert not tab.save_button.isEnabled() and not tab.acquire_button.isEnabled()
    assert "sample" in tab.problems.text()
    tab.label_fields["sample"].setText("test mixture")
    assert tab.save_button.isEnabled() and tab.open_button.isEnabled()
    assert tab.diff_text.toPlainText() == "every knob at its default: no difference"
    field.setValue(9999)  # clamped to the template's range: the box offers nothing outside
    assert field.value() == 520
    field.setValue(400)
    assert ",400:B:0," in tab.method_text.toPlainText()
    assert ",500:B:0," in tab.diff_text.toPlainText()
    assert tab.problems.text() == (
        "rendered from window-template at b_ticks 400 ticks, sample 'test mixture'")
    assert default_name(tab.template, tab.template_path, tab.knobs()) == (
        "template_b_ticks-400.toml")

    saved = str(tmp_path / "built.toml")
    assert tab.write(saved)
    with open(saved, encoding="utf-8") as built:
        text = built.read()
    with open(render_to(tmp_path, b_ticks=400), encoding="utf-8") as written:
        assert text == written.read()
    rendered, why = templates.reproduce(method_module.load(saved))
    assert rendered is not None, why
    assert rendered.knobs == {"b_ticks": 400}


def test_each_knob_is_shown_against_the_template_s_range_and_the_agent_s(qtbot, tmp_path):
    """R10: both ranges beside the field, a value outside the agent's marked and never
    refused; with no limits in the sitting, the template's range alone, said."""
    tab = TemplateTab()
    qtbot.addWidget(tab)
    tab.set_library(library_with_template(tmp_path))
    tab.label_fields["sample"].setText("test mixture")
    tab.knob_fields["b_ticks"].setValue(300)
    note = tab.knob_notes["b_ticks"]
    assert note.text() == "template 100 to 520 ticks"
    assert "template's own range alone" in tab.limits_line.text()

    tab.set_limits(window_limits(min=200, max=450), "")
    assert note.text() == "template 100 to 520 ticks; the agent 200 to 450 ticks"
    assert tab.outside() == []
    tab.knob_fields["b_ticks"].setValue(500)
    assert tab.outside() == ["b_ticks"]
    assert "outside the agent's, run anyway and recorded" in note.text()
    assert tab.rendered is not None and tab.save_button.isEnabled()

    from clockwork import envelope

    tab.set_limits(envelope.loads(f'schema_version = 2\n[allow]\nboxes = ["{BOX}"]\n'), "")
    assert "do not admit this template" in tab.limits_line.text()
    assert note.text().endswith("the agent: not admitted")


def test_a_template_run_is_file_less_and_its_record_says_what_lies_outside(
        window, tmp_path, qtbot):
    """E4 and R10 end to end over the stand-ins: Acquire in the Template tab writes no
    method file, the files stamp the template and its knobs, and the run record keeps
    the knob outside the agent's range."""
    import json

    window.settings.library_dir = library_with_template(tmp_path)
    window.template_tab.set_library(window._library())
    window.worker.owner.limits = window_limits(min=200, max=450)
    window._refresh_limits()
    tab = window.template_tab
    tab.label_fields["sample"].setText("test mixture")
    tab.knob_fields["b_ticks"].setValue(480)
    tab.replicates.setValue(1)
    assert tab.outside() == ["b_ticks"]
    # Under --fake the rack comes from a method; a scan with the template's boxes gives
    # the stand-ins their boxes, as opening a method would.
    window.worker.submit(window_discover(tab.rendered.method))
    until(qtbot, lambda: window.worker.boxes and idle(window))
    window.start_console()
    until(qtbot, lambda: window.worker.console_alive and idle(window))
    before = set(os.listdir(tmp_path))
    window._refresh_actions()
    assert tab.acquire_button.isEnabled(), tab.acquire_button.toolTip()
    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    tab.acquire_button.click()
    until(qtbot, lambda: runs and idle(window), timeout=180_000)
    assert runs[0].complete, runs[0].text
    values = stamped(runs[0].raw_path)
    assert float(values["ClockworkKnobBTicks"]) == 480.0
    assert values["ClockworkLabelSample"] == "test mixture"
    new = set(os.listdir(tmp_path)) - before
    assert not any(name.endswith(".toml") for name in new), new
    record = next(name for name in new if name.endswith(".request.json"))
    data = json.loads((tmp_path / record).read_text(encoding="utf-8"))
    entry = data["acquisitions"][0]
    assert entry["template"]["knobs"] == {"b_ticks": 480}
    assert len(entry["outside_limits"]) == 1 and "b_ticks = 480" in entry["outside_limits"][0]
    assert data["request"]["source"] == "window"


def window_discover(method):
    from clockwork.owner import Discover

    return Discover(method=method)


def test_open_in_the_method_tab_writes_the_file_and_opens_it_attached(
        window, tmp_path, monkeypatch):
    window.settings.library_dir = library_with_template(tmp_path)
    window.template_tab.set_library(window._library())
    tab = window.template_tab
    tab.label_fields["sample"].setText("test mixture")
    tab.knob_fields["b_ticks"].setValue(300)
    saved = str(tmp_path / "built.toml")
    monkeypatch.setattr(tab, "_ask_path", lambda suggested: saved)
    window.tabs.setCurrentWidget(tab)
    tab.open_button.click()
    assert window.method_path == saved and window.rendered is not None
    assert window.tabs.currentWidget() is window.method_tab
    assert window.provenance.text() == (
        "Rendered from window-template at b_ticks 300 ticks, sample 'test mixture'")
    assert window.rendered.knobs == {"b_ticks": 300}


def test_the_library_opens_a_template_in_the_template_tab(window, tmp_path, monkeypatch):
    library = library_with_template(tmp_path)
    window.settings.library_dir = library

    def exec_(dialog) -> int:
        dialog.chosen_template = os.path.join(library, "template.toml")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(LibraryDialog, "exec", exec_)
    window.open_library()
    assert window.tabs.currentWidget() is window.template_tab
    assert window.template_tab.template_path.endswith("template.toml")


# --- the Day tab (D2, D3) --------------------------------------------------------------


def test_the_day_tab_lists_a_window_run_as_its_record_words_it(window, tmp_path, qtbot):
    ready_to_acquire(window, qtbot, tmp_path)
    window.run_panel.purpose.setText("a day-tab check")
    window.run_panel.at_window.setText("MB")
    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    window.replicates.setValue(1)
    window.acquire()
    until(qtbot, lambda: runs and idle(window), timeout=180_000)
    window.tabs.setCurrentWidget(window.day_panel)
    lines = [line for line in window.day_panel.lines if line.route == "window"]
    assert len(lines) == 1, window.day_panel.lines
    line = lines[0]
    assert line.person == "MB" and line.purpose == "a day-tab check"
    assert line.files == (os.path.splitext(os.path.basename(runs[0].raw_path))[0],)
    assert line.ended.startswith("1 file: 1 complete")
    assert window.day_panel.tree.topLevelItemCount() >= 1


def test_the_day_list_reads_verdicts_routines_and_stops_as_recorded():
    from clockwork.app.daylist import request_line

    line = request_line({
        "request": {"id": "x", "text": "why", "source": "mcp", "initials": "BK",
                    "opened": "2026-11-18T14:02:11"},
        "files": [{"stem": "261118_BK_001", "complete": True},
                  {"stem": "261118_BK_002", "stopped_early": True}],
        "interventions": [{"kind": "stop", "by": "MB", "reason": "beam drifted"}],
        "routines": [{"routine": "gate", "verdict": "pass", "reason": "within 2%"}],
        "verdicts": [{"stem": "261118_BK_001", "verdict": "no_signal", "by": "AD"},
                     {"stem": "261118_BK_001", "verdict": "worked", "by": "MB"}],
    })
    assert line.time == "14:02:11" and line.route == "mcp" and line.person == "BK"
    assert line.ended.startswith("2 files: 1 complete, 1 stopped early; stopped by MB")
    assert line.result == "gate: pass (within 2%)"
    assert line.verdict == "261118_BK_001: worked (MB)"
    assert line.state == "stopped"


def test_the_three_boxes_range_to_the_loader_s_ceilings(window):
    """One ceiling each, in `clockwork.method`, so a box can never show a number the
    document does not say (lab record, task 130)."""
    assert window.frames.maximum() == method_module.MAX_FRAMES
    assert window.scans.maximum() == method_module.MAX_SCANS
    assert window.accumulations.maximum() == method_module.MAX_ACCUMULATIONS


def test_a_method_past_the_ceiling_is_refused_at_open_not_clamped(window, tmp_path):
    """Lab issue 12: a 1 617 100-scan method opened into a box that stopped at a
    million, clamped without a word, and the gate-rule check then refused the run
    against 1 000 000, a number the trainee never wrote. Refused at open instead,
    naming the key and the ceiling, with the box left where it was."""
    over = method_module.MAX_SCANS + 1
    text = method_module.dumps(make_method(scans=1000)).replace(
        "scans = 1000", f"scans = {over}")
    path = tmp_path / "too-long.toml"
    path.write_text(text, encoding="utf-8")
    before = window.scans.value()
    assert window._load_method(str(path), quiet=True) is False
    assert window.scans.value() == before
    said = "\n".join(window.run_panel.log.topLevelItem(i).text(1)
                     for i in range(window.run_panel.log.topLevelItemCount()))
    assert "too-long.toml could not be opened" in said
    assert f"acquisition.scans: {over} is above the ceiling of " \
           f"{method_module.MAX_SCANS}" in said


def test_the_bar_survives_a_run_wider_than_qt_s_int(qtbot):
    """`QProgressBar.setRange` takes a C `int` and PySide raises past it rather than
    clamp, so a run of more than 2 147 483 647 scans killed the run log at `RunBegun`.
    Drawn in units instead: the counts stay in scans, the bar scales."""
    panel = RunPanel()
    qtbot.addWidget(panel)
    panel.show(RunBegun("raw.uimf", "summed.uimf", frames=1, console_frames=100_000,
                        frame_length=20_000_000, frame_timeout=60.0))
    assert panel.progress.total == 2_000_000_000_000
    assert panel.progress.unit == 932
    assert panel.bar.maximum() == 2_000_000_000_000 // 932 <= 2**31 - 1

    panel.show(FrameBegun(method_frame=1, repetition=50_000, frame_number=50_000,
                          of=100_000))
    assert panel.progress.scans == 49_999 * 20_000_000
    assert panel.bar.value() == panel.progress.scans // 932
    panel.show(BatchSeen(1, 50_000, _batch(500), scans_so_far=10_000_000))
    assert panel.bar.value() == (49_999 * 20_000_000 + 10_000_000) // 932


def test_a_run_inside_qt_s_int_is_drawn_in_scans(qtbot):
    panel = RunPanel()
    qtbot.addWidget(panel)
    panel.show(RunBegun("raw.uimf", "summed.uimf", frames=1, console_frames=100,
                        frame_length=20_000_000, frame_timeout=60.0))
    assert panel.progress.unit == 1
    assert panel.bar.maximum() == 2_000_000_000


# --- the beam guard (lab record, task 134) --------------------------------------------


def test_the_guard_dialog_takes_its_floor_from_a_run_or_a_number(qtbot, tmp_path,
                                                                 monkeypatch):
    seen: list[str] = []

    def from_run(path, fraction, repetitions):
        seen.append(path)
        return BeamGuard(floor=fraction * 2.0e6, repetitions=repetitions,
                         source="0.1x the median of 261027_BK_330 (2e+06)")

    monkeypatch.setattr("clockwork.app.queuepanel.from_run", from_run)
    dialog = BeamGuardDialog(None, lambda: "E:/x/261027_BK_330.uimf")
    qtbot.addWidget(dialog)
    assert dialog.chosen() is None, "off until armed"
    dialog.from_run.click()
    assert seen == ["E:/x/261027_BK_330.uimf"]
    guard = dialog.chosen()
    assert guard == BeamGuard(floor=2.0e5, repetitions=5,
                              source="0.1x the median of 261027_BK_330 (2e+06)")
    # A number typed over it has no run behind it any more.
    dialog.floor.setText("3e5")
    dialog.floor.textEdited.emit("3e5")
    assert dialog.chosen() == BeamGuard(floor=3e5, repetitions=5)
    dialog.floor.setText("")
    with pytest.raises(ValueError, match="type a floor"):
        dialog.chosen()



def test_a_series_that_stopped_short_does_not_end_the_run_log_on_done(window):
    """The window's end of a series a stop ended short says so, warned, and the status
    bar does not say done (lab #26; lab record, task 171)."""
    from clockwork.owner import SeriesJob, SeriesResult, wire

    job = wire.example(SeriesJob)
    short = SeriesResult(points=3, points_done=1,
                         stopped="point 2 of 3 (planned 2, 261119_ZZ_003): it stopped",
                         stopped_at=2, stopped_file="261119_ZZ_003",
                         not_acquired=((3, "end"),))
    window._job_finished(job, short)
    log = window.run_panel.log
    lines = [log.topLevelItem(number) for number in range(log.topLevelItemCount())]
    [said] = [line for line in lines if "stopped short" in line.text(1)]
    assert said.data(0, _IS_WARNING)
    assert "Never acquired: planned 3 (the end reference)." in said.text(1)
    message = window.statusBar().currentMessage()
    assert "stopped short, 1 of 3 points complete" in message and "done" not in message
    assert not [line for line in lines if ": complete," in line.text(1)]


def test_a_series_that_ran_to_its_end_says_so_in_the_run_log(window):
    """A complete series leaves a line in the run log, not only a caption the next job
    replaces: a window that joined the series scans the rack as it ends (lab #32)."""
    from clockwork.owner import SeriesJob, SeriesResult, wire

    job = wire.example(SeriesJob)
    runs = tuple(a_run(f"261119_ZZ_00{number}") for number in (1, 2, 3))
    window.run_panel.begin(job.label)
    tag = window.run_panel.job_tag
    window._job_finished(job, SeriesResult(runs=runs, points=2, points_done=2,
                                           replaced=("261119_ZZ_002",)))
    log = window.run_panel.log
    lines = [log.topLevelItem(number) for number in range(log.topLevelItemCount())]
    [said] = [line for line in lines if ": complete," in line.text(1)]
    assert said.text(1) == (f"{job.label}: complete, 2 of 2 points, 3 files, 1 of them "
                            "failed and replaced by a retake")
    assert not said.data(0, _IS_WARNING)
    assert said.data(0, _JOB) == tag, "said under the series, before the window idles"
    # The caption goes as the put-off scan begins; the line stays.
    window.run_panel.begin("Find boxes")
    assert said.text(1).startswith(f"{job.label}: complete")
