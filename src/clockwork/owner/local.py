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
`BatchSeen` is kept without its summed spectrum (a display product of hundreds of
kilobytes a batch that no front end draws; mainspring is the only viewer), each job
keeps at most `limit` events, and only the last `history` jobs are kept at all.

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
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace

import numpy as np

from .. import keep, transcript
from ..acq import (
    SECONDS_PER_SAMPLE_2GSPS,
    AcqError,
    BatchSeen,
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
    Warned,
    prepare_console,
    refusals,
    run_acquisition,
    send_phases,
    start_chain,
)
from ..instrument import Instrument
from ..method import Method
from ..method.template import Rendered, loads_template, render
from ..mips import (
    Box,
    BoxState,
    Discovery,
    FakeBox,
    Found,
    MipsError,
    discover,
    read_state,
)
from ..naming import next_stem
from .interface import (
    BoxStateRead,
    ConsoleChanged,
    Discovered,
    Handle,
    JobFailed,
    JobFinished,
    JobStarted,
    OwnerStatus,
    Progress,
    RunDone,
    Said,
    StaleHandle,
)
from .jobs import (
    Acquire,
    Armed,
    ConsoleStatus,
    Discover,
    Job,
    ReadState,
    RestartConsole,
    Send,
    SendResult,
    StartConsole,
    wire_fingerprint,
)
from .lock import Holder, InstrumentLock, LockError

__all__ = ["FAKE_FRAME_HOLD_S", "LocalOwner", "fake_rack", "sentence"]

FAKE_FRAME_HOLD_S = 0.25
"""How long `FakeConsole` waits inside `acquire frame` before publishing, under `--fake`.

The stand-in publishes from the handler for `acquire frame`, so without this its
batches race the start list and the enable-gate guard fires correctly on a fault that
is not there. It stands in for the pushes a real frame spends waiting for its enable.
Taken from the bench script that first needed it (lab record, task 28).
"""

_NO_SPECTRUM = np.zeros(0)

_CLOSING = "clockwork is shutting down, so this job was never started"

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

        self._ids = itertools.count(1)
        self._seqs = itertools.count(1)
        self._guard = threading.Lock()
        """Over `_history` and `_pending`, which the owner's thread, a folding worker
        and any caller of `events` or `status` all touch."""
        self._history: dict[int, list[Progress]] = {}
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

    def submit(self, job: Job) -> Handle:
        """Queue a job. Never blocks; any thread may call it.

        After `shutdown` the job is not queued at all: it is reported failed at once,
        because the thread that would have run it may already be gone.
        """
        handle = Handle(id=next(self._ids), kind=type(job).__name__, label=job.label,
                        owner=self.session)
        with self._guard:
            self._history[handle.id] = []
            if not self._closing:
                self._pending.append(handle)
            self._forget_old_jobs()
        if self._closing:
            self._report(JobFailed(handle=handle, job=job, message=_CLOSING), handle)
        else:
            self._queue.put((handle, job))
        return handle

    def events(self, handle: Handle, after: int = 0) -> list[Progress]:
        if handle.owner and handle.owner != self.session:
            raise StaleHandle(
                f"job {handle.id} ({handle.label}) was submitted to a different clockwork "
                "owner, most likely one that has since stopped, and its progress went "
                "with it")
        with self._guard:
            return [entry for entry in self._history.get(handle.id, ())
                    if entry.seq > after]

    def stop(self, reason: str = "stopped by the operator") -> None:
        """Ask the run in flight to end after the current repetition and its fold.

        Sets a flag the acquisition thread reads between repetitions; it does not
        interrupt anything. A repetition is about a second, so the button feels
        immediate and what it leaves on disk is a short experiment rather than a
        broken one.
        """
        self._stop_reason = reason
        self._stop.set()

    def snapshot(self) -> Snapshot | None:
        return self._snapshot

    def status(self) -> OwnerStatus:
        with self._guard:
            queued = tuple(self._pending)
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
        )

    def shutdown(self, reason: str = "the owner is shutting down") -> None:
        """Stop the thread and let go of everything it owns. Idempotent.

        The run in flight ends after its current repetition and its fold, as a Stop
        would end it; every job queued behind it fails without starting.
        """
        self._closing = True
        self.stop(reason)
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
        without its spectrum included.
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
                self._report(JobStarted(handle=handle, job=job))
                try:
                    result = self._do(job)
                except Exception as exc:  # noqa: BLE001 -- a message, not a traceback
                    self._running = None
                    if isinstance(exc, BoxLost):
                        self._forget(exc.box)
                    message = sentence(exc)
                    if self._kept:
                        message = f"{message.rstrip('.')}. Files kept in {self._kept}"
                    self._report(JobFailed(handle=handle, job=job, message=message))
                else:
                    self._running = None
                    self._report(JobFinished(handle=handle, job=job, result=result))
        finally:
            self._close_everything()

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
        if isinstance(job, ReadState):
            return self._read_state(job)
        raise TypeError(f"no owner handler for {type(job).__name__}")

    def _report(self, event: Event, handle: Handle | None = None) -> None:
        """Keep an event against its job and hand it to `on_event`. Any thread.

        `handle` is the job's, where it is not the one running: a job refused before it
        starts is reported against itself.
        """
        handle = handle or self._current or Handle(id=0, kind="", label="",
                                                    owner=self.session)
        kept = event
        if isinstance(event, BatchSeen) and event.batch.mz.size:
            kept = replace(event, batch=replace(event.batch, mz=_NO_SPECTRUM))
        with self._guard:
            entries = self._history.setdefault(handle.id, [])
            entry = Progress(seq=next(self._seqs), event=kept)
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
            self.console = FakeConsoleProcess(open_batches=1)
        elif job.command:
            self.console = ConsoleProcess(job.command)
        else:
            raise AcqError(
                "no acquisition console is configured. Point Settings at the console "
                "executable the installer put beside clockwork, or set "
                "$CLOCKWORK_CONSOLE.")
        self.console.start()
        if self.fake and getattr(self.console, "fake", None) is not None:
            self.console.fake.frame_hold_s = FAKE_FRAME_HOLD_S  # type: ignore[attr-defined]
        seconds = self.console.wait_ready()
        self._set_status(self._status_now(seconds))
        return self.console_status

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
        seconds = self.console.restart()
        self._set_status(self._status_now(seconds))
        return self.console_status

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

    def _send(self, job: Send) -> SendResult:
        method = _needs_method(job.method)
        self._require_boxes(method)
        problems = refusals(method)
        if problems:
            raise AcqError("; ".join(problems))
        header = self._header(method, job.method_path, job.instrument,
                              job.instrument_path, job.conditions)
        started = time.perf_counter()
        directory, stem = job.directory or os.getcwd(), job.stem
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
        fingerprint = wire_fingerprint(method, setup=job.setup)
        self._armed = Armed(method=method.metadata.name, fingerprint=fingerprint,
                            directory=directory, stem=stem, setup=job.setup)
        return SendResult(snapshot=snapshot, setup=job.setup, send_log=paths[1],
                          transcript_path=paths[0],
                          seconds=time.perf_counter() - started,
                          armed=fingerprint)

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

    def _acquire(self, job: Acquire) -> list[Run]:
        method = _needs_method(job.method)
        self._require_boxes(method)
        problems = refusals(method)
        if problems:
            raise AcqError("; ".join(problems))
        rendered = _rendered(job)
        command_endpoint, data_endpoint = self._endpoints()
        directory = job.directory or os.getcwd()
        os.makedirs(directory, exist_ok=True)
        self._stop.clear()

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
                width = start_chain(console, stream)
                return width

            try:
                taken: list[str] = []
                for index in range(max(1, job.replicates)):
                    stem = job.stem if index == 0 and job.stem else next_stem(
                        directory, job.initials, taken=taken)
                    taken.append(stem)
                    replicate = job.replicate_only or index > 0
                    series = None
                    if job.series:
                        place = max(1, job.series_index) + index
                        series = Series(id=job.series, index=place, position=place)
                    provenance = (Provenance(rendered=rendered, series=series)
                                  if rendered is not None or series is not None else None)
                    run = self._one_run(
                        method, job, console, stream, width, directory, stem,
                        replicate=replicate,
                        first_log=self._send_log if not replicate else None,
                        prologue=prologue if index == 0 else None,
                        provenance=provenance,
                    )
                    runs.append(run)
                    self._last_run = run
                    self._report(RunDone(run=run))
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
        if replicate and self._setup_log is not None:
            header += ("\nthis is a replicate: its method's setup, load and arm strings "
                       f"went in {transcript.send_log_name(self._setup_log[1])} and were "
                       "not sent again")
        # Only the boxes the method names: one that is held and not used has no part in
        # the run, and handing it in made unplugging it end the run (lab #1). Checked
        # again after the header, which forgets a box it finds unplugged.
        self._require_boxes(method)
        boxes = {entry.name: self.boxes[entry.name] for entry in method.boxes}
        try:
            return self._logged_run(method, job, console, stream, width, directory, stem,
                                    header, boxes, append=append, replicate=replicate,
                                    prologue=prologue, provenance=provenance)
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


def _needs_method(method: Method | None) -> Method:
    if method is None:
        raise ValueError("there is no method to send; open or write one first")
    return method


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
