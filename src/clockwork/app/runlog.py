"""What a run is doing, and what it warned about, as it happens.

Two things a trainee watches while a method frame runs, and the design of both comes
from the instrument days rather than from taste.

**The bar counts scans, and the caption counts repetitions.** It ranged over
repetitions until 2026-09-17, on the assumption that a repetition is about a second and
a bar over a hundred of them moves visibly. That is true of a `per_repetition` method
and wrong by 65x of a `single_frame` one, which puts its hundred accumulations inside
the sequencer's own table and asks the console for **one** frame: the range was 1,
`FrameBegun` filled it, and the bar sat at 100 % with a frozen caption for the whole
65 s while a healthy CLOCK run went on behind it. Matt, watching it: *"I'm trying to
acquire, but I'm not sure if anything is happening."* So the bar now ranges over the
scans the whole run will publish -- two million for the detection-response method,
five hundred thousand for the CLOCK one -- and advances on `BatchSeen`, which arrives
about fifteen times a second in both. The caption still reads `repetition N of M`,
because that is the number a trainee matches against the method (lab record, task 56).

Nothing is ever said about the time remaining: the cost of a repetition is
occupancy-dependent, so the first one does not predict the hundredth (lab record,
task 34). The one wait that *is* estimated is the fold, which is silent for minutes and
whose cost is the file's size rather than its frame count (`clockwork.acq.Folding`).

**Left as found is collapsed, and nothing else is.** A clean golden run emits ten
`left as found` lines by design -- the ARB module settings the method does not name --
and ten correct lines that arrive on every acquisition are what teaches a trainee to
stop reading the log. So they fold into one row per box, expandable, and every other
`Warned` line is shown whole as it arrives, because on a clean run there are none. Two
kinds are never grouped even though they look similar: a declared-versus-read
disagreement is about *this* run's settings, and `DC bias monitors were not compared` is
the sentence that stops a trainee reading a frozen monitor as a fault (lab record,
task 43).

**Two groups per box, and one of them opens itself.** A setting the method names on one
module of a box and leaves on another is not the same fact as one the method has no
opinion about anywhere, and the grouping used to hide the first inside the second: on
2026-09-15 three settings were inherited rather than declared, the eleventh line was
invisible among the ten, and the one that mattered -- `SWFDIR`/`SALTWFM` left `REV` --
cost a run that showed no ions at all, which four strings then recovered a 73,000-fold
signal increase from. So a line carrying `DECLARED_ELSEWHERE` goes in its own row, shown
open, and the rest collapse as before (Matt, 2026-09-18; lab record, task 56).

**A frame that ended on silence is surfaced.** `ended_by == "silence"` means the frame
stopped because nothing had arrived for three seconds rather than because it counted
out, which is the one per-repetition outcome worth interrupting a trainee for. A frame
that counted out is not logged at all; the bar already said so.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
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
    DECLARED_ELSEWHERE,
    BatchSeen,
    BoxReady,
    BoxSaid,
    Event,
    Folded,
    Folding,
    FrameBegun,
    FrameEnded,
    GateChecked,
    PhaseSent,
    ReadingBack,
    RunBegun,
    StateRead,
    Warned,
)
from ..transcript import UNPROMPTED

__all__ = ["LEFT_AS_FOUND", "NEVER_GROUPED", "RunPanel", "is_left_as_found",
           "names_it_elsewhere"]

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


def names_it_elsewhere(message: str) -> bool:
    """Whether this left-as-found line is about a setting the method does name.

    On another module of the same box, which is the interesting case: a method with an
    opinion about module 1's direction and none about module 2's is a method with a gap
    in it, where one that names no direction anywhere is simply not about direction
    (`clockwork.acq.left_as_found`).
    """
    return DECLARED_ELSEWHERE in message


@dataclass(frozen=True, slots=True)
class Progress:
    """Where a run has got to, for the bar and its caption.

    The bar's two numbers are scans and the caption's two are repetitions, which is the
    whole of the fix of task 56: one method frame of a `single_frame` method is one
    repetition and half a million scans, and only one of those two counts is a thing a
    bar can move over.
    """

    method_frame: int = 0
    frames: int = 0
    repetition: int = 0
    of: int = 0
    scans: int = 0
    """Scans published across the whole run so far: the bar's value."""

    total: int = 0
    """Scans the run publishes if every frame counts out: the bar's maximum."""

    frame_length: int = 0
    """One console frame's scans, so a frame that has begun can put the bar at its own
    start before its first batch arrives."""

    frame_number: int = 0
    """Which console frame of the run is running, counted across method frames. What
    `scans` is measured from, since a batch says how far into *its* frame it is."""

    def at_batch(self, scans_so_far: int) -> Progress:
        """This run, with the bar moved to a batch's own count of its frame.

        Absolute rather than added up, because the window's mailbox collapses
        consecutive `BatchSeen`s into one slot and a bar built by accumulating deltas
        would drift low by everything it never drew (`clockwork.app.worker.Mailbox`,
        `clockwork.acq.BatchSeen.scans_so_far`).
        """
        before = max(0, self.frame_number - 1) * self.frame_length
        # Clamped, because scans keep arriving after a frame's own `finished` -- 500 on
        # every frame of the 2026-09-17 series -- and a bar past its maximum is a bar Qt
        # draws full while the run is not.
        return replace(self, scans=min(self.total, before + scans_so_far))

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
        self.copy_button = QPushButton("Copy")
        self.copy_button.setToolTip(
            "Put the whole log on the clipboard as text, so what happened on this "
            "machine can be pasted into a message rather than described. Collapsed "
            "groups are copied open, and a warning is marked with the send log's own "
            "! since text carries no colour.")
        self.copy_button.clicked.connect(self.copy)

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
        top.addWidget(self.copy_button)
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

    def as_text(self) -> str:
        """Every line of the log, in order, as `time<TAB>text`.

        One line per item and children after their parent, so a collapsed group of
        `left as found` lines copies out whole -- a trainee who copies the log has not
        necessarily clicked the groups open, and the lines inside one are exactly the
        lines somebody reading the paste will want.

        **A warn line is marked `!` and nothing else is.** The colouring is the only
        thing that distinguishes one on screen and text carries no colour, so the mark
        is the send log's own `UNPROMPTED`, which means the same thing there: a line
        the run raised rather than one it was asked for. A group's children inherit it
        rather than being asked themselves, since they are the warning the group's own
        line is counting.
        """
        lines: list[str] = []
        for index in range(self.log.topLevelItemCount()):
            item = self.log.topLevelItem(index)
            warn = bool(item.data(0, _IS_WARNING))
            lines.append(_as_line(item, warn))
            for child in range(item.childCount()):
                lines.append(_as_line(item.child(child), warn))
        return "\n".join(lines)

    def copy(self) -> None:
        """The whole log onto the clipboard, for pasting into a message.

        The run log is not written anywhere -- `say` only adds a tree item -- which is
        why one clean machine's run could not be brought back and had to be described
        from memory (lab record, task 61). This is the smallest thing that makes those
        lines portable; a log that writes itself to a file is a bigger question and is
        not this.
        """
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.as_text())

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
            # No line -- one per batch would bury the log at fifteen a second -- but this
            # is the only thing that moves inside a frame, and inside a `single_frame`
            # method's frame it is the only thing that moves at all.
            self.progress = self.progress.at_batch(event.scans_so_far)
            self.bar.setValue(self.progress.scans)
            return
        if isinstance(event, RunBegun):
            total = event.frames * event.console_frames * event.frame_length
            self.progress = Progress(frames=event.frames, of=event.console_frames,
                                     total=total, frame_length=event.frame_length)
            self.bar.setRange(0, max(1, total))
            self.bar.setValue(0)
            self.say(event.text)
            return
        if isinstance(event, FrameBegun):
            self.progress = replace(
                self.progress,
                method_frame=event.method_frame, frames=self.progress.frames or 1,
                repetition=event.repetition, of=event.of,
                frame_number=event.frame_number)
            # To this frame's own start, so a frame whose batches are still coming does
            # not leave the bar where the last one's overrun put it.
            self.progress = self.progress.at_batch(0)
            self.bar.setValue(self.progress.scans)
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
        if isinstance(event, (BoxReady, GateChecked, Folding, Folded, BoxSaid)):
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
        gap = names_it_elsewhere(message)
        key = f"{box}\ngap" if gap else box
        parent = self._groups.get(key)
        if parent is None:
            parent = QTreeWidgetItem([self._stamp(), ""])
            _colour(parent, self._warn_colour())
            self.log.addTopLevelItem(parent)
            self._groups[key] = parent
            # Open from the first line rather than on click. A group that hides a
            # setting the method has an opinion about elsewhere is the log doing the
            # thing the grouping cost the lab a run for (task 56).
            parent.setExpanded(gap)
        parent.addChild(QTreeWidgetItem(["", message]))
        count = parent.childCount()
        settings = f"{count} setting{'s' if count > 1 else ''}"
        parent.setText(1, f"{box}: {settings} left as found that this method names on "
                          "other modules" if gap else
                          f"{box}: {settings} left as found (click to expand)")
        self._scroll(parent)

    # -- small helpers -------------------------------------------------------

    def _stamp(self) -> str:
        return f"{time.monotonic() - self._started:7.1f}"

    def _warn_colour(self) -> QColor:
        light = self.palette().window().color().lightness() > 128
        return QColor("#9c4221") if light else QColor("#f6ad55")

    def _scroll(self, item: QTreeWidgetItem) -> None:
        self.log.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtBottom)


_IS_WARNING = int(Qt.ItemDataRole.UserRole) + 1
"""Where a line records that it is a warning, for the clipboard to read back.

The brush is the other way to ask and is the wrong one: `_warn_colour` answers two
different colours depending on the palette, so a reader would have to know which theme
the line was written under.
"""


def _as_line(item: QTreeWidgetItem, warn: bool) -> str:
    """One tree item as `time<TAB>text`, with `!` on a warning.

    A child carries no time of its own -- it is stamped by the group above it -- and
    its empty first column copies out as one, so the tab is always there and a paste is
    two columns whatever was in it.
    """
    mark = f"{UNPROMPTED} " if warn else ""
    return f"{item.text(0).strip()}\t{mark}{item.text(1)}"


def _colour(item: QTreeWidgetItem, colour: QColor) -> None:
    item.setData(0, _IS_WARNING, True)
    brush = QBrush(colour)
    font = QFont(item.font(1))
    font.setBold(True)
    for column in (0, 1):
        item.setForeground(column, brush)
    item.setFont(1, font)
    item.setTextAlignment(0, Qt.AlignmentFlag.AlignRight
                          | Qt.AlignmentFlag.AlignVCenter)
