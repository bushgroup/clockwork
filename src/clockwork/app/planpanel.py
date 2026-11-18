"""A series plan in the window: open it, read its check, start it, and follow any series.

The window revision's R2 (lab record, task 166): the agent writes a large randomized plan,
a person opens it here, sees what it costs and whether an agent checked this same text,
and Start submits it as the daemon's series job with `source` `window`. Two widgets, built
to sit in today's queue dock as a second kind of entry and to move into task 169's Queue
tab as they are:

- `PlanPanel`: Open plan…, the check as a table (each point's knobs, repetitions, files and
  minutes in planned order, the totals, each point outside the sitting's limits marked
  with the sentence an agent's refusal would use), the custody line read off the day
  folder's audit log, and Start. It judges nothing itself: `clockwork.planning.check`
  does, the same function the `series` tool calls, and a point the template's ranges or
  the acquisition loop refuse keeps Start grey (R10: limits shown, never held).
- `SeriesFollow`: the followed table (R5, D1, D5) for any client's series, built from the
  job itself -- `JobStarted`'s `SeriesJob` -- and the events every client already
  receives. `FollowedSeries` is its model and holds no widget.

Neither submits a job or reads the owner: the window does, as it does for the run queue.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..method.template import format_number
from ..owner.hold import Hold
from ..planning import Checked

__all__ = ["DONE", "FAILED", "HELD", "RUNNING", "SKIPPED", "STOPPED", "WAITING",
           "FollowedPoint", "FollowedSeries", "PlanPanel", "SeriesFollow"]

WAITING = "waiting"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"
SKIPPED = "skipped"
HELD = "held"
"""A point a person's pause rests the series at: the one in flight for a pause inside it,
the next one for a pause between two points. Shown, never kept: it is the point's own
state again the moment the pause is lifted (`FollowedSeries.shown`)."""

CHECK_HEADINGS = ("#", "point", "reps", "files", "minutes", "limits")
FOLLOW_HEADINGS = ("#", "planned", "state", "point", "files", "minutes", "warnings")


def knob_text(knobs: Mapping[str, object], reference: str = "") -> str:
    """`amplitude_v 45, duration_ms 100`, or `the start reference`, as the table says it."""
    if reference:
        return f"the {reference} reference"
    return ", ".join(f"{name} {format_number(value)}" for name, value in knobs.items()
                     ) or "the defaults"


def _minutes(value: float | None) -> str:
    return "?" if value is None else f"{value:.1f}"


def _colour(widget: QWidget, state: str) -> QColor | None:
    """Only failed, stopped, skipped and held are coloured (D5), as in the run queue."""
    if state not in (FAILED, STOPPED, SKIPPED, HELD):
        return None
    light = widget.palette().window().color().lightness() > 128
    if state == FAILED:
        return QColor("#9b2c2c") if light else QColor("#fc8181")
    if state in (STOPPED, HELD):
        return QColor("#9c4221") if light else QColor("#f6ad55")
    muted = QColor(widget.palette().windowText().color())
    muted.setAlpha(140)
    return muted


# -- the plan ------------------------------------------------------------------------


class PlanPanel(QWidget):
    """Open a series plan, read its check and its custody, and start it."""

    open_requested = Signal()
    start_requested = Signal()
    stop_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.checked: Checked | None = None
        self._busy = False
        self._running = False

        self.title = QLabel("No plan open.")
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(CHECK_HEADINGS))
        self.tree.setHeaderLabels(list(CHECK_HEADINGS))
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.tree.header()
        header.setStretchLastSection(True)
        for column in range(len(CHECK_HEADINGS) - 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.totals = QLabel("")
        self.custody = QLabel("")
        self.custody.setToolTip(
            "Whether an agent checked this same text: the audit log in the output folder "
            "is searched for a `series` check of this plan's SHA-256. A plan edited since "
            "its check shows unchecked, and still runs.")
        self.limits = QLabel("")
        self.limits.setWordWrap(True)
        self.problems = QLabel("")
        self.problems.setWordWrap(True)
        self.problems.setVisible(False)

        self.open_button = QPushButton("Open plan…")
        self.open_button.setToolTip("Open a series plan (a .toml file with plan_schema = 1), "
                                    "such as one an agent wrote and checked.")
        self.start_button = QPushButton("Start the plan")
        self.start_button.setToolTip(
            "Submit the plan as one series job: every point sent and acquired in the "
            "order its seed draws, with a run record under your initials \"At the "
            "window\". Points outside the limits are run; the record says so.")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("End the series after the current repetition and its "
                                    "fold; the points not begun are not acquired.")
        self.open_button.clicked.connect(lambda: self.open_requested.emit())
        self.start_button.clicked.connect(lambda: self.start_requested.emit())
        self.stop_button.clicked.connect(lambda: self.stop_requested.emit())

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)

        column = QVBoxLayout(self)
        column.setContentsMargins(4, 4, 4, 4)
        column.addWidget(self.title)
        column.addWidget(self.tree, 1)
        column.addWidget(self.totals)
        column.addWidget(self.custody)
        column.addWidget(self.limits)
        column.addWidget(self.problems)
        column.addLayout(buttons)
        self._refresh_buttons()

    def show_plan(self, checked: Checked, custody: str) -> None:
        """Draw a plan's check: one row a point in planned order, the totals, the line on
        its custody, and what the limits say."""
        self.checked = checked
        name = os.path.basename(checked.path) if checked.path else "a plan"
        description = f": {checked.plan.description}" if checked.plan.description else ""
        self.title.setText(f"{name}{description}")
        self.title.setToolTip(checked.path)
        self.tree.clear()
        for judged in checked.points:
            entry = judged.entry()
            marks = judged.blocked or "; ".join(judged.outside or ())
            item = QTreeWidgetItem([
                str(judged.point.index),
                knob_text(judged.point.knobs, judged.point.reference),
                "" if entry["repetitions"] is None else str(entry["repetitions"]),
                str(judged.point.replicates), _minutes(entry["minutes"]),
                ("refused: " + judged.blocked) if judged.blocked
                else ("outside: " + marks if marks else "")])
            item.setToolTip(5, marks)
            if judged.blocked or judged.outside:
                colour = _colour(self, FAILED if judged.blocked else STOPPED)
                if colour is not None:
                    item.setForeground(5, QBrush(colour))
            self.tree.addTopLevelItem(item)
        outside = checked.outside()
        seed = (f"seed {checked.plan.seed}" if checked.plan.seed is not None
                else "a seed drawn at Start" if checked.plan.shuffle else "planned order")
        self.totals.setText(
            f"{len(checked.points)} points, {checked.files} files, about "
            f"{_minutes(checked.minutes)} minutes; {seed}")
        self.custody.setText(custody)
        if outside is None:
            self.limits.setText("No limits are in force for this sitting, so no point is "
                                "shown against them.")
        elif outside:
            self.limits.setText(
                f"{len(outside)} point(s) lie outside the limits an agent is held to. You "
                "are not held to them: Start runs them, and the run record lists them.")
        else:
            self.limits.setText("Every point lies inside the limits an agent is held to.")
        blocked = checked.blocked()
        self.problems.setText("Start is not available: " + "; ".join(blocked)
                              if blocked else "")
        self.problems.setVisible(bool(blocked))
        self._refresh_buttons()

    def show_problem(self, text: str) -> None:
        """A plan that did not load: say why, and keep nothing to start."""
        self.checked = None
        self.tree.clear()
        self.title.setText("No plan open.")
        self.totals.setText("")
        self.custody.setText("")
        self.limits.setText("")
        self.problems.setText(text)
        self.problems.setVisible(bool(text))
        self._refresh_buttons()

    def set_busy(self, busy: bool, running: bool = False) -> None:
        """What the window knows: a job is in flight, and whether it is a series."""
        self._busy, self._running = busy, running
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        ready = self.checked is not None and not self.checked.blocked()
        self.start_button.setEnabled(ready and not self._busy)
        self.stop_button.setEnabled(self._running)
        self.open_button.setEnabled(True)


# -- following a series ----------------------------------------------------------------


@dataclass
class FollowedPoint:
    """One point of a followed series, in the order acquired."""

    index: int
    knobs: dict[str, float]
    replicates: int = 1
    reference: str = ""
    minutes: float | None = None
    state: str = WAITING
    files: int = 0
    warnings: int = 0


@dataclass
class FollowedSeries:
    """Where a series is, off its job and its events: no widget, no owner.

    `points` are the job's, in the order acquired. `PointStarted` moves the point in
    flight, each `RunDone` counts a file in order, each warning is counted against it,
    and the job's end settles every state: the points done, the one stopped or failed,
    the rest skipped."""

    label: str
    points: list[FollowedPoint] = field(default_factory=list)
    started_by: str = ""
    seed: int | None = None
    request_id: str = ""
    current: int | None = None
    """The point in flight, as its place in `points`."""
    ended: str = ""
    """Empty while the job runs; else `done`, `stopped` or `failed`."""
    tag: int = 0
    """The run log's job number for the series' own lines (`RunPanel.filter_to`)."""
    hold: Hold | None = None
    """A person's pause on the series, asked for or in force, off its `PauseAsked` and
    `Held`; None once it is resumed, stopped or over (lab record, task 167)."""

    @property
    def held_at(self) -> int | None:
        """The place in `points` of the point a pause in force rests the series at, or
        None: the point in flight while it has files to come, else the one after it."""
        hold = self.hold
        if hold is None or not hold.reached or self.ended or self.current is None:
            return None
        point = self.points[self.current]
        if point.files < point.replicates:
            return self.current
        return min(self.current + 1, len(self.points) - 1)

    def shown(self, number: int) -> str:
        """Point `number`'s state as the table shows it: `held` over its own while a
        pause rests the series there."""
        held = self.held_at
        if number == held:
            return HELD
        point = self.points[number]
        if (held is not None and number == self.current and point.state == RUNNING
                and point.files >= point.replicates):
            # Every file of it is written and the series rests after it: it is done,
            # though the next point's start, which is what says so, has not come.
            return DONE
        return point.state

    @classmethod
    def of(cls, job: object, origin: str = "", tag: int = 0) -> FollowedSeries:
        """A `SeriesJob` as a followed table starts: every point waiting. `origin` is the
        client that started it, empty for this window."""
        points = [FollowedPoint(index=point.index, knobs=dict(point.knobs),
                                replicates=point.replicates, reference=point.reference,
                                minutes=getattr(point, "minutes", None))
                  for point in getattr(job, "points", ())]
        person = getattr(job, "at_window", "")
        who = origin or "this window"
        if person:
            who += f", {person} at the window"
        return cls(label=getattr(job, "label", "a series"), points=points,
                   started_by=who, seed=getattr(job, "seed", None), tag=tag)

    @property
    def point(self) -> FollowedPoint | None:
        return self.points[self.current] if self.current is not None else None

    def point_started(self, number: int) -> None:
        """Point `number` (its place in the order acquired, from 1) has begun; the one
        before it, if still running, ended with every file it was asked for."""
        if self.point is not None and self.point.state == RUNNING:
            self.point.state = DONE
        if 1 <= number <= len(self.points):
            self.current = number - 1
            self.points[self.current].state = RUNNING

    def run_done(self) -> None:
        """One file written: the first point, in the order acquired, without all its files.

        Counted by order rather than against the point in flight, because a file comes as a
        signal and a point's start through the mailbox, and the two can arrive out of order
        (the full suite caught a file credited to the point after its own). A series' files
        come in that order, each point's replicates whole, until a stop or a failure ends it."""
        for point in self.points:
            if point.files < point.replicates:
                point.files += 1
                return

    def warned(self) -> None:
        if self.point is not None:
            self.point.warnings += 1

    def finished(self, points_done: int, stopped: str = "") -> None:
        """The job came back: `points_done` points acquired whole, and why it stopped."""
        for number, point in enumerate(self.points):
            if number < points_done:
                point.state = DONE
            elif stopped and number == self.current:
                point.state = STOPPED
            elif stopped:
                point.state = SKIPPED
        self.ended = STOPPED if stopped else DONE
        self.current = None
        self.hold = None

    def failed(self) -> None:
        for number, point in enumerate(self.points):
            if number == self.current:
                point.state = FAILED
            elif point.state in (WAITING, RUNNING):
                point.state = SKIPPED
        self.ended = FAILED
        self.current = None
        self.hold = None

    @property
    def minutes_left(self) -> float | None:
        """The remaining points' planned minutes, the one in flight by the files it has
        left; None where a remaining point's minutes are unknown."""
        if self.ended:
            return 0.0
        left = 0.0
        for point in self.points:
            if point.state in (DONE, FAILED, STOPPED, SKIPPED):
                continue
            if point.minutes is None:
                return None
            share = 1.0
            if point.state == RUNNING and point.replicates:
                share = max(0, point.replicates - point.files) / point.replicates
            left += point.minutes * share
        return round(left, 1)

    @property
    def caption(self) -> str:
        done = sum(point.state == DONE for point in self.points)
        head = f"{self.label}, started by {self.started_by}"
        if self.request_id:
            head += f", request {self.request_id}"
        if self.ended:
            return f"{head}: {self.ended} after {done} of {len(self.points)} points"
        point = self.point
        where = "between points"
        if point is not None:
            where = (f"point {self.current + 1} of {len(self.points)} (planned "
                     f"{point.index}), {knob_text(point.knobs, point.reference)}, file "
                     f"{min(point.files + 1, point.replicates)} of {point.replicates}")
        paused = f"; {self.hold.text}" if self.hold is not None else ""
        return (f"{head}: {where}; about {_minutes(self.minutes_left)} minutes left"
                f"{paused}")


class SeriesFollow(QWidget):
    """The followed table: one row a point, in the order acquired."""

    filter_changed = Signal(bool)
    """The run log to be narrowed to this series' lines, or opened out again."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.series: FollowedSeries | None = None
        self.caption = QLabel("")
        self.caption.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(FOLLOW_HEADINGS))
        self.tree.setHeaderLabels(list(FOLLOW_HEADINGS))
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.tree.header()
        header.setStretchLastSection(False)
        for column in range(len(FOLLOW_HEADINGS)):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch if column == 3
                else QHeaderView.ResizeMode.ResizeToContents)
        self.only = QCheckBox("Show only this series in the run log")
        self.only.toggled.connect(lambda on: self.filter_changed.emit(on))
        column = QVBoxLayout(self)
        column.setContentsMargins(4, 4, 4, 4)
        column.addWidget(self.caption)
        column.addWidget(self.tree, 1)
        column.addWidget(self.only)
        self.setVisible(False)

    def follow(self, series: FollowedSeries) -> None:
        self.series = series
        self.setVisible(True)
        self.refresh()

    def refresh(self) -> None:
        series = self.series
        if series is None:
            return
        self.caption.setText(series.caption)
        self.tree.clear()
        bold = QFont(self.tree.font())
        bold.setBold(True)
        for number, point in enumerate(series.points, start=1):
            state = series.shown(number - 1)
            item = QTreeWidgetItem([
                str(number), str(point.index), state,
                knob_text(point.knobs, point.reference),
                f"{point.files} of {point.replicates}", _minutes(point.minutes),
                str(point.warnings) if point.warnings else ""])
            if point.state == RUNNING:
                for column in range(len(FOLLOW_HEADINGS)):
                    item.setFont(column, bold)
            colour = _colour(self, state)
            if colour is not None:
                item.setForeground(2, QBrush(colour))
            self.tree.addTopLevelItem(item)
        if series.current is not None:
            self.tree.scrollToItem(self.tree.topLevelItem(series.current))
