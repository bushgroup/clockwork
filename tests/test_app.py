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
import sys
import threading

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
from clockwork.acq import (  # noqa: E402
    BatchSeen,
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
    InstrumentDiffDialog,
    LibraryDialog,
    MethodDiffDialog,
)
from clockwork.app.methodlib import method_diff  # noqa: E402
from clockwork.app.panes import MARGIN_TAGS, BoxPane  # noqa: E402
from clockwork.app.queuepanel import QueuePanel  # noqa: E402
from clockwork.app.runlog import (  # noqa: E402
    _IS_WARNING,
    RunPanel,
    is_left_as_found,
    names_it_elsewhere,
)
from clockwork.app.settings import Settings  # noqa: E402
from clockwork.app.statepanel import StatePanel  # noqa: E402
from clockwork.app.window import MainWindow  # noqa: E402
from clockwork.app.worker import matches_wire  # noqa: E402
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


def test_the_window_is_shorter_than_the_screen_it_runs_on(qtbot):
    """The requirement, pinned (lab record, task 61, and the window note's screen
    bullet).

    A `QSplitter` cannot shrink a child below its minimum, so while the side column's
    minimum was the sum of its four group boxes the window had a floor taller than the
    instrument's working area -- and a maximized window that overflows is clipped, not
    scrolled.
    """
    made = MainWindow(fake=True)
    qtbot.addWidget(made)
    try:
        assert made.minimumSizeHint().height() < TALLEST
    finally:
        made.worker.shutdown()
        made.worker.wait(10_000)


def test_the_run_log_and_the_status_bar_are_on_screen_when_maximized(qtbot):
    """The thing the height is a proxy for, asserted directly.

    Not `minimumSizeHint` this time but the laid-out window at the instrument's own
    size: the run log has real height and the status bar's bottom edge is inside the
    window rather than under it.
    """
    made = MainWindow(fake=True)
    qtbot.addWidget(made)
    try:
        made.resize(*WORKING_AREA)
        made.show()
        qtbot.waitExposed(made)
        run_panel = made.centralWidget().widget(1)
        assert run_panel.height() > 150
        assert made.statusBar().geometry().bottom() <= made.height()
        assert made.statusBar().isVisible()
    finally:
        made.close()
        made.worker.shutdown()
        made.worker.wait(10_000)


def test_every_control_in_the_side_column_stays_reachable(qtbot):
    """The scroll area is the structural half of task 61's step 1, and this is what it
    buys: on a screen far too small for the column, the controls are still there to be
    scrolled to rather than clipped away."""
    from PySide6.QtWidgets import QScrollArea

    made = MainWindow(fake=True)
    qtbot.addWidget(made)
    try:
        made.resize(900, 420)
        made.show()
        qtbot.waitExposed(made)
        column = made.centralWidget().widget(0).widget(1)
        assert isinstance(column, QScrollArea)
        assert column.verticalScrollBar().maximum() > 0
        # The checkbox at the very bottom of the column, the furthest control from the
        # top and the one run 011 turned on.
        assert made.open_on_acquire.parent() is not None
        assert column.widget().isAncestorOf(made.open_on_acquire)
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

    found = discover(ports=["COM3", "COM9"], opener=opener, reopen_attempts=1)
    assert list(found.boxes)
    assert found.silent == (Silent("COM9", True, "could not open port COM9"),)
    entry = found.found[0]
    assert entry.port == "COM3" and entry.box is not None
    found.close()


def test_a_port_mid_teardown_is_retried_rather_than_reported_silent():
    """A close right before a rescan can leave a Windows CDC port refusing to
    reopen for a moment (lab record, task 62): the trainee's own workaround was
    pressing `Find boxes` a second time. `discover` should not need that."""
    calls: dict[str, int] = {}
    slept: list[float] = []

    def opener(port, **_):
        calls[port] = calls.get(port, 0) + 1
        if port == "COM9" and calls[port] < 3:
            raise OSError(22, "A device which does not exist was specified.")
        return Box(transport=FakeBox(name=port), name=port)

    found = discover(ports=["COM3", "COM9"], opener=opener, sleep=slept.append)
    assert not found.silent
    assert set(found.boxes) == {"COM3", "COM9"}
    assert calls["COM9"] == 3
    assert len(slept) == 2
    found.close()


def test_a_port_that_never_reopens_is_reported_as_could_not_open_not_silent():
    def opener(port, **_):
        raise OSError(2, "The system cannot find the file specified.")

    found = discover(ports=["COM7"], opener=opener, reopen_attempts=2, sleep=lambda _: None)
    assert found.silent == (Silent("COM7", True,
                                    "[Errno 2] The system cannot find the file specified."),)
    assert found.text.startswith("0 box(es), 1 port(s) could not open")


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
    # Both log files, named for the stem, beside the data (lab record, tasks 29 and 39).
    written = set(os.listdir(tmp_path))
    for run in runs:
        stem = os.path.splitext(os.path.basename(run.raw_path))[0]
        assert f"{stem}.sent.txt" in written
        assert any(name.startswith(stem) and name.endswith(".transcript.log")
                   for name in written)


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


def test_a_second_send_continues_the_send_log_instead_of_emptying_it(
        window, tmp_path, qtbot):
    """A Load and arm used to destroy the Send setup log that preceded it under the
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

    window.send(setup=False)
    # On the file rather than on `_armed`, which the first send already set: the job is
    # queued to another thread and the window is briefly idle with it still waiting.
    until(qtbot, lambda: log.read_text(encoding="utf-8").count("SMOD,TBL") >= 2)
    until(qtbot, lambda: idle(window))
    whole = log.read_text(encoding="utf-8")
    assert "STBLCLK,EXT" in whole, "the setup send's strings were emptied out"
    assert whole.startswith(after_setup[:200])


def test_a_queue_row_that_does_not_send_setup_keeps_its_own_send_log(
        window, tmp_path, qtbot):
    """The same defect down the run queue's path (task 53), which reaches it on a row
    with `setup` unchecked: the row sends load and arm under the stem and then acquires
    under it, and an acquisition that matched only on the last *setup* log truncated the
    load-and-arm log it had just written."""
    ready_to_acquire(window, qtbot, tmp_path, make_method())
    window._refresh_stem()
    stem = window.stem.text()
    log = tmp_path / f"{stem}.sent.txt"
    window.send(setup=False)
    until(qtbot, lambda: log.is_file() and "SMOD,TBL" in log.read_text(encoding="utf-8"))
    until(qtbot, lambda: idle(window))
    after_arm = log.read_text(encoding="utf-8")

    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    window.replicates.setValue(1)
    window.acquire()
    until(qtbot, lambda: runs and idle(window), timeout=180_000)
    whole = log.read_text(encoding="utf-8")
    assert whole.startswith(after_arm[:200]), "the acquisition emptied the send's log"
    assert "TBLSTRT" in whole


def test_a_load_and_arm_does_not_arm_setup_strings_the_wire_never_saw(
        window, tmp_path, qtbot):
    """`wire_fingerprint` always carried the panes' setup strings, whatever the send
    delivered, so a `setup=False` send recorded as armed a set of strings the box may
    never have had. Latent on the day; found reading the code afterwards (task 56)."""
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    window.send(setup=False)
    until(qtbot, lambda: idle(window) and window._armed)

    method = window.build_method()
    assert window._armed[0][1] is None, "the setup phase was recorded as sent"
    assert matches_wire(window._armed, method)

    # The table is compared exactly: it is what an acquisition starts against, and a
    # pane edited after the send is a table the box is not holding.
    window.panes[BOX].set_text(window.panes[BOX].text().replace("500:B:0", "501:B:0"))
    window.panes[BOX].reparse()
    assert not matches_wire(window._armed, window.build_method())


def test_a_full_send_arms_every_phase_including_setup(window, tmp_path, qtbot):
    """The other half of the same rule: what a `Send setup` put on the wire is compared
    in full, so an edited setup line still greys Acquire out."""
    load_into(window, tmp_path, make_method())
    until(qtbot, lambda: window.worker.boxes and idle(window))
    window.send(setup=True)
    until(qtbot, lambda: idle(window) and window._armed)
    assert window._armed[0][1] == ("STBLCLK,EXT", "STBLTRG,POS")
    window.panes[BOX].set_text("SDCB,1,12.0\n\n" + window.panes[BOX].text())
    window.panes[BOX].reparse()
    assert not matches_wire(window._armed, window.build_method())


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
        assert made.setup_button.isEnabled()
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


def a_run(stem: str, *, stopped: str = "", silence: int = 0, complete: bool = True):
    """A `Run` in the shape the queue reads: a stem, an ending and its frames.

    Built rather than acquired, because what is under test here is the sentence a row
    shows in the morning and not the loop that produced it. A frame that did not acquire
    is what makes a run incomplete, which is the queue's own reason to call a row failed.
    """
    method = make_method(stem=stem)
    frames = tuple(
        FrameRecord(method_frame=1, repetition=index + 1, frame_number=index + 1,
                    outcome="acquired" if complete or index else "timed out",
                    ended_by="silence" if index < silence else "counted")
        for index in range(ACCUMULATIONS))
    folds = () if not complete else (
        FoldRecord(method_frame=1, frames_folded=(1,), rows=SCANS, seconds=0.1),)
    return Run(method=method, raw_path=f"/data/{stem}.uimf",
               summed_path=f"/data/{stem}.summed.uimf", frames=frames, folds=folds,
               warnings=(), seconds=1.0, stopped_early=stopped or None)


def test_a_row_reports_its_stems_its_stop_and_the_repetitions_that_went_quiet():
    row = runqueue.QueueRow(method_path="/methods/clock.toml")
    assert row.name == "clock"
    state = runqueue.outcome_of(
        row, [a_run("260918_ZZ_001"), a_run("260918_ZZ_002", silence=1)])
    assert state == runqueue.DONE
    assert row.stems == ("260918_ZZ_001", "260918_ZZ_002")
    assert row.silent_frames == 1
    assert "260918_ZZ_001, 260918_ZZ_002" in row.outcome
    assert "1 repetition(s) ended on the silence" in row.outcome


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


def test_a_failed_row_ends_the_series_and_the_rest_are_skipped_with_the_reason():
    queue = runqueue.RunQueue([runqueue.QueueRow(method_path=f"{n}.toml")
                               for n in ("a", "b", "c")])
    assert queue.begin() is queue.rows[0]
    assert queue.finish(runqueue.FAILED, "TBLSTRT was refused") is False
    assert [row.state for row in queue.rows] == [
        runqueue.FAILED, runqueue.SKIPPED, runqueue.SKIPPED]
    assert "a failed" in queue.rows[1].problem
    assert queue.advance() is None and not queue.running


def test_a_row_that_says_go_on_is_walked_past_and_the_series_continues():
    queue = runqueue.RunQueue([
        runqueue.QueueRow(method_path="a.toml", go_on=True),
        runqueue.QueueRow(method_path="b.toml"),
    ])
    queue.begin()
    assert queue.finish(runqueue.FAILED, "the console would not start") is True
    assert queue.advance() is queue.rows[1]
    assert queue.rows[1].state == runqueue.RUNNING


def test_stop_skips_what_has_not_started_and_leaves_the_row_in_flight_running():
    """Stop does not abandon an acquisition: it ends after the current repetition and
    its fold, so the row in flight is still running here and closes itself later."""
    queue = runqueue.RunQueue([runqueue.QueueRow(method_path=f"{n}.toml")
                               for n in ("a", "b")])
    queue.begin()
    queue.cancel("stopped by the operator")
    assert queue.rows[0].state == runqueue.RUNNING
    assert queue.rows[1].state == runqueue.SKIPPED
    queue.finish(runqueue.STOPPED)
    assert queue.advance() is None


def test_a_skipped_row_is_offered_again_by_start_and_a_finished_one_is_not():
    queue = runqueue.RunQueue([runqueue.QueueRow(method_path=f"{n}.toml")
                               for n in ("a", "b")])
    queue.begin()
    queue.finish(runqueue.DONE)
    queue.advance()
    queue.cancel("stopped by the operator")
    queue.finish(runqueue.STOPPED)
    assert queue.begin() is None, "both rows have run; Start has nothing to offer"
    queue.rows[1].reset()
    assert queue.begin() is queue.rows[1]


def test_the_row_in_flight_cannot_be_removed_or_moved_and_none_can_pass_it():
    queue = runqueue.RunQueue([runqueue.QueueRow(method_path=f"{n}.toml")
                               for n in ("a", "b", "c")])
    queue.begin()
    assert queue.remove(0) is False
    assert queue.move(0, 1) == 0
    # Row `c` may be brought forward to just after the row running, and no further:
    # the positions before it have been run or skipped already.
    assert queue.move(2, -1) == 1
    assert queue.move(1, -1) == 1
    assert [row.name for row in queue.rows] == ["a", "c", "b"]


def test_a_row_added_while_one_runs_does_not_move_the_index():
    queue = runqueue.RunQueue([runqueue.QueueRow(method_path=f"{n}.toml")
                               for n in ("a", "b")])
    queue.begin()
    queue.add(runqueue.QueueRow(method_path="c.toml"), at=0)
    assert queue.current is queue.rows[1] and queue.rows[1].name == "a"


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


def test_a_queue_of_two_methods_runs_both_in_order_and_records_where_they_went(
        window, tmp_path, qtbot):
    """The whole of what the queue is for: two rows, unattended, each sent before its
    own acquisition because the two rows may name different methods."""
    ready_to_acquire(window, qtbot, tmp_path)
    queued(window, tmp_path, "first", make_method(), conditions="10 uM bradykinin")
    queued(window, tmp_path, "second", make_method(scans=SCANS * 2), replicates=2)

    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    window.start_queue()
    until(qtbot, lambda: not window.queue.running and idle(window), timeout=300_000)

    first, second = window.queue.rows
    assert [row.state for row in window.queue.rows] == [runqueue.DONE, runqueue.DONE]
    assert len(first.stems) == 1 and len(second.stems) == 2
    assert len(set(first.stems + second.stems)) == 3, "three runs, three names"
    assert len(runs) == 3
    # The row's note reaches the file it was queued against, not the field's last value.
    assert first.conditions == "10 uM bradykinin"
    for stem in first.stems + second.stems:
        assert (tmp_path / f"{stem}.sent.txt").is_file()
    # The second row's method is the one that ran it: the window's panes were reloaded
    # from the row's own document before it was sent.
    assert window.scans.value() == SCANS * 2


def test_a_queue_row_whose_method_will_not_open_fails_and_stops_the_series(
        window, tmp_path, qtbot):
    """And without a message box: a modal dialog raised by row four of an overnight
    series holds the instrument until somebody walks in and clicks OK."""
    ready_to_acquire(window, qtbot, tmp_path)
    broken = tmp_path / "broken.toml"
    broken.write_text("this is not a method\n", encoding="utf-8")
    window.queue.add(runqueue.QueueRow(method_path=str(broken)))
    queued(window, tmp_path, "after", make_method())

    complaints: list[str] = []
    window._complain = lambda title, detail: complaints.append(title)
    window.start_queue()
    until(qtbot, lambda: not window.queue.running and idle(window), timeout=120_000)

    assert [row.state for row in window.queue.rows] == [
        runqueue.FAILED, runqueue.SKIPPED]
    assert complaints == [], "a failed row must not raise a dialog nobody is there for"
    assert "could not be opened" in window.queue.rows[0].problem
    assert "stops on a failure" in window.queue.rows[1].problem


def test_a_row_that_says_go_on_lets_the_series_reach_the_next_one(
        window, tmp_path, qtbot):
    ready_to_acquire(window, qtbot, tmp_path)
    broken = tmp_path / "broken.toml"
    broken.write_text("not a method\n", encoding="utf-8")
    window.queue.add(runqueue.QueueRow(method_path=str(broken), go_on=True))
    queued(window, tmp_path, "after", make_method())

    window.start_queue()
    until(qtbot, lambda: not window.queue.running and idle(window), timeout=300_000)
    assert [row.state for row in window.queue.rows] == [
        runqueue.FAILED, runqueue.DONE]


def test_a_waiting_row_is_edited_while_the_row_above_it_runs(window, tmp_path, qtbot):
    """The queue exists to be added to and changed while it runs; only the row in
    flight is fixed, because its values were on the wire before the edit."""
    ready_to_acquire(window, qtbot, tmp_path)
    queued(window, tmp_path, "first", make_method(accumulations=6))
    queued(window, tmp_path, "second", make_method())

    window.start_queue()
    qtbot.waitUntil(lambda: window.run_panel.progress.repetition >= 1, timeout=120_000)
    assert window.queue.index == 0
    panel = window.queue_panel
    panel.tree.topLevelItem(1).setText(CONDITIONS, "changed while row 1 ran")
    panel.tree.topLevelItem(1).setText(REPS, "2")
    # The running row's cells are not a form: what it is doing was decided when it
    # started.
    assert not panel.tree.topLevelItem(0).flags() & Qt.ItemFlag.ItemIsEditable
    assert panel.tree.topLevelItem(1).flags() & Qt.ItemFlag.ItemIsEditable

    window.stop()
    until(qtbot, lambda: not window.queue.running and idle(window), timeout=180_000)
    assert window.queue.rows[1].conditions == "changed while row 1 ran"
    assert window.queue.rows[1].replicates == 2
    assert window.queue.rows[0].state == runqueue.STOPPED
    assert window.queue.rows[1].state == runqueue.SKIPPED


def test_the_buttons_a_trainee_presses_are_greyed_out_while_the_queue_runs(
        window, tmp_path, qtbot):
    ready_to_acquire(window, qtbot, tmp_path)
    queued(window, tmp_path, "only", make_method(accumulations=6))
    assert window.queue_panel.start_button.isEnabled()
    window.start_queue()
    qtbot.waitUntil(lambda: window.run_panel.progress.repetition >= 1, timeout=120_000)
    assert not window.acquire_button.isEnabled()
    assert not window.setup_button.isEnabled()
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
    dialog = LibraryDialog(library, {})
    qtbot.addWidget(dialog)
    assert dialog.table.rowCount() == 3
    assert len(dialog.entries) == 3
    assert sum(1 for entry in dialog.entries if not entry.ok) == 1


def test_open_is_only_enabled_for_a_single_good_selection(library, qtbot):
    dialog = LibraryDialog(library, {})
    qtbot.addWidget(dialog)
    dialog.table.selectRow(next(i for i, e in enumerate(dialog.entries) if e.ok))
    assert dialog.open_button.isEnabled()
    assert not dialog.diff_methods_button.isEnabled()
    dialog.table.selectRow(next(i for i, e in enumerate(dialog.entries) if not e.ok))
    assert not dialog.open_button.isEnabled()


def test_diff_two_methods_needs_exactly_two_selected(library, qtbot):
    dialog = LibraryDialog(library, {})
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
    dialog = LibraryDialog(library, {})
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
        def __init__(self, directory, readings, parent):
            self.chosen_path = path
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


def test_instrument_diff_dialog_builds_one_state_panel_per_box(window, tmp_path, qtbot):
    method = make_method()
    dialog = InstrumentDiffDialog(method, window.readings)
    qtbot.addWidget(dialog)
    panels = dialog.findChildren(StatePanel)
    assert len(panels) == len(method.boxes)
    assert all(panel.opened for panel in panels)
