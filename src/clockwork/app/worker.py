"""The window's owner of the hardware, speaking in signals: a daemon's client, or `--fake`'s own.

Everything that talks to the boxes and the console is `clockwork.owner.LocalOwner`,
which imports no Qt; this module is the adapter the window has always connected to,
turning each event an owner reports into the signal that used to say it, emitted on a
thread of this module's and delivered on the UI thread by Qt's queued connections --
which is the only reason a window may connect a slot that touches a widget to one. The
owner was taken out of this module (lab record, task 67) so that a front end with no
window at all -- the daemon, the MCP server -- drives exactly the same code.

**Two adapters behind one set of signals** (lab record, task 77). `RemoteWorker` is the
window on an instrument: a client of `clockwork serve`, which it starts when none is
running, following every job the daemon issues -- its own, Claude's through the MCP
server, a command line's -- so that the person at the instrument sees another client's
work arrive exactly as their own does, labelled with whose it is. `Worker` is `--fake`:
a `LocalOwner` in this process on a `QThread`, as the window always had, which is what
keeps a window test from paying for a daemon it does not exercise.

**A work queue, not a mailbox.** Mainspring's render path is a single-slot mailbox
because a stale frame is waste; here every job must happen -- a send that was overtaken
by the next one is a half-configured instrument. What *is* a single slot is the progress
that comes back: `Mailbox` keeps every event worth reading and collapses only the
repetition counter, so a hundred repetitions cost the UI thread a hundred drains of one
line rather than a thousand cross-thread signals.

**One job at a time, and the window knows which.** `started_job` and `finished_job`
bracket every job, whichever client submitted it, and the window greys out whatever
would queue a second send while one is in flight; the owner's queue is there so that a
job asked for during another one is honoured rather than dropped, not so that two can
overlap.

What crosses the seam is frozen: a job going in, a dataclass or a `Run` coming back.
Nothing above this line holds a `Box` or a `Console`, and nothing in it holds a widget.
The jobs, results and helpers the window imports from here are `clockwork.owner`'s,
under the names they have always had.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from collections.abc import Callable, Sequence

from PySide6.QtCore import QThread, Signal

from ..acq import ConsoleConfig, ConsoleProcess, ConsoleSupervisor, Event, Snapshot, find_console
from ..mips import Box, discover
from ..owner import (
    FAKE_FRAME_HOLD_S,
    Acquire,
    BoxStateRead,
    ConsoleChanged,
    ConsoleStatus,
    Discover,
    Discovered,
    Handle,
    Job,
    JobFailed,
    JobFinished,
    JobStarted,
    LocalOwner,
    ReadState,
    RestartConsole,
    RunDone,
    Said,
    Send,
    SendResult,
    StartConsole,
    fake_rack,
    matches_wire,
    wire_fingerprint,
)
from ..owner.interface import OwnerStatus, StaleHandle
from ..owner.local import sentence
from ..owner.remote import DEFAULT_COMMAND, DaemonError, DaemonUnavailable, Hello, RemoteOwner
from .serving import why_serve_stopped, window_origin

__all__ = [
    "FAKE_FRAME_HOLD_S",
    "Acquire",
    "ConsoleStatus",
    "Discover",
    "Job",
    "Mailbox",
    "ReadState",
    "RemoteWorker",
    "RestartConsole",
    "Send",
    "SendResult",
    "StartConsole",
    "Worker",
    "fake_rack",
    "matches_wire",
    "wire_fingerprint",
]

PROGRAM = "the clockwork window"
"""What a second owner is told holds the instrument while a `--fake` window's owner is
built. A `--fake` owner takes no lock, so in practice only a test sees it."""

SERVE = "clockwork serve"
"""The origin shown for a job the daemon submitted itself: its start-up scan and
console, and a restart of a console that stopped on its own."""


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


# -- the signals both adapters speak -------------------------------------------------


class _Signals(QThread):
    """The signals the window connects to, and the one relay that emits them."""

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
    warned = Signal(str)
    """As `said`, a line that is a warning: the daemon could not be reached or started."""
    connected = Signal(object, bool)
    """`(Hello, started)` -- a daemon answered, and whether this window started it.
    Never emitted under `--fake`."""
    status_changed = Signal(object)
    """An `OwnerStatus` that differs from the last one. Never emitted under `--fake`."""

    def __init__(self, mailbox: Mailbox | None) -> None:
        super().__init__()
        self.mailbox = mailbox or Mailbox()

    def _emit(self, event: Event, job: Job | None = None) -> None:
        """One owner event, as the signal that says it; the loop's own into the mailbox.
        `job` stands in for the event's own copy of its job, where there is one."""
        if isinstance(event, JobStarted):
            self.started_job.emit(job or event.job)
        elif isinstance(event, JobFinished):
            self.finished_job.emit(job or event.job, event.result)
        elif isinstance(event, JobFailed):
            self.failed_job.emit(job or event.job, event.message)
        elif isinstance(event, Said):
            self.said.emit(event.line)
        elif isinstance(event, Discovered):
            self.discovered.emit(event.discovery)
        elif isinstance(event, ConsoleChanged):
            self.console_state.emit(event.status)
        elif isinstance(event, RunDone):
            self.run_done.emit(event.run)
        elif isinstance(event, BoxStateRead):
            self.state_read.emit(event.box, event.state)
        else:
            self.mailbox.put(event)


# -- --fake: the owner in this process ----------------------------------------------


def _scan(**kwargs: object):  # noqa: ANN202 -- `discover`'s own return type
    """`discover`, looked up in this module when a scan runs rather than when the owner
    is built, so a test that stands a rack in for this module's `discover` reaches the
    owner's scan as it reached `Worker`'s."""
    return discover(**kwargs)


class Worker(_Signals):
    """The instrument, behind a queue, in the window's own process.

    What a `--fake` window runs on, and what every window ran on before the daemon.
    Started with the window and left running for its lifetime; `shutdown()` on close
    is what lets it exit instead of blocking the process. Every signal here is emitted
    from this thread and delivered on the UI thread by Qt's queued connections.

    **Under the instrument lock** when not `--fake`: `refused` is the lock's sentence
    when another owner holds it, and every hardware job fails with that sentence until
    the other owner is gone.
    """

    remote = False
    spawned = None

    def __init__(self, *, fake: bool = False, mailbox: Mailbox | None = None,
                 kept_root: str | None = None, errors_log: str = "") -> None:
        super().__init__(mailbox)
        self.fake = fake
        self.owner = LocalOwner(fake=fake, on_event=self._relay, program=PROGRAM,
                                discover=_scan, kept_root=kept_root,
                                errors_log=errors_log)
        self.start()

    def run(self) -> None:  # noqa: D102 -- QThread's own entry point
        self.owner.serve()

    def _relay(self, _handle: Handle, event: Event) -> None:
        self._emit(event)

    # -- the queue -----------------------------------------------------------

    def submit(self, job: Job) -> Handle:
        """Queue a job. Never blocks; the window's own thread calls this."""
        return self.owner.submit(job)

    def request_stop(self, reason: str = "stopped by the operator") -> None:
        """Ask the run in flight to end after the current repetition and its fold."""
        self.owner.stop(reason)

    @property
    def stopping(self) -> bool:
        return self.owner.stopping

    def shutdown(self) -> None:
        """Stop the thread and let go of everything it owns. Idempotent."""
        self.owner.shutdown("the window is closing")

    # -- what the window reads, off the owner --------------------------------

    def origin(self, job: Job) -> str:
        """Every job here is the window's own."""
        return ""

    @property
    def refused(self) -> str:
        """The instrument lock's sentence while another owner holds it, else empty."""
        return self.owner.refused

    @property
    def boxes(self) -> dict[str, Box]:
        return self.owner.boxes

    @property
    def listings(self) -> dict[str, frozenset[str]]:
        return self.owner.listings

    @property
    def snapshot(self) -> Snapshot | None:
        return self.owner.snapshot()

    @property
    def has_snapshot(self) -> bool:
        return self.owner.snapshot() is not None

    @property
    def status(self) -> ConsoleStatus:
        return self.owner.console_status

    @property
    def console_alive(self) -> bool:
        return self.console is not None and self.console.alive

    @property
    def console(self) -> ConsoleSupervisor | None:
        return self.owner.console

    @console.setter
    def console(self, console: ConsoleSupervisor | None) -> None:
        self.owner.console = console

    def config_reader(self) -> Callable[[], ConsoleConfig] | None:
        """What reads the running console's `config.txt`, or None for a stand-in."""
        console = self.console
        return console.config if isinstance(console, ConsoleProcess) else None

    def set_kept_root(self, root: str) -> bool:
        """Point failed runs' files somewhere else from now on. True: it took."""
        self.owner.kept_root = root
        return True

    def _console_config(self) -> ConsoleConfig | None:
        return self.owner._console_config()  # noqa: SLF001 -- the adapter's own owner


# -- the instrument: a client of the daemon -------------------------------------------


class RemoteWorker(_Signals):
    """The window as a client of `clockwork serve`, starting one if none is running.

    One thread does everything that reaches the daemon -- connecting, starting it,
    submitting, stopping, and following -- so that the UI thread never waits on a
    socket: `submit`, `request_stop` and `shutdown_daemon` hand their request to that
    thread and return. A request fails with a sentence, as a job does, and never with a
    frozen window.

    **Following every client's jobs.** Handle ids are dense for a daemon's session and
    `OwnerStatus.issued` is the newest, so each poll reads `status` and then the
    progress of every job it has not yet seen end, however it was submitted; a job's
    progress comes off `RemoteOwner`'s stream copy, so an idle poll costs one `status`
    request. The events a job reports are emitted as the same signals `Worker` emits.
    For a job this window submitted, the `Job` emitted is the very object submitted,
    because the window asks `job is self._queue_job`; for another client's it is the
    daemon's copy, one object per job, and `origin(job)` names that client.

    `launch` returns a started daemon process or is None for a window that must not
    start one; `connect_timeout` bounds one `hello`, `start_timeout` a daemon's start.
    """

    remote = True

    def __init__(self, *, mailbox: Mailbox | None = None,
                 endpoint: str = DEFAULT_COMMAND,
                 launch: Callable[[], subprocess.Popen] | None = None,
                 origin: str = "",
                 console_path: str = "",
                 poll_s: float = 0.1,
                 connect_timeout: float = 2.0,
                 start_timeout: float = 45.0,
                 log: str | None = None) -> None:
        super().__init__(mailbox)
        self.fake = False
        self.owner = RemoteOwner(endpoint, timeout=connect_timeout,
                                 origin=origin or window_origin())
        self.launch = launch
        self.console_path = console_path
        self.poll_s = poll_s
        self.start_timeout = start_timeout
        self._log = log
        self.hello: Hello | None = None
        self.spawned: subprocess.Popen | None = None
        """The daemon this window started, for as long as it is the one answering."""
        self._launched: subprocess.Popen | None = None
        """A daemon started and not yet answering."""

        self._inbox: queue.Queue[tuple[str, object]] = queue.Queue()
        self._done = threading.Event()
        self._guard = threading.Lock()
        """Over what the UI thread reads: the status, the jobs and their origins."""
        self._status: OwnerStatus | None = None
        self._unavailable = ""
        self._stop_asked = False
        self._session: str | None = None
        self._cursor = 0
        self._tracked: dict[int, Handle] = {}
        self._after: dict[int, int] = {}
        self._silent: dict[int, int] = {}
        self._mine: dict[int, Job] = {}
        """This window's jobs not yet ended, by handle id: the objects it submitted."""
        self._own: set[int] = set()
        """Every handle id this window submitted in the daemon's session, ended or not."""
        self._jobs: dict[int, tuple[Job, str]] = {}
        """Handle id to the `Job` emitted for it and its client, newest 64 kept.

        Not started here, unlike `Worker`: its first act is to connect, and `connected`
        emitted before the window had connected a slot to it would be lost. The window
        calls `start()` once its slots are in place; a job submitted before that
        waits in the inbox."""

    # -- the UI thread's calls ---------------------------------------------------

    def submit(self, job: Job) -> None:
        """Queue a job with the daemon. Never blocks; the answer comes as signals."""
        self._inbox.put(("submit", job))

    def request_stop(self, reason: str = "stopped by the operator") -> None:
        """Ask the run in flight to end after the current repetition and its fold,
        whichever client started it."""
        self._stop_asked = True
        self._inbox.put(("stop", reason))

    def shutdown_daemon(self, reason: str = "the window that started it is closing") -> None:
        """Ask the daemon to shut down: the run in flight ends after its repetition and
        fold, and the console, the boxes and the lock are let go."""
        self._inbox.put(("shutdown", reason))

    def shutdown(self) -> None:
        """End this client, after anything still in its inbox has been sent. The
        daemon is left running; `shutdown_daemon` first is what stops it."""
        self._done.set()

    # -- what the window reads -----------------------------------------------------

    @property
    def status(self) -> OwnerStatus | None:
        with self._guard:
            return self._status

    @property
    def refused(self) -> str:
        """Why no hardware job can run: the daemon's lock refusal, or no daemon."""
        with self._guard:
            if self._unavailable:
                return self._unavailable
            return self._status.refused if self._status is not None else ""

    @property
    def boxes(self) -> tuple[str, ...]:
        status = self.status
        return status.boxes if status is not None else ()

    @property
    def has_snapshot(self) -> bool:
        status = self.status
        return bool(status is not None and status.snapshot)

    @property
    def stopping(self) -> bool:
        status = self.status
        return self._stop_asked or bool(status is not None and status.stopping)

    @property
    def console_alive(self) -> bool:
        status = self.status
        return status is not None and status.console.state == "ready"

    @property
    def console(self) -> None:
        """The console is the daemon's; this process holds none."""
        return None

    def config_reader(self) -> Callable[[], ConsoleConfig] | None:
        """What reads the `config.txt` beside the console this PC is configured with,
        which is the one the daemon started when the window started it."""
        path = find_console(self.console_path or None)
        return ConsoleProcess(path).config if path else None

    def set_kept_root(self, root: str) -> bool:
        """The daemon was told its kept-files folder when it started; False."""
        return False

    def origin(self, job: Job) -> str:
        """Who submitted `job`: empty for this window's own, else the client's words."""
        with self._guard:
            for job_id, (known, origin) in self._jobs.items():
                if known is job:
                    return "" if job_id in self._own else (origin or SERVE)
        return ""

    def mine(self, handle: Handle | None) -> bool:
        return handle is not None and handle.id in self._own

    def others(self) -> list[tuple[str, Handle]]:
        """`(running|queued, handle)` for every job in the daemon another client
        submitted, in the order they will run."""
        status = self.status
        if status is None:
            return []
        found = [("running", status.running)] if status.running is not None else []
        found += [("queued", handle) for handle in status.queued]
        return [(state, handle) for state, handle in found if not self.mine(handle)]

    def busy_mine(self, kinds: tuple[str, ...] = ("Send", "Acquire")) -> bool:
        """Whether a send or an acquisition this window submitted is running or waiting
        in the daemon. A scan or a reading of its own left queued behind another
        client's run is not what closing the window has to ask about."""
        status = self.status
        if status is None:
            return False
        pending = ([status.running] if status.running is not None else []) + list(
            status.queued)
        return any(self.mine(handle) and handle.kind in kinds for handle in pending)

    # -- the thread ------------------------------------------------------------------

    def run(self) -> None:  # noqa: D102 -- QThread's own entry point
        try:
            self._connect(start=True)
            while True:
                self._take_inbox()
                if self._done.is_set():
                    self._take_inbox()
                    return
                if self.hello is None:
                    self._done.wait(0.5)
                    continue
                try:
                    self._follow()
                except DaemonError as exc:
                    self._lose(exc)
                self._done.wait(self.poll_s)
        finally:
            self._abandon_start()
            self.owner.close()

    def _abandon_start(self) -> None:
        """A window closed while the daemon it started had not answered yet: give it a
        few seconds to, and ask it to shut down, rather than leave a daemon nobody
        started holding the instrument."""
        process = self._launched
        if self.hello is not None or process is None or process.poll() is not None:
            return
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and process.poll() is None:
            try:
                self.owner.hello()
                self.owner.shutdown("the window that started it closed before it answered")
                return
            except DaemonError:
                continue

    def _take_inbox(self) -> None:
        while True:
            try:
                what, value = self._inbox.get_nowait()
            except queue.Empty:
                return
            if what == "submit":
                self._submit(value)  # type: ignore[arg-type]
                continue
            if self.hello is None:
                continue
            try:
                if what == "stop":
                    self.owner.stop(str(value))
                elif what == "shutdown":
                    self.owner.shutdown(str(value))
            except DaemonError as exc:
                self.warned.emit(f"clockwork serve could not be asked to {what}: {exc}")

    def _submit(self, job: Job) -> None:
        if self.hello is None and not self._connect(start=True):
            self.failed_job.emit(job, self._unavailable or "clockwork serve is not running")
            return
        try:
            handle = self.owner.submit(job)
        except DaemonError as exc:
            self.failed_job.emit(job, sentence(exc))
            return
        self._mine[handle.id] = job
        self._own.add(handle.id)
        self._track(handle)

    # -- connecting --------------------------------------------------------------------

    def _connect(self, *, start: bool) -> bool:
        """Say hello to the daemon, starting one first if none answers and `launch`
        allows. True once connected; else `refused` says why."""
        try:
            hello = self.owner.hello()
            # A daemon this window started that answered only after `_start` gave up on
            # it is still this window's to stop.
            started = self._launched is not None and self._launched.poll() is None
            if started:
                self.spawned, self._launched = self._launched, None
        except DaemonUnavailable:
            if not start or self.launch is None:
                self._set_unavailable("no clockwork serve is running")
                return False
            hello = self._start()
            if hello is None:
                return False
            started = True
        except DaemonError as exc:
            self._set_unavailable(sentence(exc))
            return False
        try:
            status = self.owner.status()
        except DaemonError as exc:
            self._set_unavailable(sentence(exc))
            return False
        self.hello = hello
        self._session = self.owner.session
        if started:
            self._cursor = 0
        else:
            self.spawned = None
            live = ([status.running.id] if status.running is not None else []
                    ) + [handle.id for handle in status.queued]
            self._cursor = min(live) - 1 if live else status.issued
        with self._guard:
            self._unavailable = ""
        self._set_status(status)
        self.connected.emit(hello, started)
        return True

    def _start(self) -> Hello | None:
        self.said.emit("no clockwork serve is running on this PC; starting one")
        try:
            process = self._launched = self.launch()  # type: ignore[misc]
        except OSError as exc:
            self._set_unavailable(f"clockwork serve could not be started: {exc}")
            return None
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline and not self._done.is_set():
            code = process.poll()
            if code is not None:
                self._set_unavailable(why_serve_stopped(code, self._log))
                return None
            try:
                hello = self.owner.hello()
            except DaemonUnavailable:
                continue
            except DaemonError as exc:
                self._set_unavailable(sentence(exc))
                return None
            self.spawned = process
            self._launched = None
            return hello
        self._set_unavailable(why_serve_stopped(None, self._log))
        return None

    def _set_unavailable(self, message: str) -> None:
        with self._guard:
            changed = message != self._unavailable
            self._unavailable = message
        if changed:
            self.warned.emit(message)
            self.status_changed.emit(self.status)

    def _lose(self, exc: DaemonError) -> None:
        """The daemon stopped answering: say so once, and fail this window's jobs that
        were in it, whose progress went with it."""
        self.hello = None
        self.spawned = None
        self._fail_mine("clockwork serve stopped before this job finished")
        with self._guard:
            self._status = None
        self._set_unavailable(f"clockwork serve stopped answering ({exc}); press Find "
                              "boxes to start it again")

    def _fail_mine(self, message: str) -> None:
        for job_id in list(self._tracked):
            job = self._mine.get(job_id)
            if job is not None:
                self.failed_job.emit(job, message)
        self._tracked.clear()
        self._after.clear()
        self._silent.clear()
        self._mine.clear()
        self._own.clear()
        self._stop_asked = False

    # -- following -------------------------------------------------------------------

    def _follow(self) -> None:
        status = self.owner.status()
        if self.owner.session != self._session:
            self._fail_mine("clockwork serve was restarted before this job finished")
            self.warned.emit("clockwork serve was restarted by something else; following "
                             "the new one")
            self._session = self.owner.session
            self.spawned = None
            self._cursor = 0
            self.hello = self.owner.hello()
        self._set_status(status)
        for job_id in range(self._cursor + 1, status.issued + 1):
            self._track(Handle(id=job_id, kind="", label="", owner=self._session or ""))
        self._cursor = max(self._cursor, status.issued)
        live = {handle.id for handle in status.queued}
        if status.running is not None:
            live.add(status.running.id)
        for job_id, handle in sorted(self._tracked.items()):
            try:
                entries = self.owner.events(handle, self._after.get(job_id, 0))
            except StaleHandle:
                self._untrack(job_id)
                continue
            ended = False
            for entry in entries:
                self._after[job_id] = entry.seq
                ended = self._relay(job_id, entry.event) or ended
            if ended:
                self._untrack(job_id)
            elif entries or job_id in live:
                self._silent[job_id] = 0
            else:
                # Neither waiting, nor running, nor saying anything: its history has
                # been dropped, or its end is about to arrive. A few quiet polls in a
                # row tell the two apart.
                self._silent[job_id] = self._silent.get(job_id, 0) + 1
                if self._silent[job_id] >= 3:
                    self._untrack(job_id)

    def _track(self, handle: Handle) -> None:
        if handle.id not in self._tracked or handle.label:
            self._tracked[handle.id] = handle

    def _untrack(self, job_id: int) -> None:
        self._tracked.pop(job_id, None)
        self._after.pop(job_id, None)
        self._silent.pop(job_id, None)

    def _relay(self, job_id: int, event: Event) -> bool:
        """Emit one event of job `job_id`. True if it was the job's end."""
        job = getattr(event, "job", None)
        if isinstance(event, (JobStarted, JobFinished, JobFailed)):
            with self._guard:
                known = self._jobs.get(job_id)
                if known is None:
                    known = (self._mine.get(job_id) or event.job, event.handle.origin)
                    self._jobs[job_id] = known
                    while len(self._jobs) > 64:
                        del self._jobs[next(iter(self._jobs))]
            job = known[0]
        self._emit(event, job)
        if isinstance(event, (JobFinished, JobFailed)):
            self._stop_asked = False
            self._mine.pop(job_id, None)
            return True
        return False

    def _set_status(self, status: OwnerStatus) -> None:
        with self._guard:
            before, self._status = self._status, status
        if before is None or before.console != status.console:
            self.console_state.emit(status.console)
        if before != status:
            self.status_changed.emit(status)
