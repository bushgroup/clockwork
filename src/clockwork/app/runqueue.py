"""The list of methods a window runs in order, and what it is told back about them.

The Qt-free half of the run queue (lab record, task 53, and the window design's
decision 10). A row is a method document, a sample or conditions note and a replicate
count; the queue is those rows and what each of them did. `clockwork.app.queuepanel`
is the table that draws it. Nothing here knows about a widget, a worker or a wire.

**A started queue is a snapshot, run by the owner** (the window revision's R9 and F1-F3;
lab record, task 168). Start reads and hashes every row's document and hands the owner
the whole of it as one `SeriesJob` whose points are saved documents, so a queue
outlives the window that started it, leaves a run record, stamps its files with a
series id, index, position and seed, and is followed, paused, guarded and stopped as a
series plan is. Before 1.2.0 the window was the queue's sequencer: it loaded each row
through the panes when the row started and sent a `Send` and an `Acquire` for it, and the
queue died with the window. What the window keeps here is the table, and the job's
progress written back onto its rows (`start`, `point_started`, `run_done`, `ended`).

**A row is read at Start, not when it was added.** An overnight series set up at five
o'clock and started at seven runs what the files said at seven. Changing course is a
stop, then the rows not yet run started as a new queue, whose run record names the one
before it (`follows`); a running queue is not edited.

**A failed row ends the queue**, as a failed point ends a series (F3). The per-row "go on
if it fails" and `setup` flags are gone: the owner sends `setup` only where it differs
from what it last sent. A queue file written before 1.2.0 still opens, the two values
ignored (`RunQueue.ignored`).

**Six states, not five.** `waiting`, `running`, `done`, `failed` and `skipped` are the
obvious ones. `stopped` is the sixth and it is not a synonym for any of them: a run the
operator stopped folded the method frame it was in and closed its files, so what it left
on disk is a short experiment rather than a broken one (`Run.stop_asked`). Calling
that `done` would claim the row acquired what it was asked for and calling it `failed`
would claim the files are no good; neither is true.

**Randomized passes are real rows** (lab record, task 86). `RunQueue.randomize` turns
the waiting rows into N passes, each holding every one of them once in its own shuffled
order, so a series' technical replicates are spread over it rather than acquired back to
back. The row is the unit shuffled: its replicates stay together. What comes out is
ordinary rows with a pass number on them, so what the table shows is what runs. The seed
is drawn as a plan's is (`clockwork.series.draw_seed`), under the Int32 every file of the
started queue stamps it as.

**A queue file is a plan, not a record.** `save` and `load` write and read the rows'
settings and the last seed, never their outcomes, which are in the run record and the
files: every row of an opened queue is waiting.

**The beam guard is not the queue's** (lab record, task 168; before it, task 134). It is
the owner's, armed by a person at the window on whatever runs; a row whose file it ends
holds the queue for a person, whose Resume acquires that row again from its start
(`QueueRow.replaced`).
"""

from __future__ import annotations

import os
import random
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field

import tomli_w

from ..acq import Run
from ..series import draw_seed

__all__ = [
    "DONE",
    "FAILED",
    "FINISHED",
    "RUNNING",
    "SKIPPED",
    "STOPPED",
    "WAITING",
    "RETIRED",
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
"""In flight: the row's method is being sent, or its replicates acquired, or the queue
rests on it."""

DONE = "done"
"""Every replicate acquired and folded, nothing warned about that ended a run."""

FAILED = "failed"
"""The send was refused, a job raised, or a run came back incomplete."""

STOPPED = "stopped"
"""The operator pressed Stop while this row was running. Its files are valid."""

SKIPPED = "skipped"
"""Never started: the queue ended before it reached it. Start offers it again, as a new
queue whose record names the one that skipped it."""

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
    """What the running row is doing right now, such as `acquiring again` after the
    beam guard. Empty for every other state, so the table shows one word per row."""

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
    beam_lost: str = ""
    """The beam guard's sentence, where it ended one of the row's runs (`Run.beam_lost`)."""
    problem: str = ""
    replaced: tuple[str, ...] = field(default_factory=tuple)
    """The stems of files the beam guard ended and a retake of this row replaced: on
    disk and stamped `failed`, and not among `stems`, which are the row's own."""

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
        if self.beam_lost:
            parts.append(self.beam_lost)
        elif self.stopped_early:
            parts.append(f"stopped: {self.stopped_early}")
        if self.replaced:
            parts.append(f"acquired again after the beam guard ended "
                         f"{', '.join(self.replaced)}")
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
        self.beam_lost = ""
        self.problem = ""
        self.replaced = ()


def outcome_of(row: QueueRow, runs: Sequence[Run]) -> str:
    """Fold a row's runs into it and say which state it ended in.

    A run that came back incomplete is a failure even though it wrote files: frames that
    did not acquire, folds that did not write and a run the loop aborted are all the
    instrument telling the queue that the next eleven rows will go the same way. So is a
    run the loop ended early for a reason of its own -- three frames failed in a row, a
    repetition damaged twice, a gate never seen to come down -- whose reason becomes the
    row's problem; only a run whose caller asked it to stop (`Run.stop_asked`) is
    `stopped` (lab record, task 134). A run the beam guard ended is one of the loop's own,
    and keeps its sentence in `beam_lost`; the owner holds the queue on it, and a run
    that a retake replaced is not among `runs`.
    """
    row.stems = tuple(
        os.path.splitext(os.path.basename(run.raw_path))[0] for run in runs)
    row.directory = next(
        (os.path.dirname(run.raw_path) for run in runs if run.raw_path), "")
    row.retried_frames = sum(len(run.retried) for run in runs)
    row.console_errors = sum(run.console_errors for run in runs)
    row.beam_lost = next((run.beam_lost for run in runs if run.beam_lost), "") or ""
    ended = next((run for run in runs if run.stopped_early), None)
    row.stopped_early = (ended.stopped_early or "") if ended and ended.stop_asked else ""
    if not runs or row.beam_lost:
        return FAILED
    if row.stopped_early:
        return STOPPED
    if ended is not None:
        row.problem = ended.stopped_early or ""
        return FAILED
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
    """The rows, the queue running in the owner, and what each row did.

    A plain object rather than a Qt model: the table is rebuilt from `rows` whenever
    anything changes, which for a queue of a dozen rows is cheaper than the bookkeeping
    a `QAbstractTableModel` would need.
    """

    def __init__(self, rows: Sequence[QueueRow] = ()) -> None:
        self.rows: list[QueueRow] = list(rows)
        self.running = False
        """Whether this window's queue job is running in the owner. The table is the
        snapshot's while it is, and is not edited."""
        self.started: list[QueueRow] = []
        """The rows the running or last queue job was started with, in its points'
        order: point `k` is `started[k - 1]`."""
        self.seed: int | None = None
        """The seed of the last randomization, which a saved queue file carries and a
        started queue stamps (`stamped_seed`)."""
        self.path = ""
        """The queue file the rows were last saved to or opened from, or empty."""
        self.request_id = ""
        """The run record of the queue job running, or of the last one, once the owner
        has begun it."""
        self.follows = ""
        """The request id of the queue whose unrun rows these are: a stopped or failed
        queue leaves its rows skipped, and the next Start is a new queue that names it."""
        self.ignored = 0
        """Rows of an opened file that carried the `setup` or `go_on` an earlier release
        wrote, which are ignored now (F3)."""

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def waiting(self) -> int:
        return sum(1 for row in self.rows if row.state == WAITING)

    @property
    def to_run(self) -> int:
        """The rows Start would run: the waiting ones and the skipped ones (`begin`)."""
        return sum(1 for row in self.rows if row.state in UNSTARTED)

    # -- editing, while nothing runs -------------------------------------------

    def add(self, row: QueueRow, at: int | None = None) -> int:
        """Put a row in the list. Appended by default, which is what Add means."""
        where = len(self.rows) if at is None else max(0, min(at, len(self.rows)))
        self.rows.insert(where, row)
        return where

    def remove(self, index: int) -> bool:
        """Take a row out. Refused while the queue runs: its rows are the snapshot."""
        if self.running or not 0 <= index < len(self.rows):
            return False
        del self.rows[index]
        return True

    def move(self, index: int, delta: int) -> int:
        """Shift a row up or down, and say where it ended up; nothing moves while the
        queue runs."""
        where = index + delta
        if (self.running or not 0 <= index < len(self.rows)
                or not 0 <= where < len(self.rows)):
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
        left where they are, above the new passes. Refused while the queue runs.
        """
        if self.running:
            raise ValueError("a running queue cannot be randomized")
        if passes < 1:
            raise ValueError("a randomization needs at least one pass")
        template = self.template()
        if not template:
            raise ValueError("there are no waiting rows to randomize")
        if seed is None:
            seed = draw_seed()
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
        self.seed = seed
        return Randomized(seed=seed, passes=passes, reshuffle=reshuffle,
                          order=tuple(names))

    @property
    def stamped_seed(self) -> int | None:
        """The seed a started queue stamps: the last randomization's, while the queue
        still has its passes; None for an order nobody shuffled."""
        return self.seed if self.passes else None

    # -- what the owner reports back ---------------------------------------------

    def begin(self) -> list[QueueRow]:
        """The rows Start would run, in table order, skipped ones made waiting again:
        pressing Start after a queue stopped means run the rest. Rows that have run are
        not offered, because running one again is what `reset` is for."""
        for row in self.rows:
            if row.state == SKIPPED:
                row.reset()
        return [row for row in self.rows if row.state == WAITING]

    def start(self, rows: Sequence[QueueRow]) -> None:
        """The owner has been handed these rows, in this order, as one queue job."""
        self.started = list(rows)
        self.running = True
        self.request_id = ""
        self.follows = ""  # carried by the job; a later queue names its own

    def row(self, number: int) -> QueueRow | None:
        """Point `number` of the job (from 1) as its row, or None."""
        return self.started[number - 1] if 1 <= number <= len(self.started) else None

    def point_started(self, number: int, retake: int = 0) -> QueueRow | None:
        """Point `number` has begun: its row runs, and a row before it still running
        has every file it was asked for. A retake sets aside the files the beam guard
        ended, which it replaces."""
        for row in self.started[: max(0, number - 1)]:
            if row.state == RUNNING:
                row.state, row.step = DONE, ""
        row = self.row(number)
        if row is None:
            return None
        if retake:
            row.replaced += row.stems
            row.stems = ()
            row.beam_lost = ""
        row.state = RUNNING
        row.step = "acquiring again" if retake else ""
        return row

    def run_done(self, number: int, run: Run) -> None:
        """One of point `number`'s files is written: the row keeps its stem."""
        row = self.row(number)
        if row is None:
            return
        stem = _stem(run)
        if stem and stem not in row.stems:
            row.stems += (stem,)
        row.directory = row.directory or os.path.dirname(run.raw_path)
        if run.beam_lost:
            row.beam_lost = run.beam_lost

    def ended(self, runs: Sequence[Run], numbers: Sequence[int],
              replaced: Sequence[str] = (), stopped: str = "") -> None:
        """The queue job finished: each row's state from its own files.

        `numbers` is each run's point, as `SeriesResult.numbers`; with none, from an
        owner older than it, the stems the rows kept as the files came are used. A row
        with files is judged by `outcome_of`, and one a stop cut short of its replicates
        is `stopped`; one never reached is `skipped`."""
        gone = set(replaced)
        for number, row in enumerate(self.started, start=1):
            row.step = ""
            if numbers:
                ours = [run for run, place in zip(runs, numbers, strict=False)
                        if place == number]
                mine = [run for run in ours if _stem(run) not in gone]
                row.replaced = tuple(_stem(run) for run in ours if _stem(run) in gone)
            else:
                mine = [run for run in runs if _stem(run) in row.stems]
            if mine:
                row.problem = ""
                row.state = outcome_of(row, mine)
                if row.state == DONE and len(mine) < row.replicates and stopped:
                    row.state, row.stopped_early = STOPPED, stopped
            elif row.state == RUNNING and stopped:
                row.state, row.problem = STOPPED, stopped
            else:
                row.state = SKIPPED
                row.problem = (f"the queue stopped before this row: {stopped}" if stopped
                               else "the queue ended before this row")
        self._over()

    def failed(self, message: str) -> None:
        """The queue job failed: the row running fails with the job's sentence, as a
        failed point ends a series (F3), and every row after it is skipped."""
        failed = ""
        for row in self.started:
            if row.state == RUNNING and not failed:
                row.state, row.step, row.problem = FAILED, "", message
                failed = row.name
            elif row.state in (WAITING, RUNNING):
                row.state, row.step = SKIPPED, ""
                row.problem = (f"{failed} failed, and a failed row ends the queue"
                               if failed else f"the queue failed: {message}")
        self._over()

    def _over(self) -> None:
        self.running = False
        if any(row.state == SKIPPED for row in self.started) and self.request_id:
            # The rows left unrun, started again, are a new queue naming this one (F1).
            self.follows = self.request_id

    @property
    def summary(self) -> str:
        """One line for the run log when a queue ends."""
        rows = self.started or self.rows
        counts = {state: sum(1 for row in rows if row.state == state)
                  for state in FINISHED}
        parts = [f"{counts[state]} {state}" for state in FINISHED if counts[state]]
        return f"the queue is finished: {', '.join(parts) or 'nothing ran'}"


def _stem(run: Run) -> str:
    return os.path.splitext(os.path.basename(run.raw_path))[0]


def _fresh(row: QueueRow, number: int, count: int) -> QueueRow:
    """A waiting copy of a row's settings, in pass `number` of `count`."""
    return QueueRow(method_path=row.method_path, conditions=row.conditions,
                    replicates=row.replicates, stays_first=row.stays_first,
                    pass_number=number, pass_count=count)


def _described(row: QueueRow) -> str:
    return f"{row.name} ({row.conditions})" if row.conditions else row.name


# -- the queue file ------------------------------------------------------------------

QUEUE_SCHEMA = 1
"""The queue file's own version, apart from the method document's.

Still 1 after the `setup` and `go_on` keys stopped being written (lab record, task 168):
an earlier release reads a file without them as `setup` true and `go_on` false, which is
what every queue now does, and this one reads a file with them and ignores both."""

RETIRED = ("setup", "go_on")
"""Row keys an earlier release wrote and this one ignores (F3)."""

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
                     stays_first=row.stays_first)
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
    queue.ignored = sum(1 for entry in entries
                        if isinstance(entry, dict) and any(key in entry for key in RETIRED))
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
    for key, default in (("setup", True), ("go_on", False)):
        value(key, bool, default)  # ignored, but a value that is not true or false is said
    return QueueRow(method_path=path, conditions=value("conditions", str, ""),
                    replicates=replicates,
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
    queue = loads(text, os.path.dirname(os.path.abspath(path)))
    queue.path = os.path.abspath(path)
    return queue
