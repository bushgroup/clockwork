"""The window a person runs an acquisition day on SLIMPHONY from.

Five tabs, one for each way of using it (the window revision's E1; lab record, task 169):
**Template** renders a template at knob values and runs it; **Queue** runs a row queue or
an agent's series plan and follows any client's series; **Method** holds one pane per box
with the strings that used to be pasted into MIPS_QT6's terminal, the `[acquisition]`
settings as a form, Acquire and Send; **Day** lists the day's requests as their records
word them; **Setup** holds where the files go, the sitting, the boxes and the console.
Under all five, the run strip (the job in flight, whose, where, minutes left; the pauses,
Resume, Stop, the note, the "at the window" initials, the beam guard) and the run log.
No FALKOR, no MIPS_QT6, no terminal. The lab record's task 50 is the first design and
its decisions; task 169 is the revision this layout is.

**It holds no copy of the acquisition sequence and no interpretation of a string.**
`send_phases` and `run_acquisition` are the whole of what an acquisition is, and
`clockwork.method.text` is the whole of what a line means; the window's job is to put a
method together out of the panes, hand it to the worker and render what comes back. When
this module and the loop could be said to disagree, the loop is right. The one judgement
made here is which button may be pressed, and even that is `refusals(method)`.

**Nothing here touches the instrument.** Every serial write and every round trip to the
console is in `clockwork serve`, whose client this window is (`RemoteWorker`), or under
`--fake` on `Worker`'s thread; this thread assembles jobs and drains a mailbox on a timer. The
timer is 50 ms, which is faster than a repetition and slower than a batch, so the bar
moves smoothly and a hundred repetitions cost a hundred redraws rather than a thousand.

**One path onto the wire.** There is no terminal and no second sender: every string that
reaches a box is in a pane, becomes part of a `Method`, and goes through `send_phases`.
That is what makes the send log beside a file a description of the method, and the
file's own stamp a description of what was sent.
"""

from __future__ import annotations

import datetime as _dt
import os
import threading
import time
from collections.abc import Callable
from dataclasses import replace

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import clockwork

from .. import instrument as instrument_module
from .. import keep, library_dirs, planning
from .. import method as method_module
from .. import series as series_module
from ..acq import BeamGuard, StateRead, Warned, find_console
from ..acq.loop import WHEN_ARMED, RunBegun, cautions, refusals
from ..dayrecord import closed_sitting
from ..envelope import Limits
from ..instrument import UNCALIBRATED, Instrument
from ..mcp.audit import AuditLog
from ..method import (
    MAX_ACCUMULATIONS,
    MAX_FRAMES,
    MAX_SCANS,
    REPETITION_MODES,
    Acquisition,
    BoxMethod,
    Enable,
    Metadata,
    Method,
    RfChannel,
    is_comment,
    same_run,
)
from ..method import template as templates
from ..method.template import Rendered
from ..method.text import PaneResult, render_pane, start_order
from ..owner import (
    POINT,
    REPETITION,
    GuardChanged,
    Held,
    LogsOpened,
    PauseAsked,
    PlannedPoint,
    PointStarted,
    RecordBegun,
    Resumed,
    SeriesJob,
    SeriesResult,
    StopAsked,
)
from ..owner.hold import refusal as pause_refusal
from ..sitting import instrument_and_limits
from . import errors, runqueue
from .boxstate import Reading
from .console_panel import ConsoleBar, ConsoleSettings
from .daypanel import DayPanel
from .launch import (
    open_data_file,
    open_data_file_with_options,
    open_path,
    still_running,
    viewer_options,
)
from .librarypanel import LibraryDialog
from .naming import clean_initials, next_stem
from .panes import BoxPane
from .planpanel import FollowedSeries, PlanPanel, SeriesFollow
from .queuepanel import BeamGuardDialog, QueuePanel, RandomizeDialog
from .runlog import RunPanel
from .runqueue import QueueRow, RunQueue
from .serving import serve_command, start_serve
from .settings import Settings
from .templatetab import TemplateTab
from .worker import (
    Acquire,
    ConsoleStatus,
    Discover,
    Job,
    Mailbox,
    ReadState,
    RemoteWorker,
    RestartConsole,
    Send,
    SendResult,
    StartConsole,
    Worker,
    matches_wire,
    wire_fingerprint,
)

__all__ = ["DAY_TAB", "DRAIN_MS", "METHOD_TAB", "QUEUE_TAB", "SETUP_TAB", "TEMPLATE_TAB",
           "MainWindow", "TABS"]

TEMPLATE_TAB, QUEUE_TAB, METHOD_TAB, DAY_TAB, SETUP_TAB = (
    "Template", "Queue", "Method", "Day", "Setup")
TABS = (TEMPLATE_TAB, QUEUE_TAB, METHOD_TAB, DAY_TAB, SETUP_TAB)
"""The five tabs, in the order the window shows them (E1)."""

MAINSPRING_TIP = ("Open the file in mainspring, which is the only viewer clockwork has. "
                  "The summed companion once the fold has written it, the raw file "
                  "before that.")
"""The button between runs: a file that is finished, and the fold's companion for choice."""

MAINSPRING_LIVE_TIP = (
    "Open the file being acquired in mainspring, following it as the console writes "
    "it, in the Show mode this method's repetition mode asks for. The raw file and "
    "never the summed companion: the companion is written by a fold afterwards and is "
    "not the file filling now. Press it again for a second viewer.")
"""The button during a run. Different enough from the other to be worth saying."""

DRAIN_MS = 50
"""How often the UI thread empties the worker's progress mailbox.

Faster than a repetition (about a second) so the bar moves while a method frame runs,
and slower than a published batch so a frame's ten batches cost one redraw rather than
ten. Nothing is lost by the gap: the mailbox keeps everything and collapses only the
counter.
"""


class MainWindow(QMainWindow):
    """Everything a trainee sees. One worker below it, one method held in the panes."""

    error_seen = Signal(str)
    """A traceback has been written to the errors log, and this is the line to say.

    A signal rather than a direct call because `threading.excepthook` fires on the
    thread that raised: the worker's. Adding a tree item from there would be touching
    widgets off the UI thread, which is the one thing this window does not do. An
    auto-connected signal queues it onto the UI thread, which is the same route the
    mailbox's own events take.
    """

    report_ready = Signal(str, str, str)
    """`(address, kept folder, problem)` from the thread that kept a report's files."""

    kept_pruned = Signal(str)
    """A line about the start-up prune of the kept-files root, for the run log."""

    def __init__(self, *, fake: bool = False,
                 client: Callable[[Mailbox], RemoteWorker] | None = None) -> None:
        """`client` builds the daemon's client in place of the one this window would,
        for a test that points it at a daemon of its own."""
        super().__init__()
        self.fake = fake
        self.settings = Settings()
        self.mailbox = Mailbox()
        self.worker: Worker | RemoteWorker
        if fake:
            # A --fake run's failures are no evidence about the instrument, so its owner
            # keeps nothing; a report made from it still does (task 81). In this
            # process, with no daemon: nothing --fake does is worth a second process,
            # and the window tests stay as fast as they were (task 77).
            self.worker = Worker(fake=True, mailbox=self.mailbox, kept_root="",
                                 errors_log=errors.errors_log_path())
        else:
            # The instrument belongs to `clockwork serve`; the window is one of its
            # clients (task 77), and starts it at its first act that needs the instrument
            # when none is running (task 175). What the daemon needs of this window's
            # settings goes on its command line, built as it is launched.
            self._launch_command = self._serve_command()
            self.worker = client(self.mailbox) if client is not None else RemoteWorker(
                mailbox=self.mailbox, launch=lambda: start_serve(self._launch_command),
                console_path=self.settings.console_path)
        self._close_when_idle = False
        self._closed_when_idle = False
        self._scan_when_idle = False
        """Set by a join that found the daemon busy: the scan it put off, made by the
        first status that shows the daemon idle (lab record, task 96)."""
        """Set by a close that chose to stop this window's run: the window closes itself
        when the job comes back, having shown the frame end in the run log."""

        self.method_path = self.settings.method_path
        self.instrument_path = self.settings.instrument_path
        self.instrument: Instrument = UNCALIBRATED
        self.metadata = Metadata(name="untitled", created=_dt.date.today())
        self._closed_ok = ""
        """A closed sitting's folder the person at this window chose to start clockwork
        serve on anyway, asked about once (lab record, task 175)."""
        self._serve_output = ""
        """The output folder of the `clockwork serve` this window is a client of, from its
        `hello`, or empty in process or for a serve from before it said one. Where set it
        is where the window's runs, Day tab and plan checks are, in the window that started
        the serve as in one that joined it, and it is never remembered (lab record, task
        172)."""
        self._at_window_confirmed = False
        """Whether the person at the window has typed or confirmed the field since this
        window opened. A value restored from an earlier session, or prefilled from the file
        initials, is asked about at the first Start (A2 as revised; lab record, task
        172)."""
        self.ports: dict[str, str] = {}
        """Which port each box answered on, or the method's hint for one that did not.
        A pane's header shows what the rack said; this is what the method is built with.
        """
        self.declared: dict[str, tuple[tuple[tuple[int, float], ...],
                                       tuple[RfChannel, ...]]] = {}
        """The DC bias and RF each box's method declares. Not pane text -- they are form
        fields in the document rather than strings a trainee types -- so they are kept
        here across an edit and written back out unchanged (`method-file-format.md`)."""

        self.rendered: Rendered | None = None
        """The render the panes are attached to, or None for a hand-written method.

        Set by opening a rendered file whose `[rendered]` table still reproduces its
        strings (`templates.reproduce`), and cleared -- once, for good, with one line in
        the run log -- by the first edit after which the panes no longer make the same run
        (`same_run`). While it is set, an Acquire carries the template, knobs and labels
        and the run stamps as rendered, exactly as a run Claude acquires from the template
        does (lab record, task 94). Reopening the file is what attaches it again."""

        self.panes: dict[str, BoxPane] = {}
        self.readings: dict[str, Reading] = {}
        """The last state reading of each box, kept here rather than in the pane.

        A pane is rebuilt whenever the rack's box list changes, and what a box last
        answered does not belong to a widget: a rediscovery that finds the same three
        boxes should not lose three readings that cost fifteen seconds."""

        self._job: Job | None = None
        self._sent_shown: tuple | None = None
        """The send the state panels were last marked against, so they are re-marked
        only when it moves."""
        self._described: dict[str, tuple[str, str, bool]] = {}
        """What each box answered at the last scan, `(port, firmware, answered)`, kept
        here so that a pane rebuilt by a method load keeps its header (the window
        design's FLAG of 2026-09-22, closed by task 169): the boxes were never closed by
        a load, only the display of them was, and the natural repair was Find boxes."""

        self.queue = RunQueue()
        """The run queue's rows, empty until a person puts something in it (task 53).
        Start hands a snapshot of them to the owner as one series job (task 168)."""

        self._queue_job: SeriesJob | None = None
        """The queue job this window started, while it runs: the owner's series job
        whose points are the rows (lab record, task 168). Its progress is written back
        onto the rows; the queue itself is the owner's and outlives this window."""

        self._plan_job: SeriesJob | None = None
        """The series a person started here off an open plan, until it comes back."""
        self._prefilled = ""
        """The purpose line a plan's `description` filled in, so a second plan replaces
        it and a purpose the person typed is left alone (A1)."""
        self.followed: FollowedSeries | None = None
        self._held_said = ""
        """The pause the status bar last said, so that it is said once."""
        """The series the followed table shows: the last any client started (R5)."""

        self._last_run_paths: tuple[str, str, str] = ("", "", "")
        """`(raw, summed, stem)` of the last run, for Open in mainspring and Open the
        log. The summed path is only offered once the fold has written it, and the
        directory is taken off the paths rather than off the field, which a trainee may
        have changed since."""

        self._live_run: tuple[str, str, str] = ("", "", "")
        """The same three for the run *in progress*, empty between jobs (task 55).

        Taken from `RunBegun`, which is the moment the raw file exists, rather than
        from `_run_done`, which is a minute later and the whole point: a trainee who
        wants to know whether anything is happening needs the file while it is filling.
        A replicate series keeps the previous run's names through the gap between one
        run and the next, which is right -- that file is still there and still worth
        opening -- and the job ending clears it."""

        self._logged: tuple[str, str] = ("", "")
        """`(directory, stem)` of the last piece of work that opened its transcript, for
        Report (lab record, task 104).

        Taken from `LogsOpened`, which every send and every run reports before its first
        string goes out, rather than from `RunBegun`, which a run stopped before its first
        frame never reaches: a refused `acquire` left Report knowing no stem at all, so it
        kept the method alone in a second folder beside the one the failure had already
        kept. Never cleared, since a run that has ended is exactly the one reported on;
        `_live_run` keeps its narrower meaning, the files being written now."""

        self._viewer_launched = False
        """Whether this window has opened a viewer since it started.

        Not persisted and not per run. A viewer launched with `--follow` ends with
        `Live` ticked and moves to each later acquisition of the session by itself
        through the run pointer (task 58), so a second launch would only be a second
        window doing the same thing. A trainee who closes the viewer wants the next
        Acquire to bring one back, which is what `_viewer_is_up` below is for."""

        self._viewer_process: object | None = None
        """What the launch handed back, where the route that took it has a handle:
        polled, never waited on, so that a closed viewer is noticed."""

        self.launched_commands: list[list[str]] = []
        """Under `--fake`, the command lines a launch would have run. A simulated
        instrument writes a simulated file and a viewer opened on one would be showing
        numbers that came from nowhere, so the command line is the thing to check at a
        desk and it is recorded and said rather than run."""

        self._build()
        self._connect()
        if isinstance(self.worker, RemoteWorker):
            self.worker.start()  # after the slots, so that `connected` finds one
        self._restore()
        # After `_build`, since the line goes in the run panel. `main` installed the
        # hooks before this window existed, so anything raised on the way up here is
        # already in the file; this is what puts the next one in front of the person
        # watching, rather than only in a file nobody has been told to read (task 61).
        self._stop_reporting = errors.reporting_to(self.error_seen.emit)
        self.error_seen.connect(self._say_error)
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(DRAIN_MS)
        self._drain_timer.timeout.connect(self._drain)
        self._drain_timer.start()
        self._refresh_actions()
        self.prune_kept()

    # -- construction --------------------------------------------------------

    def _build(self) -> None:
        self.setWindowTitle(f"clockwork {clockwork.__version__}"
                            + ("  — simulated instrument" if self.fake else ""))

        # Five tabs, one for each way of using the window, over the run strip and the run
        # log, which stay on screen under all of them (the window revision's E1; lab
        # record, task 169). Reverses the window design's decision 7, one method at a
        # time with the queue as a dock: since then the daemon, templates and series
        # give the window three ways of starting a run and a fourth use, following one
        # it did not start.
        self.template_tab = TemplateTab()
        self.queue_panel = QueuePanel(self.queue)
        self.plan_panel = PlanPanel()
        self.follow = SeriesFollow()
        self.queue_tabs = QTabWidget()
        self.queue_tabs.addTab(self.queue_panel, "Rows")
        self.queue_tabs.addTab(self.plan_panel, "Series plan")
        self.queue_tab = QSplitter(Qt.Orientation.Vertical)
        """The Queue tab: the row queue and a series plan, one surface (R9), over the
        followed table, which follows any client's series (R5)."""
        self.queue_tab.addWidget(self.queue_tabs)
        self.queue_tab.addWidget(self.follow)
        self.method_tab = self._method_tab()
        self.day_panel = DayPanel()
        self.setup_tab = self._setup_tab()

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        for widget, name in ((self.template_tab, TEMPLATE_TAB), (self.queue_tab, QUEUE_TAB),
                             (self.method_tab, METHOD_TAB), (self.day_panel, DAY_TAB),
                             (self.setup_tab, SETUP_TAB)):
            self.tabs.addTab(widget, name)

        self.run_panel = RunPanel()
        whole = QSplitter(Qt.Orientation.Vertical)
        whole.addWidget(self.tabs)
        whole.addWidget(self.run_panel)
        whole.setStretchFactor(0, 3)
        whole.setStretchFactor(1, 2)
        whole.setChildrenCollapsible(False)
        self.setCentralWidget(whole)

        self.lock_label = QLabel()
        """The one sentence a window refused the instrument lock shows: who holds it."""
        self.lock_label.setVisible(False)
        self.statusBar().addPermanentWidget(self.lock_label)
        self.boxes_label = QLabel("no boxes yet")
        """Which boxes answered, on which ports: the status bar's boxes."""
        self.daemon_label = QLabel("in process, --fake" if self.fake else "no daemon yet")
        """Which owner holds the instrument: the status bar's daemon."""
        self.statusBar().addPermanentWidget(self.boxes_label)
        self.statusBar().addPermanentWidget(self.daemon_label)
        self.console_bar = ConsoleBar()
        self.statusBar().addPermanentWidget(self.console_bar)
        self.statusBar().showMessage("ready")
        self._build_menus()

    def _method_tab(self) -> QWidget:
        """The Method tab: the panes and their state panels, the acquisition form,
        Acquire and Send, and the line saying whether the panes are what the boxes hold."""
        self.pane_box = QSplitter(Qt.Orientation.Horizontal)
        self.pane_box.setChildrenCollapsible(False)
        # The panes no longer set the window's minimum width (the window design's FLAG
        # of 2026-09-19, closed by task 169): three of them side by side wanted some
        # 400 px more than the instrument's monitor gave, so they scroll sideways
        # inside the tab rather than widen the window.
        pane_scroll = QScrollArea()
        pane_scroll.setWidget(self.pane_box)
        pane_scroll.setWidgetResizable(True)
        pane_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        pane_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pane_scroll = pane_scroll
        self.start_order = QLabel("")
        self.start_order.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.start_order.setFont(mono)
        self.start_order.setToolTip(
            "The order the start list is walked across boxes, derived from the panes "
            "and never typed: TARBTRG before TBLSTRT, box order within each. This is "
            "the experiment's order, not a pane's.")

        self.provenance = QLabel("")
        """Which template and knob values the panes are rendered from, while attached."""
        self.provenance.setWordWrap(True)
        self.provenance.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.provenance.setToolTip(
            "This method was rendered from a template, and its runs stamp the template, "
            "its knobs and its labels while the panes are unedited. The first edit that "
            "changes what is sent makes it a hand-written method.")
        self.provenance.setVisible(False)

        self.held_line = QLabel("")
        """Whether the panes are what the boxes hold (E2): one line over the panes."""
        self.held_line.setWordWrap(True)
        self.held_line.setToolTip(
            "Compared with what clockwork serve remembers sending the boxes last, "
            "whichever window, agent or command line sent it. The state panels under "
            "the panes mark each box's reading against that same send.")

        panes_and_order = QWidget()
        column = QVBoxLayout(panes_and_order)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.provenance)
        column.addWidget(self.held_line)
        column.addWidget(pane_scroll, 1)
        order_row = QHBoxLayout()
        order_row.setContentsMargins(0, 0, 0, 0)
        order_row.addWidget(QLabel("Start order"))
        order_row.addWidget(self.start_order, 1)
        column.addLayout(order_row)

        middle = QSplitter(Qt.Orientation.Horizontal)
        middle.addWidget(panes_and_order)
        middle.addWidget(self._method_column())
        middle.setStretchFactor(0, 3)
        middle.setStretchFactor(1, 1)
        middle.setChildrenCollapsible(False)
        return middle

    def _method_column(self) -> QWidget:
        """The Method tab's form half: what is acquired, the file, Acquire and Send."""
        # Ranged to the loader's own ceilings, so a box can never show a number other
        # than the one the document says: a document past a ceiling is refused at
        # open rather than clamped into the box (`clockwork.method.MAX_SCANS`).
        self.frames = _spin(1, MAX_FRAMES, 1, "How many ion mobility experiments this "
                                              "method acquires, each folded into one "
                                              "frame of the companion file.")
        self.scans = _spin(1, MAX_SCANS, 5000,
                           "Pusher pulses in one ion mobility experiment. One scan is "
                           f"one push. At most {MAX_SCANS:,}: about 21 minutes of frame "
                           "at a 62 µs pusher.")
        self.accumulations = _spin(1, MAX_ACCUMULATIONS, 100,
                                   "How many times each experiment is repeated and "
                                   "summed. Under per_repetition each is its own "
                                   "console frame with its own start edge.")
        self.repetition_mode = QComboBox()
        self.repetition_mode.addItems(list(REPETITION_MODES))
        self.repetition_mode.setToolTip(
            "per_repetition: one console frame per repetition, each started by its own "
            "edge. single_frame: the table loops on the box and the whole method frame "
            "is one console frame, which needs the enable line declared.")
        self.keep_raw = QCheckBox("keep the raw per-repetition file")
        self.keep_raw.setChecked(True)
        self.keep_raw.setToolTip(
            "On, the unsummed file is kept beside the companion. Off, it is deleted "
            "once a fold has written the companion that replaces it; if something has "
            "that file open and the delete is refused, the run log says so and says "
            "which file to keep.")
        self.enable_box = QComboBox()
        self.enable_box.setToolTip(
            "Which box drives the digitizer's enable line, and on which digital "
            "output. Declared rather than assumed: it is a fact about the cabling. "
            "single_frame needs it; per_repetition does not.")
        self.enable_channel = QLineEdit()
        self.enable_channel.setPlaceholderText("DIOA")
        self.enable_channel.setMaximumWidth(80)

        acquisition = QGroupBox("Acquisition")
        form = QFormLayout(acquisition)
        _tighten(form)
        form.addRow("Method frames", self.frames)
        form.addRow("Scans", self.scans)
        form.addRow("Accumulations", self.accumulations)
        form.addRow("Repetition mode", self.repetition_mode)
        form.addRow("Enable", _row(self.enable_box, self.enable_channel))
        form.addRow("", self.keep_raw)

        self.stem = QLineEdit()
        self.stem.setToolTip(
            "What this acquisition's files will be called. Filled from the output "
            "directory each time -- one past the highest number the file initials "
            "already use there -- and editable before Acquire. Each replicate takes the "
            "next number without asking.")
        rename = QPushButton("Next")
        rename.setMaximumWidth(56)
        rename.setToolTip("Re-read the output directory and take the next free number.")
        rename.clicked.connect(self._refresh_stem)
        self.replicates = QSpinBox()
        self.replicates.setRange(1, 999)
        self.replicates.setPrefix("replicates: ")
        self.replicates.setToolTip(
            "Files acquired from one press, the first and its technical replicates, "
            "run unattended: the method's reset list, then the same again into a file "
            "of its own. Stop ends them after the current repetition.")
        self.conditions = QPlainTextEdit()
        self.conditions.setPlaceholderText(
            "sample, MCP voltage, pusher period, collision energy…")
        self.conditions.setMaximumHeight(56)
        self.conditions.setToolTip(
            "The part of the experiment no getter reads. Stamped into every file this "
            "method writes and into the header of its send log. Everything else in "
            "that record came off a wire; this exists only if somebody types it.")

        self.acquire_button = QPushButton("Acquire")
        self.send_button = QPushButton("Send")
        self.setup_again = QCheckBox("send setup again")
        self.mainspring_button = QPushButton("Open in mainspring")
        self.log_button = QPushButton("Open the log")
        for button, tip in (
            (self.acquire_button, "Send the boxes what they do not already hold of the "
                                  "panes, then acquire the first file and its "
                                  "replicates. On boxes that hold this method nothing "
                                  "is sent, and the first file is a replicate."),
            (self.send_button, "Send the boxes what they do not already hold of the "
                               "panes, and acquire nothing: every phase where they "
                               "hold something else, setup only where it differs from "
                               "what they were last sent."),
            (self.setup_again, "Send every box its setup phase with the next Acquire or "
                               "Send, even where it is what they were last sent: for a "
                               "box switched off and on since. Clears itself once sent."),
            (self.mainspring_button, MAINSPRING_TIP),
            (self.log_button, "Open both files a run leaves beside the data: the wire "
                              "transcript and the send log."),
        ):
            button.setToolTip(tip)

        files = QGroupBox("This run")
        files_form = QFormLayout(files)
        _tighten(files_form)
        files_form.addRow("Name", _row(self.stem, rename))
        files_form.addRow("Conditions", self.conditions)
        files_form.addRow(_row(self.acquire_button, self.replicates))
        files_form.addRow(_row(self.send_button, self.setup_again))
        files_form.addRow(_row(self.mainspring_button, self.log_button))

        self.problems = QLabel("")
        self.problems.setWordWrap(True)
        self.problems.setTextFormat(Qt.TextFormat.PlainText)

        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setSpacing(6)
        column.addWidget(acquisition)
        column.addWidget(files)
        column.addWidget(self.problems)
        column.addStretch(1)

        # The column does not get to decide how tall the window is (task 61): it
        # scrolls inside the tab rather than push the run strip off a short screen.
        frame = QScrollArea()
        frame.setWidget(panel)
        frame.setWidgetResizable(True)
        frame.setFrameShape(QScrollArea.Shape.NoFrame)
        frame.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        frame.setMinimumHeight(acquisition.minimumSizeHint().height())
        frame.setMinimumWidth(panel.minimumSizeHint().width())
        frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        return frame

    def _setup_tab(self) -> QWidget:
        """The Setup tab: where the files go, the sitting, the boxes, the console, the
        kept files and the viewer (E1, E6, E8)."""
        self.output_dir = QLineEdit()
        self.output_dir.setPlaceholderText("where the files go")
        self.browse_output = QPushButton("…")
        self.browse_output.setMaximumWidth(32)
        self.browse_output.clicked.connect(self._pick_output_dir)
        self.initials = QLineEdit()
        self.initials.setMaximumWidth(80)
        self.initials.setToolTip(
            "The middle field of every file name, remembered per machine. The lab's "
            "files use the ion's code here; the window takes no view.")
        files = QGroupBox("Files")
        files_form = QFormLayout(files)
        _tighten(files_form)
        files_form.addRow("Output directory", _row(self.output_dir, self.browse_output))
        files_form.addRow("File initials", self.initials)

        self.instrument_field = QLineEdit()
        self.instrument_field.setReadOnly(True)
        self.instrument_field.setPlaceholderText("no instrument document")
        self.browse_instrument = QPushButton("…")
        self.browse_instrument.setMaximumWidth(32)
        self.browse_instrument.clicked.connect(self._pick_instrument)
        self.instrument_status = QLabel("")
        self.instrument_status.setWordWrap(True)
        self.limits_field = QLineEdit()
        self.limits_field.setReadOnly(True)
        self.limits_field.setPlaceholderText("beside the instrument document, if any")
        self.browse_limits = QPushButton("…")
        self.browse_limits.setMaximumWidth(32)
        self.browse_limits.clicked.connect(lambda: self._pick_limits(project=False))
        self.project_limits_field = QLineEdit()
        self.project_limits_field.setReadOnly(True)
        self.project_limits_field.setPlaceholderText("none")
        self.browse_project_limits = QPushButton("…")
        self.browse_project_limits.setMaximumWidth(32)
        self.browse_project_limits.clicked.connect(lambda: self._pick_limits(project=True))
        self.sitting_status = QLabel("")
        """Whose sitting this is: the daemon this window started, or one it joined."""
        self.sitting_status.setWordWrap(True)
        sitting = QGroupBox("The sitting")
        sitting_form = QFormLayout(sitting)
        _tighten(sitting_form)
        sitting_form.addRow("Instrument document",
                            _row(self.instrument_field, self.browse_instrument))
        sitting_form.addRow("", self.instrument_status)
        sitting_form.addRow("Standing limits", _row(self.limits_field, self.browse_limits))
        sitting_form.addRow("Project limits",
                            _row(self.project_limits_field, self.browse_project_limits))
        sitting_form.addRow("", self.sitting_status)

        self.find_button = QPushButton("Find boxes")
        self.find_button.setToolTip(
            "Ask every MIPS-class port what box is behind it. A port that does not "
            "answer has its box off or absent; port presence says nothing. The boxes "
            "held open are asked again over their own ports and nothing is reset.")
        self.read_button = QPushButton("Read the boxes' state")
        self.read_button.setToolTip("A whole-state reading of every box, for the state "
                                    "panels under the panes.")
        boxes = QGroupBox("Boxes")
        boxes_row = QHBoxLayout(boxes)
        boxes_row.addWidget(self.find_button)
        boxes_row.addWidget(self.read_button)
        boxes_row.addStretch(1)

        self.console_button = QPushButton("Start the console")
        self.console_restart = QPushButton("Restart the console")
        self.console_settings_button = QPushButton("Console settings…")
        self.console_settings_button.setToolTip(
            "The six config.txt keys that decide what the card records, and the "
            "timeout: written only where changed, and the console restarted in the "
            "same click, since it reads the file at startup only.")
        console = QGroupBox("Acquisition console")
        console_row = QHBoxLayout(console)
        console_row.addWidget(self.console_button)
        console_row.addWidget(self.console_restart)
        console_row.addWidget(self.console_settings_button)
        console_row.addStretch(1)

        self.kept_field = QLineEdit()
        self.kept_field.setReadOnly(True)
        self.kept_field.setToolTip(
            "Where a failed run's files, and a reported one's, are copied with a "
            "manifest, so tidying the output directory never loses what a report "
            "points at.")
        kept_button = QPushButton("…")
        kept_button.setMaximumWidth(32)
        kept_button.clicked.connect(self.choose_kept_root)
        self.open_on_acquire = QCheckBox("Open mainspring when a run starts")
        self.open_on_acquire.setToolTip(
            "On, the first run that starts this session, whichever client started it, "
            "opens mainspring on the file being written, following, in the Show mode "
            "its repetition mode asks for. One viewer a session and not one a run: a "
            "viewer opened this way ends with Live ticked and moves to each later run "
            "by itself. Close it and the next run brings one back. It keeps the first "
            "run's Show mode, which is mainspring's to change.")
        other = QGroupBox("Kept files and the viewer")
        other_form = QFormLayout(other)
        _tighten(other_form)
        other_form.addRow("Kept files folder", _row(self.kept_field, kept_button))
        other_form.addRow("", self.open_on_acquire)

        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setSpacing(6)
        for group in (files, sitting, boxes, console, other):
            column.addWidget(group)
        column.addStretch(1)
        frame = QScrollArea()
        frame.setWidget(panel)
        frame.setWidgetResizable(True)
        frame.setFrameShape(QScrollArea.Shape.NoFrame)
        return frame

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.action_open = QAction("&Open method…", self)
        self.action_save = QAction("&Save method", self)
        self.action_save_as = QAction("Save method &as…", self)
        self.action_library = QAction("Method &library…", self)
        self.action_library.setToolTip(
            "Every method and template a directory holds, named, hashed, dated and "
            "described. Open a method in the Method tab, a template in the Template "
            "tab, or compare two methods.")
        self.action_plan = QAction("Open a series &plan…", self)
        self.action_open_queue = QAction("Open a run &queue…", self)
        self.action_quit = QAction("&Quit", self)
        for action in (self.action_open, self.action_save, self.action_save_as):
            file_menu.addAction(action)
        file_menu.addSeparator()
        for action in (self.action_library, self.action_plan, self.action_open_queue):
            file_menu.addAction(action)
        file_menu.addSeparator()
        self.action_forget_geometry = QAction("Forget the window position", self)
        file_menu.addAction(self.action_forget_geometry)
        file_menu.addAction(self.action_quit)

        run_menu = self.menuBar().addMenu("&Run")
        self.action_find = QAction("&Find boxes", self)
        self.action_read_state = QAction("&Read the boxes' state", self)
        self.action_console = QAction("Start the &console", self)
        for action in (self.action_find, self.action_read_state, self.action_console):
            run_menu.addAction(action)

        help_menu = self.menuBar().addMenu("&Help")
        self.action_report = QAction("&Report a problem...", self)
        self.action_report.setToolTip(
            "Open a new bug report in the browser with the version, this PC, the error "
            "log and the last run's transcript already filled in.")
        help_menu.addAction(self.action_report)
        self.action_request = QAction("Request a &feature...", self)
        self.action_request.setToolTip(
            "Open a new feature request in the browser with the version and this PC "
            "already filled in.")
        help_menu.addAction(self.action_request)
        self.action_about = QAction("&About clockwork", self)
        help_menu.addAction(self.action_about)

    def _connect(self) -> None:
        self.worker.started_job.connect(self._job_started)
        self.worker.finished_job.connect(self._job_finished)
        self.worker.failed_job.connect(self._job_failed)
        self.worker.discovered.connect(self._discovered)
        self.worker.console_state.connect(self.console_bar.show_status)
        self.worker.run_done.connect(self._run_done)
        self.worker.state_read.connect(self._state_read)
        self.worker.said.connect(self.run_panel.say)
        self.worker.warned.connect(self._warn)
        self.worker.connected.connect(self._daemon_connected)
        self.worker.no_serve.connect(self._no_serve)
        self.worker.status_changed.connect(self._owner_status)

        # `clicked` carries a `checked` flag, and every slot below takes none: bound
        # through a lambda rather than directly, or Qt passes False into the first
        # positional parameter and a keyword-only argument turns a button press into a
        # TypeError.
        self.find_button.clicked.connect(lambda: self.find_boxes())
        self.read_button.clicked.connect(lambda: self.read_state())
        self.acquire_button.clicked.connect(lambda: self.acquire())
        self.send_button.clicked.connect(lambda: self.send())
        self.mainspring_button.clicked.connect(lambda: self.open_in_mainspring())
        self.log_button.clicked.connect(lambda: self.open_the_log())
        self.console_button.clicked.connect(lambda: self.start_console())
        self.console_restart.clicked.connect(lambda: self.restart_console())
        self.console_settings_button.clicked.connect(lambda: self.console_settings())

        self.action_open.triggered.connect(self.open_method)
        # Not bound directly: `triggered` carries a `checked` flag, which would arrive
        # as `ask` and make Save behave as Save as on a menu that never asked for it.
        self.action_save.triggered.connect(lambda: self.save_method())
        self.action_save_as.triggered.connect(lambda: self.save_method(ask=True))
        self.action_library.triggered.connect(self.open_library)
        self.action_plan.triggered.connect(lambda: self.open_plan())
        self.action_open_queue.triggered.connect(lambda: self.open_queue())
        self.action_quit.triggered.connect(self.close)
        self.action_find.triggered.connect(self.find_boxes)
        self.action_read_state.triggered.connect(lambda: self.read_state())
        self.action_console.triggered.connect(self.start_console)
        self.action_report.triggered.connect(self.report_problem)
        self.action_request.triggered.connect(self.request_feature)
        self.action_about.triggered.connect(self._about)
        self.action_forget_geometry.triggered.connect(self._forget_geometry)
        self.report_ready.connect(self._report_ready)
        self.kept_pruned.connect(self.run_panel.say)

        self.console_bar.restart_requested.connect(self.restart_console)
        self.console_bar.settings_requested.connect(self.console_settings)

        self.template_tab.acquire_requested.connect(lambda: self.acquire_template())
        self.template_tab.open_requested.connect(self._open_rendered)
        self.queue_panel.start_requested.connect(self.start_queue)
        self.queue_panel.stop_requested.connect(lambda: self.stop())
        self.queue_panel.add_requested.connect(self.queue_add_methods)
        self.queue_panel.add_open_requested.connect(self.queue_add_open_method)
        self.queue_panel.changed.connect(self._refresh_actions)
        self.queue_panel.report_requested.connect(self.report_row)
        self.queue_panel.randomize_requested.connect(self.randomize_queue)
        self.queue_panel.save_requested.connect(self.save_queue)
        self.queue_panel.open_requested.connect(self.open_queue)
        self.plan_panel.open_requested.connect(lambda: self.open_plan())
        self.plan_panel.start_requested.connect(lambda: self.start_plan())
        self.plan_panel.stop_requested.connect(lambda: self.stop())
        self.follow.filter_changed.connect(self._filter_run_log)
        self.day_panel.refresh_requested.connect(self.refresh_day)
        self.run_panel.pause_requested.connect(self.pause)
        self.run_panel.resume_requested.connect(lambda: self.resume())
        self.run_panel.stop_requested.connect(lambda: self.stop())
        self.run_panel.note_requested.connect(self.keep_note)
        self.run_panel.guard_requested.connect(self.queue_guard)
        self.tabs.currentChanged.connect(self._tab_changed)

        self.initials.editingFinished.connect(self._initials_changed)
        self.run_panel.at_window.editingFinished.connect(lambda: self._at_window())
        self.run_panel.at_window.textEdited.connect(self._at_window_typed)
        self.output_dir.editingFinished.connect(self._refresh_stem)
        self.repetition_mode.currentTextChanged.connect(lambda _: self._refresh_actions())
        for spin in (self.frames, self.scans, self.accumulations):
            spin.valueChanged.connect(lambda _: self._refresh_actions())

    def _restore(self) -> None:
        geometry = self.settings.geometry
        if not geometry.isEmpty():
            self.restoreGeometry(geometry)
        self.initials.setText(self.settings.initials)
        # The person's own, remembered per machine; the file initials until one is typed.
        # Unconfirmed until typed or confirmed at the first Start of this launch.
        self.run_panel.at_window.setText(self.settings.at_window
                                         or clean_initials(self.settings.initials))
        self._show_at_window()
        self.output_dir.setText(self.settings.output_dir)
        self.replicates.setValue(self.settings.replicates)
        self.conditions.setPlainText(self.settings.conditions)
        self.open_on_acquire.setChecked(self.settings.open_mainspring_on_acquire)
        self.kept_field.setText(self.kept_root())
        self._show_sitting()
        names = [self.tabs.tabText(number) for number in range(self.tabs.count())]
        remembered = self.settings.tab
        self.tabs.setCurrentIndex(names.index(remembered) if remembered in names
                                  else self.tabs.indexOf(self.method_tab))
        if self.instrument_path:
            self._load_instrument(self.instrument_path)
        if self.method_path and os.path.isfile(self.method_path):
            self._load_method(self.method_path)
        self._refresh_stem()
        self.template_tab.set_library(self._library())
        self._refresh_limits()
        # On launch as well as on demand (lab record, task 50): a trainee arriving at the
        # instrument should find the panes already named for the boxes that are on, not
        # have to press a button to be told. Under `--fake` the rack comes from the
        # method, so `_load_method` above has already done it; otherwise the daemon
        # scans as it starts, and `_daemon_connected` scans one that was already up.

    @property
    def _armed(self) -> tuple:
        """What the boxes were last sent, whoever sent it, as `wire_fingerprint` reads
        it, or () where nothing is remembered: the owner's memory (`Armed`), not this
        window's, since a series, an agent's `arm` or another window may have sent since
        (the window revision's E2 and E7; lab record, task 169)."""
        armed = self.worker.armed
        return tuple(armed.fingerprint) if armed is not None else ()

    # -- assembling the method -----------------------------------------------

    def build_method(self) -> Method | None:
        """The panes, the form and the declarations as one `Method`.

        Built fresh on every use rather than kept and mutated, so that what a button
        sends is what the panes say at the moment it was pressed. Returns None when
        there are no panes, which is a window that has not found its boxes yet.
        """
        if not self.panes:
            return None
        results = {name: pane.result for name, pane in self.panes.items()}
        warnings: list[str] = []
        boxes: list[BoxMethod] = []
        for name, result in results.items():
            dc_bias, rf = self.declared.get(name, ((), ()))
            entry = result.box_method(
                self.ports.get(name, ""), dc_bias=dc_bias, rf=rf)
            # A pane is not a declaration. The launch scan gives every box it finds a
            # pane, so the rack's fourth box -- which no method on this instrument names
            # -- became a `BoxMethod` with five empty phases, and `send_phases` spent
            # forty round trips reading the state of a box nothing was about to be sent
            # to (found at the instrument, 2026-09-17; task 56). The method names the
            # boxes it has strings or analog declarations for; the others keep their
            # panes, and their state panels say "left as found" about everything, which
            # is exactly what is true of them.
            if _names_something(entry, result):
                boxes.append(entry)
            warnings += list(result.warnings)
            for line in result.unplaced:
                warnings.append(
                    f"{name} line {line.number}: {line.text!r} is not a command and is "
                    "not sent. Tag it with `# clockwork: <phase>` if it should be.")
        reset = tuple(step for name in results for step in results[name].reset)
        enable = self._enable()
        acquisition = Acquisition(
            frames=self.frames.value(),
            scans=self.scans.value(),
            accumulations=self.accumulations.value(),
            file_stem=self.stem.text().strip() or "clockwork",
            repetition_mode=self.repetition_mode.currentText(),
            keep_raw=self.keep_raw.isChecked(),
            enable=enable,
        )
        return Method(
            metadata=self.metadata,
            acquisition=acquisition,
            boxes=tuple(boxes),
            start=start_order(results),
            reset=reset,
            warnings=tuple(warnings),
        )

    def _pane_changed(self, *_: object) -> None:
        # The state panels are not re-marked here: they compare a reading with what the
        # boxes were last sent, which an edit to the panes does not change (E2), and the
        # line over the panes says whether the two agree.
        self._refresh_start_order()
        self._refresh_actions()

    def _refresh_start_order(self) -> None:
        method = self.build_method()
        if method is None:
            self.start_order.setText("")
            return
        lines = [f"{step.box:<12} {step.command}" for step in method.start]
        self.start_order.setText("\n".join(lines) or "(nothing classified as start)")

    def _refresh_actions(self) -> None:
        """Which buttons may be pressed, and why the greyed-out one is greyed out."""
        # A running queue is busy even in the instant between its submission and the
        # job's start being seen here: a second Start in that gap would be a second job.
        busy = self._job is not None or self.queue.running
        method = self.build_method()
        self._check_attached(method)
        problems = refusals(method) if method is not None else ["no boxes found yet"]
        notes = (list(method.warnings) + cautions(method)) if method is not None else []
        # The document's channel offset is sent to the console and stamped into the file
        # as one number, so a run without one cannot be prepared at all: `prepare_console`
        # refuses it by name. Said here, before the button, rather than by a job that
        # fails a second after it is pressed. Sending is not affected -- the boxes do not
        # need a document -- which is why it greys only the acquisitions.
        no_offset = ([] if self.instrument.vertical.offset_v is not None else [
            "the instrument document states no channel offset, and the offset is sent "
            "to the console and stamped into the file as one number. Pick a document "
            "with an `offset_v` in its [vertical] table, in Setup."])
        # Acquire is no longer greyed for "not armed" (E7): it sends what the boxes do
        # not hold first, by the owner's memory of what they were last sent, and on
        # boxes that hold the method its first file is the replicate.

        have_console = self.worker.console_alive
        # Another program owns the instrument (lab record, task 67): every hardware job
        # would be refused with the lock's sentence, so the buttons that queue one are
        # grey and the sentence stays in the status bar. Find boxes is left as the retry
        # -- the owner takes the lock on the next job once the holder has closed.
        locked = bool(self.worker.refused)
        self.lock_label.setText(
            f"{self.worker.refused[:1].upper()}{self.worker.refused[1:]}; then Find "
            "boxes." if locked else "")
        self.lock_label.setVisible(locked)
        self.find_button.setEnabled(not busy)
        self.send_button.setEnabled(not busy and not problems and not locked)
        blocking = problems + no_offset
        self.acquire_button.setEnabled(not busy and not blocking and not locked)
        # Enabled from the moment a run's raw file exists and not from the moment the
        # run ends (task 55), which is why `_live_run` is taken off `RunBegun`. The
        # tooltip is part of the same answer: the two cases open different files, and a
        # button whose wording did not change would be describing one of them wrongly.
        self.mainspring_button.setEnabled(any(self._last_run_paths[:2]))
        self.mainspring_button.setToolTip(
            MAINSPRING_LIVE_TIP if self._live_run_path() else MAINSPRING_TIP)
        self.log_button.setEnabled(bool(self._last_run_paths[2]))
        can_read = not busy and bool(self.worker.boxes)
        self.action_read_state.setEnabled(can_read)
        self.read_button.setEnabled(can_read)
        for pane in self.panes.values():
            pane.state.set_busy(busy or not self.worker.boxes)
        can_start = not busy and not have_console and not locked
        self.action_console.setEnabled(can_start)
        self.console_button.setEnabled(can_start)
        self.console_restart.setEnabled(not busy and not locked)
        self.queue_panel.set_busy(busy or locked)
        self.plan_panel.set_busy(busy or locked or self._plan_job is not None,
                                 running=isinstance(self._job, SeriesJob)
                                 and not self.worker.stopping)
        self.template_tab.set_blocking(
            no_offset + ([self.worker.refused] if locked else []), busy=busy)
        self.template_tab.set_stem(self.stem.text().strip())
        self._show_held_line(method)
        self._show_sent()
        self._refresh_hold()
        self._refresh_strip()

        text: list[str] = []
        if blocking:
            text.append("Acquire is not available:")
            text += [f"  • {line}" for line in blocking]
        if notes:
            text.append("Worth knowing:")
            text += [f"  • {line}" for line in notes]
        self.problems.setText("\n".join(text))

    def _show_held_line(self, method: Method | None) -> None:
        """The one line over the panes: whether they are what the boxes hold (E2)."""
        armed = self.worker.armed
        if armed is None:
            text = ("Nothing is known to be on the boxes since clockwork serve started: "
                    "Acquire and Send send every phase first.")
        elif method is not None and matches_wire(armed.fingerprint, method):
            text = (f"The panes are what the boxes hold, as last sent ({armed.method}): "
                    "Acquire sends nothing first, and its first file is a replicate.")
        else:
            text = (f"The boxes hold {armed.method or 'another method'}, not the panes: "
                    "Acquire and Send send the panes first, setup only where it differs.")
        if text != self.held_line.text():
            self.held_line.setText(text)

    def _sent_box(self, name: str) -> BoxMethod | None:
        """What the boxes were last sent for box `name`, whoever sent it, as a
        `BoxMethod` the state panel marks a reading against (E2), or None where nothing
        sent to it is remembered. A setup phase the send left out is empty here."""
        armed = self.worker.armed
        if armed is None:
            return None
        for entry in armed.fingerprint:
            if entry[0] != name:
                continue
            return BoxMethod(
                name=name, port=self.ports.get(name, ""),
                setup=tuple(entry[1] or ()), load=tuple(entry[2]), arm=tuple(entry[3]),
                dc_bias=tuple(tuple(pair) for pair in entry[4]), rf=tuple(entry[5]))
        return None

    def _show_sent(self) -> None:
        """Re-mark the state panels where what the boxes were last sent has moved."""
        armed = self.worker.armed
        fingerprint = tuple(armed.fingerprint) if armed is not None else ()
        if fingerprint == self._sent_shown:
            return
        self._sent_shown = fingerprint
        for name, pane in self.panes.items():
            pane.show_method(self._sent_box(name))

    def _refresh_hold(self) -> None:
        """The person's controls over the job in flight: the two pauses while a series
        runs, each greyed with the owner's own sentence where it cannot be asked, Resume
        while a pause stands, and the pause itself said wherever a run is said."""
        job = self._job
        series = isinstance(job, SeriesJob)
        hold = self.worker.hold
        if hold is not None and job is None:
            hold = None  # the job's end has been seen here; the status is a poll behind
        why = {POINT: "", REPETITION: ""}
        if series:
            followed = self.followed
            number = (followed.current + 1 if followed is not None
                      and followed.current is not None else 0)
            method = job.points[number - 1].method if 0 < number <= len(job.points) else None
            for grain in why:
                why[grain] = pause_refusal(job, grain, number=number, method=method,
                                           stopping=self.worker.stopping, hold=hold)
        self.run_panel.show_hold(hold, series=series, running=job is not None,
                                 stopping=self.worker.stopping, point=why[POINT],
                                 repetition=why[REPETITION])
        if hold is not None and hold.reached and hold.text != self._held_said:
            self.statusBar().showMessage(f"{job.label}: {hold.text}")
        self._held_said = hold.text if hold is not None and hold.reached else ""

    # -- methods on disk -----------------------------------------------------

    def open_method(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open method", os.path.dirname(self.method_path) or "",
            "Method documents (*.toml);;All files (*)")
        if path:
            self._load_method(path)

    def _load_method(self, path: str, *, quiet: bool = False) -> bool:
        """Put a document into the panes and the form, and say whether it opened.

        `quiet` sends the failure to the run log instead of a message box. It existed for
        the queue's rows, which no longer come through the panes (lab record, task 168),
        and is kept for any caller that must not raise a dialog nobody is there to close.
        """
        try:
            method = method_module.load(path)
        except (OSError, method_module.MethodError) as exc:
            if quiet:
                self.run_panel.say(
                    f"{os.path.basename(path)} could not be opened: {exc}", warn=True)
            else:
                self._complain("That method could not be opened", str(exc))
            return False
        # Detached while the panes fill, one by one: a half-filled set is not the render,
        # and judging it would detach the method before it had finished opening.
        self._attach(None)
        self.method_path = path
        self.settings.method_path = path
        self.metadata = method.metadata
        self.declared = {entry.name: (tuple(entry.dc_bias), tuple(entry.rf))
                         for entry in method.boxes}
        for entry in method.boxes:
            self.ports.setdefault(entry.name, entry.port)
        self._ensure_panes([entry.name for entry in method.boxes])
        for entry in method.boxes:
            self.panes[entry.name].set_text(
                render_pane(entry, method.start, method.reset))
        acquisition = method.acquisition
        self.frames.setValue(acquisition.frames)
        self.scans.setValue(acquisition.scans)
        self.accumulations.setValue(acquisition.accumulations)
        index = self.repetition_mode.findText(acquisition.repetition_mode)
        if index >= 0:
            self.repetition_mode.setCurrentIndex(index)
        self.keep_raw.setChecked(acquisition.keep_raw)
        self._set_enable(acquisition.enable)
        for message in method.warnings:
            self.run_panel.say(f"{os.path.basename(path)}: {message}", warn=True)
        if method.rendered_from is not None:
            rendered, why = templates.reproduce(method)
            if rendered is None:
                self.run_panel.say(
                    f"{os.path.basename(path)} was rendered from a template, but {why}. "
                    "It is opened as a hand-written method, and its runs will not stamp "
                    "the template or its knobs.", warn=True)
            self._attach(rendered)
        self.statusBar().showMessage(f"opened {os.path.basename(path)}")
        if self.fake:
            # The stand-in rack is built from the method, so opening one is what gives
            # `--fake` its boxes. Against hardware the ports are the authority and a
            # method is only a hint, so nothing is re-discovered here.
            self.find_boxes()
        self._pane_changed()
        return True

    def _attach(self, rendered: Rendered | None) -> None:
        """Attach the panes to a render, or detach them silently, and say which above."""
        self.rendered = rendered
        self.provenance.setText(
            templates.describe(rendered).capitalize() if rendered is not None else "")
        self.provenance.setVisible(rendered is not None)

    def _check_attached(self, method: Method | None) -> None:
        """Detach, saying so once, where the panes no longer make the rendered run.

        Every string, sequence and acquisition setting, and not the name, file stem or
        ports, which the window assigns (`same_run`). One line in the run log rather than
        a dialog: editing a rendered method is an ordinary thing to do, and what a trainee
        needs is to know that its runs now stamp as hand-written.
        """
        rendered = self.rendered
        if rendered is None or method is None or same_run(rendered.method, method):
            return
        self._attach(None)
        name = os.path.basename(self.method_path) or "this method"
        self.run_panel.say(
            f"{name} is now a hand-written method: the panes or settings no longer match "
            f"what its template renders at those knobs, so its runs will not stamp the "
            "template or its knobs. Reopen the file to attach it again.", warn=True)

    def save_method(self, ask: bool = False) -> None:
        method = self.build_method()
        if method is None:
            self._complain("There is nothing to save",
                           "Find the boxes first, or open a method.")
            return
        path = self.method_path
        if ask or not path:
            path, _ = QFileDialog.getSaveFileName(
                self, "Save method", path or "", "Method documents (*.toml)")
            if not path:
                return
        # The name in the document follows the file it is saved as, unless the method
        # was opened from a document that already named itself: a trainee saving a new
        # method should not have to find a name field to fill in.
        if self.metadata.name in ("", "untitled"):
            self.metadata = replace(
                self.metadata, name=os.path.splitext(os.path.basename(path))[0])
            method = replace(method, metadata=self.metadata)
        # An attached method keeps its `[rendered]` table, so the file still says what
        # made it; a detached one is a hand-written method and is saved as one.
        self._check_attached(method)
        if self.rendered is not None:
            method = replace(method,
                             rendered_from=templates.attached(self.rendered).rendered_from)
        try:
            method_module.save(method, path)
        except (OSError, method_module.MethodError) as exc:
            self._complain("That method could not be saved", str(exc))
            return
        self.method_path = path
        self.settings.method_path = path
        self.statusBar().showMessage(f"saved {os.path.basename(path)}")

    def open_library(self) -> None:
        """The method browser: open a method in the Method tab or a template in the
        Template tab, or compare two methods.

        The directory offered the first time is the instrument's library,
        `instrument_dir("golden")`, where it resolves and nothing has been picked yet --
        a one-time suggestion, not a stored default (`Settings.library_dir`), since a
        public clone has no instrument beside it and the instrument's library is lab
        material this module never names by path.
        """
        directory = self._library()
        dialog = LibraryDialog(directory, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if dialog.directory() != self.settings.library_dir:
            self.settings.library_dir = dialog.directory()
            self.template_tab.set_library(self._library())
        if accepted and dialog.chosen_path:
            self._load_method(dialog.chosen_path)
            self.tabs.setCurrentWidget(self.method_tab)
        elif accepted and dialog.chosen_template:
            self.template_tab.choose(dialog.chosen_template)
            self.tabs.setCurrentWidget(self.template_tab)

    def _library(self) -> str:
        """The method library: the window's setting, else the instrument's own."""
        return self.settings.library_dir or clockwork.instrument_dir("golden") or ""

    def _open_rendered(self, path: str) -> None:
        """The Template tab wrote `path`: open it in the Method tab, attached (E4)."""
        if self._load_method(path):
            self.tabs.setCurrentWidget(self.method_tab)

    # -- the instrument document ---------------------------------------------

    def _pick_instrument(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open instrument document",
            os.path.dirname(self.instrument_path) or "",
            "Instrument documents (*.toml);;All files (*)")
        if path:
            self._load_instrument(path)

    def _refresh_actions_if_built(self) -> None:
        if hasattr(self, "run_panel"):
            self._refresh_actions()

    def _load_instrument(self, path: str, *, remember: bool = True) -> None:
        try:
            document = instrument_module.load(path)
        except (OSError, instrument_module.InstrumentError) as exc:
            self._complain("That instrument document could not be opened", str(exc))
            return
        self.instrument = document
        self.instrument_path = path
        if remember:
            self.settings.instrument_path = path
        self.instrument_field.setText(os.path.basename(path))
        self.instrument_field.setToolTip(path)
        vertical = document.vertical
        mass_axis = ("this run will have a mass axis"
                     if document.calibration.usable
                     else "this run will have NO mass axis: the document states no "
                          "usable calibration, so mainspring will show flight time")
        parts = [mass_axis]
        window = ", ".join(part for part in (
            f"full scale {vertical.full_scale_v} V"
            if vertical.full_scale_v is not None else "",
            f"offset {vertical.offset_v} V" if vertical.offset_v is not None else "",
            "inverted" if vertical.inverted else "not inverted",
        ) if part)
        if window:
            parts.append(window)
        self.instrument_status.setText("\n".join(parts))
        self._refresh_actions_if_built()
        # The console holds what it was last configured with, so a changed document
        # has to reach it before the next run rather than at the next restart.

    # -- the sitting (Setup) ---------------------------------------------------

    def _joined(self) -> bool:
        """Whether this window joined a daemon it did not start, whose sitting is then
        shown read-only (E6)."""
        worker = self.worker
        return (isinstance(worker, RemoteWorker) and worker.hello is not None
                and worker.spawned is None)

    def _show_sitting(self) -> None:
        """The sitting's documents in Setup: the daemon's, read-only, where this window
        joined one it did not start; otherwise the ones this window starts it with, which
        take effect at its next start (the window revision's E6)."""
        joined = self._joined()
        hello = getattr(self.worker, "hello", None)
        if joined:
            limits = str(getattr(hello, "limits", "") or "")
            project = str(getattr(hello, "project_limits", "") or "")
            self.limits_field.setText(limits or "none")
            self.project_limits_field.setText(project or "none")
            self.sitting_status.setText(
                f"The sitting of clockwork serve (pid {getattr(hello, 'pid', '?')}), which "
                "this window joined and did not start: shown here, not changed here. Every "
                "client is shown and held to it; it changes when clockwork serve is "
                "started again.")
        else:
            self.limits_field.setText(self.settings.limits_path)
            self.project_limits_field.setText(self.settings.project_limits_path)
            self.sitting_status.setText(
                "The simulated instrument's sitting, in this window's own process."
                if self.fake else
                f"No clockwork serve is running. Find boxes, Send, Acquire or a Start "
                f"starts one, given these documents and the output folder "
                f"{self._next_output() or 'of its own default'}; nothing has been started "
                "until then." if self._serve_absent() else
                "Given to clockwork serve when this window starts it; a change takes "
                "effect at its next start.")
        for button in (self.browse_instrument, self.browse_limits,
                       self.browse_project_limits):
            button.setEnabled(not joined)
        # The output folder is the serve's for every client, a window included, as the
        # MCP server and the CLI already took it (lab record, task 172): shown read-only,
        # and in a window that started the serve, a folder chosen for its next start.
        serving = self._serve_output
        if serving:
            self.output_dir.setText(serving)
            saved = self.settings.output_dir
            if joined:
                self.sitting_status.setText(
                    self.sitting_status.text() + f" Its output folder, {serving}, is "
                    "where this window's runs go and where its Day tab and a plan's "
                    "check are read.")
            elif saved and not _same_folder(saved, serving):
                self.sitting_status.setText(
                    self.sitting_status.text() + f" This window's runs go to clockwork "
                    f"serve's output folder, {serving}, until then; {saved} takes "
                    "effect at its next start.")
        self.output_dir.setReadOnly(bool(serving))
        self.browse_output.setEnabled(not (joined and serving))
        self.output_dir.setToolTip(
            "clockwork serve's output folder, where every client's runs go: changed by "
            "starting clockwork serve again" if serving else "")
        self.limits_field.setToolTip(self.limits_field.text())
        self.project_limits_field.setToolTip(self.project_limits_field.text())

    def _pick_limits(self, *, project: bool) -> None:
        what = "project limits" if project else "standing limits"
        current = (self.settings.project_limits_path if project
                   else self.settings.limits_path)
        path, _ = QFileDialog.getOpenFileName(
            self, f"The {what}", os.path.dirname(current or self.instrument_path) or "",
            "Limits documents (*.toml);;All files (*)")
        if not path:
            return
        if project:
            self.settings.project_limits_path = path
        else:
            self.settings.limits_path = path
        self._show_sitting()
        self.run_panel.say(f"the {what} are {path}, from the next start of clockwork serve")

    def _refresh_limits(self) -> None:
        """Hand the Template tab the limits the sitting carries, the agent's range each
        knob is shown against (R10), or why there are none."""
        try:
            limits = self._sitting_limits()
        except (OSError, ValueError) as exc:
            self.template_tab.set_limits(None, f"the sitting's limits did not load ({exc})")
            return
        self.template_tab.set_limits(
            limits, "" if limits is not None else "clockwork serve carries no limits")

    def _show_boxes(self) -> None:
        """The status bar's boxes: which answered, on which port."""
        answered = [f"{name} {port}" for name, (port, _, ok) in self._described.items()
                    if ok]
        silent = [name for name, (_, _, ok) in self._described.items() if not ok]
        text = (", ".join(answered) if answered else "no boxes answered") + (
            f"; {', '.join(silent)} off or absent" if silent else "")
        self.boxes_label.setText(text)

    def _show_daemon(self) -> None:
        """The status bar's daemon: which owner holds the instrument."""
        if self.fake:
            self.daemon_label.setText("in process, --fake")
            return
        hello = getattr(self.worker, "hello", None)
        if hello is None:
            self.daemon_label.setText(
                "no clockwork serve running: Find boxes, Send or Acquire starts one"
                if self._serve_absent() else "no clockwork serve yet")
            return
        whose = "started by this window" if not self._joined() else "joined"
        self.daemon_label.setText(f"clockwork serve pid {getattr(hello, 'pid', '?')}, "
                                  f"{whose}")

    # -- the run strip and the Day tab -------------------------------------------

    def _refresh_strip(self) -> None:
        """The run strip's line: the job in flight, whose, where, minutes left (E1)."""
        job = self._job
        if job is None:
            text = "Nothing is running."
        else:
            origin = self.worker.origin(job)
            person = getattr(job, "at_window", "")
            whose = (f"from {origin}" if origin
                     else "this window" + (f", {person} at the window" if person else ""))
            parts = [job.label, whose]
            where = getattr(job, "directory", "")
            if where:
                parts.append(f"into {where}")
            followed = self.followed
            if isinstance(job, SeriesJob) and followed is not None and not followed.ended:
                left = followed.minutes_left
                parts.append(f"about {left:.0f} minutes left" if left is not None
                             else "minutes left not known")
            text = "; ".join(parts)
        if text != self.run_panel.job_line.text():
            self.run_panel.job_line.setText(text)

    def refresh_day(self) -> None:
        """Read the day folder again for the Day tab, with the daemon's live jobs."""
        live: list[tuple[str, str, str]] = []
        job = self._job
        if job is not None:
            live.append(("running", job.label, self.worker.origin(job) or "this window"))
        worker = self.worker
        if isinstance(worker, RemoteWorker):
            live += [(state, handle.label, handle.origin or "clockwork serve")
                     for state, handle in worker.others() if state != "running"]
        self.day_panel.show_day(self._directory(), live)

    def _tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.day_panel:
            self.refresh_day()

    # -- the console ---------------------------------------------------------

    def start_console(self) -> None:
        if not self._serve_ready():
            return
        command = "" if self.fake else (
            self.settings.console_path or find_console() or "")
        if not self.fake and not command:
            self._complain(
                "No acquisition console is configured",
                "Point the console path at the executable the installer put beside "
                "clockwork, or set $CLOCKWORK_CONSOLE. clockwork starts it and stops "
                "it; a trainee never sees its window.")
            return
        self.worker.submit(StartConsole(command=command))

    def restart_console(self) -> None:
        if not self._serve_ready():
            return
        self.worker.submit(RestartConsole())

    def console_settings(self) -> None:
        reader = self.worker.config_reader()
        if reader is None:
            self._complain(
                "There is no config.txt to edit",
                "The simulated console reads no configuration file; --fake exercises "
                "the status bar and the restart path and nothing below them." if self.fake
                else "No acquisition console is configured on this PC, so there is no "
                     "config.txt beside one to edit.")
            return
        dialog = ConsoleSettings(reader, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if not values:
            self.statusBar().showMessage("nothing changed")
            return
        if not self._serve_ready():
            return
        self.worker.submit(RestartConsole(values=values))

    # -- the instrument ------------------------------------------------------

    def find_boxes(self) -> None:
        if not self._serve_ready():
            return
        self.worker.submit(Discover(method=self.build_method()))

    def read_state(self, box: str = "") -> None:
        """A whole-state reading of one box, or of every box the rack answered for.

        A panel's own button names its box; the Run menu's entry does not and reads the
        whole rack. Queued rather than called: forty round trips on the UI thread would
        freeze the window for the seconds they cost.
        """
        if not self._serve_ready():
            return
        self.worker.submit(ReadState(
            label=f"reading {box}" if box else "reading the boxes",
            names=(box,) if box else ()))

    def send(self, *, setup: bool = False) -> Send | None:
        """Send the panes: what the boxes do not hold, `setup` only where it differs
        from what they were last sent, and nothing acquired (E7). `setup`, or the "send
        setup again" tick, sends it regardless. The job is handed back for a test."""
        method = self.build_method()
        if method is None or not self._serve_ready():
            return None
        again = setup or self.setup_again.isChecked()
        job = Send(
            label="sending the panes", method=method, setup=again, as_needed=True,
            conditions=self.conditions.toPlainText(),
            directory=self._directory(), stem=self.stem.text().strip(),
            method_path=self.method_path,
            instrument=self.instrument, instrument_path=self.instrument_path,
        )
        self.setup_again.setChecked(False)
        self.worker.submit(job)
        return job

    def acquire(self, *, setup: bool = False) -> Acquire | None:
        """Send what the boxes do not hold of the panes, then acquire (E7), and hand the
        job back. On boxes that hold the method nothing is sent and the first file is
        the replicate; the owner decides which, by its memory of the last send.

        Every one is a request of its own with a run record, begun by the owner when the
        job starts (`source="window"`, lab record, task 165): the purpose line is its
        request and "at the window" names who started it, which a run needs before it
        starts."""
        method = self.build_method()
        if method is None:
            return None
        # Judged on the method this job carries, not on the last refresh: the owner renders
        # the template again and refuses a render that is not of this method, so a stale
        # attachment would cost a trainee a run rather than a stamp.
        self._check_attached(method)
        return self._submit_acquire(
            method, label="acquiring", method_path=self.method_path,
            replicates=self.replicates.value(), conditions=self.conditions.toPlainText(),
            rendered=self.rendered, setup=setup)

    def acquire_template(self) -> Acquire | None:
        """The Template tab's Acquire: its render, with no file on disk (E4). The job
        carries the template text, knob values and labels; the owner renders it again,
        stamps it, and its record keeps the template's hash, the values, the rendered
        method's hash and where a value lies outside the agent's limits (R10)."""
        tab = self.template_tab
        rendered = tab.rendered
        if rendered is None:
            return None
        method = replace(rendered.method, boxes=tuple(
            replace(entry, port=self.ports.get(entry.name) or entry.port)
            for entry in rendered.method.boxes))
        outside = tab.outside()
        if outside:
            self.run_panel.say(
                f"{', '.join(outside)} outside the agent's limits: run as set, and the "
                "run record says so", warn=True)
        name = rendered.template.name if rendered.template is not None else ""
        return self._submit_acquire(
            method, label=f"acquiring {name or 'the template'} as set in the Template tab",
            method_path="", replicates=tab.replicates.value(),
            conditions=tab.conditions.toPlainText(), rendered=rendered)

    def _submit_acquire(self, method: Method, *, label: str, method_path: str,
                        replicates: int, conditions: str, rendered: Rendered | None,
                        setup: bool = False) -> Acquire | None:
        if not self._serve_ready():
            return None
        person = self._starter()
        if not person:
            return None
        if not self.worker.console_alive:
            self.run_panel.say("starting the acquisition console first")
            self.start_console()
        self.settings.conditions = self.conditions.toPlainText()
        self.settings.replicates = self.replicates.value()
        stem = self.stem.text().strip()
        if stem:
            method = replace(method, acquisition=replace(method.acquisition,
                                                         file_stem=stem))
        job = Acquire(
            label=label, method=method, instrument=self.instrument,
            instrument_path=self.instrument_path, method_path=method_path,
            directory=self._directory(), stem=stem, initials=self.initials.text(),
            replicates=replicates, conditions=conditions,
            template=rendered.template_text if rendered is not None else "",
            knobs=dict(rendered.knobs) if rendered is not None else {},
            labels=dict(rendered.labels) if rendered is not None else {},
            request=self.run_panel.purpose.text().strip(), source="window",
            at_window=person, send=True, setup=setup or self.setup_again.isChecked(),
        )
        self.setup_again.setChecked(False)
        self.worker.submit(job)
        return job

    def _at_window(self) -> str:
        """The person at the window, cleaned and remembered, or empty for nobody."""
        person = clean_initials(self.run_panel.at_window.text())
        self.run_panel.at_window.setText(person)
        if person:
            self.settings.at_window = person
        return person

    def _starter(self) -> str:
        """The person at the window, for a Start: required, and asked about once per
        launch where the field holds a value nobody typed since the window opened, so
        that a restored value never reaches a run record unseen (A2 as revised; lab
        record, task 172). Empty, with the reason said, for no run."""
        person = self._at_window()
        if person and not self._at_window_confirmed:
            answer = self._ask_at_window(person)
            if answer is None:
                self.run_panel.say("nothing was started: say who is at the window first",
                                   warn=True)
                self.run_panel.at_window.setFocus()
                return ""
            self.run_panel.at_window.setText(clean_initials(answer))
            person = self._at_window()
            if person:
                self._at_window_confirmed = True
                self._show_at_window()
        if not person:
            self.run_panel.say("type your initials in \"At the window\" first: every run "
                               "you start records who started it", warn=True)
            self.run_panel.at_window.setFocus()
        return person

    def _ask_at_window(self, person: str) -> str | None:
        """Whether `person` is at the window: the initials to record, or None for
        cancel. A method of its own so that a test can answer it."""
        text, ok = QInputDialog.getText(
            self, "At the window",
            f"At the window: {person}. Is that you?\n\nThe initials here go into the "
            "record of every run you start and beside every pause, stop and note. "
            "Correct them if they are not yours.",
            QLineEdit.EchoMode.Normal, person)
        return text if ok else None

    def _at_window_typed(self, _text: str) -> None:
        """Typing in the field is the person saying who they are."""
        self._at_window_confirmed = True
        self._show_at_window()

    def _show_at_window(self) -> None:
        """The field in italics, with a tooltip saying why, while its value is one
        nobody has typed or confirmed since the window opened."""
        field = self.run_panel.at_window
        if not hasattr(self, "_at_window_tip"):
            self._at_window_tip = field.toolTip()
        pending = bool(field.text().strip()) and not self._at_window_confirmed
        field.setStyleSheet("font-style: italic;" if pending else "")
        field.setToolTip(self._at_window_tip + (
            "\n\nNot yet confirmed: a value from an earlier session or from the file "
            "initials. The first run started from this window asks whether it is you."
            if pending else ""))

    def _signed(self, what: str) -> str:
        """The person at the window, for an act on a running job, or empty with the
        reason said: every pause, resume, stop and note is written under initials (the
        window revision's A2)."""
        person = self._at_window()
        if not person:
            self.run_panel.say(f"type your initials in \"At the window\" first: {what} "
                               "is recorded with who made it", warn=True)
            self.run_panel.at_window.setFocus()
        return person

    def pause(self, grain: str) -> None:
        """Hold the series running, this window's or another client's, after its point
        or its repetition in flight (R6). The owner decides whether it can be held and
        says why not; what comes back is the job's own `PauseAsked` and `Held`."""
        person = self._signed("a pause")
        if person:
            self.worker.pause(grain, person)

    def resume(self) -> None:
        """Lift the pause on the series running (R7: only a person at a window does)."""
        person = self._signed("a resume")
        if person:
            self.worker.resume(person)

    def keep_note(self, text: str) -> None:
        """Keep a note in the running job's record, or the day record with nothing
        running (C1, C2). The field is emptied once the note has been sent to be kept."""
        text = text.strip()
        if not text:
            return
        person = self._signed("a note")
        if person:
            self.worker.note(text, person)
            self.run_panel.note.clear()

    def stop(self, *, signed: bool = True) -> None:
        """End the run in flight, under the initials at the window. `signed` false is
        the close question's stop, which the person has just confirmed and which is not
        held up for want of initials; a Stop button's is."""
        person = self._signed("a stop") if signed else self._at_window()
        if signed and not person:
            return
        hold = self.worker.hold
        self.worker.request_stop("stopped at the window", by=person)
        self.run_panel.say("stopping the paused series where it rests"
                           if hold is not None and hold.reached
                           else "stopping after this repetition and its fold", warn=True)
        if self.queue.running and self._job is self._queue_job:
            # The owner ends the queue as it ends any series: the row in flight closes
            # its files, and the rows after it come back skipped, for a new queue.
            self.run_panel.say("the queue stops with this row; the rows after it are "
                               "skipped, and Start runs them as a new queue", warn=True)
        self._refresh_actions()

    # -- the queue -----------------------------------------------------------

    def queue_add_methods(self) -> None:
        """Rows for one or more documents on disk, with the fields as they stand."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add methods to the queue",
            os.path.dirname(self.method_path) or "",
            "Method documents (*.toml);;All files (*)")
        for path in paths:
            self.queue.add(self._queue_row(path))
        if paths:
            self.tabs.setCurrentWidget(self.queue_tab)
            self.queue_panel.refresh()
            self._refresh_actions()

    def queue_add_open_method(self) -> None:
        """A row for the method the panes hold, which has to have been saved first.

        A row names a document and reads it when it starts, so a pane edited since the
        last Save is not what the row would run. That is said rather than prevented:
        a trainee who queued the file on purpose and is still typing in the panes is
        doing something reasonable, and the warning is what makes it deliberate.
        """
        if not self.method_path:
            self._complain(
                "This method has not been saved",
                "A queue row names a document on disk and reads it when the row "
                "starts, so there has to be a file. Save the method first.")
            return
        method = self.build_method()
        try:
            saved = method_module.load(self.method_path)
        except (OSError, method_module.MethodError) as exc:
            self._complain("That method could not be re-read", str(exc))
            return
        if method is not None and wire_fingerprint(saved) != wire_fingerprint(method):
            self.run_panel.say(
                f"{os.path.basename(self.method_path)} was queued, but the panes have "
                "changed since it was saved: the row will run what the file says, not "
                "what is on screen. Save the method again to queue the edit.", warn=True)
        self.queue.add(self._queue_row(self.method_path))
        self.tabs.setCurrentWidget(self.queue_tab)
        self.queue_panel.refresh()
        self._refresh_actions()

    def _queue_row(self, path: str) -> QueueRow:
        return QueueRow(method_path=path,
                        conditions=self.conditions.toPlainText().strip(),
                        replicates=self.replicates.value())

    def randomize_queue(self) -> None:
        """Turn the waiting rows into shuffled passes, and log the seed and the order.

        The rules are `RunQueue.randomize`'s. What is the window's is the asking: how
        many passes, and -- on a queue that has passes already -- whether to replace
        them, since the new ones are built from pass 1 and an edit made only to a later
        pass's row goes with them (lab record, task 86).
        """
        if self.queue.running:
            return
        template = self.queue.template()
        if not template:
            self.run_panel.say("the queue has no waiting rows to randomize", warn=True)
            return
        existing = self.queue.passes
        if existing and not self._confirm(
                f"Replace the {existing} passes?",
                f"The new passes are built from pass 1's {len(template)} row(s). Edits "
                "made only to rows of later passes will be lost; rows that have run stay "
                "where they are."):
            return
        chosen = self._ask_randomize(len(template), existing)
        if chosen is None:
            return
        passes, reshuffle = chosen
        done = self.queue.randomize(passes, reshuffle)
        self.run_panel.say(done.line)
        self.queue_panel.tree.clearSelection()
        self.queue_panel.refresh()
        self._refresh_actions()

    def queue_guard(self) -> None:
        """Arm, change or disarm the owner's beam guard, under the initials at the window.

        The owner's since lab record task 168 (R9, B8): every acquisition watches it,
        whichever client started it, and it is armed on whatever is running as readily
        as with nothing running; each change is recorded with who made it, in the
        running job's record or the day record. Never the queue file's: the floor is a
        reading of the day's beam."""
        current = self.worker.guarding
        answer = self._ask_guard(current.guard if current is not None else None)
        if answer is None:
            return
        guard = answer[0]
        if guard == (current.guard if current is not None else None):
            return
        person = self._signed("the beam guard")
        if not person:
            return
        self.worker.guard(guard, person)
        self.run_panel.say(f"the beam guard is armed: {guard.text}" if guard is not None
                           else "the beam guard is off")

    def _show_guard(self) -> None:
        """The guard button says what the owner holds, whoever armed it."""
        self.run_panel.show_guard(self.worker.guarding)

    def _ask_guard(self, guard: BeamGuard | None) -> tuple[BeamGuard | None] | None:
        """The guard dialog, a method of its own so that a test can answer it: the
        guard chosen (None to disarm) in a one-tuple, or None for Cancel."""
        dialog = BeamGuardDialog(guard, self._guard_run_path, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return (dialog.chosen(),)

    def _guard_run_path(self) -> str:
        """The file dialog for the run a floor is taken from."""
        path, _ = QFileDialog.getOpenFileName(
            self, "The run to take the floor from", self._directory(),
            "Per-repetition files (*.uimf);;All files (*)")
        return path

    def _ask_randomize(self, rows: int, passes: int) -> tuple[int, bool] | None:
        """The passes dialog, a method of its own so that a test can answer it."""
        dialog = RandomizeDialog(rows, passes, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.chosen()

    def save_queue(self) -> None:
        """Write the rows, their passes and the seed to a queue file."""
        if not self.queue.rows:
            return
        path = self._queue_file_path(save=True)
        if not path:
            return
        if not path.endswith(".toml"):
            path += runqueue.QUEUE_SUFFIX
        try:
            runqueue.save(self.queue, path)
            self.queue.path = os.path.abspath(path)
        except OSError as exc:
            self._complain("The queue could not be saved", str(exc))
            return
        self.run_panel.say(f"the queue is saved to {path}")

    def open_queue(self) -> None:
        """Replace the queue with a saved one, every row waiting.

        Refused while a series runs, as Randomize is: the rows being replaced include
        the one in flight.
        """
        if self.queue.running:
            return
        path = self._queue_file_path(save=False)
        if not path:
            return
        try:
            opened = runqueue.load(path)
        except (OSError, runqueue.QueueFileError) as exc:
            self._complain("That queue could not be opened", str(exc))
            return
        if self.queue.rows and not self._confirm(
                "Replace the queue?",
                f"The {len(self.queue.rows)} row(s) in the queue now are replaced by "
                f"the {len(opened.rows)} in {os.path.basename(path)}. What the rows that "
                "ran left is in the run log and their files."):
            return
        self.queue.rows = opened.rows
        self.queue.seed = opened.seed
        self.queue.path = opened.path
        self.queue.follows = ""
        self.queue.started = []
        if opened.ignored:
            self.run_panel.say(
                f"{opened.ignored} row(s) of {os.path.basename(path)} carry the \"setup\" "
                "and \"go on if it fails\" values an earlier clockwork wrote; both are "
                "ignored now: every queue sends setup only where it differs from what the "
                "boxes were last sent, and a failed row ends the queue", warn=True)
        missing = sum(1 for row in opened.rows if not os.path.isfile(row.method_path))
        self.run_panel.say(
            f"the queue is opened from {path}: {len(opened.rows)} row(s)"
            + (f", seed {opened.seed}" if opened.seed is not None else "")
            + (f"; {missing} method(s) not found, which fail when they start"
               if missing else ""), warn=bool(missing))
        self.tabs.setCurrentWidget(self.queue_tab)
        self.queue_tabs.setCurrentWidget(self.queue_panel)
        self.queue_panel.tree.clearSelection()
        self.queue_panel.refresh()
        self._refresh_actions()

    def _queue_file_path(self, save: bool) -> str:
        """The file dialog for a queue file, a method of its own so a test can answer."""
        directory = os.path.dirname(self.method_path) or self.settings.output_dir or ""
        pattern = f"Queue files (*{runqueue.QUEUE_SUFFIX});;All files (*)"
        if save:
            path, _ = QFileDialog.getSaveFileName(
                self, "Save the run queue", directory, pattern)
        else:
            path, _ = QFileDialog.getOpenFileName(
                self, "Open a run queue", directory, pattern)
        return path

    def _confirm(self, title: str, detail: str) -> bool:
        """A yes-or-cancel question, a method of its own so that a test can answer it."""
        answer = QMessageBox.question(
            self, title, detail,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Yes

    def start_queue(self) -> SeriesJob | None:
        """Hand every waiting row to the owner as one series job (F1, F2; task 168).

        Every row's document is read and hashed now and the job carries what was read,
        so what runs is what the files said at Start, and the queue runs on if this
        window closes. A row whose document will not open, or that the acquisition loop
        would refuse, keeps the queue from starting and says why on the row: a snapshot
        with a hole in it is not the queue the table shows."""
        if (self.queue.running or self._job is not None or self._plan_job is not None):
            return None
        if not self._serve_ready():
            return None
        person = self._starter()
        if not person:
            return None
        if not clean_initials(self.initials.text()):
            self.run_panel.say("set the file initials first: they name the queue's files",
                               warn=True)
            return None
        rows = self.queue.begin()
        self.queue_panel.refresh()
        if not rows:
            self.run_panel.say("the queue has no waiting rows", warn=True)
            self._refresh_actions()
            return None
        points: list[PlannedPoint] = []
        problems: list[str] = []
        for number, row in enumerate(rows, start=1):
            try:
                points.append(self._row_point(number, row))
            except (OSError, UnicodeDecodeError, method_module.MethodError,
                    ValueError) as exc:
                row.problem = f"not started: {exc}"
                problems.append(f"row {self.queue.rows.index(row) + 1} ({row.name}): {exc}")
        if problems:
            self.queue_panel.refresh()
            self.run_panel.say("the queue did not start, because Start reads every row "
                               "first: " + "; ".join(problems), warn=True)
            return None
        for row in rows:
            row.problem = ""
        if not self.worker.console_alive:
            self.run_panel.say("starting the acquisition console first")
            self.start_console()
        snapshot = RunQueue(rows)
        snapshot.seed = self.queue.seed
        name = (os.path.basename(self.queue.path) if self.queue.path
                else f"{len(rows)} row(s)")
        seed = self.queue.stamped_seed
        job = SeriesJob(
            label=f"acquiring the queue {name}", points=tuple(points), seed=seed,
            directory=self._directory(), initials=clean_initials(self.initials.text()),
            request=self.run_panel.purpose.text().strip(), instrument=self.instrument,
            instrument_path=self.instrument_path, source="window", at_window=person,
            queue=runqueue.dumps(snapshot, os.path.dirname(self.queue.path)
                                 if self.queue.path else ""),
            queue_path=self.queue.path, follows=self.queue.follows)
        self.queue.start(rows)
        self.queue_panel.refresh()
        files = sum(point.replicates for point in points)
        self.run_panel.say(
            f"the queue starts in clockwork serve: {len(points)} row(s), {files} file(s), "
            + (f"seed {seed}" if seed is not None else "in table order")
            + (f"; it continues request {self.queue.follows}" if self.queue.follows
               else ""))
        guarding = self.worker.guarding
        self.run_panel.say(f"the beam guard is {guarding.text}" if guarding is not None
                           else "the beam guard is off")
        self._queue_job = job
        self.worker.submit(job)
        self._refresh_actions()
        return job

    def _row_point(self, number: int, row: QueueRow) -> PlannedPoint:
        """One row as a point of the queue's series job: its document read now, hashed
        as its text stands, its boxes on the ports this window knows them by, and for a
        rendered document its template, so its files stamp as rendered (task 168)."""
        with open(row.method_path, encoding="utf-8") as handle:
            text = handle.read()
        method = method_module.loads(text)
        method = replace(method, boxes=tuple(
            replace(entry, port=self.ports.get(entry.name) or entry.port)
            for entry in method.boxes))
        problems = refusals(method)
        if problems:
            raise ValueError("; ".join(problems))
        rendered = None
        if method.rendered_from is not None:
            rendered, why = templates.reproduce(method)
            if rendered is None:
                self.run_panel.say(f"{row.name} was rendered from a template, but {why}; "
                                   "it runs as a hand-written method", warn=True)
        name = row.name + (f" (pass {row.pass_label})" if row.pass_label else "")
        return PlannedPoint(
            index=number, method=method, replicates=row.replicates,
            method_path=os.path.abspath(row.method_path), sha256=planning.sha256(text),
            conditions=row.conditions, name=name,
            template=rendered.template_text if rendered is not None else "",
            knobs=dict(rendered.knobs) if rendered is not None else {},
            labels=dict(rendered.labels) if rendered is not None else {})

    def _queue_ended(self, result: object = None, failed: str = "") -> None:
        """The window's queue job came back: each row's state from its own files."""
        self._queue_job = None
        followed = self.followed
        if not self.queue.request_id and followed is not None and followed.request_id:
            # A `RecordBegun` drained before the job's start was seen here is in the
            # followed table, which reads it whatever the order.
            self.queue.request_id = followed.request_id
        if failed:
            self.queue.failed(failed)
        elif isinstance(result, SeriesResult):
            self.queue.ended(result.runs, result.numbers, result.replaced, result.stopped)
        else:
            self.queue.ended((), (), stopped="the queue job came back with no result")
        for number, row in enumerate(self.queue.started, start=1):
            self.run_panel.say(f"queue row {number} ({row.name}): {row.state}"
                               + (f" -- {row.outcome}" if row.outcome else ""),
                               warn=row.state in (runqueue.FAILED, runqueue.STOPPED))
        self.queue_panel.refresh()
        unrun = sum(row.state == runqueue.SKIPPED for row in self.queue.started)
        summary = self.queue.summary + (
            f"; Start runs the {unrun} row(s) not run as a new queue" if unrun else "")
        self.run_panel.say(summary, warn=bool(failed or unrun))
        self.statusBar().showMessage(summary)

    # -- a series plan (lab record, task 166) --------------------------------

    def open_plan(self, path: str = "") -> planning.Checked | None:
        """Open a series plan and show its check; the file dialog where no path is given.
        Answers the check, or None for a plan that did not load."""
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "Open a series plan", self._directory(),
                "Series plans (*.toml);;All files (*)")
            if not path:
                return None
        self.tabs.setCurrentWidget(self.queue_tab)
        self.queue_tabs.setCurrentWidget(self.plan_panel)
        checked = self._check_plan(path)
        if checked is None:
            return None
        description = checked.plan.description
        purpose = self.run_panel.purpose.text().strip()
        if description and (not purpose or purpose == self._prefilled):
            # A1: the plan's description is the purpose until the person types over it.
            self.run_panel.purpose.setText(description)
            self._prefilled = description
        self.run_panel.say(f"opened the plan {os.path.basename(path)}: "
                           f"{len(checked.points)} points, {checked.files} files; "
                           + self.plan_panel.custody.text())
        self._refresh_actions()
        return checked

    def _check_plan(self, path: str) -> planning.Checked | None:
        """The plan at `path` checked as the `series` tool checks it and drawn, or None
        with the reason drawn instead."""
        try:
            text, path = series_module.text_of(path)
            planned = series_module.loads(text)
            template_path = planning.locate(planned.template,
                                            library_dirs(self._plan_library()))
            template = templates.load_template(template_path)
            limits = self._sitting_limits()
            checked = planning.check(
                planned, template, template_path=template_path, path=path, limits=limits,
                real=not self.fake, pusher_period_us=self.instrument.pusher_period_us)
        except (series_module.PlanError, templates.TemplateError, OSError,
                ValueError) as exc:
            self.plan_panel.show_problem(f"The plan did not load: {exc}")
            self.run_panel.say(f"the plan {os.path.basename(path)} did not load: {exc}",
                               warn=True)
            return None
        found = AuditLog.beside(self._directory()).check_for(checked.sha256)
        self.plan_panel.show_plan(checked, planning.custody(found))
        return checked

    def _plan_library(self) -> str:
        """Where a plan's template is looked for: the daemon's library, which is the one
        an agent's check used, else this window's, else the instrument's."""
        hello = getattr(self.worker, "hello", None)
        return (str(getattr(hello, "library", "") or "") or self.settings.library_dir
                or clockwork.instrument_dir("golden") or "")

    def _sitting_limits(self) -> Limits | None:
        """The limits the owner's sitting carries, which a plan is shown against: a
        daemon's, loaded from the paths its `hello` reports (E6), or an in-process
        owner's own. `ValueError` with the sentence for limits that do not load."""
        owner = getattr(self.worker, "owner", None)
        hello = getattr(self.worker, "hello", None)
        if hello is None:
            return getattr(owner, "limits", None)
        _, limits = instrument_and_limits(
            fake=bool(getattr(hello, "fake", False)),
            limits_path=str(getattr(hello, "limits", "") or ""),
            project_limits_path=str(getattr(hello, "project_limits", "") or ""))
        return limits

    def start_plan(self) -> SeriesJob | None:
        """Submit the open plan as the daemon's series job, `source="window"`.

        The plan is read again first: a file changed since it was opened is shown again
        with its new check and custody, and Start pressed again runs that. Its seed is
        drawn here where it gives none, as the tool draws one, and the daemon begins the
        record with the plan's text, hash and matched check (`keeper.begin`)."""
        shown = self.plan_panel.checked
        if (shown is None or self._job is not None or self.queue.running
                or self._plan_job is not None):
            return None
        if not self._serve_ready():
            return None
        person = self._starter()
        if not person:
            return None
        if not clean_initials(self.initials.text()):
            self.run_panel.say("set the file initials first: they name the series' files",
                               warn=True)
            return None
        checked = self._check_plan(shown.path) if shown.path else shown
        if checked is None:
            return None
        if checked.sha256 != shown.sha256:
            self.run_panel.say("the plan changed on disk since it was opened; its check is "
                               "shown again, and Start runs it as it is now", warn=True)
            return None
        if checked.blocked():
            return None
        if not self.worker.console_alive:
            self.run_panel.say("starting the acquisition console first")
            self.start_console()
        planned = checked.plan
        seed = ((planned.seed if planned.seed is not None else series_module.draw_seed())
                if planned.shuffle else None)
        name = os.path.basename(checked.path) or "a plan"
        job = checked.job(
            seed=seed, label=f"acquiring the series plan {name}",
            conditions=self.conditions.toPlainText(), directory=self._directory(),
            initials=clean_initials(self.initials.text()),
            request=self.run_panel.purpose.text().strip(), instrument=self.instrument,
            instrument_path=self.instrument_path, cold_start="caution", source="window",
            at_window=person, with_plan=True)
        outside = checked.outside() or []
        self.run_panel.say(
            f"starting {name}: {len(checked.points)} points, {checked.files} files, about "
            f"{checked.minutes if checked.minutes is not None else '?'} minutes, "
            + (f"seed {seed}" if seed is not None else "in planned order")
            + (f"; {len(outside)} point(s) outside the agent's limits" if outside else ""))
        self._plan_job = job
        self.worker.submit(job)
        # Grey until the job's start re-reads the buttons: a second press in between would
        # be a second series.
        self.plan_panel.set_busy(True)
        return job

    def _filter_run_log(self, only: bool) -> None:
        series = self.followed
        self.run_panel.filter_to(series.tag if only and series is not None else None)

    def _follow_series(self, job: SeriesJob) -> None:
        """A series has started, whoever started it: the followed table is its (R5).

        Another client's series, this window's own from before it was reopened among
        them, says in the run log the order the table was built in, as planned indices,
        for a sitting to set beside the record's `order` (lab #30)."""
        origin = self.worker.origin(job)
        self.followed = FollowedSeries.of(job, origin, tag=self.run_panel.job_tag)
        if origin:
            order = ", ".join(str(point.index) for point in self.followed.points)
            self.run_panel.say(f"following {job.label}: {len(self.followed.points)} "
                               f"points in the order acquired, as planned indices: {order}")
        self.follow.follow(self.followed)
        if self.follow.only.isChecked():
            self._filter_run_log(True)

    # -- opening what a run left ---------------------------------------------

    def _live_run_path(self) -> str:
        """The raw file of the run in progress, or "" when no run is in progress.

        One source of truth and not two: `_live_run` is filled by `RunBegun` and
        emptied by the job ending, whichever way it ended, so there is no second
        condition here that could disagree with it.
        """
        return self._live_run[0]

    def _viewer_is_up(self) -> bool:
        """Whether the viewer this window launched is still open.

        Asked rather than assumed, because a trainee who closed the viewer wants the
        next Acquire to bring one back. The process handle comes from the resolved
        association, so this costs a `poll` and no more; where there is no handle --
        under `--fake`, or a route that started nothing we can hold -- the launch is
        taken to have stuck, and the button is how a second one is opened.
        """
        if self._viewer_launched and self._viewer_process is not None \
                and not still_running(self._viewer_process):
            self._viewer_launched = False
            self._viewer_process = None
        return self._viewer_launched

    def _run_begun(self, event: RunBegun) -> None:
        """The run's files exist, so the buttons that open them are worth pressing.

        This is what makes live viewing reachable (task 55): the raw file is the one
        the console is filling, and `_run_done` -- where the paths used to be recorded
        first -- is a minute or an hour later. `_run_done` still has the last word,
        since it is the one that knows whether the raw file was kept.

        The checkbox is answered here and not in `acquire`, for the same reason and one
        more: every run reports `RunBegun`, whichever client started it, so "Open
        mainspring when a run starts" opens one on an agent's series or another window's
        queue as readily as on this window's Acquire (the window revision's D4; lab
        record, task 169). One viewer a session, default off.
        """
        stem = os.path.splitext(os.path.basename(event.raw_path))[0]
        self._live_run = (event.raw_path, event.summed_path, stem)
        self._last_run_paths = self._live_run
        if self.open_on_acquire.isChecked() and not self._viewer_is_up():
            self._launch_viewer()
        self._refresh_actions()

    def _launch_viewer(self) -> None:
        """Open the run in progress in mainspring, following, in the method's mode.

        The raw file and never the summed companion: the companion is written by a fold
        after a method frame ends and is not the file being written now (the raw file
        keeps the plain name *because* it is the one that can be watched). A failure
        goes to the status bar and the run log and never to a dialog -- the run is what
        matters, and a modal box over a running acquisition is exactly what a trainee
        who ticked a checkbox did not ask for.
        """
        path = self._live_run[0]
        if not path:
            return
        result = open_data_file_with_options(
            path, viewer_options(self.repetition_mode.currentText()),
            self.settings.mainspring_path,
            spawn=self._record_launch if self.fake else None,
        )
        if result:
            self._viewer_launched = True
            self._viewer_process = result.process
            self.statusBar().showMessage(
                f"{os.path.basename(path)} opened in mainspring through {result.how}, "
                "following")
        else:
            self.statusBar().showMessage(
                f"mainspring could not be opened: {result.problem}")
            self.run_panel.say(
                f"the file being acquired could not be opened in mainspring, tried "
                f"{result.how}: {result.problem}", warn=True)

    def _record_launch(self, line: object) -> None:
        """Under `--fake`, say what would have been run instead of running it."""
        parts = [str(part) for part in line]  # type: ignore[union-attr]
        self.launched_commands.append(parts)
        self.run_panel.say("--fake: mainspring would have been launched as "
                           + " ".join(parts))

    def open_in_mainspring(self) -> None:
        if self._live_run_path():
            # During a run the button and the checkbox do the same thing, deliberately:
            # pressing it again is how a trainee gets a second viewer, which is the one
            # thing the once-a-session rule takes away.
            self._launch_viewer()
            return
        raw, summed, _ = self._last_run_paths
        path = summed if summed and os.path.isfile(summed) else raw
        if not path:
            return
        result = open_data_file(path, self.settings.mainspring_path)
        if result:
            self.statusBar().showMessage(
                f"opened {os.path.basename(path)} through {result.how}")
        else:
            self._complain(
                "That file could not be opened in mainspring",
                f"{result.problem}\n\nmainspring's installer registers itself for "
                "`.uimf`, which is what clockwork tries first. Where that is not the "
                "case, set the mainspring path in this window's settings.")

    def open_the_log(self) -> None:
        """Both files a run leaves beside the data, not one.

        The wire transcript has every byte and the send log has the strings a trainee
        reads; a question one raises is answered in the other, and opening only the
        second is what makes the first get forgotten.
        """
        raw, summed, stem = self._last_run_paths
        if not stem:
            return
        from .. import transcript

        # Beside the file the run actually wrote, not beside whatever the output
        # directory says now: a trainee who has moved on to the next sample should
        # still be able to open the log of the run that just finished.
        directory = os.path.dirname(raw or summed) or self._directory()
        opened = 0
        for name in (transcript.send_log_name(stem), transcript.default_name(stem)):
            path = os.path.join(directory, name)
            if open_path(path):
                opened += 1
        if not opened:
            self._complain("No log files were found",
                           f"Looked beside {directory} for the send log and the wire "
                           f"transcript of {stem}.")

    # -- worker traffic ------------------------------------------------------

    def _drain(self) -> None:
        events, dropped = self.mailbox.drain()
        if dropped:
            self.run_panel.say(f"{dropped} progress line(s) dropped: the log was "
                               "filling faster than it could be drawn", warn=True)
        for event in events:
            self.run_panel.show(event)
            if isinstance(event, LogsOpened):
                self._logged = (event.directory, event.stem)
            if isinstance(event, RunBegun):
                self._run_begun(event)
            followed = self.followed
            if followed is not None and not followed.ended and isinstance(
                    event, (PointStarted, Warned, RecordBegun)):
                if isinstance(event, PointStarted):
                    followed.point_started(event.number, event.retake, said=event)
                    if self._own_queue_running():
                        self.queue.point_started(event.number, event.retake)
                        self.queue_panel.refresh()
                elif isinstance(event, Warned):
                    followed.warned()
                elif not followed.request_id:
                    followed.request_id = event.request_id
                if isinstance(event, RecordBegun) and self._own_queue_running():
                    self.queue.request_id = event.request_id
                    self.queue_panel.refresh()
                self.follow.refresh()
            if isinstance(event, GuardChanged):
                self._show_guard()
            if isinstance(event, (PauseAsked, Held, Resumed, StopAsked)):
                # A person's pause, whoever's window asked for it: the followed table
                # shows where the series rests, and the controls follow the owner.
                if followed is not None and not followed.ended:
                    followed.hold = (event.hold if isinstance(event, (PauseAsked, Held))
                                     else None)
                    self.follow.refresh()
                if self._own_queue_running():
                    held = isinstance(event, Held)
                    for row in self.queue.started:
                        if row.state == runqueue.RUNNING:
                            row.step = ("held by the beam guard" if held and event.hold.guard
                                        else "held" if held else "")
                    self.queue_panel.refresh()
                if isinstance(event, Held):
                    self.run_panel.caption.setText(event.hold.text)
                self._refresh_actions()
            if isinstance(event, StateRead):
                # The panels are fed from the send's own readings rather than by
                # queuing a second `read_state` after every send. The reading
                # `send_phases` takes between `setup` and `load` is both free and
                # better than a fresh one: the box is still local there and its DC bias
                # monitors are still converting, where a reading taken after the send
                # would spend five seconds producing the one number nobody should
                # believe (task 43).
                self._show_state(event.box, event.state, event.when,
                                 sequencer=event.when == WHEN_ARMED)

    def _job_started(self, job: Job) -> None:
        self._job = job
        # Another client's job arrives here exactly as this window's own do, so that
        # the person at the instrument sees what Claude did as their own actions would
        # appear, and says whose it is (task 77).
        origin = self.worker.origin(job)
        label = f"{job.label} (from {origin})" if origin else job.label
        if origin:
            self.run_panel.say(f"{origin} started a job in clockwork serve: {job.label}")
        self.run_panel.begin(label)
        if isinstance(job, SeriesJob):
            self._follow_series(job)
        self.statusBar().showMessage(label)
        self._refresh_actions()

    def _job_finished(self, job: Job, result: object) -> None:
        self._job = None
        self._drain()
        # Drained first, so a `RunBegun` still in the mailbox does not refill this
        # after the run it describes has ended.
        self._live_run = ("", "", "")
        if isinstance(result, SeriesResult):
            if self.followed is not None and isinstance(job, SeriesJob):
                self.followed.finished(result.points_done, result.stopped)
                self.follow.refresh()
        if job is self._plan_job:
            self._plan_job = None
        elif isinstance(result, SendResult):
            self.run_panel.say(
                "nothing sent: the boxes hold the panes already" if not result.sent else
                f"{'setup, load and arm' if result.setup else 'load and arm'} sent in "
                f"{result.seconds:.1f} s; {os.path.basename(result.send_log)} beside "
                "the file")
        elif isinstance(result, ConsoleStatus):
            self.console_bar.show_status(result)
        ending = f"{job.label}: done"
        if isinstance(result, SeriesResult) and not result.complete:
            # Never "done" for a series a stop ended short (lab #26): which point, which
            # file, why, and what was never acquired, as every other client reads it.
            self.run_panel.say(f"{job.label}: {result.shortfall}", warn=True)
            ending = (f"{job.label}: stopped short, {result.points_done} of "
                      f"{result.points} points complete")
        elif isinstance(result, SeriesResult):
            # A line, not only the caption: a window that joined the series scans the
            # rack the moment it ends, and that job's caption replaced the only "done"
            # the window had written (lab #32). Said before `idle`, under the series' tag.
            files = len(result.runs)
            replaced = (f", {len(result.replaced)} of them failed and replaced by a "
                        "retake" if result.replaced else "")
            self.run_panel.say(
                f"{job.label}: complete, {result.points_done} of {result.points} points, "
                f"{files} file{'' if files == 1 else 's'}{replaced}")
        self.run_panel.idle(ending)
        self.statusBar().showMessage(ending)
        # After the log lines and before the buttons are re-read: the next row of a
        # queue is submitted from here, and a window that had already re-enabled Acquire
        # would offer a button that the very next statement takes away again.
        if job is self._queue_job:
            self._queue_ended(result)
        if self.tabs.currentWidget() is self.day_panel:
            self.refresh_day()
        self._scan_if_put_off()
        self._refresh_actions()
        self._close_if_asked()

    def _job_failed(self, job: Job, message: str) -> None:
        self._job = None
        self._drain()
        self._live_run = ("", "", "")
        self.run_panel.say(f"{job.label} failed: {message}", warn=True)
        self.run_panel.idle(f"{job.label}: failed")
        self.statusBar().showMessage(f"{job.label}: failed")
        # A failed Send or Acquire has used its number up, whatever it managed to write,
        # and the next one must not offer it again (lab #3). Before the queue moves on,
        # so its next row sends under the new name.
        if isinstance(job, (Send, Acquire, SeriesJob)):
            self._refresh_stem()
        if isinstance(job, SeriesJob):
            if self.followed is not None:
                self.followed.failed()
                self.follow.refresh()
        if job is self._plan_job:
            self._plan_job = None
        if job is self._queue_job:
            self._queue_ended(failed=message)
        self._scan_if_put_off()
        self._refresh_actions()
        self._close_if_asked()

    def _warn(self, line: str) -> None:
        """A warning line from the daemon's client. A method rather than a lambda, so
        that Qt drops the connection with the window rather than calling into a run
        panel that has been deleted."""
        self.run_panel.say(line, warn=True)

    # -- starting clockwork serve on demand (lab record, task 175) ------------------

    def _serve_absent(self) -> bool:
        worker = self.worker
        return isinstance(worker, RemoteWorker) and worker.hello is None and worker.absent

    def _next_output(self) -> str:
        """The folder the next serve this window starts writes to: the field's while no
        serve has said one, the saved one, a folder picked since, under one that has."""
        if not self._serve_output:
            self.settings.output_dir = self.output_dir.text().strip()
        return self.settings.output_dir

    def _serve_command(self) -> list[str]:
        """The command line of a serve started now, from the settings as they are now: a
        relaunch takes a folder or a sitting picked since the window opened."""
        return serve_command(
            output=self._next_output() if hasattr(self, "output_dir")
            else self.settings.output_dir,
            library=self.settings.library_dir, console=self.settings.console_path,
            kept=self.kept_root(), errors_log=errors.errors_log_path(),
            instrument=self.settings.instrument_path, limits=self.settings.limits_path,
            project_limits=self.settings.project_limits_path)

    def _no_serve(self) -> None:
        """No serve answered and this window started none: say so, where one would write
        and what starts one. Nothing is started by opening the window (D1 (b))."""
        folder = self._next_output()
        self.run_panel.say(
            "no clockwork serve is running on this PC, and opening this window started "
            "none; Find boxes, Send, Acquire or a Start starts one, writing to "
            + (folder or "its own default folder"))
        self._show_daemon()
        self._show_sitting()
        self._refresh_actions()

    def _serve_ready(self) -> bool:
        """Before an act that needs the instrument: True to go on, False when the person
        chose not to start clockwork serve. Where the act will start one, the command it
        is started with is built now, from the settings as they are (D2 (b)): a closed
        sitting's folder (`closed_sitting`, D3) is asked about first, start there or
        choose another, and the folder chosen is saved and is the one the serve gets.
        Asked before the job is built, so the job carries that folder too. On the UI
        thread; nothing here waits on hardware."""
        worker = self.worker
        if not isinstance(worker, RemoteWorker) or not worker.may_start():
            return True
        folder = self._next_output()
        accepted = bool(self._closed_ok) and bool(folder) and _same_folder(
            folder, self._closed_ok)
        why = "" if not folder or accepted else closed_sitting(folder)
        if why:
            answer = self._ask_closed_folder(folder, why)
            if answer == "choose":
                chosen = QFileDialog.getExistingDirectory(
                    self, "Where the files go", os.path.dirname(folder) or folder)
                if not chosen:
                    answer = "cancel"
                else:
                    self.settings.output_dir = chosen
                    if not self._serve_output:
                        self.output_dir.setText(chosen)
                    self._refresh_stem()
                    self._show_sitting()
                    self.run_panel.say(f"clockwork serve starts on {chosen}, chosen in "
                                       f"place of {folder}")
            elif answer == "here":
                self._closed_ok = folder
                self.run_panel.say(f"clockwork serve starts on {folder} as asked, though "
                                   f"{why}")
            if answer == "cancel":
                self.run_panel.say(f"clockwork serve was not started, and nothing was "
                                   f"done: {folder} is a closed sitting's folder", warn=True)
                return False
        self._launch_command = self._serve_command()
        return True

    def _ask_closed_folder(self, folder: str, why: str) -> str:
        """Start there, choose a folder, or cancel: `"here"`, `"choose"` or `"cancel"`.
        Choosing is the default, since the folder was saved by a sitting that has ended.
        A method of its own so that a test can answer it."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("A closed sitting's folder")
        box.setText(f"The output folder, {folder}, belongs to a sitting that has ended: "
                    f"{why}.")
        box.setInformativeText(
            "Starting clockwork serve there writes this sitting's day record and runs "
            "into it. Choose a folder for this sitting, or start there anyway.")
        choose = box.addButton("Choose a folder…", QMessageBox.ButtonRole.AcceptRole)
        here = box.addButton("Start there", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(choose)
        box.exec()
        clicked = box.clickedButton()
        return "choose" if clicked is choose else "here" if clicked is here else "cancel"

    def _daemon_connected(self, hello: object, started: bool) -> None:
        """A daemon answered: say which, and name the panes for its boxes.

        One this window started scans the rack as it starts, and its scan arrives as
        the other jobs do. One that was already running scanned long ago, so an idle one
        is asked again -- a rescan keeps the ports it holds open (task 62), so it costs
        seconds and resets nothing -- and a busy one is not held up by a scan: the panes
        are named from the boxes it reports, and the scan that gives them their ports is
        made as soon as the daemon is idle, not left to a Find boxes the trainee has to
        know to press before Send setup (lab record, task 96).
        """
        pid = getattr(hello, "pid", "?")
        if started:
            self.run_panel.say(f"started clockwork serve (pid {pid}); it owns the boxes "
                               "and the console, and this window is its client")
        else:
            self.run_panel.say(f"connected to clockwork serve (pid {pid}, since "
                               f"{getattr(hello, 'started', '?')}), which owns the boxes "
                               "and the console")
        simulated = bool(getattr(hello, "fake", False))
        # The title a `--fake` window has, for a plain window following a `clockwork
        # serve --fake`: the L3 replay is watched in one (lab record, task 165, A6).
        self.setWindowTitle(f"clockwork {clockwork.__version__}"
                            + ("  — simulated instrument" if self.fake or simulated else ""))
        if simulated:
            self.run_panel.say("that clockwork serve is --fake: nothing it reports is "
                               "evidence about a MIPS box or a digitizer", warn=True)
        self._show_daemon()
        before, was = self._directory(), self._serve_output
        output = str(getattr(hello, "output", "") or "")
        self._serve_output = os.path.normpath(output) if output else ""
        if was and not self._serve_output:
            # A serve from before it said its folder: the window's own, as it was.
            self.output_dir.setText(self.settings.output_dir)
        self._show_sitting()
        if self._serve_output and not _same_folder(before, self._serve_output):
            # One folder for every client, as for the sitting's documents (lab record,
            # task 172): the Day tab, a plan's check and the runs all read it.
            self.run_panel.say(f"the output folder is clockwork serve's, "
                               f"{self._serve_output}")
        self._refresh_stem()
        if self.tabs.currentWidget() is self.day_panel:
            self.refresh_day()
        instrument = str(getattr(hello, "instrument", "") or "")
        if self._joined() and instrument and os.path.normcase(instrument) != \
                os.path.normcase(self.instrument_path or ""):
            # One sitting for every client (E6): a joined daemon's instrument document is
            # the one this window's runs carry, not the one it last remembered.
            self._load_instrument(instrument, remember=False)
            self.run_panel.say(f"the instrument document is clockwork serve's, {instrument}")
        self._refresh_limits()
        status = getattr(self.worker, "status", None)
        if started or status is None:
            return
        if getattr(status.running, "kind", "") == "SeriesJob":
            # A routine choice: a window opened onto a series or a queue in flight opens
            # on the tab that follows it.
            self.tabs.setCurrentWidget(self.queue_tab)
        if status.running is None and not status.queued:
            self.find_boxes()
        else:
            self._scan_when_idle = True
            self._ensure_panes(list(status.boxes) + [name for name in self.ports
                                                     if name not in status.boxes])
            self._pane_changed()

    def _owner_status(self, status: object) -> None:
        """The daemon's status moved: other clients' jobs, the lock, the boxes."""
        worker = self.worker
        lines = []
        if isinstance(worker, RemoteWorker):
            lines = [f"{state}: {handle.label} (from {handle.origin or 'clockwork serve'})"
                     for state, handle in worker.others()]
        self.queue_panel.show_others(lines)
        self._show_guard()
        self._scan_if_put_off()
        self._refresh_actions()

    def _scan_if_put_off(self) -> None:
        """The scan a join put off, once the daemon is idle: its last status shows no
        job and the window has seen the last one end, in whichever order they arrived."""
        status = getattr(self.worker, "status", None)
        if (not self._scan_when_idle or self._job is not None or self.queue.running
                or self._close_when_idle
                or status is None or status.running is not None or status.queued):
            return
        self._scan_when_idle = False
        self.find_boxes()

    def _state_read(self, name: str, state: object) -> None:
        """A whole-state reading off a `ReadState` job, for the panel that asked."""
        self._show_state(name, state, "on demand")

    def _show_state(self, name: str, state: object, when: str,
                    sequencer: bool = False) -> None:
        """Fold one reading into a box's panel, keeping what it does not supersede.

        A two-getter `read_sequencer` updates the table engine and leaves the rest of
        the panel saying when *it* was read, because two commands were sent and sixty
        rows were not (task 43). Every other reading replaces the whole thing and drops
        the sequencer overlay, which is part of it again -- except the last converting
        DC bias monitors, which `Reading.with_state` keeps, because a refresh taken with
        the boxes armed is a true reading that destroys the only measurement of their
        outputs there is (task 56).
        """
        stamp = f"{when}, at {time.strftime('%H:%M:%S')}"
        held = self.readings.get(name, Reading())
        if sequencer:
            reading = replace(held, sequencer=state, sequencer_when=stamp)
        else:
            reading = held.with_state(state, stamp)
        self.readings[name] = reading
        pane = self.panes.get(name)
        if pane is not None:
            pane.show_state(reading, self._sent_box(name))

    def _discovered(self, found: object) -> None:
        entries = list(getattr(found, "found", ()))
        answered = {entry.name: entry for entry in entries if entry.name}
        names = list(answered) or list(self.panes)
        method_names = [entry for entry in self.ports if entry not in answered]
        self._ensure_panes(names + method_names)
        for name, pane in self.panes.items():
            entry = answered.get(name)
            if entry is not None:
                self.ports[name] = entry.port
                self._described[name] = (entry.port, entry.version, True)
            else:
                self._described[name] = ("", "", False)
            pane.describe(*self._described[name][:2], answered=self._described[name][2])
        self._show_boxes()
        # Through `_set_enable`, not by clearing and refilling the combo: `clear()` drops
        # the current index to the first item, and `_pane_changed()` below then reads the
        # widget back into the method, so a scan would silently rewrite `acquisition.enable`
        # to whichever box happened to sort first. That is the gate declaration -- the box
        # whose DIOA the digitizer's Control I/O 2 watches, and since the gating witness the
        # box whose `TBLCMPLT` is counted -- and a method is entitled to name a box that is
        # off, which `_set_enable` preserves by re-adding it. Found on the instrument,
        # 2026-09-17: a launch scan moved a method's enable from `auklet` to `cormorant`.
        self._set_enable(self._enable())
        self._pane_changed()

    def _enable(self) -> Enable | None:
        """What the widgets currently declare, or None where they declare nothing."""
        box = self.enable_box.currentText()
        channel = self.enable_channel.text().strip()
        return Enable(box=box, channel=channel.upper()) if box and channel else None

    def _own_queue_running(self) -> bool:
        """Whether the job running is this window's queue: not merely submitted, since it
        may wait behind another client's series, whose points are not its rows."""
        return (self._queue_job is not None and self._job is self._queue_job
                and self.queue.running)

    def _run_done(self, run: object) -> None:
        followed = self.followed
        if followed is not None and not followed.ended:
            number = followed.run_done()
            if self._own_queue_running() and number:
                self.queue.run_done(number, run)  # type: ignore[arg-type]
                self.queue_panel.refresh()
            self.follow.refresh()
        raw = getattr(run, "raw_path", "")
        summed = getattr(run, "summed_path", "")
        stem = os.path.splitext(os.path.basename(raw))[0]
        self._last_run_paths = (raw if getattr(run, "raw_kept", True) else "",
                                summed, stem)
        self.run_panel.say(getattr(run, "text", ""))
        self._refresh_stem()
        self._refresh_actions()

    # -- small things --------------------------------------------------------

    def _ensure_panes(self, names: list[str]) -> None:
        """One pane per box named, in the order given, keeping the text of any that
        survive. A box that has gone loses its pane and its text with it, which is
        correct: the text belongs to a box, and a method saved before the box went is
        still on disk."""
        wanted = list(dict.fromkeys(name for name in names if name))
        if wanted == list(self.panes):
            return
        kept = {name: pane.text() for name, pane in self.panes.items()}
        while self.pane_box.count():
            widget = self.pane_box.widget(0)
            widget.setParent(None)
            widget.deleteLater()
        self.panes = {}
        opened = self.settings.open_state_panels
        for name in wanted:
            pane = BoxPane(name)
            pane.parsed.connect(self._pane_changed)
            pane.read_state_requested.connect(self.read_state)
            pane.state.toggled.connect(self._state_panel_toggled)
            pane.state.set_open(name in opened)
            pane.state.set_busy(self._job is not None or not self.worker.boxes)
            if name in self._described:
                port, version, answered = self._described[name]
                pane.describe(port, version, answered=answered)
            pane.show_method(self._sent_box(name))
            if name in kept:
                pane.set_text(kept[name])
            if name in self.readings:
                pane.show_state(self.readings[name], self._sent_box(name))
            self.panes[name] = pane
            self.pane_box.addWidget(pane)

    def _state_panel_toggled(self, box: str, opened: bool) -> None:
        names = set(self.settings.open_state_panels)
        if opened:
            names.add(box)
        else:
            names.discard(box)
        self.settings.open_state_panels = names

    def _set_enable(self, enable: Enable | None) -> None:
        self.enable_box.clear()
        self.enable_box.addItems(list(self.panes))
        if enable is None:
            self.enable_channel.setText("")
            return
        index = self.enable_box.findText(enable.box)
        if index < 0:
            self.enable_box.addItem(enable.box)
            index = self.enable_box.count() - 1
        self.enable_box.setCurrentIndex(index)
        self.enable_channel.setText(enable.channel)

    def _directory(self) -> str:
        """Where this window's runs go and its Day tab and plan checks are read: the
        serve's output folder where it said one, else the field, else the cwd."""
        return self._serve_output or self.output_dir.text().strip() or os.getcwd()

    def _pick_output_dir(self) -> None:
        """The window's own folder; under a serve this window started, the one it is
        started with next time, as the sitting's documents are (lab record, task 172)."""
        path = QFileDialog.getExistingDirectory(
            self, "Where the files go", self.settings.output_dir or self._directory())
        if not path:
            return
        self.settings.output_dir = path
        # Written through at once: Warm up, run next from its shortcut, reads it from
        # the saved settings (lab record, task 175).
        self.settings.sync()
        if self._serve_output:
            self._show_sitting()
            self.run_panel.say(f"the output folder is {path} from the next start of "
                               f"clockwork serve; until then this window's runs go to "
                               f"{self._serve_output}")
            return
        self.output_dir.setText(path)
        self._refresh_stem()

    def _initials_changed(self) -> None:
        cleaned = clean_initials(self.initials.text())
        self.initials.setText(cleaned)
        self.settings.initials = cleaned
        if not self.run_panel.at_window.text().strip():
            # Prefilled, never overwritten: the person types their own once (A2), and a
            # prefilled value is asked about at the first Start (lab record, task 172).
            self.run_panel.at_window.setText(cleaned)
            self._at_window_confirmed = False
            self._show_at_window()
        self._refresh_stem()

    def _refresh_stem(self) -> None:
        if not self._serve_output:
            # Never the serve's folder: a window on its own, or Warm up's serve, would
            # inherit a folder it was never given.
            self.settings.output_dir = self.output_dir.text().strip()
        initials = clean_initials(self.initials.text())
        if not initials:
            self.stem.setPlaceholderText("set your initials first")
            return
        self.stem.setText(next_stem(self._directory(), initials))

    def kept_root(self) -> str:
        """The kept-files root in force: `$CLOCKWORK_REPORTS`, the setting, the default."""
        return keep.root(self.settings.kept_root)

    def choose_kept_root(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Kept files folder", self.kept_root())
        if not chosen:
            return
        self.settings.kept_root = chosen
        self.kept_field.setText(self.kept_root())
        line = f"failed and reported runs' files are kept in {self.kept_root()}"
        if not self.fake and not self.worker.set_kept_root(self.kept_root()):
            line += (" from the next start of clockwork serve; the one running keeps "
                     "failed runs where it was told to when it started")
        if os.environ.get(keep.ENV, "").strip():
            line += f" (${keep.ENV} is set, and outranks the folder just chosen)"
        self.run_panel.say(line)

    def prune_kept(self) -> threading.Thread:
        """Remove kept folders no report claimed, past their 90 days, on a thread."""
        root = self.kept_root()

        def work() -> None:
            removed = keep.prune(root)
            if removed:
                self.kept_pruned.emit(
                    f"removed {len(removed)} kept folder(s) older than "
                    f"{keep.RETENTION_DAYS} days that no report claimed, from {root}")

        thread = threading.Thread(target=work, name="clockwork-prune", daemon=True)
        thread.start()
        return thread

    def _report_inputs(self) -> tuple[str, str, str]:
        """`(directory, stem, transcript)` of the run in flight or else the last one.

        The last work to open a transcript, whether or not it got as far as a file: a run
        stopped before its first frame was kept under its stem by the owner, and Report
        reuses that folder only if it is given the stem (`keep.for_report`, lab record,
        task 104). The run paths are the fallback for a window that has opened no
        transcript this session.
        """
        from ..report import transcript_beside

        directory, stem = self._logged
        if not stem:
            raw, summed, stem = (self._live_run if self._live_run[2]
                                 else self._last_run_paths)
            directory = os.path.dirname(raw or summed) or self._directory()
        return directory, stem, transcript_beside(directory, stem)

    def report_url(self, *, report_id: str = "", kept: str = "") -> str:
        """The pre-filled bug report for this window: the method's name, and the
        transcript of the run in flight or else of the last one. No project: the window
        knows none, whatever folder its runs are written to (lab record, task 158)."""
        from ..report import Report

        _directory, _stem, transcript = self._report_inputs()
        return Report(method=self.method_path or "", errors_log=errors.errors_log_path(),
                      transcript=transcript, report_id=report_id, kept=kept).url()

    def report_problem(self) -> threading.Thread:
        """Keep the run's files off the UI thread, then open the report
        (`_report_ready`): copying a UIMF can take a while."""
        # Read here, on the UI thread: the thread below touches no widget.
        directory, stem, transcript = self._report_inputs()
        return self._report(directory, (stem,), transcript, self.method_path or "")

    def report_row(self, index: int) -> threading.Thread | None:
        """"Report this run" on a queue row: that row's stems, method and transcript.

        The toolbar's Report takes the run in flight or the last one, which in a queue
        is the wrong run as soon as a later row has started; lab #2 kept the next row's
        files for exactly that reason. The transcript is the row's last replicate's,
        which is where a stopped or failed row ended.
        """
        from ..report import transcript_beside

        if not 0 <= index < len(self.queue.rows):
            return None
        row = self.queue.rows[index]
        if not row.reportable:
            return None
        directory = row.directory or self._directory()
        transcript = transcript_beside(directory, row.stems[-1])
        return self._report(directory, row.stems, transcript, row.method_path)

    def _report(self, directory: str, stems: tuple[str, ...], transcript: str,
                method: str) -> threading.Thread:
        from ..report import Report

        errors_log = errors.errors_log_path()
        root = self.kept_root()
        self.action_report.setEnabled(False)
        self.statusBar().showMessage("keeping the run's files for the report…")

        def work() -> None:
            kept = problem = ""
            try:
                kept = keep.for_report(root, directory=directory, stems=stems,
                                       method_path=method, errors_log=errors_log)
            except Exception as exc:  # noqa: BLE001 -- the report goes out regardless
                problem = f"the files could not be kept in {root}: {exc}"
            address = Report(method=method, errors_log=errors_log, transcript=transcript,
                             report_id=os.path.basename(kept) if kept else "",
                             kept=kept).url()
            self.report_ready.emit(address, kept, problem)

        thread = threading.Thread(target=work, name="clockwork-report", daemon=True)
        thread.start()
        return thread

    def _report_ready(self, address: str, kept: str, problem: str) -> None:
        self.action_report.setEnabled(True)
        self.statusBar().clearMessage()
        if kept:
            self.run_panel.say(f"the report's files are kept in {kept}")
        if problem:
            self.run_panel.say(problem, warn=True)
        self._open_tracker(address, "report")

    def request_feature(self) -> None:
        """Open the feature request form. Nothing is kept and no log is read, so the
        address is made here on the UI thread."""
        from ..report import request_url

        self._open_tracker(request_url(), "request")

    def _open_tracker(self, address: str, what: str) -> None:
        if not QDesktopServices.openUrl(QUrl.fromEncoded(address.encode("ascii"))):
            # Eight kilobytes of address is no message to read in a dialog.
            QApplication.clipboard().setText(address)
            self._complain("No browser could be opened",
                           f"The {what}'s address is copied; paste it into a browser's "
                           "address bar to file it.")

    def _about(self) -> None:
        QMessageBox.about(
            self, "clockwork",
            f"clockwork {clockwork.__version__}\n"
            f"{clockwork.built_commit() or 'unknown commit'}\n\n"
            "Control software for SLIMPHONY: MIPS pulse sequences, SA220P "
            "acquisition, UIMF output.\n\n"
            "Data is viewed in mainspring; clockwork draws nothing.\n\n"
            f"Anything that goes wrong inside the window is written to\n"
            f"{errors.errors_log_path()}")

    def _forget_geometry(self) -> None:
        self.settings.reset_geometry()
        self.statusBar().showMessage(
            "the window position is forgotten; it takes effect at the next launch")

    def _say_error(self, line: str) -> None:
        """One warn line for a traceback the errors log has just taken.

        Not a dialog. A slot that raised has already returned and the window is still
        usable, so the thing to do is say what happened and where it is written and
        let the trainee decide whether this run is spoilt -- a modal box over a running
        acquisition would be worse than the exception.
        """
        self.run_panel.say(line, warn=True)
        self.statusBar().showMessage(line)

    def _complain(self, title: str, detail: str) -> None:
        QMessageBox.warning(self, title, detail)
        self.run_panel.say(f"{title}: {detail}", warn=True)

    # -- shutdown ------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802, ANN001 -- Qt's name
        self._stop_reporting()
        self.settings.geometry = self.saveGeometry()
        self.settings.initials = clean_initials(self.initials.text())
        if not self._serve_output:
            self.settings.output_dir = self.output_dir.text().strip()
        self.settings.conditions = self.conditions.toPlainText()
        self.settings.replicates = self.replicates.value()
        self.settings.tab = self.tabs.tabText(self.tabs.currentIndex())
        self.settings.open_mainspring_on_acquire = self.open_on_acquire.isChecked()
        self.settings.sync()
        if isinstance(self.worker, RemoteWorker) and not self._close_as_client():
            event.ignore()
            return
        self._drain_timer.stop()
        self.worker.shutdown()
        self.worker.wait(10_000)
        super().closeEvent(event)

    def _close_as_client(self) -> bool:
        """What closing does to the daemon: True to close now, False to stay open.

        Three cases (task 77, Matt's two decisions of 2026-09-26). **This window's own
        run is in flight, or its queue has rows to come**: ask whether to stop it, leave
        it running in the daemon, or not close; stopping keeps the window open to show
        the frame end and closes it when the job comes back. **Another client's job is
        in the daemon**: say so, and leave it and the daemon alone. **Nothing is
        running**: a daemon this window started is shut down with it, one it found
        running is left as it was found.
        """
        worker = self.worker
        assert isinstance(worker, RemoteWorker)
        if self._close_when_idle:
            return self._job is None
        if worker.status is None:
            return True
        mine = worker.busy_mine() or self._queue_job is not None
        others = worker.others()
        hold = worker.hold
        if mine:
            answer = (self._ask_close_paused() if hold is not None and hold.reached
                      else self._ask_close())
            if answer == "cancel":
                return False
            if answer == "stop":
                self._close_when_idle = True
                self.stop(signed=False)
                if self._job is None:
                    self._shutdown_if_mine()
                    return True
                self.run_panel.say("closing once the run has stopped and its files are "
                                   "closed", warn=True)
                return False
            # Left running: a queue is the daemon's series job, and runs on (R9).
            return True
        if others:
            state, handle = others[0]
            if hold is not None and hold.reached and hold.job == handle.id:
                # Another client's series, paused by a person: it stays paused (B5).
                self._tell(
                    "A paused series stays paused",
                    f"{handle.label} ({handle.origin or 'clockwork serve'}) is "
                    f"{hold.text}. It stays paused in clockwork serve after this window "
                    "closes, holding the instrument, until a person opens clockwork "
                    "again and presses Resume or Stop.")
                return True
            self._tell(
                "An acquisition is still running",
                f"{handle.label} ({handle.origin or 'clockwork serve'}) is {state} in "
                "clockwork serve, and carries on after this window closes. The daemon "
                "and the acquisition console stay running; open clockwork again to "
                "follow it.")
            return True
        self._shutdown_if_mine()
        return True

    def _shutdown_if_mine(self) -> None:
        """Stop the daemon if this window started it and nobody's job is left in it."""
        worker = self.worker
        assert isinstance(worker, RemoteWorker)
        if worker.spawned is not None and not worker.others():
            worker.shutdown_daemon("the window that started it closed")

    def _close_if_asked(self) -> None:
        """Close a window whose close waited for its run, once. A scan put off at the
        join is submitted by the same job end, and its own end arrives after `closeEvent`
        has waited out the worker: without the latch it asked the daemon to shut down a
        second time (lab record, task 175, where the window tests met it)."""
        if not self._close_when_idle or self._job is not None or self._closed_when_idle:
            return
        self._closed_when_idle = True
        if isinstance(self.worker, RemoteWorker):
            self._shutdown_if_mine()
        self.close()

    def _ask_close(self) -> str:
        """Stop, leave, or cancel: `"stop"`, `"leave"` or `"cancel"`. A method of its own
        so that a test can answer it."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("An acquisition is running")
        box.setText("This window's acquisition is still running in clockwork serve.")
        box.setInformativeText(
            "Stop it after the current repetition, fold it and close its files, then "
            "close; or leave it running and close now. Rows of the run queue still "
            "waiting are skipped either way.")
        stop = box.addButton("Stop, then close", QMessageBox.ButtonRole.AcceptRole)
        leave = box.addButton("Leave it running", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(stop)
        box.exec()
        clicked = box.clickedButton()
        return "stop" if clicked is stop else "leave" if clicked is leave else "cancel"

    def _ask_close_paused(self) -> str:
        """The close question's third case, this window's series at rest at a person's
        pause: `"leave"`, `"stop"` or `"cancel"`. Leaving it leaves it paused in the
        daemon, holding the instrument until a person at a window resumes or stops it
        (the window revision's B5), and is the default: a pause is asked for in order to
        come back to it."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("A series is paused")
        box.setText("This window's series is paused in clockwork serve.")
        box.setInformativeText(
            "Leave it paused and close now: it stays paused in clockwork serve, holding "
            "the instrument, until a person opens clockwork again and presses Resume or "
            "Stop. Or stop it where it rests, close its files, then close.")
        leave = box.addButton("Leave it paused", QMessageBox.ButtonRole.AcceptRole)
        stop = box.addButton("Stop, then close", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(leave)
        box.exec()
        clicked = box.clickedButton()
        return "stop" if clicked is stop else "leave" if clicked is leave else "cancel"

    def _tell(self, title: str, detail: str) -> None:
        """An information box, a method of its own so that a test can take it."""
        QMessageBox.information(self, title, detail)


# -- layout helpers ------------------------------------------------------------------


def _names_something(entry: BoxMethod, result: PaneResult) -> bool:
    """Whether this pane declares anything at all for its box.

    A comment is not a declaration and neither is an empty pane. `start` and `reset` are
    counted even though they are not phases a send delivers: a box that only appears in
    the start list is still a box the method drives, and one dropped from `method.boxes`
    would lose its readback and its stamp in the file.
    """
    return bool(
        [command for command in entry.setup + entry.load + entry.arm
         if not is_comment(command)]
        or entry.dc_bias or entry.rf or result.start or result.reset)


def _same_folder(one: str, other: str) -> bool:
    """Whether two folder paths name the same folder, as Windows compares them."""
    return (os.path.normcase(os.path.abspath(one))
            == os.path.normcase(os.path.abspath(other)))


def _tighten(form: QFormLayout) -> None:
    """Take the slack out of a form in the side column.

    Qt's default spacing is generous for a dialog and profligate for a column of three
    stacked group boxes that has to fit beside a run log on a 912 px working area
    (task 61). Four pixels between rows still reads as separate rows.
    """
    form.setVerticalSpacing(4)
    form.setContentsMargins(9, 6, 9, 6)


def _row(*widgets: QWidget) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    for index, widget in enumerate(widgets):
        layout.addWidget(widget, 1 if index == 0 else 0)
    return holder


def _spin(low: int, high: int, value: int, tip: str) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(low, high)
    spin.setValue(value)
    spin.setToolTip(tip)
    spin.setGroupSeparatorShown(True)
    return spin
