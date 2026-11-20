"""The owner of the hardware, in this process: one thread, one queue, every wire call.

Every serial write, every ZeroMQ round trip and every second of an acquisition happens
on the thread that runs `serve`. The rule it exists to keep is the first one on the
never-do list: an `STBLDAT` table string has a three-second inter-token timeout on the
box, so a send that stalls behind a repaint is a table the box silently abandons, and a
`run_acquisition` on a UI thread would freeze a window for the length of an experiment.
Taken out of `clockwork.app.worker.Worker` whole (lab record, task 67); `Worker` is now
the adapter that runs `serve` on a `QThread` and turns `on_event` into its signals.

**A work queue, not a mailbox.** Every job must happen -- a send that was overtaken by
the next one is a half-configured instrument -- so jobs queue and run one at a time, in
order, each bracketed by `JobStarted` and `JobFinished` or `JobFailed`.

**Progress goes two ways.** `on_event(handle, event)` is called for every event as it
happens, on whichever thread reported it (the loop's, or the folding worker's for
`Folding`), and must be quick and thread-safe -- a `list.append`, the window's mailbox.
Every event is also kept against its job's handle, numbered, for `events(handle,
after)`: a front end in another process reads that instead. What is kept is bounded the
way the window's mailbox is: consecutive `BatchSeen`s collapse into the newest, a
`BatchSeen` is kept with its scan count and none of its arrays (`Batch.kept`: the
summed spectrum is hundreds of kilobytes of display product a batch that no front end
draws, mainspring being the only viewer, and nothing past the loop reads the per-scan
`tic` or `time_stamps`; a full-cap history with them was ~55 MB), each job
keeps at most `limit` events, and only the last `history` jobs are kept at all. Beside
each job's history is its `JobSummary`, kept as the events arrive, which is what a
follower reads instead of the history (`summary`; lab record, task 170).

**The boxes are opened once and kept.** Closing a port drops DTR, which makes the
firmware reset its own USB port and re-enumerate, so an owner that opened a box per send
would reset the rack on every button (lab record, task 37). `discover` hands back open
boxes and this owner holds them until it shuts down, through every rescan: a scan hands
the open ones back to `discover` rather than closing them first (lab record, task 62).

**The instrument lock is taken at construction and before every job.** A refused owner
still comes up -- a window must be able to say why its buttons are grey -- but every job
it is given fails with the lock's own sentence until the holder is gone, and the next
job after that takes the lock and runs. `fake=True` never takes it: a rehearsal opens no
port and must be able to run beside a real session.

**A person's pause is this owner's state, and it holds the queue** (`clockwork.owner.hold`;
lab record, task 167). `pause` is accepted only while a series runs and names where it is
to rest: after the point in flight, where the series waits between two points with no
file open and the console's chain closed; or after the repetition in flight, where the
loop itself waits inside the file (`run_acquisition`'s `hold`), or, that repetition being
its file's last, where the series waits before its next file. Nothing bounds the wait:
the job stays the one running, every job queued behind it waits, and `resume`, `stop` or
`shutdown` is what ends it, the last two as a series stopped while paused. `pause`,
`resume`, `note` and `stop` are called from another thread than the one that runs jobs
and never block on it; each is written, with who made it, into the run record of the job
it acted on, and a note typed with nothing running into the day record.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace

from .. import keep, transcript
from ..acq import (
    SECONDS_PER_SAMPLE_2GSPS,
    AcqError,
    BatchSeen,
    BeamGuard,
    BoxLost,
    Console,
    ConsoleConfig,
    ConsoleProcess,
    ConsoleSupervisor,
    DataStream,
    Event,
    FakeConsoleProcess,
    Provenance,
    Run,
    Series,
    Snapshot,
    TofWidth,
    Warned,
    prepare_console,
    refusals,
    run_acquisition,
    send_phases,
    start_chain_retrying,
)
from ..acq.fake import INSTRUMENT_PERIOD_SAMPLES
from ..acq.standing import stand_down, warm_up
from ..dayrecord import DayRecord
from ..envelope import Limits, cold_start, judge
from ..instrument import Instrument
from ..method import Method
from ..method.template import Rendered, loads_template, render
from ..mips import (
    Box,
    BoxHealth,
    BoxState,
    Discovery,
    FakeBox,
    Found,
    MipsError,
    discover,
    read_health,
    read_state,
)
from ..naming import files_of, next_stem, parse_stem
from ..record import find as find_record
from .hold import BEAM_GUARD, POINT, REPETITION, Guarding, Hold, HoldRefused
from .hold import now as _now
from .hold import refusal as pause_refusal
from .interface import (
    BoxStateRead,
    ConsoleChanged,
    Discovered,
    GuardChanged,
    Handle,
    Held,
    JobFailed,
    JobFinished,
    JobStarted,
    JobSummary,
    LogsOpened,
    Noted,
    OwnerStatus,
    PauseAsked,
    PointStarted,
    Progress,
    RecordBegun,
    Resumed,
    RunDone,
    Said,
    StaleHandle,
    StopAsked,
)
from .jobs import (
    Acquire,
    Armed,
    ConsoleStatus,
    Discover,
    Job,
    PlannedPoint,
    ReadState,
    RestartConsole,
    Send,
    SendResult,
    SeriesJob,
    SeriesResult,
    SnapshotBoxes,
    SnapshotResult,
    StandDown,
    StandingResult,
    StartConsole,
    WarmUp,
    matches_wire,
    wire_fingerprint,
)
from .keeper import RecordKeeper, begin, stem_of
from .lock import Holder, InstrumentLock, LockError
from .wire import to_wire

__all__ = ["FAKE_FRAME_HOLD_S", "HOLD_POLL_S", "LocalOwner", "fake_rack", "sentence"]

HOLD_POLL_S = 0.2
"""How often a series resting between two files looks at its boxes' ports and at whether
it has been resumed or stopped. A resume or a stop wakes it at once; this is the cadence
of the drain, and of noticing a box that was unplugged while the series rested."""

FAKE_FRAME_HOLD_S = 0.25
"""How long `FakeConsole` waits inside `acquire frame` before publishing, under `--fake`.

The stand-in publishes from the handler for `acquire frame`, so without this its
batches race the start list and the enable-gate guard fires correctly on a fault that
is not there. It stands in for the pushes a real frame spends waiting for its enable.
Taken from the bench script that first needed it (lab record, task 28).
"""

_CLOSING = "clockwork is shutting down, so this job was never started"

RECORD_DRAIN_S = 120.0
"""How long the owner waits, at a job's end, for its files to be entered in the run record
before reporting the end anyway: a summary reads every frame, seconds for a long run."""

_LOOP_LOG = logging.getLogger("clockwork.acq.loop")
"""The loop's own logger, for the one line the owner adds to a run's events: why it
stopped. The transcript follows `clockwork.acq.loop` and not the owner."""


# -- the rack ----------------------------------------------------------------------


def fake_rack(method: Method) -> dict[str, Box]:
    """One `FakeBox` per box the method names, shaped like the box it stands in for.

    A stand-in is given ARB modules when the method sends it anything from section 6 of
    the wire format, because a box without them NAKs the whole of it with the
    firmware's own code for "no ARB module in system" and a rehearsal would fail on the
    visitor's `setup` rather than on anything real. A sequencer stand-in gets the DC
    bias bank and the two RF heads AUKLET has instead, so a method that declares them
    rehearses its setters too. Both readings are the bench script's (lab record,
    task 28).

    **An ARB stand-in has no DC bias bank and answers two RF heads at 0 % drive**,
    which is what the instrument's ARB boxes answer with no RF board fitted
    (`GCHAN,DCB` 0, `GCHAN,RF` 2, lab record, task 71). A stand-in with sixteen
    channels at 0 V would be refused by the cold-start check on every rehearsal for
    settings no real ARB box has.
    """
    rack: dict[str, Box] = {}
    for entry in method.boxes:
        arb = 4 if any(
            command.startswith(("SARB", "SWF", "SALTWFM", "ARBSYNC", "TARB"))
            for command in tuple(entry.setup) + tuple(entry.load)
        ) else 0
        rack[entry.name] = Box(
            transport=FakeBox(arb_modules=arb, rf_channels=2,
                              dcb_channels=0 if arb else 16),
            name=entry.name,
        )
    return rack


# -- the owner ---------------------------------------------------------------------


class LocalOwner:
    """The instrument, behind a queue, in this process.

    Built idle: `start()` gives it a thread of its own, or a caller that already has one
    (`Worker`'s `QThread`) calls `serve()` on it. `shutdown()` is what lets `serve`
    return; it closes every box and the console and releases the lock on the way out.

    `discover` is the scan, injectable so a test can stand a rack in for the ports.
    """

    def __init__(
        self,
        *,
        fake: bool = False,
        on_event: Callable[[Handle, Event], None] | None = None,
        program: str = "clockwork",
        lock_path: str | None = None,
        discover: Callable[..., Discovery] = discover,
        limit: int = 20000,
        history: int = 64,
        kept_root: str | None = None,
        errors_log: str = "",
    ) -> None:
        self.fake = fake
        self.kept_root = keep.root() if kept_root is None and not fake else (kept_root or "")
        """Where a failed run's files are copied (`clockwork.keep`), or empty for nowhere.

        The configured root by default; nothing by default under `--fake`, whose
        failures are not evidence about an instrument. A front end with a setting of its
        own assigns this; it is read once per failure, on this owner's thread."""
        self.errors_log = errors_log
        """A front end's error log, copied with a failed run's files. The owner has
        none of its own and does not know where the window keeps one."""
        self._kept = ""
        """The folder the running job's failure was copied into, for its `JobFailed`."""
        self.program = program
        self.session = uuid.uuid4().hex[:12]
        """This owner's own name for itself, stamped into every handle it issues, so
        that a handle from an owner that has since stopped is refused rather than read
        as this one's job of the same number (lab record, task 68)."""
        self._on_event = on_event
        self._listeners: list[Callable[[Handle, Progress], None]] = []
        self._scan = discover
        self._limit = limit
        self._history_jobs = history

        self.boxes: dict[str, Box] = {}
        """Open boxes, keyed by `GNAME`. Owned by this owner; nothing else closes one."""

        self._held: dict[str, Box] = {}
        """Every box the last scan left open, keyed by port: `boxes` and the ones a
        method cannot address (no name, or a name two boxes answer to). The next
        scan is handed all of them, since closing one resets it (lab record, task
        62) and leaving one out would keep its port busy with nothing holding it."""

        self.listings: dict[str, frozenset[str]] = {}
        """The `GCMDS` cache, kept for the session. Three boxes cost about five
        seconds of listing on the first send and 0.3 s on every one after
        (`send_phases(listings=)`)."""

        self._snapshot: Snapshot | None = None
        """What the last `Send` read off the boxes, and what every run until the next
        send is stamped with."""

        self._armed: Armed | None = None
        """What the last `Send` that finished put on the boxes (`OwnerStatus.armed`)."""

        self.console: ConsoleSupervisor | None = None
        self.console_status = ConsoleStatus()

        self._queue: queue.Queue[tuple[Handle, Job] | None] = queue.Queue()
        self._closing = False
        """Set by `shutdown`: every job still queued behind it fails unstarted. Without
        it a queued Acquire would begin after the stop meant to end the session, since
        `_acquire` clears the stop flag for its own series."""
        self._stop = threading.Event()
        self._stop_reason = ""
        self._setup_log: tuple[str, str] | None = None
        """`(directory, stem)` of the send log that holds the last setup send.

        Only for the sentence a replicate's own log carries, saying where the strings it
        did not re-send went. What decides whether a log is appended to is `_send_log`,
        which is every send and not only a setup one."""

        self._send_log: tuple[str, str] | None = None
        """`(directory, stem)` of the last send log this owner opened, of any kind.

        The one thing that decides `append`. A send used to open its log `append=False`
        unconditionally, so a **Load and arm destroyed the Send setup log that preceded
        it under the same stem** -- and the 2026-09-17 sitting then read the emptied file
        and reported that the CLOCK `setup` had never been sent, on the strength of
        which it was sent again. It had gone out before every CLOCK series that
        afternoon, which the transcripts, which append, said plainly. A stem is a piece
        of work and its send log is that work's record, so everything done under one
        stem is added to it and only a new stem starts a new file (lab record, task 56).

        The rule that bought stands after the fix: **"was this string ever sent" is a
        transcript question and never a send-log one.**"""

        self._last_run: Run | None = None

        self._records = RecordKeeper()
        """Enters each file a request's job writes, and the note a failure or a stopped
        series leaves, into that request's run record (`keeper`, lab record, task 164)."""
        self.limits: Limits | None = None
        """The limits the sitting carries (`clockwork.sitting`), against which a window
        run's record says which knobs lie outside an agent's range; None for none. Set
        by the daemon from its flags, and read on this owner's thread."""
        self.day: DayRecord | None = None
        """The day record this owner writes its console's starts and stops into, or None
        for none: the daemon's, in its output directory (`clockwork.dayrecord`)."""
        self._point: PointStarted | None = None
        """The series point under way, for the note a failure leaves in its record."""

        self._job: Job | None = None
        """The job running, as it runs (a window run's with its request id), for the
        record a stop, a pause or a note is written into. Under `_guard`."""
        self._at: tuple[int, Method | None] = (0, None)
        """The series point in flight: its place among the points as acquired, from 1,
        and its method; `(0, None)` outside a series and before its first point, which
        is when no pause is accepted. Under `_guard`, with the two below."""
        self._hold: Hold | None = None
        """A person's pause on the series under way (`OwnerStatus.hold`)."""
        self._held_from = 0.0
        """When the series came to rest, on `time.monotonic`."""
        self._lifted = threading.Event()
        """Set by `resume` and by `stop`, so a series resting between two files wakes
        at once rather than on its next look."""
        self._stem = ""
        """The stem of the file in flight, for the words that say where a hold rests."""
        self._guarding: Guarding | None = None
        """The beam guard every acquisition watches, and who armed it
        (`OwnerStatus.guard`); None for none. Set by `guard`, from any thread, and read
        by the loop at every repetition (lab record, task 168)."""

        self._ids = itertools.count(1)
        self._issued = 0
        """The newest id issued (`OwnerStatus.issued`), under `_guard` with the id."""
        self._seqs = itertools.count(1)
        self._guard = threading.Lock()
        """Over `_history` and `_pending`, which the owner's thread, a folding worker
        and any caller of `events` or `status` all touch."""
        self._history: dict[int, list[Progress]] = {}
        self._summaries: dict[int, JobSummary] = {}
        self._pending: list[Handle] = []
        self._running: Handle | None = None
        self._current: Handle | None = None
        """The job the next event belongs to: the one running, or the one that last
        ran, for an event that lands after its job has been reported finished."""
        self._thread: threading.Thread | None = None

        self._lock = None if fake else InstrumentLock(program, lock_path)
        self._refused = ""
        self._refused_by: Holder | None = None
        if self._lock is not None:
            with contextlib.suppress(LockError):
                self._hold_lock()

    # -- the protocol --------------------------------------------------------

    def submit(self, job: Job, *, origin: str = "") -> Handle:
        """Queue a job. Never blocks; any thread may call it.

        After `shutdown` the job is not queued at all: it is reported failed at once,
        because the thread that would have run it may already be gone. `origin` names
        the client for every other one (`Handle.origin`).
        """
        with self._guard:
            handle = Handle(id=next(self._ids), kind=type(job).__name__, label=job.label,
                            owner=self.session, origin=origin)
            self._issued = handle.id
            self._history[handle.id] = []
            self._summaries[handle.id] = JobSummary(handle=handle)
            if not self._closing:
                self._pending.append(handle)
            self._forget_old_jobs()
        if self._closing:
            self._report(JobFailed(handle=handle, job=job, message=_CLOSING), handle)
        else:
            self._queue.put((handle, job))
        return handle

    def _check_session(self, handle: Handle) -> None:
        if handle.owner and handle.owner != self.session:
            raise StaleHandle(
                f"job {handle.id} ({handle.label}) was submitted to a different clockwork "
                "owner, most likely one that has since stopped, and its progress went "
                "with it")

    def events(self, handle: Handle, after: int = 0) -> list[Progress]:
        self._check_session(handle)
        with self._guard:
            return [entry for entry in self._history.get(handle.id, ())
                    if entry.seq > after]

    def summary(self, handle: Handle) -> JobSummary:
        self._check_session(handle)
        with self._guard:
            kept = self._summaries.get(handle.id)
        return kept if kept is not None else JobSummary(handle=handle)

    def stop(self, reason: str = "stopped by the operator", *, by: str = "",
             client: str = "") -> None:
        """Ask the run in flight to end after the current repetition and its fold.

        Sets a flag the acquisition thread reads between repetitions; it does not
        interrupt anything. A repetition is about a second, so the button feels
        immediate and what it leaves on disk is a short experiment rather than a
        broken one.

        **A paused series ends where it rests**, as stopped while paused, and the pause
        goes with it (the window revision's B5). **Every stop of an acquisition is
        written into its run record's `interventions`** with who asked, `by` a person's
        initials and `client` the client it came through, so that a person's Stop at
        the window and an agent's `stop` are told apart afterwards (A4).
        """
        with self._guard:
            hold, self._hold = self._hold, None
            job, handle = self._job, self._running
        paused = hold is not None and hold.reached
        if paused:
            reason = f"{reason} (while {hold.text})"  # type: ignore[union-attr]
        self._stop_reason = reason
        self._stop.set()
        self._lifted.set()
        if handle is None or not isinstance(job, (Acquire, SeriesJob)):
            return
        client = client or self.program
        self._report(StopAsked(reason=reason, by=by, client=client, while_paused=paused),
                     handle)
        self._intervene(job, handle, kind="stop", by=by or client, client=client,
                        reason=reason, **({"while_paused": True} if paused else {}))

    def pause(self, grain: str = POINT, *, by: str, client: str = "") -> Hold:
        """Hold the series under way after its point or its repetition in flight.

        Answers at once with the `Hold` as asked for; the series goes on to the boundary
        `grain` names and `Held` says when it rests there. A pause asked for while one
        is still on its way replaces it, which is how the grain is changed. `HoldRefused`
        with one sentence where nothing can be held (`clockwork.owner.hold.refusal`).
        Never blocks; any thread may call it.
        """
        by = by.strip()
        if not by:
            raise HoldRefused("a pause is a person's: give the initials of who is at "
                              "the window")
        with self._guard:
            job, handle = self._job, self._running
            number, method = self._at
            why = pause_refusal(job if handle is not None else None, grain, number=number,
                                method=method, hold=self._hold,
                                stopping=self._stop.is_set() or self._closing)
            if why:
                raise HoldRefused(why)
            assert handle is not None
            hold = self._hold = Hold(job=handle.id, grain=grain, by=by, client=client,
                                     asked=_now())
            self._lifted.clear()
        self._report(PauseAsked(hold=hold), handle)
        self._intervene(job, handle, kind="pause", grain=grain, by=by, client=client)
        return hold

    def resume(self, *, by: str, client: str = "") -> None:
        """Lift the pause: a series at rest goes on, and one still on its way to a pause
        is no longer to be paused. `HoldRefused` where there is no pause. The first
        repetition after a hold inside a file is re-armed as a run's first is, and the
        file and the record say how long the hold was (`run_acquisition`'s `hold`)."""
        by = by.strip()
        if not by:
            raise HoldRefused("a resume is a person's: give the initials of who is at "
                              "the window")
        with self._guard:
            hold, self._hold = self._hold, None
            job, handle = self._job, self._running
            seconds = (time.monotonic() - self._held_from
                       if hold is not None and hold.reached else 0.0)
        if hold is None:
            raise HoldRefused("nothing is paused, so there is nothing to resume")
        self._lifted.set()
        if handle is None:
            return
        self._report(Resumed(hold=hold, by=by, client=client, seconds=seconds), handle)
        self._intervene(job, handle, kind="resume", grain=hold.grain, by=by, client=client,
                        held_since=hold.since, held_s=round(seconds, 1), where=hold.where)

    def note(self, text: str, *, by: str, client: str = "") -> str:
        """Keep a note a person typed at the window, and answer where it went.

        Into the run record of the job running, under their initials, and to every
        client following that job as a `Noted` event: a record, never a command (the
        window revision's C1). With no job running, or one that has no run record, into
        the day record (C2). `HoldRefused` for an empty note, no initials, or a note
        with nowhere to go, which is an owner with no day record.
        """
        text, by = " ".join(text.split()), by.strip()
        if not text:
            raise HoldRefused("a note says something: type its text")
        if not by:
            raise HoldRefused("a note is a person's: give the initials of who is at the "
                              "window")
        with self._guard:
            job, handle = self._job, self._running
        if handle is not None and isinstance(job, (Acquire, SeriesJob)) and job.series:
            directory = job.directory or os.getcwd()
            if find_record(directory, job.series) is not None:
                self._report(Noted(note=text, by=by, client=client), handle)
                self._records.person_note(directory, job.series, text, by, client)
                return f"the run record of job {handle.id}, request {job.series}"
        if self.day is None:
            raise HoldRefused("no job with a run record is running, and a note typed "
                              "then goes to the day record, which this owner does not "
                              "keep: the note was not kept")
        self.day.write("note", text=text, by=by, client=client,
                       job=handle.id if handle is not None else None)
        if handle is not None:
            self._report(Noted(note=text, by=by, client=client), handle)
        return "the day record"

    def guard(self, guard: BeamGuard | None, *, by: str, client: str = "") -> str:
        """Arm, change or disarm (None) the beam guard, as a person at the window, and
        answer where the change was recorded.

        The owner's from now on (R9, B8; lab record, task 168): every acquisition watches
        it from its next repetition, whichever client started it, until it is disarmed or
        the owner ends. Each change is an intervention under the person's initials, in
        the run record of the job running, or with nothing running, or a job that has no
        record, a `guard` line in the day record; and a `GuardChanged` event on the job
        running. No MCP tool or command-line verb sends this (B8). `HoldRefused` with no
        initials. Never blocks; any thread may call it.
        """
        by = by.strip()
        if not by:
            raise HoldRefused("the beam guard is a person's: give the initials of who is "
                              "at the window")
        guarding = (Guarding(guard=guard, by=by, client=client, since=_now())
                    if guard is not None else None)
        with self._guard:
            before, self._guarding = self._guarding, guarding
            job, handle = self._job, self._running
        if before is not None and guarding is not None and before.guard == guard:
            return "nowhere: the beam guard was already armed so"
        fields: dict[str, object] = {
            "armed": guard is not None,
            "floor": guard.floor if guard is not None else None,
            "repetitions": guard.repetitions if guard is not None else None,
            "source": guard.source if guard is not None else "",
            "was": (None if before is None else {"floor": before.guard.floor,
                                                 "repetitions": before.guard.repetitions,
                                                 "source": before.guard.source})}
        if handle is not None:
            self._report(GuardChanged(guarding=guarding, by=by, client=client), handle)
        if (handle is not None and isinstance(job, (Acquire, SeriesJob)) and job.series
                and find_record(job.directory or os.getcwd(), job.series) is not None):
            self._intervene(job, handle, kind="guard", by=by, client=client, **fields)
            return f"the run record of job {handle.id}, request {job.series}"
        if self.day is None:
            return "nowhere: no job with a run record is running and this owner keeps no " \
                   "day record"
        self.day.write("guard", by=by, client=client,
                       job=handle.id if handle is not None else None, **fields)
        return "the day record"

    def _beam_guard(self, job: Acquire) -> Callable[[], BeamGuard | None]:
        """What a run of `job` watches, asked at every repetition: the job's own guard,
        where a caller built one in, else the owner's as it stands then."""
        def current() -> BeamGuard | None:
            if job.beam_guard is not None:
                return job.beam_guard
            guarding = self._guarding
            return guarding.guard if guarding is not None else None
        return current

    def snapshot(self) -> Snapshot | None:
        return self._snapshot

    def status(self) -> OwnerStatus:
        with self._guard:
            queued = tuple(self._pending)
            issued = self._issued
            hold = self._hold
            guarding = self._guarding
        held = self._lock is not None and self._lock.held
        return OwnerStatus(
            program=self.program,
            fake=self.fake,
            console=self.console_status,
            boxes=tuple(self.boxes),
            ports=tuple(self._held),
            running=self._running,
            queued=queued,
            stopping=self.stopping,
            snapshot=self._snapshot is not None,
            armed=self._armed,
            holder=self._lock.holder if held else self._refused_by,
            refused=self._refused,
            issued=issued,
            hold=hold,
            guard=guarding,
        )

    def shutdown(self, reason: str = "the owner is shutting down") -> None:
        """Stop the thread and let go of everything it owns. Idempotent.

        The run in flight ends after its current repetition and its fold, as a Stop
        would end it, and a paused series as stopped while paused; every job queued
        behind it fails without starting.
        """
        self._closing = True
        self.stop(reason, client=self.program)
        self._queue.put(None)

    # -- beside the protocol, for a caller in this process ---------------------

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def refused(self) -> str:
        """The lock's sentence while another owner holds the instrument, else empty."""
        return self._refused

    @property
    def closing(self) -> bool:
        """Whether `shutdown` has been asked for."""
        return self._closing

    def listen(self, callback: Callable[[Handle, Progress], None]) -> None:
        """Also hand every numbered entry to `callback`, as it is kept.

        For the daemon's event stream (`clockwork.owner.remote`), which has to publish
        the entries in the order they were numbered: `on_event` is called outside the
        owner's lock, so two threads reporting at once (the loop and the folding worker)
        can reach it out of order. **`callback` is called with that lock held** and must
        do nothing but hand the entry on (a `queue.put`); calling back into this owner
        from it deadlocks. What it is given is what `events` would return, a `BatchSeen`
        without its arrays (`Batch.kept`) included.
        """
        with self._guard:
            self._listeners.append(callback)

    def start(self) -> LocalOwner:
        """Run `serve` on a thread of this owner's own."""
        if self._thread is None:
            self._thread = threading.Thread(target=self.serve, name="clockwork owner")
            self._thread.start()
        return self

    def join(self, timeout: float | None = None) -> bool:
        """Wait for `start()`'s thread to end, after `shutdown()`. True if it has."""
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    # -- the loop ------------------------------------------------------------

    def serve(self) -> None:
        """Run jobs until `shutdown()`, on the calling thread, then close everything."""
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                handle, job = item
                with self._guard:
                    if handle in self._pending:
                        self._pending.remove(handle)
                if self._closing:
                    self._report(JobFailed(handle=handle, job=job, message=_CLOSING),
                                 handle)
                    continue
                self._running = self._current = handle
                self._kept = ""
                self._point = None
                # Every event names the job as it was submitted, the object a front end
                # knows its own by; the one run is a window run's with its request id.
                submitted = job
                self._report(JobStarted(handle=handle, job=submitted))
                job = self._begin_record(handle, job)
                with self._guard:
                    self._job = job
                try:
                    result = self._do(job)
                except Exception as exc:  # noqa: BLE001 -- a message, not a traceback
                    self._lapse(handle, job)
                    self._running = None
                    if isinstance(exc, BoxLost):
                        self._forget(exc.box)
                    message = sentence(exc)
                    if self._kept:
                        message = f"{message.rstrip('.')}. Files kept in {self._kept}"
                    self._record_end(handle, job, failed=message)
                    self._report(JobFailed(handle=handle, job=submitted, message=message))
                else:
                    self._lapse(handle, job)
                    self._running = None
                    self._record_end(handle, job, result=result)
                    self._report(JobFinished(handle=handle, job=submitted, result=result))
        finally:
            self._records.close(RECORD_DRAIN_S)
            self._close_everything()

    def _begin_record(self, handle: Handle, job: Job) -> Job:
        """The job as it runs: a window run's with its request id minted and its record
        begun (`keeper.begin`), said in a `RecordBegun` so every client following it
        learns the id; anything else as it came. A record that cannot be begun is said,
        and the run goes on without one."""
        if not isinstance(job, (Acquire, SeriesJob)) or not job.source or job.series:
            return job
        try:
            running, path = begin(job, handle.id, limits=self.limits)
        except Exception as exc:  # noqa: BLE001 -- the record is the run's, never its end
            self._report(Warned(f"the run record was not begun, and this run has none: "
                                f"{sentence(exc)}"))
            return job
        self._report(RecordBegun(request_id=running.series, path=path))
        return running

    # -- a person's pause --------------------------------------------------------

    def _intervene(self, job: Job | None, handle: Handle, *, kind: str, by: str,
                   client: str, grain: str = "", reason: str = "",
                   **more: object) -> None:
        """One entry in the `interventions` of the run record of the job acted on, where
        it has one (`docs/run-record.md`)."""
        if not isinstance(job, (Acquire, SeriesJob)) or not job.series:
            return
        self._records.intervention(job.directory or os.getcwd(), job.series, {
            "kind": kind, "grain": grain, "by": by, "client": client, "job": handle.id,
            "reason": reason, **more})

    def _reach(self, where: str) -> Hold | None:
        """The series has come to rest at the pause asked for: say since when and where,
        once. The hold as it now stands, or None if it was lifted on the way."""
        with self._guard:
            hold = self._hold
            if hold is None or hold.reached:
                return hold
            hold = self._hold = replace(hold, since=_now(), where=where)
            self._held_from = time.monotonic()
        self._report(Held(hold=hold))
        return hold

    def _hold_check(self, method_frame: int, repetition: int) -> str | None:
        """What `run_acquisition` asks between repetitions, after `_stop_check`: rest
        here for a pause at the inner grain, for as long as it stands. Must not block."""
        hold = self._hold
        if hold is None or hold.grain != REPETITION:
            return None
        if not hold.reached:
            self._reach(f"after method frame {method_frame} repetition {repetition} "
                        f"of {self._stem}")
        return f"paused by {hold.by} at the window"

    def _rest(self, where: str) -> None:
        """Rest between two files for as long as a pause stands: no file is open and
        nothing is asked of the console or the boxes. The boxes' ports are still read,
        so that one unplugged while the series rested is let go of here and named by
        the run that next needs it. A resume ends the rest, and so does a stop, which
        the caller then finds set."""
        if self._hold is None or self._stop.is_set():
            return
        self._reach(where)
        while self._hold is not None and not self._stop.is_set():
            self._lifted.wait(HOLD_POLL_S)
            for name, box in list(self.boxes.items()):
                try:
                    box.drain(0.0)
                except OSError:
                    self._forget(name)
                except Exception:  # noqa: BLE001 -- a line that does not parse is no fault
                    pass

    def _lapse(self, handle: Handle, job: Job) -> None:
        """The job has ended: its series is no longer one to pause, and a pause it never
        came to rest at is let go, and said."""
        with self._guard:
            hold, self._hold = self._hold, None
            self._job = None
            self._at = (0, None)
        if hold is None:
            return
        line = (f"the pause {hold.by} asked for at {hold.asked}, after this {hold.grain}, "
                "was never reached: the job ended first"
                if not hold.reached else f"the job ended while {hold.text}")
        self._say(line)
        if isinstance(job, (Acquire, SeriesJob)):
            self._records.note(job.directory or os.getcwd(), job.series, line)

    def day_line(self, kind: str, **fields: object) -> None:
        """A line in the day record, if this owner keeps one; any thread, never raises."""
        if self.day is not None:
            self.day.write(kind, **fields)

    def _console_line(self, event: str, detail: str = "") -> None:
        """A console line in the day record, naming the job and the client it came from."""
        running = self._running
        self.day_line("console", event=event,
                      job=running.id if running is not None else None,
                      by=(running.origin if running is not None and running.origin
                          else self.program),
                      detail=detail)

    def _record_end(self, handle: Handle, job: Job, *, failed: str = "",
                    result: object = None) -> None:
        """The note a failed job or a stopped series leaves in its request's run record,
        then every entry still being written: before the end is reported, so that a
        client waiting for the end reads a whole record."""
        if not isinstance(job, (Acquire, SeriesJob)) or not job.series:
            return
        directory = job.directory or os.getcwd()
        if failed:
            point = self._point
            where = (f" at planned point {point.index}, point {point.number} of "
                     f"{point.points} as acquired" if point is not None else "")
            self._records.note(directory, job.series,
                               f"job {handle.id} failed{where}: {failed}")
        elif isinstance(result, SeriesResult) and not result.complete:
            # Points and files counted apart: a retake's failed files and the stopped
            # point's file are files but not points, and one line of "19 files" beside
            # "19 points" read as a series that had run to its end (lab #26).
            replaced = (f", {len(result.replaced)} of them failed and replaced by a "
                        f"retake ({', '.join(result.replaced)})" if result.replaced else "")
            self._records.note(directory, job.series, (
                f"series job {handle.id} {result.shortfall} {len(result.runs)} files "
                f"written{replaced}."))
        self._records.drain(RECORD_DRAIN_S)

    def _do(self, job: Job) -> object:
        if self._lock is not None:
            self._hold_lock()
        if isinstance(job, Discover):
            return self._discover(job)
        if isinstance(job, StartConsole):
            return self._start_console(job)
        if isinstance(job, RestartConsole):
            return self._restart_console(job)
        if isinstance(job, Send):
            return self._send(job)
        if isinstance(job, Acquire):
            return self._acquire(job)
        if isinstance(job, SeriesJob):
            return self._series(job)
        if isinstance(job, ReadState):
            return self._read_state(job)
        if isinstance(job, WarmUp):
            return self._warm_up(job)
        if isinstance(job, StandDown):
            return self._stand_down(job)
        if isinstance(job, SnapshotBoxes):
            return self._snapshot_boxes(job)
        raise TypeError(f"no owner handler for {type(job).__name__}")

    def _report(self, event: Event, handle: Handle | None = None) -> None:
        """Keep an event against its job and hand it to `on_event`. Any thread.

        `handle` is the job's, where it is not the one running: a job refused before it
        starts is reported against itself.
        """
        handle = handle or self._current or Handle(id=0, kind="", label="",
                                                    owner=self.session)
        kept = event
        if isinstance(event, BatchSeen):
            kept = replace(event, batch=event.batch.kept())
        with self._guard:
            entries = self._history.setdefault(handle.id, [])
            entry = Progress(seq=next(self._seqs), event=kept)
            summary = self._summaries.get(handle.id) or JobSummary(handle=handle)
            self._summaries[handle.id] = summary.after(entry.seq, kept)
            if (isinstance(kept, BatchSeen) and entries
                    and isinstance(entries[-1].event, BatchSeen)):
                entries[-1] = entry
            else:
                entries.append(entry)
                if len(entries) > self._limit:
                    del entries[:len(entries) - self._limit]
            for listener in self._listeners:
                listener(handle, entry)
        if self._on_event is not None:
            self._on_event(handle, event)

    def _say(self, line: str) -> None:
        self._report(Said(line=line))

    def _forget_old_jobs(self) -> None:
        """Drop the oldest finished jobs' progress past `history`. Under `_guard`."""
        keep = {entry.id for entry in self._pending}
        for handle in (self._running, self._current):
            if handle is not None:
                keep.add(handle.id)
        for job_id in list(self._history):
            if len(self._history) <= self._history_jobs:
                break
            if job_id not in keep:
                del self._history[job_id]
                self._summaries.pop(job_id, None)

    def _hold_lock(self) -> None:
        """Take the instrument lock if it is not already held, or raise its sentence."""
        assert self._lock is not None
        if self._lock.held:
            return
        try:
            self._lock.acquire()
        except LockError as exc:
            self._refused, self._refused_by = str(exc), exc.holder
            raise
        self._refused, self._refused_by = "", None

    # -- the boxes -----------------------------------------------------------

    def _discover(self, job: Discover) -> Discovery:
        if self.fake:
            self._close_boxes()
            if job.method is None:
                self._say("--fake: open a method and the rack is built from it")
                found = Discovery()
            else:
                self.boxes = fake_rack(job.method)
                # The method's own port for each box, not the word "simulated": the
                # window writes what a scan reports back into the method it saves, and
                # a `--fake` session must not quietly replace a document's port map.
                ports = {entry.name: entry.port for entry in job.method.boxes}
                found = Discovery(found=tuple(
                    Found(name=name, port=ports.get(name, ""), version=box.version(),
                          box=box)
                    for name, box in self.boxes.items()
                ))
                self._say(
                    f"--fake: {len(self.boxes)} simulated box(es) from the method. "
                    "Nothing here is evidence about a MIPS box.")
            self._report(Discovered(discovery=found))
            return found

        # The open boxes go back in rather than being closed first: a close resets the
        # Due, it leaves the bus to re-enumerate, and a rescan that closed and reopened
        # found one box fewer on every press (lab record, task 62).
        found = self._scan(ports=job.ports, held=self._held)
        self._held = {entry.port: entry.box for entry in found.found
                      if entry.box is not None}
        self.boxes = found.boxes
        self.listings = {name: listing for name, listing in self.listings.items()
                         if name in self.boxes}
        self._say(f"{found.text}")
        for entry in found.silent:
            self._say(entry.text)
        for port, why in found.unusable:
            self._say(f"{port}: {why}")
        self._report(Discovered(discovery=found))
        return found

    def _read_state(self, job: ReadState) -> dict[str, BoxState]:
        names = job.names or tuple(self.boxes)
        states: dict[str, BoxState] = {}
        for name in names:
            box = self.boxes.get(name)
            if box is None:
                continue
            listing = self.listings.get(name)
            if listing is None:
                with box.summarised():
                    listing = self.listings[name] = box.command_listing()
            with box.summarised():
                state = read_state(box, listing=listing)
            states[name] = state
            self._report(BoxStateRead(box=name, state=state))
        return states

    # -- the console ---------------------------------------------------------

    def _start_console(self, job: StartConsole) -> ConsoleStatus:
        if self.console is not None and self.console.alive:
            return self.console_status
        self._set_status(ConsoleStatus(state="starting"))
        if self.fake:
            # Claiming the instrument's pusher, so a dry run of a method rendered for
            # the instrument's 129 us tick is checked against 129 us and passes, rather
            # than refused against the stand-in's own 4 us (lab record, task 88).
            self.console = FakeConsoleProcess(
                open_batches=1, measured_period_samples=INSTRUMENT_PERIOD_SAMPLES)
        elif job.command:
            self.console = ConsoleProcess(job.command)
        else:
            self._console_line("failed", "no acquisition console is configured")
            raise AcqError(
                "no acquisition console is configured. Point Settings at the console "
                "executable the installer put beside clockwork, or set "
                "$CLOCKWORK_CONSOLE.")
        try:
            self.console.start()
            self._hold_fake_frames()
            seconds = self.console.wait_ready()
        except Exception as exc:
            self._console_line("failed", sentence(exc))
            raise
        self._set_status(self._status_now(seconds))
        self._console_line("started", f"ready in {seconds:.1f} s")
        return self.console_status

    def _hold_fake_frames(self) -> None:
        """Give a stand-in console the instrument's frame hold, on every start: a restart
        makes a new stand-in, which would otherwise run its frames with no hold at all."""
        if self.fake and getattr(self.console, "fake", None) is not None:
            self.console.fake.frame_hold_s = FAKE_FRAME_HOLD_S  # type: ignore[attr-defined]

    def _restart_console(self, job: RestartConsole) -> ConsoleStatus:
        if self.console is None:
            raise AcqError("there is no console to restart; start one first")
        if job.values and isinstance(self.console, ConsoleProcess):
            config = self.console.config.set(**dict(job.values))
            problems = config.problems()
            if problems:
                raise AcqError("; ".join(problems))
            config.save()
            self._say(f"wrote {', '.join(sorted(job.values))} to config.txt")
        self._set_status(ConsoleStatus(state="starting"))
        try:
            seconds = self.console.restart()
        except Exception as exc:
            self._console_line("failed", f"{job.label}: {sentence(exc)}")
            raise
        self._hold_fake_frames()
        self._set_status(self._status_now(seconds))
        written = (f"; wrote {', '.join(sorted(job.values))} to config.txt"
                   if job.values else "")
        self._console_line("restarted", f"{job.label}, ready in {seconds:.1f} s{written}")
        return self.console_status

    def _recover(self, console: Console, stream: DataStream, job: Acquire) -> TofWidth:
        """`run_acquisition`'s `recover`: a new console process under a running run.

        For a console whose samples stream has fallen out of step with its markers, which
        only a new process has been seen to clear (lab record, task 99). The run's two
        clients are pointed at the new process, which for the stand-in may be listening
        on new ports, and the card is prepared and the chain opened exactly as the run's
        prologue did. The chain is the run's from here on, and `_acquire`'s `finally`
        stops it like the one it replaced.
        """
        self._restart_under_run(console, stream, job)
        return start_chain_retrying(
            console, stream, progress=self._report,
            restart=self._clock_lock_restart(console, stream, job))

    def _clock_lock_restart(self, console: Console, stream: DataStream,
                            job: Acquire) -> Callable[[], object] | None:
        """`start_chain_retrying`'s `restart`, or None where this owner holds no console
        process to restart: a clock-lock refusal has only ever cleared on a new process
        (lab record, task 162)."""
        if self.console is None:
            return None
        return lambda: self._restart_under_run(console, stream, job)

    def _restart_under_run(self, console: Console, stream: DataStream, job: Acquire) -> None:
        """A new console process, the run's two clients pointed at it and the card
        prepared as the run's prologue prepared it; no chain is opened."""
        if self.console is None:
            raise AcqError("there is no console to restart")
        self._set_status(ConsoleStatus(state="starting"))
        try:
            seconds = self.console.restart()
        except Exception as exc:
            self._console_line("failed", f"restarting under a run: {sentence(exc)}")
            raise
        self._hold_fake_frames()
        self._set_status(self._status_now(seconds))
        self._console_line("restarted", f"under a run, ready in {seconds:.1f} s")
        console.reconnect(self.console.command_endpoint)
        stream.reconnect(self.console.data_endpoint)
        info = console.info()
        prepared = prepare_console(console, job.instrument, info=info,
                                   config=self._console_config())
        for message in prepared.warnings:
            self._report(Warned(message))
        self._say(f"console restarted in {seconds:.1f} s; offset {prepared.offset_v} V, "
                  f"{'inverted' if prepared.inverted else 'not inverted'}")

    def _status_now(self, seconds: float | None = None) -> ConsoleStatus:
        console = self.console
        if console is None:
            return ConsoleStatus()
        drift: tuple[str, ...] = ()
        try:
            differences = console.needs_restart()
        except (AcqError, OSError):
            differences = {}
        if differences:
            drift = tuple(
                f"{key}: config.txt says {wanted}, the running console has {holding}"
                for key, (holding, wanted) in sorted(differences.items())
            )
        info = getattr(console, "info", None)
        return ConsoleStatus(
            state="ready" if console.alive else "stopped",
            info=getattr(info, "text", "") or "",
            seconds=seconds if seconds is not None else console.started_seconds,
            endpoint=console.command_endpoint,
            drift=drift,
        )

    def _set_status(self, status: ConsoleStatus) -> None:
        self.console_status = status
        self._report(ConsoleChanged(status=status))

    def _endpoints(self) -> tuple[str, str]:
        """Where the console is listening, off the supervisor rather than assumed.

        A caller that hardcodes 5555 works against the instrument and against nothing
        else: the stand-in binds whatever ports it is given, and that is the whole of
        what makes `--fake` exercise this path (lab record, task 49).
        """
        if self.console is None or not self.console.alive:
            raise AcqError("the acquisition console is not running")
        return self.console.command_endpoint, self.console.data_endpoint

    # -- sending -------------------------------------------------------------

    def _send_as_needed(self, job: Send) -> SendResult:
        """The window's Send, and the send an `Acquire` with `send` makes first (E7; lab
        record, task 169): only what the boxes do not hold, by what they were last sent.

        Nothing where they hold this method (`matches_wire` on `Armed`), unless `setup`
        asks for it again; otherwise every phase, with `setup` only where the stack
        differs from the last send's, or no send is remembered, or the last one did not
        carry it. A send that leaves `setup` out because the boxes hold it records it as
        held, so what the boxes hold is still whole for the next comparison."""
        method = _needs_method(job.method)
        armed = self._armed
        holds = armed is not None and matches_wire(armed.fingerprint, method)
        if holds and not job.setup and self._snapshot is not None:
            assert armed is not None
            self._say(f"the boxes hold {method.metadata.name or 'this method'} already, "
                      "as they were last sent it: nothing sent")
            return SendResult(snapshot=self._snapshot, setup=False,
                              armed=armed.fingerprint, sent=False)
        setup = (job.setup or armed is None
                 or _setup_differs_wire(armed.fingerprint, method))
        return self._send(replace(job, setup=setup, as_needed=False), setup_held=not setup)

    def _send(self, job: Send, *, setup_held: bool = False) -> SendResult:
        if job.as_needed:
            return self._send_as_needed(job)
        method = _needs_method(job.method)
        self._require_boxes(method)
        problems = refusals(method)
        if problems:
            raise AcqError("; ".join(problems))
        header = self._header(method, job.method_path, job.instrument,
                              job.instrument_path, job.conditions)
        started = time.perf_counter()
        directory, stem = job.directory or os.getcwd(), job.stem
        self._refuse_taken(directory, stem)
        append = self._send_log == (directory, stem)
        # Before the work and not after it, so a send that raises part way through does
        # not leave the next one free to overwrite what it managed to write.
        self._send_log = (directory, stem)
        # Cleared before the first string goes out: a send that raises part way leaves
        # boxes holding some of one method and some of another, which is not armed.
        self._armed = None
        with self._logs(directory, stem, header, append=append) as paths:
            snapshot = send_phases(
                method, self.boxes, setup=job.setup, progress=self._report,
                conditions=job.conditions, listings=self.listings,
            )
        self._snapshot = snapshot
        if job.setup:
            self._setup_log = (directory, stem)
        fingerprint = wire_fingerprint(method, setup=job.setup or setup_held)
        self._armed = Armed(method=method.metadata.name, fingerprint=fingerprint,
                            directory=directory, stem=stem, setup=job.setup)
        return SendResult(snapshot=snapshot, setup=job.setup, send_log=paths[1],
                          transcript_path=paths[0],
                          seconds=time.perf_counter() - started,
                          armed=fingerprint)

    # -- the two ends of the day -----------------------------------------------

    def _warm_up(self, job: WarmUp) -> StandingResult:
        method = _needs_method(job.method)
        self._require_boxes(method)
        header = self._header(method, job.method_path, job.instrument,
                              job.instrument_path, "")
        return self._standing(job.directory, job.stem, header, lambda: warm_up(
            method, self.boxes, steps=job.steps, dwell_s=job.dwell_s,
            progress=self._report, listings=self.listings, stop=self._stop_check) + ((),))

    def _stand_down(self, job: StandDown) -> StandingResult:
        if not self.boxes:
            raise MipsError("no boxes are open to stand down: find them first, or check "
                            "they are powered on")
        header = transcript.run_header(
            console=getattr(self.console, "info", None),
            boxes=[(name, row[0], row[1], row[2]) for name, row in self._identities()])
        return self._standing(job.directory, job.stem, header, lambda: stand_down(
            self.boxes, steps=job.steps, dwell_s=job.dwell_s, progress=self._report,
            listings=self.listings, stop=self._stop_check))

    def _snapshot_boxes(self, job: SnapshotBoxes) -> SnapshotResult:
        """Every held box's state and health, getters only, into the stem's own files.

        Unlike `_standing`, nothing the owner remembers is touched: the arming, the
        snapshot an acquisition compares against and the send log a later `Send`
        appends to all stay as they were, because nothing on any box changed. A box
        whose reading raises is recorded under `failed` and the rest are still read,
        since a snapshot taken while something is wrong must not stop at the first
        box that is part of it.
        """
        if not self.boxes:
            raise MipsError("no boxes are open to read: find them first, or check "
                            "they are powered on")
        names = job.names or tuple(self.boxes)
        directory = job.directory or os.getcwd()
        header = transcript.run_header(
            console=getattr(self.console, "info", None),
            boxes=[(name, row[0], row[1], row[2]) for name, row in self._identities()])
        started = time.perf_counter()
        taken_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        states: dict[str, BoxState] = {}
        health: dict[str, BoxHealth] = {}
        failed: dict[str, str] = {}
        with self._logs(directory, job.stem, header, append=False) as paths:
            if job.note:
                transcript.note_block("snapshot note\n" + job.note, source="run")
            for name in names:
                box = self.boxes.get(name)
                if box is None:
                    failed[name] = "not held: find the boxes first"
                    continue
                try:
                    listing = self.listings.get(name)
                    if listing is None:
                        with box.summarised():
                            listing = self.listings[name] = box.command_listing()
                    states[name] = read_state(box, listing=listing)
                    health[name] = read_health(box, listing=listing)
                # `OSError` for an unplugged port, as `_read_state` would meet it.
                except (MipsError, ValueError, OSError) as exc:
                    failed[name] = str(exc)
                    self._say(f"{name}: snapshot reading failed ({exc})")
                    continue
                transcript.note_block("state at snapshot\n" + states[name].render(),
                                      source=name)
                transcript.note_block(health[name].render(), source=name)
                self._report(BoxStateRead(box=name, state=states[name]))
        json_path = os.path.join(directory, f"{job.stem}.snapshot.json")
        result = SnapshotResult(states=states, health=health, failed=failed,
                                note=job.note, taken_at=taken_at, send_log=paths[1],
                                transcript_path=paths[0], json_path=json_path,
                                seconds=time.perf_counter() - started)
        with open(json_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(to_wire(result), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return result

    def _standing(self, directory: str, stem: str, header: str,
                  work: Callable[[], tuple[Snapshot, int, tuple[str, ...]]]
                  ) -> StandingResult:
        """A warm-up or a stand-down inside its own send log and transcript.

        What the boxes hold afterwards is neither the last method's arming nor the
        snapshot it read, so both are forgotten before the first string goes: an
        `acquire` after a warm-up is refused until something is armed again."""
        directory = directory or os.getcwd()
        self._stop.clear()
        started = time.perf_counter()
        self._armed = None
        self._snapshot = None
        self._send_log = (directory, stem)
        with self._logs(directory, stem, header, append=False) as paths:
            snapshot, steps, lowered = work()
        return StandingResult(snapshot=snapshot, steps=steps, lowered=lowered,
                              send_log=paths[1], transcript_path=paths[0],
                              seconds=time.perf_counter() - started)

    # -- acquiring -----------------------------------------------------------

    def _console_config(self) -> ConsoleConfig | None:
        """The `config.txt` beside the running console, for `prepare_console` to compare
        the console's reported settings against, or None where there is nothing to read.

        `ConsoleProcess.config` is a **method** -- it reads the file off disk each time,
        because what the file says now is what the *next* start will read. Passing it
        uncalled handed `prepare_console` a function object, and the first acquisition on
        a real console died with `AttributeError: 'function' object has no attribute
        'full_scale_v'` (found at the instrument, 2026-09-17; lab record, task 50).
        Nothing caught it because `FakeConsoleProcess` subclasses `ConsoleSupervisor` and
        not `ConsoleProcess`, so under `--fake` the branch short-circuits to None and is
        never evaluated.

        `OSError` is swallowed the way `needs_restart` swallows it: a config.txt that has
        been moved or cannot be read is a comparison clockwork cannot make, not a reason
        to refuse an acquisition the console is otherwise ready for.
        """
        if not isinstance(self.console, ConsoleProcess):
            return None
        try:
            return self.console.config()
        except OSError:
            return None

    def _acquire(self, job: Acquire, *, clear_stop: bool = True) -> list[Run]:
        """A run and its replicates. `clear_stop` false for a point of a series, whose
        stop was cleared when the series began: a stop asked for between two points
        must end the series, not be forgotten by the next point."""
        method = _needs_method(job.method)
        self._require_boxes(method)
        problems = refusals(method)
        if problems:
            raise AcqError("; ".join(problems))
        rendered = _rendered(job)
        command_endpoint, data_endpoint = self._endpoints()
        directory = job.directory or os.getcwd()
        if job.stem:
            self._refuse_taken(directory, job.stem, initials=job.initials)
        os.makedirs(directory, exist_ok=True)
        if clear_stop:
            self._stop.clear()
        first_replicate = job.replicate_only
        if job.send:
            # The window's Acquire (E7): what the boxes do not hold goes first, under the
            # run's own stem, and where nothing went the first run is a replicate on the
            # wire, as a series point the boxes held is.
            if not job.stem:
                job = replace(job, stem=next_stem(directory, job.initials))
            sent = self._send(Send(
                label=f"sending {method.metadata.name or 'the method'}", method=method,
                setup=job.setup, conditions=job.conditions, directory=directory,
                stem=job.stem, method_path=job.method_path, instrument=job.instrument,
                instrument_path=job.instrument_path, as_needed=True))
            first_replicate = first_replicate or not sent.sent
            if self._stop.is_set():
                self._say(f"stopped before acquiring: {self._stop_reason}")
                return []

        runs: list[Run] = []
        # Subscribed before anything is configured, because the console binds the data
        # socket in its first `acquire` and PUB drops what has no subscriber yet.
        with DataStream(data_endpoint) as stream, Console(command_endpoint) as console:
            width: object | None = None

            def prologue() -> object:
                """Configure the card and open the chain, once for the whole series.

                Handed to the first run rather than called here, because the transcript
                is opened by `_one_run` and a command sent in front of it is a command
                nothing writes down: see that method's docstring.

                The chain is opened once and closed in the `finally` below, which is the
                loop's own ownership rule -- whoever opens it closes it. It also keeps a
                series to one `acquire`: a second one on a console already holding an
                acquisition is the call that kills the process (lab record, task 20).
                """
                nonlocal width
                info = console.info()
                prepared = prepare_console(
                    console, job.instrument, info=info, config=self._console_config(),
                )
                for message in prepared.warnings:
                    self._report(Warned(message))
                self._say(
                    f"offset {prepared.offset_v} V, "
                    f"{'inverted' if prepared.inverted else 'not inverted'}")
                # A clock-lock refusal of `acquire` is tried once more on a new console
                # process, the card's settings sent again first (lab record, tasks 104
                # and 162).
                width = start_chain_retrying(
                    console, stream, progress=self._report,
                    restart=self._clock_lock_restart(console, stream, job))
                return width

            try:
                taken: list[str] = []
                for index in range(max(1, job.replicates)):
                    stem = job.stem if index == 0 and job.stem else next_stem(
                        directory, job.initials, taken=taken)
                    taken.append(stem)
                    replicate = first_replicate or index > 0
                    series = None
                    if job.series:
                        if job.series_position > 0:
                            # A point of a planned series: its replicates share its
                            # planned index and take consecutive positions.
                            series = Series(id=job.series, index=max(1, job.series_index),
                                            position=job.series_position + index,
                                            seed=job.series_seed)
                        else:
                            place = max(1, job.series_index) + index
                            series = Series(id=job.series, index=place, position=place,
                                            seed=job.series_seed)
                    provenance = (Provenance(rendered=rendered, series=series)
                                  if rendered is not None or series is not None else None)
                    run = self._one_run(
                        method, job, console, stream, width, directory, stem,
                        replicate=replicate,
                        # The first file continues a send log under its own stem even
                        # where it is a replicate on the wire: a Send and then an
                        # Acquire that found nothing to send share one stem, and its
                        # log must not be opened afresh over the send (task 56's rule).
                        first_log=self._send_log if index == 0 else None,
                        prologue=prologue if index == 0 else None,
                        provenance=provenance,
                    )
                    runs.append(run)
                    self._last_run = run
                    self._report(RunDone(run=run))
                    self._records.enter(directory, job.series, run)
                    hold = self._hold
                    if (hold is not None and hold.grain == REPETITION
                            and index + 1 < max(1, job.replicates)
                            and not run.stopped_early):
                        # The repetition the pause was asked in was its file's last, so
                        # the series rests here, before the next file.
                        self._rest(f"after file {index + 1} of {job.replicates} ({stem}), "
                                   "before the next file")
                    if run.stopped_early or self._stop.is_set():
                        if index + 1 < job.replicates:
                            self._say(
                                f"stopped: {job.replicates - index - 1} replicate(s) "
                                "not acquired")
                        break
            finally:
                if console.acquiring or console.running:
                    try:
                        console.stop_acquire()
                    except AcqError as exc:
                        self._say(
                            f"`stop acquire` failed: {exc}. THE CONSOLE IS STILL "
                            "HOLDING AN ACQUISITION; restart it before acquiring again.")
        return runs

    def _series(self, job: SeriesJob) -> SeriesResult:
        """Every point of a planned series, in the order given: arm it, then acquire it.

        A point is sent unless the boxes hold it already from the point before, and its
        `setup` phase goes with it only where that differs from the one last sent: the
        stack is the same across a template's points unless a knob moves it. After each
        send, what the boxes read back is judged against what the point declares, under
        the series' cold-start rule, as `acquire` judges an `arm`'s read-back; a refusal
        ends the series before that point acquires anything.

        A stop, from any client, ends the series after the current repetition, or
        before the next point when it arrives between two. Everything a point raises
        ends the series too, with the files already written left as they are.

        A person's pause rests it between two points, here, or inside a point's file
        or between two of its files (`_hold_check`, `_acquire`); a stop while it rests
        ends it as any stop does.

        **A point whose file the beam guard ended holds the series** (B8, lab record,
        task 168): the file ends failed with the guard's sentence, as before, and the
        series rests, paused by the guard, until a person resumes or stops it. A resume
        acquires the point again from its start, as new files under the same planned
        index at the next positions, and the record's `retakes` names the files it
        replaced; then the series goes on. The guard is the daemon's rule, not a
        person's choice of point, so nothing else repeats a point (R6).

        A queue's points are saved documents (`PlannedPoint.method_path`): each carries
        its own document path, conditions note and, for a rendered document, its own
        template, which stand in for the series'.
        """
        if not job.points:
            raise ValueError("a series has at least one point")
        directory = job.directory or os.getcwd()
        self._stop.clear()
        runs: list[Run] = []
        numbers: list[int] = []
        replaced: list[str] = []
        position = max(1, job.first_position)
        sent: Method | None = None
        armed: tuple = ()
        done = 0
        stopped = ""
        for number, point in enumerate(job.points, start=1):
            method = _needs_method(point.method)
            method_path = point.method_path or job.method_path
            conditions = point.conditions or job.conditions
            retake = 0
            whole = False
            while not stopped:
                if self._stop.is_set():
                    stopped = self._stop_reason
                    break
                sends = sent is None or self._armed is None or not matches_wire(
                    self._armed.fingerprint, method)
                with self._guard:
                    self._at = (number, method)
                self._point = PointStarted(
                    index=point.index, position=position, number=number,
                    points=len(job.points), knobs=dict(point.knobs),
                    replicates=point.replicates, reference=point.reference, sends=sends,
                    retake=retake, name=point.name)
                self._report(self._point)
                stem = ""
                if sends:
                    stem = next_stem(directory, job.initials)
                    result = self._send(Send(
                        label=f"arming point {point.index} of {job.series}", method=method,
                        setup=job.setup if sent is None else _setup_differs(sent, method),
                        conditions=conditions, directory=directory, stem=stem,
                        method_path=method_path, instrument=job.instrument,
                        instrument_path=job.instrument_path))
                    sent, armed = method, result.armed
                    self._judge_send(job, point, result.snapshot)
                    if self._stop.is_set():
                        stopped = self._stop_reason
                        break
                made = self._acquire(Acquire(
                    label=f"acquiring point {point.index} of {job.series}", method=method,
                    instrument=job.instrument, instrument_path=job.instrument_path,
                    method_path=method_path, directory=directory, stem=stem,
                    initials=job.initials, replicates=point.replicates,
                    # Nothing sent: on the wire it is a replicate of the point before,
                    # and its send log says where the strings went.
                    replicate_only=not sends,
                    conditions=conditions, request=job.request, series=job.series,
                    series_index=point.index, series_position=position,
                    series_seed=job.seed,
                    template=point.template or job.template,
                    knobs=dict(point.knobs), labels=dict(point.labels or job.labels)),
                    clear_stop=False)
                runs += made
                numbers += [number] * len(made)
                position += len(made)
                lost = next((run.beam_lost for run in made if run.beam_lost), "")
                if lost and not self._stop.is_set():
                    stems = [stem_of(run) for run in made]
                    if not self._guard_hold(job, point, number, lost, stems):
                        stopped = (self._stop_check()
                                   or "stopped while the beam guard held")
                        break
                    replaced += stems
                    retake += 1
                    self._records.retake(directory, job.series, {
                        "index": point.index, "number": number, "retake": retake,
                        "job": self._running.id if self._running is not None else None,
                        "replaces": stems, "beam_lost": lost, "first_place": position})
                    self._say(f"acquiring point {number} of {len(job.points)} (planned "
                              f"{point.index}) again from its start: {', '.join(stems)} "
                              "ended by the beam guard")
                    continue
                whole = (len(made) == point.replicates
                         and not any(run.stopped_early for run in made))
                done += whole
                ended = next((run for run in made if run.stopped_early), None)
                if self._stop.is_set() or ended is not None:
                    # A person's or an agent's reason where one was asked, else the
                    # run's own sentence: a loop that stopped itself said why, and the
                    # series' end has to carry that, not a stand-in (lab #26).
                    stopped = (self._stop_check()
                               or (ended.stopped_early if ended is not None else "")
                               or "a run stopped early")
                break
            if stopped:
                break
            if number < len(job.points):
                # A pause of either grain that has not yet come to rest rests here; a
                # stop that ends the rest is found at the top of the loop.
                self._rest(f"after point {number} of {len(job.points)} "
                           f"(planned {point.index})")
        result = SeriesResult(runs=tuple(runs), points=len(job.points), points_done=done,
                              armed=armed, numbers=tuple(numbers),
                              replaced=tuple(replaced))
        if not stopped or done == len(job.points):
            return result
        # No line of its own: `JobFinished.text` says the shortfall, and the record's
        # note the counts (lab #26).
        return _short(result, job.points, number, whole=whole, reason=stopped)

    def _guard_hold(self, job: SeriesJob, point: PlannedPoint, number: int, lost: str,
                    stems: list[str]) -> bool:
        """Hold the series for a person, the beam guard having ended a file of point
        `number` (B8): an intervention by the guard in the record, then a rest like a
        person's pause after this point. True once a person resumes, False for a stop."""
        handle = self._running
        where = (f"after point {number} of {len(job.points)} (planned {point.index}), "
                 f"whose file {stems[-1] if stems else ''} the guard ended")
        with self._guard:
            self._hold = Hold(job=handle.id if handle is not None else 0, grain=POINT,
                              by=BEAM_GUARD, client=self.program, asked=_now(),
                              beam_lost=lost)
            self._lifted.clear()
        if handle is not None:
            self._intervene(job, handle, kind="pause", grain=POINT, by=BEAM_GUARD,
                            client=self.program, reason=lost)
        self._rest(where)
        return not self._stop.is_set()

    def _judge_send(self, job: SeriesJob, point: PlannedPoint,
                    snapshot: Snapshot) -> None:
        """What a series' send read back, against what its point declares: cautions
        said, a refusal raised, under the job's cold-start rule; nothing without one."""
        if not job.cold_start:
            return
        method = _needs_method(point.method)
        refused, cautioned = judge(
            cold_start(method, snapshot.after or snapshot.before, declared=True),
            job.cold_start)
        for line in cautioned:
            self._report(Warned(f"cold start, point {point.index}: {line}"))
        if refused:
            raise AcqError(
                f"point {point.index}'s send left the boxes holding what the standing "
                "limits refuse a cold start on: " + "; ".join(refused)
                + ". The series stopped before acquiring it")

    def _one_run(
        self,
        method: Method,
        job: Acquire,
        console: Console,
        stream: DataStream,
        width: object,
        directory: str,
        stem: str,
        *,
        replicate: bool,
        first_log: tuple[str, str] | None,
        prologue: Callable[[], object] | None = None,
        provenance: Provenance | None = None,
    ) -> Run:
        """One `run_acquisition` with its two log files open around it.

        **`prologue` is run in here and not by the caller, and that is the point.** The
        transcript and the send log are attached to the `clockwork` logger by `_logs`,
        and `clockwork.acq.console` builds a record only under `isEnabledFor(DEBUG)`, so
        outside this block every command to the console is sent and none of it is
        written down. Configuring the card and opening the chain used to happen a few
        lines above this call: the instrument sitting's transcripts therefore carry
        `info`, a hundred `acquire frame`s and a hundred `stop`s and nothing else, and
        read as a run that never sent the instrument document's offset and inversion --
        which it had sent (lab record, task 50). The settings a file was acquired under
        belong in that file's own transcript, so the series' first run opens the log and
        then configures the card inside it.
        """
        header = self._header(method, job.method_path, job.instrument,
                              job.instrument_path, job.conditions, request=job.request)
        # A replicate re-sends neither `setup` nor `load`, so its send log carries its
        # reset and start lists and a line saying where the others went -- which is how
        # the bench script's replicate logs read and what makes them findable. Its stem
        # is its own, so `first_log` never matches and it opens a file of its own.
        #
        # `first_log` is the last send log of any kind and not only the last *setup* one
        # (`_send_log`): a queue row with `setup` unchecked sends load and arm under this
        # stem and then acquires under it, and matching on the setup log alone made the
        # acquisition truncate the load-and-arm log it had just written (task 56).
        append = first_log is not None and first_log == (directory, stem)
        self._send_log = (directory, stem)
        self._stem = stem
        if replicate and self._setup_log is not None:
            header += ("\nthis is a replicate: its method's setup, load and arm strings "
                       f"went in {transcript.send_log_name(self._setup_log[1])} and were "
                       "not sent again")
        # Only the boxes the method names: one that is held and not used has no part in
        # the run, and handing it in made unplugging it end the run (lab #1). Checked
        # again after the header, which forgets a box it finds unplugged.
        self._require_boxes(method)
        boxes = {entry.name: self.boxes[entry.name] for entry in method.boxes}
        self._refuse_taken(directory, stem, initials=job.initials)
        # The arming still holds, and the name it was made under is this run's now:
        # nothing else may be written under it (lab #3).
        if self._armed is not None and self._armed.stem == stem:
            self._armed = replace(self._armed, used=True)
        # Counted across the run off the console's own stdout, which the loop never
        # sees: a row's outcome says how many errors the console logged (lab #2).
        errors_before = self._console_errors()
        try:
            run = self._logged_run(method, job, console, stream, width, directory, stem,
                                   header, boxes, append=append, replicate=replicate,
                                   prologue=prologue, provenance=provenance)
            return replace(run, console_errors=self._console_errors() - errors_before)
        except Exception as exc:
            # After `_logs` has closed the transcript, so the copy ends on `Stopped:`,
            # and after `run_acquisition` has folded and closed the files.
            self._keep_failed(exc, job, directory, stem)
            raise

    def _logged_run(self, method: Method, job: Acquire, console: Console,
                    stream: DataStream, width: object, directory: str, stem: str,
                    header: str, boxes: dict[str, Box], *, append: bool,
                    replicate: bool, prologue: Callable[[], object] | None,
                    provenance: Provenance | None) -> Run:
        with self._logs(directory, stem, header, append=append):
            try:
                if prologue is not None:
                    width = prologue()
                return run_acquisition(
                    method,
                    boxes=boxes,
                    console=console,
                    stream=stream,
                    width=width,  # type: ignore[arg-type]
                    directory=directory,
                    stem=stem,
                    post_trigger_samples=self._post_trigger_samples(console),
                    replicate=replicate,
                    progress=self._report,
                    instrument=job.instrument,
                    snapshot=self._snapshot,
                    provenance=provenance,
                    stop=self._stop_check,
                    hold=self._hold_check,
                    recover=lambda: self._recover(console, stream, job),
                    beam_guard=self._beam_guard(job),
                )
            except Exception as exc:
                # Written while the transcript is still open: the run log is not the
                # only record of why a run stopped, and a transcript that just ends
                # reads as a run that was cut off with no reason given (lab #1).
                _LOOP_LOG.debug("Stopped: %s", sentence(exc))
                raise

    def _keep_failed(self, exc: BaseException, job: Acquire, directory: str,
                     stem: str) -> None:
        """Copy a failed run's files, its method and the error log (`clockwork.keep`).

        Never raises: a copy that fails is said and logged, and the run's own failure
        is the one the job reports. A run the operator stopped does not come here; it
        returns, short, rather than raising.
        """
        if not self.kept_root:
            return
        paths = [*keep.run_files(directory, stem), job.method_path, self.errors_log]
        try:
            self._kept = keep.keep(self.kept_root, paths, reason=sentence(exc),
                                   stems=[stem])
        except Exception as problem:  # noqa: BLE001 -- the run's failure comes first
            _LOOP_LOG.warning("the failed run's files could not be kept in %s: %s",
                              self.kept_root, problem)
            self._say(f"the failed run's files could not be kept in {self.kept_root}: "
                      f"{problem}")

    def _refuse_taken(self, directory: str, stem: str, *, initials: str = "") -> None:
        """Refuse a stem any file already uses, before a string goes to a box.

        Lab #3: the window kept a failed run's stem, and the next Acquire loaded the
        rack, armed the gate and acquired a frame before `Recording.create` found the
        file and refused it, which cost the sample. `Recording.create`'s refusal stays
        as the last guard; this is the first, here in the owner rather than the window
        so that the queue and the MCP tools meet it too.

        The one stem that may be written under twice is the one the last finished send
        armed and no run has used yet (`Armed.stem`, `Armed.used`): Send setup writes the transcript
        and the send log that the acquisition under the same stem continues. A stem
        typed by hand is judged the same way as a stale one, and never bumped silently.
        """
        if not stem:
            return
        armed = self._armed
        if (armed is not None and armed.stem == stem and not armed.used
                and os.path.normcase(os.path.abspath(armed.directory or os.getcwd()))
                == os.path.normcase(os.path.abspath(directory))):
            return
        found = files_of(directory, stem)
        if not found:
            return
        parsed = parse_stem(stem)
        who = initials or (parsed[1] if parsed is not None else "")
        offer = (f"; the next free name is {next_stem(directory, who)}" if who
                 else "; choose another name")
        raise AcqError(f"{stem} is already taken in {directory} ({found[0]}), so nothing "
                       f"was sent{offer}")

    def _console_errors(self) -> int:
        """`[error]` lines the console process has logged so far; 0 without one."""
        return self.console.errors_logged if self.console is not None else 0

    def _stop_check(self) -> str | None:
        """What `run_acquisition` asks between repetitions. Must not block."""
        return self._stop_reason if self._stop.is_set() else None

    def _post_trigger_samples(self, console: Console) -> int:
        """`PostTriggerDelay` in samples, which every scan's leading zero run is built
        from. Off the running console's own startup block where there is one, because
        the file is not the authority on what the process read (lab record, task 47).
        """
        # 2 GS/s where the client has not sent `horizontal` yet, which is the only rate
        # this instrument acquires at and the same assumption the loop makes when it is
        # handed a console another process configured (`_sample_rate`).
        seconds_per_sample = (1.0 / console.sample_rate_hz if console.sample_rate_hz
                              else SECONDS_PER_SAMPLE_2GSPS)
        delay: float | None = None
        startup = getattr(self.console, "startup", {}) or {}
        if "PostTriggerDelay" in startup:
            try:
                delay = float(startup["PostTriggerDelay"])
            except ValueError:
                delay = None
        if delay is None:
            config = self._console_config()
            if config is not None:
                delay = config.post_trigger_delay_s
        if delay is None:
            delay = float(ConsoleConfig().in_force("PostTriggerDelay"))
        return int(round(delay / seconds_per_sample))

    # -- the two log files ---------------------------------------------------

    @contextlib.contextmanager
    def _logs(self, directory: str, stem: str, header: str,
              *, append: bool) -> Iterator[tuple[str, str]]:
        """The wire transcript and the send log, open together around one piece of work.

        Both named for the stem so they sort beside the UIMF file, and both opened
        here rather than by a front end because a file opened on a UI thread is a
        file whose writes are on the UI thread. `append` is the one case where a send
        log is added to rather than replaced: "Send setup" writes the setup, load and
        arm strings before any file exists, and the acquisition under the same stem
        continues that file rather than overwriting the strings the lab troubleshoots
        from (lab record, task 39).

        The wire transcript is always opened in append mode: it is named for the stem
        *and the date*, so a day's work on one stem belongs in one forensic file and
        the header block written on the way in separates one piece of it from the next.
        """
        os.makedirs(directory, exist_ok=True)
        transcript_path = os.path.join(directory, transcript.default_name(stem))
        send_path = os.path.join(directory, transcript.send_log_name(stem))
        with transcript.to_file(transcript_path, mode="a", header=header), \
                transcript.send_log(send_path, mode="a" if append else "w",
                                    header=header):
            # Before anything is sent: a front end's Report names this stem from here
            # on, whether or not the work gets as far as `RunBegun` (lab record,
            # task 104).
            self._report(LogsOpened(directory=directory, stem=stem,
                                    transcript=transcript_path))
            yield (transcript_path, send_path)

    def _header(self, method: Method, method_path: str, instrument: Instrument,
                instrument_path: str, conditions: str, *, request: str = "") -> str:
        return transcript.run_header(
            method=method, method_path=method_path or None,
            instrument=instrument, instrument_path=instrument_path or None,
            console=getattr(self.console, "info", None),
            boxes=[(name, row[0], row[1], row[2]) for name, row in self._identities()],
            conditions=conditions,
            request=request,
        )

    def _identities(self) -> list[tuple[str, tuple[str, str, str]]]:
        """`_identities` over the open boxes, forgetting any whose port has gone.

        The header is the one place a run reads every held box, used or not, so it is
        where one unplugged since the last scan is noticed: before lab #1 such a box
        stayed held and ended the next run as well as the one it was pulled during.
        """
        rows, lost = _identities(self.boxes)
        for name in lost:
            self._forget(name)
        return rows

    # -- housekeeping --------------------------------------------------------

    def _forget(self, name: str) -> None:
        """Close a box whose port has gone and stop holding it.

        Out of `boxes` and `_held` both, so the next run that names it is refused by
        `_require_boxes` with a sentence rather than failing on a dead handle, and the
        next scan opens its port afresh once it is plugged back in.
        """
        box = self.boxes.pop(name, None)
        if box is None:
            return
        self._held = {port: held for port, held in self._held.items() if held is not box}
        self.listings.pop(name, None)
        try:
            box.close()
        except Exception:  # noqa: BLE001 -- a port that has gone may not close cleanly
            pass
        self._say(f"{name} is no longer held; Find boxes once it is plugged back in")


    def _require_boxes(self, method: Method) -> None:
        missing = [entry.name for entry in method.boxes if entry.name not in self.boxes]
        if missing:
            raise MipsError(
                "the method names box(es) no port answered to: " + ", ".join(missing)
                + ". Find boxes again, or check they are powered on.")

    def _close_boxes(self) -> None:
        opened = {id(box): box for box in (*self.boxes.values(), *self._held.values())}
        for box in opened.values():
            try:
                box.close()
            except Exception:  # noqa: BLE001 -- a close that fails has nothing to fail
                pass
        self.boxes = {}
        self._held = {}

    def _close_everything(self) -> None:
        """Every box, then the console, then the lock: the lock goes last, so a second
        owner that takes it the moment it is free finds every port already closed."""
        self._close_boxes()
        if self.console is not None:
            try:
                self.console.stop()
            except Exception:  # noqa: BLE001
                pass
            self.console = None
            self._console_line("stopped", f"the owner closed: {self._stop_reason}"
                               if self._stop_reason else "the owner closed")
        if self._lock is not None:
            self._lock.release()


# -- small helpers -----------------------------------------------------------------


def _identities(
    boxes: Mapping[str, Box],
) -> tuple[list[tuple[str, tuple[str, str, str]]], list[str]]:
    """Each box's port, `GNAME` and `GVER` for the run header, asked once, and the
    names of the boxes whose port raised `OSError` on the way (unplugged).

    Two getters per box on a thread that is about to send hundreds of strings; a box
    that will not answer them contributes an empty row rather than stopping the run,
    because a header with a blank firmware is better than no acquisition.
    """
    rows: list[tuple[str, tuple[str, str, str]]] = []
    lost: list[str] = []
    for name, box in boxes.items():
        port = getattr(box.transport, "port_name", "simulated")
        try:
            with box.summarised():
                rows.append((name, (port, box.box_name(), box.version())))
        except OSError:
            rows.append((name, (port, "", "")))
            lost.append(name)
        except (MipsError, ValueError):
            rows.append((name, (port, "", "")))
    return rows, lost


def _setup_differs_wire(fingerprint: tuple, after: Method) -> bool:
    """`_setup_differs` against what a send left the boxes holding (`Armed.fingerprint`)
    rather than a method: true too where a box's setup was not recorded, a send that
    left it out on a person's word that the box already had it."""
    def stack(entries: object) -> tuple:
        return tuple((entry[0], None if entry[1] is None else tuple(entry[1]),
                      tuple(tuple(pair) if isinstance(pair, list) else pair
                            for pair in entry[4]), tuple(entry[5]))
                     for entry in entries)  # type: ignore[union-attr]
    before = stack(fingerprint)
    if any(entry[1] is None for entry in before):
        return True
    return before != stack(wire_fingerprint(after))


def _setup_differs(before: Method, after: Method) -> bool:
    """Whether `after` would send any box a `setup` phase, a DC bias or an RF head
    other than `before` sent it, which is when a series' send needs its setup."""
    def stack(method: Method) -> tuple:
        return tuple((entry.name, tuple(entry.setup), tuple(entry.dc_bias), tuple(entry.rf))
                     for entry in method.boxes)
    return stack(before) != stack(after)


def _needs_method(method: Method | None) -> Method:
    if method is None:
        raise ValueError("there is no method to send; open or write one first")
    return method


def _short(result: SeriesResult, points: tuple[PlannedPoint, ...], number: int, *,
           whole: bool, reason: str) -> SeriesResult:
    """`result` for a series a stop ended at the point in place `number`: where it ended
    and why, and every point it left without a file (lab #26; lab record, task 171).

    The point in place `number` is the one the stop ended part way when it has a file
    and is not whole; with every file whole the stop came after it, and with none
    before it, so it is not acquired either. A retake's failed files count as files:
    a stop while the guard held ends the point those files are of.
    """
    point = points[number - 1]
    mine = [run for run, place in zip(result.runs, result.numbers, strict=True)
            if place == number]
    at = f"{number} of {len(points)} (planned {point.index}"
    stopped_at, stopped_file, first_missing = 0, "", number
    if mine and whole:
        where, first_missing = f"after point {at})", number + 1
    elif mine:
        run = next((run for run in mine if run.stopped_early), mine[-1])
        stopped_at, stopped_file, first_missing = number, stem_of(run), number + 1
        where = f"point {at}, {stopped_file})"
    else:
        where = f"before point {at})"
    return replace(result, stopped=f"{where}: {reason}", stopped_at=stopped_at,
                   stopped_file=stopped_file,
                   not_acquired=tuple((entry.index, entry.reference)
                                      for entry in points[first_missing - 1:]))


def _rendered(job: Acquire) -> Rendered | None:
    """The render an `Acquire` names, done again here, or None for a hand-written method.

    Rendered on this side rather than carried, because a `Rendered` holds its `Template`
    and the owner may be in another process. The template text and the values are
    enough to reproduce it exactly; a render that does not reproduce the job's method
    is refused by the stamp, before a file exists."""
    if not job.template:
        return None
    return render(loads_template(job.template), dict(job.knobs), dict(job.labels))


def sentence(exc: BaseException) -> str:
    """One line for a trainee, from whatever was raised.

    The type is kept only where the message alone would not say what went wrong: a
    `MipsError`, an `AcqError` and a refused lock already read as sentences, and
    prefixing them with their class name makes a status bar read like a traceback.
    """
    text = str(exc).strip()
    if isinstance(exc, (MipsError, AcqError, LockError, ValueError)) and text:
        return text
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
