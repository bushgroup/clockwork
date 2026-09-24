"""The run record: one small document per request, beside the files it produced.

`<stem>.request.json`, where the stem is the first file's: what was asked for, in the
words of the person it was for; the plan the driving session stated; each arming, with
the template's hash, the knob values and what the cold-start check said; each
acquisition; each file written, with its summary; and the session's notes. The server
writes it as the request proceeds and nothing curates it. The files remain the primary
record: every one stamps its request's id, and this document is what joins them and
says why they were made (lab record, task 71).

A request continued in a later session, by its id, finds its record by that id and
appends to it, so one request is one document however many sessions it spans. A run
started from the window is the same document with `window` as its source (Matt,
2026-09-24).

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

__all__ = ["RECORD_SCHEMA", "RECORD_SUFFIX", "SOURCES", "RunRecord", "brief", "find",
           "record_path"]

RECORD_SCHEMA = 1
RECORD_SUFFIX = ".request.json"
SOURCES = ("agent", "window")

SECTIONS = ("plans", "arms", "acquisitions", "files", "notes")

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
        "saturated_fraction": saturation.get("fraction"),
    }
