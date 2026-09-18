"""Stage the acquisition console's build output for the installer to carry.

`clockwork.acq.find_console` is how a running app locates the console beside itself
(`console/` next to the installation, a lab checkout otherwise, lab record task 50
decision 9); this script uses the same search at build time to find the console this
machine already built, and copies its executable, DLLs and `config.txt` into
`packaging/console_payload/` -- `tools/build_exe.ps1` then copies that directory
beside `clockwork.exe` in the onedir, so the installer's existing `[Files]` wildcard
(`packaging/clockwork.iss`) picks it up without a change of its own.

Nothing here builds the console: the lab repo's console notes have that recipe (lab
record, task 17). A public clone, or a machine that has not built the console yet, has
nothing for `find_console` to locate -- this prints why and leaves
`packaging/console_payload/` empty rather than failing, since a bare `clockwork.exe`
(no console payload) is still a useful build to make. `packaging/README.md` documents
when the payload is required.

The console's own CMake build writes `app.h` beside its executable with the version
and commit it was built from (`AqMD3_console_VERSION_S`, `GIT_COMMIT_HASH`); this
script copies that file too, and `tools/check_public.py` compares its commit against
`EXPECTED_CONSOLE_COMMIT` below, SKIPPED when no payload is staged. Bump the pin here
when the lab repo's console notes record a new commit (lab record, task 52); the two
are kept in agreement by this check, not by construction.

Run:  uv run tools/stage_console.py
"""

from __future__ import annotations

import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD_DIR = os.path.join(ROOT, "packaging", "console_payload")

# The commit the lab's console build tree (lab record, task 17) is expected to be
# built from, from `bushgroup/AqMD3-Acquisition-Console`'s `clockwork` branch -- a
# public fork, so the commit hash is a derived fact and not vendor material (CLAUDE.md,
# "derived facts only, never vendor documents"). The lab repo's console notes carry the
# copy of record; this is the copy `check_public.py` can actually check the staged
# build against.
EXPECTED_CONSOLE_COMMIT = "795fef6"

# What travels into the installer: the executable, every DLL beside it, the config.txt
# template and app.h (the version/commit record the check above reads). Not the .pdb
# (5-6 MB of debug symbols nobody installed needs), not logs/ (a running console's own
# output), not any other build-tree leftover.
_PAYLOAD_SUFFIXES = (".exe", ".dll")
_PAYLOAD_NAMES = ("config.txt", "app.h")


def _payload_files(console_dir: str) -> list[str]:
    names = sorted(os.listdir(console_dir))
    return [
        name for name in names
        if os.path.isfile(os.path.join(console_dir, name))
        and (name.endswith(_PAYLOAD_SUFFIXES) or name in _PAYLOAD_NAMES)
    ]


def stage(console_dir: str, payload_dir: str = PAYLOAD_DIR) -> list[str]:
    """Copy the payload files from `console_dir` into `payload_dir`, replacing it."""
    shutil.rmtree(payload_dir, ignore_errors=True)
    os.makedirs(payload_dir, exist_ok=True)
    copied = []
    for name in _payload_files(console_dir):
        shutil.copy2(os.path.join(console_dir, name), os.path.join(payload_dir, name))
        copied.append(name)
    return copied


_COMMIT_DEFINE = re.compile(r'^\s*#define\s+GIT_COMMIT_HASH\s+"([^"]*)"', re.MULTILINE)
_VERSION_DEFINE = re.compile(r'^\s*#define\s+AqMD3_console_VERSION_S\s+"([^"]*)"', re.MULTILINE)


def staged_commit(payload_dir: str = PAYLOAD_DIR) -> str | None:
    """The commit `app.h` in an already-staged payload names, or None."""
    app_h = os.path.join(payload_dir, "app.h")
    try:
        with open(app_h, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    found = _COMMIT_DEFINE.search(text)
    return found.group(1) if found else None


def staged_version(payload_dir: str = PAYLOAD_DIR) -> str | None:
    """The version `app.h` in an already-staged payload names, or None."""
    app_h = os.path.join(payload_dir, "app.h")
    try:
        with open(app_h, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    found = _VERSION_DEFINE.search(text)
    return found.group(1) if found else None


def main() -> int:
    from clockwork.acq import find_console

    exe = find_console()
    if exe is None:
        print(
            "No acquisition console build found (checked $CLOCKWORK_CONSOLE, a "
            "console/ directory beside this checkout, and the lab repo's console/). "
            "Leaving packaging/console_payload/ empty -- the next build will carry "
            "clockwork alone. See the lab repo's console notes (lab record, task 17) "
            "to build one."
        )
        shutil.rmtree(PAYLOAD_DIR, ignore_errors=True)
        return 0

    console_dir = os.path.dirname(exe)
    copied = stage(console_dir)
    if not copied:
        print(f"Found {exe} but nothing beside it matched the payload list; "
              f"packaging/console_payload/ is empty.")
        return 1
    print(f"Staged {len(copied)} files from {console_dir} into {PAYLOAD_DIR}:")
    for name in copied:
        print(f"  {name}")

    commit = staged_commit()
    version = staged_version()
    if commit is None:
        print("app.h was not among the files copied (or named no commit); "
              "check_public.py will skip the console version check.")
    elif commit != EXPECTED_CONSOLE_COMMIT:
        print(
            f"WARNING: staged console is commit {commit!r} (version {version}), "
            f"but this script pins {EXPECTED_CONSOLE_COMMIT!r}. Update "
            "EXPECTED_CONSOLE_COMMIT here once the lab repo's console notes agree, "
            "or check_public.py will fail on this build."
        )
    else:
        print(f"Console commit {commit} (version {version}) matches the pin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
