r"""A copy of a failed or reported run's files, kept where tidying a folder cannot reach it.

A run's own files belong to the trainee: they delete a bad run so its stem can be used
again, and a transcript named for a stem and a date is appended to by the re-run. Both
are ordinary and both destroy the evidence behind a report. The first report filed
through the window (lab #1) pointed at files that had been deleted, and at a transcript
that by the time it was read described the re-run (lab record, task 81). So the copy is
made **when the failure or the report happens, on the PC that ran it**: copying later,
when someone reads the report, is too late by construction.

**One folder per report id, under one root.** The id is `R-YYYYMMDD-HHMMSS-<PC>`, made
here, because the number a tracker gives a report does not exist yet when clockwork acts
and clockwork holds no tracker credentials. A report's URL carries the id; whoever reads
the tracker matches ids to reports afterwards and may rename a folder, and a folder whose
whole name is no longer a bare id is theirs: `prune` never touches it.

**Copies, not links**, UIMF included, and not zipped: a UIMF is most of a folder and
hardly compresses, and a hard link is the same file the trainee then deletes or appends
to. Each folder carries `manifest.txt`: why it was kept, the version, the PC, the time,
and one row per file with its original path, size and SHA-256, so a copy can be checked
against what it claims to be long after the original has gone.

**The root comes from configuration**: `$CLOCKWORK_REPORTS`, then the window's setting,
then `%LOCALAPPDATA%\clockwork\reports`. Qt-free, and it never looks up the window's
error log itself: a caller that has one passes its path.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import os
import platform
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field

__all__ = [
    "ENV",
    "MANIFEST",
    "RETENTION_DAYS",
    "Manifest",
    "Row",
    "add",
    "default_root",
    "for_report",
    "is_report_id",
    "keep",
    "kept_for",
    "new_id",
    "prune",
    "read_manifest",
    "root",
    "run_files",
    "stem_of",
    "write_manifest",
]

ENV = "CLOCKWORK_REPORTS"
"""The environment variable that names the root, ahead of any setting."""

MANIFEST = "manifest.txt"

RETENTION_DAYS = 90
"""How long a folder no report has claimed is kept."""

_ID = re.compile(r"R-\d{8}-\d{6}-[A-Za-z0-9-]+\Z")

_PC_OK = re.compile(r"[^A-Za-z0-9-]+")

_TRANSCRIPT = re.compile(r"(?P<stem>.+)-\d{4}-\d{2}-\d{2}\.transcript\.log\Z")


def default_root() -> str:
    r"""`%LOCALAPPDATA%\clockwork\reports`, or `~/.cache` off Windows, beside the
    error log."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return os.path.join(base, "clockwork", "reports")


def root(setting: str = "") -> str:
    """The root in force: `$CLOCKWORK_REPORTS`, then `setting`, then `default_root()`."""
    return os.environ.get(ENV, "").strip() or setting.strip() or default_root()


def is_report_id(name: str) -> bool:
    """Whether a folder's whole name is a bare report id, which is what `prune` may
    remove. A folder renamed by anything else no longer is one."""
    return _ID.match(name) is not None


def new_id(root_dir: str, *, pc: str = "", when: _dt.datetime | None = None) -> str:
    """`R-YYYYMMDD-HHMMSS-<PC>`, with `-2`, `-3`... if that folder already exists.

    The PC name is cut to letters, digits and hyphens, so the id is a folder name on any
    file system and a word a tracker's search finds whole.
    """
    moment = when or _dt.datetime.now()
    machine = _PC_OK.sub("", pc or platform.node()) or "PC"
    base = f"R-{moment:%Y%m%d-%H%M%S}-{machine}"
    candidate, count = base, 1
    while os.path.exists(os.path.join(root_dir, candidate)):
        count += 1
        candidate = f"{base}-{count}"
    return candidate


def stem_of(transcript_path: str) -> str:
    """The stem a `<stem>-<date>.transcript.log` belongs to, else the empty string."""
    found = _TRANSCRIPT.match(os.path.basename(transcript_path or ""))
    return found["stem"] if found else ""


def run_files(directory: str, stem: str) -> list[str]:
    """Every file in `directory` that belongs to `stem`, sorted.

    A run's files are the stem followed by a `.` or a `-`: `<stem>.uimf`,
    `<stem>.summed.uimf`, `<stem>.sent.txt` and `<stem>-<date>.transcript.log`, and a
    SQLite journal left beside a file. That rule, not a list of suffixes, so that a file
    the run left and nobody listed here is kept too; and it leaves out `<stem>0.uimf`,
    which is the next run's, and a `Sample ID.txt` the trainee wrote themselves.
    """
    if not directory or not stem:
        return []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    found = [os.path.join(directory, name) for name in names
             if name.startswith(stem) and name[len(stem):len(stem) + 1] in (".", "-")]
    return sorted(path for path in found if os.path.isfile(path))


@dataclass
class Row:
    """One file in a manifest."""

    status: str
    """`copied`, `missing`, or `failed: <why>`."""
    size: str
    sha256: str
    kept: str
    """The copy's name in the folder, or `-`."""
    original: str

    def line(self) -> str:
        return "\t".join((self.status, self.size, self.sha256, self.kept, self.original))


@dataclass
class Manifest:
    """What `manifest.txt` holds: `key: value` lines, a `files:` line, then one
    tab-separated row per file (status, size, SHA-256, kept name, original path)."""

    fields: dict[str, str] = field(default_factory=dict)
    rows: list[Row] = field(default_factory=list)

    def text(self) -> str:
        lines = ["clockwork kept files", ""]
        lines += [f"{key}: {value}" for key, value in self.fields.items()]
        lines += ["", "files:", *(row.line() for row in self.rows)]
        return "\n".join(lines) + "\n"

    def stems(self) -> list[str]:
        return [word for word in self.fields.get("stems", "").split(", ") if word]

    def time(self) -> _dt.datetime | None:
        try:
            return _dt.datetime.fromisoformat(self.fields.get("time", ""))
        except ValueError:
            return None


def read_manifest(folder: str) -> Manifest | None:
    """A folder's manifest, or None where it has none or it cannot be read."""
    try:
        with open(os.path.join(folder, MANIFEST), encoding="utf-8") as stream:
            lines = stream.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    manifest = Manifest()
    in_files = False
    for line in lines[1:]:
        if in_files:
            parts = line.split("\t")
            if len(parts) == 5:
                manifest.rows.append(Row(*parts))
        elif line == "files:":
            in_files = True
        elif ": " in line:
            key, value = line.split(": ", 1)
            manifest.fields[key] = value
    return manifest


def write_manifest(folder: str, manifest: Manifest) -> None:
    """Write a manifest whole, LF line ends on every platform."""
    with open(os.path.join(folder, MANIFEST), "w", encoding="utf-8", newline="\n") as stream:
        stream.write(manifest.text())


def keep(root_dir: str, paths: Iterable[str], *, reason: str, stems: Iterable[str] = (),
         report_id: str | None = None, pc: str = "",
         when: _dt.datetime | None = None) -> str:
    """Copy `paths` into a new folder under `root_dir`, write its manifest, return it.

    A file that is missing or cannot be read is a row saying so, never an exception: the
    copy is made at the moment something has already gone wrong, and a second failure
    must not replace the first. What can raise is making the folder itself, which a
    caller on a failure path catches and logs.
    """
    import clockwork

    moment = when or _dt.datetime.now()
    os.makedirs(root_dir, exist_ok=True)
    name = report_id or new_id(root_dir, pc=pc, when=moment)
    folder = os.path.join(root_dir, name)
    os.makedirs(folder)
    manifest = Manifest(fields={
        "report id": name,
        "reason": " ".join(reason.split()) or "not given",
        "clockwork": f"{clockwork.__version__} ({clockwork.built_commit() or 'unknown commit'})",
        "pc": pc or platform.node(),
        "time": moment.isoformat(timespec="seconds"),
        "stems": ", ".join(dict.fromkeys(stem for stem in stems if stem)),
    })
    manifest.rows = _copy_all(folder, paths, taken={MANIFEST})
    write_manifest(folder, manifest)
    return folder


def add(folder: str, paths: Iterable[str], *, note: str,
        when: _dt.datetime | None = None) -> str:
    """Copy more files into an existing folder and add them to its manifest.

    For a report made after a failure already kept the run: the folder and id are
    reused, and a file already in it is kept under a new name beside the old copy
    rather than over it -- a fresh copy of the error log is not the one the failure saw.
    """
    manifest = read_manifest(folder) or Manifest(fields={"report id": os.path.basename(folder)})
    moment = when or _dt.datetime.now()
    manifest.fields[note] = moment.isoformat(timespec="seconds")
    taken = set(os.listdir(folder))
    manifest.rows += _copy_all(folder, paths, taken=taken)
    write_manifest(folder, manifest)
    return folder


def _copy_all(folder: str, paths: Iterable[str], *, taken: set[str]) -> list[Row]:
    rows: list[Row] = []
    for path in dict.fromkeys(os.path.abspath(p) for p in paths if p):
        if not os.path.isfile(path):
            rows.append(Row("missing", "-", "-", "-", path))
            continue
        kept = _free_name(os.path.basename(path), taken)
        target = os.path.join(folder, kept)
        try:
            shutil.copy2(path, target)
            digest = _sha256(target)
            size = os.path.getsize(target)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.remove(target)
            rows.append(Row(f"failed: {exc.strerror or exc}", "-", "-", "-", path))
            continue
        taken.add(kept)
        rows.append(Row("copied", str(size), digest, kept, path))
    return rows


def _free_name(name: str, taken: set[str]) -> str:
    if name not in taken:
        return name
    root_part, extension = os.path.splitext(name)
    count = 2
    while f"{root_part}-{count}{extension}" in taken:
        count += 1
    return f"{root_part}-{count}{extension}"


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def for_report(root_dir: str, *, directory: str = "", stem: str = "",
               method_path: str = "", errors_log: str = "") -> str:
    """The folder a report names: the run's failure folder, else a new one.

    A report made after a failure already kept the run reuses that folder and its id,
    adding a fresh copy of the error log (which now holds whatever the report is
    about); otherwise the run's files, the method and the error log are kept afresh
    with the reason `reported`. With no stem, only the error log and the method.
    """
    folder = kept_for(root_dir, stem)
    if folder is not None:
        return add(folder, [errors_log], note="reported")
    paths = [*run_files(directory, stem), method_path, errors_log]
    return keep(root_dir, paths, reason="reported", stems=[stem])


def kept_for(root_dir: str, stem: str) -> str | None:
    """The newest folder no report has claimed whose manifest lists `stem`, or None."""
    if not stem:
        return None
    best: tuple[_dt.datetime, str] | None = None
    for folder, manifest in _unclaimed(root_dir):
        moment = manifest.time()
        if stem in manifest.stems() and moment is not None and (
                best is None or moment > best[0]):
            best = (moment, folder)
    return best[1] if best else None


def prune(root_dir: str, days: int = RETENTION_DAYS,
          now: _dt.datetime | None = None) -> list[str]:
    """Remove unclaimed folders whose manifest is older than `days`; return them.

    Only a folder whose whole name is a bare report id and whose manifest gives a time:
    a renamed folder, one without a readable manifest, and anything else under the root
    are left alone. A folder that cannot be removed is skipped and tried next time.
    """
    cutoff = (now or _dt.datetime.now()) - _dt.timedelta(days=days)
    removed: list[str] = []
    for folder, manifest in _unclaimed(root_dir):
        moment = manifest.time()
        if moment is None or moment >= cutoff:
            continue
        try:
            shutil.rmtree(folder)
        except OSError:
            continue
        removed.append(folder)
    return removed


def _unclaimed(root_dir: str) -> list[tuple[str, Manifest]]:
    try:
        names = os.listdir(root_dir)
    except OSError:
        return []
    found = []
    for name in sorted(names):
        folder = os.path.join(root_dir, name)
        if not is_report_id(name) or not os.path.isdir(folder):
            continue
        manifest = read_manifest(folder)
        if manifest is not None:
            found.append((folder, manifest))
    return found
