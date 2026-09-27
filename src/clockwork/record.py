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

**A verdict is the person's judgement of one file, not the machine's outcome.** The run
outcome stamped in the file (`completed`, `stopped`, `failed`, `incomplete`) says how the
acquisition ended; a run that completed with no ions in it is `completed`, and its verdict
is `no_signal`. One entry per call in `verdicts`, keyed by stem (a run's raw and summed
files are one run); a later verdict on the same stem supersedes the earlier, which is kept
with its time, and `newest_verdict` reads back the last (lab record, task 90).

Written whole on every change, through a temporary file and a rename, so a reader never
sees half of it; UTF-8, LF. Qt-free.
"""

from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import threading
from collections.abc import Mapping
from typing import Any

__all__ = ["RECORD_SCHEMA", "RECORD_SUFFIX", "SOURCES", "VERDICTS", "RunRecord", "brief",
           "find", "find_for_stem", "holds", "newest_verdict", "record_path"]

RECORD_SCHEMA = 1
RECORD_SUFFIX = ".request.json"
SOURCES = ("agent", "window")

SECTIONS = ("plans", "arms", "acquisitions", "files", "notes", "routines", "verdicts")

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


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def record_path(directory: str, stem: str) -> str:
    return os.path.join(directory, stem + RECORD_SUFFIX)


def find(directory: str, request_id: str) -> str | None:
    """The record of `request_id` in `directory`, or None."""
    if not request_id or not os.path.isdir(directory):
        return None
    for path in sorted(glob.glob(os.path.join(glob.escape(directory), "*" + RECORD_SUFFIX))):
        try:
            with open(path, encoding="utf-8") as handle:
                if json.load(handle).get("request", {}).get("id") == request_id:
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
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
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
             source: str = "agent", initials: str = "") -> RunRecord:
        """The record of `request_id` in `directory`, begun at `stem` if it has none."""
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
            record._write({
                "record_schema": RECORD_SCHEMA,
                "request": {"id": request_id, "text": text, "source": source,
                            "initials": initials, "opened": _now(), "stem": stem},
                **{section: [] for section in SECTIONS},
            })
            return record

    def read(self) -> dict[str, Any]:
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    def append(self, section: str, entry: Mapping[str, object]) -> None:
        """Add `entry` to `section`, stamped with the time."""
        if section not in SECTIONS:
            raise ValueError(f"a run record's sections are {', '.join(SECTIONS)}")
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
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        os.replace(temporary, self.path)


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
