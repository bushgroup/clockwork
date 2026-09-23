"""The owner of the hardware: the one party that holds the boxes and the console.

Between the wire and the front ends. `clockwork.acq` and `clockwork.mips` are the wire;
`clockwork.app` is Qt; this package is what a front end talks to instead of either, and
it never imports Qt (enforced by `tests/test_architecture.py` and
`tools/check_public.py`).

    interface.py  the `Owner` protocol, `Handle`, `Progress`, `OwnerStatus`, and the
                  events that say what a job did outside the loop
    jobs.py       the jobs that go in and the results that come back
    local.py      `LocalOwner`: the queue, the thread, every wire call, in this process
    lock.py       the instrument lock: one owner per user, refused before any port
    wire.py       every job, event and result to and from JSON-ready data

`send_phases` and `run_acquisition` stay the only two entry points onto the wire, and
`LocalOwner` is the only caller of either outside a bench script. The window's `Worker`
is an adapter over a `LocalOwner`; the daemon's client will be a second implementation
of the same protocol (lab record, task 68).
"""

from __future__ import annotations

from .interface import (
    BoxStateRead,
    ConsoleChanged,
    Discovered,
    Handle,
    JobFailed,
    JobFinished,
    JobStarted,
    Owner,
    OwnerStatus,
    Progress,
    RunDone,
    Said,
)
from .jobs import (
    Acquire,
    ConsoleStatus,
    Discover,
    Job,
    ReadState,
    RestartConsole,
    Send,
    SendResult,
    StartConsole,
    matches_wire,
    wire_fingerprint,
)
from .local import FAKE_FRAME_HOLD_S, LocalOwner, fake_rack, sentence
from .lock import Holder, InstrumentLock, LockError, LockHeld

__all__ = [
    "FAKE_FRAME_HOLD_S",
    "Acquire",
    "BoxStateRead",
    "ConsoleChanged",
    "ConsoleStatus",
    "Discover",
    "Discovered",
    "Handle",
    "Holder",
    "InstrumentLock",
    "Job",
    "JobFailed",
    "JobFinished",
    "JobStarted",
    "LocalOwner",
    "LockError",
    "LockHeld",
    "Owner",
    "OwnerStatus",
    "Progress",
    "ReadState",
    "RestartConsole",
    "RunDone",
    "Said",
    "Send",
    "SendResult",
    "StartConsole",
    "fake_rack",
    "matches_wire",
    "sentence",
    "wire_fingerprint",
]
