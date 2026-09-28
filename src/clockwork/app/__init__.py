"""The PySide6 window: one pane per box, Send setup, Acquire, Replicate, Stop.

The only package in `clockwork` that imports Qt, and it imports it late -- `main` builds
the window, and importing this package does not pull PySide6 in, so `--self-check` and
the tests that assert the seam cost nothing for it.

    window.py         the window itself; holds no acquisition sequence of its own
    worker.py         the thread that follows `clockwork serve`, or --fake's own owner
    serving.py        starting `clockwork serve` when the window finds none running
    panes.py          a box's pane, with the phase clockwork read each line as
    runlog.py         the progress bar and the warnings worth reading
    console_panel.py  the console in the status bar, and its six editable settings
    settings.py       what is remembered between launches
    naming.py         `YYMMDD_INITIALS_NNN`, scanned off the output directory
    launch.py         handing a file to mainspring, and the logs to an editor
    errors.py         where a traceback goes in a build with no stderr to print it to

`naming.py`, `launch.py`, `serving.py` and `errors.py` import no Qt: what a run is
called, how a file is opened, how the daemon is started and where a traceback goes are
questions a test can ask without a window, and the last of them has to be answerable
before `QApplication` exists.

Three arguments and one command. `--fake` builds the whole window over `FakeBox` and
`FakeConsole` in its own process, so every path above the wire runs with no instrument
on the bench; `--self-check` is the installer's proof that the lower layers work inside
a frozen build, with no window shown; and no argument at all is the trainee's launch, a
window that is a client of `clockwork serve` and starts one if none is running (lab
record, task 77). `clockwork serve` is the daemon (`clockwork.owner.daemon`), which owns
the instrument with no window at all and never imports Qt; `clockwork mcp` serves its
tools to an MCP client, and every one of those tools is also a verb of its own,
`clockwork status`, `clockwork arm` and the rest (`clockwork.mcp.cli`), over the same
daemon.
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


class _Tee:
    """Every write to each of several streams, each guarded on its own, so a console that
    has gone away cannot cost the report file its copy (task 84)."""

    def __init__(self, *streams: object) -> None:
        self._streams = [s if isinstance(s, _GuardedStream) else _GuardedStream(s)
                         for s in streams]

    @property
    def encoding(self) -> str | None:
        return self._streams[0].encoding if self._streams else None

    def write(self, text: str) -> None:
        for stream in self._streams:
            stream.write(text)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


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


SEEDED_MARKER = ".seeded-by"
"""The file in the cache folder naming the executable whose seed was last copied in."""

WARM_OPTION = "--warm-numba-cache"
"""What the build runs the built `.exe` with, and a directory: compile into it and exit.

Not an argparse option, because nobody but `tools/warm_numba_cache.py` has a reason to
type it. The seed has to come from the frozen executable itself: numba stamps a frozen
program's cache with the executable's modification time and size, which the installer
preserves, so a cache compiled by any other interpreter is never read. mainspring 1.10.0
makes a frozen program's numba cache one folder, `clockwork_uimf` under
`NUMBA_CACHE_DIR`, where before it was a folder per launch directory under numba's own
(mainspring's lab record, task 34).
"""


def _exe_stamp() -> str:
    st = os.stat(sys.executable)
    return f"{os.path.basename(sys.executable)} {int(st.st_mtime)} {st.st_size}"


def _seed_numba_cache(cache_dir: str) -> None:
    """Copy the build's pre-warmed numba cache into `cache_dir`, once per executable
    (`tools/warm_numba_cache.py`; mirrors mainspring's `viewer.app._seed_numba_cache`).
    Unseeded, the cost lands inside clockwork's first fold rather than at launch, since
    the decode kernels are touched on the folding thread, not at start (task 32's
    progress log).

    Once per executable rather than into an empty folder only: an upgrade lands on a
    folder the previous version filled, and its seed must still arrive. A marker names
    the executable (file name, modification time, size) whose seed was copied.
    """
    if not getattr(sys, "frozen", False):
        return
    import shutil

    bundle_root = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    seed = os.path.join(bundle_root, "numba_cache_seed")
    if not os.path.isdir(seed):
        return
    marker = os.path.join(cache_dir, SEEDED_MARKER)
    stamp = _exe_stamp()
    try:
        with open(marker, encoding="utf-8") as fh:
            if fh.read().strip() == stamp:
                return
    except OSError:
        pass
    shutil.copytree(seed, cache_dir, dirs_exist_ok=True)
    with open(marker, "w", encoding="utf-8") as fh:
        print(stamp, file=fh)


def _warm_numba_cache(directory: str) -> int:
    """`WARM_OPTION`: compile every mainspring kernel into `directory`; an exit status."""
    os.environ["NUMBA_CACHE_DIR"] = os.path.abspath(directory)
    os.makedirs(os.environ["NUMBA_CACHE_DIR"], exist_ok=True)
    from mainspring.uimf import decode

    try:
        decode.warm_kernels()
    except Exception:  # noqa: BLE001 -- the build reads the status, not a traceback
        return 1
    # A frozen build that warmed without the locator wrote a cache nothing will read.
    if getattr(sys, "frozen", False) and not decode.install_frozen_cache_locator():
        return 1
    return 0


def _prepare_numba_cache() -> str:
    """Point numba's cache somewhere that survives a rebuild, and seed it on a frozen
    build's first launch. Before anything imports numba, lazily or otherwise: the
    self-check folds, and the real window will too."""
    os.environ.setdefault("NUMBA_CACHE_DIR", _numba_cache_dir())
    os.makedirs(os.environ["NUMBA_CACHE_DIR"], exist_ok=True)
    _seed_numba_cache(os.environ["NUMBA_CACHE_DIR"])
    return os.environ["NUMBA_CACHE_DIR"]


# The self-check's wait for each simulated repetition to end. A fresh build's first
# launch spends 5.2-5.7 s in repetition 1 where a warm one spends 0.01-0.9 s (lab record,
# task 84), and the 10 s this used to be was the likeliest cause of two first runs that
# failed with their reports lost; an in-process fake can hang but has no reason to be
# slow, so the limit only has to tell the two apart.
SELF_CHECK_FRAME_TIMEOUT_S = 60.0


class _Rows:
    """The self-check's report, one row per stage with the seconds it took, each flushed
    as it is written: a stage that fails is named by the row it fails on, and one that
    only ran slow shows how slow (task 84)."""

    def __init__(self) -> None:
        import time

        self._clock = time.perf_counter
        self.started = self._clock()
        self.failed: str | None = None

    def __call__(self, name: str):
        import contextlib

        @contextlib.contextmanager
        def row():
            began = self._clock()
            try:
                yield
            except BaseException:
                self.failed = name
                self._write(f"  {name:<38} FAILED after {self._clock() - began:.2f} s")
                raise
            self._write(f"  {name:<38} ok  {self._clock() - began:6.2f} s")

        return row()

    def total(self) -> float:
        return self._clock() - self.started

    @staticmethod
    def _write(line: str) -> None:
        print(line)
        sys.stdout.flush()


def _self_check() -> int:
    """The cheapest proof that `clockwork.mips`, `clockwork.acq` and `mainspring.uimf`
    all work inside this build, numba's decode kernels included: one box with no
    hardware, two repetitions through a console simulated in this process, the fold
    that is the one place clockwork calls into `mainspring.uimf.decode`, and the summed
    file read back. One timed row per stage; on the first failure, the stage, the
    exception and its traceback, and 1 rather than a raise, since this runs from a
    frozen `.exe` with no console attached to a traceback.
    """
    import contextlib
    import datetime as dt
    import tempfile
    import traceback

    step = _Rows()
    try:
        with step("numba cache"):
            cache = _prepare_numba_cache()
        print(f"    NUMBA_CACHE_DIR = {cache}")
        with step("imports"):
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

        with step("box: table sent and armed"):
            box = Box(transport=FakeBox(), name="box1")
            box.local()
            box.command("STBLCLK,EXT")
            box.send_table("STBLDAT;0:[A:1,10:A:0,20:];")
            box.arm()

        with step("method"):
            method = from_dict({
                "schema_version": 2,
                "metadata": {"name": "self-check", "created": dt.date.today()},
                "acquisition": {"frames": 1, "scans": 16, "accumulations": 2,
                                 "file_stem": "selfcheck",
                                 "repetition_mode": "per_repetition"},
                "boxes": [{"name": "box1", "port": "COM1", "load": ["STBLDAT;..."]}],
                "start": [["box1", "TBLSTRT"]],
            })

        with contextlib.ExitStack() as stack:
            with step("simulated console: connect"):
                directory = stack.enter_context(tempfile.TemporaryDirectory())
                fake = stack.enter_context(FakeConsole())
                stream = stack.enter_context(DataStream(fake.data_endpoint))
                console = stack.enter_context(Console(fake.command_endpoint))
                console.configure(offset_v=0.251)
            with step("simulated console: start chain"):
                width = start_chain(console, stream, timeout=10.0, settle=2.0, quiet=0.1)
                geometry = Geometry.from_tof_width(
                    width, sample_rate_hz=console.sample_rate_hz,
                    post_trigger_samples=fake.post_trigger_samples,
                )
            with step("recording created"):
                recording = stack.enter_context(
                    Recording.create(directory, method, geometry))
            for repetition in (1, 2):
                # Three rows, not one: a fresh build's first repetition runs several times
                # slower than a warm one's, and only the middle row is under a deadline.
                with contextlib.ExitStack() as frame:
                    with step(f"repetition {repetition}: frame opened"):
                        request = frame.enter_context(recording.frame(1, repetition))
                    with step(f"repetition {repetition}: acquired "
                              f"({SELF_CHECK_FRAME_TIMEOUT_S:g} s limit)"):
                        run_frame(console, stream, request,
                                  timeout=SELF_CHECK_FRAME_TIMEOUT_S)
                    with step(f"repetition {repetition}: frame written"):
                        frame.close()
            with step("fold (numba decode)"):
                recording.fold(1)
                summed_path = recording.summed_path
            with step("recording closed"):
                recording.close()
            with step("simulated console: stop"):
                console.stop_acquire()
            with step("summed file read back"):
                frame = UimfFile(summed_path).frame_params(1)
            step.failed = "teardown"
        step.failed = None
    except Exception as exc:  # noqa: BLE001 -- report it, whatever it is, and exit
        print(f"self-check FAILED at {step.failed or 'an unnamed stage'!r} after "
              f"{step.total():.2f} s: {exc!r}", file=sys.stderr)
        print(traceback.format_exc().rstrip(), file=sys.stderr)
        return 1

    if frame.scans != 16 or frame.accumulations != 2:
        print(f"self-check FAILED: the folded frame declares {frame.scans} scans and "
              f"{frame.accumulations} accumulations, not 16 and 2", file=sys.stderr)
        return 1
    print(f"self-check OK: clockwork.mips, clockwork.acq, mainspring.uimf and its numba "
          f"decode kernels all import and run inside this build ({step.total():.2f} s).")
    return 0


def _run_self_check(report: str) -> int:
    """`--self-check`: find the report somewhere to go, then run it. The console it was
    started from when there is one; also `report`'s file when the caller names one
    (`tools/build_exe.ps1`, task 84), since whether a windowed exe attaches is not
    predictable from how it was started; the per-user log only when neither is there.
    """
    import datetime as dt

    attached = _attach_parent_console()
    named = None
    if report:
        report = os.path.abspath(report)
        try:
            os.makedirs(os.path.dirname(report), exist_ok=True)
            named = open(report, "w", encoding="utf-8", newline="\n", buffering=1)
        except OSError as exc:
            print(f"the self-check report {report} could not be opened: {exc}",
                  file=sys.stderr)
        else:
            sys.stdout = _Tee(sys.stdout, named)
            sys.stderr = _Tee(sys.stderr, named)
    fallback = None if attached or named is not None else _open_report_file()

    try:
        import clockwork

        print(f"clockwork {clockwork.__version__} "
              f"({clockwork.built_commit() or 'unknown commit'}) --self-check, "
              f"{dt.datetime.now().isoformat(timespec='seconds')}")
        print(f"    executable = {sys.executable}"
              f"{' (frozen)' if getattr(sys, 'frozen', False) else ''}")
        print(f"    console attached = {'yes' if attached else 'no'}; report file = "
              f"{report if named is not None else fallback or 'none'}")
        sys.stdout.flush()
        code = _self_check()
        if fallback is not None:
            print(f"self-check report written to {fallback}")
        return code
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        if named is not None:
            named.close()


def _report(args: argparse.Namespace) -> int:
    """`clockwork report`: keep the run's files, print the pre-filled issue's address,
    and open it."""
    import webbrowser

    from clockwork import keep
    from clockwork.report import Report

    from . import errors

    errors_log = errors.errors_log_path()
    report_id = kept = ""
    if not args.no_keep:
        from .settings import Settings

        transcript = os.path.abspath(args.transcript) if args.transcript else ""
        method = args.method if os.path.isfile(args.method) else ""
        try:
            kept = keep.for_report(keep.root(Settings().kept_root),
                                   directory=os.path.dirname(transcript),
                                   stem=keep.stem_of(transcript),
                                   method_path=method, errors_log=errors_log)
            report_id = os.path.basename(kept)
        except OSError as exc:
            print(f"the files could not be kept: {exc}", file=sys.stderr)
    address = Report(method=args.method, transcript=args.transcript,
                     errors_log=errors_log, report_id=report_id, kept=kept).url()
    if kept:
        print(f"files kept in {kept}", file=sys.stderr)
    print(address)
    if not args.print_only and not webbrowser.open(address):
        print("no browser could be opened; paste the address above into one",
              file=sys.stderr)
        return 1
    return 0


def _request(args: argparse.Namespace) -> int:
    """`clockwork request`: print the feature request form's address, and open it."""
    import webbrowser

    from clockwork.report import request_url

    address = request_url()
    print(address)
    if not args.print_only and not webbrowser.open(address):
        print("no browser could be opened; paste the address above into one",
              file=sys.stderr)
        return 1
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

    early = list(sys.argv[1:] if argv is None else argv)
    if len(early) == 2 and early[0] == WARM_OPTION:
        return _warm_numba_cache(early[1])

    parser = argparse.ArgumentParser(prog="clockwork")
    parser.add_argument("--version", action="store_true",
                        help="print the version and the commit it was built from, and exit")
    parser.add_argument(
        "--self-check", action="store_true",
        help="run a hardware-free stand-in acquisition and exit, no window shown",
    )
    parser.add_argument(
        "--self-check-report", metavar="PATH", default="",
        help="with --self-check, also write its report to PATH (replaced), whether or "
             "not a console is attached",
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
    serve.add_argument("--kept", metavar="DIR", default=None,
                       help="where a failed run's files are copied (default: "
                            "$CLOCKWORK_REPORTS, then the per-user folder)")
    serve.add_argument("--errors-log", metavar="PATH", default="",
                       help="a front end's error log, copied with a failed run's files")
    serve.add_argument("--detached", action="store_true",
                       help="log to serve.log alone, never to the console of whatever "
                            "started it: what the window and the verbs that start a "
                            "daemon pass")
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
    mcp.add_argument("--routines", metavar="DIR", default="",
                     help="the instrument's routines (default: routines beside the library; "
                          "docs/routines.md)")
    mcp.add_argument("--endpoint", metavar="ADDRESS", default="",
                     help="the daemon's command socket (default: tcp://127.0.0.1:5570)")
    report = commands.add_parser(
        "report", help="open a bug report on the lab's issue tracker, filled in with this "
                       "installation's facts",
        description="Open a new bug report in the browser with the version, the build "
                    "commit, the PC, the operating system and the tail of the window's "
                    "error log already filled in, and the tail of a run's transcript when "
                    "one is named. Strings sent to the boxes and the console are left out.")
    report.add_argument("--transcript", metavar="PATH", default="",
                        help="a run's .transcript.log, whose tail goes in the report")
    report.add_argument("--method", metavar="NAME", default="",
                        help="the method's name or path, for the report; its contents never "
                             "go in, but a path's file is kept with the run's")
    report.add_argument("--print-only", action="store_true",
                        help="print the address and open no browser")
    report.add_argument("--no-keep", action="store_true",
                        help="copy nothing to the kept-files folder")
    request = commands.add_parser(
        "request", help="open a feature request on the lab's issue tracker",
        description="Open a new feature request in the browser with the version, the "
                    "build commit and the PC already filled in.")
    request.add_argument("--print-only", action="store_true",
                         help="print the address and open no browser")
    # The verbs are built from the tool registry, which costs half a second of imports
    # (the toolbox, the loop, mainspring's reader): paid only when the command line
    # could name a verb or asks for help, never by the window's own launch.
    words = list(sys.argv[1:] if argv is None else argv)
    command = next((word for word in words if not word.startswith("-")), None)
    cli = None
    if (command not in (None, "serve", "mcp", "report", "request")
            or {"-h", "--help"} & set(words[:1])):
        from clockwork.mcp import cli

        cli.add_verbs(commands)
    args = parser.parse_args(argv)
    if cli is not None and cli.is_verb(args.command) and (args.fake or args.self_check):
        parser.error(f"{'--fake' if args.fake else '--self-check'} opens the window or "
                     f"checks the build; a verb such as {args.command} drives a daemon, and "
                     "a rehearsal is `clockwork serve --fake` with the verbs over it")

    if args.self_check_report and not args.self_check:
        parser.error("--self-check-report is only read with --self-check")
    if args.self_check:
        # Before the numba cache is seeded, which is the self-check's first row: a copy
        # that fails there is reported like any other stage rather than raised with
        # nowhere to print it (task 84).
        return _run_self_check(args.self_check_report)

    _prepare_numba_cache()

    import clockwork

    if args.version or args.command in ("report", "request"):
        # As `serve`: the answer belongs in the terminal it was typed at (task 60).
        if getattr(sys, "frozen", False):
            _attach_parent_console()
        if args.version:
            print(f"clockwork {clockwork.__version__} "
                  f"({clockwork.built_commit() or 'unknown commit'})")
            return 0
        return _report(args) if args.command == "report" else _request(args)

    if args.command == "serve":
        # A windowed build has no stdout of its own; the daemon's log belongs in the
        # terminal it was started from, as `--self-check`'s report does (task 60). A
        # checkout already has one, and attaching would take it from pytest. A daemon
        # a verb started detached is not typed at that terminal: attached, it printed
        # its later lines after the verb's prompt, which read as the verb still
        # running (lab record, task 96).
        if getattr(sys, "frozen", False) and not args.detached:
            _attach_parent_console()
        from clockwork.owner import daemon

        return daemon.run(fake=args.serve_fake, output=args.output, library=args.library,
                          console=args.console, kept_root=args.kept,
                          errors_log=args.errors_log, contain=True)

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
                          limits_path=args.limits, routines=args.routines)

    if cli is not None and cli.is_verb(args.command):
        # As `serve`: a windowed build has no stdout of its own, and the answer belongs
        # in the terminal the verb was typed at (task 60).
        if getattr(sys, "frozen", False):
            _attach_parent_console()
        return cli.run(args)

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
