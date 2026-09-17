"""Write `src/clockwork/_commit.py`: the commit a built artefact was built from.

The PyInstaller build leaves the checkout behind, so `clockwork.built_commit()` can
only answer None for a frozen `.exe` unless something records the commit at build
time. This script does that, and records it as a *commit*: nothing here reads,
derives or touches a version, which is the arrangement `check_public.py`'s
`declared_versions` exists to protect (lab record, task 32; mirrors mainspring's
`tools/write_commit.py`, task 20).

Run from `tools/build_exe.ps1` before PyInstaller. The generated module is
gitignored and rewritten before every build.

Stdlib only: this runs before the venv's `clockwork` is necessarily importable
from the tree being built.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "src", "clockwork", "_commit.py")

_TEMPLATE = '''\
"""The commit this artefact was built from -- generated, gitignored, never edited.

Written by `tools/write_commit.py` at build time and imported defensively by
`clockwork.built_commit`, which prefers a checkout's live HEAD over this and falls
back to None when neither answers (lab record, task 32). A `-dirty` suffix means
the build tree carried uncommitted changes, so the sha names the commit the build
was *closest to* rather than one that reproduces it.
"""

COMMIT: str | None = {value!r}
'''

_VALUE = re.compile(r"^COMMIT: str \| None = (.+)$", re.MULTILINE)


def commit_of(root: str) -> str | None:
    """The short HEAD commit of the git repository rooted at exactly `root`, else None."""
    try:
        done = subprocess.run(
            ["git", "-C", root, "rev-parse", "--show-toplevel", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    lines = done.stdout.splitlines()
    if len(lines) != 2:
        return None
    toplevel, commit = lines[0].strip(), lines[1].strip()
    if not toplevel or not commit:
        return None
    if os.path.normcase(os.path.realpath(toplevel)) != os.path.normcase(os.path.realpath(root)):
        return None
    return commit


def is_dirty(root: str) -> bool:
    """Whether the checkout at `root` carries uncommitted changes.

    Ignored files are not changes (`--porcelain` leaves them out), and untracked
    files do not count either (`--untracked-files=no`) -- neither this generated
    module nor `dist/`, `build/` or the numba seed can make a build dirty.
    """
    try:
        done = subprocess.run(
            ["git", "-C", root, "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return bool(done.stdout.strip())


def read(target: str = TARGET) -> tuple[bool, str | None]:
    """`(the module is there, the commit it states)`; `(False, None)` when it is not."""
    try:
        with open(target, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return False, None
    found = _VALUE.search(text)
    if not found:
        return False, None
    try:
        value = ast.literal_eval(found.group(1))
    except (SyntaxError, ValueError):
        return False, None
    return True, value if isinstance(value, str) else None


def write(root: str = ROOT, target: str = TARGET) -> str | None:
    """Generate the module for the tree at `root` and return the commit it now states."""
    commit = commit_of(root)
    if commit is None:
        present, existing = read(target)
        if present:
            return existing
    elif is_dirty(root):
        commit += "-dirty"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_TEMPLATE.format(value=commit))
    return commit


def remove(target: str = TARGET) -> bool:
    """Delete the generated module; True if there was one. A checkout keeps none."""
    try:
        os.remove(target)
    except OSError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--remove", action="store_true",
                        help="delete the generated module instead of writing it")
    parser.add_argument("--quiet", action="store_true", help="say nothing on success")
    args = parser.parse_args(argv)

    if args.remove:
        gone = remove()
        if not args.quiet:
            print(f"{TARGET}: {'removed' if gone else 'nothing to remove'}")
        return 0
    commit = write()
    if not args.quiet:
        print(f"{TARGET}: COMMIT = {commit!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
