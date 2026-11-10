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

__version__ = "1.2.0rc11"

# The library emits records and configures nothing: no handler, no level, no
# format. `clockwork.transcript.to_file` is the only thing that attaches one,
# and a caller who wants the records elsewhere attaches their own handler to
# `clockwork` or to one of the four names `transcript.LOGGERS` documents.
logging.getLogger(__name__).addHandler(logging.NullHandler())

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
"""The repository root when running from a checkout (the parent of src/)."""

# The lab repo's directories a session may need, by name. Nothing in src/ hard
# codes a lab-side path; everything goes through lab_dir() or instrument_dir().
# `golden` here is the lab's founding experiments, the fixtures the tests read; the
# method library the daemon serves is the instrument's `golden` (instrument_dir).
_LAB_SUBDIRS = ("tasks", "notes", "explorations", "golden", "vendor", "falkor", "console")

INSTRUMENT_ENV = "CLOCKWORK_INSTRUMENT"
"""An environment variable naming the instrument directory in place of the sibling."""

INSTRUMENT_SIBLING = "slimphony"
"""The instrument directory's name beside this checkout, when the variable is not set."""

INSTRUMENT_FILE = "instrument.toml"
"""The instrument document at the instrument directory's root, which is also what
recognises the directory (`docs/instrument-file-format.md`); the standing limits are
the `limits.toml` beside it (`clockwork.envelope.default_path`)."""

# The instrument directory's directories, by name: the method library and the routines
# beside it (`clockwork.routine.default_directory`).
_INSTRUMENT_SUBDIRS = ("golden", "routines")


def lab_dir(name: str | None = None) -> str | None:
    """Resolve the private lab repo, or one of its top-level directories.

    The lab repo is clockwork's development record: the console build, vendor material
    and the founding experiments the tests read. What the daemon serves -- the method
    library, routines, limits and instrument document -- is the instrument's
    (`instrument_dir`). Order: `$CLOCKWORK_LAB`, then this repo's own root (a lab checkout that
    contains the code), then the sibling `../clockwork-lab`. Returns None when
    nothing resolves -- a public clone has no lab material and every public
    code path must work without it. An unknown name is refused whether or not a lab
    repo resolves, so a public clone refuses it as a lab checkout does.
    """
    if name is not None and name not in _LAB_SUBDIRS:
        raise ValueError(f"unknown lab directory {name!r}; one of {_LAB_SUBDIRS}")
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
        path = os.path.join(root, name)
        return path if os.path.isdir(path) else None
    return None


def instrument_dir(name: str | None = None) -> str | None:
    """Resolve the instrument directory, or one of its top-level directories.

    The instrument directory holds what belongs to one instrument rather than to
    clockwork: the instrument document, the standing limits beside it, the method
    library (`golden`) and the routines. `$CLOCKWORK_INSTRUMENT` when it is set, and
    then nothing else: a variable naming the wrong folder resolves nothing rather than
    quietly serving another instrument's material (`instrument_unresolved` is the
    sentence for it). Otherwise the sibling
    `../slimphony` beside this checkout, which an installed clockwork does not have, so
    an instrument PC sets the variable. Recognised by the instrument document at its
    root (`INSTRUMENT_FILE`), never by a task system, which a project's repository has
    too. Returns None when nothing resolves: a public clone has no instrument beside
    it, and every code path that defaults to one must work without it.
    """
    if name is not None and name not in _INSTRUMENT_SUBDIRS:
        raise ValueError(f"unknown instrument directory {name!r}; one of "
                         f"{_INSTRUMENT_SUBDIRS}")
    named = _instrument_named()
    candidates = ([named] if named else
                  [os.path.abspath(os.path.join(ROOT, "..", INSTRUMENT_SIBLING))])
    for root in candidates:
        if not root or not os.path.isfile(os.path.join(root, INSTRUMENT_FILE)):
            continue
        root = os.path.abspath(root)
        if name is None:
            return root
        path = os.path.join(root, name)
        return path if os.path.isdir(path) else None
    return None


def _instrument_named() -> str:
    """`$CLOCKWORK_INSTRUMENT` as it was meant, or the empty string when it is not set.

    Surrounding whitespace and one pair of quotes are taken off: `set NAME="D:\\x"` in
    cmd.exe keeps the quotes in the value and a pasted value keeps its trailing space,
    and either would name a folder that is not there.
    """
    value = (os.environ.get(INSTRUMENT_ENV) or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def instrument_unresolved(*, when_unset: bool = True) -> str:
    """Why no instrument directory resolves, in one sentence that names the variable;
    the empty string when one does.

    Two cases read differently. The variable set and naming nothing is a mistake on this
    PC, and the sentence says what the value is and what is wrong with it, so a caller
    says it wherever it starts (the daemon's log, `clockwork mcp`'s first line, a verb's
    standard error). The variable unset and no sibling is a public clone or an installed
    build nobody has pointed at an instrument: ordinary, and said only where something
    then cannot be done for want of a library or limits, so `when_unset` false answers
    the empty string for it.
    """
    if instrument_dir() is not None:
        return ""
    named = _instrument_named()
    if not named and not when_unset:
        return ""
    if named:
        wrong = (f"holds no {INSTRUMENT_FILE}" if os.path.isdir(named)
                 else "is not a folder on this PC")
        return (f"{INSTRUMENT_ENV} is set to {named!r}, which {wrong}, so no instrument "
                "directory is in use: no library, instrument document or limits are found "
                "by default until it names the folder that holds "
                f"{INSTRUMENT_FILE}")
    return (f"no instrument directory was found: set {INSTRUMENT_ENV} to the folder that "
            f"holds {INSTRUMENT_FILE} (a checkout also finds ../{INSTRUMENT_SIBLING} "
            "beside it)")


def instrument_file() -> str | None:
    """The instrument document at the instrument directory's root, or None."""
    root = instrument_dir()
    return os.path.join(root, INSTRUMENT_FILE) if root is not None else None


def library_dirs(value: str) -> tuple[str, ...]:
    """The directories a library setting names, absolute, in the order they are searched.

    A library setting (`--library`, the daemon's `hello`, the window's) is one directory
    or several joined by `os.pathsep` -- `;` on Windows -- so a project's own templates
    can sit beside the instrument's library. Empty parts are dropped, and a directory
    named twice is kept once, where it first appears.
    """
    found: list[str] = []
    seen: set[str] = set()
    for part in (value or "").split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        path = os.path.abspath(part)
        # The directory itself rather than its spelling: one folder named by its long
        # and its 8.3 short name, or through a link, is one library, and kept as two
        # every document in it would be its own duplicate.
        key = os.path.normcase(os.path.realpath(path))
        if key not in seen:
            seen.add(key)
            found.append(path)
    return tuple(found)


def library_setting(directories: tuple[str, ...] | list[str]) -> str:
    """`library_dirs`'s inverse: the directories as one setting."""
    return os.pathsep.join(directories)


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


__all__ = ["INSTRUMENT_ENV", "INSTRUMENT_FILE", "INSTRUMENT_SIBLING", "ROOT", "__version__",
           "built_commit", "commit_of", "instrument_dir", "instrument_file",
           "instrument_unresolved", "lab_dir", "library_dirs", "library_setting"]
