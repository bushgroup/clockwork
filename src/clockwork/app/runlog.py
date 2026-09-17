"""What a run is doing, and what it warned about, as it happens.

Two things a trainee watches while a method frame runs, and the design of both comes
from the instrument days rather than from taste.

**The bar counts repetitions in place.** A repetition is about a second, so a bar over a
method frame's hundred of them moves visibly and a line appended per repetition would
push everything worth reading off the top within two minutes. No estimate of the time
remaining is shown: the cost of a repetition is occupancy-dependent, so the first one
does not predict the hundredth (lab record, task 34).

**Left as found is collapsed, and nothing else is.** A clean golden run emits ten
`left as found` lines by design -- the ARB module settings the method does not name --
and ten correct lines that arrive on every acquisition are what teaches a trainee to
stop reading the log. So they fold into one row per box, expandable, and every other
`Warned` line is shown whole as it arrives, because on a clean run there are none. Two
kinds are never grouped even though they look similar: a declared-versus-read
disagreement is about *this* run's settings, and `DC bias monitors were not compared` is
the sentence that stops a trainee reading a frozen monitor as a fault (lab record,
task 43).

**A frame that ended on silence is surfaced.** `ended_by == "silence"` means the frame
stopped because nothing had arrived for three seconds rather than because it counted
out, which is the one per-repetition outcome worth interrupting a trainee for. A frame
that counted out is not logged at all; the bar already said so.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..acq import (
    BatchSeen,
    BoxReady,
    BoxSaid,
    Event,
    Folded,
    FrameBegun,
    FrameEnded,
    GateChecked,
    PhaseSent,
    ReadingBack,
    RunBegun,
    StateRead,
    Warned,
)

__all__ = ["LEFT_AS_FOUND", "NEVER_GROUPED", "RunPanel", "is_left_as_found"]

LEFT_AS_FOUND = " is left as found on module"
"""The phrase `clockwork.acq.loop.left_as_found` builds every one of its lines around.

Matched as a substring rather than parsed, because what is being detected is a *class*
of message rather than a structure, and a line whose wording changes should stop being
grouped and start being read rather than silently vanish into the wrong bucket.
"""

NEVER_GROUPED = (
    " was declared ",
    " monitors ",
    "DC bias monitors were not compared",
    " reports no such channel",
)
"""Warnings that look like state readings and must never be folded away.

The first three are the declared-versus-read lines and the table-mode caveat, which
are about what this run is doing rather than about what it left alone.
"""


def is_left_as_found(message: str) -> bool:
    """Whether this `Warned` line is one of the ten a clean golden run emits."""
    if any(phrase in message for phrase in NEVER_GROUPED):
        return False
    return LEFT_AS_FOUND in message


@dataclass(frozen=True, slots=True)
class Progress:
    """Where a run has got to, for the bar and its caption."""

    method_frame: int = 0
    frames: int = 0
    repetition: int = 0
    of: int = 0
    scans: int = 0

    @property
    def text(self) -> str:
        if not self.of:
            return ""
        frames = f"method frame {self.method_frame} of {self.frames}, " \
            if self.frames > 1 else ""
        return f"{frames}repetition {self.repetition} of {self.of}"


class RunPanel(QWidget):
    """The bar, its caption, and the log of everything worth reading.

    `show(event)` takes one of the loop's `Event`s and does the right thing with it;
    `say(text)` is for the window's own lines, which are the ones that did not come off
    a wire. Nothing here blocks and nothing here touches the instrument: it is fed from
    the worker's mailbox, drained on the UI thread.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.progress = Progress()
        self._groups: dict[str, QTreeWidgetItem] = {}
        self._started = time.monotonic()
        self._reading_back: QTreeWidgetItem | None = None

        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.caption = QLabel("")
        self.clear_button = QPushButton("Clear")
        self.clear_button.setToolTip(
            "Empty the log. The transcript and the send log beside the file keep "
            "everything, so nothing is lost by clearing this.")
        self.clear_button.clicked.connect(self.clear)

        self.log = QTreeWidget()
        self.log.setColumnCount(2)
        self.log.setHeaderLabels(["at", "what happened"])
        self.log.setRootIsDecorated(True)
        self.log.setUniformRowHeights(True)
        self.log.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.log.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.log.header().setStretchLastSection(True)
        self.log.setColumnWidth(0, 64)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.caption, 1)
        top.addWidget(self.clear_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.bar)
        layout.addLayout(top)
        layout.addWidget(self.log, 1)

    # -- the window's own lines ----------------------------------------------

    def clear(self) -> None:
        self.log.clear()
        self._groups.clear()
        self._reading_back = None

    def begin(self, what: str) -> None:
        """A job has started: reset the bar and say what is happening."""
        self._started = time.monotonic()
        self.progress = Progress()
        self.caption.setText(what)
        self.bar.setRange(0, 0)  # indeterminate until a run says how many frames

    def idle(self, what: str = "") -> None:
        self.bar.setRange(0, 1)
        self.bar.setValue(1 if self.progress.of else 0)
        self.caption.setText(what)

    def say(self, text: str, *, warn: bool = False) -> QTreeWidgetItem:
        """One line of the window's own. Returns it, for a caller that will update it."""
        item = QTreeWidgetItem([self._stamp(), text])
        if warn:
            _colour(item, self._warn_colour())
        self.log.addTopLevelItem(item)
        self._scroll(item)
        return item

    # -- the loop's events ---------------------------------------------------

    def show(self, event: Event) -> None:  # noqa: C901 -- one branch per event type
        """Render one event, or decide it is not worth a line."""
        if isinstance(event, BatchSeen):
            return  # the bar's caption carries it; a line per batch would bury the log
        if isinstance(event, RunBegun):
            self.progress = Progress(frames=event.frames, of=event.console_frames)
            self.bar.setRange(0, max(1, event.frames * event.console_frames))
            self.bar.setValue(0)
            self.say(event.text)
            return
        if isinstance(event, FrameBegun):
            self.progress = Progress(
                method_frame=event.method_frame, frames=self.progress.frames or 1,
                repetition=event.repetition, of=event.of)
            self.bar.setValue(event.frame_number)
            self.caption.setText(self.progress.text)
            return
        if isinstance(event, FrameEnded):
            record = event.record
            if record.ended_by == "silence":
                self.say(f"{record.text}  (ended on silence, not on a count)", warn=True)
            elif not record.acquired:
                self.say(record.text, warn=True)
            return
        if isinstance(event, Warned):
            self._warned(event.message)
            return
        if isinstance(event, ReadingBack):
            self.reading_back(event.box, listing=event.listing)
            return
        if isinstance(event, StateRead):
            self._finish_readback()
            self.say(event.text)
            return
        if isinstance(event, PhaseSent):
            if event.error is not None:
                self.say(event.text, warn=True)
            return
        if isinstance(event, (BoxReady, GateChecked, Folded, BoxSaid)):
            self.say(event.text)
            return
        self.say(event.text)

    def reading_back(self, box: str, *, listing: bool = False) -> None:
        """The line shown for the ~5 s the three `GCMDS` listings cost.

        Replaced in place by the reading itself when it arrives, so the log does not
        carry a "reading back" line for every box on top of every reading. The listings
        are cached for the session, so only the first send of a session pays this and
        the line says which kind of wait a trainee is looking at
        (`send_phases(listings=)`).
        """
        self._finish_readback()
        self._reading_back = self.say(
            f"reading back {box}…"
            + (" (asking what commands it has, about 2 s)" if listing else ""))

    def _finish_readback(self) -> None:
        item, self._reading_back = self._reading_back, None
        if item is not None:
            index = self.log.indexOfTopLevelItem(item)
            if index >= 0:
                self.log.takeTopLevelItem(index)

    # -- warnings ------------------------------------------------------------

    def _warned(self, message: str) -> None:
        if not is_left_as_found(message):
            self.say(message, warn=True)
            return
        box = message.split(" ", 1)[0]
        parent = self._groups.get(box)
        if parent is None:
            parent = QTreeWidgetItem([self._stamp(), ""])
            _colour(parent, self._warn_colour())
            self.log.addTopLevelItem(parent)
            self._groups[box] = parent
        parent.addChild(QTreeWidgetItem(["", message]))
        count = parent.childCount()
        parent.setText(1, f"{box}: {count} setting{'s' if count > 1 else ''} left as "
                          "found (click to expand)")
        self._scroll(parent)

    # -- small helpers -------------------------------------------------------

    def _stamp(self) -> str:
        return f"{time.monotonic() - self._started:7.1f}"

    def _warn_colour(self) -> QColor:
        light = self.palette().window().color().lightness() > 128
        return QColor("#9c4221") if light else QColor("#f6ad55")

    def _scroll(self, item: QTreeWidgetItem) -> None:
        self.log.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtBottom)


def _colour(item: QTreeWidgetItem, colour: QColor) -> None:
    brush = QBrush(colour)
    font = QFont(item.font(1))
    font.setBold(True)
    for column in (0, 1):
        item.setForeground(column, brush)
    item.setFont(1, font)
    item.setTextAlignment(0, Qt.AlignmentFlag.AlignRight
                          | Qt.AlignmentFlag.AlignVCenter)
