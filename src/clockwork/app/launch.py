"""Handing a file to the program that should open it: mainspring, and a text editor.

Two buttons use this. **Open in mainspring** on the file a run just wrote, and **Open
the log** on the transcript and the send log beside it. Both are the same problem --
this process knows a path and wants another program to have it -- and both are solved
the same way, by asking the shell first and falling back to something explicit.

**The association first, a configured path second** (Matt, 2026-09-17). mainspring's
own installer registers itself as the handler for `.uimf`, so on an instrument PC where
both installers have run, opening the file *is* opening mainspring and there is nothing
to configure. Where that fails -- a dev clone with mainspring only as a checkout, or a
machine where something else claimed the extension -- the window's remembered
mainspring path is tried, and the failure says which of the two ways was tried and what
it said. Guessing a third way, or searching the disk for an executable, would make the
button's behaviour depend on what happens to be installed.

Qt-free: `os.startfile` is the Windows shell's own "open this the way a double-click
would", and the two fallbacks are `open` and `xdg-open`, which are the same idea. The
window shows what comes back; nothing here raises.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

__all__ = ["OpenResult", "open_path", "open_with"]


class OpenResult:
    """Whether a file was handed off, and what to tell a trainee if it was not.

    A small class rather than a bare bool because the failure is the useful half:
    "nothing on this machine opens .uimf files" and "the mainspring you configured is
    not there any more" lead to different remedies, and a button that only went grey
    would name neither.
    """

    __slots__ = ("opened", "how", "problem")

    def __init__(self, opened: bool, how: str = "", problem: str = "") -> None:
        self.opened = opened
        self.how = how
        self.problem = problem

    def __bool__(self) -> bool:
        return self.opened

    def __repr__(self) -> str:
        return (f"OpenResult({self.opened}, how={self.how!r}, "
                f"problem={self.problem!r})")


def open_path(path: str) -> OpenResult:
    """Open a file the way a double-click would, through the shell's association.

    On Windows that is `os.startfile`, which is `ShellExecute` and so honours whatever
    handler is registered; elsewhere it is `open` or `xdg-open`, which is the same
    idea on the platforms clockwork is not tested on but should not break on.
    """
    if not os.path.isfile(path):
        return OpenResult(False, problem=f"there is no file at {path}")
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 -- the shell's own association, by design
            return OpenResult(True, how="the file association")
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, path])  # noqa: S603
        return OpenResult(True, how=opener)
    except OSError as exc:
        return OpenResult(False, how="the file association", problem=str(exc))


def open_with(command: str, path: str) -> OpenResult:
    """Run a configured program on a file.

    `command` may carry arguments -- `uv run mainspring` is a real answer on a
    development machine -- so it is split the way a shell would and the path is
    appended as one argument, never interpolated into a string. Nothing here goes
    through a shell.
    """
    if not command.strip():
        return OpenResult(False, problem="no program is configured")
    if not os.path.isfile(path):
        return OpenResult(False, problem=f"there is no file at {path}")
    parts = shlex.split(command, posix=False) if sys.platform == "win32" \
        else shlex.split(command)
    parts = [part.strip('"') for part in parts]
    try:
        subprocess.Popen([*parts, path])  # noqa: S603
    except OSError as exc:
        return OpenResult(False, how=parts[0], problem=str(exc))
    return OpenResult(True, how=parts[0])


def open_data_file(path: str, configured: str = "") -> OpenResult:
    """A UIMF file, through the association first and the configured path second.

    The order is the decision and not an implementation detail: an instrument PC
    where both installers have run needs no setting at all, and a machine where the
    association is wrong is exactly the machine where a trainee has been told to fill
    the setting in. A failure names both attempts.
    """
    first = open_path(path)
    if first or not configured.strip():
        return first
    second = open_with(configured, path)
    if second:
        return second
    return OpenResult(
        False,
        how=f"{first.how or 'the file association'}, then {second.how or configured}",
        problem=f"{first.problem or 'nothing is registered for .uimf files'}; "
                f"and {second.problem}",
    )
