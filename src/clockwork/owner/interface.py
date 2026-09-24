"""The owner protocol: what a front end may ask of whoever owns the hardware.

Five calls and a shutdown. `submit` a job and get a `Handle`; read that job's progress
with `events(handle, after)`; `stop` the run in flight; ask for the `snapshot` the next
run will be stamped with and the `status` of the whole owner. A front end that holds an
`Owner` holds nothing else -- no `Box`, no `Console`, no thread -- which is what lets
the same front end drive an owner in its own process (`LocalOwner`) or one in another
(`RemoteOwner`, the client of `clockwork serve`) without knowing which.

**Everything that crosses is plain data.** Jobs are `clockwork.owner.jobs`; progress is
the loop's own `Event` family plus the eight below, which say what `Worker`'s signals
used to say on their own; results are the dataclasses the jobs always returned. All of
it round-trips through JSON by `clockwork.owner.wire`, and `tools/check_public.py`
round-trips every `Event` subclass so that a new one cannot be added without a wire
form.

**Progress is numbered, not counted.** Each event is a `Progress` with a sequence
number the owner assigns, and a reader passes the last number it saw. A count of events
read would be wrong the first time the owner collapsed two `BatchSeen`s into one or let
an old line go, both of which it does for the reason the window's mailbox does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..acq import Event, Run, Snapshot
from ..mips import BoxState, Discovery
from .jobs import ConsoleStatus, Job
from .lock import Holder

__all__ = [
    "BoxStateRead",
    "ConsoleChanged",
    "Discovered",
    "Handle",
    "JobFailed",
    "JobFinished",
    "JobStarted",
    "Owner",
    "OwnerStatus",
    "Progress",
    "RunDone",
    "Said",
    "StaleHandle",
]


@dataclass(frozen=True, slots=True)
class Handle:
    """One submitted job. `id` is the owner's own, counted from 1 for its lifetime."""

    id: int
    kind: str
    """The job's class name: `Discover`, `Send`, `Acquire` and the rest."""
    label: str
    owner: str = ""
    """The session of the owner that issued it. Ids restart at 1 with every owner, so a
    client holding job 3 of a daemon that has since been restarted would otherwise be
    told about the new daemon's job 3; with this the new one refuses (`StaleHandle`).
    Empty means "whichever owner is asked", which is what an in-process caller that
    builds a handle by id gets (lab record, task 68)."""


class StaleHandle(ValueError):
    """A handle issued by a different owner, almost always one that has since stopped.
    Its progress went with that owner; nothing can be read for it any more."""


@dataclass(frozen=True, slots=True)
class Progress:
    """One event of one job, with the owner's sequence number for it."""

    seq: int
    event: Event


@dataclass(frozen=True, slots=True)
class OwnerStatus:
    """The owner as a whole, in one object: what `status()` answers."""

    program: str
    fake: bool
    console: ConsoleStatus
    boxes: tuple[str, ...] = ()
    """Box names a method can address, off the last scan."""
    ports: tuple[str, ...] = ()
    """Every port held open, the unaddressable ones included (lab record, task 62)."""
    running: Handle | None = None
    queued: tuple[Handle, ...] = ()
    stopping: bool = False
    snapshot: bool = False
    """Whether a send has left a snapshot for the next run to be stamped with."""
    holder: Holder | None = None
    """Who holds the instrument lock as far as this owner knows: itself, the owner that
    refused it, or None under `--fake`, which never takes it."""
    refused: str = ""
    """The sentence the lock refused this owner with, or empty while it holds the lock
    or needs none. Non-empty means every hardware job will be refused the same way."""


class Owner(Protocol):
    """Whoever owns the boxes and the console, as a front end sees it."""

    def submit(self, job: Job) -> Handle:
        """Queue a job and return at once. Jobs run one at a time, in order."""
        ...

    def events(self, handle: Handle, after: int = 0) -> list[Progress]:
        """That job's progress numbered after `after`, oldest first; empty if none.
        `StaleHandle` for a handle another owner issued."""
        ...

    def stop(self, reason: str = "stopped by the operator") -> None:
        """End the run in flight after its current repetition and fold."""
        ...

    def snapshot(self) -> Snapshot | None:
        """What the last send read off the boxes, which every run until the next is
        stamped with."""
        ...

    def status(self) -> OwnerStatus: ...

    def shutdown(self) -> None:
        """Stop, close every box and the console, and let go of the lock."""
        ...


# -- the events that used to be Worker's signals ------------------------------------
#
# Each is reported inside the job that caused it and carries the objects themselves in
# process: a `JobStarted` holds the very `Job` that was submitted, because the window
# asks `job is self._queue_job`, and a `Discovered` holds the open boxes, which
# `clockwork.owner.wire` leaves behind when it crosses a process boundary.


@dataclass(frozen=True, slots=True)
class JobStarted(Event):
    handle: Handle
    job: Job

    @property
    def text(self) -> str:
        return self.job.label


@dataclass(frozen=True, slots=True)
class JobFinished(Event):
    handle: Handle
    job: Job
    result: object = None
    """What the job produced: a `Discovery`, a `ConsoleStatus`, a `SendResult`, the list
    of `Run`s of a series, or a `{box: BoxState}` reading."""

    @property
    def text(self) -> str:
        return f"{self.job.label}: done"


@dataclass(frozen=True, slots=True)
class JobFailed(Event):
    handle: Handle
    job: Job
    message: str
    """The one sentence to show, never a traceback; the transcript has the rest."""

    @property
    def text(self) -> str:
        return f"{self.job.label} failed: {self.message}"


@dataclass(frozen=True, slots=True)
class Said(Event):
    """One line for the run log that did not come from the loop: what the owner is
    about to do, and what it found when it did."""

    line: str

    @property
    def text(self) -> str:
        return self.line


@dataclass(frozen=True, slots=True)
class ConsoleChanged(Event):
    status: ConsoleStatus

    @property
    def text(self) -> str:
        return self.status.text


@dataclass(frozen=True, slots=True)
class Discovered(Event):
    """What a scan answered, what was silent, what was not asked."""

    discovery: Discovery

    @property
    def text(self) -> str:
        return self.discovery.text


@dataclass(frozen=True, slots=True)
class RunDone(Event):
    """One run of a series, as it finishes rather than at the end of the series."""

    run: Run

    @property
    def text(self) -> str:
        return self.run.text


@dataclass(frozen=True, slots=True)
class BoxStateRead(Event):
    """One box's reading from a `ReadState` job, for the state panel."""

    box: str
    state: BoxState

    @property
    def text(self) -> str:
        return f"{self.box} read back"
