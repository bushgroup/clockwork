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

**Randomized passes are real rows** (lab record, task 86). `RunQueue.randomize` turns
the waiting rows into N passes, each holding every one of them once in its own shuffled
order, so a series' technical replicates are spread over it rather than acquired back to
back. The row is the unit shuffled: its replicates stay together. What comes out is
ordinary rows with a pass number on them, each with its own state, outcome and report,
so what the table shows is what runs and a pass can still be edited by hand. There is no
hidden original list; randomizing again rebuilds every pass from pass 1.

**A queue file is a plan, not a record.** `save` and `load` write and read the rows'
settings and the last seed, never their outcomes, which stay in the run log and the kept
files: every row of an opened queue is waiting.
"""

from __future__ import annotations

import os
import random
import secrets
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field

import tomli_w

from ..acq import Run

__all__ = [
    "DONE",
    "FAILED",
    "FINISHED",
    "RUNNING",
    "SKIPPED",
    "STOPPED",
    "WAITING",
    "QueueFileError",
    "QueueRow",
    "Randomized",
    "RunQueue",
    "dumps",
    "load",
    "loads",
    "outcome_of",
    "save",
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

UNSTARTED = (WAITING, SKIPPED)
"""The rows Start would run: a skipped row is offered again (`RunQueue.begin`), so a
randomization takes it with the waiting ones rather than leaving it to run twice."""


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
    stays_first: bool = False
    """Open every randomized pass with this row, ahead of the shuffled ones: a blank or
    a calibrant that has to come first. Several keep the order they were entered in."""
    pass_number: int = 0
    """Which randomized pass the row belongs to, from 1; 0 for a row of a queue that was
    never randomized, or one added by hand afterwards."""
    pass_count: int = 0
    """How many passes that randomization made, so the table can say `2/3`."""

    state: str = WAITING
    step: str = ""
    """What the running row is doing right now: `sending` or `acquiring`. Empty for
    every other state, so the table shows one word per row and not two."""

    stems: tuple[str, ...] = field(default_factory=tuple)
    """What each replicate's files were called. The row's whole record of where its
    data went: the stem names the UIMF pair, the transcript and the send log."""
    directory: str = ""
    """Where those files are: the folder the row's runs wrote to, for a report of this
    row made after later rows have run (lab #2)."""
    stopped_early: str = ""
    retried_frames: int = 0
    """Repetitions that came up short and were acquired again (`Run.retried`). The
    files are whole either way, but a morning's reading of an overnight queue should
    find the row where the console lost data (lab record, task 82)."""
    console_errors: int = 0
    """`[error]` lines the console logged during the row's runs (`Run.console_errors`)."""
    problem: str = ""

    @property
    def pass_label(self) -> str:
        """`k/N` for a row of a randomized pass, empty for any other."""
        return f"{self.pass_number}/{self.pass_count}" if self.pass_number else ""

    @property
    def reportable(self) -> bool:
        """Whether "Report this run" has anything to keep: a finished row with files."""
        return self.state in (DONE, FAILED, STOPPED) and bool(self.stems)

    @property
    def name(self) -> str:
        """What the table and the run log call this row: the document's name, not its path.

        A library document is `<folder>/method.toml`, so its basename names nothing; the
        folder does. Read off the path rather than the document's `[metadata]`, because
        the document is read when the row starts, not when it is named.
        """
        stem = os.path.splitext(os.path.basename(self.method_path))[0]
        if stem.lower() == "method":
            folder = os.path.basename(os.path.dirname(self.method_path))
            return folder or stem
        return stem or self.method_path

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
        if self.retried_frames:
            parts.append(f"{self.retried_frames} repetition(s) acquired again")
        if self.console_errors:
            plural = "s" if self.console_errors != 1 else ""
            parts.append(f"console reported {self.console_errors} error{plural}")
        if self.problem:
            parts.append(self.problem)
        return "; ".join(parts)

    def reset(self) -> None:
        """Back to `waiting`, with the last attempt's outcome cleared off it."""
        self.state = WAITING
        self.step = ""
        self.stems = ()
        self.directory = ""
        self.stopped_early = ""
        self.retried_frames = 0
        self.console_errors = 0
        self.problem = ""


def outcome_of(row: QueueRow, runs: Sequence[Run]) -> str:
    """Fold a row's runs into it and say which state it ended in.

    A run that came back incomplete is a failure even though it wrote files: frames that
    did not acquire, folds that did not write and a run the loop aborted are all the
    instrument telling the queue that the next eleven rows will go the same way.
    """
    row.stems = tuple(
        os.path.splitext(os.path.basename(run.raw_path))[0] for run in runs)
    row.directory = next(
        (os.path.dirname(run.raw_path) for run in runs if run.raw_path), "")
    row.retried_frames = sum(len(run.retried) for run in runs)
    row.console_errors = sum(run.console_errors for run in runs)
    row.stopped_early = next(
        (run.stopped_early for run in runs if run.stopped_early), "") or ""
    if not runs:
        return FAILED
    if row.stopped_early:
        return STOPPED
    if any(not run.complete for run in runs):
        return FAILED
    return DONE


@dataclass(frozen=True)
class Randomized:
    """What `RunQueue.randomize` did, for the run log: enough to reproduce and audit it."""

    seed: int
    passes: int
    reshuffle: bool
    order: tuple[tuple[str, ...], ...]
    """Each pass's rows by name, and note where there is one, in the order they run."""

    @property
    def line(self) -> str:
        how = "a new order each pass" if self.reshuffle else "one order repeated"
        rows = len(self.order[0]) if self.order else 0
        passes = "; ".join(f"pass {number}: {', '.join(names)}"
                           for number, names in enumerate(self.order, start=1))
        return (f"the queue is randomized: {rows} row(s) in {self.passes} pass(es), "
                f"{how}, seed {self.seed} -- {passes}")


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
        self.seed: int | None = None
        """The seed of the last randomization, which a saved queue file carries."""

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

    @property
    def to_run(self) -> int:
        """The rows Start would run: the waiting ones and the skipped ones (`begin`)."""
        return sum(1 for row in self.rows if row.state in UNSTARTED)

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

    # -- randomized passes --------------------------------------------------

    @property
    def passes(self) -> int:
        """How many passes the last randomization made; 0 when there are none."""
        return max((row.pass_count for row in self.rows if row.pass_number), default=0)

    def template(self) -> list[QueueRow]:
        """The rows a randomization is built from, in table order.

        Pass 1 when the queue has passes -- "pass 1 is the template", so an edit made
        only to a later pass's copy is not carried -- together with any waiting row
        added by hand since, which has no pass and joins every new one. A queue never
        randomized offers its waiting rows; its finished rows are the record of what
        ran and are not offered again.
        """
        if self.passes:
            return [row for row in self.rows
                    if row.pass_number == 1
                    or (not row.pass_number and row.state in UNSTARTED)]
        return [row for row in self.rows if row.state in UNSTARTED]

    def randomize(self, passes: int, reshuffle: bool = True,
                  seed: int | None = None) -> Randomized:
        """Replace the unstarted rows with `passes` passes of the template, shuffled.

        Each pass holds a fresh copy of every template row once: its `stays_first` rows
        open the pass in template order and the rest follow in an order drawn from
        `seed`, drawn again for each pass unless `reshuffle` is off. Finished rows are
        left where they are, above the new passes. Refused while the queue runs, which
        keeps `move`'s rule -- nothing on or above the row in flight -- true without a
        second case.
        """
        if self.running:
            raise ValueError("a running queue cannot be randomized")
        if passes < 1:
            raise ValueError("a randomization needs at least one pass")
        template = self.template()
        if not template:
            raise ValueError("there are no waiting rows to randomize")
        if seed is None:
            seed = secrets.randbits(32)
        draw = random.Random(seed)
        first = [row for row in template if row.stays_first]
        rest = [row for row in template if not row.stays_first]
        order = list(rest)
        built: list[QueueRow] = []
        names: list[tuple[str, ...]] = []
        for number in range(1, passes + 1):
            if reshuffle or number == 1:
                order = list(rest)
                draw.shuffle(order)
            copies = [_fresh(row, number, passes) for row in first + order]
            built.extend(copies)
            names.append(tuple(_described(row) for row in copies))
        self.rows = [row for row in self.rows if row.state not in UNSTARTED] + built
        self.index = -1
        self.seed = seed
        return Randomized(seed=seed, passes=passes, reshuffle=reshuffle,
                          order=tuple(names))

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


def _fresh(row: QueueRow, number: int, count: int) -> QueueRow:
    """A waiting copy of a row's settings, in pass `number` of `count`."""
    return QueueRow(method_path=row.method_path, conditions=row.conditions,
                    replicates=row.replicates, setup=row.setup, go_on=row.go_on,
                    stays_first=row.stays_first, pass_number=number, pass_count=count)


def _described(row: QueueRow) -> str:
    return f"{row.name} ({row.conditions})" if row.conditions else row.name


# -- the queue file ------------------------------------------------------------------

QUEUE_SCHEMA = 1
"""The queue file's own version, apart from the method document's."""

QUEUE_SUFFIX = ".queue.toml"
"""What a queue file is called, so the method library can tell one from a method."""


_KINDS = {str: "text", int: "a whole number", bool: "true or false"}


class QueueFileError(ValueError):
    """A queue file that could not be read, with the reason a trainee can act on."""


def dumps(queue: RunQueue, directory: str = "") -> str:
    """The queue as TOML: each row's settings and the last seed, no outcomes.

    Every method is written by its absolute path and, where one exists, by its path
    relative to `directory` (the queue file's folder), so a queue copied to another PC
    with its methods beside it opens by the second when the first is gone.
    """
    data: dict[str, object] = {"schema_version": QUEUE_SCHEMA}
    if queue.seed is not None:
        data["seed"] = queue.seed
    rows = []
    for row in queue.rows:
        path = os.path.abspath(row.method_path)
        entry: dict[str, object] = {"method": path.replace(os.sep, "/")}
        if directory:
            try:
                entry["relative"] = os.path.relpath(path, directory).replace(os.sep, "/")
            except ValueError:
                pass  # another drive on Windows: there is no relative path
        entry.update(conditions=row.conditions, replicates=row.replicates,
                     setup=row.setup, go_on=row.go_on, stays_first=row.stays_first)
        if row.pass_number:
            entry.update({"pass": row.pass_number, "passes": row.pass_count})
        rows.append(entry)
    data["rows"] = rows
    return tomli_w.dumps(data)


def loads(text: str, directory: str = "") -> RunQueue:
    """A queue from `dumps`' TOML, every row waiting.

    `directory` resolves the relative paths. A method found by neither path keeps its
    absolute one, so its row fails when it starts and says why, as a row whose file was
    moved after it was added always has.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise QueueFileError(f"this is not a queue file: {exc}") from exc
    if data.get("schema_version") != QUEUE_SCHEMA:
        raise QueueFileError(
            f"this is not a queue file this version reads (schema_version "
            f"{data.get('schema_version')!r}, expected {QUEUE_SCHEMA})")
    seed = data.get("seed")
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)):
        raise QueueFileError("the seed is not a whole number")
    entries = data.get("rows", [])
    if not isinstance(entries, list):
        raise QueueFileError("`rows` is not a list of rows")
    queue = RunQueue([_row(number, entry, directory)
                      for number, entry in enumerate(entries, start=1)])
    queue.seed = seed
    return queue


def _row(number: int, entry: object, directory: str) -> QueueRow:
    if not isinstance(entry, dict):
        raise QueueFileError(f"row {number} is not a table")

    def value(key: str, kind: type, default: object):  # noqa: ANN202
        found = entry.get(key, default)
        if not isinstance(found, kind) or (kind is int and isinstance(found, bool)):
            raise QueueFileError(f"row {number}: `{key}` is not {_KINDS[kind]}")
        return found

    path = value("method", str, "")
    if not path:
        raise QueueFileError(f"row {number} names no method")
    relative = value("relative", str, "")
    if not os.path.isfile(path) and relative and directory:
        beside = os.path.normpath(os.path.join(directory, relative))
        if os.path.isfile(beside):
            path = beside
    path = os.path.normpath(path)
    replicates = value("replicates", int, 1)
    pass_number = value("pass", int, 0)
    pass_count = value("passes", int, 0)
    if replicates < 1:
        raise QueueFileError(f"row {number}: `replicates` is less than 1")
    if pass_number and not 1 <= pass_number <= pass_count:
        raise QueueFileError(f"row {number}: pass {pass_number} of {pass_count}")
    return QueueRow(method_path=path, conditions=value("conditions", str, ""),
                    replicates=replicates, setup=value("setup", bool, True),
                    go_on=value("go_on", bool, False),
                    stays_first=value("stays_first", bool, False),
                    pass_number=pass_number,
                    pass_count=pass_count if pass_number else 0)


def save(queue: RunQueue, path: str) -> None:
    """Write the queue to `path`, its methods named relative to the file's folder too."""
    text = dumps(queue, os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def load(path: str) -> RunQueue:
    """Read a queue file: `OSError` for one that will not open, `QueueFileError` for
    one that opens and is not a queue."""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    return loads(text, os.path.dirname(os.path.abspath(path)))
