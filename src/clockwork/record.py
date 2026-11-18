"""The run record: one small document per request, beside the files it produced.

`<stem>.request.json`, where the stem is the first file's: what was asked for, in the
words of the person it was for; the plan the driving session stated; each arming, with
the template's hash, the knob values and what the cold-start check said; each
acquisition; each file written, with its summary; the session's notes; and each verdict a
person gave on a file, in their words and under their initials. The server
writes it as the request proceeds and nothing curates it. The files remain the primary
record: every one stamps its request's id, and this document is what joins them and
says why they were made (lab record, task 71).

A request continued in a later session, by its id, finds its record by that id and
appends to it, so one request is one document however many sessions it spans. A run
started from the window is the same document with `window` as its source (Matt,
2026-09-24).

**The format is `docs/run-record.md`**, which a reader outside clockwork writes against;
this module follows it, and a change is made there first (lab record, task 165).

**`source` is the route the request came by**: `mcp`, a session's tool call through
`clockwork-mcp`; `cli`, a verb of `clockwork-cli`, typed by a person or run from an agent's
shell; `window`, a run a person started at the window, whose record the owner begins when
its job starts (`clockwork.owner.keeper.begin`), and before 1.2.0 a verdict given on a
window run that had no record. Records written before 1.2.0 say `agent` for both of the
first two, undistinguished; a reader accepts it (`READ_SOURCES`) and nothing writes it
again (Matt, 2026-11-18; lab record, task 164). The source is the request's, set when its
record is begun: a request continued by its id over the other route keeps the first.

The files of a request's job are entered by the owner that wrote them
(`clockwork.owner.keeper`), so a record stays whole when the process that issued the
request ends before its job does.

**`interventions` is begun by its first entry** (`LATER_SECTIONS`): a person's pause,
resume, stop or guard change on a running job (lab record, tasks 165 and 167). A record
with none has no such key, so an `mcp` or `cli` record is byte for byte what it was before
the section existed, and `record_schema` stays 1.

**A verdict is the person's judgement of one file, not the machine's outcome.** The run
outcome stamped in the file (`completed`, `stopped`, `failed`, `incomplete`) says how the
acquisition ended; a run that completed with no ions in it is `completed`, and its verdict
is `no_signal`. One entry per call in `verdicts`, keyed by stem (a run's raw and summed
files are one run); a later verdict on the same stem supersedes the earlier, which is kept
with its time, and `newest_verdict` reads back the last (lab record, task 90).

Written whole on every change, through a temporary file and a rename, so a reader never
sees half of it; UTF-8, LF. Qt-free.

On Windows the rename and a reader collide: `os.replace` is refused (PermissionError)
while a reader holds the record open, and a reader's `open` is refused while the rename
is under way. Both last a moment, so the writer retries its rename and every reader goes
through `load`, which retries its open, for up to `SHARING_WAIT_S`. Before this, a run's
file entry written by the recording thread was dropped, or a manifest read just then
missed the record and its verdict.
"""

from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["LATER_SECTIONS", "READ_SOURCES", "RECORD_SCHEMA", "RECORD_SUFFIX",
           "RECORD_TEMPORARY", "SECTIONS", "SHARING_WAIT_S", "SOURCES", "VERDICTS",
           "RunRecord", "brief", "find", "find_for_stem", "holds", "load", "mint_id",
           "newest_verdict", "record_path"]

RECORD_SCHEMA = 1
RECORD_SUFFIX = ".request.json"
RECORD_TEMPORARY = RECORD_SUFFIX + ".tmp"
"""The suffix of the file a record is written through before its rename (`_write`). It
exists for a moment on every change, under the same stem as the record, so whatever
leaves the record out of a stem's files leaves this out too (`naming.files_of`)."""
SOURCES = ("mcp", "cli", "window")
"""The routes a record is begun under now."""
READ_SOURCES = (*SOURCES, "agent")
"""Every `source` a record may carry: `agent` is a record from before 1.2.0, by `mcp` or
`cli` without saying which."""

SECTIONS = ("plans", "arms", "acquisitions", "files", "notes", "routines", "verdicts")
"""The sections every record is begun with, each an empty list."""
LATER_SECTIONS = ("interventions",)
"""Sections a record gains with their first entry, and lacks until then."""

VERDICTS = {
    "worked": "the run gave the data it was taken for",
    "no_signal": "no ions, or too few to use, where some were expected",
    "saturated": "the signal reached the card's top code where it mattered",
    "wrong_sample": "the file holds something other than what it was meant to",
    "other": "anything else, said in the person's own words",
}
"""A verdict's word, with the sentence that says what it means. Fixed, so a manifest's
`verdict` column can be filtered; what the words cannot say goes in the entry's `words`,
required with `other`."""

_GUARD = threading.Lock()
"""One lock for every record this process writes: they are small and written seldom."""


SHARING_WAIT_S = 2.0
"""How long a record's reader or writer keeps retrying a Windows sharing refusal. The
other side holds the file for a read or a rename of a few kilobytes, milliseconds."""


def _shared[T](attempt: Callable[[], T]) -> T:
    """`attempt()`, retried while Windows refuses it for another handle on the file
    (PermissionError), for up to `SHARING_WAIT_S`; then the refusal is raised."""
    deadline = time.monotonic() + SHARING_WAIT_S
    pause = 0.002
    while True:
        try:
            return attempt()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(pause)
            pause = min(pause * 2, 0.05)


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def mint_id(words: str = "") -> str:
    """A new request id, `YYMMDD-HHMMSS-xxxxxx`: the time, and six hexadecimal digits of
    a hash over the time in nanoseconds and the request's words. Every route mints its
    ids here, so a reader can take the time off any of them."""
    digest = hashlib.sha256(f"{time.time_ns()}{words}".encode()).hexdigest()[:6]
    return f"{_dt.datetime.now():%y%m%d-%H%M%S}-{digest}"


def record_path(directory: str, stem: str) -> str:
    return os.path.join(directory, stem + RECORD_SUFFIX)


def load(path: str) -> Any:
    """The record at `path`, parsed; every reader of a record reads through this. A
    sharing refusal from a writer's rename is waited out (`_shared`); OSError for any
    other failure to open, ValueError for a document that is not JSON."""
    def attempt() -> Any:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return _shared(attempt)


def find(directory: str, request_id: str) -> str | None:
    """The record of `request_id` in `directory`, or None."""
    if not request_id or not os.path.isdir(directory):
        return None
    for path in sorted(glob.glob(os.path.join(glob.escape(directory), "*" + RECORD_SUFFIX))):
        try:
            if load(path).get("request", {}).get("id") == request_id:
                return path
        except (OSError, ValueError, AttributeError):
            continue
    return None


def holds(data: Mapping[str, Any], stem: str) -> bool:
    """Whether the record `data` holds a file of `stem`: in its `files` section, or as the
    stem its request was begun at."""
    stems = {kept.get("stem") for kept in data.get("files") or [] if isinstance(kept, dict)}
    return stem in stems or (data.get("request") or {}).get("stem") == stem


def find_for_stem(directory: str, stem: str, request_id: str | None = None) -> str | None:
    """The record in `directory` a file of `stem` belongs to: its request's, by the id the
    file stamps, or else one that holds the stem. None if there is none."""
    found = find(directory, request_id) if request_id else None
    if found is not None or not os.path.isdir(directory):
        return found
    for path in sorted(glob.glob(os.path.join(glob.escape(directory), "*" + RECORD_SUFFIX))):
        try:
            data = load(path)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and holds(data, stem):
            return path
    return None


def newest_verdict(data: Mapping[str, Any], stem: str) -> dict[str, Any] | None:
    """The last verdict entry on `stem` in the record `data`, or None."""
    newest = None
    for entry in data.get("verdicts") or []:
        if isinstance(entry, dict) and entry.get("stem") == stem and entry.get("verdict"):
            newest = entry
    return newest


class RunRecord:
    """One request's record, at `path`."""

    def __init__(self, path: str) -> None:
        self.path = path

    @classmethod
    def open(cls, directory: str, stem: str, *, request_id: str, text: str,
             source: str, initials: str = "", at_window: str = "") -> RunRecord:
        """The record of `request_id` in `directory`, begun at `stem` under the route
        `source` if it has none; one it already has keeps the source it was begun with.
        `at_window` is the person who pressed Start at the window, for a `window` record
        alone: the request block of any other carries no such key."""
        if source not in SOURCES:
            raise ValueError(f"a run record's source is one of {', '.join(SOURCES)}")
        with _GUARD:
            found = find(directory, request_id)
            if found is not None:
                return cls(found)
            path = record_path(directory, stem)
            if os.path.exists(path):
                # Another request's, begun at the same stem: never written over.
                path = record_path(directory, f"{stem}-{request_id}")
            record = cls(path)
            request: dict[str, object] = {"id": request_id, "text": text, "source": source,
                                          "initials": initials, "opened": _now(),
                                          "stem": stem}
            if source == "window":
                request["at_window"] = at_window
            record._write({
                "record_schema": RECORD_SCHEMA,
                "request": request,
                **{section: [] for section in SECTIONS},
            })
            return record

    def read(self) -> dict[str, Any]:
        return load(self.path)

    def append(self, section: str, entry: Mapping[str, object]) -> None:
        """Add `entry` to `section`, stamped with the time; a section of
        `LATER_SECTIONS` is begun by its first entry."""
        if section not in SECTIONS + LATER_SECTIONS:
            raise ValueError("a run record's sections are "
                             f"{', '.join(SECTIONS + LATER_SECTIONS)}")
        with _GUARD:
            data = self.read()
            data.setdefault(section, []).append({"time": _now(), **entry})
            self._write(data)

    def add_file(self, entry: Mapping[str, object]) -> None:
        """Add one file's entry, or replace the entry already there for its stem."""
        with _GUARD:
            data = self.read()
            files = [kept for kept in data.setdefault("files", [])
                     if kept.get("stem") != entry.get("stem")]
            files.append({"time": _now(), **entry})
            data["files"] = files
            self._write(data)

    def add_verdict(self, stem: str, verdict: str, by: str, words: str = "") -> int:
        """Add a verdict on `stem`, which the record must hold; answers how many verdicts
        the record now keeps on that stem. `ValueError` for a word outside `VERDICTS`,
        `other` without words, no initials, or a stem the record does not hold."""
        words = words.strip()
        if verdict not in VERDICTS:
            raise ValueError(f"{verdict!r} is not a verdict; say one of "
                             f"{', '.join(VERDICTS)}, and anything more in words")
        if verdict == "other" and not words:
            raise ValueError("a verdict of other says what it is: pass its words")
        if not by:
            raise ValueError("a verdict is a person's: give their initials")
        with _GUARD:
            data = self.read()
            if not holds(data, stem):
                raise ValueError(f"{os.path.basename(self.path)} holds no file of {stem}")
            data.setdefault("verdicts", []).append(
                {"time": _now(), "stem": stem, "verdict": verdict, "words": words, "by": by})
            self._write(data)
        return sum(1 for entry in data["verdicts"] if entry.get("stem") == stem)

    def _write(self, data: Mapping[str, object]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        temporary = self.path + RECORD_TEMPORARY[len(RECORD_SUFFIX):]
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        _shared(lambda: os.replace(temporary, self.path))


def brief(summary: Mapping[str, Any]) -> dict[str, Any]:
    """The few numbers of `clockwork.summary.summarize` a record keeps for one file."""
    frames = summary.get("frames") or {}
    base = summary.get("base_peak") or {}
    saturation = summary.get("saturation") or {}
    return {
        "file": summary.get("file"),
        "frames": frames.get("count"),
        "provisional_frames": frames.get("provisional"),
        "scans_per_frame": summary.get("scans_per_frame"),
        "accumulations": summary.get("accumulations"),
        "total_counts": summary.get("total_counts"),
        "base_peak_mz": base.get("mz"),
        "base_peak_intensity": base.get("intensity"),
        "pusher_period_ns": summary.get("pusher_period_ns"),
        "pusher_period_ratio": (summary.get("pusher_period_check") or {}).get("ratio"),
        "saturated_fraction": saturation.get("fraction"),
    }
