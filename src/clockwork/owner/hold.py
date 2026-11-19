"""A person's hold on a running series: what it is, when it may be asked for, how it is said.

The window revision's R6, R7 and B1 to B7 (lab record, task 167). A person at the window
may hold any client's series after its point in flight or after its repetition in flight,
resume it, and stop it; the hold is the owner's state, so it outlives the window that
asked for it and every client reads it in `OwnerStatus.hold`. This module is the part of
that both sides of the socket share: the `Hold` itself, the rule for when a pause can be
asked (`refusal`, which the owner applies and the window greys its buttons by, in the
same words), and the sentence an agent's tools refuse with while one stands (`held`).

**A pause exists only while a series runs** (B2). A lone acquisition has Stop alone: it
is one file, and the only place inside it to rest is the inner grain, which a series
offers too. So a request an agent runs as a single `acquire` cannot be held, and one it
runs as a `series` can.

**Two grains.** `point` rests the series once the point in flight has acquired its last
file: the console's chain is closed and no file is open, which is the state between any
two points. `repetition` rests it once the repetition in flight has ended, inside the
file, with the file and the chain open (`run_acquisition`'s `hold`); where that
repetition is its file's last, the series rests before the next file instead. The inner
grain is for `per_repetition` methods only: a `single_frame` method's repetitions are
inside the sequencer's table, and the loop has no boundary between them.

**Nothing here is a lock.** The daemon's socket is loopback with no login
(`docs/daemon-protocol.md`), and `pause` and `resume` are commands any local program
could send. Only the window sends them, and no MCP tool or command-line verb exists that
does: capability by absence (B6). What the tools do is refuse to submit, stop or stand
down while a hold stands (B7), which is their own rule and written in `clockwork.mcp`.

Qt-free.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from ..acq import BeamGuard
from ..method import Method
from .jobs import Job, SeriesJob

__all__ = ["BEAM_GUARD", "GRAINS", "POINT", "REPETITION", "Guarding", "Hold", "HoldRefused",
           "held", "now", "refusal"]

POINT = "point"
REPETITION = "repetition"
GRAINS = (POINT, REPETITION)
"""Where a pause rests its series: after the point in flight, or after the repetition."""


BEAM_GUARD = "the beam guard"
"""What a hold the guard set says in `Hold.by`: the owner's own, not a person's (B8)."""


class HoldRefused(ValueError):
    """A pause, a resume, a note or a guard change the owner declined; the message is
    one sentence."""


def now() -> str:
    """Local time, ISO 8601 to the second, as a run record stamps its entries."""
    return _dt.datetime.now().isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class Hold:
    """A pause on the series running: asked for, and from `since` on, in force.

    Between the two the series is still acquiring, up to the boundary its `grain` names;
    a client reads `since` empty as "will pause" and non-empty as "is paused"."""

    job: int
    """The daemon's number for the series job held."""
    grain: str
    """`point` or `repetition` (`GRAINS`)."""
    by: str
    """The initials of the person at the window who asked."""
    client: str = ""
    """The client the pause came through, as the daemon names it."""
    asked: str = ""
    """When it was asked for, local time."""
    since: str = ""
    """When the series came to rest, local time; empty until it has."""
    where: str = ""
    """Where it rests, in words: `after point 3 of 12 (planned 7)`; empty until it has."""
    beam_lost: str = ""
    """For the hold the beam guard set when it ended a file (`by` is `BEAM_GUARD`), the
    guard's sentence; empty for a person's (B8, lab record, task 168)."""

    @property
    def reached(self) -> bool:
        return bool(self.since)

    @property
    def guard(self) -> bool:
        """Whether this is the beam guard's hold, from which a person's Resume retakes
        the point it ended."""
        return self.by == BEAM_GUARD

    @property
    def text(self) -> str:
        if self.guard:
            lost = f" ({self.beam_lost})" if self.beam_lost else ""
            return (f"paused by the beam guard since {self.since or self.asked}, "
                    f"{self.where or 'after the file it ended'}{lost}; a person's Resume "
                    "acquires that point again from its start")
        who = f"{self.by} at the window" if self.by else "a person at the window"
        if self.reached:
            return f"paused by {who} since {self.since}, {self.where}"
        return (f"pause asked by {who} at {self.asked}: after this "
                f"{'repetition' if self.grain == REPETITION else 'point'}")


@dataclass(frozen=True, slots=True)
class Guarding:
    """The beam guard the owner holds, and who armed it: `OwnerStatus.guard`.

    The owner's and not a front end's (R9, B8; lab record, task 168): armed, changed
    and disarmed only by a person at the window, on whatever is running or with nothing
    running, and applied to every acquisition the owner makes until it is disarmed or
    the owner ends. A trip ends the file in flight as failed; inside a series or a queue
    it then holds the series for a person (`Hold.guard`), and a lone acquisition simply
    ends, since it has nothing to hold (B2)."""

    guard: BeamGuard
    by: str
    """The initials of the person who armed it, or last changed it."""
    client: str = ""
    since: str = ""
    """When it was armed or last changed, local time."""

    @property
    def text(self) -> str:
        return f"armed by {self.by} at {self.since}: {self.guard.text}"


def refusal(job: Job | None, grain: str, *, number: int = 0,
            method: Method | None = None, stopping: bool = False,
            hold: Hold | None = None) -> str:
    """Why the job running cannot be paused at `grain` now, or empty if it can.

    `job` is the job running, None for none; `number` the place, from 1, of the point in
    flight among a series' points as acquired, 0 before the first; `method` that point's.
    A series can be paused from its first point's start on: before that the owner is
    still beginning its record, and a pause would have no point to rest after.
    One function for the owner, which refuses the command with the sentence, and for the
    window, which greys the button with it."""
    if grain not in GRAINS:
        return f"a pause is after this {POINT} or after this {REPETITION}, not {grain!r}"
    if job is None:
        return "nothing is running, so there is nothing to pause"
    if not isinstance(job, SeriesJob):
        return ("only a series or a queue can be paused: this is a single job, and Stop "
                "is what ends it")
    if stopping:
        return "a stop has been asked for, so the series is ending and cannot be paused"
    if hold is not None and hold.reached:
        return f"the series is already {hold.text}"
    if number < 1:
        return ("the series has not started its first point yet; it can be paused "
                "once it has")
    if grain == POINT and number >= len(job.points):
        return ("the series is on its last point, so there is no point after it to "
                "pause before; Stop is what ends it")
    if (grain == REPETITION and method is not None
            and method.acquisition.repetition_mode != "per_repetition"):
        return ("this point's method is single_frame: its repetitions are inside the "
                "sequencer's table, so its file has no place to pause between them. "
                "Pause after this point instead")
    return ""


def held(hold: Hold) -> str:
    """The one sentence a tool refuses with while `hold` stands (B7)."""
    if hold.guard:
        return (f"job {hold.job} is {hold.text}. Only a person at the window resumes or "
                "stops it; until then this route reads and notes only, and nothing here "
                "resumes, retakes or works around the guard")
    return (f"job {hold.job} is {hold.text}. Only a person at the window resumes or "
            "stops it; until then this route reads and notes only, and nothing here "
            "pauses, resumes or works around a pause")
