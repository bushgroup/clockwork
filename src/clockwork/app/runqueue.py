"""The list of methods a window runs in order, and the rules for walking it.

The Qt-free half of the run queue (lab record, task 53, and the window design's
decision 10). A row is a method document, a sample or conditions note and a replicate
count; the queue is those rows, which one is in flight, and the decision of whether the
series goes on after each one. `clockwork.app.queuepanel` is the table that draws it
and the window is the sequencer that submits the jobs, so nothing here knows about a
widget, a worker or a wire.

**A row is a path, not a method.** What a row names is a document on disk, read at the
moment the row starts rather than when it was added. An overnight series set up at five
o'clock and started at seven runs what the files say at seven, which is the behaviour a
trainee who fixed a typo in between expects, and it is also the only version that
survives the window being closed and reopened.

**Every row is sent before its first acquisition.** Two rows may name different methods,
so the boxes cannot be assumed to hold the previous row's table: a row loads its method,
sends it and only then acquires. `setup` is the row's own choice -- the whole of
`send_phases` including the state readback, or the load and arm alone for a row whose
method the boxes have already had their setup for -- because the readback costs seconds
per box and a series of ten rows over one method pays it ten times for nothing.

**A failed row stops the series unless the row says otherwise.** The queue exists to be
left alone overnight, and an instrument that has started refusing `TBLSTRT` will refuse
it three hundred more times before morning. So the default is to stop and leave the rest
of the rows `skipped` with the reason on them; a row whose work is independent of the
last one's can say `go_on` and be walked past.

**Six states, not five.** `waiting`, `running`, `done`, `failed` and `skipped` are the
obvious ones. `stopped` is the sixth and it is not a synonym for any of them: a run the
operator stopped folded the method frame it was in and closed its files, so what it left
on disk is a short experiment rather than a broken one (`Run.stopped_early`). Calling
that `done` would claim the row acquired what it was asked for and calling it `failed`
would claim the files are no good; neither is true.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..acq import Run

__all__ = [
    "DONE",
    "FAILED",
    "FINISHED",
    "RUNNING",
    "SKIPPED",
    "STOPPED",
    "WAITING",
    "QueueRow",
    "RunQueue",
    "outcome_of",
]

WAITING = "waiting"
"""Not started. The only state a row the queue has not reached can be in."""

RUNNING = "running"
"""In flight: the row's method is being sent, or its replicates acquired."""

DONE = "done"
"""Every replicate acquired and folded, nothing warned about that ended a run."""

FAILED = "failed"
"""The send was refused, a job raised, or a run came back incomplete."""

STOPPED = "stopped"
"""The operator pressed Stop while this row was running. Its files are valid."""

SKIPPED = "skipped"
"""Never started: the series ended before the queue reached it."""

FINISHED = (DONE, FAILED, STOPPED, SKIPPED)
"""The states a row does not leave without being reset."""


@dataclass
class QueueRow:
    """One unit of an unattended series: a method, a note, and how many times.

    Mutable, unlike most of what crosses this package, because a row is edited in a
    table by a trainee while the row below it runs and copying the list on every
    keystroke buys nothing. Nothing on another thread ever sees one: the window reads a
    row on the UI thread, builds a frozen job out of it and hands that to the worker.
    """

    method_path: str
    conditions: str = ""
    replicates: int = 1
    setup: bool = True
    """Send the whole of `send_phases` before this row's first acquisition, readback
    and all, rather than the load and arm alone."""
    go_on: bool = False
    """Walk past this row if it fails, instead of ending the series."""

    state: str = WAITING
    step: str = ""
    """What the running row is doing right now: `sending` or `acquiring`. Empty for
    every other state, so the table shows one word per row and not two."""

    stems: tuple[str, ...] = field(default_factory=tuple)
    """What each replicate's files were called. The row's whole record of where its
    data went: the stem names the UIMF pair, the transcript and the send log."""
    stopped_early: str = ""
    silent_frames: int = 0
    """Repetitions that ended because the stream went quiet rather than because they
    counted out. Worth a trainee's eye per `runlog`, and worth keeping per row so a
    morning's reading of an overnight queue finds the one that was not clean."""
    problem: str = ""

    @property
    def name(self) -> str:
        """What the table calls this row: the document's name, not its path."""
        base = os.path.basename(self.method_path)
        return os.path.splitext(base)[0] or self.method_path

    @property
    def outcome(self) -> str:
        """The row's whole result in one line, or empty where it has none yet."""
        if self.state == RUNNING:
            return self.step or RUNNING
        parts: list[str] = []
        if self.stems:
            parts.append(", ".join(self.stems))
        if self.stopped_early:
            parts.append(f"stopped: {self.stopped_early}")
        if self.silent_frames:
            parts.append(f"{self.silent_frames} repetition(s) ended on the silence")
        if self.problem:
            parts.append(self.problem)
        return "; ".join(parts)

    def reset(self) -> None:
        """Back to `waiting`, with the last attempt's outcome cleared off it."""
        self.state = WAITING
        self.step = ""
        self.stems = ()
        self.stopped_early = ""
        self.silent_frames = 0
        self.problem = ""


def outcome_of(row: QueueRow, runs: Sequence[Run]) -> str:
    """Fold a row's runs into it and say which state it ended in.

    A run that came back incomplete is a failure even though it wrote files: frames that
    did not acquire, folds that did not write and a run the loop aborted are all the
    instrument telling the queue that the next eleven rows will go the same way.
    """
    row.stems = tuple(
        os.path.splitext(os.path.basename(run.raw_path))[0] for run in runs)
    row.silent_frames = sum(
        1 for run in runs for record in run.frames if record.ended_by == "silence")
    row.stopped_early = next(
        (run.stopped_early for run in runs if run.stopped_early), "") or ""
    if not runs:
        return FAILED
    if row.stopped_early:
        return STOPPED
    if any(not run.complete for run in runs):
        return FAILED
    return DONE


class RunQueue:
    """The rows, which one is in flight, and whether the series goes on.

    A plain object rather than a Qt model: the table is rebuilt from `rows` whenever
    anything changes, which for a queue of a dozen rows is cheaper than the bookkeeping
    a `QAbstractTableModel` would need to get an edit-while-running right.
    """

    def __init__(self, rows: Sequence[QueueRow] = ()) -> None:
        self.rows: list[QueueRow] = list(rows)
        self.running = False
        self.index = -1
        self.cancelled = ""
        """Why the series is ending, where something asked it to. The row in flight
        still finishes and closes itself; nothing after it starts."""

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def current(self) -> QueueRow | None:
        """The row in flight, or None between rows and when nothing is running."""
        if 0 <= self.index < len(self.rows):
            return self.rows[self.index]
        return None

    @property
    def waiting(self) -> int:
        return sum(1 for row in self.rows if row.state == WAITING)

    # -- editing, including while a row runs ---------------------------------

    def add(self, row: QueueRow, at: int | None = None) -> int:
        """Put a row in the list. Appended by default, which is what Add means."""
        where = len(self.rows) if at is None else max(0, min(at, len(self.rows)))
        self.rows.insert(where, row)
        if self.index >= where:
            self.index += 1
        return where

    def remove(self, index: int) -> bool:
        """Take a row out. Refuses the row in flight, which is already on the wire."""
        if not 0 <= index < len(self.rows) or index == self.index:
            return False
        del self.rows[index]
        if self.index > index:
            self.index -= 1
        return True

    def move(self, index: int, delta: int) -> int:
        """Shift a row up or down, and say where it ended up.

        A row cannot be moved onto or above the one in flight: the rows before it have
        been run or skipped, and a queue that let a waiting row be dragged into the past
        would be offering an order it cannot walk.
        """
        if not 0 <= index < len(self.rows) or index == self.index:
            return index
        where = index + delta
        floor = self.index + 1 if self.running and self.index >= 0 else 0
        if not floor <= where < len(self.rows):
            return index
        self.rows.insert(where, self.rows.pop(index))
        return where

    # -- walking it ----------------------------------------------------------

    def begin(self) -> QueueRow | None:
        """Start the series, and hand back the first row to run.

        Rows skipped by an earlier series are offered again -- pressing Start after a
        row failed means run the rest -- and rows that have already run are not, because
        re-running one is what `reset` is for.
        """
        for row in self.rows:
            if row.state == SKIPPED:
                row.reset()
        self.cancelled = ""
        self.running = True
        self.index = -1
        return self.advance()

    def advance(self) -> QueueRow | None:
        """The next waiting row, made running. None once the series is over."""
        if not self.cancelled:
            for index, row in enumerate(self.rows):
                if row.state == WAITING:
                    self.index = index
                    row.state = RUNNING
                    row.step = ""
                    return row
        self.running = False
        self.index = -1
        return None

    def finish(self, state: str, problem: str = "") -> bool:
        """Close the row in flight off, and say whether the series should go on."""
        row = self.current
        if row is None:
            return False
        row.state = state
        row.step = ""
        if problem:
            row.problem = problem
        if state == STOPPED:
            self.cancel(row.problem or "stopped by the operator")
        elif state == FAILED and not row.go_on:
            self.cancel(f"{row.name} failed and the series stops on a failure")
        return not self.cancelled

    def cancel(self, reason: str) -> None:
        """Skip everything that has not started. The row in flight closes itself.

        Pressing Stop during an acquisition does not abandon it -- `run_acquisition`
        ends after the current repetition and its fold -- so the running row is left
        running here and reaches `finish` when its job comes back.
        """
        self.cancelled = reason
        for row in self.rows:
            if row.state == WAITING:
                row.state = SKIPPED
                row.problem = reason

    @property
    def summary(self) -> str:
        """One line for the run log when a series ends."""
        counts = {state: sum(1 for row in self.rows if row.state == state)
                  for state in FINISHED}
        parts = [f"{counts[state]} {state}" for state in FINISHED if counts[state]]
        return f"the queue is finished: {', '.join(parts) or 'nothing ran'}"
