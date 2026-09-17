"""The PySide6 window: one pane per box, Send all, Acquire, Replicate.

The only package in `clockwork` that imports Qt. Today `main` is the placeholder task
32 asked for -- one label naming the build, and a `--self-check` that proves the three
lower layers and `mainspring.uimf` work inside a frozen build without opening it -- and
task 08's successors (`notes/gui-execution-plan.md`) fill it in.
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
    parser = argparse.ArgumentParser(prog="clockwork")
    parser.add_argument(
        "--self-check", action="store_true",
        help="run a hardware-free stand-in acquisition and exit, no window shown",
    )
    args = parser.parse_args(argv)

    # Before anything imports numba, lazily or otherwise: `_self_check` folds, and the
    # real window will too, and both need the cache pointed somewhere that survives a
    # rebuild before that first import happens.
    os.environ.setdefault("NUMBA_CACHE_DIR", _numba_cache_dir())
    os.makedirs(os.environ["NUMBA_CACHE_DIR"], exist_ok=True)
    _seed_numba_cache(os.environ["NUMBA_CACHE_DIR"])

    import clockwork

    if sys.platform == "win32":
        # `console=True` (see `packaging/clockwork.spec`) means this process owns a
        # console window as well as the one Qt shows below, and Windows titles a
        # console "<path to the exe>" until something says otherwise -- which is also
        # what a build that crashed before reaching this line leaves it as. Naming it
        # here is what lets `tools/build_exe.ps1`'s launch check tell "reached working
        # code" from "died on the way here" without caring which of the two windows it
        # happens to see first.
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(f"clockwork {clockwork.__version__}")

    if args.self_check:
        return _self_check()

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QLabel

    app = QApplication(sys.argv[:1])
    if os.path.isfile(_ICON):
        app.setWindowIcon(QIcon(_ICON))
    commit = clockwork.built_commit() or "unknown commit"
    label = QLabel(f"clockwork {clockwork.__version__}\n{commit}")
    label.setWindowTitle(f"clockwork {clockwork.__version__}")
    label.setMargin(24)
    label.show()
    return app.exec()
