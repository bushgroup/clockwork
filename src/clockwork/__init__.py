"""clockwork: control software for a SLIMPHONY-style ion mobility mass spectrometer.

Three layers under one seam, none of which imports Qt:

    mips      USB-serial link to MIPS controller boxes: send command and table
              strings, parse ACK/NAK and the asynchronous table-status lines
    acq       ZeroMQ client to PNNL's AqMD3 acquisition console for the SA220P
              digitizer, plus creation of the UIMF file it appends scans to
    method    the saved experiment: per-box strings and acquisition settings,
              stamped into every acquisition as provenance

and `app`, the PySide6 window on top of them. Anything that imports the three
lower layers must keep working with no GUI stack installed, which is what lets
a script drive one box and what `tools/check_public.py` exercises.
"""

from __future__ import annotations

import os

__version__ = "0.1.0"

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


__all__ = ["ROOT", "__version__", "lab_dir"]
