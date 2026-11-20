"""The owner protocol: what a front end may ask of whoever owns the hardware.

Five calls and a shutdown. `submit` a job and get a `Handle`; read that job's progress
with `events(handle, after)`, and where it has got to with `summary(handle)`, whose size
does not grow with the job; `stop` the run in flight; ask for the `snapshot` the next
run will be stamped with and the `status` of the whole owner. Three more are a person's
at the window and nobody else's: `pause` and `resume` a running series, and `note`
(`clockwork.owner.hold`). A front end that holds an
`Owner` holds nothing else -- no `Box`, no `Console`, no thread -- which is what lets
the same front end drive an owner in its own process (`LocalOwner`) or one in another
(`RemoteOwner`, the client of `clockwork serve`) without knowing which.

**Everything that crosses is plain data.** Jobs are `clockwork.owner.jobs`; progress is
the loop's own `Event` family plus those below, which say what `Worker`'s signals
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

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Protocol

from ..acq import BatchSeen, BeamGuard, Event, Run, Snapshot
from ..mips import BoxState, Discovery
from .hold import POINT, Guarding, Hold
from .jobs import Armed, ConsoleStatus, Job
from .lock import Holder

__all__ = [
    "BoxStateRead",
    "ConsoleChanged",
    "Discovered",
    "GuardChanged",
    "Handle",
    "Held",
    "JobFailed",
    "JobFinished",
    "JobStarted",
    "JobSummary",
    "LogsOpened",
    "Noted",
    "Owner",
    "OwnerStatus",
    "PauseAsked",
    "PointStarted",
    "Progress",
    "RecordBegun",
    "Resumed",
    "RunDone",
    "Said",
    "StaleHandle",
    "StopAsked",
    "summarize",
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
    origin: str = ""
    """A few words naming the client that submitted it (`the clockwork window (pid
    5120)`, `Claude, through the MCP server`), carried unexamined, so that every other
    client can say whose job it is; empty for the owner's own jobs and a caller that
    named none (lab record, task 77)."""


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
    armed: Armed | None = None
    """What the last send that finished left the boxes holding, or None before one and
    while one is under way; `acquire` from any client is checked against it."""
    holder: Holder | None = None
    """Who holds the instrument lock as far as this owner knows: itself, the owner that
    refused it, or None under `--fake`, which never takes it."""
    refused: str = ""
    """The sentence the lock refused this owner with, or empty while it holds the lock
    or needs none. Non-empty means every hardware job will be refused the same way."""
    issued: int = 0
    """The id of the newest handle this owner has issued, 0 before the first. Ids are
    dense from 1, so with this a client can follow every job, whoever submitted it."""
    hold: Hold | None = None
    """A person's pause on the series running: asked for, and once `hold.since` is set,
    in force; None for none (`clockwork.owner.hold`). While it is not None an agent's
    tools read and note only."""
    guard: Guarding | None = None
    """The beam guard the owner holds and who armed it, or None for none: every
    acquisition watches it (lab record, task 168)."""


class Owner(Protocol):
    """Whoever owns the boxes and the console, as a front end sees it."""

    def submit(self, job: Job) -> Handle:
        """Queue a job and return at once. Jobs run one at a time, in order."""
        ...

    def events(self, handle: Handle, after: int = 0) -> list[Progress]:
        """That job's progress numbered after `after`, oldest first; empty if none.
        `StaleHandle` for a handle another owner issued."""
        ...

    def summary(self, handle: Handle) -> JobSummary:
        """Where that job has got to (`JobSummary`), empty for a job whose progress was
        let go. `StaleHandle` for a handle another owner issued."""
        ...

    def stop(self, reason: str = "stopped by the operator", *, by: str = "",
             client: str = "") -> None:
        """End the run in flight after its current repetition and fold; one that is
        paused ends where it rests. `by` is the person who asked, `client` the client
        it came through, for the run record's `interventions`."""
        ...

    def pause(self, grain: str = POINT, *, by: str, client: str = "") -> Hold:
        """Hold the series running after its point or its repetition in flight.
        `HoldRefused`, with one sentence, where nothing running can be held there."""
        ...

    def resume(self, *, by: str, client: str = "") -> None:
        """Lift the pause: the series goes on. `HoldRefused` where nothing is paused."""
        ...

    def note(self, text: str, *, by: str, client: str = "") -> str:
        """Keep a person's note: in the run record of the job running, or with nothing
        running in the day record. Answers where it went, in a few words."""
        ...

    def guard(self, guard: BeamGuard | None, *, by: str, client: str = "") -> str:
        """Arm, change or disarm (None) the beam guard every acquisition watches, as a
        person at the window: an intervention in the running job's record, or a line in
        the day record with nothing running. Answers where it was recorded.
        `HoldRefused`, with one sentence, for no initials."""
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
class LogsOpened(Event):
    """A piece of work under a stem has opened its transcript and send log.

    Reported before the first string or command goes anywhere, so a front end knows
    which run a problem belongs to from the start of sending and not only from
    `RunBegun`, which a run stopped before its first frame never reaches: a refused
    `acquire`, a refused pusher period, a refused method, a box that did not answer.
    That is what lets Report name the folder the failure was kept in (lab record,
    task 104). `directory` is where the run's files go, `transcript` the wire
    transcript's path.
    """

    directory: str
    stem: str
    transcript: str

    @property
    def text(self) -> str:
        return f"logging {self.stem} to {os.path.basename(self.transcript)}"


@dataclass(frozen=True, slots=True)
class RecordBegun(Event):
    """The owner has begun the run record of the job it is starting, a window run's
    (`clockwork.owner.keeper.begin`, lab record, task 165): the request id it minted,
    which every file of the job stamps as `ClockworkSeriesId`, and the record's path.

    An event of its own, after `JobStarted`, rather than the id written into the job
    that `JobStarted` carries: a front end knows its own jobs by the object it submitted,
    and a job replaced on the way in was a job the window's queue never saw come back."""

    request_id: str
    path: str

    @property
    def text(self) -> str:
        return f"run record {os.path.basename(self.path)}, request {self.request_id}"


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
class PointStarted(Event):
    """A `SeriesJob` beginning one of its points, before it sends or acquires anything."""

    index: int
    """The point's planned index."""
    position: int
    """The place in the order acquired its first file will take."""
    number: int
    """Its place among the series' points as acquired, from 1."""
    points: int
    """How many points the series has."""
    knobs: Mapping[str, float]
    replicates: int = 1
    reference: str = ""
    sends: bool = True
    """False for a point the boxes already hold from the point before."""
    retake: int = 0
    """0 for a point's first attempt; 1 and on for each time it is acquired again from
    its start after the beam guard ended it and a person resumed (B8, task 168)."""
    name: str = ""
    """A queue row's name, as `PlannedPoint.name`; empty for a plan's point."""

    @property
    def text(self) -> str:
        what = (self.name if self.name else f"the {self.reference} reference"
                if self.reference
                else ", ".join(f"{name} {value:g}" for name, value in self.knobs.items()))
        files = f"{self.replicates} file{'s' if self.replicates != 1 else ''}"
        again = f", acquired again (retake {self.retake})" if self.retake else ""
        return (f"point {self.number} of {self.points} (planned {self.index}): "
                f"{what or 'the defaults'}, {files} from position {self.position}{again}"
                + ("" if self.sends else ", the boxes already holding it"))


@dataclass(frozen=True, slots=True)
class PauseAsked(Event):
    """A person at the window asked for the series running to be held (`hold`): it goes
    on to the boundary the hold's grain names, and `Held` says when it is there."""

    hold: Hold

    @property
    def text(self) -> str:
        return self.hold.text


@dataclass(frozen=True, slots=True)
class Held(Event):
    """The series has come to rest at a person's pause, and waits for a person."""

    hold: Hold

    @property
    def text(self) -> str:
        return (f"{self.hold.text}; it waits for a person at the window to resume or "
                "stop it")


@dataclass(frozen=True, slots=True)
class Resumed(Event):
    """A person lifted the pause. `seconds` is how long the series rested, 0 for a pause
    withdrawn before it was reached."""

    hold: Hold
    by: str
    client: str = ""
    seconds: float = 0.0

    @property
    def text(self) -> str:
        who = f"{self.by} at the window" if self.by else "a person at the window"
        if not self.hold.reached:
            return f"the pause asked for was withdrawn by {who} before it was reached"
        return f"resumed by {who} after {self.seconds:.0f} s paused"


@dataclass(frozen=True, slots=True)
class StopAsked(Event):
    """A stop was asked for, and by whom: `by` a person's initials or empty, `client`
    the client it came through. `while_paused` for a stop that ended a paused series."""

    reason: str
    by: str = ""
    client: str = ""
    while_paused: bool = False

    @property
    def text(self) -> str:
        who = (f"{self.by} at the window" if self.by else self.client or "a client")
        return (f"stop asked by {who}{' while paused' if self.while_paused else ''}: "
                f"{self.reason}")


@dataclass(frozen=True, slots=True)
class Noted(Event):
    """A note a person typed at the window while this job ran, kept in its run record.

    **A record, not a command** (the window revision's C1): it reaches every client
    following the job, an agent's `progress` among them, as data. Nothing acts on it."""

    note: str
    by: str
    client: str = ""

    @property
    def text(self) -> str:
        return f"note from {self.by} at the window, kept with the data: {self.note}"


@dataclass(frozen=True, slots=True)
class GuardChanged(Event):
    """A person at the window armed, changed or disarmed the owner's beam guard while
    this job ran; `guarding` is the guard now, None for none (lab record, task 168)."""

    guarding: Guarding | None
    by: str
    client: str = ""

    @property
    def text(self) -> str:
        if self.guarding is None:
            return f"the beam guard is off, turned off by {self.by} at the window"
        return f"the beam guard is {self.guarding.text}"


@dataclass(frozen=True, slots=True)
class BoxStateRead(Event):
    """One box's reading from a `ReadState` job, for the state panel."""

    box: str
    state: BoxState

    @property
    def text(self) -> str:
        return f"{self.box} read back"


# -- where a job has got to ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobSummary:
    """Where one job has got to, in a form whose size does not grow with the job.

    What a follower otherwise read off the job's whole history on every call: the job as
    it started, the runs it has finished, the scans the frame in flight has published,
    the point a series is on, and the event that ended it. Read that way, one `progress`
    from a new process cost the daemon its whole history, encoded on the loop that also
    serves the acquisition, and a follower that asked every few seconds dropped
    repetitions for the gate guard and starved the window's `status` (lab #25, lab #27;
    lab record, task 170). A follower now reads this and the events after its last
    number, and never needs `events` from 0.

    The owner keeps it as each event arrives, so `runs_done` counts every run even after
    the oldest entries of a long job's history have been let go.
    """

    handle: Handle
    last: int = 0
    """The number of the job's newest event, 0 before its first."""
    job: Job | None = None
    """The job as its `JobStarted` carried it; None until it starts."""
    runs_done: int = 0
    """How many `RunDone` events the job has reported."""
    scans_so_far: int = 0
    """The newest `BatchSeen.scans_so_far`: what the console frame in flight has
    published, 0 before the first batch."""
    point: PointStarted | None = None
    """The newest `PointStarted` of a series, None for any other job."""
    ended: Event | None = None
    """The `JobFinished` or `JobFailed` that ended the job, None while it runs."""

    def after(self, seq: int, event: Event) -> JobSummary:
        """This summary with event number `seq` taken in."""
        if isinstance(event, BatchSeen):
            return replace(self, last=seq, scans_so_far=event.scans_so_far)
        if isinstance(event, JobStarted):
            return replace(self, last=seq, job=event.job)
        if isinstance(event, RunDone):
            return replace(self, last=seq, runs_done=self.runs_done + 1)
        if isinstance(event, PointStarted):
            return replace(self, last=seq, point=event)
        if isinstance(event, (JobFinished, JobFailed)):
            return replace(self, last=seq, ended=event)
        return replace(self, last=seq)


def summarize(handle: Handle, entries: Iterable[Progress]) -> JobSummary:
    """A `JobSummary` worked out from a job's history, for an owner that keeps none: a
    daemon from before `summary`. Undercounts `runs_done` where the history has let its
    oldest entries go, which a kept summary does not."""
    summary = JobSummary(handle=handle)
    for entry in entries:
        summary = summary.after(entry.seq, entry.event)
    return summary
