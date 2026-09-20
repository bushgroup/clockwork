r"""Where a traceback goes in a build that has nowhere to print one.

**Task 60 made `clockwork.exe` a Windows-subsystem binary** (`console=False`, so no
console window opens behind the Qt one), and the cost of that is where this module
starts. `sys.stderr` is `None` in such a build; PySide6 reports an exception raised
inside a Qt slot by calling `sys.excepthook`, whose default writes the traceback to
`sys.stderr` and returns; the slot then returns as though nothing had happened. The
window carries on, the button that was pressed did nothing, and there is no file, no
dialog and no line anywhere saying why. A run on the clean machine that wrote no line
about mainspring at all is what named the gap (lab record, tasks 55 and 61): the only
two explanations were a box that had not been ticked and an exception nobody could
see, and nothing on the machine could tell them apart.

So an exception that reaches the top of a slot, or the top of a thread, is appended to
`%LOCALAPPDATA%\clockwork\errors.log` -- beside `self-check.log`, which is the
directory an operator is already told to look in -- and, when a window is up, said once
in the run log so that the person watching knows to go and read it.

**Installed in a checkout as well as in a frozen build**, though a checkout has a
perfectly good `stderr`. Two reasons: a test can then raise inside a queued slot and
find the file, which is the only way to know the hook works at all; and a trainee
running from a clone has the same right to the file as one running the installer. The
console keeps its traceback either way -- the previous hook is called, not replaced.

Qt-free, so `main` can install it before `QApplication` exists and before the window
that will report the lines has been built.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
import threading
import traceback
from collections.abc import Callable

__all__ = ["errors_log_path", "install", "record"]

_SAY: list[Callable[[str], None]] = []
"""Where a line goes when there is a window to put it in.

A list rather than a single slot because "no window yet" and "a window" are the two
states that matter and a list is the shortest way to have both; `main` installs the
hooks before the window exists, and the window registers itself when it does.
"""


def errors_log_path() -> str:
    r"""`%LOCALAPPDATA%\clockwork\errors.log`, or `~/.cache` off Windows.

    The same base as `_report_log_path` and `_numba_cache_dir`, so everything a build
    leaves behind for a person to read is in one directory and the user guide has one
    path to name.
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return os.path.join(base, "clockwork", "errors.log")


def record(kind: type[BaseException], value: BaseException,
           tb: object, where: str = "") -> str:
    """Append one traceback to the errors log, and return the path it went to.

    Never raises. It is called from an exception handler of last resort, and a hook
    that itself raises is worse than the silence it was written to end: Python prints
    "Error in sys.excepthook" to the stderr this build does not have, and the original
    exception is lost with it. So every failure here -- an unwritable profile, a full
    disk, a locked file -- ends in the path being returned anyway, since the caller's
    next move is to say where it looked.
    """
    path = errors_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        when = _dt.datetime.now().isoformat(timespec="seconds")
        text = "".join(traceback.format_exception(kind, value, tb))
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"\n---- {when}{' ' + where if where else ''} ----\n{text}")
    except (OSError, ValueError):
        pass
    return path


def say(line: str) -> None:
    """Tell whatever window has registered itself, if one has."""
    for report in list(_SAY):
        try:
            report(line)
        except Exception:  # noqa: BLE001 -- a hook of last resort reports nothing back
            pass


def reporting_to(report: Callable[[str], None]) -> Callable[[], None]:
    """Register a window's way of saying a line, and hand back how to stop.

    The window calls this when it is built and the returned callable when it closes, so
    a traceback raised after the window is gone is written to the file and not into a
    widget that Qt has deleted.
    """
    _SAY.append(report)

    def stop() -> None:
        if report in _SAY:
            _SAY.remove(report)

    return stop


def install() -> None:
    """Point `sys.excepthook` and `threading.excepthook` at the errors log.

    **Both**, because the work is on a thread. `sys.excepthook` catches what a Qt slot
    raises, since PySide6 routes a slot's exception through it; `threading.excepthook`
    catches what escapes the top of the worker's `run`, which `sys.excepthook` never
    sees. A `SystemExit` or a `KeyboardInterrupt` is passed straight to the previous
    hook: neither is a defect, and writing "the program was asked to quit" into a file
    named errors.log is how a file stops being read.

    Idempotent, so a test may call it and `main` may have called it already.
    """
    if getattr(sys.excepthook, "_clockwork", False):
        return

    previous = sys.excepthook
    previous_thread = threading.excepthook

    def hook(kind, value, tb) -> None:  # noqa: ANN001 -- sys.excepthook's own signature
        if issubclass(kind, (SystemExit, KeyboardInterrupt)):
            previous(kind, value, tb)
            return
        path = record(kind, value, tb)
        say(f"something went wrong inside the window and was written to {path}: "
            f"{kind.__name__}: {value}")
        previous(kind, value, tb)

    def thread_hook(args) -> None:  # noqa: ANN001 -- threading.excepthook's own signature
        if issubclass(args.exc_type, SystemExit):
            previous_thread(args)
            return
        where = f"on thread {args.thread.name}" if args.thread is not None else ""
        path = record(args.exc_type, args.exc_value, args.exc_traceback, where)
        say(f"something went wrong on a background thread and was written to {path}: "
            f"{args.exc_type.__name__}: {args.exc_value}")
        previous_thread(args)

    hook._clockwork = True  # noqa: SLF001 -- the idempotence flag, read just above
    sys.excepthook = hook
    threading.excepthook = thread_hook
