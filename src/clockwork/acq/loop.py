"""One whole acquisition, from a method to the files it leaves behind.

`session.py` knows the console and nothing else; `uimf.py` knows the files and nothing
else; `clockwork.mips` knows the boxes. This module is the one place that knows the
order all three happen in, which is `docs/method-file-format.md` and the lab record's
architecture note put together:

    send_phases(method, boxes)                 setup, load, arm -- nothing starts yet
    run_acquisition(method, boxes=..., ...)    the files, the frames, the fold
    run_acquisition(..., replicate=True)       the reset list, then the same again

Both calls block, both are meant for a worker thread, and neither imports Qt. A window
wraps them one per button and shows what the progress callback reports; a bench script
calls them in a row. Nothing above this layer should hold a second copy of the sequence,
which is why this exists at all (lab record, task 23).

**The order per console frame is not a style.** `acquire frame` goes to the console
first, with the digitizer's enable still low, and the method's `start` list is walked
afterwards with `TBLSTRT` last. The enable on Control I/O 2 is a level the card samples
at each trigger rather than an edge it arms on, so a frame released before it was asked
for begins on whatever push follows the request, and the tens of milliseconds the start
list costs in serial round trips become an offset instead of being invisible (lab
record, task 05).

**A frame is over when the stream falls silent, not when it says `finished`.** The
console publishes batches from a subscriber thread and `finished` from the acquisition
thread, and its writer is still inserting rows for a frame whose end has already gone
out -- measured as much as 9.5 seconds later. So the completion marker is written after
a silence, not at `finished`, and a fold that ran any earlier would fold a fraction of
its frame (lab record, tasks 20 and 18).

**Three commands are read rather than relayed**, and only three. `STBLDAT` is streamed
in paced chunks instead of written in one go, because the box has no flow control and
silently drops what overruns its input buffer; `SMOD,LOC` treats the box's "already
local" rejection as success; and an `SMOD` into any other mode is followed by a wait for
`TBLRDY`. All three are facts `docs/mips-wire-format.md` states and `clockwork.mips`
already implements. Every other string in a method reaches its box exactly as written.

What the loop refuses to do at all is `repetition_mode = "single_frame"` with more than
one method frame: that method's table raises the enable and never lowers it, so the
second frame's `acquire frame` meets a gate that is already high. See
`AcquisitionRefused`.
"""

from __future__ import annotations

import dataclasses
import math
import os
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from ..instrument import UNCALIBRATED, Instrument
from ..method import Method, Step
from ..mips import Box, MipsError, TableEvent
from .console import Console
from .session import EMPTY_SETTLE_S, run_frame, start_chain
from .stream import DataStream, StreamTimeout
from .uimf import Geometry, Recording
from .wire import (
    SECONDS_PER_SAMPLE_2GSPS,
    AcqError,
    Batch,
    ConsoleAcquisitionError,
    EmptyFrameError,
    Status,
    TofWidth,
)

__all__ = [
    "ABORT_AFTER_FAILURES",
    "ARM_TIMEOUT_S",
    "FRAME_TIMEOUT_FLOOR_S",
    "FRAME_TIMEOUT_SLACK",
    "SILENCE_S",
    "AcquisitionRefused",
    "BatchSeen",
    "BoxReady",
    "BoxSaid",
    "EnableGateError",
    "Event",
    "FoldRecord",
    "Folded",
    "FrameBegun",
    "FrameEnded",
    "FrameRecord",
    "PhaseSent",
    "Run",
    "RunBegun",
    "Warned",
    "refusals",
    "run_acquisition",
    "send_phases",
]

SILENCE_S = 6.0
"""How long the data stream has to be quiet before a frame is taken to be over.

Most of a frame's batches arrive after that frame's own `finished` and at full occupancy
all of them do, the first measured 0.4 to 0.9 s after the end and the last as much as
9.5 s after it (lab record, task 20). Six seconds is generous against the longest gap
*between* messages rather than against that total, because the wait restarts on every
one. Stopping earlier reports a frame as short when it was only slow, and writes the
completion marker onto a frame the console is still filling.
"""

ARM_TIMEOUT_S = 5.0
"""How long to wait for `TBLRDY` after an `SMOD` into table mode.

The box answers the command immediately and emits the status line on its own a moment
later. Generous against a USB round trip and short enough that a box which did not arm
is reported rather than waited on.
"""

FRAME_TIMEOUT_FLOOR_S = 30.0
FRAME_TIMEOUT_SLACK = 2.0
"""How a frame's timeout is derived when the caller does not give one.

A frame takes `frame_length` pusher pulses and the pusher is ~129 us, so the wait has to
scale with the frame: the instrument's `per_repetition` frame is 0.65 s and its
`single_frame` one is 64.5 s, and one fixed number cannot serve both. Twice the expected
duration plus the floor leaves room for the console's own restart and for a pusher
slower than the period measured at `acquire`, and still fails a frame that never starts
rather than hanging on it.
"""

ABORT_AFTER_FAILURES = 3
"""Consecutive failed frames after which a run gives up.

A failure is an outcome and not an escape: one dead frame among a hundred is recorded
and the run goes on, because the ninety-nine are worth having. But a console that has
died fails every frame the same way, and a hundred repetitions of a 60-second timeout is
an hour of a trainee's afternoon spent proving it. Three in a row is a broken run.
Pass `abort_after=None` to let a run try every frame it was asked for.
"""


# --- what goes wrong -----------------------------------------------------------------


class AcquisitionRefused(AcqError):
    """The method describes an acquisition this loop will not run.

    Raised before anything is sent, so a refused method costs nothing and leaves nothing
    behind. `refusals(method)` is the same judgement without the exception, for a window
    that wants to grey a button out rather than catch something.
    """


class EnableGateError(AcqError):
    """The digitizer was recording before the frame was released.

    The one failure on this instrument that produces a full frame of plausible data at
    the wrong offset, from either of two causes that look identical afterwards: a table
    that left the enable high behind the previous frame, or the enable lead off the
    card, whose input is pulled up so that an unconnected Control I/O 2 reads high and
    the card acquires every push (lab record, tasks 05 and 18).
    """


# --- what the loop says while it works ------------------------------------------------


@dataclass(frozen=True, slots=True)
class Event:
    """Something the loop did, reported as it happened.

    Handed to the `progress` callback on the loop's own thread, so a window's handler
    posts it to the UI thread and returns. Every event carries a `text` fit for a status
    line, and the fields behind it for anything that wants more.
    """

    @property
    def text(self) -> str:
        return type(self).__name__


@dataclass(frozen=True, slots=True)
class Warned(Event):
    """Something the method said about itself on the way in, passed straight through."""

    message: str

    @property
    def text(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class BoxReady(Event):
    """A box answered `GVER`, so the port has the right thing on the end of it."""

    box: str
    port: str
    version: str

    @property
    def text(self) -> str:
        return f"{self.box} on {self.port}: firmware {self.version}"


@dataclass(frozen=True, slots=True)
class PhaseSent(Event):
    """One string, to one box, in one phase. `error` is set if the box refused it."""

    box: str
    phase: str
    command: str
    seconds: float
    detail: str = ""
    error: str | None = None

    @property
    def text(self) -> str:
        shown = self.command if len(self.command) <= 48 else self.command[:45] + "..."
        if self.error is not None:
            return f"{self.box} {self.phase}: {shown} refused: {self.error}"
        return f"{self.box} {self.phase}: {shown} {self.detail}".rstrip()


@dataclass(frozen=True, slots=True)
class RunBegun(Event):
    """The files exist and the console holds a chain; frames follow."""

    raw_path: str
    summed_path: str
    frames: int
    console_frames: int
    frame_length: int
    frame_timeout: float

    @property
    def text(self) -> str:
        total = self.frames * self.console_frames
        return (f"{os.path.basename(self.raw_path)}: {total} console frames of "
                f"{self.frame_length} scans")


@dataclass(frozen=True, slots=True)
class FrameBegun(Event):
    """A frame's parameters are written and the console has been asked for it."""

    method_frame: int
    repetition: int
    frame_number: int
    of: int

    @property
    def text(self) -> str:
        return (f"frame {self.method_frame}, repetition {self.repetition} of {self.of} "
                f"(file frame {self.frame_number})")


@dataclass(frozen=True, slots=True)
class BoxSaid(Event):
    """A status line a box emitted on its own: `TBLTRIG`, `TBLCMPLT`, `TBLRDY`."""

    box: str
    event: str

    @property
    def text(self) -> str:
        return f"{self.box}: {self.event}"


@dataclass(frozen=True, slots=True)
class BatchSeen(Event):
    """One published batch, while the frame it belongs to is still running."""

    method_frame: int
    repetition: int
    batch: Batch

    @property
    def text(self) -> str:
        return f"frame {self.method_frame}.{self.repetition}: {self.batch.scans} scans"


@dataclass(frozen=True, slots=True)
class FrameEnded(Event):
    """A frame is over, its stream silent and its completion marker written."""

    record: FrameRecord

    @property
    def text(self) -> str:
        return self.record.text


@dataclass(frozen=True, slots=True)
class Folded(Event):
    """A method frame's repetitions are summed into the companion, or failed to be."""

    record: FoldRecord

    @property
    def text(self) -> str:
        return self.record.text


# --- what the loop hands back ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FrameRecord:
    """One console frame, as it turned out."""

    method_frame: int
    repetition: int
    frame_number: int
    outcome: str
    """`"acquired"`, or the name of the exception that ended it."""

    detail: str = ""
    batches: int = 0
    scans_published: int = 0
    scans_after_finished: int = 0
    trailing_batches: int = 0
    rows_at_finished: int | None = None
    rows_after_silence: int | None = None
    started_s: float = 0.0
    """When the frame began, on the run's clock."""

    seconds: float = 0.0
    silence_seconds: float = 0.0

    @property
    def acquired(self) -> bool:
        return self.outcome == "acquired"

    @property
    def writer_lag_rows(self) -> int | None:
        """Rows the console's writer had not yet inserted when it said `finished`.

        The fold's whole timing constraint, and nothing else in the system measures it.
        None where either count could not be read.
        """
        if self.rows_at_finished is None or self.rows_after_silence is None:
            return None
        return self.rows_after_silence - self.rows_at_finished

    @property
    def text(self) -> str:
        late = ""
        if self.scans_after_finished:
            late = f", {self.scans_after_finished} of them after its own finished"
        lag = self.writer_lag_rows
        rows = f", {lag} rows written after it" if lag else ""
        if not self.acquired:
            return (f"frame {self.method_frame}.{self.repetition}: {self.outcome}: "
                    f"{self.detail}")
        return (f"frame {self.method_frame}.{self.repetition}: "
                f"{self.scans_published} scans in {self.seconds:.3f} s{late}{rows}")


@dataclass(frozen=True, slots=True)
class FoldRecord:
    """One method frame's fold into the summed companion."""

    method_frame: int
    frames_folded: tuple[int, ...]
    rows: int = 0
    seconds: float = 0.0
    error: str | None = None

    @property
    def text(self) -> str:
        if self.error is not None:
            return f"frame {self.method_frame}: the fold failed: {self.error}"
        return (f"frame {self.method_frame}: {len(self.frames_folded)} repetitions "
                f"folded into {self.rows} rows in {self.seconds:.3f} s")


@dataclass(frozen=True, slots=True)
class Run:
    """Everything one acquisition produced, including the parts that did not work."""

    method: Method
    raw_path: str
    summed_path: str
    frames: tuple[FrameRecord, ...]
    folds: tuple[FoldRecord, ...]
    warnings: tuple[str, ...]
    seconds: float
    replicate: bool = False
    stopped_early: str | None = None
    raw_kept: bool = True
    """Whether the per-repetition file survived the close, which `keep_raw` decides."""

    @property
    def failures(self) -> tuple[FrameRecord, ...]:
        return tuple(record for record in self.frames if not record.acquired)

    @property
    def complete(self) -> bool:
        """Every frame the method asked for acquired, every fold written."""
        expected = self.method.acquisition.frames * self.method.acquisition.console_frames
        return (self.stopped_early is None
                and len(self.frames) == expected
                and not self.failures
                and len(self.folds) == self.method.acquisition.frames
                and all(fold.error is None for fold in self.folds))

    @property
    def scans_published(self) -> int:
        return sum(record.scans_published for record in self.frames)

    @property
    def text(self) -> str:
        name = os.path.basename(self.summed_path if not self.raw_kept else self.raw_path)
        if self.stopped_early is not None:
            return f"{name}: stopped after {len(self.frames)} frames: {self.stopped_early}"
        failed = len(self.failures)
        tail = f", {failed} failed" if failed else ""
        return (f"{name}: {len(self.frames)} frames, {self.scans_published} scans, "
                f"{len(self.folds)} folded in {self.seconds:.1f} s{tail}")


# --- what a method has to say for itself before anything is sent ----------------------


def refusals(method: Method) -> list[str]:
    """Why this method cannot be acquired, one line each; empty if it can.

    The judgement `run_acquisition` makes before it touches a box, separated out so that
    a window can make it while a trainee is still editing.

    Today there is one: **`single_frame` with more than one method frame**. The whole
    point of that mode is that the trainee's table runs its own loop, and such a table
    raises the digitizer's enable at tick 0 and never lowers it. That is harmless for
    one frame, which is what the instrument does today, and wrong for two: Control I/O 2
    is a level, so the second frame's `acquire frame` meets a gate that is already high
    and begins recording on the next push, before its start list has run, offset by
    however long the serial round trips took. The offset is invisible in the file and
    looks exactly like an enable lead that has fallen off (lab record, task 05).

    The fix is the table's, not the loop's: end it by lowering the enable one tick past
    the last counted scan, as `per_repetition` does. Lowering it from the host instead
    would take a command this protocol document does not yet describe, which is its own
    piece of work rather than something to improvise here (Matt, 2026-09-11).
    """
    acquisition = method.acquisition
    problems: list[str] = []
    if acquisition.repetition_mode == "single_frame" and acquisition.frames > 1:
        problems.append(
            f"repetition_mode 'single_frame' with frames = {acquisition.frames}: a table "
            "that loops on the box raises the digitizer's enable once and never lowers "
            "it, so every frame after the first would begin recording before its start "
            "list ran and be offset by the serial latency. Either acquire one frame at a "
            "time, or write a table that lowers the enable one tick past its last "
            "counted scan"
        )
    return problems


# --- phases ---------------------------------------------------------------------------


def send_phases(
    method: Method,
    boxes: Mapping[str, Box],
    *,
    setup: bool = True,
    verify_tables: bool = False,
    progress: Callable[[Event], None] | None = None,
    arm_timeout: float = ARM_TIMEOUT_S,
) -> None:
    """Send every box its `setup`, `load` and `arm` phases, in the method's order.

    Nothing here starts anything: at the end of it every box is loaded and waiting for
    the start edge that `run_acquisition` gives it. This is a window's "Send all" and
    the first half of an acquisition from cold.

    `setup` persists on a box across acquisitions, so pass `setup=False` for a box that
    has had it since power-up; `load` and `arm` go every time. A box the method names and
    the mapping does not is an error rather than a skip, because a method whose boxes are
    half connected acquires something that is not the experiment.

    `verify_tables` reads every loaded table back through `TBLRPT` and compares it with
    what this host predicted, which is thousands of lines per table and is worth the
    minute when a table is new. Off by default.

    Raises whatever the box raised, after reporting it, so a refused string stops the
    send rather than leaving a half-loaded instrument that looks armed.
    """
    report = progress if progress is not None else _ignore
    for message in method.warnings:
        report(Warned(message))
    missing = [box.name for box in method.boxes if box.name not in boxes]
    if missing:
        raise KeyError(
            f"the method names {len(missing)} box(es) with no open port: "
            + ", ".join(sorted(missing))
        )
    for entry in method.boxes:
        box = boxes[entry.name]
        report(BoxReady(entry.name, entry.port, box.version()))
        phases = (("setup", entry.setup),) if setup else ()
        for phase, commands in phases + (("load", entry.load), ("arm", entry.arm)):
            for command in commands:
                _send(box, entry.name, phase, command, report,
                      arm_timeout=arm_timeout, verify_tables=verify_tables)


def _send(
    box: Box,
    name: str,
    phase: str,
    command: str,
    report: Callable[[Event], None],
    *,
    arm_timeout: float,
    verify_tables: bool = False,
) -> None:
    """One string to one box, reported either way, and raised if the box refused it.

    The three commands this reads rather than relays are here and nowhere else; the
    module docstring says why each one has to be.
    """
    started = time.perf_counter()
    try:
        detail = _deliver(box, command, arm_timeout=arm_timeout,
                          verify_tables=verify_tables)
    except (MipsError, ValueError) as exc:
        # `ValueError` is a table string the sender will not put on the wire at all,
        # which is a method problem rather than a box problem and is worth reporting in
        # the same place as a rejection.
        report(PhaseSent(name, phase, command, time.perf_counter() - started,
                         error=str(exc)))
        raise
    report(PhaseSent(name, phase, command, time.perf_counter() - started, detail))


def _deliver(box: Box, command: str, *, arm_timeout: float, verify_tables: bool) -> str:
    head = command.split(",", 1)[0].split(";", 1)[0].strip().upper()
    if head == "STBLDAT":
        # Paced chunks, not one write: the box has no flow control and drops what
        # overruns its 4 KB input buffer without saying so (wire format, section 1).
        load = box.send_table(command)
        detail = (f"{load.bytes_sent} bytes in {load.write_seconds:.3f} s, "
                  f"{load.stall_margin:.2f} s of stall margin")
        if verify_tables:
            differences = box.verify_table(load)
            detail += "; reads back clean" if not differences else \
                f"; reads back differently: {'; '.join(differences)}"
        return detail
    if head == "SMOD":
        mode = command.partition(",")[2].strip().upper()
        if mode == "LOC":
            # A box already in local mode NAKs, and that answer says the mode is
            # already what was asked for, which is success (wire format, section 4).
            box.local()
            return "local"
        box.command(command)
        box.wait_for(TableEvent.READY, timeout=arm_timeout)
        return "armed, TBLRDY"
    box.command(command)
    return ""


# --- the acquisition ------------------------------------------------------------------


def run_acquisition(
    method: Method,
    *,
    boxes: Mapping[str, Box],
    console: Console,
    stream: DataStream,
    recording: Recording | None = None,
    directory: str | os.PathLike[str] | None = None,
    stem: str | None = None,
    post_trigger_samples: int | None = None,
    width: TofWidth | None = None,
    replicate: bool = False,
    progress: Callable[[Event], None] | None = None,
    frame_timeout: float | None = None,
    silence: float = SILENCE_S,
    empty_settle: float = EMPTY_SETTLE_S,
    arm_timeout: float = ARM_TIMEOUT_S,
    guard_gate: bool = True,
    rearm_with_reset: bool = False,
    abort_after: int | None = ABORT_AFTER_FAILURES,
    instrument: Instrument = UNCALIBRATED,
    adc_name: str = "",
    overwrite: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> Run:
    """Acquire everything one method asks for, into one pair of files.

    The boxes are expected to be loaded and armed already, which is `send_phases`. This
    is steps 3 to 6 of one acquisition: the files, the console's chain, a console frame
    per repetition with the start list walked into each one, the fold after each method
    frame, and `keep_raw` at the close.

    **A technical replicate is `replicate=True` with a new `stem`.** It walks the
    method's `reset` list first and then does exactly the same again, re-sending neither
    `setup` nor `load`, because on this instrument a reset returns the box to local mode
    and arms it again and leaves the table where it was. Two runs given the same stem
    collide rather than overwrite, which is deliberate: the second acquisition of an
    afternoon should not quietly replace the first.

    **Who opens the acquisition chain closes it.** Pass `width` -- the `TofWidth` a
    `start_chain` already returned -- and the caller keeps the chain and owes the
    `stop_acquire`; leave it out and this opens the chain and stops it on the way out,
    whatever happened in between. `recording` works the same way for the files, except
    that a recording is finished when the run that fills it is, so it is always closed
    here. Creating one needs `directory` and `post_trigger_samples`, which is the
    console's `PostTriggerDelay` in samples and the number every scan's leading zero run
    is built from.

    `frame_timeout` defaults to twice how long the frame should take plus a floor, which
    is derived from the pusher period the console measured; the constants say why one
    fixed number will not do. `silence` is how long the data stream has to be quiet
    before a frame is over, and `empty_settle` how long a frame that published nothing
    is given to prove otherwise before it is called empty.

    Returns a `Run` describing what happened, including the frames that did not work: an
    empty frame, a console error and a frame that never ended are outcomes recorded
    against their frame, which is left provisional in the file, and the run goes on to
    the next one. `abort_after` consecutive failures ends it early and says so.
    """
    report = progress if progress is not None else _ignore
    problems = refusals(method)
    if problems:
        raise AcquisitionRefused("; ".join(problems))
    for message in method.warnings:
        report(Warned(message))
    for message in _vertical_warnings(console, instrument):
        report(Warned(message))
    missing = [entry.box for entry in method.start + method.reset
               if entry.box not in boxes]
    if missing:
        raise KeyError(
            "the method's start or reset sequence names a box with no open port: "
            + ", ".join(sorted(set(missing)))
        )

    started = clock()
    owns_chain = width is None
    if width is None:
        width = start_chain(console, stream)
    try:
        if recording is None:
            if directory is None or post_trigger_samples is None:
                raise ValueError(
                    "either pass a Recording, or pass directory and post_trigger_samples "
                    "so that one can be created"
                )
            geometry = Geometry.from_tof_width(
                width, sample_rate_hz=_sample_rate(console),
                post_trigger_samples=post_trigger_samples,
            )
            recording = Recording.create(
                directory, method, geometry, stem=stem, instrument=instrument,
                adc_name=adc_name, console_version=console.info().text,
                clock=clock, started=started, overwrite=overwrite,
            )
        loop = _Loop(
            method=method, boxes=boxes, console=console, stream=stream,
            recording=recording, report=report, silence=silence,
            empty_settle=empty_settle, arm_timeout=arm_timeout, guard_gate=guard_gate,
            rearm_with_reset=rearm_with_reset, abort_after=abort_after,
            clock=clock, started=started,
            frame_timeout=(frame_timeout if frame_timeout is not None
                           else _frame_timeout(method, recording.geometry)),
        )
        run = loop.run(replicate=replicate)
    finally:
        # A recording is finished when the run that fills it is, whoever made it, and
        # closing it is what honours `keep_raw`. The chain is the other way round: it
        # outlives a run, so it is stopped only by whoever opened it.
        if recording is not None:
            recording.close()
        if owns_chain:
            console.stop_acquire()
    # Read off the directory rather than predicted: `keep_raw` removes the raw file only
    # once a fold has written the companion that replaces it.
    return dataclasses.replace(run, raw_kept=os.path.isfile(run.raw_path))


def _vertical_warnings(console: Console, instrument: Instrument) -> list[str]:
    """Whether the window the instrument document describes is the one the card is in.

    The stamp records the vertical settings so that two files acquired through different
    ranges can be told apart (lab record, task 25), and a document that has drifted from
    the machine makes that record wrong rather than absent. Only the offset can be
    checked: the console reads its full scale from `config.txt` at startup and does not
    report it back, so half the window is unverifiable until the fork's `get_info` says
    what it is using (routed to task 24).

    A warning and not a refusal, for that reason. A check that can only ever see one of
    two settings is not a thing to stop a run on, and the message names both numbers and
    which of them is the machine, so that a trainee can act on it without opening
    anything.
    """
    declared = instrument.vertical.offset_v
    actual = console.offset_v
    if declared is None or actual is None:
        return []
    if math.isclose(declared, actual, rel_tol=1e-9, abs_tol=1e-12):
        return []
    return [
        f"the console was set to a channel offset of {actual} V and the instrument "
        f"document says {declared} V; the file will be stamped with the document's "
        "value, so one of the two is wrong"
    ]


def _sample_rate(console: Console) -> float:
    """What `horizontal` set, or the rate a zero-suppressed separation runs at.

    The console keeps the rate it was told and this client remembers it, but a client
    attached to a console another process configured has never sent one. 2 GS/s is the
    only rate this instrument acquires at (lab record, task 03), so assuming it is
    honest; assuming silently would not be, which is why it is written down here.
    """
    return console.sample_rate_hz or 1.0 / SECONDS_PER_SAMPLE_2GSPS


def _frame_timeout(method: Method, geometry: Geometry) -> float:
    """Twice how long a frame should take, plus the floor. See the constants."""
    period_s = geometry.average_tof_length_ns * 1e-9
    expected = method.acquisition.frame_length * period_s
    return FRAME_TIMEOUT_FLOOR_S + FRAME_TIMEOUT_SLACK * expected


def _ignore(_: Event) -> None:
    """The progress callback a caller that wants none gets."""


@dataclass
class _Loop:
    """The state one run carries. Not public: `run_acquisition` is the surface."""

    method: Method
    boxes: Mapping[str, Box]
    console: Console
    stream: DataStream
    recording: Recording
    report: Callable[[Event], None]
    frame_timeout: float
    silence: float
    empty_settle: float
    arm_timeout: float
    guard_gate: bool
    rearm_with_reset: bool
    abort_after: int | None
    clock: Callable[[], float]
    started: float

    frames: list[FrameRecord] = field(default_factory=list)
    folds: list[FoldRecord] = field(default_factory=list)
    stopped_early: str | None = None
    _consecutive_failures: int = 0

    # -- the run -----------------------------------------------------------------------

    def run(self, *, replicate: bool) -> Run:
        acquisition = self.method.acquisition
        self.report(RunBegun(
            self.recording.raw_path, self.recording.summed_path,
            acquisition.frames, acquisition.console_frames,
            acquisition.frame_length, self.frame_timeout,
        ))
        if replicate:
            self._walk(self.method.reset, "reset")

        # One worker, so folds stay in order and only ever one reads the raw file while
        # the console writes it. A fold of the last method frame is joined below rather
        # than waited on here, which is what "overlapping the next frame" means.
        folder = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clockwork-fold")
        pending: list[Future[FoldRecord]] = []
        try:
            for method_frame in range(1, acquisition.frames + 1):
                for repetition in range(1, acquisition.console_frames + 1):
                    self._one_frame(method_frame, repetition)
                    if self.stopped_early is not None:
                        break
                if any(record.method_frame == method_frame and record.acquired
                       for record in self.frames):
                    # A method frame none of whose repetitions acquired is not folded.
                    # The fold would succeed, write an empty frame to the companion, and
                    # so let `keep_raw = false` delete the raw file on the strength of a
                    # companion that replaces nothing.
                    pending.append(folder.submit(self._fold, method_frame))
                pending = self._collect(pending, wait=False)
                if self.stopped_early is not None:
                    break
        finally:
            self._collect(pending, wait=True)
            # The companion's SQLite connection belongs to the thread that made it,
            # which is this worker, so it is closed from here and not by whoever closes
            # the recording afterwards.
            folder.submit(self.recording.close_companion).result()
            folder.shutdown(wait=True)

        return Run(
            method=self.method,
            raw_path=self.recording.raw_path,
            summed_path=self.recording.summed_path,
            frames=tuple(self.frames),
            folds=tuple(self.folds),
            warnings=self.method.warnings,
            seconds=self.clock() - self.started,
            replicate=replicate,
            stopped_early=self.stopped_early,
        )

    def _collect(
        self, pending: list[Future[FoldRecord]], *, wait: bool
    ) -> list[Future[FoldRecord]]:
        """Report the folds that have finished, and hand back the ones that have not.

        In submission order, so a fold is never reported before an earlier one: they run
        on a single worker and finish in that order anyway, and a window reading the
        progress stream should see them in the order the method frames come.
        """
        for at, future in enumerate(pending):
            if not wait and not future.done():
                return pending[at:]
            record = future.result()
            self.folds.append(record)
            self.report(Folded(record))
        return []

    # -- one console frame --------------------------------------------------------------

    def _one_frame(self, method_frame: int, repetition: int) -> None:
        acquisition = self.method.acquisition
        request = self.recording.begin_frame(method_frame, repetition)
        self.report(FrameBegun(method_frame, repetition, request.frame_number,
                               acquisition.console_frames))
        seen: list[Batch] = []
        began = self.clock()
        outcome, detail = "", ""

        def on_batch(batch: Batch) -> None:
            seen.append(batch)
            self.report(BatchSeen(method_frame, repetition, batch))

        def release() -> None:
            if self.rearm_with_reset and repetition > 1:
                # The fallback the sync design names for a box whose table does not
                # re-arm itself after a software trigger. Unlocked by a bench answer,
                # not by this loop's opinion.
                self._walk(self.method.reset, "reset")
            self._walk(self.method.start, "start")
            if self.guard_gate:
                self._check_gate(method_frame, repetition)

        try:
            try:
                run_frame(self.console, self.stream, request,
                          timeout=self.frame_timeout, on_batch=on_batch,
                          release=release, settle=self.empty_settle)
            except (StreamTimeout, EmptyFrameError, ConsoleAcquisitionError,
                    EnableGateError, MipsError) as exc:
                # An outcome, not an escape. `run_frame` has already sent its
                # `stop frame`, so the console is in a state the next frame can start
                # from, and every frame already acquired is worth more than a clean
                # traceback. Anything else -- a console that refused the command, a
                # client in the wrong state -- is a failure of the run and propagates,
                # leaving this frame provisional through the `finally` below.
                outcome, detail = type(exc).__name__, str(exc)
            else:
                outcome = "acquired"

            scans_before = sum(batch.scans for batch in seen)
            rows_at_finished = self.recording.rows_in(request.frame_number)
            silence_began = self.clock()
            trailing = self._drain_to_silence(on_batch)
            silence_seconds = self.clock() - silence_began
            rows_after = self.recording.rows_in(request.frame_number)
        finally:
            # The completion marker last, after the console has stopped writing to this
            # frame: it is the only thing in the file that tells a frame that finished
            # from one that was cut off, and a frame that failed must not carry it.
            self.recording.end_frame(complete=outcome == "acquired")

        record = FrameRecord(
            method_frame=method_frame,
            repetition=repetition,
            frame_number=request.frame_number,
            outcome=outcome,
            detail=detail,
            batches=len(seen),
            scans_published=sum(batch.scans for batch in seen),
            scans_after_finished=sum(batch.scans for batch in seen) - scans_before,
            trailing_batches=trailing,
            rows_at_finished=rows_at_finished,
            rows_after_silence=rows_after,
            started_s=began - self.started,
            seconds=self.clock() - began,
            silence_seconds=silence_seconds,
        )
        self.frames.append(record)
        self.report(FrameEnded(record))
        self._note_box_events()

        if record.acquired:
            self._consecutive_failures = 0
            return
        self._consecutive_failures += 1
        if self.abort_after is not None and self._consecutive_failures >= self.abort_after:
            self.stopped_early = (
                f"{self._consecutive_failures} frames in a row failed, the last with "
                f"{record.outcome}: {record.detail}"
            )

    def _drain_to_silence(self, on_batch: Callable[[Batch], None]) -> int:
        """Keep reading until the stream has said nothing for `silence` seconds.

        A frame's batches mostly arrive after its own `finished` and the console's
        writer trails even those, so this is what makes a frame's scan count the frame's
        and lets the completion marker mean something. It doubles as the precondition
        for the next frame's gate guard: what the guard sees has to be this frame's
        doing, not the last one's backlog.
        """
        trailing = 0
        quiet_since = time.perf_counter()
        while time.perf_counter() - quiet_since < self.silence:
            event = self.stream.poll(0.2)
            if event is None:
                continue
            quiet_since = time.perf_counter()
            if isinstance(event, Batch):
                trailing += 1
                on_batch(event)
        return trailing

    def _check_gate(self, method_frame: int, repetition: int) -> None:
        """Refuse a frame that was already recording before its start list finished.

        A batch is `NotifyOnScansCount` scans -- 500 on the instrument, 64.5 ms of
        recording -- against a start list of a few serial round trips, and the previous
        frame's backlog has been waited out. So anything on the data topic here is stray
        recording, and there are exactly two ways to get it: a table that left the enable
        high, or the enable lead off the card. Nothing else in the system notices either,
        and both produce a full frame of plausible data at the wrong offset (lab record,
        task 05).

        Whatever is not a batch goes back on the stream: a status message belongs to the
        wait for this frame's end, not to the guard.
        """
        held: list[Batch | Status] = []
        stray = 0
        while True:
            event = self.stream.poll(0.0)
            if event is None:
                break
            if isinstance(event, Batch):
                stray += 1
            else:
                held.append(event)
        for event in reversed(held):
            self.stream.unread(event)
        if stray:
            raise EnableGateError(
                f"frame {method_frame}.{repetition}: the console published {stray} "
                "batch(es) before the start list had finished, so the digitizer was "
                "already recording. Either the previous frame's table left the enable "
                "high, or the enable lead is off Control I/O 2, whose input is pulled "
                "up so that an unconnected one reads high and the card acquires every "
                "push. The frame that would have come out of this looks like data and "
                "is at the wrong offset"
            )

    # -- boxes -------------------------------------------------------------------------

    def _walk(self, steps: Sequence[Step], phase: str) -> None:
        """One ordered method-level sequence, box by box, in the order written.

        The order is part of the experiment rather than a convenience: a box whose
        compression table must already be waiting at its first hold is told to run
        before the box that issues the release edge is triggered, and `TBLSTRT` is last
        because it is what starts everything.
        """
        for step in steps:
            _send(self.boxes[step.box], step.box, phase, step.command, self.report,
                  arm_timeout=self.arm_timeout)

    def _note_box_events(self) -> None:
        """Report whatever the boxes said on their own while the frame ran.

        `TBLTRIG`, `TBLCMPLT` and the `TBLRDY` of a table that re-armed itself, which
        under `per_repetition` is how a box says it is ready for the next start edge.
        Read without waiting: a box that has not got there yet says so on the next frame.
        """
        for name, box in self.boxes.items():
            for event in box.drain(0.0):
                self.report(BoxSaid(name, event.value))

    # -- the fold ----------------------------------------------------------------------

    def _fold(self, method_frame: int) -> FoldRecord:
        """Sum one method frame's repetitions, on the folding thread.

        Runs while the next method frame acquires. A fold that fails is recorded and
        does not end the run: the raw file still holds every repetition, and a fold can
        be redone from it afterwards, which is exactly the case `keep_raw` protects.
        """
        numbers = tuple(self.recording.frames_of(method_frame))
        began = time.perf_counter()
        try:
            rows = self.recording.fold(method_frame)
        except (AcqError, OSError, ValueError, KeyError) as exc:
            return FoldRecord(method_frame, numbers, seconds=time.perf_counter() - began,
                              error=f"{type(exc).__name__}: {exc}")
        return FoldRecord(method_frame, numbers, rows=rows,
                          seconds=time.perf_counter() - began)
