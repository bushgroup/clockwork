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

**A frame is over when it has counted out, not when it says `finished`.** The console
publishes batches from a subscriber thread and `finished` from the acquisition thread,
and its writer is a third thread still inserting rows for a frame whose end has already
gone out. So the completion marker is written once every scan the frame asked for has
arrived on the data socket and the file has stopped growing, and a fold that ran any
earlier would fold a fraction of its frame (lab record, tasks 20, 18 and 34). A frame
that never counts out ends on `SILENCE_S` instead, which is the fallback and is all
there was until task 34 measured what it cost.

**Three commands are read rather than relayed**, and only three. `STBLDAT` is streamed
in paced chunks instead of written in one go, because the box has no flow control and
silently drops what overruns its input buffer; `SMOD,LOC` treats the box's "already
local" rejection as success; and an `SMOD` into any other mode is followed by a wait for
`TBLRDY`. All three are facts `docs/mips-wire-format.md` states and `clockwork.mips`
already implements. Every other string in a method reaches its box exactly as written.

**The enable is lowered between the method frames of a `single_frame` acquisition**,
because that method's table loops on the box and raises the gate once, so the next
frame would be released against a gate that is already high. The loop puts the
sequencer in local mode, clears the line with `SDIO` and arms it again, which is the
only mechanism that moves the line at a time the host chooses: a host write in table
mode is latched by the table's next event instead, up to a whole table period later
(lab record, task 26). It needs `acquisition.enable` to say which line, and refuses the
combination without it. See `AcquisitionRefused`.

**A method is checked against its own strings before anything is sent.** The counts
`[acquisition]` states are written a second time inside the trainee's strings -- the
sequencer table's loop count and period, each compression table's `]N`, and the ticks
the sequencer raises and lowers the digitizer's enable on -- and nothing used to compare
them. Each way they can disagree fails silently: a loop count short of `accumulations`
folds the wrong pushes together, one past it never finishes the frame, an enable lowered
early leaves the frame a batch short, and a table that never raises it at all acquires
nothing (lab record, tasks 31 and 33). So `refusals` reads the strings and refuses a
contradiction between two numbers that both parsed, and `cautions` says which strings
could not be read well enough to check, which reaches a caller as a `Warned` event
rather than stopping anything.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from ..instrument import UNCALIBRATED, Instrument
from ..method import (
    NOTIFY_ON_SCANS_COUNT,
    Acquisition,
    Enable,
    Method,
    Step,
    enable_fall_tick,
    table_period,
)
from ..mips import (
    UNNAMED,
    Box,
    Compiled,
    MipsError,
    Table,
    TableEvent,
    TableSyntaxError,
    compile_table,
    compression_passes,
    digital_events,
    dio_command,
)
from .console import Console
from .session import EMPTY_SETTLE_S, run_frame, start_chain
from .stream import DataStream, StreamTimeout
from .uimf import Geometry, Recording
from .wire import (
    SECONDS_PER_SAMPLE_2GSPS,
    AcqError,
    Batch,
    ConsoleAcquisitionError,
    ConsoleInfo,
    EmptyFrameError,
    FrameRequest,
    Status,
    TofWidth,
)

__all__ = [
    "ABORT_AFTER_FAILURES",
    "ARM_TIMEOUT_S",
    "FRAME_POLL_S",
    "FRAME_TIMEOUT_FLOOR_S",
    "FRAME_TIMEOUT_SLACK",
    "GATE_PUBLISH_ALLOWANCE_S",
    "ROW_SETTLE_S",
    "SILENCE_S",
    "START_STEP_GAP_S",
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
    "GateChecked",
    "PhaseSent",
    "Run",
    "RunBegun",
    "Warned",
    "cautions",
    "refusals",
    "run_acquisition",
    "send_phases",
]

_LOG = logging.getLogger("clockwork.acq.loop")
"""The transcript's name for the loop's own narrative (`clockwork.transcript`).

Every `Event` this module reports goes here as well as to the caller's `progress`,
so that a transcript holds what the loop decided beside what the two links
carried. It is the one of the four names that is not the wire, which matters when
the two disagree: an `EnableGateError` is this module's judgement about batches
`clockwork.acq.stream` recorded arriving, and both lines are in the file."""

SILENCE_S = 3.0
"""How long the data stream has to be quiet before a frame that never counted out is over.

**The fallback, not the criterion.** A frame whose scans all arrive ends on the count
(`_wait_for_frame`) and never waits this out; this is what ends a frame that stops short
-- a gate trip, a refused start, an error on `status`, a batch the socket dropped -- and
so never reaches its own `frame_length`.

Three seconds is measured rather than chosen. Over 608 complete frames across three
sessions, at occupancies from 0.1 % to past what the console can stream, the longest gap
between two consecutive messages of a frame still delivering was **1.194 s** (lab record,
task 34); three seconds is two and a half times that. It also clears the console's
`AcquisitionTimeoutMs` of 2000 ms, so a short frame's own `error` line, which arrives
about 1.97 s after its last batch, is read inside this wait rather than left for whoever
waits next (lab record, task 35).

It was six seconds until task 34, when it was the only criterion there was, and it was
then the whole of the per-repetition dead time: the day's frames delivered their last
batch 0.69 to 0.75 s after `acquire frame` and the loop waited a further six.
"""

FRAME_POLL_S = 0.050
"""How often the wait for a frame's end looks at anything.

One number for three cadences that have no reason to differ: how often the file's row
count is re-read, how often the boxes' serial ports are drained so that a status line is
timestamped near when it arrived, and how finely the silence above is measured. Fifty
milliseconds is comfortably shorter than a batch at the instrument's period (64.5 ms) and
long enough that reading the row count is not itself part of what is being measured.
"""

ROW_SETTLE_S = 0.200
"""How long the file's row count has to hold still before the frame is called written.

The console's writer is a second subscriber on a queue of its own, so the data socket
having delivered every scan says nothing about how far the writer has got
(`docs/console-protocol.md`, "Division of UIMF writing"). Nor can the rows be counted
out: a push that crossed the threshold nowhere stores no row, so a real frame holds fewer
rows than scans and there is no total to compare against. What is left is the pause, and
this is how long a pause is taken for an answer.

**Provisional, and the loop now measures the number that replaces it.** Every frame
records `settle_seconds`, how long after the last scan arrived the row count last moved,
which is the measurement the BUFFLEHEAD day could not make: it read the row count twice,
six seconds apart, and saw the writer no more than one batch behind (0 or 500 rows) without
ever learning how long that batch took. Two hundred milliseconds is three times a batch at
the instrument's period and inside the 350 ms per repetition this may spend (Matt,
2026-09-14); the rig replaces it with the measured settle plus a margin (lab record,
task 34 step 6).
"""

START_STEP_GAP_S = 0.020
"""How long to leave between one start-list step and the next.

The list guarantees order on the wire and nothing about interval: measured across 54
repetitions of a two-box run, `TARBTRG` and the `TBLSTRT` after it left the host 0 to
147 ms apart, median 4 ms, and on 13 of them in the same millisecond (lab record,
task 33). What the order is *for* is an ARB box whose compression table has to be
sitting at its first `HR` before the release edge arrives, and that box does not get
there when `TARBTRG` is acknowledged: the compressor's trigger handler arms a timer at
the box's saved trigger delay (`SARBCTD`, milliseconds) and walks no table at all until
that timer fires. So the cushion has to exceed the delay the box is holding, and two
consecutive serial writes do not reliably provide any cushion whatever.

Twenty milliseconds against a repetition that costs seconds. It is insurance and not a
derivation: the number to beat is the boxes' own `SARBCTD`, which is why a method sets
that explicitly rather than inheriting whatever a box remembers.
"""

GATE_PUBLISH_ALLOWANCE_S = 0.100
"""How long a batch takes to reach a client on top of the pushes it is made of.

The run's first frame is asked for and then waited on before anything is released, so
that a digitizer which was already recording has time to prove it (`_check_first_gate`).
How long that wait has to be is one batch of pushes -- `NotifyOnScansCount` of them,
64.5 ms at the instrument's period -- plus whatever the console's publisher and the
socket add on top, which is this. The day's one ungated frame published its first batch
**135 ms** after `acquire frame`, of which 64.5 ms was the recording, so the path costs
about 70 ms; 100 rounds that up rather than deriving it, because nothing about it scales
with the frame (lab record, task 26).

**The console has a budget of its own and this spends it.** Each batch is one
`CstZs1Context::acquire` with `AcquisitionTimeoutMs` from the moment it begins, and the
first one begins at `acquire frame`, so everything between that and the frame's first
batch has to fit inside the timeout or the console errors the frame -- and that first
batch is not `NotifyOnScansCount` pushes but rather more, because the console takes
markers from the card in fixed granules of one batch's worth of marker hunks and the
granule carrying its last trigger has to fill. The dwell is added to a start list that
already costs `START_STEP_GAP_S` per gap and a serial round trip per step, and the sum
is what the lab's `AcquisitionTimeoutMs` has to clear (lab record, task 35).
"""

LOC_ONLY_SETUP = frozenset({"STBLCLK", "STBLTRG"})
"""Setup commands the box accepts only in local mode (wire format, section 4).

`STBLDAT` is deliberately not here: the same table says it is accepted in LOC mode *or*
in TBL mode with the table ready, so a `load` phase does not need the box dropped out of
table mode and a method that only loads and arms is untouched by this.
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
class GateChecked(Event):
    """The run's first frame was held open and the digitizer stayed silent through it.

    Reported once per run, and it is the evidence that the invariant held rather than an
    assertion that it did: every other frame of the run is released against a gate this
    one proved shut.
    """

    method_frame: int
    repetition: int
    seconds: float

    @property
    def text(self) -> str:
        return (f"frame {self.method_frame}.{self.repetition}: the gate is shut, "
                f"nothing published in {self.seconds:.3f} s before the start list")


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
    rows_at_end: int | None = None
    started_s: float = 0.0
    """When the frame began, on the run's clock."""

    seconds: float = 0.0
    wait_seconds: float = 0.0
    """How long the loop waited after `finished` for the frame to be over."""

    ended_by: str = ""
    """`"counted"` or `"silence"`, which of the two rules ended the wait.

    `"counted"` is the ordinary end: every scan the frame asked for arrived and the file
    stopped growing. `"silence"` says the frame never reached its own `frame_length` and
    the fallback ended it, so the file may hold less than the frame asked for whatever
    the outcome says. A run whose frames all say `"silence"` is a run to look at.
    """

    start_list_seconds: float = 0.0
    """How long the method's `start` list took to walk, release to release.

    The one term in a repetition's cost that is neither the acquisition nor the wait,
    and the one that varies: measured over the BUFFLEHEAD day's 158 frames it was a
    median of **7.5 ms** and a maximum of **378 ms**, and the long ones were the frames
    whose release coincided with a fold running on the folding thread (lab record,
    task 34). `START_STEP_GAP_S` per gap is in it by design; anything beyond that is the
    boxes' serial round trips and whatever else the interpreter was doing.
    """

    settle_seconds: float | None = None
    """How long after the last scan arrived the file's row count last moved.

    None on a frame that ended on the silence, which never reached the count, or whose
    rows could not be read. This is what `ROW_SETTLE_S` should be derived from and is not
    yet: nothing measured the console's writer in time before task 34.
    """

    @property
    def acquired(self) -> bool:
        return self.outcome == "acquired"

    @property
    def writer_lag_rows(self) -> int | None:
        """Rows the console's writer had not yet inserted when it said `finished`.

        The fold's whole timing constraint, and nothing else in the system measures it.
        None where either count could not be read.
        """
        if self.rows_at_finished is None or self.rows_at_end is None:
            return None
        return self.rows_at_end - self.rows_at_finished

    @property
    def text(self) -> str:
        late = ""
        if self.scans_after_finished:
            late = f", {self.scans_after_finished} of them after its own finished"
        lag = self.writer_lag_rows
        rows = f", {lag} rows written after it" if lag else ""
        fell_back = ", ended on the silence" if self.ended_by == "silence" else ""
        if not self.acquired:
            return (f"frame {self.method_frame}.{self.repetition}: {self.outcome}: "
                    f"{self.detail}")
        return (f"frame {self.method_frame}.{self.repetition}: "
                f"{self.scans_published} scans in {self.seconds:.3f} s"
                f"{late}{rows}{fell_back}")


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

    The judgement `run_acquisition` and `send_phases` both make before they touch a box,
    separated out so that a window can make it while a trainee is still editing.

    Two are about the digitizer's enable and are below. The rest come from
    `_consistency`, which reads the counts out of the method's own strings and refuses
    where one of them contradicts `[acquisition]`; `cautions` is the other half of that
    judgement, the strings it could not read.

    **`single_frame` with more than one method frame and no `acquisition.enable`.** The
    whole point of that mode is that the trainee's table runs its own loop, and such a
    table raises the enable at tick 0 and never lowers it. That is harmless for one
    frame, which is what the instrument does today, and wrong for two: Control I/O 2 is
    a level, so the second frame's `acquire frame` meets a gate that is already high and
    begins recording on the next push, before its start list has run, offset by however
    long the serial round trips took. The offset is invisible in the file and looks
    exactly like an enable lead that has fallen off (lab record, task 05).

    A method that says which line the enable is on lifts it, because the loop can then
    lower the line itself between method frames (`_lower_enable`) and the trainee's
    table is acquirable as written -- which is what the lab record's task 26 exists for,
    and what Matt chose on 2026-09-11 when he took the refusal as a stopgap. A method
    that does not say is still refused: nothing in this package knows which output
    carries the gate, and the other fix is the table's -- end it by lowering the enable
    one tick past the last counted scan, as `per_repetition` does.

    **A declared enable line that is not a digital output.** MIPS answers `SDIO,Q,1`
    with an ACK and drives output `I`, so a document naming an input silently corrupts a
    line rather than failing; `dio_command` is where that is refused and this is where
    it is refused early, before a box has been opened.
    """
    acquisition = method.acquisition
    problems: list[str] = []
    if acquisition.enable is not None:
        try:
            _enable_steps(acquisition.enable)
        except ValueError as exc:
            problems.append(f"acquisition.enable.channel: {exc}")
    if (acquisition.repetition_mode == "single_frame" and acquisition.frames > 1
            and acquisition.enable is None):
        problems.append(
            f"repetition_mode 'single_frame' with frames = {acquisition.frames}: a table "
            "that loops on the box raises the digitizer's enable once and never lowers "
            "it, so every frame after the first would begin recording before its start "
            "list ran and be offset by the serial latency. Either declare "
            "acquisition.enable so that the loop can lower the line between frames, "
            "acquire one frame at a time, or write a table that lowers the enable one "
            "tick past its last counted scan"
        )
    return problems + _consistency(method)[0]


def cautions(method: Method) -> list[str]:
    """Which of this method's strings could not be checked against it, and why.

    The other half of `refusals`, and the reason the two are separate. A string that
    parsed and disagrees with `[acquisition]` is a contradiction and the run cannot be
    right, so it is refused; a string this package could not read well enough to compare
    is not evidence of anything, and a trainee's verbatim string is not clockwork's to
    reject for being unusual (Matt, 2026-09-14). These reach a caller as `Warned` events
    from `send_phases` and `run_acquisition` and stop nothing.
    """
    return _consistency(method)[1]


def _consistency(method: Method) -> tuple[list[str], list[str]]:
    """The counts `[acquisition]` states against the counts its strings embed.

    Returns (contradictions, cautions). Closes the lab record's task 31, which is the
    `slimphony-map.md` FLAG "the accumulation count is written in three places": for the
    CLOCK method the sequencer's table loops `A:100`, both compression tables end `]100`
    and `accumulations` is 100, the loop's period is 5000 ticks and `scans` is 5000, and
    until this nothing compared any of them. A trainee editing `A:100` to `A:50` for a
    quicker run and leaving `accumulations` alone is the ordinary way they part company.

    **What each disagreement costs is the reason this is v1 work and not the deferred
    compiler's.** Every one of them fails silently. Under `single_frame` the console is
    told a frame is `scans * accumulations` long, so a table that loops fewer times fills
    a frame that never completes and one that loops more fills it early, after which the
    fold's `ScanNum mod scans` sums the wrong pushes together with nothing reported
    anywhere. Under `per_repetition` a table that stops re-arming early times out every
    later repetition, and one that runs on leaves the enable high into the next frame,
    which is the offset frame the gate guard exists to catch. An enable lowered before
    `enable_fall_tick` leaves the console a whole batch short on every frame, measured
    (lab record, task 33). An enable never raised at all acquires nothing, which is how
    the BUFFLEHEAD day's first failure presented.

    **It reads the few numbers the sequence depends on and does not become an
    interpreter.** Timing, channels and waveforms are the v2 compiler's.
    """
    acquisition = method.acquisition
    tables = [(box.name, command) for box in method.boxes for command in box.load
              if _head(command) == "STBLDAT"]
    compressions = [(box.name, command) for box in method.boxes for command in box.load
                    if _head(command) == "SARBCTBL"]
    problems: list[str] = []
    unreadable: list[str] = []
    _check_compression(acquisition, compressions, problems, unreadable)
    _check_sequencer(acquisition, tables, problems, unreadable)
    return problems, unreadable


def _head(command: str) -> str:
    """The command word of a method string, however its arguments are punctuated."""
    return command.split(",", 1)[0].split(";", 1)[0].strip().upper()


def _expected_passes(acquisition: Acquisition) -> int:
    """How many times one load of a table runs, which the repetition mode decides.

    `single_frame` puts a whole method frame in one console frame, so the trainee's
    strings loop `accumulations` times; `per_repetition` gives each repetition its own
    console frame and its own start edge, so they run once (lab record, task 14).
    """
    if acquisition.repetition_mode == "single_frame":
        return acquisition.accumulations
    return 1


def _check_compression(
    acquisition: Acquisition,
    compressions: list[tuple[str, str]],
    problems: list[str],
    unreadable: list[str],
) -> None:
    """Each `SARBCTBL`'s top-level `]N` against the passes the mode expects."""
    expected = _expected_passes(acquisition)
    for name, command in compressions:
        try:
            passes = compression_passes(command)
        except ValueError as exc:
            unreadable.append(f"{name}'s compression table was not checked: {exc}")
            continue
        if len(passes) > 1:
            unreadable.append(
                f"{name}'s compression table has {len(passes)} top-level loops and "
                "which of them is the accumulation loop cannot be told from the string, "
                "so its pass count was not checked"
            )
            continue
        # No loop at all is one pass, said the other way (wire format, section 6.6).
        count = passes[0] if passes else 1
        if count != expected:
            problems.append(
                f"{name}'s compression table runs {count} pass(es) and "
                f"{_passes_mean(acquisition)} is {expected}: under repetition_mode "
                f"{acquisition.repetition_mode!r} one load of a compression table covers "
                f"{'a whole method frame' if expected != 1 else 'one repetition'}, so "
                f"the two have to agree. The string is {command.strip()!r}"
            )


def _passes_mean(acquisition: Acquisition) -> str:
    """What the expected pass count is called in the document, for a message."""
    if acquisition.repetition_mode == "single_frame":
        return "acquisition.accumulations"
    return "one pass per console frame under 'per_repetition'"


def _check_sequencer(
    acquisition: Acquisition,
    tables: list[tuple[str, str]],
    problems: list[str],
    unreadable: list[str],
) -> None:
    """The sequencer's `STBLDAT` loop count, loop period and enable edges."""
    if not tables:
        unreadable.append(
            "no box loads an STBLDAT table, so the loop count, the loop period and the "
            "digitizer's enable were not checked against this method"
        )
        return
    if len(tables) > 1:
        unreadable.append(
            f"{len(tables)} boxes load an STBLDAT table and which of them is the "
            "sequencer cannot be told from the strings, so the loop count, the loop "
            "period and the digitizer's enable were not checked"
        )
        return
    name, command = tables[0]
    try:
        compiled = compile_table(command)
    except TableSyntaxError as exc:
        unreadable.append(f"{name}'s table was not checked: {exc}")
        return
    found = _loop_table(compiled)
    if found is None:
        unreadable.append(
            f"{name}'s table holds {len([t for t in compiled.tables if t.name != UNNAMED])}"
            " named sub-tables and which of them is the sequence cannot be told from the "
            "string, so its loop count and period were not checked"
        )
        return
    at, loop = found
    expected = _expected_passes(acquisition)
    if loop.repeat != expected:
        problems.append(
            f"{name}'s table loops {loop.repeat} time(s) and {_passes_mean(acquisition)} "
            f"is {expected}: under repetition_mode {acquisition.repetition_mode!r} one "
            f"run of the table covers "
            f"{'a whole method frame' if expected != 1 else 'one repetition'}, so the "
            "two have to agree"
        )
    _check_enable(acquisition, name, compiled, at, loop, problems, unreadable)


def _loop_table(compiled: Compiled) -> tuple[int, Table] | None:
    """The sub-table the sequence loops in and its index, or None where several could be.

    One named sub-table is the ordinary shape and is the one that carries the repeat
    count. A string with none -- a bare `0:A:1,5000:;` -- runs its one table once, which
    is a loop count of 1 said differently and is what the last table already reports. The
    index is the position `digital_events` reports an event against, so the two agree
    about which sub-table a tick belongs to.
    """
    named = [(at, table) for at, table in enumerate(compiled.tables)
             if table.name != UNNAMED]
    if len(named) == 1:
        return named[0]
    if not named:
        if not compiled.tables:
            return None
        return len(compiled.tables) - 1, compiled.tables[-1]
    return None


def _check_enable(
    acquisition: Acquisition,
    name: str,
    compiled: Compiled,
    at: int,
    loop: Table,
    problems: list[str],
    unreadable: list[str],
) -> None:
    """Where the sequencer's table raises and lowers the digitizer's gate.

    Needs `acquisition.enable` to say which line: which output carries the gate is a
    fact about the cabling and not a property of a document, and Matt chose the
    declaration over a hardcoded `A` (2026-09-14, lab record, task 26). A method that
    does not declare it gets a caution and keeps the two count comparisons above, which
    are the whole of the original three-places question.
    """
    enable = acquisition.enable
    period = loop.max_count
    if enable is None:
        unreadable.append(
            "acquisition.enable is not declared, so the tick the table raises the "
            "digitizer's enable on and the tick it lowers it on were not checked. Add "
            'it as { box = "...", channel = "..." } naming the output wired to the '
            "card's Control I/O 2"
        )
        # Without it the fall cannot be found, so both loop periods a correct table can
        # have are accepted rather than one of them being guessed at.
        if period not in (acquisition.scans, table_period(acquisition.frame_length)):
            problems.append(
                f"{name}'s table loops over {period} ticks and acquisition.scans is "
                f"{acquisition.scans}: one pass of the table is one ion mobility "
                "experiment and one tick is one pusher push, so the period is either "
                f"{acquisition.scans} or, where the table lowers the digitizer's enable "
                f"itself, {table_period(acquisition.frame_length)}"
            )
        return
    if enable.box != name:
        unreadable.append(
            f"acquisition.enable names box {enable.box!r} and the only STBLDAT table is "
            f"{name}'s, so the enable's edges were not checked"
        )
        return
    try:
        events = digital_events(compiled, enable.channel)
    except ValueError as exc:
        unreadable.append(f"acquisition.enable.channel: {exc}")
        return
    rises = [(index, tick) for index, tick, value in events if value == "1"]
    falls = [(index, tick) for index, tick, value in events if value == "0"]
    if not rises:
        problems.append(
            f"{name}'s table never raises {enable.channel}, which acquisition.enable "
            "names as the digitizer's gate, so every frame of this method would acquire "
            f"nothing. Note that the `{enable.channel}:n` in a loop header names the "
            "table and does not drive the line; the event has to be written again inside "
            "the loop (lab record, task 33)"
        )
        return
    if rises[0][1] != 0:
        problems.append(
            f"{name}'s table raises {enable.channel} at tick {rises[0][1]} rather than at "
            "tick 0, so the digitizer would take no record until then and the frame "
            f"would begin {rises[0][1]} pushes into the sequence"
        )
    _check_enable_fall(acquisition, name, enable, at, loop, period, falls, problems)


def _check_enable_fall(
    acquisition: Acquisition,
    name: str,
    enable: Enable,
    at: int,
    loop: Table,
    period: int,
    falls: list[tuple[int, int]],
    problems: list[str],
) -> None:
    """Whether the gate comes down, and whether it comes down where the rule says.

    `clockwork.method.enable_fall_tick` and `table_period` are the rule and the only
    copy of it: the enable stays high a whole `NotifyOnScansCount` past the last counted
    scan, because the console holds a batch until it has seen the next trigger's marker
    and fetches markers a batch of hunks at a time (lab record, task 33).
    """
    console_frames = acquisition.frames * acquisition.console_frames
    if not falls:
        if console_frames > 1 and acquisition.repetition_mode != "single_frame":
            problems.append(
                f"{name}'s table never lowers {enable.channel} and this method asks for "
                f"{console_frames} console frames: Control I/O 2 is a level, so every "
                "frame after the first would meet a gate the previous one left high and "
                "begin recording before its start list ran. Lower it at tick "
                f"{enable_fall_tick(acquisition.frame_length)} and give the loop a "
                f"period of {table_period(acquisition.frame_length)}"
            )
        elif period != acquisition.scans:
            problems.append(
                f"{name}'s table loops over {period} ticks and acquisition.scans is "
                f"{acquisition.scans}: one pass is one ion mobility experiment and one "
                "tick is one pusher push, so a table that does not lower the digitizer's "
                f"enable has a period of {acquisition.scans}"
            )
        return
    index, tick = falls[-1]
    if index == at and loop.repeat > 1:
        problems.append(
            f"{name}'s table lowers {enable.channel} at tick {tick} inside a loop that "
            f"runs {loop.repeat} times, so the digitizer's gate would come down after "
            "the first pass and the remaining passes would be recorded by nothing"
        )
        return
    wanted = enable_fall_tick(acquisition.frame_length)
    if tick != wanted:
        problems.append(
            f"{name}'s table lowers {enable.channel} at tick {tick} and the rule for "
            f"acquisition.frame_length = {acquisition.frame_length} puts it at {wanted}: "
            f"the gate stays high a whole console batch ({NOTIFY_ON_SCANS_COUNT} pushes) "
            "past the last counted scan, because the console holds a batch until it has "
            "seen the next trigger's marker. Lowering it earlier leaves every frame one "
            "batch short and it never finishes; later holds the card open on pushes the "
            "frame has stopped counting (lab record, task 33)"
        )
    elif period != table_period(acquisition.frame_length):
        problems.append(
            f"{name}'s table loops over {period} ticks and lowers {enable.channel} at "
            f"{tick}, so its period has to be {table_period(acquisition.frame_length)}, "
            "one tick past the fall"
        )


def _enable_steps(enable: Enable) -> tuple[Step, ...]:
    """The three commands that put the digitizer's gate line down and re-arm the box.

    `SDIO` is accepted in any mode and takes effect immediately in **local** mode only:
    the latch that applies the digital-output image is the LDAC pin, which entering
    table mode hands to the table engine's timer, so a host write made in table mode is
    not lost but pending, and the table's next event applies it at a time the host did
    not choose. Measured on the bench, that is up to a whole table period late -- 76 ms
    at a period of 500 ticks and 351 ms at 5000 -- which is useless for an invariant
    that has to hold at a particular instant. Hence the round trip through local mode,
    which the day also showed leaves the table loaded and re-armable (lab record,
    task 26; `docs/mips-wire-format.md` section 4).

    Built through `dio_command` rather than written out, because the firmware aliases
    the digital *inputs* `Q`-`X` onto outputs `I`-`P` and acknowledges them, so a
    channel that is not an output has to be refused by the host or not at all.
    """
    return (
        Step(enable.box, "SMOD,LOC"),
        Step(enable.box, dio_command(enable.channel, False)),
        Step(enable.box, "SMOD,TBL"),
    )


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
    report = _reporter(progress)
    problems = refusals(method)
    if problems:
        raise AcquisitionRefused("; ".join(problems))
    for message in list(method.warnings) + cautions(method):
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
        phases = (("setup", _setup_phase(entry.setup)),) if setup else ()
        for phase, commands in phases + (("load", entry.load), ("arm", entry.arm)):
            for command in commands:
                _send(box, entry.name, phase, command, report,
                      arm_timeout=arm_timeout, verify_tables=verify_tables)


def _setup_phase(commands: Sequence[str]) -> list[str]:
    """A setup phase with `SMOD,LOC` ahead of it when one of its commands needs it.

    `STBLCLK` and `STBLTRG` are LOC-mode only (`LOC_ONLY_SETUP`), so a box still armed
    from the acquisition before refuses them — and `send_phases(setup=True)` is exactly
    what the *second* acquisition from cold sends. A bench session met this by running
    the loop twice against one box: the first send worked only because the line that
    drove the enable low had left the box local, and the second was refused on its first
    string (lab record, task 30).

    The `load` and `arm` phases put the box back into table mode a moment later, so
    dropping it out first costs nothing and is what "from cold" already meant. A setup
    phase carrying no LOC-only command is returned untouched, so a method whose boxes
    take only a frequency block sends exactly what it sent before.
    """
    heads = [command.split(",", 1)[0].strip().upper() for command in commands]
    already = [part.strip().upper() for part in commands[:1]] == ["SMOD,LOC"]
    if already or not LOC_ONLY_SETUP.intersection(heads):
        return list(commands)
    return ["SMOD,LOC", *commands]


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
    row_settle: float = ROW_SETTLE_S,
    frame_poll: float = FRAME_POLL_S,
    empty_settle: float = EMPTY_SETTLE_S,
    arm_timeout: float = ARM_TIMEOUT_S,
    start_step_gap: float = START_STEP_GAP_S,
    guard_gate: bool = True,
    gate_dwell: float | None = None,
    ungate_chain: bool = True,
    rearm_with_reset: bool = False,
    abort_after: int | None = ABORT_AFTER_FAILURES,
    instrument: Instrument = UNCALIBRATED,
    adc_name: str = "",
    overwrite: bool = False,
    clock: Callable[[], float] = time.perf_counter,
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
    fixed number will not do. `empty_settle` is how long a frame that published nothing
    is given to prove otherwise before it is called empty. `start_step_gap` separates
    the start list's steps in time as well as in order, which the ARB boxes need and
    which two consecutive serial writes do not supply.

    **A frame ends when it has counted out**, which is `row_settle` after the last of
    its `frame_length` scans arrived and the file stopped growing, and a frame that never
    counts out ends `silence` after the last thing the console said. `frame_poll` is the
    cadence all three are measured on and is also how often the boxes' ports are drained,
    which is what dates a box's status line to when it arrived rather than to the end of
    the frame. The three constants carry the measurements behind them.

    `guard_gate` is the enable-gate check, in its two halves, and turning it off turns
    off both: the run's first frame is held open for `gate_dwell` seconds before anything
    is released, which is long enough for a digitizer that was already recording to
    publish, and every frame is checked for a batch once its start list has been walked.
    `gate_dwell` defaults to one batch of pushes at the period the console measured plus
    `GATE_PUBLISH_ALLOWANCE_S`; zero keeps the per-frame check and drops the dwell.

    `ungate_chain` is passed to `start_chain` and is how a cold instrument opens its
    chain at all: the period measurement needs triggers the card will not count while
    the enable input is held low. On by default since the bench session that showed the
    enable still in force on a chain built that way, and since the dwell above became
    the check that catches the one failure it can have silently (lab record, task 26);
    `start_chain`'s docstring carries both halves.

    Returns a `Run` describing what happened, including the frames that did not work: an
    empty frame, a console error and a frame that never ended are outcomes recorded
    against their frame, which is left provisional in the file, and the run goes on to
    the next one. `abort_after` consecutive failures ends it early and says so.
    """
    report = _reporter(progress)
    problems = refusals(method)
    if problems:
        raise AcquisitionRefused("; ".join(problems))
    for message in list(method.warnings) + cautions(method):
        report(Warned(message))
    # Asked once and used twice: the window check reads the full scale out of it, and a
    # recording created below stamps the whole string as the console that acquired it.
    info = console.info()
    for message in _vertical_warnings(console, instrument, info):
        report(Warned(message))
    missing = [entry.box for entry in method.start + method.reset
               if entry.box not in boxes]
    if missing:
        raise KeyError(
            "the method's start or reset sequence names a box with no open port: "
            + ", ".join(sorted(set(missing)))
        )

    # The run's origin, shared with the recording below so that this run's record of
    # when a frame began and the file's `StartTime` are the same measurement. The
    # default clock is `perf_counter` rather than `monotonic`, and has to agree with
    # `Recording.create`'s: on Windows `monotonic` ticks at about 15.6 ms, which is
    # comparable to a frame and coarser than the setup in front of the first one.
    started = clock()
    owns_chain = width is None
    if width is None:
        width = start_chain(console, stream, ungate=ungate_chain)
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
                adc_name=adc_name, console_version=info.text,
                clock=clock, started=started, overwrite=overwrite,
            )
        loop = _Loop(
            method=method, boxes=boxes, console=console, stream=stream,
            recording=recording, report=report, silence=silence,
            empty_settle=empty_settle, arm_timeout=arm_timeout, guard_gate=guard_gate,
            start_step_gap=start_step_gap, row_settle=row_settle,
            frame_poll=frame_poll,
            gate_dwell=(gate_dwell if gate_dwell is not None
                        else _gate_dwell(recording.geometry)),
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


def _vertical_warnings(
    console: Console, instrument: Instrument, info: ConsoleInfo | None = None
) -> list[str]:
    """Whether the window the instrument document describes is the one the card is in.

    The stamp records the vertical settings so that two files acquired through different
    ranges can be told apart (lab record, task 25), and a document that has drifted from
    the machine makes that record wrong rather than absent. **Both halves of the window
    are checkable since task 24**: the offset is what this client last sent, and the full
    scale is what `info` reports the console is using. A console that reports no full
    scale is a stock build or an older fork, and leaves that half unchecked as before.

    Warnings and not refusals. The two disagreements are stamped differently, since the
    console is the authority on the full scale and the document is the only source for
    the offset, and each message says which value the file will carry. Both name the two
    numbers and which of them is the machine, so that a trainee can act on one without
    opening anything.
    """
    pairs = (
        ("channel offset", instrument.vertical.offset_v, console.offset_v,
         "the file will be stamped with the document's value"),
        ("full scale", instrument.vertical.full_scale_v,
         info.full_scale_v if info is not None else None,
         "the file will be stamped with the console's value"),
    )
    messages: list[str] = []
    for name, declared, actual, stamped in pairs:
        if declared is None or actual is None:
            continue
        if math.isclose(declared, actual, rel_tol=1e-9, abs_tol=1e-12):
            continue
        messages.append(
            f"the console was set to a {name} of {actual} V and the instrument "
            f"document says {declared} V; {stamped}, so one of the two is wrong"
        )
    return messages


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


def _gate_dwell(geometry: Geometry) -> float:
    """How long the run's first frame is held open before anything is released.

    One batch of pushes at the period the console measured, plus the allowance for the
    path a batch takes to reach a client. Derived rather than fixed because the pushes
    are the larger half and a bench rig's generator is not the pusher: at 129 us it is
    64.5 ms, and a rig running ten times slower needs ten times the wait to prove the
    same thing.

    `NOTIFY_ON_SCANS_COUNT` is this package's assumption about the console's
    `NotifyOnScansCount` and the same number the enable window is measured in
    (`clockwork.method`). A console configured with a larger batch publishes later than
    this expects, and the dwell then proves less than it means to rather than failing.
    """
    period_s = geometry.average_tof_length_ns * 1e-9
    return NOTIFY_ON_SCANS_COUNT * period_s + GATE_PUBLISH_ALLOWANCE_S


def _ignore(_: Event) -> None:
    """The progress callback a caller that wants none gets."""


def _reporter(progress: Callable[[Event], None] | None) -> Callable[[Event], None]:
    """The caller's progress callback, with the transcript in front of it.

    Wrapped rather than left to the caller because a window that draws a status
    line and a bench script that prints one should not each have to remember to
    write the same events to the file as well. The level is checked per event
    rather than once, so a transcript opened part way through a run catches the
    rest of it.
    """
    report = progress if progress is not None else _ignore

    def reported(event: Event) -> None:
        if _LOG.isEnabledFor(logging.DEBUG):
            _LOG.debug("%s: %s", type(event).__name__, event.text)
        report(event)

    return reported


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
    gate_dwell: float
    start_step_gap: float = START_STEP_GAP_S
    row_settle: float = ROW_SETTLE_S
    frame_poll: float = FRAME_POLL_S

    frames: list[FrameRecord] = field(default_factory=list)
    folds: list[FoldRecord] = field(default_factory=list)
    stopped_early: str | None = None
    _consecutive_failures: int = 0
    _gate_checked: bool = False
    _folder: ThreadPoolExecutor | None = None
    _pending: list[Future[FoldRecord]] = field(default_factory=list)
    _fold_due: int | None = None

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
        self._folder = folder
        self._pending = []
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
                    #
                    # Held rather than submitted: the next method frame's first
                    # repetition has a start list to walk, and a fold running across it
                    # stretches serial round trips that normally cost single-figure
                    # milliseconds to hundreds -- measured at 226 ms on a `TBLSTRT`,
                    # which is a box that started its table long before the host knew,
                    # and so a frame the loop cannot tell from one released against a
                    # gate that was already high (lab record, task 33). Almost none of
                    # the overlap is given up: the fold still runs through that
                    # repetition's acquisition and its silence.
                    self._fold_due = method_frame
                self._pending = self._collect(self._pending, wait=False)
                if self.stopped_early is not None:
                    break
        finally:
            # A run that ended with a fold still held -- the last method frame's, or a
            # run that stopped early -- submits it here, where there is no start list
            # left to disturb.
            self._submit_deferred_fold()
            self._collect(self._pending, wait=True)
            # The companion's SQLite connection belongs to the thread that made it,
            # which is this worker, so it is closed from here and not by whoever closes
            # the recording afterwards.
            folder.submit(self.recording.close_companion).result()
            folder.shutdown(wait=True)
            self._folder = None

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

    def _submit_deferred_fold(self) -> None:
        """Start the fold a previous method frame is owed, now that nothing waits on it.

        Called once the start list has been walked and the gate checked, which is the
        one stretch of a run where the folding thread must not be competing for the
        interpreter: everything between `acquire frame` and the release is a serial
        round trip whose *answer* is what tells the loop the experiment has begun.
        """
        if self._fold_due is None or self._folder is None:
            return
        self._pending.append(self._folder.submit(self._fold, self._fold_due))
        self._fold_due = None

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
        self._lower_enable(method_frame, repetition)
        request = self.recording.begin_frame(method_frame, repetition)
        self.report(FrameBegun(method_frame, repetition, request.frame_number,
                               acquisition.console_frames))
        seen: list[Batch] = []
        began = self.clock()
        outcome, detail = "", ""
        timings: dict[str, float] = {}
        """What `release` measured, which only it can: it runs inside `run_frame`."""

        def on_batch(batch: Batch) -> None:
            seen.append(batch)
            self.report(BatchSeen(method_frame, repetition, batch))

        def release() -> None:
            self._check_first_gate(method_frame, repetition)
            if self.rearm_with_reset and (method_frame, repetition) > (1, 1):
                # The fallback the sync design names for a box whose table does not
                # re-arm itself after a software trigger. Unlocked by a bench answer,
                # not by this loop's opinion.
                #
                # Every console frame but the very first of the run needs it, which is
                # not the same as every repetition but the first: `repetition` counts
                # within a method frame and starts again at 1 for the next one, while
                # the table that has to be re-armed was spent by the previous method
                # frame's last repetition. A run's own first frame is excluded because
                # `send_phases` armed the box, and a replicate's because `run` has just
                # walked the same reset list.
                self._walk(self.method.reset, "reset")
            began_list = time.perf_counter()
            self._walk(self.method.start, "start", gap=self.start_step_gap)
            timings["start_list"] = time.perf_counter() - began_list
            if self.guard_gate:
                self._check_gate(method_frame, repetition)
            self._submit_deferred_fold()

        try:
            try:
                run_frame(self.console, self.stream, request,
                          timeout=self.frame_timeout, on_batch=on_batch,
                          release=release, tick=self._note_box_events,
                          settle=self.empty_settle)
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
            wait_began = self.clock()
            trailing, ended_by, settle_seconds = self._wait_for_frame(
                request, seen, on_batch
            )
            wait_seconds = self.clock() - wait_began
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
            rows_at_end=rows_after,
            started_s=began - self.started,
            seconds=self.clock() - began,
            wait_seconds=wait_seconds,
            ended_by=ended_by,
            settle_seconds=settle_seconds,
            start_list_seconds=timings.get("start_list", 0.0),
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

    def _wait_for_frame(
        self,
        request: FrameRequest,
        seen: Sequence[Batch],
        on_batch: Callable[[Batch], None],
    ) -> tuple[int, str, float | None]:
        """Wait until the frame is over, and say which of the two rules ended it.

        A frame's batches mostly arrive after its own `finished` and the console's writer
        trails even those, so waiting is what makes a frame's scan count the frame's and
        lets the completion marker mean something. It doubles as the precondition for the
        next frame's gate guard: what the guard sees has to be this frame's doing, not
        the last one's backlog. What changed in task 34 is not that the loop waits but
        what it waits *for*.

        **The frame has counted out**, which is the ordinary end: the data socket has
        delivered `frame_length` scans for this frame, so nothing more is coming on that
        link, and the file's row count has then held still for `ROW_SETTLE_S`, which is
        the console's other subscriber saying it has caught up. Both halves are needed.
        The count alone says nothing about the writer, which is a separate thread behind
        a queue of its own; the pause alone cannot tell a writer that has finished from
        one that is between batches.

        The row count is a pause and not a total on purpose. The console writes a row for
        a scan whose encoded spectrum holds more than one element, and for scan 0
        whatever it holds, so a push that crossed the zero suppress threshold nowhere
        stores no row and a real frame holds fewer rows than it has scans
        (`docs/console-protocol.md`, "Division of UIMF writing"). There is no number to
        count up to. That same rule is why an unreadable count is not settled on: any
        frame that acquired its first scan has at least one row, so `rows_in` answering
        None means the file cannot be read rather than that the frame is empty, and the
        silence below is the right end for a frame nothing can be learned about.

        **The stream has fallen silent** for `SILENCE_S`, which ends a frame that never
        reaches its count: a gate trip, a refused start, an error on `status`, a batch
        the socket dropped. It is also the backstop for a frame that counted out and
        whose rows could not be read, which is a file that cannot be opened rather than
        a frame with nothing in it; that frame is reported as counted with no settle.

        Returns the trailing batches, which of the two ended it, and how long after the
        count completed the file's row count last moved (`FrameRecord.settle_seconds`).
        """
        asked = int(request.frame_length)
        frame = request.frame_number
        began = time.perf_counter()
        quiet_since = began
        trailing = 0
        counted_at: float | None = None
        rows = self.recording.rows_in(frame)
        rows_moved_at = began
        while True:
            # First, so that a box event raised during the frame is timestamped within
            # a poll of arriving rather than at the end of this wait (lab record,
            # task 34). The ports are read without blocking.
            self._note_box_events()
            if counted_at is None and sum(batch.scans for batch in seen) >= asked:
                counted_at = time.perf_counter()
            poll_for = self.frame_poll
            if counted_at is not None:
                now = time.perf_counter()
                reading = self.recording.rows_in(frame)
                if reading is not None and reading != rows:
                    rows, rows_moved_at = reading, now
                elif reading is not None and now - rows_moved_at >= self.row_settle:
                    return trailing, "counted", max(0.0, rows_moved_at - counted_at)
                if reading is not None:
                    # Wake when the settle is due rather than on the next whole poll.
                    # The settle is the largest part of what a repetition now costs, so
                    # a poll's worth of overshoot on top of it is a quarter of the
                    # budget (lab record, task 34).
                    poll_for = min(poll_for, self.row_settle - (now - rows_moved_at))
            if time.perf_counter() - quiet_since >= self.silence:
                # The backstop, and the only end a frame short of its count has. A frame
                # that counted out and got here instead could not have its rows read at
                # all, which `settle_seconds` of None is the sign of.
                return trailing, ("counted" if counted_at is not None else "silence"), None
            event = self.stream.poll(max(0.0, poll_for))
            if event is None:
                continue
            quiet_since = time.perf_counter()
            if isinstance(event, Batch):
                trailing += 1
                on_batch(event)

    def _lower_enable(self, method_frame: int, repetition: int) -> None:
        """Put the digitizer's gate down by command, before the frame is asked for.

        Only under `single_frame`, and only between method frames. That mode's table
        loops on the box and raises the enable once, so every frame after the run's
        first would meet a gate the previous frame left high; `per_repetition`'s table
        lowers the line itself one batch past its last counted scan, and a round trip
        through local mode per repetition would cost a hundred of them per method frame
        against a dead time the lab record's task 34 is trying to cut.

        **Before `acquire frame` and not inside the release.** The invariant is that the
        gate is low when the console is asked for the frame, so lowering it a few
        milliseconds afterwards would leave the card free to take records the frame
        counts -- a batch short of publishing anything, and so invisible to both halves
        of the gate check.

        It also re-arms the box, which is the same `SMOD,LOC` / `SMOD,TBL` a replicate's
        reset list makes, so a `single_frame` table spent by the previous method frame
        is ready for the next `TBLSTRT` without `rearm_with_reset` as well.
        """
        acquisition = self.method.acquisition
        if (acquisition.enable is None
                or acquisition.repetition_mode != "single_frame"
                or (method_frame, repetition) == (1, 1)):
            return
        self._walk(_enable_steps(acquisition.enable), "enable")

    def _check_first_gate(self, method_frame: int, repetition: int) -> None:
        """Hold the run's first frame open long enough to catch a gate that is not shut.

        The per-frame guard below reads a silence of a few milliseconds -- the start
        list -- and infers from it that the gate was shut when the frame was asked for.
        That inference is sound and it is also thin, and it is thin in exactly the case
        that matters most: a chain opened with `ungate` whose re-enable never reached the
        card acquires every frame of the run and looks like a run that worked, because
        the first batch of an ungated frame arrives about 135 ms after `acquire frame`
        and the start list is long gone by then (lab record, task 26).

        So once per run the loop waits where nothing should arrive. The frame has been
        asked for, nothing has been released, and a digitizer that is recording anyway
        has `gate_dwell` seconds to publish a batch and prove it. Silence through that
        window is the invariant holding, reported as `GateChecked`, and it is the only
        positive evidence the run has: every frame after this one is released against a
        gate this frame proved shut.

        **Once, and on the first frame, for two reasons.** A dwell per frame would be
        spent on every repetition of a hundred and is the per-repetition dead time the
        lab record's task 34 is trying to cut. And the three ways the gate can be open --
        the enable lead off Control I/O 2, whose input is pulled up; a table from an
        earlier run that left the enable high; the console's enable input still disabled
        behind an ungated chain -- are all in place before the run's first frame and none
        of them arrives part way through one. A check that passes here has ruled out all
        three.

        It marks itself done only when it passes, so a gate that is open fails frame
        after frame until `abort_after` ends the run, which is the right end for a
        failure that is not going to clear up.

        What is not a batch goes back on the stream, the same way the per-frame guard
        puts it back: a status message belongs to the wait for this frame's end.
        """
        if self._gate_checked or not self.guard_gate or self.gate_dwell <= 0:
            return
        began = time.perf_counter()
        deadline = began + self.gate_dwell
        held: list[Batch | Status] = []
        stray = 0
        while stray == 0:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            event = self.stream.poll(min(remaining, 0.05))
            if event is None:
                continue
            if isinstance(event, Batch):
                stray += 1
            else:
                held.append(event)
        for event in reversed(held):
            self.stream.unread(event)
        seconds = time.perf_counter() - began
        if stray:
            raise EnableGateError(
                f"frame {method_frame}.{repetition}: the console published a batch "
                f"{seconds:.3f} s after the frame was asked for and before anything had "
                "been released, so the digitizer was recording with nothing to record. "
                "Either the enable lead is off Control I/O 2, whose input is pulled up "
                "so that an unconnected one reads high and the card acquires every push; "
                "or a table left the enable high behind an earlier run; or this chain "
                "was opened with the enable input disabled and enabling it again did not "
                "reach the card. Every frame of this run would be a frame of plausible "
                "data at the wrong offset"
            )
        self._gate_checked = True
        self.report(GateChecked(method_frame, repetition, seconds))

    def _check_gate(self, method_frame: int, repetition: int) -> None:
        """Refuse a frame that was already recording before its start list finished.

        A batch is `NotifyOnScansCount` scans -- 500 on the instrument, 64.5 ms of
        recording -- against a start list of a few serial round trips, and the previous
        frame's backlog has been waited out. So anything on the data topic here is stray
        recording, and there are exactly two ways to get it: a table that left the enable
        high, or the enable lead off the card. Nothing else in the system notices either,
        and both produce a full frame of plausible data at the wrong offset (lab record,
        task 05).

        That reading holds only while the start list really does cost a few round trips.
        A box releases its table when it *parses* the command, and the host learns of it
        when the echo comes back, so a slow acknowledgement is recording the guard
        cannot distinguish from a stale gate: the one time this fired on hardware, a
        `TBLSTRT` took 226 ms to answer against the 0-7 ms of every other frame in the
        run, and the frame it failed was a good one (lab record, task 33). What made the
        acknowledgement slow was the fold of the previous method frame, which is why the
        fold is now started after this check rather than before it.

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

    def _walk(self, steps: Sequence[Step], phase: str, *, gap: float = 0.0) -> None:
        """One ordered method-level sequence, box by box, in the order written.

        The order is part of the experiment rather than a convenience: a box whose
        compression table must already be waiting at its first hold is told to run
        before the box that issues the release edge is triggered, and `TBLSTRT` is last
        because it is what starts everything.

        `gap` separates the steps in time as well as in order. Order alone leaves them
        as close together as two serial writes happen to be, which is sometimes not
        apart at all, and the box that has to be waiting needs a real interval
        (`START_STEP_GAP_S`).
        """
        for index, step in enumerate(steps):
            if index and gap:
                time.sleep(gap)
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
