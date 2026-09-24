"""clockwork: control software for a SLIMPHONY-style ion mobility mass spectrometer.

Three layers under one seam, none of which imports Qt:

    mips      USB-serial link to MIPS controller boxes: send command and table
              strings, parse ACK/NAK and the asynchronous table-status lines
    acq       ZeroMQ client to PNNL's AqMD3 acquisition console for the SA220P
              digitizer, plus creation of the UIMF file it appends scans to
    method    the saved experiment: per-box strings and acquisition settings,
              stamped into every acquisition as provenance
    instrument the machine rather than the experiment: the m/z calibration and
              the digitizer's vertical settings, which no method carries

and `app`, the PySide6 window on top of them. Anything that imports the three
lower layers must keep working with no GUI stack installed, which is what lets
a script drive one box and what `tools/check_public.py` exercises.

`transcript` is a fifth module and is not a layer: it is the one helper that
turns the wire loggers the three lower layers emit to into a file beside a run's
results. Importing clockwork configures no logging at all.
"""

from __future__ import annotations

import logging
import os
import subprocess

__version__ = "1.1.0rc1"

# The library emits records and configures nothing: no handler, no level, no
# format. `clockwork.transcript.to_file` is the only thing that attaches one,
# and a caller who wants the records elsewhere attaches their own handler to
# `clockwork` or to one of the four names `transcript.LOGGERS` documents.
logging.getLogger(__name__).addHandler(logging.NullHandler())

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
"""The repository root when running from a checkout (the parent of src/)."""

# The lab repo's directories a session may need, by name. Nothing in src/ hard
# codes a lab-side path; everything goes through lab_dir().
_LAB_SUBDIRS = ("tasks", "notes", "explorations", "golden", "vendor", "falkor", "console")


def lab_dir(name: str | None = None) -> str | None:
    """Resolve the private lab repo, or one of its top-level directories.

    Order: `$CLOCKWORK_LAB`, then this repo's own root (a lab checkout that
    contains the code), then the sibling `../clockwork-lab`. Returns None when
    nothing resolves -- a public clone has no lab material and every public
    code path must work without it.
    """
    candidates = [
        os.environ.get("CLOCKWORK_LAB"),
        ROOT,
        os.path.abspath(os.path.join(ROOT, "..", "clockwork-lab")),
    ]
    for root in candidates:
        if not root or not os.path.isdir(root):
            continue
        # A lab checkout is recognised by its task system, not by its name.
        if not os.path.isfile(os.path.join(root, "tasks", "README.md")):
            continue
        if name is None:
            return root
        if name not in _LAB_SUBDIRS:
            raise ValueError(f"unknown lab directory {name!r}; one of {_LAB_SUBDIRS}")
        path = os.path.join(root, name)
        return path if os.path.isdir(path) else None
    return None


def commit_of(repo: str) -> str | None:
    """The short HEAD commit of the git repository rooted at exactly `repo`, else None.

    Git searches parent directories, so asking it for a commit inside an installed
    package would otherwise answer for whatever checkout happens to enclose it; the
    toplevel is read in the same call and the commit kept only when that toplevel is
    `repo` itself. Never raises: a wheel install, the packaged `.exe`, a directory
    inside an unrelated repository, or a machine with no `git` all get None.
    """
    try:
        done = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--show-toplevel", "--short", "HEAD"],
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
    if os.path.normcase(os.path.realpath(toplevel)) != os.path.normcase(os.path.realpath(repo)):
        return None
    return commit


def built_commit() -> str | None:
    """The commit clockwork is running from: the live checkout, else the commit a
    frozen `.exe` or wheel was built from (`tools/write_commit.py`'s generated
    `_commit.py`, gitignored and absent in a checkout), else None.
    """
    try:
        from ._commit import COMMIT as _built  # generated at build time
    except ImportError:
        _built = None
    return commit_of(ROOT) or _built


__all__ = ["ROOT", "__version__", "built_commit", "commit_of", "lab_dir"]
