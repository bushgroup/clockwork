"""The PySide6 window: one pane per box, Send setup, Acquire, Replicate, Stop.

The only package in `clockwork` that imports Qt, and it imports it late -- `main` builds
the window, and importing this package does not pull PySide6 in, so `--self-check` and
the tests that assert the seam cost nothing for it.

    window.py         the window itself; holds no acquisition sequence of its own
    worker.py         the one thread that talks to the boxes and the console
    panes.py          a box's pane, with the phase clockwork read each line as
    runlog.py         the progress bar and the warnings worth reading
    console_panel.py  the console in the status bar, and its six editable settings
    settings.py       what is remembered between launches
    naming.py         `YYMMDD_INITIALS_NNN`, scanned off the output directory
    launch.py         handing a file to mainspring, and the logs to an editor
    errors.py         where a traceback goes in a build with no stderr to print it to

`naming.py`, `launch.py` and `errors.py` import no Qt: what a run is called, how a file
is opened and where a traceback goes are questions a test can ask without a window, and
the last of them has to be answerable before `QApplication` exists.

Three arguments and one command. `--fake` builds the whole window over `FakeBox` and
`FakeConsole`, so every path above the wire runs with no instrument on the bench;
`--self-check` is the installer's proof that the lower layers work inside a frozen
build, with no window shown; and no argument at all is the trainee's launch.
`clockwork serve` is the daemon (`clockwork.owner.daemon`), which owns the instrument
with no window at all and never imports Qt; `clockwork mcp` serves its tools to an MCP
client, and every one of those tools is also a verb of its own, `clockwork status`,
`clockwork arm` and the rest (`clockwork.mcp.cli`), over the same daemon.
"""

from __future__ import annotations

import argparse
import os
import sys

_ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "clockwork.ico")


def _numba_cache_dir() -> str:
    """A writable, persistent directory for numba's compiled-kernel cache (mainspring's
    fold decode, task 34): must be set before numba is imported, which happens lazily on
    the folding thread rather than at launch. A frozen build's own directory is not a
    candidate -- PyInstaller rebuilds it on every packaging run, and an installed copy
    may not be writable -- so this is a per-user directory outside it (task 32; mirrors
    mainspring's `viewer.app._numba_cache_dir`, task 07).
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return os.path.join(base, "clockwork", "numba-cache")


class _GuardedStream:
    """A stream that never raises: `print`, `parser.error` and argparse's usage message
    all call `write`/`flush` on `sys.stdout`/`sys.stderr` without expecting either to
    fail. A windowed build (`packaging/clockwork.spec`, `console=False`) starts both as
    `None`, and a self-check's report file may fail to open (a locked file, a read-only
    profile) -- neither should be the reason an exit code never comes back.
    """

    def __init__(self, stream: object | None = None) -> None:
        self._stream = stream

    @property
    def encoding(self) -> str | None:
        """The stream's, so a writer can tell what it will be able to encode."""
        return getattr(self._stream, "encoding", None)

    def write(self, text: str) -> None:
        if self._stream is None:
            return
        try:
            self._stream.write(text)
        except (AttributeError, OSError, ValueError):
            pass

    def flush(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.flush()
        except (AttributeError, OSError, ValueError):
            pass


def _attach_parent_console() -> bool:
    """Undo `console=False`'s redirection to nothing by attaching this process's
    stdio to the console it was started from, so a `--self-check` run from PowerShell
    or cmd prints where the operator is looking (task 60). False when there is no
    parent console to attach to -- launched from the Start menu, or by anything else
    that is not itself a console -- which is not a failure: the caller falls back to
    `_open_report_file`.
    """
    if sys.platform != "win32":
        return False
    import ctypes

    attach_parent_process = -1
    if not ctypes.windll.kernel32.AttachConsole(attach_parent_process):
        return False
    sys.stdout = _GuardedStream(open("CONOUT$", "w", encoding="utf-8", errors="replace"))
    sys.stderr = _GuardedStream(open("CONOUT$", "w", encoding="utf-8", errors="replace"))
    return True


def _report_log_path() -> str:
    """Where `--self-check`'s report goes with no console to attach to: a per-user log
    file beside `_numba_cache_dir`'s own directory, so a launch with nothing to print to
    still leaves a file an operator can go and read (task 60)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return os.path.join(base, "clockwork", "self-check.log")


def _open_report_file() -> str | None:
    """Point stdout and stderr at `_report_log_path()`, for a `--self-check` run with no
    parent console to attach to. Returns the path on success, or None if even the log
    file could not be opened -- in which case both streams are left silently guarded
    rather than raising, and only the exit code carries the verdict.
    """
    path = _report_log_path()
    stream = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stream = open(path, "a", encoding="utf-8")
    except OSError:
        pass
    sys.stdout = _GuardedStream(stream)
    sys.stderr = _GuardedStream(stream)
    return path if stream is not None else None


def _seed_numba_cache(cache_dir: str) -> None:
    """Copy a build-time pre-warmed numba cache into `cache_dir`, on a frozen build's
    first launch only (`tools/warm_numba_cache.py`; mirrors mainspring's
    `viewer.app._seed_numba_cache`, task 07). Unseeded, the cost lands inside clockwork's
    first fold rather than at launch, since the decode kernels are touched on the folding
    thread, not at start (task 32's progress log).
    """
    if not getattr(sys, "frozen", False):
        return
    if os.path.isdir(cache_dir) and os.listdir(cache_dir):
        return  # already seeded, or numba has already compiled into it
    import shutil

    bundle_root = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    seed = os.path.join(bundle_root, "numba_cache_seed")
    if os.path.isdir(seed):
        shutil.copytree(seed, cache_dir, dirs_exist_ok=True)


def _self_check() -> int:
    """The cheapest proof that `clockwork.mips`, `clockwork.acq` and `mainspring.uimf`
    all work inside this build, numba's decode kernels included: one box with no
    hardware, two repetitions through a console simulated in this process, the fold
    that is the one place clockwork calls into `mainspring.uimf.decode`, and the summed
    file read back. Prints and returns 1 on the first failure rather than raising, since
    this runs from a frozen `.exe` with no console attached to a traceback.
    """
    import datetime as dt
    import tempfile

    from mainspring.uimf import UimfFile

    from clockwork.acq import (
        Console,
        DataStream,
        FakeConsole,
        Geometry,
        Recording,
        run_frame,
        start_chain,
    )
    from clockwork.method import from_dict
    from clockwork.mips import Box, FakeBox

    try:
        box = Box(transport=FakeBox(), name="box1")
        box.local()
        box.command("STBLCLK,EXT")
        box.send_table("STBLDAT;0:[A:1,10:A:0,20:];")
        box.arm()

        method = from_dict({
            "schema_version": 2,
            "metadata": {"name": "self-check", "created": dt.date.today()},
            "acquisition": {"frames": 1, "scans": 16, "accumulations": 2,
                             "file_stem": "selfcheck", "repetition_mode": "per_repetition"},
            "boxes": [{"name": "box1", "port": "COM1", "load": ["STBLDAT;..."]}],
            "start": [["box1", "TBLSTRT"]],
        })

        with tempfile.TemporaryDirectory() as directory, \
                FakeConsole() as fake, \
                DataStream(fake.data_endpoint) as stream, \
                Console(fake.command_endpoint) as console:
            console.configure(offset_v=0.251)
            width = start_chain(console, stream, timeout=10.0, settle=2.0, quiet=0.1)
            geometry = Geometry.from_tof_width(
                width, sample_rate_hz=console.sample_rate_hz,
                post_trigger_samples=fake.post_trigger_samples,
            )
            with Recording.create(directory, method, geometry) as recording:
                for repetition in (1, 2):
                    with recording.frame(1, repetition) as request:
                        run_frame(console, stream, request, timeout=10.0)
                recording.fold(1)
                summed_path = recording.summed_path
            console.stop_acquire()

            frame = UimfFile(summed_path).frame_params(1)
    except Exception as exc:  # noqa: BLE001 -- report it, whatever it is, and exit
        print(f"self-check FAILED: {exc!r}", file=sys.stderr)
        return 1

    if frame.scans != 16 or frame.accumulations != 2:
        print(f"self-check FAILED: the folded frame declares {frame.scans} scans and "
              f"{frame.accumulations} accumulations, not 16 and 2", file=sys.stderr)
        return 1
    print("self-check OK: clockwork.mips, clockwork.acq, mainspring.uimf and its numba "
          "decode kernels all import and run inside this build.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point, and the frozen build's entry point through
    `packaging/entrypoint.py`.
    """
    # `console=False` (packaging/clockwork.spec, task 60) starts both streams as
    # `None`; guarded before the parser exists so an unrecognised option's usage
    # message -- argparse's own error path -- cannot raise on a stream that is not
    # there, whether or not `--self-check` is the argument that follows.
    sys.stdout = _GuardedStream(sys.stdout)
    sys.stderr = _GuardedStream(sys.stderr)

    parser = argparse.ArgumentParser(prog="clockwork")
    parser.add_argument(
        "--self-check", action="store_true",
        help="run a hardware-free stand-in acquisition and exit, no window shown",
    )
    parser.add_argument(
        "--fake", action="store_true",
        help="drive simulated boxes and a simulated console, for a desk with no "
             "instrument on it. Nothing a --fake run reports is evidence about a MIPS "
             "box or a digitizer.",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    serve = commands.add_parser(
        "serve", help="own the instrument with no window and serve clients over a "
                      "loopback socket (docs/daemon-protocol.md)",
        description="Own the boxes and the acquisition console, and serve the window, "
                    "the command line and the MCP server over a loopback socket, until "
                    "Ctrl-C or a client's shutdown.")
    serve.add_argument(
        "--fake", dest="serve_fake", action="store_true",
        help="simulated boxes and console; takes no lock. Nothing it reports is "
             "evidence about a MIPS box or a digitizer.")
    serve.add_argument("--output", metavar="DIR", default="",
                       help="where a run's files go when a job names no directory "
                            "(default: the working directory)")
    serve.add_argument("--library", metavar="DIR", default="",
                       help="the method library clients may list")
    serve.add_argument("--console", metavar="PATH", default="",
                       help="the acquisition console executable, or a directory holding "
                            "it (default: $CLOCKWORK_CONSOLE, then the installer's)")
    mcp = commands.add_parser(
        "mcp", help="serve the instrument's tools to an MCP client, such as Claude Code, "
                    "over stdio (docs/mcp-server.md)",
        description="Serve clockwork's tools over stdio to an MCP client: templates and "
                    "methods, the boxes, acquisition, and reading the files back. Drives "
                    "`clockwork serve`, or simulated hardware of its own under --fake.")
    mcp.add_argument(
        "--fake", dest="mcp_fake", action="store_true",
        help="simulated boxes and console in this process; no daemon needed. Nothing it "
             "reports is evidence about a MIPS box or a digitizer.")
    mcp.add_argument("--library", metavar="DIR", default="",
                     help="the method and template library (default: the daemon's)")
    mcp.add_argument("--output", metavar="DIR", default="",
                     help="where runs are written and read back (default: the daemon's)")
    mcp.add_argument("--instrument", metavar="PATH", default="",
                     help="the instrument document runs are acquired under")
    mcp.add_argument("--limits", metavar="PATH", default="",
                     help="the instrument's standing limits (default: limits.toml beside "
                          "--instrument; docs/instrument-limits.md)")
    mcp.add_argument("--endpoint", metavar="ADDRESS", default="",
                     help="the daemon's command socket (default: tcp://127.0.0.1:5570)")
    # The verbs are built from the tool registry, which costs half a second of imports
    # (the toolbox, the loop, mainspring's reader): paid only when the command line
    # could name a verb or asks for help, never by the window's own launch.
    words = list(sys.argv[1:] if argv is None else argv)
    command = next((word for word in words if not word.startswith("-")), None)
    cli = None
    if command not in (None, "serve", "mcp") or {"-h", "--help"} & set(words[:1]):
        from clockwork.mcp import cli

        cli.add_verbs(commands)
    args = parser.parse_args(argv)
    if cli is not None and cli.is_verb(args.command) and (args.fake or args.self_check):
        parser.error(f"{'--fake' if args.fake else '--self-check'} opens the window or "
                     f"checks the build; a verb such as {args.command} drives a daemon, and "
                     "a rehearsal is `clockwork serve --fake` with the verbs over it")

    # Before anything imports numba, lazily or otherwise: `_self_check` folds, and the
    # real window will too, and both need the cache pointed somewhere that survives a
    # rebuild before that first import happens.
    os.environ.setdefault("NUMBA_CACHE_DIR", _numba_cache_dir())
    os.makedirs(os.environ["NUMBA_CACHE_DIR"], exist_ok=True)
    _seed_numba_cache(os.environ["NUMBA_CACHE_DIR"])

    import clockwork

    if args.command == "serve":
        # A windowed build has no stdout of its own; the daemon's log belongs in the
        # terminal it was started from, as `--self-check`'s report does (task 60). A
        # checkout already has one, and attaching would take it from pytest.
        if getattr(sys, "frozen", False):
            _attach_parent_console()
        from clockwork.owner import daemon

        return daemon.run(fake=args.serve_fake, output=args.output, library=args.library,
                          console=args.console)

    if args.command == "mcp":
        # The protocol is stdin and stdout, and the SDK claims the real streams by their
        # file descriptors: the guard put round them above has neither, and attaching a
        # parent console, as `serve` does, would take them from the client.
        for name in ("stdout", "stderr"):
            real = getattr(sys, f"__{name}__")
            if real is not None:
                setattr(sys, name, real)
        from clockwork.mcp import server

        return server.run(fake=args.mcp_fake, library=args.library, output=args.output,
                          endpoint=args.endpoint, instrument_path=args.instrument,
                          limits_path=args.limits)

    if cli is not None and cli.is_verb(args.command):
        # As `serve`: a windowed build has no stdout of its own, and the answer belongs
        # in the terminal the verb was typed at (task 60).
        if getattr(sys, "frozen", False):
            _attach_parent_console()
        return cli.run(args)

    if args.self_check:
        report_path = None if _attach_parent_console() else _open_report_file()
        code = _self_check()
        if report_path is not None:
            print(f"self-check report written to {report_path}")
        return code

    # Before the window, so an exception raised while it is being built is written
    # down too. A windowed build has no stderr for PySide6's own report of what a slot
    # raised, and without this the slot returns and nothing anywhere says why (task 61).
    from . import errors

    errors.install()

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from .window import MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("clockwork")
    app.setApplicationVersion(clockwork.__version__)
    if os.path.isfile(_ICON):
        app.setWindowIcon(QIcon(_ICON))
    # The title carries the version, which is what `tools/build_exe.ps1`'s launch check
    # reads to tell a build that reached working code from one that died on the way
    # (mainspring's `packaging/entrypoint.py` does the same, task 32).
    window = MainWindow(fake=args.fake)
    window.show()
    return app.exec()
