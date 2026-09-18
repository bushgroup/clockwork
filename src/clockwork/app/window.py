"""The window a trainee runs an acquisition day on SLIMPHONY from.

One pane per box holding the strings that used to be pasted into MIPS_QT6's terminal,
the `[acquisition]` settings as a form, and six buttons: find the boxes, send the setup,
load and arm, acquire N replicates, one more replicate, stop. No FALKOR, no MIPS_QT6, no
terminal. The lab record's task 50 is the design and its twelve decisions; this module
is the part of it that has widgets.

**It holds no copy of the acquisition sequence and no interpretation of a string.**
`send_phases` and `run_acquisition` are the whole of what an acquisition is, and
`clockwork.method.text` is the whole of what a line means; the window's job is to put a
method together out of the panes, hand it to the worker and render what comes back. When
this module and the loop could be said to disagree, the loop is right. The one judgement
made here is which button may be pressed, and even that is `refusals(method)`.

**Nothing here touches the instrument.** Every serial write and every ZeroMQ round trip
is on `Worker`'s thread; this thread assembles jobs and drains a mailbox on a timer. The
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
import time
from dataclasses import replace

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

import clockwork

from .. import instrument as instrument_module
from .. import method as method_module
from ..acq import ConsoleProcess, StateRead, find_console
from ..acq.loop import WHEN_ARMED, cautions, refusals
from ..instrument import UNCALIBRATED, Instrument
from ..method import (
    REPETITION_MODES,
    Acquisition,
    BoxMethod,
    Enable,
    Metadata,
    Method,
    RfChannel,
)
from ..method.text import render_pane, split_trainee_file, start_order
from .boxstate import Reading
from .console_panel import ConsoleBar, ConsoleSettings
from .launch import open_data_file, open_path
from .librarypanel import LibraryDialog
from .naming import clean_initials, next_stem
from .panes import BoxPane
from .queuepanel import QueuePanel
from .runlog import RunPanel
from .runqueue import FAILED, STOPPED, QueueRow, RunQueue, outcome_of
from .settings import Settings
from .worker import (
    Acquire,
    ConsoleStatus,
    Discover,
    Job,
    Mailbox,
    ReadState,
    RestartConsole,
    Send,
    SendResult,
    StartConsole,
    Worker,
    wire_fingerprint,
)

__all__ = ["DRAIN_MS", "MainWindow"]

DRAIN_MS = 50
"""How often the UI thread empties the worker's progress mailbox.

Faster than a repetition (about a second) so the bar moves while a method frame runs,
and slower than a published batch so a frame's ten batches cost one redraw rather than
ten. Nothing is lost by the gap: the mailbox keeps everything and collapses only the
counter.
"""


class MainWindow(QMainWindow):
    """Everything a trainee sees. One worker below it, one method held in the panes."""

    def __init__(self, *, fake: bool = False) -> None:
        super().__init__()
        self.fake = fake
        self.settings = Settings()
        self.mailbox = Mailbox()
        self.worker = Worker(fake=fake, mailbox=self.mailbox)

        self.method_path = self.settings.method_path
        self.instrument_path = self.settings.instrument_path
        self.instrument: Instrument = UNCALIBRATED
        self.metadata = Metadata(name="untitled", created=_dt.date.today())
        self.ports: dict[str, str] = {}
        """Which port each box answered on, or the method's hint for one that did not.
        A pane's header shows what the rack said; this is what the method is built with.
        """
        self.declared: dict[str, tuple[tuple[tuple[int, float], ...],
                                       tuple[RfChannel, ...]]] = {}
        """The DC bias and RF each box's method declares. Not pane text -- they are form
        fields in the document rather than strings a trainee types -- so they are kept
        here across an edit and written back out unchanged (`method-file-format.md`)."""

        self.panes: dict[str, BoxPane] = {}
        self.readings: dict[str, Reading] = {}
        """The last state reading of each box, kept here rather than in the pane.

        A pane is rebuilt whenever the rack's box list changes, and what a box last
        answered does not belong to a widget: a rediscovery that finds the same three
        boxes should not lose three readings that cost fifteen seconds."""

        self._job: Job | None = None
        self._armed: tuple = ()
        """What the last successful send put on the boxes (`wire_fingerprint`).

        `run_acquisition` expects the boxes to be loaded and armed already, so this is
        what lets the window say "send first" rather than let a trainee watch three
        `TBLSTRT`s be refused for "not in table mode". It survives a run and a
        replicate, because neither changes what a box is holding, and it goes stale the
        moment a pane is edited."""

        self.queue = RunQueue()
        """The unattended series, empty until a trainee puts something in it (task 53).

        The window is its sequencer: it submits the same `Send` and `Acquire` jobs the
        buttons do, one row at a time, so a queued acquisition and a pressed one are the
        same path onto the wire and leave the same files and logs behind."""

        self._queue_job: Job | None = None
        """The job the queue is waiting on, so the sequencer reacts to its own work and
        not to a discovery or a state reading that happened to land in between."""

        self._last_run_paths: tuple[str, str, str] = ("", "", "")
        """`(raw, summed, stem)` of the last run, for Open in mainspring and Open the
        log. The summed path is only offered once the fold has written it, and the
        directory is taken off the paths rather than off the field, which a trainee may
        have changed since."""

        self._build()
        self._connect()
        self._restore()
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(DRAIN_MS)
        self._drain_timer.timeout.connect(self._drain)
        self._drain_timer.start()
        self._refresh_actions()

    # -- construction --------------------------------------------------------

    def _build(self) -> None:
        self.setWindowTitle(f"clockwork {clockwork.__version__}"
                            + ("  — simulated instrument" if self.fake else ""))

        self.pane_box = QSplitter(Qt.Orientation.Horizontal)
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

        panes_and_order = QWidget()
        column = QVBoxLayout(panes_and_order)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.pane_box, 1)
        column.addWidget(QLabel("Start order"))
        column.addWidget(self.start_order)

        self.run_panel = RunPanel()
        self.problems = QLabel("")
        self.problems.setWordWrap(True)
        self.problems.setTextFormat(Qt.TextFormat.PlainText)

        middle = QSplitter(Qt.Orientation.Horizontal)
        middle.addWidget(panes_and_order)
        middle.addWidget(self._side_panel())
        middle.setStretchFactor(0, 3)
        middle.setStretchFactor(1, 2)

        whole = QSplitter(Qt.Orientation.Vertical)
        whole.addWidget(middle)
        bottom = QWidget()
        bottom_column = QVBoxLayout(bottom)
        bottom_column.setContentsMargins(0, 0, 0, 0)
        bottom_column.addWidget(self.problems)
        bottom_column.addWidget(self.run_panel, 1)
        whole.addWidget(bottom)
        whole.setStretchFactor(0, 3)
        whole.setStretchFactor(1, 2)
        self.setCentralWidget(whole)

        # The queue is a dock rather than a fourth splitter pane: one method at a time
        # is the first release and stays the ordinary way to work (the window design's
        # decision 7), so a trainee who never runs a series should not pay screen for
        # one. Shut by default, remembered per machine, and it opens to the full
        # width of the window under the run log, where an overnight series is read
        # beside the warnings it produced.
        self.queue_panel = QueuePanel(self.queue)
        self.queue_dock = QDockWidget("Run queue", self)
        self.queue_dock.setObjectName("run_queue")
        self.queue_dock.setWidget(self.queue_panel)
        self.queue_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea
                                        | Qt.DockWidgetArea.TopDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.queue_dock)
        self.queue_dock.setVisible(False)

        self.console_bar = ConsoleBar()
        self.statusBar().addPermanentWidget(self.console_bar)
        self.statusBar().showMessage("ready")
        self._build_menus()

    def _side_panel(self) -> QWidget:
        """The form half: what is acquired, where it goes, and the six buttons."""
        self.frames = _spin(1, 10_000, 1, "How many ion mobility experiments this "
                                          "method acquires, each folded into one frame "
                                          "of the companion file.")
        self.scans = _spin(1, 1_000_000, 5000,
                           "Pusher pulses in one ion mobility experiment. One scan is "
                           "one push.")
        self.accumulations = _spin(1, 100_000, 100,
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
            "once a fold has written the companion that replaces it.")
        self.enable_box = QComboBox()
        self.enable_box.setToolTip(
            "Which box drives the digitizer's enable line, and on which digital "
            "output. Declared rather than assumed: it is a fact about the cabling. "
            "single_frame needs it; per_repetition does not.")
        self.enable_channel = QLineEdit()
        self.enable_channel.setPlaceholderText("DIOA")
        self.enable_channel.setMaximumWidth(80)

        enable_row = QWidget()
        enable_layout = QHBoxLayout(enable_row)
        enable_layout.setContentsMargins(0, 0, 0, 0)
        enable_layout.addWidget(self.enable_box, 1)
        enable_layout.addWidget(self.enable_channel)

        acquisition = QGroupBox("Acquisition")
        form = QFormLayout(acquisition)
        form.addRow("Method frames", self.frames)
        form.addRow("Scans", self.scans)
        form.addRow("Accumulations", self.accumulations)
        form.addRow("Repetition mode", self.repetition_mode)
        form.addRow("Enable", enable_row)
        form.addRow("", self.keep_raw)

        # -- the file ---------------------------------------------------
        self.output_dir = QLineEdit()
        self.output_dir.setPlaceholderText("where the files go")
        browse_out = QPushButton("…")
        browse_out.setMaximumWidth(32)
        browse_out.clicked.connect(self._pick_output_dir)
        out_row = _row(self.output_dir, browse_out)

        self.initials = QLineEdit()
        self.initials.setMaximumWidth(80)
        self.initials.setToolTip(
            "The middle field of every file name, remembered per machine. The lab's "
            "files use the ion's code here; the window takes no view.")
        self.stem = QLineEdit()
        self.stem.setToolTip(
            "What this acquisition's files will be called. Filled from the output "
            "directory each time -- one past the highest number these initials already "
            "use there -- and editable before Acquire. A replicate takes the next "
            "number without asking.")
        rename = QPushButton("Next")
        rename.setMaximumWidth(56)
        rename.setToolTip("Re-read the output directory and take the next free number.")
        rename.clicked.connect(self._refresh_stem)

        self.replicates = QSpinBox()
        self.replicates.setRange(1, 999)
        self.replicates.setToolTip(
            "Technical replicates, run unattended after the first acquisition: the "
            "method's reset list, then the same again into a file of its own. Stop "
            "ends the series after the current repetition.")
        self.conditions = QPlainTextEdit()
        self.conditions.setPlaceholderText(
            "sample, MCP voltage, pusher period, collision energy…")
        self.conditions.setMaximumHeight(72)
        self.conditions.setToolTip(
            "The part of the experiment no getter reads. Stamped into every file this "
            "method writes and into the header of its send log. Everything else in "
            "that record came off a wire; this exists only if somebody types it.")

        files = QGroupBox("Files")
        files_form = QFormLayout(files)
        files_form.addRow("Output directory", out_row)
        files_form.addRow("Initials", self.initials)
        files_form.addRow("Name", _row(self.stem, rename))
        files_form.addRow("Replicates", self.replicates)
        files_form.addRow("Conditions", self.conditions)

        # -- the instrument document ------------------------------------
        self.instrument_field = QLineEdit()
        self.instrument_field.setReadOnly(True)
        self.instrument_field.setPlaceholderText("no instrument document")
        browse_instrument = QPushButton("…")
        browse_instrument.setMaximumWidth(32)
        browse_instrument.clicked.connect(self._pick_instrument)
        self.instrument_status = QLabel("")
        self.instrument_status.setWordWrap(True)

        document = QGroupBox("Instrument")
        document_form = QFormLayout(document)
        document_form.addRow("Document", _row(self.instrument_field, browse_instrument))
        document_form.addRow("", self.instrument_status)

        # -- the buttons -------------------------------------------------
        self.find_button = QPushButton("Find boxes")
        self.setup_button = QPushButton("Send setup")
        self.arm_button = QPushButton("Load and arm")
        self.acquire_button = QPushButton("Acquire")
        self.replicate_button = QPushButton("Replicate")
        self.stop_button = QPushButton("Stop")
        self.mainspring_button = QPushButton("Open in mainspring")
        self.log_button = QPushButton("Open the log")
        for button, tip in (
            (self.find_button, "Ask every MIPS-class port what box is behind it. A "
                               "port that does not answer has its box off or absent; "
                               "port presence says nothing."),
            (self.setup_button, "send_phases(setup=True): every phase, plus the state "
                                "readback that records what the boxes were holding and "
                                "what setup left them at."),
            (self.arm_button, "send_phases(setup=False): the table and the mode change "
                              "only, for boxes that have had their setup since "
                              "power-up."),
            (self.acquire_button, "The first acquisition, then the method's reset list "
                                  "and a replicate for each further count, one file "
                                  "each."),
            (self.replicate_button, "One more acquisition off the last one's readback: "
                                    "the reset list, then the same again."),
            (self.stop_button, "End the series after the current repetition and its "
                               "fold. What it leaves on disk is a short experiment, "
                               "not a broken one."),
            (self.mainspring_button, "Open the file in mainspring, which is the only "
                                     "viewer clockwork has. The summed companion once "
                                     "the fold has written it, the raw file before "
                                     "that."),
            (self.log_button, "Open both files a run leaves beside the data: the wire "
                              "transcript and the send log."),
        ):
            button.setToolTip(tip)

        buttons = QWidget()
        grid = QVBoxLayout(buttons)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(self.find_button)
        grid.addWidget(self.setup_button)
        grid.addWidget(self.arm_button)
        grid.addWidget(_row(self.acquire_button, self.replicates))
        grid.addWidget(self.replicate_button)
        grid.addWidget(self.stop_button)
        grid.addWidget(_row(self.mainspring_button, self.log_button))

        panel = QWidget()
        column = QVBoxLayout(panel)
        column.addWidget(acquisition)
        column.addWidget(files)
        column.addWidget(document)
        column.addWidget(buttons)
        column.addStretch(1)
        return panel

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.action_open = QAction("&Open method…", self)
        self.action_save = QAction("&Save method", self)
        self.action_save_as = QAction("Save method &as…", self)
        self.action_import = QAction("Open a &trainee paste file…", self)
        self.action_import.setToolTip(
            "Split one of the lab's old multi-box paste files into panes, off the "
            "`MIPS A`/`MIPS B` comments it names its boxes in. Anything the file does "
            "not attribute comes back whole, for the clipboard.")
        self.action_library = QAction("Method &library…", self)
        self.action_library.setToolTip(
            "Every method a directory holds, named, hashed, dated and described. Open "
            "one into the panes, or compare two -- or compare one against what the "
            "boxes are holding now.")
        self.action_quit = QAction("&Quit", self)
        for action in (self.action_open, self.action_save, self.action_save_as):
            file_menu.addAction(action)
        file_menu.addSeparator()
        file_menu.addAction(self.action_import)
        file_menu.addAction(self.action_library)
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
        run_menu.addSeparator()
        self.action_queue = self.queue_dock.toggleViewAction()
        self.action_queue.setText("Run &queue")
        self.action_queue.setToolTip(
            "A list of methods with their samples and replicate counts, run in order "
            "on the one worker: a series of conditions overnight or over lunch, from "
            "one click.")
        run_menu.addAction(self.action_queue)

        help_menu = self.menuBar().addMenu("&Help")
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

        self.find_button.clicked.connect(lambda: self.find_boxes())
        self.setup_button.clicked.connect(lambda: self.send(setup=True))
        self.arm_button.clicked.connect(lambda: self.send(setup=False))
        # `clicked` carries a `checked` flag, and every slot below takes none: bound
        # through a lambda rather than directly, or Qt passes False into the first
        # positional parameter and a keyword-only argument turns a button press into a
        # TypeError.
        self.acquire_button.clicked.connect(lambda: self.acquire())
        self.replicate_button.clicked.connect(lambda: self.replicate())
        self.stop_button.clicked.connect(lambda: self.stop())
        self.mainspring_button.clicked.connect(lambda: self.open_in_mainspring())
        self.log_button.clicked.connect(lambda: self.open_the_log())

        self.action_open.triggered.connect(self.open_method)
        # Not bound directly: `triggered` carries a `checked` flag, which would arrive
        # as `ask` and make Save behave as Save as on a menu that never asked for it.
        self.action_save.triggered.connect(lambda: self.save_method())
        self.action_save_as.triggered.connect(lambda: self.save_method(ask=True))
        self.action_import.triggered.connect(self.open_trainee_file)
        self.action_library.triggered.connect(self.open_library)
        self.action_quit.triggered.connect(self.close)
        self.action_find.triggered.connect(self.find_boxes)
        self.action_read_state.triggered.connect(self.read_state)
        self.action_console.triggered.connect(self.start_console)
        self.action_about.triggered.connect(self._about)
        self.action_forget_geometry.triggered.connect(self._forget_geometry)

        self.console_bar.restart_requested.connect(self.restart_console)
        self.console_bar.settings_requested.connect(self.console_settings)

        self.queue_panel.start_requested.connect(self.start_queue)
        self.queue_panel.stop_requested.connect(lambda: self.stop())
        self.queue_panel.add_requested.connect(self.queue_add_methods)
        self.queue_panel.add_open_requested.connect(self.queue_add_open_method)
        self.queue_panel.changed.connect(self._refresh_actions)

        self.initials.editingFinished.connect(self._initials_changed)
        self.output_dir.editingFinished.connect(self._refresh_stem)
        self.repetition_mode.currentTextChanged.connect(lambda _: self._refresh_actions())
        for spin in (self.frames, self.scans, self.accumulations):
            spin.valueChanged.connect(lambda _: self._refresh_actions())

    def _restore(self) -> None:
        geometry = self.settings.geometry
        if not geometry.isEmpty():
            self.restoreGeometry(geometry)
        self.initials.setText(self.settings.initials)
        self.output_dir.setText(self.settings.output_dir)
        self.replicates.setValue(self.settings.replicates)
        self.conditions.setPlainText(self.settings.conditions)
        self.queue_dock.setVisible(self.settings.queue_open)
        if self.instrument_path:
            self._load_instrument(self.instrument_path)
        if self.method_path and os.path.isfile(self.method_path):
            self._load_method(self.method_path)
        self._refresh_stem()
        if not self.fake:
            # On launch as well as on demand (lab record, task 50): a trainee arriving at
            # the instrument should find the panes already named for the boxes that are
            # on, not have to press a button to be told. Under `--fake` the rack comes
            # from the method instead, so `_load_method` above has already done it.
            self.find_boxes()

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
            boxes.append(result.box_method(
                self.ports.get(name, ""), dc_bias=dc_bias, rf=rf))
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
        self._refresh_start_order()
        self._refresh_actions()
        # A trainee who types the `SWFDIR` line a panel said was missing should watch
        # the row stop saying "left as found" as they type it, rather than wait five
        # seconds for a readback to confirm what the pane in front of them decides.
        method = self.build_method()
        for name, pane in self.panes.items():
            pane.show_method(self._box_method(name, method))

    def _refresh_start_order(self) -> None:
        method = self.build_method()
        if method is None:
            self.start_order.setText("")
            return
        lines = [f"{step.box:<12} {step.command}" for step in method.start]
        self.start_order.setText("\n".join(lines) or "(nothing classified as start)")

    def _refresh_actions(self) -> None:
        """Which buttons may be pressed, and why the greyed-out one is greyed out."""
        # A running queue is busy even in the instant between two of its jobs: the next
        # one is submitted from the signal that ended the last, and a trainee who got a
        # button back in that gap would be sending to boxes the queue is about to arm.
        busy = self._job is not None or self.queue.running
        method = self.build_method()
        problems = refusals(method) if method is not None else ["no boxes found yet"]
        notes = (list(method.warnings) + cautions(method)) if method is not None else []
        # The document's channel offset is sent to the console and stamped into the file
        # as one number, so a run without one cannot be prepared at all: `prepare_console`
        # refuses it by name. Said here, before the button, rather than by a job that
        # fails a second after it is pressed. Sending is not affected -- the boxes do not
        # need a document -- which is why it greys only the two acquisition buttons.
        no_offset = ([] if self.instrument.vertical.offset_v is not None else [
            "the instrument document states no channel offset, and the offset is sent "
            "to the console and stamped into the file as one number. Pick a document "
            "with an `offset_v` in its [vertical] table."])
        # An acquisition starts boxes that are expected to be loaded and armed already,
        # so a method that has not been sent, or one edited since it was, would spend
        # its frames having `TBLSTRT` refused for "not in table mode". Said here rather
        # than discovered three frames in.
        now = wire_fingerprint(method) if method is not None else ()
        if not self._armed:
            not_armed = ["the boxes have not been loaded and armed with this method. "
                         "Send setup, or Load and arm for boxes that have had their "
                         "setup since power-up."]
        elif now != self._armed:
            not_armed = ["the panes have changed since the boxes were armed, so the "
                         "table in the box is not the one on screen. Load and arm "
                         "again."]
        else:
            not_armed = []

        have_console = (self.worker.console is not None
                        and self.worker.console.alive)
        self.find_button.setEnabled(not busy)
        self.setup_button.setEnabled(not busy and not problems)
        self.arm_button.setEnabled(not busy and not problems)
        blocking = (problems if method is not None else []) + no_offset + not_armed
        self.acquire_button.setEnabled(not busy and not blocking)
        self.replicate_button.setEnabled(
            not busy and not blocking and self.worker.snapshot is not None)
        self.stop_button.setEnabled(busy and not self.worker.stopping)
        self.mainspring_button.setEnabled(any(self._last_run_paths[:2]))
        self.log_button.setEnabled(bool(self._last_run_paths[2]))
        self.action_read_state.setEnabled(not busy and bool(self.worker.boxes))
        for pane in self.panes.values():
            pane.state.set_busy(busy or not self.worker.boxes)
        self.action_console.setEnabled(not busy and not have_console)
        self.queue_panel.set_busy(busy)

        text: list[str] = []
        if blocking:
            text.append("Acquire is not available:")
            text += [f"  • {line}" for line in blocking]
        if notes:
            text.append("Worth knowing:")
            text += [f"  • {line}" for line in notes]
        self.problems.setText("\n".join(text))

    # -- methods on disk -----------------------------------------------------

    def open_method(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open method", os.path.dirname(self.method_path) or "",
            "Method documents (*.toml);;All files (*)")
        if path:
            self._load_method(path)

    def _load_method(self, path: str, *, quiet: bool = False) -> bool:
        """Put a document into the panes and the form, and say whether it opened.

        `quiet` sends the failure to the run log instead of a message box, and exists
        for the queue: a modal dialog raised by row four of an overnight series would
        stop the instrument at three in the morning and hold it there until somebody
        walked in and clicked OK.
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
        self.statusBar().showMessage(f"opened {os.path.basename(path)}")
        if self.fake:
            # The stand-in rack is built from the method, so opening one is what gives
            # `--fake` its boxes. Against hardware the ports are the authority and a
            # method is only a hint, so nothing is re-discovered here.
            self.find_boxes()
        self._pane_changed()
        return True

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
        try:
            method_module.save(method, path)
        except (OSError, method_module.MethodError) as exc:
            self._complain("That method could not be saved", str(exc))
            return
        self.method_path = path
        self.settings.method_path = path
        self.statusBar().showMessage(f"saved {os.path.basename(path)}")

    def open_trainee_file(self) -> None:
        """Split one of the lab's old paste files into panes, once.

        The map from a file's `MIPS A` to a box's `GNAME` is asked for rather than
        built in: which letter is which box is a fact about a rack and not about a
        document. Anything the file does not attribute lands in its own pane for the
        trainee to move by hand, which is the honest answer -- one golden file gives a
        block once, unlabelled, for two boxes.
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "Open a trainee paste file", "", "Text files (*.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            self._complain("That file could not be read", str(exc))
            return
        names = list(self.panes) or [entry for entry in self.ports]
        letters = ["MIPS A", "MIPS B", "MIPS C", "MIPS D"]
        mapping = {letter: name for letter, name in zip(letters, names, strict=False)}
        panes = split_trainee_file(text, mapping)
        unattributed = panes.pop(None, "")
        for name, pane_text in panes.items():
            if name in self.panes:
                self.panes[name].set_text(pane_text)
        if unattributed.strip():
            self.run_panel.say(
                f"{os.path.basename(path)}: {len(unattributed.splitlines())} line(s) "
                "name no box and were not placed in a pane. They are in the log below; "
                "move them by hand.", warn=True)
            for line in unattributed.splitlines():
                if line.strip():
                    self.run_panel.say(f"    {line}")
        self._pane_changed()

    def open_library(self) -> None:
        """The method browser: open a document into the panes, or run either diff.

        The directory offered the first time is `lab_dir("golden")` where it resolves
        and nothing has been picked yet -- a one-time suggestion, not a stored default
        (`Settings.library_dir`), since a public clone has no golden library to suggest
        and this repo's own is lab material this module never names by path.
        """
        directory = self.settings.library_dir or clockwork.lab_dir("golden") or ""
        dialog = LibraryDialog(directory, self.readings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.chosen_path:
            self.settings.library_dir = dialog.directory()
            self._load_method(dialog.chosen_path)
        else:
            self.settings.library_dir = dialog.directory()

    # -- the instrument document ---------------------------------------------

    def _pick_instrument(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open instrument document",
            os.path.dirname(self.instrument_path) or "",
            "Instrument documents (*.toml);;All files (*)")
        if path:
            self._load_instrument(path)

    def _load_instrument(self, path: str) -> None:
        try:
            document = instrument_module.load(path)
        except (OSError, instrument_module.InstrumentError) as exc:
            self._complain("That instrument document could not be opened", str(exc))
            return
        self.instrument = document
        self.instrument_path = path
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
        # The console holds what it was last configured with, so a changed document
        # has to reach it before the next run rather than at the next restart.

    # -- the console ---------------------------------------------------------

    def start_console(self) -> None:
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
        self.worker.submit(RestartConsole())

    def console_settings(self) -> None:
        console = self.worker.console
        if not isinstance(console, ConsoleProcess):
            self._complain(
                "There is no config.txt to edit",
                "The simulated console reads no configuration file; --fake exercises "
                "the status bar and the restart path and nothing below them.")
            return
        dialog = ConsoleSettings(console.config, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if not values:
            self.statusBar().showMessage("nothing changed")
            return
        self.worker.submit(RestartConsole(values=values))

    # -- the instrument ------------------------------------------------------

    def find_boxes(self) -> None:
        self.worker.submit(Discover(method=self.build_method()))

    def read_state(self, box: str = "") -> None:
        """A whole-state reading of one box, or of every box the rack answered for.

        A panel's own button names its box; the Run menu's entry does not and reads the
        whole rack. Queued rather than called: forty round trips on the UI thread would
        freeze the window for the seconds they cost.
        """
        self.worker.submit(ReadState(
            label=f"reading {box}" if box else "reading the boxes",
            names=(box,) if box else ()))

    def send(self, *, setup: bool) -> Send | None:
        """Queue a send, and hand the job back so the queue can wait on its own work."""
        method = self.build_method()
        if method is None:
            return None
        job = Send(
            label="sending setup, load and arm" if setup else "loading and arming",
            method=method, setup=setup,
            conditions=self.conditions.toPlainText(),
            directory=self._directory(), stem=self.stem.text().strip(),
            method_path=self.method_path,
            instrument=self.instrument, instrument_path=self.instrument_path,
        )
        self.worker.submit(job)
        return job

    def acquire(self, *, replicate_only: bool = False) -> Acquire | None:
        method = self.build_method()
        if method is None:
            return None
        if self.worker.console is None or not self.worker.console.alive:
            self.run_panel.say("starting the acquisition console first")
            self.start_console()
        self.settings.conditions = self.conditions.toPlainText()
        self.settings.replicates = self.replicates.value()
        job = Acquire(
            label="acquiring" if not replicate_only else "acquiring a replicate",
            method=method, instrument=self.instrument,
            instrument_path=self.instrument_path, method_path=self.method_path,
            directory=self._directory(), stem=self.stem.text().strip(),
            initials=self.initials.text(),
            replicates=1 if replicate_only else self.replicates.value(),
            conditions=self.conditions.toPlainText(),
            replicate_only=replicate_only,
        )
        self.worker.submit(job)
        return job

    def replicate(self) -> None:
        self._refresh_stem()
        self.acquire(replicate_only=True)

    def stop(self) -> None:
        self.worker.request_stop()
        self.run_panel.say("stopping after this repetition and its fold", warn=True)
        if self.queue.running:
            # The row in flight is not abandoned: `run_acquisition` ends after the
            # current repetition and its fold, and the row closes itself when its job
            # comes back. What Stop decides here is that nothing after it starts.
            self.queue.cancel("stopped by the operator")
            self.run_panel.say(
                "the queue stops with this row; the rows after it are skipped",
                warn=True)
            self.queue_panel.refresh()
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
            self.queue_dock.setVisible(True)
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
        self.queue_dock.setVisible(True)
        self.queue_panel.refresh()
        self._refresh_actions()

    def _queue_row(self, path: str) -> QueueRow:
        return QueueRow(method_path=path,
                        conditions=self.conditions.toPlainText().strip(),
                        replicates=self.replicates.value())

    def start_queue(self) -> None:
        """Walk every waiting row in order: load it, send it, acquire its replicates."""
        if self.queue.running or self._job is not None:
            return
        row = self.queue.begin()
        self.queue_panel.refresh()
        if row is None:
            self.run_panel.say("the queue has no waiting rows", warn=True)
            self._refresh_actions()
            return
        self.run_panel.say(
            f"the queue starts: {self.queue.waiting + 1} row(s) to run")
        self._start_row(row)

    def _start_row(self, row: QueueRow) -> None:
        """Open the row's method into the panes and put it on the wire.

        The document goes through the panes rather than past them, so what a queued row
        sends is what `build_method` makes of the panes -- the same method a trainee
        would have sent by hand -- and the window shows the experiment that is running
        rather than the one before it.
        """
        self.run_panel.say(
            f"queue row {self.queue.index + 1} of {len(self.queue.rows)}: {row.name}"
            + (f" -- {row.conditions}" if row.conditions else ""))
        if not self._load_method(row.method_path, quiet=True):
            self._row_finished(FAILED, "that method could not be opened")
            return
        self.conditions.setPlainText(row.conditions)
        self.replicates.setValue(row.replicates)
        method = self.build_method()
        problems = (refusals(method) if method is not None
                    else ["no boxes answered, so there is nothing to send to"])
        if problems:
            self._row_finished(FAILED, "; ".join(problems))
            return
        row.step = "sending"
        self.queue_panel.refresh()
        self._queue_job = self.send(setup=row.setup)
        if self._queue_job is None:
            self._row_finished(FAILED, "there was no method to send")

    def _queue_step(self, job: Job, result: object) -> None:
        """One of the queue's own jobs came back: acquire the row, or close it off."""
        if isinstance(job, Send):
            if self.queue.cancelled:
                self._row_finished(STOPPED, self.queue.cancelled)
                return
            row = self.queue.current
            if row is None:
                return
            row.step = "acquiring"
            self.queue_panel.refresh()
            self._queue_job = self.acquire()
            if self._queue_job is None:
                self._row_finished(FAILED, "there was no method to acquire")
            return
        row = self.queue.current
        if row is None:
            return
        runs = list(result) if isinstance(result, list) else []
        self._row_finished(outcome_of(row, runs))

    def _row_finished(self, state: str, problem: str = "") -> None:
        """Close the row in flight off and start the next one, or end the series."""
        self._queue_job = None
        row = self.queue.current
        if row is None:
            return
        where = self.queue.index + 1
        self.queue.finish(state, problem)
        self.run_panel.say(
            f"queue row {where} ({row.name}): {row.state}"
            + (f" -- {row.outcome}" if row.outcome else ""),
            warn=row.state in (FAILED, STOPPED))
        following = self.queue.advance()
        self.queue_panel.refresh()
        if following is not None:
            self._start_row(following)
            return
        self.run_panel.say(self.queue.summary, warn=bool(self.queue.cancelled))
        self.statusBar().showMessage(self.queue.summary)
        self._refresh_actions()

    # -- opening what a run left ---------------------------------------------

    def open_in_mainspring(self) -> None:
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
        self.run_panel.begin(job.label)
        self.statusBar().showMessage(job.label)
        self._refresh_actions()

    def _job_finished(self, job: Job, result: object) -> None:
        self._job = None
        self._drain()
        if isinstance(result, SendResult):
            self._armed = result.armed
            self.run_panel.say(
                f"{'setup, load and arm' if result.setup else 'load and arm'} sent in "
                f"{result.seconds:.1f} s; {os.path.basename(result.send_log)} beside "
                "the file")
        elif isinstance(result, ConsoleStatus):
            self.console_bar.show_status(result)
        self.run_panel.idle(f"{job.label}: done")
        self.statusBar().showMessage(f"{job.label}: done")
        # After the log lines and before the buttons are re-read: the next row of a
        # queue is submitted from here, and a window that had already re-enabled Acquire
        # would offer a button that the very next statement takes away again.
        if job is self._queue_job:
            self._queue_step(job, result)
        self._refresh_actions()

    def _job_failed(self, job: Job, message: str) -> None:
        self._job = None
        self._drain()
        self.run_panel.say(f"{job.label} failed: {message}", warn=True)
        self.run_panel.idle(f"{job.label}: failed")
        self.statusBar().showMessage(f"{job.label}: failed")
        if job is self._queue_job:
            self._row_finished(FAILED, message)
        self._refresh_actions()

    def _state_read(self, name: str, state: object) -> None:
        """A whole-state reading off a `ReadState` job, for the panel that asked."""
        self._show_state(name, state, "on demand")

    def _show_state(self, name: str, state: object, when: str,
                    sequencer: bool = False) -> None:
        """Fold one reading into a box's panel, keeping what it does not supersede.

        A two-getter `read_sequencer` updates the table engine and leaves the rest of
        the panel saying when *it* was read, because two commands were sent and sixty
        rows were not (task 43). Every other reading replaces the whole thing and drops
        the sequencer overlay, which is part of it again.
        """
        stamp = f"{when}, at {time.strftime('%H:%M:%S')}"
        held = self.readings.get(name, Reading())
        if sequencer:
            reading = replace(held, sequencer=state, sequencer_when=stamp)
        else:
            reading = Reading(state=state, when=stamp)
        self.readings[name] = reading
        pane = self.panes.get(name)
        if pane is not None:
            pane.show_state(reading, self._box_method(name))

    def _box_method(self, name: str, method: Method | None = None) -> BoxMethod | None:
        """This box's entry in the method the panes currently make, if it has one."""
        method = method if method is not None else self.build_method()
        if method is None:
            return None
        try:
            return method.box(name)
        except KeyError:
            return None

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
                pane.describe(entry.port, entry.version, answered=True)
            else:
                pane.describe(answered=False)
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

    def _run_done(self, run: object) -> None:
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
            if name in kept:
                pane.set_text(kept[name])
            if name in self.readings:
                pane.show_state(self.readings[name], self._box_method(name))
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
        return self.output_dir.text().strip() or os.getcwd()

    def _pick_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Where the files go", self._directory())
        if path:
            self.output_dir.setText(path)
            self.settings.output_dir = path
            self._refresh_stem()

    def _initials_changed(self) -> None:
        cleaned = clean_initials(self.initials.text())
        self.initials.setText(cleaned)
        self.settings.initials = cleaned
        self._refresh_stem()

    def _refresh_stem(self) -> None:
        self.settings.output_dir = self.output_dir.text().strip()
        initials = clean_initials(self.initials.text())
        if not initials:
            self.stem.setPlaceholderText("set your initials first")
            return
        self.stem.setText(next_stem(self._directory(), initials))

    def _about(self) -> None:
        QMessageBox.about(
            self, "clockwork",
            f"clockwork {clockwork.__version__}\n"
            f"{clockwork.built_commit() or 'unknown commit'}\n\n"
            "Control software for SLIMPHONY: MIPS pulse sequences, SA220P "
            "acquisition, UIMF output.\n\n"
            "Data is viewed in mainspring; clockwork draws nothing.")

    def _forget_geometry(self) -> None:
        self.settings.reset_geometry()
        self.statusBar().showMessage(
            "the window position is forgotten; it takes effect at the next launch")

    def _complain(self, title: str, detail: str) -> None:
        QMessageBox.warning(self, title, detail)
        self.run_panel.say(f"{title}: {detail}", warn=True)

    # -- shutdown ------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802, ANN001 -- Qt's name
        self.settings.geometry = self.saveGeometry()
        self.settings.initials = clean_initials(self.initials.text())
        self.settings.output_dir = self.output_dir.text().strip()
        self.settings.conditions = self.conditions.toPlainText()
        self.settings.replicates = self.replicates.value()
        self.settings.queue_open = self.queue_dock.isVisible()
        self.settings.sync()
        self._drain_timer.stop()
        self.worker.shutdown()
        self.worker.wait(10_000)
        super().closeEvent(event)


# -- layout helpers ------------------------------------------------------------------


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
