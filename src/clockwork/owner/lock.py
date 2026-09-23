r"""One owner of the instrument per user, enforced before any port is opened.

One COM port per box and one console: exactly one process may own the hardware.
Windows already refuses a second `CreateFile` on a held COM port, but that refusal
surfaces as a box that will not open or will not answer -- `Silent(could_not_open=True)`,
the whole story of a rescan that lost a box (lab record, task 62) -- and never as "the
window owns the instrument". This lock exists so that the second would-be owner fails
*before* it touches a port, in one sentence that names the first. **The operating
system's own COM exclusivity stays the backstop**: a program that does not take this
lock (MIPS_QT6, a bench script) is refused by the port as it always was.

The file is `%LOCALAPPDATA%\clockwork\instrument.lock` (`$CLOCKWORK_LOCK` names another,
which is how the tests keep off the real one). Per user rather than per machine (lab
record, task 67): two people logged into one instrument PC at once is not a case the lab
has, and a file under `%PROGRAMDATA%` created by one user is not writable by the next
without an installer-time ACL.

**The lock is an operating-system byte-range lock on that file, not the file's
existence.** The holder locks one byte far past the end of the file and writes its pid,
program and start time at the front, where a refused owner can read them without
touching the locked range. The OS releases the lock with the process however the process
ends, so a crashed window leaves nothing to clean up, and a pid Windows has since reused
for something else cannot keep the instrument locked -- the two failures a lock that
meant "the file exists and its pid is alive" would have. The file itself is never
deleted: deleting a lock file that another process may have open is the classic way two
processes both end up holding a lock, and an empty file costs nothing. The record is
cleared on release, so a file with a holder written in it and no lock on it is only ever
a process that died.

`msvcrt.locking` on Windows, `fcntl.flock` elsewhere. Both conflict between two handles
in one process as well as between two processes, so two owners built by one test refuse
each other too.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from dataclasses import dataclass

__all__ = [
    "LOCK_ENV",
    "LOCK_NAME",
    "Holder",
    "InstrumentLock",
    "LockError",
    "LockHeld",
    "default_path",
    "read_holder",
]

LOCK_ENV = "CLOCKWORK_LOCK"
"""An environment variable naming the lock file in place of the per-user default."""

LOCK_NAME = "instrument.lock"

_LOCK_OFFSET = 1 << 20
"""Where the locked byte is: past anything the holder record could grow to, so reading
the record never meets the lock. Windows' byte-range locks are mandatory, and a lock
over the record would make the refusal unable to say who refused it. Locking beyond the
end of a file is allowed on both platforms."""


def default_path() -> str:
    r"""`$CLOCKWORK_LOCK`, or `%LOCALAPPDATA%\clockwork\instrument.lock` (`~/.cache` off
    Windows, as the errors log does)."""
    override = os.environ.get(LOCK_ENV)
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return os.path.join(base, "clockwork", LOCK_NAME)


@dataclass(frozen=True, slots=True)
class Holder:
    """Who holds the instrument: what the holder wrote at the front of the lock file."""

    pid: int
    program: str
    started: str
    """Local time the lock was taken, to the second, ISO order."""

    @property
    def text(self) -> str:
        return f"{self.program} (pid {self.pid}, since {self.started.replace('T', ' ')})"


class LockError(RuntimeError):
    """The instrument lock could not be taken. The message is the sentence to show."""

    def __init__(self, message: str, *, path: str, holder: Holder | None = None) -> None:
        super().__init__(message)
        self.path = path
        self.holder = holder


class LockHeld(LockError):
    """Another owner holds the instrument. `holder` names it where it has said who it is."""


def read_holder(path: str) -> Holder | None:
    """The holder record at the front of `path`, or None where there is none to read.

    None covers a missing file, a free lock whose record was cleared on release, and the
    moment between another process taking the lock and writing its record.
    """
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read(4096).decode("utf-8") or "null")
        return Holder(pid=int(data["pid"]), program=str(data["program"]),
                      started=str(data["started"]))
    except (OSError, ValueError, TypeError, KeyError):
        return None


class InstrumentLock:
    """The lock, held from `acquire` until `release` or the end of the process.

    `program` is what a refused owner is told holds the instrument ("clockwork window",
    "clockwork serve"). Not reentrant: a second `acquire` on a held lock is a no-op that
    returns the same holder.
    """

    def __init__(self, program: str, path: str | None = None) -> None:
        self.program = program
        self.path = path or default_path()
        self.holder: Holder | None = None
        """This process's own record while it holds the lock; None otherwise."""
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> Holder:
        """Take the lock, or raise `LockHeld` naming whoever has it. Never blocks."""
        if self.holder is not None:
            return self.holder
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0),
                         0o644)
        except OSError as exc:
            raise LockError(
                f"could not open the instrument lock at {self.path} ({exc}), so this "
                "program cannot tell whether another one owns the hardware and will not "
                "touch it", path=self.path) from exc
        try:
            _lock(fd)
        except OSError:
            os.close(fd)
            holder = read_holder(self.path)
            if holder is None:
                message = (
                    f"the instrument is already owned by another program that has not "
                    f"said which it is (the lock at {self.path} is held); close the "
                    "other clockwork before using the hardware from here")
            else:
                message = (
                    f"the instrument is already owned by {holder.text}; close that one "
                    "before using the hardware from here")
            raise LockHeld(message, path=self.path, holder=holder) from None
        holder = Holder(pid=os.getpid(), program=self.program,
                        started=_dt.datetime.now().isoformat(timespec="seconds"))
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, json.dumps({"pid": holder.pid, "program": holder.program,
                                     "started": holder.started}).encode("utf-8"))
        except OSError:
            # Holding the lock is what matters; a record that could not be written makes
            # a refusal say less, and is not a reason to give the instrument up.
            pass
        self._fd = fd
        self.holder = holder
        return holder

    def release(self) -> None:
        """Clear the record and let go. Idempotent; the process ending does the same."""
        fd, self._fd, self.holder = self._fd, None, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        try:
            _unlock(fd)
        except OSError:
            pass
        os.close(fd)

    def __enter__(self) -> InstrumentLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
