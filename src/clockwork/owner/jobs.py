"""What goes into the owner of the hardware, and what comes back out of it.

Moved whole out of `clockwork.app.worker` when the owner was taken out of `Worker`, and
still importable from there under the same names. Every class here is a frozen
dataclass of plain data, which is the whole of what lets `clockwork.owner.wire` carry it
across a process boundary: a `Method` or an `Instrument` inside a job travels as its
document's text, and nothing here holds a port, a socket or a widget.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ..acq import Snapshot
from ..instrument import UNCALIBRATED, Instrument
from ..method import Method

__all__ = [
    "Acquire",
    "ConsoleStatus",
    "Discover",
    "Job",
    "ReadState",
    "RestartConsole",
    "Send",
    "SendResult",
    "StartConsole",
    "matches_wire",
    "wire_fingerprint",
]


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

    Each replicate does the same again into a file of its own, with **the same
    `Snapshot`** the first run was stamped with -- a replicate re-sends neither `setup`
    nor `load`, so the boxes hold what the first run left them holding and reading them
    again would record one measurement twice.

    **Every run in the series re-arms the rack before its own first frame**, the
    replicates and the first alike, which is `run_acquisition`'s own doing and not this
    job's: a run that sends the boxes nothing -- a replicate, or a second press of
    Acquire on panes the boxes already match -- would otherwise begin behind whatever
    the previous run's table left on the enable line (lab record, task 63).

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
