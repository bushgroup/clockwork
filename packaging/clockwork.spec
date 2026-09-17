# PyInstaller build spec for the placeholder clockwork.app (task 32).
#
# Run through `tools/build_exe.ps1`, not `pyinstaller` directly, so the working and dist
# paths land under `dist/`/`build/` (both gitignored) regardless of the caller's cwd.
#
# The excludes below are PySide6 submodules clockwork never imports -- WebEngine,
# Qml/Quick, Multimedia, the 3D and remaining device-facing modules -- cut because
# PySide6 ships every Qt module in one wheel and PyInstaller's default Qt hook
# otherwise pulls each one's own native Qt libraries in with it (mainspring's
# `packaging/mainspring.spec`, task 07). `mainspring.uimf.decode`'s numba path and
# PySide6 itself are covered by `pyinstaller-hooks-contrib`'s `hook-numba.py` and
# PyInstaller's own Qt hook utility; neither needs a hand-written hook here.
#
# Builds onedir only, for the same reason mainspring does: an Inno Setup installer
# wants a folder to copy, not a single .exe that extracts on every start
# (`../mainspring-lab/notes/packaging.md`, task 07).

import os

from PyInstaller.building.datastruct import Tree
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None
# SPECPATH is injected by PyInstaller into this file's exec namespace; resolving the
# entry script against it means the build works from any cwd, not just this directory.
ENTRYPOINT = os.path.join(SPECPATH, "entrypoint.py")

# `clockwork.app` sets this on its own windows, so it has to travel with the package for
# a source run as well as for this build, and `collect_data_files("clockwork")` below
# already brings it into the bundle. This is the same file, handed to the Windows
# resource section of the .exe. `tools/make_icon.py` regenerates it from packaging/icon/.
ICON = os.path.join(
    os.path.dirname(SPECPATH), "src", "clockwork", "app", "resources", "clockwork.ico"
)
if not os.path.isfile(ICON):
    raise RuntimeError(f"{ICON} is missing -- run `uv run tools/make_icon.py`.")

# `clockwork.app._seed_numba_cache` copies this into NUMBA_CACHE_DIR on a frozen build's
# first launch, so the first-fold JIT cost (task 32's progress log; mainspring's
# equivalent is a first-launch cost, task 07) is paid at build time instead. Required,
# not optional: an unwarmed build would still work, just slowly on its first fold, and
# that regression should fail the build rather than ship quietly.
SEED_DIR = os.path.join(SPECPATH, "numba_cache_seed")
if not os.path.isdir(SEED_DIR) or not os.listdir(SEED_DIR):
    raise RuntimeError(
        "packaging/numba_cache_seed is missing or empty -- run "
        "`uv run tools/warm_numba_cache.py` before building (lab record, task 32)."
    )
NUMBA_SEED_DATAS = Tree(SEED_DIR, prefix="numba_cache_seed")

# The commit this build is built from, written by `tools/write_commit.py` into the
# package itself so that a frozen clockwork can still say what code it is (mirrors
# mainspring's task 20). It reaches the bundle as an ordinary import --
# `clockwork.built_commit` does `from ._commit import COMMIT` -- rather than through
# `collect_data_files`, which collects data and not a `.py`.
COMMIT_MODULE = os.path.join(os.path.dirname(SPECPATH), "src", "clockwork", "_commit.py")
if not os.path.isfile(COMMIT_MODULE):
    raise RuntimeError(
        f"{COMMIT_MODULE} is missing -- run `uv run tools/write_commit.py` before "
        "building (lab record, task 32). `tools/build_exe.ps1` does this for you."
    )

PYSIDE6_EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtWebView",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickTest",
    "PySide6.QtQuickWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSpatialAudio",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtGraphsWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtSensors",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtRemoteObjects",
    "PySide6.QtNetworkAuth",
    "PySide6.QtHttpServer",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtHelp",
    "PySide6.QtDesigner",
    "PySide6.QtUiTools",
    "PySide6.QtAxContainer",
    "PySide6.QtCanvasPainter",
    "PySide6.QtTextToSpeech",
    "PySide6.QtDBus",
    # pyqtgraph comes in transitively through mainspring's own viewer dependency,
    # but nothing in clockwork imports it (mainspring is the only viewer, Matt,
    # 2026-09-10; clockwork.app draws nothing) -- confirmed excluded cleanly, 0
    # files, once task 32 dropped it as a direct dependency too.
    "pyqtgraph",
]
# `clockwork.mips.transport` reads a box's COM port through pyserial's `list_ports`,
# which is a Windows-only ctypes path (`serial.tools.list_ports_windows`); the two
# other platform variants are dead code on the only OS this ships for (CLAUDE.md,
# "Windows first").
PYSERIAL_EXCLUDES = [
    "serial.tools.list_ports_linux",
    "serial.tools.list_ports_osx",
]

a = Analysis(
    [ENTRYPOINT],
    pathex=[],
    binaries=[],
    datas=collect_data_files("clockwork"),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=PYSIDE6_EXCLUDES + PYSERIAL_EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
# Tree() yields the 3-entry TOC form (dest, src, typecode), unlike Analysis(datas=...)'s
# 2-entry (src, dest_dir) form, so it is appended to the already-built TOC rather than
# passed into Analysis itself.
a.datas += NUMBA_SEED_DATAS

# --- Provenance guard --------------------------------------------------------------------
# PyInstaller resolves each collected binary's DLL dependencies by searching the binary's
# own directory, then sys.path, then PATH. A PATH carrying another Python distribution
# substitutes its DLLs quietly -- mainspring's task 07 measured anaconda3/Library/bin's
# ICU shadowing Windows's own icuuc.dll this way, killing every launch importing QtCore.
# `tools/build_exe.ps1` strips PATH down to the Windows directories and uv's for the
# build; this refuses to ship anything that still gets through. Legitimate sources are
# exactly the venv, the interpreter it was created from, and this repo.
import sys

def _root(path):
    return os.path.normcase(os.path.realpath(path)).rstrip(os.sep) + os.sep

ALLOWED_ROOTS = tuple(_root(p) for p in (sys.prefix, sys.base_prefix, os.path.dirname(SPECPATH)))

def _foreign(toc):
    for entry in toc:
        src = os.path.normcase(os.path.realpath(entry[1]))
        if not src.startswith(ALLOWED_ROOTS):
            yield entry

foreign = list(_foreign(a.binaries)) + list(_foreign(a.datas))
if foreign:
    listing = "\n".join(f"  {dest}  <-  {src}" for dest, src, *_ in foreign)
    raise RuntimeError(
        "Refusing to build: these files come from outside the venv, its base interpreter and "
        "this repo (a foreign directory on PATH, most likely another Python distribution):\n"
        + listing
    )

if not any(entry[0] == "clockwork._commit" for entry in a.pure):
    raise RuntimeError(
        "Refusing to build: clockwork._commit is not in the bundle, so the .exe would "
        "report no commit. It exists on disk (checked above), so the Analysis did not "
        "follow the import (lab record, task 32)."
    )

# --- One C++ runtime at the bundle root ---------------------------------------------------
# numba's and llvmlite's extension modules import MSVCP140.dll by name and get whichever
# copy PyInstaller found first; Windows then reuses that already-loaded copy for Qt6Core,
# which needs the newer runtime PySide6 ships with (mainspring's task 07). Pin the root
# copy to PySide6's, the newest in the venv, so every importer in the bundle shares one
# runtime that is at least as new as anything expects.
import PySide6

_pyside_msvcp = os.path.join(os.path.dirname(PySide6.__file__), "MSVCP140.dll")
if not os.path.isfile(_pyside_msvcp):
    raise RuntimeError(f"PySide6 no longer ships MSVCP140.dll at {_pyside_msvcp}; revisit this pin.")
a.binaries = [e for e in a.binaries if e[0].lower() != "msvcp140.dll"] + [
    ("MSVCP140.dll", _pyside_msvcp, "BINARY")
]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_exe_common = dict(
    name="clockwork",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    # Windowed (console=False, mainspring's choice) redirects stdout/stderr to nowhere,
    # which would swallow --self-check's own report -- the one thing this placeholder
    # exists to run. Console stays on while --self-check does the packaging chain's
    # talking; task 50's real window is free to turn it off once nothing needs it.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_exe_common)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[], name="clockwork")
