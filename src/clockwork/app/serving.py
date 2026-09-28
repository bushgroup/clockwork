"""Starting `clockwork serve` from the window, and saying why it did not start.

The window is a client of the daemon (lab record, task 77): when it opens and nothing
answers on the command socket it starts one itself, the installed executable with the
subcommand, or this checkout's interpreter running the same entry point. What the
daemon needs from the window -- the output directory, the library, the console, the
kept-files folder and the window's own error log -- goes on its command line, since the
daemon reads no window setting of its own.

**Started with no console of its own and outside the window's job.** A daemon the window
started is stopped by the protocol's `shutdown`, never by a console signal: under the
installed, windowed executable a shell does not own it (lab record, task 75), and a
console window a trainee could close would kill it without a shutdown. So it gets no
window (`CREATE_NO_WINDOW`), a process group of its own, so that a Ctrl-C meant for
whatever started the window does not reach it, and it breaks away from any job the
window is in, so that closing the window cannot end it; its log is `serve.log` alone.
`--detached` on its command line says so to the daemon, which would otherwise attach to
its parent's console: a `clockwork-cli.exe` in a shortcut's `cmd` window has one, and a
daemon it started printed into it after the verb had ended (lab record, task 96).

**The command line starts one too, and stops one.** `clockwork warm-up` starts a daemon
the same way when none answers and leaves it running for the day; `clockwork stand-down`
shuts it down in the evening and waits until it is gone -- its process ended, the
instrument lock free and the console's command port closed -- before it says so (lab
record, task 95). `connect_or_start` and `wait_gone` are both, shared with nothing Qt.

Qt-free, like `launch.py` and `errors.py`: a test asks what the command line would be
without building a window.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable

from ..owner.daemon import log_path

__all__ = ["START_TIMEOUT_S", "connect_or_start", "process_running", "serve_command",
           "start_serve", "wait_gone", "why_serve_stopped", "window_origin"]

START_TIMEOUT_S = 45.0
"""How long a started daemon has to answer `hello`: the window's own wait."""

GONE_TIMEOUT_S = 90.0
"""How long a daemon that was asked to shut down has to be gone. A run in flight ends
after its repetition and fold, and the console takes a few seconds to stop."""

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

WINDOWED_EXE = "clockwork.exe"
"""The installed build's windowed executable, which every daemon runs as."""

CLI_EXE = "clockwork-cli.exe"
"""Its console-subsystem twin, which the desktop shortcuts run and no daemon is."""

_ENTRY = "import sys; from clockwork.app import main; sys.exit(main(sys.argv[1:]))"
"""What a checkout runs: the frozen executable's own entry point, from its interpreter."""


def window_origin() -> str:
    """How this window names itself in the jobs it submits (`Handle.origin`)."""
    return f"the clockwork window (pid {os.getpid()})"


def serve_command(*, output: str = "", library: str = "", console: str = "",
                  kept: str = "", errors_log: str = "") -> list[str]:
    """The command line that starts a daemon with the window's settings, detached.

    Frozen, always the windowed `clockwork.exe`, even from the console-subsystem
    `clockwork-cli.exe` beside it that the desktop shortcuts run: numba stamps a frozen
    program's kernel cache with its executable, and a daemon run from the other one
    would fold with no seed and rewrite the cache under its own stamp."""
    if getattr(sys, "frozen", False):
        executable = sys.executable
        if os.path.basename(executable).lower() == CLI_EXE:
            windowed = os.path.join(os.path.dirname(executable), WINDOWED_EXE)
            executable = windowed if os.path.isfile(windowed) else executable
        command = [executable, "serve"]
    else:
        command = [sys.executable, "-c", _ENTRY, "serve"]
    command.append("--detached")
    for flag, value in (("--output", output), ("--library", library),
                        ("--console", console), ("--kept", kept),
                        ("--errors-log", errors_log)):
        if value:
            command += [flag, value]
    return command


def start_serve(command: list[str]) -> subprocess.Popen:
    """Start the daemon detached from this process and return at once.

    Nothing waits on it here: the caller asks `hello` until it answers or the process
    has ended. A window in a job that forbids breaking away (some launchers put their
    children in one) starts the daemon inside that job instead, which a window closed
    normally still leaves running.
    """
    options: dict[str, object] = {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL, "close_fds": True,
        "cwd": os.path.expanduser("~"),
    }
    if os.name != "nt":
        return subprocess.Popen(command, start_new_session=True, **options)  # noqa: S603
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(command, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB,  # noqa: S603
                                **options)
    except OSError:
        return subprocess.Popen(command, creationflags=flags, **options)  # noqa: S603


def why_serve_stopped(code: int | None, log: str | None = None) -> str:
    """The sentence for a daemon that ended before it answered: its log's last line,
    which is the lock's sentence when another program holds the instrument."""
    path = log or log_path()
    last = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = [line.rstrip() for line in handle.readlines()[-40:] if line.strip()]
    except OSError:
        lines = []
    if lines:
        last = lines[-1]
        # `2026-09-26 14:02:11 ` in front of every line the daemon writes.
        if len(last) > 20 and last[4] == "-" and last[10] == " " and last[13] == ":":
            last = last[20:]
    exited = f"exit code {code}" if code is not None else "it did not answer"
    if last:
        return f"clockwork serve did not start ({exited}): {last}"
    return f"clockwork serve did not start ({exited}); its log is {path}"


def connect_or_start(endpoint: str, command: list[str], *,
                     timeout: float = START_TIMEOUT_S,
                     say: Callable[[str], None] | None = None,
                     launch: Callable[[list[str]], subprocess.Popen] | None = None,
                     ) -> tuple[object, bool]:
    """`hello` from the daemon at `endpoint`, starting `command` first if none answers.

    Answers the `Hello` and whether this call started the daemon. `DaemonError` with
    one sentence for a daemon that would not start, or that answered and is not one this
    client can talk to."""
    from ..owner.remote import DaemonError, DaemonUnavailable, RemoteOwner

    probe = RemoteOwner(endpoint, timeout=2.0)
    try:
        try:
            return probe.hello(), False
        except DaemonUnavailable:
            pass
    finally:
        probe.close()
    if say is not None:
        say("no clockwork serve is running on this PC; starting one")
    try:
        process = (launch or start_serve)(command)
    except OSError as exc:
        raise DaemonError(f"clockwork serve could not be started: {exc}") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise DaemonError(why_serve_stopped(code))
        probe = RemoteOwner(endpoint, timeout=2.0)
        try:
            return probe.hello(), True
        except DaemonUnavailable:
            continue
        finally:
            probe.close()
    raise DaemonError(why_serve_stopped(None))


def process_running(pid: int) -> bool:
    """Whether process `pid` is still running. A process this one cannot open to ask is
    counted as running, since it exists."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes

    query_limited_information, still_active = 0x1000, 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        # 87, ERROR_INVALID_PARAMETER: there is no such process.
        return ctypes.GetLastError() != 87
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def wait_gone(endpoint: str, *, pid: int, fake: bool,
              timeout: float = GONE_TIMEOUT_S) -> dict[str, bool]:
    """Wait for a daemon that has been asked to shut down to be gone, and say how far it
    got: `answering` false once `hello` goes unanswered, `process` false once its process
    has ended, `lock` false once the instrument lock names it no longer, and `console`
    false once nothing listens on the console's command port.

    A daemon in this very process -- a test's -- is not waited on to exit, and under
    `--fake` there is no lock and no real console to look for."""
    from ..acq.process import listening_on
    from ..owner.lock import default_path, read_holder
    from ..owner.remote import DaemonUnavailable, RemoteOwner

    deadline = time.monotonic() + timeout
    state = {"answering": True, "process": pid != os.getpid(), "lock": not fake,
             "console": not fake}
    while time.monotonic() < deadline:
        if state["answering"]:
            probe = RemoteOwner(endpoint, timeout=1.0)
            try:
                probe.hello()
            except DaemonUnavailable:
                state["answering"] = False
            finally:
                probe.close()
        if not state["answering"]:
            if state["process"]:
                state["process"] = process_running(pid)
            if state["lock"]:
                holder = read_holder(default_path())
                state["lock"] = holder is not None and holder.pid == pid
            if state["console"]:
                state["console"] = listening_on() is not None
        if not any(state.values()):
            break
        time.sleep(0.25)
    return state
