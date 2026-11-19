"""The day record: what a daemon did under no request, beside the day's files.

`<output>/day-record.jsonl`, appended one JSON line per entry, UTF-8, LF; the format is
`docs/run-record.md`, "The day record". Written by the owner of the instrument, never by
a client: the sitting the daemon was started with (the instrument document and both
limits files, each with its SHA-256), each daemon start and stop, and each console
start, restart, stop and failure, each with a time and who caused it. Before it, a
console restart between two runs was written under no stem and read by nobody (lab
record, task 165, which closes the window design's FLAG on it; the window revision's C2).
From 167 and 168 it also takes a note typed at the window with nothing running, and a
beam-guard change made with nothing running.

**Segments, sealed with the audit log** (`clockwork.mcp.audit`, lab record, task 161), so
that a listed day folder never changes: `seal` renames the live file
`day-record.<YYYYMMDD-HHMMSS>.jsonl`, and the next line starts a new live file. Every
segment opens with the sitting, written by whichever line finds no live file, so each can
be read alone. The seal is made by the client that seals the audit log (`seal_calls`,
`stand_down`), in its own process; the daemon opens the live name per line, as the audit
log's writers do, so a rename between two lines costs nothing, and a line that meets a
rename under way (Windows' sharing refusal) is retried for `SHARING_WAIT_S`.

Neither name parses as a run's stem (`naming.parse_stem`), so a day record never moves a
file counter and is never one of a stem's files. A line that cannot be written is
dropped and logged, never raised: the day record is the day's, never a reason to
disturb a run. Qt-free.
"""

from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping

__all__ = ["LIVE_NAME", "SEGMENTS", "DayRecord", "seal_order", "segments"]

LIVE_NAME = "day-record.jsonl"
"""The live segment, the only one written to."""

SEGMENTS = "day-record*.jsonl"
"""Every segment of a folder's day record, the live one among them."""

SHARING_WAIT_S = 2.0
SEAL_WAIT_S = 5.0

_log = logging.getLogger(__name__)


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _first_time(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            line = json.loads(handle.readline())
    except (OSError, ValueError):
        return ""
    return str(line.get("time") or "") if isinstance(line, dict) else ""


_SEALED = re.compile(r"\.(\d{8}-\d{6})(?:-(\d+))?\.(?:log|jsonl)$")


def seal_order(path: str) -> tuple[str, int]:
    """Where a segment's name puts its seal: `(<YYYYMMDD-HHMMSS>, N)`, N being 1 with no
    suffix and N for `-N`; a name a seal did not give (a tail split off by hand) after
    every sealed one. Times in the lines are to the second, so two segments can begin in
    the same one, and their names, not their lines, say which was sealed first: by name
    alone `-2` sorts before the unsuffixed seal it follows. Shared with
    `clockwork.mcp.audit`, whose segments are named the same way."""
    match = _SEALED.search(os.path.basename(path))
    return (match.group(1), int(match.group(2) or 1)) if match else ("~", 0)


def segments(directory: str) -> list[str]:
    """Every segment of the day record in `directory`, oldest first, the live one last."""
    live = os.path.join(directory, LIVE_NAME)
    found = [path for path in glob.glob(os.path.join(glob.escape(directory), SEGMENTS))
             if os.path.isfile(path) and os.path.normcase(path) != os.path.normcase(live)]
    found.sort(key=lambda path: (_first_time(path), seal_order(path), os.path.basename(path)))
    return found + ([live] if os.path.isfile(live) else [])


class DayRecord:
    """The day record in `directory`; `sitting` is called for the line every segment
    opens with. `directory` empty writes nothing."""

    def __init__(self, directory: str,
                 sitting: Callable[[], Mapping[str, object]] | None = None) -> None:
        self.directory = directory
        self.path = os.path.join(directory, LIVE_NAME) if directory else ""
        self._sitting = sitting
        self._guard = threading.Lock()

    @classmethod
    def beside(cls, output: str) -> DayRecord:
        """The day record of `output`, for a reader or a seal, with no sitting."""
        return cls(output)

    def write(self, kind: str, **fields: object) -> None:
        """Append one line of `kind`, stamped with the time; never raises."""
        if not self.path:
            return
        try:
            self._append({"time": _now(), "kind": kind, **fields})
        except Exception:  # noqa: BLE001 -- the day's record, never a run's end
            _log.exception("a day record line was not written")

    def _append(self, line: Mapping[str, object]) -> None:
        with self._guard:
            os.makedirs(self.directory, exist_ok=True)
            deadline = time.monotonic() + SHARING_WAIT_S
            while True:
                try:
                    begins = not os.path.isfile(self.path)
                    with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                        if begins and self._sitting is not None and line.get("kind") != "sitting":
                            handle.write(_text({"time": line["time"], "kind": "sitting",
                                                **self._sitting()}))
                        handle.write(_text(line))
                    return
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)

    def lines(self) -> Iterator[dict]:
        """Every line of every segment, in order; one that does not parse is passed over."""
        if not self.directory:
            return
        for path in segments(self.directory):
            try:
                with open(path, encoding="utf-8") as handle:
                    texts = handle.readlines()
            except OSError:
                continue
            for text in texts:
                try:
                    line = json.loads(text)
                except ValueError:
                    continue
                if isinstance(line, dict):
                    yield line

    def seal(self, wait_s: float = SEAL_WAIT_S) -> dict[str, object] | None:
        """End the live segment: rename it `day-record.<YYYYMMDD-HHMMSS>.jsonl` (`-2` and
        on if taken, never over another) and answer its name, size and MD5; None when
        there is no live file. A rename refused for another process's open handle is
        retried for `wait_s`, then the refusal is raised (`OSError`)."""
        if not self.path:
            return None
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        deadline = time.monotonic() + wait_s
        with self._guard:
            if not os.path.isfile(self.path):
                return None
            number = 1
            while True:
                name = f"day-record.{stamp}{'' if number == 1 else f'-{number}'}.jsonl"
                target = os.path.join(self.directory, name)
                if os.path.lexists(target):
                    number += 1
                    continue
                try:
                    os.rename(self.path, target)
                    break
                except FileExistsError:
                    number += 1
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
        with open(target, "rb") as handle:
            data = handle.read()
        return {"name": name, "bytes": len(data), "md5": hashlib.md5(data).hexdigest()}


def _text(line: Mapping[str, object]) -> str:
    return json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n"
