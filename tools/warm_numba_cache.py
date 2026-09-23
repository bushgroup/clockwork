"""Pre-compile mainspring's UIMF kernels and seed a cache to bundle into clockwork.exe.

clockwork depends on `mainspring[fast]` for the fold's decode (lab record, task 34) and,
since mainspring 1.7.0, for its write too: `UimfWriter.write_scans` encodes the summed
frame through `decode.encode_frame_blobs`, three more kernels. A fresh process pays
2.5-4.7 s compiling the four decode kernels when no on-disk numba cache exists yet,
against 0.9-1.4 s once one does (mainspring's task 04), and the encoders add their own
compile on top. Unlike mainspring's own build, that cost lands inside clockwork's first
fold rather than at window-open, because both sets are first touched on the folding
thread, not at launch -- but it is the same fix, moved here from `tools/write_commit.py`'s
sibling in mainspring (task 20, task 07): pay it once, at build time, rather than during
the first acquisition.

Writes compiled kernels for every intensity dtype the format uses (ADC int32, TDC int16,
FOLDED float32; `mainspring.uimf.decode.INTENSITY_DTYPES`) to
`packaging/numba_cache_seed/`, which `packaging/clockwork.spec` bundles as data and
`clockwork.app._seed_numba_cache` copies into the real per-user `NUMBA_CACHE_DIR` the
first time it finds that directory empty. `tools/build_exe.ps1` runs this before every
build; run it by hand only to inspect or refresh the seed on its own.

Run:  uv run tools/warm_numba_cache.py
"""

from __future__ import annotations

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SEED_DIR = os.path.join(ROOT, "packaging", "numba_cache_seed")


def main() -> int:
    # Numba reads NUMBA_CACHE_DIR at import time, so it must be set before the first
    # `import numba` -- which `mainspring.uimf.decode` does lazily, on first call.
    shutil.rmtree(SEED_DIR, ignore_errors=True)
    os.makedirs(SEED_DIR, exist_ok=True)
    os.environ["NUMBA_CACHE_DIR"] = SEED_DIR

    import numpy as np
    from mainspring.uimf import decode

    if not decode.numba_available():
        print("numba is not importable in this environment; nothing to warm.")
        return 1

    # One compiled specialisation per element type: the fold's fill kernel's output
    # array is dtype-specific, so each of the three the format uses needs its own compile.
    for type_name in sorted(decode.INTENSITY_DTYPES):
        dtype = decode.dtype_for(type_name)
        bin_index = np.array([0, 3, 500, 4096], dtype=np.int64)
        intensity = np.array([1, 2, 3, 4], dtype=dtype)
        # The encoders with the argument types `write_scans` passes them: an int64 row
        # pointer and an int64 bin index over a CSR block of several scans, one empty,
        # with the intensities already in the element type. Anything else would compile
        # a specialisation the fold never asks for and leave the one it does cold.
        scan_start = np.array([0, 4, 4, 8], dtype=np.int64)
        blobs = decode.encode_frame_blobs(
            scan_start, np.concatenate([bin_index, bin_index]),
            np.concatenate([intensity, intensity]), dtype, backend="numba",
        )
        assert blobs[1] == b"" and blobs[0] == blobs[2]
        blob = decode.encode_intensities(bin_index, intensity, dtype, backend="pure")
        assert blob == blobs[0]
        counts, bins_out, values_out = decode.decode_frame_blobs(
            [blob, None, blob], dtype=dtype
        )
        assert values_out.dtype == dtype and int(counts.sum()) == 2 * bin_index.size
        print(f"warmed {type_name} ({dtype})")

    written = [
        os.path.join(dirpath, name)
        for dirpath, _, names in os.walk(SEED_DIR)
        for name in names
    ]
    if not written:
        print(f"numba compiled with no on-disk output under {SEED_DIR}; nothing to bundle.")
        return 1
    print(f"{len(written)} cache files written to {SEED_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
