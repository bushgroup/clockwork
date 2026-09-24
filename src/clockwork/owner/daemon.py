r"""`clockwork serve`: the process that owns the instrument and lets clients drive it.

A `LocalOwner` behind `clockwork.owner.remote.DaemonServer`, with what a process needs
around it and a window used to supply: the instrument lock taken before anything else,
the console left behind by a dead owner stopped, the boxes found and the console started
as the window does when it opens, a watch on the console that restarts it if it stops on
its own, a log a person can audit the session from, and Ctrl-C. `docs/daemon-protocol.md`
is the outward description; this is its implementation (lab record, task 68).

**A console left running is stopped, not adopted.** A console this process did not start
has no captured output, so neither the startup block that is the authority on what it
read from `config.txt` (lab record, task 47) nor anything it writes afterwards could
reach a transcript. Stopping it costs one console start, about five seconds. It is
stopped only when it is the console executable, and only once the lock is held, so no
live clockwork can be the owner of what is stopped.

**Ctrl-C is a shutdown, and a second one abandons it.** The first asks the owner to stop
the run in flight after its current repetition and fold; the socket keeps answering
`status` while that happens. The second stops the console and exits at once, which
leaves the run as its last completed repetition.

The log is `%LOCALAPPDATA%\clockwork\serve.log`, appended, and the terminal. It is not
the window's `errors.log` (Matt, 2026-09-23): that file is tracebacks for a window with
nowhere to print them, and a request-by-request narrative would bury them.

Qt-free: `clockwork serve` never builds a window and never imports PySide6.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import traceback
from collections.abc import Callable
from typing import TextIO

from .. import __version__
from ..acq import Event, Warned
from ..acq.process import listening_on, stop_listener
from ..acq.wire import COMMAND_PORT
from ..mips import Discovery
from .interface import ConsoleChanged, Handle, JobFailed, JobFinished, JobStarted, RunDone, Said
from .jobs import Discover, RestartConsole, StartConsole
from .local import LocalOwner
from .remote import DEFAULT_COMMAND, DEFAULT_EVENTS, DaemonError, DaemonServer

__all__ = ["LOG_NAME", "PROGRAM", "WATCH_S", "ConsoleWatch", "log_path", "run"]

PROGRAM = "clockwork serve"
"""What the lock tells a refused owner holds the instrument."""

LOG_NAME = "serve.log"

WATCH_S = 2.0
"""How often the console is looked at. A dead console costs nothing until a job needs
it, so this is about the log line appearing promptly, not about the instrument."""

_LOGGED = (JobStarted, JobFinished, JobFailed, Said, Warned, RunDone, ConsoleChanged)
"""The events worth a line in the log. Not `BatchSeen`, fifteen a second, nor a
`PhaseSent` per string, which the run's own send log already holds."""


def log_path() -> str:
    r"""`%LOCALAPPDATA%\clockwork\serve.log`, `~/.cache` off Windows, as `errors.log`."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return os.path.join(base, "clockwork", LOG_NAME)


def run(
    *,
    fake: bool = False,
    output: str = "",
    library: str = "",
    console: str = "",
    command: str = DEFAULT_COMMAND,
    events: str = DEFAULT_EVENTS,
    lock_path: str | None = None,
    log_file: str | None = None,
    stream: TextIO | None = None,
    on_ready: Callable[[DaemonServer], None] | None = None,
    handle_signals: bool = True,
    discover: Callable[..., Discovery] | None = None,
    clear_port: bool = True,
    start_console: bool = True,
) -> int:
    """Take the instrument and serve it until shut down. Returns the exit code.

    Everything after `console` is for a test: other endpoints, another lock and log
    file, a stream in place of stdout, a callback given the server once it is bound, and
    no Ctrl-C handler, which Python lets only the main thread install. The last three
    are what let a test run a daemon that is not `--fake` on an instrument PC without
    touching the instrument: a stand-in scan, no console port cleared -- which on that
    PC would be the trainee's console -- and no console started.
    """
    log = _logger(stream if stream is not None else sys.stdout, log_file or log_path())
    try:
        return _run(log, fake=fake, output=output, library=library, console=console,
                    command=command, events=events, lock_path=lock_path, on_ready=on_ready,
                    handle_signals=handle_signals, discover=discover,
                    clear_port=clear_port, start_console=start_console)
    except Exception:  # noqa: BLE001 -- the log is the only place this can be read
        log.error("clockwork serve failed:\n%s", traceback.format_exc().rstrip())
        return 1
    finally:
        for handler in list(log.handlers):
            handler.close()
            log.removeHandler(handler)


def _run(log: logging.Logger, *, fake: bool, output: str, library: str, console: str,
         command: str, events: str, lock_path: str | None,
         on_ready: Callable[[DaemonServer], None] | None,
         handle_signals: bool, discover: Callable[..., Discovery] | None,
         clear_port: bool, start_console: bool) -> int:
    def on_event(handle: Handle, event: Event) -> None:
        if isinstance(event, _LOGGED):
            prefix = f"job {handle.id}: " if handle.id else ""
            log.info("%s%s", prefix, event.text)

    output = os.path.abspath(output or os.getcwd())
    owner = LocalOwner(fake=fake, program=PROGRAM, lock_path=lock_path, on_event=on_event,
                       **({"discover": discover} if discover is not None else {}))
    if owner.refused:
        log.error("%s", owner.refused)
        return 1

    if not fake and clear_port:
        _clear_console_port(log)

    try:
        server = DaemonServer(owner, command=command, events=events,
                              info={"library": library}, directory=output,
                              on_request=log.info)
    except DaemonError as exc:
        log.error("%s", exc)
        owner.shutdown("could not listen")
        owner.serve()  # nothing queued: closes up and lets go of the lock
        return 1

    status = owner.status()
    log.info("clockwork serve %s (pid %d)%s", __version__, os.getpid(),
             " --fake: simulated boxes and console; nothing it reports is evidence "
             "about a MIPS box or a digitizer" if fake else "")
    log.info("commands on %s, events on %s; files to %s", server.command_endpoint,
             server.events_endpoint, output)
    if status.holder is not None:
        log.info("holding the instrument lock as %s", status.holder.text)

    owner.start()
    if not fake:
        owner.submit(Discover())
    if not start_console:
        pass
    elif fake:
        owner.submit(StartConsole(command=""))
    else:
        from ..acq import find_console

        path = find_console(console or None)
        if path:
            owner.submit(StartConsole(command=path))
        else:
            log.warning("no acquisition console found (--console, $CLOCKWORK_CONSOLE, or "
                        "the installer's console directory); a client's StartConsole "
                        "will fail until one is given")

    watch = ConsoleWatch(owner, log).start()
    if handle_signals:
        _handle_ctrl_c(server, owner, log)
    if on_ready is not None:
        on_ready(server)
    try:
        server.serve()
    finally:
        watch.stop()
    owner.join(30)
    log.info("clockwork serve stopped")
    return 0


class ConsoleWatch:
    """Restart the console once when it stops on its own while no job is running.

    Through the owner's queue, like every other job, so the restart happens on the
    owner's thread. Only a console that was last seen ready is restarted: a restart
    that fails leaves the status at `starting`, which is not retried, so a console that
    cannot start is reported once rather than restarted in a loop.
    """

    def __init__(self, owner: LocalOwner, log: logging.Logger,
                 every: float = WATCH_S) -> None:
        self.owner = owner
        self.log = log
        self.every = every
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="console watch",
                                        daemon=True)

    def start(self) -> ConsoleWatch:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._done.set()
        self._thread.join(timeout=2 * self.every)

    def check(self) -> bool:
        """One look. True if a restart was queued."""
        owner = self.owner
        console = owner.console
        if owner.closing or console is None or console.alive:
            return False
        if owner.console_status.state != "ready":
            return False
        status = owner.status()
        if status.running is not None or any(
                handle.kind in ("StartConsole", "RestartConsole")
                for handle in status.queued):
            return False
        self.log.warning("the acquisition console stopped on its own; restarting it")
        owner.submit(RestartConsole(label="restarting the console, which had stopped"))
        return True

    def _watch(self) -> None:
        while not self._done.wait(self.every):
            try:
                self.check()
            except Exception:  # noqa: BLE001 -- a watch that raises watches nothing
                self.log.error("the console watch failed:\n%s",
                               traceback.format_exc().rstrip())


def _clear_console_port(log: logging.Logger) -> None:
    """Stop a console an owner that died left on the console's command port."""
    listener = listening_on(COMMAND_PORT)
    if listener is None:
        return
    if not listener.is_console:
        log.warning("port %d is held by %s, which is not the acquisition console; the "
                    "console cannot start until that program lets it go",
                    COMMAND_PORT, listener.text)
        return
    log.warning("an acquisition console, %s, was already answering on port %d with no "
                "clockwork holding the instrument; stopping it, since a console this "
                "process did not start has no output it can read", listener.text,
                COMMAND_PORT)
    if stop_listener(listener):
        log.info("stopped it; port %d is free", COMMAND_PORT)
    else:
        log.error("it did not stop, and the console will not start until port %d is "
                  "free", COMMAND_PORT)


def _handle_ctrl_c(server: DaemonServer, owner: LocalOwner, log: logging.Logger) -> None:
    presses = [0]

    def interrupted(_signum: int, _frame: object) -> None:
        presses[0] += 1
        if presses[0] == 1:
            log.info("Ctrl-C: stopping after the current repetition, folding and closing "
                     "the files; press Ctrl-C again to abandon the run")
            server.request_shutdown("Ctrl-C at the terminal")
            return
        log.warning("Ctrl-C again: abandoning the run in flight and stopping the console")
        console = owner.console
        if console is not None:
            try:
                console.stop()
            except Exception:  # noqa: BLE001
                pass
        for handler in log.handlers:
            handler.flush()
        os._exit(2)

    signal.signal(signal.SIGINT, interrupted)


def _logger(stream: TextIO, path: str) -> logging.Logger:
    log = logging.getLogger("clockwork.serve")
    log.setLevel(logging.INFO)
    log.propagate = False
    for handler in list(log.handlers):
        handler.close()
        log.removeHandler(handler)
    formatter = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = [logging.StreamHandler(stream)]
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(path, mode="a", encoding="utf-8"))
    except OSError as exc:
        print(f"clockwork serve: could not open {path} ({exc}); logging to the "
              "terminal only", file=stream)
    for handler in handlers:
        handler.setFormatter(formatter)
        log.addHandler(handler)
    return log
