"""Seed the built clockwork.exe with its own compiled UIMF kernels, so an install compiles nothing.

Run after PyInstaller and before Inno Setup; `tools/build_exe.ps1` does. It starts
`dist\\clockwork\\clockwork.exe --warm-numba-cache <dir>` with `<dir>` the bundle's
`_internal\\numba_cache_seed`, waits for it, and checks that every kernel landed in the one
folder the installed program will look in, `clockwork_uimf` (mainspring's
`decode.frozen_cache_folder`). `clockwork.app._seed_numba_cache` copies that seed into
the per-user cache the first time each executable runs.

clockwork depends on `mainspring[fast]` for the fold's decode (lab record, task 34) and,
since mainspring 1.7.0, for its write too, and since 1.9.0 for its sum; the kernels are
mainspring's (`decode.warm_kernels`: four decode, three encode, the sum, for all three
intensity types). Unseeded, their compile lands inside the first fold, on the folding
thread.

Why the built executable and not this interpreter. numba stamps a frozen program's cache
with `sys.executable`'s modification time and size, and a cache compiled from the source
tree is stamped with a source file and filed under a hash of its path, so an installed
copy never read the seed this script used to write (mainspring's lab record, task 34).
Inno Setup rounds every installed file's modification time down to an even second, so the
executable is rounded first and the installer has nothing left to change.

Run:  uv run tools/warm_numba_cache.py
"""

from __future__ import annotations

import os
import pickle
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
EXE = os.path.join(ROOT, "dist", "clockwork", "clockwork.exe")
SEED_DIR = os.path.join(ROOT, "dist", "clockwork", "_internal", "numba_cache_seed")
FOLDER = "clockwork_uimf"
KERNELS = ("decode._k_lzf_sizes", "decode._k_lzf_expand", "decode._k_rlz_count",
           "decode._k_rlz_fill", "decode._k_rlz_encode_sizes", "decode._k_rlz_encode_fill",
           "decode._k_lzf_compress", "frame._k_sum_rows")


def _stamp_of(index_path: str) -> tuple:
    """The executable stamp numba pickled into one index file, as it reads it back."""
    with open(index_path, "rb") as fh:
        pickle.load(fh)  # numba's version
        stamp, _ = pickle.loads(fh.read())
    return tuple(stamp)


def main() -> int:
    if not os.path.isfile(EXE):
        print(f"{EXE} does not exist; build it first (tools/build_exe.ps1).")
        return 1
    st = os.stat(EXE)
    os.utime(EXE, (st.st_atime, float(int(st.st_mtime) // 2 * 2)))
    shutil.rmtree(SEED_DIR, ignore_errors=True)
    os.makedirs(SEED_DIR)
    # The executable is windowed, so the exit status and the files it leaves are the
    # whole answer.
    status = subprocess.run([EXE, "--warm-numba-cache", SEED_DIR], timeout=600).returncode
    if status != 0:
        print(f"{EXE} --warm-numba-cache exited {status}.")
        return 1
    folder = os.path.join(SEED_DIR, FOLDER)
    names = os.listdir(folder) if os.path.isdir(folder) else []
    missing = [k for k in KERNELS if not any(n.startswith(k + "-") and n.endswith(".nbi")
                                             for n in names)]
    stray = [n for n in os.listdir(SEED_DIR) if n != FOLDER]
    if missing or stray:
        print(f"seed incomplete: missing {missing or 'nothing'}, unexpected {stray or 'nothing'}")
        return 1
    stamps = {_stamp_of(os.path.join(folder, n)) for n in names if n.endswith(".nbi")}
    want = (os.stat(EXE).st_mtime, os.stat(EXE).st_size)
    if stamps != {want}:
        print(f"seed stamped {sorted(stamps)}, but the executable is {want}")
        return 1
    print(f"{len(names)} cache files for {len(KERNELS)} kernels in {folder}, stamped {want}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
