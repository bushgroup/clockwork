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

Qt-free, like `launch.py` and `errors.py`: a test asks what the command line would be
without building a window.
"""

from __future__ import annotations

import os
import subprocess
import sys

from ..owner.daemon import log_path

__all__ = ["serve_command", "start_serve", "why_serve_stopped", "window_origin"]

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

_ENTRY = "import sys; from clockwork.app import main; sys.exit(main(sys.argv[1:]))"
"""What a checkout runs: the frozen executable's own entry point, from its interpreter."""


def window_origin() -> str:
    """How this window names itself in the jobs it submits (`Handle.origin`)."""
    return f"the clockwork window (pid {os.getpid()})"


def serve_command(*, output: str = "", library: str = "", console: str = "",
                  kept: str = "", errors_log: str = "") -> list[str]:
    """The command line that starts a daemon with the window's settings."""
    if getattr(sys, "frozen", False):
        command = [sys.executable, "serve"]
    else:
        command = [sys.executable, "-c", _ENTRY, "serve"]
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
