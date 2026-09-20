r"""Handing a file to the program that should open it: mainspring, and a text editor.

Two buttons use this. **Open in mainspring** on the file a run wrote or is writing, and
**Open the log** on the transcript and the send log beside it. Both are the same problem
-- this process knows a path and wants another program to have it -- and both are solved
the same way, by asking the shell first and falling back to something explicit.

**The association first, a configured path second** (Matt, 2026-09-17). mainspring's
own installer registers itself as the handler for `.uimf`, so on an instrument PC where
both installers have run, opening the file *is* opening mainspring and there is nothing
to configure. Where that fails -- a dev clone with mainspring only as a checkout, or a
machine where something else claimed the extension -- the window's remembered
mainspring path is tried, and the failure says which of the two ways was tried and what
it said. Guessing a third way, or searching the disk for an executable, would make the
button's behaviour depend on what happens to be installed.

**A file being acquired is opened with arguments, and `os.startfile` cannot carry
them.** `--follow` and `--show <mode>` are how a viewer is told to watch a file that is
still filling, and `os.startfile` is `ShellExecute` on a *document*: Windows documents
`lpParameters` as meaningful for an executable and to be null for a document, so there
is nowhere to put them. The live route therefore resolves the association to a command
instead of invoking it -- `AssocQueryStringW(ASSOCSTR_COMMAND)`, and the extension's
ProgID's `shell\open\command` out of the registry if that answers nothing -- and runs
that command with the path substituted for its `"%1"` and the options after it. The
alternative was to use the configured path whenever options are wanted, which is
simpler and makes the live button depend on a setting that is normally empty, which is
the thing the association decision was taken to avoid. Resolving also hands back a
process handle, which `os.startfile` does not, so the window can tell whether the viewer
it launched is still open.

Qt-free: `os.startfile` is the Windows shell's own "open this the way a double-click
would", and the two fallbacks are `open` and `xdg-open`, which are the same idea. The
window shows what comes back; nothing here raises.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence

from mainspring.interface import OPTION_FOLLOW, OPTION_SHOW, SHOW_WORDS

__all__ = [
    "SHOW_FOR_MODE",
    "OpenResult",
    "association_command",
    "open_data_file",
    "open_data_file_with_options",
    "open_path",
    "open_with",
    "show_word",
    "still_running",
    "viewer_options",
]


class OpenResult:
    """Whether a file was handed off, and what to tell a trainee if it was not.

    A small class rather than a bare bool because the failure is the useful half:
    "nothing on this machine opens .uimf files" and "the mainspring you configured is
    not there any more" lead to different remedies, and a button that only went grey
    would name neither.

    `process` is the handle of what was started, where the route that started it has
    one -- the resolved association and the configured path both do, the shell's own
    `os.startfile` does not. It is there so a window can ask whether the viewer it
    launched is still open rather than launch a second one.
    """

    __slots__ = ("how", "opened", "problem", "process")

    def __init__(self, opened: bool, how: str = "", problem: str = "",
                 process: object | None = None) -> None:
        self.opened = opened
        self.how = how
        self.problem = problem
        self.process = process

    def __bool__(self) -> bool:
        return self.opened

    def __repr__(self) -> str:
        return (f"OpenResult({self.opened}, how={self.how!r}, "
                f"problem={self.problem!r})")


def still_running(process: object | None) -> bool:
    """Whether something a launch handed back is still up.

    `poll` and nothing else -- no wait, no signal -- so a window may ask this on its
    way through a refresh. Anything that is not a process counts as gone, which is the
    answer that makes a caller open a viewer rather than assume one is there.
    """
    poll = getattr(process, "poll", None)
    if poll is None:
        return False
    try:
        return poll() is None
    except OSError:
        return False


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


def open_with(command: str, path: str, options: Sequence[str] = (), *,
              spawn: Callable[[Sequence[str]], object] | None = None) -> OpenResult:
    """Run a configured program on a file, with any options after the path.

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
    line = [*parts, path, *options]
    try:
        process = (spawn or _spawn)(line)
    except OSError as exc:
        return OpenResult(False, how=parts[0], problem=str(exc))
    return OpenResult(True, how=parts[0], process=process)


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


def open_data_file_with_options(
    path: str,
    options: Sequence[str] = (),
    configured: str = "",
    *,
    resolve: Callable[[str], str] = lambda extension: association_command(extension),
    spawn: Callable[[Sequence[str]], object] | None = None,
) -> OpenResult:
    """A UIMF file opened with arguments, the association first and the setting second.

    The same order `open_data_file` keeps and the same failure text, by a different
    route: the association is resolved to a command and run, rather than invoked
    through `os.startfile`, which cannot carry an argument onto a document. The
    options go after the path, which is the shape mainspring's command line takes.

    `resolve` and `spawn` are arguments so that a test can hand in a registry that says
    what it wants it to say, and so that `--fake` can record the command line it would
    have run rather than open a viewer on simulated data.
    """
    if not os.path.isfile(path):
        return OpenResult(False, problem=f"there is no file at {path}")
    extension = os.path.splitext(path)[1] or ".uimf"
    command = resolve(extension)
    if command:
        line = [*_command_parts(command, path), *options]
        try:
            process = (spawn or _spawn)(line)
        except OSError as exc:
            first = OpenResult(False, how=f"the file association ({line[0]})",
                               problem=str(exc))
        else:
            return OpenResult(True, process=process,
                              how=f"the file association ({os.path.basename(line[0])})")
    else:
        first = OpenResult(
            False, how="the file association",
            problem=f"nothing is registered for {extension} files")
    if not configured.strip():
        return first
    second = open_with(configured, path, options, spawn=spawn)
    if second:
        return second
    return OpenResult(
        False,
        how=f"{first.how}, then {second.how or configured}",
        problem=f"{first.problem}; and {second.problem}",
    )


# -- resolving the association to a command --------------------------------------------

PATH_PLACEHOLDERS = ("%1", "%L", "%l")
"""What a registered command calls the file it is being asked to open.

`%1` is the ordinary one and `%L` the long-name form; `%*`, "every remaining argument",
is dropped, since the only further arguments are the ones this module appends itself.
"""


def association_command(extension: str) -> str:
    r"""The command line the shell would run for a file of this extension, or "".

    Two ways, in the order Windows itself prefers. `AssocQueryStringW` with
    `ASSOCSTR_COMMAND` is what the shell resolves a double-click through and honours
    a user's chosen default; the extension's ProgID's `shell\open\command`, read out of
    `HKEY_CLASSES_ROOT`, answers where that returns nothing. mainspring's installer
    registers the `mainspring.uimf` ProgID under `HKA`, and `HKEY_CLASSES_ROOT` is the
    merged view of the per-user and per-machine classes, so a per-user install answers.

    Off Windows there is no association to resolve and this says so by answering "",
    which sends the caller to the configured path.
    """
    if sys.platform != "win32":
        return ""
    return _assoc_query(extension) or _progid_command(extension)


def _assoc_query(extension: str) -> str:
    """`AssocQueryStringW(ASSOCSTR_COMMAND)`, or "" for any reason it does not answer.

    Called twice, which is the documented shape: the first call has a null buffer and
    fills in the length, the second fills in the string. Every failure is the same
    answer here -- there is no association -- because the caller's next step does not
    depend on which of them it was.

    **`ASSOCF_INIT_IGNOREUNKNOWN` is why an unregistered extension fails here rather
    than answering.** Without it the shell falls back to its `Unknown` class and
    reports `OpenWith.exe "%1"` -- the "How do you want to open this file?" dialog --
    which is not an association, and a caller told it had one would launch that dialog
    with `--follow --show ...` appended, tell a trainee mainspring had opened, and
    never reach the configured path that was the remedy. Measured on the lab's
    machines (lab record, task 55): with the flag an extension nothing owns fails with
    `ERROR_NO_ASSOCIATION` and a registered one still answers with its exe.
    """
    import ctypes
    from ctypes import wintypes

    assocf_init_ignoreunknown = 0x400
    assocstr_command = 1
    try:
        query = ctypes.WinDLL("shlwapi").AssocQueryStringW
        query.restype = ctypes.c_long
        query.argtypes = [
            wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        size = wintypes.DWORD(0)
        query(assocf_init_ignoreunknown, assocstr_command, extension, None, None,
              ctypes.byref(size))
        if not size.value:
            return ""
        buffer = ctypes.create_unicode_buffer(size.value)
        if query(assocf_init_ignoreunknown, assocstr_command, extension, None, buffer,
                 ctypes.byref(size)) != 0:
            return ""
        return buffer.value or ""
    except (OSError, AttributeError, ValueError):
        return ""


def _progid_command(extension: str) -> str:
    r"""The extension's ProgID's `shell\open\command`, or "" where there is none."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, extension) as key:
            progid = str(winreg.QueryValueEx(key, "")[0] or "")
        if not progid:
            return ""
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                            progid + r"\shell\open\command") as key:
            return str(winreg.QueryValueEx(key, "")[0] or "")
    except OSError:
        return ""


def _command_parts(command: str, path: str) -> list[str]:
    """A registered command template as an argument list, with the path in its place.

    The template says where the file goes -- `"mainspring.exe" "%1"` -- and putting the
    path *there* rather than after the last argument is what lets a template that takes
    the file anywhere but last still work. A template that names no placeholder at all
    gets the path appended, which is the same thing for the ordinary shape.
    """
    parts = [part.strip('"') for part in shlex.split(command, posix=False)]
    line: list[str] = []
    placed = False
    for part in parts:
        if part == "%*":
            continue
        for token in PATH_PLACEHOLDERS:
            if token in part:
                part = part.replace(token, path)  # noqa: PLW2901 -- the substitution
                placed = True
        line.append(part)
    if not placed:
        line.append(path)
    return line


def _spawn(line: Sequence[str]) -> object:
    return subprocess.Popen(list(line))  # noqa: S603


# -- which view a viewer launched on a run in progress opens in ------------------------

SHOW_FOR_MODE = {
    "single_frame": "newest",
    "per_repetition": "rolling-sum",
}
"""The `Show` mode a viewer launched on a run in progress starts in (Matt, 2026-09-19).

`single_frame` is one long frame filling for a minute, so a sum over finished frames
shows nothing at all until the run ends, and the newest frame is the only thing moving.
`per_repetition` finishes a frame every few hundred milliseconds, and the rolling sum --
the N newest finished frames, N a mainspring setting clockwork says nothing about --
holds a fixed integration time and so keeps moving, where the method sum only grows and
a late repetition barely changes it. The question a trainee has at the start of a long
run is whether anything is happening, and that is the mode that answers it.

The words are mainspring's, checked against `SHOW_WORDS` below rather than trusted: a
mode renamed on that side should stop clockwork at import with the name in the message,
not reach the viewer and exit 2 in front of a trainee.
"""

_unknown = set(SHOW_FOR_MODE.values()) - set(SHOW_WORDS)
if _unknown:  # pragma: no cover -- a guard against the viewer renaming a mode
    raise ValueError(
        "clockwork names a mainspring Show mode that does not exist: "
        + ", ".join(sorted(_unknown)) + "; mainspring offers " + ", ".join(SHOW_WORDS))
del _unknown


def show_word(repetition_mode: str) -> str:
    """The `Show` mode for a method acquiring in this repetition mode.

    A mode nobody has heard of gets `single_frame`'s answer: the newest frame is the
    one view that is certainly moving whatever the file turns out to be, where a sum
    of a shape this function did not expect could be empty and say nothing.
    """
    return SHOW_FOR_MODE.get(repetition_mode, SHOW_FOR_MODE["single_frame"])


def viewer_options(repetition_mode: str) -> tuple[str, ...]:
    """mainspring's command line for a file being acquired: follow it, in this mode.

    One word is passed, once, at launch. What the viewer does with it afterwards is the
    viewer's: a window launched this way ends with `Live` ticked and follows every later
    run of the session through clockwork's run pointer, carrying this mode with it
    (lab record, task 55).
    """
    return (OPTION_FOLLOW, OPTION_SHOW, show_word(repetition_mode))
