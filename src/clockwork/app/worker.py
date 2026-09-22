"""The one thread that talks to the boxes and the console, and the queue that feeds it.

Every serial write, every ZeroMQ round trip and every second of an acquisition happens
here. The rule it exists to keep is the first one on the never-do list: an `STBLDAT`
table string has a three-second inter-token timeout on the box, so a send that stalls
behind a repaint is a table the box silently abandons, and a `run_acquisition` on the UI
thread would freeze the window for the length of an experiment.

**A work queue, not a mailbox.** Mainspring's render path is a single-slot mailbox
because a stale frame is waste; here every job must happen -- a send that was overtaken
by the next one is a half-configured instrument. What *is* a single slot is the progress
that comes back: `Mailbox` keeps every event worth reading and collapses only the
repetition counter, so a hundred repetitions cost the UI thread a hundred drains of one
line rather than a thousand cross-thread signals.

**One job at a time, and the window knows which.** `started` and `finished` bracket
every job, and the window greys out whatever would queue a second send while one is in
flight; the queue itself is there so that a job asked for during another one is honoured
rather than dropped, not so that two can overlap.

**The boxes are opened once and kept.** Closing a port drops DTR, which makes the
firmware reset its own USB port and re-enumerate, so a window that opened a box per send
would reset the rack on every button (lab record, task 37). `discover` hands back open
boxes and this thread holds them until the window closes.

What crosses the seam is frozen: a job going in, a dataclass or a `Run` coming back.
Nothing above this line holds a `Box` or a `Console`, and nothing in it holds a widget.
"""

from __future__ import annotations

import contextlib
import os
import queue
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from PySide6.QtCore import QThread, Signal

from .. import transcript
from ..acq import (
    SECONDS_PER_SAMPLE_2GSPS,
    AcqError,
    Console,
    ConsoleConfig,
    ConsoleProcess,
    ConsoleSupervisor,
    DataStream,
    Event,
    FakeConsoleProcess,
    Run,
    Snapshot,
    Warned,
    prepare_console,
    refusals,
    run_acquisition,
    send_phases,
    start_chain,
)
from ..instrument import UNCALIBRATED, Instrument
from ..method import Method
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
from .naming import next_stem

__all__ = [
    "Acquire",
    "ConsoleStatus",
    "Discover",
    "Job",
    "Mailbox",
    "ReadState",
    "RestartConsole",
    "Send",
    "SendResult",
    "StartConsole",
    "Worker",
    "fake_rack",
    "matches_wire",
    "wire_fingerprint",
]

FAKE_FRAME_HOLD_S = 0.25
"""How long `FakeConsole` waits inside `acquire frame` before publishing, under `--fake`.

The stand-in publishes from the handler for `acquire frame`, so without this its
batches race the start list and the enable-gate guard fires correctly on a fault that
is not there. It stands in for the pushes a real frame spends waiting for its enable.
Taken from the bench script that first needed it (lab record, task 28).
"""


# -- what goes in ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Job:
    """One thing to do on the worker thread. Subclasses carry the arguments.

    `label` is what the window shows while it runs and what `started` and `finished`
    carry, so a status line never has to map a job type onto a sentence.
    """

    label: str = "working"


@dataclass(frozen=True, slots=True)
class Discover(Job):
    """Find the boxes: `GNAME` across the MIPS-class ports, or a simulated rack.

    `method` is the roster `--fake` builds its stand-ins from, and is ignored against
    real hardware, where the ports say what is there and the method is only a hint.
    """

    label: str = "finding boxes"
    method: Method | None = None
    ports: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class StartConsole(Job):
    """Launch the acquisition console and wait for it to answer. About 5.4 s."""

    label: str = "starting the console"
    command: str = ""
    """The executable. Empty under `--fake`, where the stand-in runs in process."""


@dataclass(frozen=True, slots=True)
class RestartConsole(Job):
    """Stop and start it again, which is what a written `config.txt` key needs.

    `values` is written to the file first, where one is given; the console reads that
    file at startup and never again, so writing without restarting changes nothing
    (lab record, task 49).
    """

    label: str = "restarting the console"
    values: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Send(Job):
    """`send_phases`, in one of its two shapes.

    `setup=True` is "Send setup": every phase, plus the state readback that is the
    half of the record the strings do not carry. `setup=False` is "Load and arm": the
    table and the mode change only, for a box that has had its setup since power-up.
    """

    label: str = "sending"
    method: Method | None = None
    setup: bool = True
    conditions: str = ""
    directory: str = ""
    stem: str = ""
    method_path: str = ""
    instrument: Instrument = UNCALIBRATED
    instrument_path: str = ""


@dataclass(frozen=True, slots=True)
class Acquire(Job):
    """A whole series: one acquisition, then `replicates - 1` technical replicates.

    Each replicate walks the method's `reset` list and does the same again into a file
    of its own, with **the same `Snapshot`** the first run was stamped with -- a
    replicate re-sends neither `setup` nor `load`, so the boxes hold what the first run
    left them holding and reading them again would record one measurement twice.

    The stems are worked out one at a time rather than up front, so a file a trainee
    drops into the directory between two replicates still moves the counter.
    """

    label: str = "acquiring"
    method: Method | None = None
    instrument: Instrument = UNCALIBRATED
    instrument_path: str = ""
    method_path: str = ""
    directory: str = ""
    stem: str = ""
    initials: str = ""
    replicates: int = 1
    conditions: str = ""
    replicate_only: bool = False
    """True for the Replicate button: one more run off the last one's snapshot,
    walking the reset list first. False for Acquire, whose first run is not a
    replicate of anything."""


@dataclass(frozen=True, slots=True)
class ReadState(Job):
    """Read every box's persistent state back, for the state panel (task 51)."""

    label: str = "reading the boxes"
    names: tuple[str, ...] = ()


# -- what comes back ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConsoleStatus:
    """What the status bar says about the console, in one object.

    `state` is one of `"not started"`, `"starting"`, `"ready"`, `"stopped"` or
    `"failed"`. Everything else is what the console itself said, so the bar never
    reports a number the process did not state.
    """

    state: str = "not started"
    detail: str = ""
    info: str = ""
    seconds: float | None = None
    endpoint: str = ""
    drift: tuple[str, ...] = ()
    """Keys where `config.txt` and the running process disagree, each as one line.
    A restart is what makes them agree; nothing else can."""

    @property
    def text(self) -> str:
        parts = [f"console: {self.state}"]
        if self.seconds is not None and self.state == "ready":
            parts[0] += f" in {self.seconds:.1f} s"
        if self.detail:
            parts.append(self.detail)
        return "  ".join(parts)


@dataclass(frozen=True, slots=True)
class SendResult:
    """What one `Send` job left behind: the snapshot, and where its log went."""

    snapshot: Snapshot
    setup: bool
    send_log: str = ""
    transcript_path: str = ""
    seconds: float = 0.0
    armed: tuple = ()
    """What was put on the wire, as `wire_fingerprint` reads it.

    The window compares this with the panes as they stand: `run_acquisition` expects
    the boxes to be loaded and armed already, so a method edited after a send is a
    method whose table is not the one in the box. Without this, pressing Acquire on
    cold boxes is three refused `TBLSTRT`s and a run that stopped after three frames,
    which is a true report of a mistake nobody was warned about.
    """


def wire_fingerprint(method: Method, *, setup: bool = True) -> tuple:
    """What a send would put on each box, in a form two methods can be compared by.

    The phases and the analog declarations, and nothing else. Deliberately not
    `method.stamp()`'s hash, which covers the whole document including `file_stem` --
    that changes on every acquisition, so an arming would expire the moment the counter
    moved, which is exactly when it is still good.

    **`setup=False` leaves the setup strings out, because that send does not deliver
    them.** A `Load and arm` sends `load` and `arm` and nothing else, so a fingerprint
    that carried the panes' setup lines would record as armed a set of strings the wire
    never saw, and Acquire would go green on a box whose setup is whatever the last
    method left it at. The recorded tuple holds `None` there instead, and `matches_wire`
    declines to compare it -- which is the truthful answer, since a trainee who presses
    Load and arm has said the box already has its setup and clockwork has no reading that
    either confirms or denies it. Latent on 2026-09-17, found reading the code afterwards
    (lab record, task 56).
    """
    return tuple(
        (entry.name, tuple(entry.setup) if setup else None,
         tuple(entry.load), tuple(entry.arm),
         tuple(entry.dc_bias), tuple(entry.rf))
        for entry in method.boxes
    )


def matches_wire(armed: tuple, method: Method) -> bool:
    """Whether the panes still say what a recorded send actually put on the wire.

    Not `==` against a fresh fingerprint, because a `setup=False` send records `None`
    for the phase it did not deliver and a phase that was never sent cannot have
    changed since. Everything else is compared exactly: the table in the box and the
    mode it was left in are what an acquisition starts against, and a pane edited after
    the send is a table the box is not holding.
    """
    if not armed:
        return False
    now = wire_fingerprint(method)
    if len(armed) != len(now):
        return False
    # `strict` on both: the lengths were compared above and a box-count or field-count
    # mismatch that got past that is a bug here, not a method to be judged.
    return all(
        all(was is None or was == this
            for was, this in zip(before, after, strict=True))
        for before, after in zip(armed, now, strict=True)
    )


# -- the progress mailbox ----------------------------------------------------------


class Mailbox:
    """Progress from the worker thread to the UI thread, with the counter collapsed.

    Every event the loop reports is worth showing except one: `BatchSeen` arrives ten
    times a repetition and a hundred repetitions of a method frame would put a thousand
    lines through a widget that can show a number instead. So a `BatchSeen` that lands
    on top of another `BatchSeen` replaces it -- that is the single slot -- and every
    other event queues behind whatever is already there.

    **A collapsed event has to carry absolute state**, which is why `BatchSeen` reports
    the scans its frame has published rather than the scans in its own batch: what
    survives a collapse must say as much as everything it replaced, or the progress bar
    it feeds counts only the batches that happened to be drawn (task 56).

    Bounded, because the one failure this must not have is a run that fills memory
    because the window stopped draining: past `limit` the oldest events go and a note
    takes their place, so the log says it lost lines rather than quietly losing them.

    Thread-safe and lock-free at the reader: `drain` takes everything in one swap.
    """

    def __init__(self, limit: int = 20000, collapse: Sequence[str] = ("BatchSeen",)) -> None:
        self._lock = threading.Lock()
        self._events: list[Event] = []
        self._limit = limit
        self._collapse = frozenset(collapse)
        self._dropped = 0

    def put(self, event: Event) -> None:
        with self._lock:
            kind = type(event).__name__
            if (kind in self._collapse and self._events
                    and type(self._events[-1]).__name__ == kind):
                self._events[-1] = event
                return
            self._events.append(event)
            if len(self._events) > self._limit:
                gone = len(self._events) - self._limit
                del self._events[:gone]
                self._dropped += gone

    def drain(self) -> tuple[list[Event], int]:
        """Everything waiting, and how many lines were dropped since the last drain."""
        with self._lock:
            events, self._events = self._events, []
            dropped, self._dropped = self._dropped, 0
        return events, dropped


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
    """
    rack: dict[str, Box] = {}
    for entry in method.boxes:
        arb = 4 if any(
            command.startswith(("SARB", "SWF", "SALTWFM", "ARBSYNC", "TARB"))
            for command in tuple(entry.setup) + tuple(entry.load)
        ) else 0
        rack[entry.name] = Box(
            transport=FakeBox(arb_modules=arb, rf_channels=0 if arb else 2),
            name=entry.name,
        )
    return rack


# -- the thread --------------------------------------------------------------------


class Worker(QThread):
    """The instrument, behind a queue.

    Started with the window and left running for its lifetime; `shutdown()` on close
    is what lets it exit instead of blocking the process. Every signal here is emitted
    from this thread and delivered on the UI thread by Qt's queued connections, which
    is the only reason a window may connect a slot that touches a widget to one.
    """

    started_job = Signal(object)
    """The `Job` that has just begun. The window greys out what would queue a send."""
    finished_job = Signal(object, object)
    """`(Job, result)` -- the job that ended and whatever it produced, or None."""
    failed_job = Signal(object, str)
    """`(Job, message)` -- it raised, and the message is the sentence to show.

    Never the exception object: a window that showed a traceback to a trainee in the
    middle of an acquisition day would be showing them the wrong thing, and the
    transcript beside the file has the rest."""

    discovered = Signal(object)
    """A `Discovery`: what answered, what was silent, what was not asked."""
    console_state = Signal(object)
    """A `ConsoleStatus`, whenever it changes."""
    run_done = Signal(object)
    """One `Run`, as each replicate of a series finishes rather than at the end, so a
    trainee sees the first file land while the second is still acquiring."""
    state_read = Signal(str, object)
    """`(box name, BoxState)` from a `ReadState` job (task 51's panel)."""
    said = Signal(str)
    """One line for the run log that did not come from the loop: what this thread is
    about to do, and what it found when it did."""

    def __init__(self, *, fake: bool = False, mailbox: Mailbox | None = None) -> None:
        super().__init__()
        self.fake = fake
        self.mailbox = mailbox or Mailbox()

        self.boxes: dict[str, Box] = {}
        """Open boxes, keyed by `GNAME`. Owned by this thread; nothing else closes
        one."""

        self.listings: dict[str, frozenset[str]] = {}
        """The `GCMDS` cache, kept for the session. Three boxes cost about five
        seconds of listing on the first send and 0.3 s on every one after
        (`send_phases(listings=)`)."""

        self.snapshot: Snapshot | None = None
        """What the last `Send` read off the boxes, and what every run until the next
        send is stamped with."""

        self.console: ConsoleSupervisor | None = None
        self.status = ConsoleStatus()

        self._queue: queue.Queue[Job | None] = queue.Queue()
        self._stop = threading.Event()
        self._stop_reason = ""
        self._setup_log: tuple[str, str] | None = None
        """`(directory, stem)` of the send log that holds the last setup send.

        Only for the sentence a replicate's own log carries, saying where the strings it
        did not re-send went. What decides whether a log is appended to is `_send_log`,
        which is every send and not only a setup one."""

        self._send_log: tuple[str, str] | None = None
        """`(directory, stem)` of the last send log this worker opened, of any kind.

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
        self.start()

    # -- the queue -----------------------------------------------------------

    def submit(self, job: Job) -> None:
        """Queue a job. Never blocks; the window's own thread calls this."""
        self._queue.put(job)

    def request_stop(self, reason: str = "stopped by the operator") -> None:
        """Ask the run in flight to end after the current repetition and its fold.

        Sets a flag the acquisition thread reads between repetitions; it does not
        interrupt anything. A repetition is about a second, so the button feels
        immediate and what it leaves on disk is a short experiment rather than a
        broken one.
        """
        self._stop_reason = reason
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def shutdown(self) -> None:
        """Stop the thread and let go of everything it owns. Idempotent."""
        self.request_stop("the window is closing")
        self._queue.put(None)

    # -- the loop ------------------------------------------------------------

    def run(self) -> None:  # noqa: D102 -- QThread's own entry point
        try:
            while True:
                job = self._queue.get()
                if job is None:
                    break
                self.started_job.emit(job)
                try:
                    result = self._do(job)
                except Exception as exc:  # noqa: BLE001 -- a message, not a traceback
                    self.failed_job.emit(job, _sentence(exc))
                else:
                    self.finished_job.emit(job, result)
        finally:
            self._close_everything()

    def _do(self, job: Job) -> object:
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
        raise TypeError(f"no worker handler for {type(job).__name__}")

    # -- the boxes -----------------------------------------------------------

    def _discover(self, job: Discover) -> Discovery:
        self._close_boxes()
        if self.fake:
            if job.method is None:
                self.said.emit("--fake: open a method and the rack is built from it")
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
                self.said.emit(
                    f"--fake: {len(self.boxes)} simulated box(es) from the method. "
                    "Nothing here is evidence about a MIPS box.")
            self.discovered.emit(found)
            return found

        found = discover(ports=job.ports)
        self.boxes = found.boxes
        self.listings = {name: listing for name, listing in self.listings.items()
                         if name in self.boxes}
        self.said.emit(f"{found.text}")
        for entry in found.silent:
            self.said.emit(entry.text)
        for port, why in found.unusable:
            self.said.emit(f"{port}: {why}")
        self.discovered.emit(found)
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
            self.state_read.emit(name, state)
        return states

    # -- the console ---------------------------------------------------------

    def _start_console(self, job: StartConsole) -> ConsoleStatus:
        if self.console is not None and self.console.alive:
            return self.status
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
        return self.status

    def _restart_console(self, job: RestartConsole) -> ConsoleStatus:
        if self.console is None:
            raise AcqError("there is no console to restart; start one first")
        if job.values and isinstance(self.console, ConsoleProcess):
            config = self.console.config.set(**dict(job.values))
            problems = config.problems()
            if problems:
                raise AcqError("; ".join(problems))
            config.save()
            self.said.emit(f"wrote {', '.join(sorted(job.values))} to config.txt")
        self._set_status(ConsoleStatus(state="starting"))
        seconds = self.console.restart()
        self._set_status(self._status_now(seconds))
        return self.status

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
        self.status = status
        self.console_state.emit(status)

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
        with self._logs(directory, stem, header, append=append) as paths:
            snapshot = send_phases(
                method, self.boxes, setup=job.setup, progress=self.mailbox.put,
                conditions=job.conditions, listings=self.listings,
            )
        self.snapshot = snapshot
        if job.setup:
            self._setup_log = (directory, stem)
        return SendResult(snapshot=snapshot, setup=job.setup, send_log=paths[1],
                          transcript_path=paths[0],
                          seconds=time.perf_counter() - started,
                          armed=wire_fingerprint(method, setup=job.setup))

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
                    self.mailbox.put(Warned(message))
                self.said.emit(
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
                    run = self._one_run(
                        method, job, console, stream, width, directory, stem,
                        replicate=replicate,
                        first_log=self._send_log if not replicate else None,
                        prologue=prologue if index == 0 else None,
                    )
                    runs.append(run)
                    self._last_run = run
                    self.run_done.emit(run)
                    if run.stopped_early or self._stop.is_set():
                        if index + 1 < job.replicates:
                            self.said.emit(
                                f"stopped: {job.replicates - index - 1} replicate(s) "
                                "not acquired")
                        break
            finally:
                if console.acquiring or console.running:
                    try:
                        console.stop_acquire()
                    except AcqError as exc:
                        self.said.emit(
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
                              job.instrument_path, job.conditions)
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
        with self._logs(directory, stem, header, append=append):
            if prologue is not None:
                width = prologue()
            return run_acquisition(
                method,
                boxes=self.boxes,
                console=console,
                stream=stream,
                width=width,  # type: ignore[arg-type]
                directory=directory,
                stem=stem,
                post_trigger_samples=self._post_trigger_samples(console),
                replicate=replicate,
                progress=self.mailbox.put,
                instrument=job.instrument,
                snapshot=self.snapshot,
                stop=self._stop_check,
            )

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
        here rather than by the window because a file opened on the UI thread is a
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
                instrument_path: str, conditions: str) -> str:
        return transcript.run_header(
            method=method, method_path=method_path or None,
            instrument=instrument, instrument_path=instrument_path or None,
            console=getattr(self.console, "info", None),
            boxes=[(name, row.port, row.identity, row.firmware)
                   for name, row in _identities(self.boxes)],
            conditions=conditions,
        )

    # -- housekeeping --------------------------------------------------------

    def _require_boxes(self, method: Method) -> None:
        missing = [entry.name for entry in method.boxes if entry.name not in self.boxes]
        if missing:
            raise MipsError(
                "the method names box(es) no port answered to: " + ", ".join(missing)
                + ". Find boxes again, or check they are powered on.")

    def _close_boxes(self) -> None:
        for box in self.boxes.values():
            try:
                box.close()
            except Exception:  # noqa: BLE001 -- a close that fails has nothing to fail
                pass
        self.boxes = {}

    def _close_everything(self) -> None:
        self._close_boxes()
        if self.console is not None:
            try:
                self.console.stop()
            except Exception:  # noqa: BLE001
                pass
            self.console = None


# -- small helpers -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Identity:
    port: str
    identity: str
    firmware: str


def _identities(boxes: Mapping[str, Box]) -> list[tuple[str, _Identity]]:
    """Each box's port, `GNAME` and `GVER` for the run header, asked once.

    Two getters per box on a thread that is about to send hundreds of strings; a box
    that will not answer them contributes an empty row rather than stopping the run,
    because a header with a blank firmware is better than no acquisition.
    """
    rows: list[tuple[str, _Identity]] = []
    for name, box in boxes.items():
        port = getattr(box.transport, "port_name", "simulated")
        try:
            with box.summarised():
                rows.append((name, _Identity(port, box.box_name(), box.version())))
        except (MipsError, OSError, ValueError):
            rows.append((name, _Identity(port, "", "")))
    return rows


def _needs_method(method: Method | None) -> Method:
    if method is None:
        raise ValueError("there is no method to send; open or write one first")
    return method


def _sentence(exc: BaseException) -> str:
    """One line for a trainee, from whatever was raised.

    The type is kept only where the message alone would not say what went wrong: a
    `MipsError` and an `AcqError` already read as sentences, and prefixing them with
    their class name makes a status bar read like a traceback.
    """
    text = str(exc).strip()
    if isinstance(exc, (MipsError, AcqError, ValueError)) and text:
        return text
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
