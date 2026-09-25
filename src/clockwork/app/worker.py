"""The window's owner of the hardware: a `LocalOwner` on a `QThread`, speaking in signals.

Everything that talks to the boxes and the console is `clockwork.owner.LocalOwner`,
which imports no Qt; this module is the adapter the window has always connected to. It
runs the owner's `serve` loop on its own `QThread` and turns each event the owner
reports into the signal that used to say it, emitted on that thread and delivered on
the UI thread by Qt's queued connections -- which is the only reason a window may
connect a slot that touches a widget to one. The owner was taken out of this class
(lab record, task 67) so that a front end with no window at all -- the daemon, the MCP
server -- drives exactly the same code.

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

What crosses the seam is frozen: a job going in, a dataclass or a `Run` coming back.
Nothing above this line holds a `Box` or a `Console`, and nothing in it holds a widget.
The jobs, results and helpers the window imports from here are `clockwork.owner`'s,
under the names they have always had.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

from PySide6.QtCore import QThread, Signal

from ..acq import ConsoleConfig, ConsoleSupervisor, Event, Snapshot
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

__all__ = [
    "FAKE_FRAME_HOLD_S",
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

PROGRAM = "the clockwork window"
"""What a second owner is told holds the instrument while this window is open."""


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


# -- the thread --------------------------------------------------------------------


def _scan(**kwargs: object):  # noqa: ANN202 -- `discover`'s own return type
    """`discover`, looked up in this module when a scan runs rather than when the owner
    is built, so a test that stands a rack in for this module's `discover` reaches the
    owner's scan as it reached `Worker`'s."""
    return discover(**kwargs)


class Worker(QThread):
    """The instrument, behind a queue, for the window.

    Started with the window and left running for its lifetime; `shutdown()` on close
    is what lets it exit instead of blocking the process. Every signal here is emitted
    from this thread and delivered on the UI thread by Qt's queued connections.

    **Under the instrument lock.** A window that is not `--fake` takes the lock as it
    is built; `refused` is the lock's sentence when another owner holds it, and every
    hardware job fails with that sentence until the other owner is gone.
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

    def __init__(self, *, fake: bool = False, mailbox: Mailbox | None = None,
                 kept_root: str | None = None, errors_log: str = "") -> None:
        super().__init__()
        self.fake = fake
        self.mailbox = mailbox or Mailbox()
        self.owner = LocalOwner(fake=fake, on_event=self._relay, program=PROGRAM,
                                discover=_scan, kept_root=kept_root,
                                errors_log=errors_log)
        self.start()

    def run(self) -> None:  # noqa: D102 -- QThread's own entry point
        self.owner.serve()

    def _relay(self, _handle: Handle, event: Event) -> None:
        """One owner event, as the signal that says it; the loop's own into the mailbox."""
        if isinstance(event, JobStarted):
            self.started_job.emit(event.job)
        elif isinstance(event, JobFinished):
            self.finished_job.emit(event.job, event.result)
        elif isinstance(event, JobFailed):
            self.failed_job.emit(event.job, event.message)
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
    def status(self) -> ConsoleStatus:
        return self.owner.console_status

    @property
    def console(self) -> ConsoleSupervisor | None:
        return self.owner.console

    @console.setter
    def console(self, console: ConsoleSupervisor | None) -> None:
        self.owner.console = console

    def _console_config(self) -> ConsoleConfig | None:
        return self.owner._console_config()  # noqa: SLF001 -- the adapter's own owner
