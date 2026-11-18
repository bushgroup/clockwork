"""The audit log: one JSON line per tool call, beside the files the calls made.

`<output>/mcp-calls.log`, appended, UTF-8, LF. Each line is one call: when it ended, the
tool, its arguments, the request it served, a summary of what it answered, how long it
took, the error sentence if it failed, the daemon session it was made in, and the
surface that made it (`mcp` or `cli`; lab record, task 73). A person
who was away reads what was done from it, `list_files` reads the words of each request
back from it, since a file stamps only the request's id (lab record, task 69), and
`status` counts a daemon session's acquisitions from it: the accepted `acquire` lines,
and each accepted `series` line's count of acquisitions (lab record, tasks 71 and 91).
A send the limits refuse is a failed call, and its line carries the refusal verbatim as
its error.

**The log is segments** (lab record, task 161), so a folder once listed never changes:
`mcp-calls.log` is the live one, and `AuditLog.seal` renames it to
`mcp-calls.<YYYYMMDD-HHMMSS>.log`, which nothing writes again; the next line starts a new
live file, since `write` opens the live name per call. Every reader reads every
`mcp-calls*.log` in the folder (`SEGMENTS`), in the order of each file's first line, the
live one last -- which also takes in a tail another tool split off by hand.

**A long text argument is hashed, never quoted.** A method's or a template's text is
kilobytes, and what an auditor needs is whether two calls were given the same one; the
line carries its SHA-256 and length instead. A result is summarised to its top-level
scalars and the lengths of its lists, for the same reason: the result itself went to
the caller, and the files hold what matters.

**Every `series` line carries `plan_sha256`**, a check's and a refusal's too: the hash of
the plan's text as it is read, LF line endings, however the plan was passed
(`clockwork.planning.plan_sha256`). The hashed argument alone would not tie a run to its
check, since a plan passed as a path is logged as the path. `check_for` finds the check of
a hash, for the window and for a series' run record (lab record, task 166).
"""

from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator, Mapping

__all__ = ["LOG_NAME", "LONG_TEXT", "SEAL_WAIT_S", "SEGMENTS", "AuditLog", "SealFailure",
           "hashed", "segments", "summarised"]

LOG_NAME = "mcp-calls.log"
"""The live segment, the only one written to."""

SEGMENTS = "mcp-calls*.log"
"""Every segment of a folder's log, the live one among them."""

SEAL_WAIT_S = 5.0
"""How long a seal retries a rename another process's open handle refuses (Windows)."""

LONG_TEXT = 200
"""Past this many characters, or with a line break in it, an argument is hashed."""


def hashed(value: object) -> object:
    """`value` with every long text inside it replaced by its hash and length."""
    if isinstance(value, str):
        if len(value) > LONG_TEXT or "\n" in value:
            return {"sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                    "chars": len(value)}
        return value
    if isinstance(value, Mapping):
        return {str(key): hashed(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [hashed(item) for item in value]
    return value


def summarised(result: object) -> object:
    """A result cut to its top-level scalars, with a list or a mapping as its length."""
    if not isinstance(result, Mapping):
        return hashed(result) if isinstance(result, (str, int, float, bool)) else None
    out: dict[str, object] = {}
    for key, value in result.items():
        if value is None or isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str):
            out[key] = hashed(value)
        elif isinstance(value, (list, tuple)):
            out[key] = {"items": len(value)}
        elif isinstance(value, Mapping):
            out[key] = {"keys": len(value)}
    return out


class SealFailure(OSError):
    """The live segment could not be renamed; the sentence says why."""


def _first_time(path: str) -> str:
    """The `time` of a segment's first line, or empty when it has none to read."""
    try:
        with open(path, encoding="utf-8") as handle:
            line = json.loads(handle.readline())
    except (OSError, ValueError):
        return ""
    return str(line.get("time") or "") if isinstance(line, dict) else ""


def segments(directory: str) -> list[str]:
    """Every segment of the log in `directory`, oldest first and the live one last."""
    live = os.path.join(directory, LOG_NAME)
    found = [path for path in glob.glob(os.path.join(glob.escape(directory), SEGMENTS))
             if os.path.isfile(path) and os.path.normcase(path) != os.path.normcase(live)]
    # Times are to the second, and two seals in one second begin two segments in it:
    # the one last written to is the later (`-2` sorts before `.log` by name).
    found.sort(key=lambda path: (_first_time(path), _modified(path), os.path.basename(path)))
    return found + ([live] if os.path.isfile(live) else [])


def _modified(path: str) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


class AuditLog:
    """The log file, written from any thread; `path` empty writes nothing."""

    def __init__(self, path: str, via: str = "") -> None:
        self.path = path
        self.via = via
        """Which surface made the calls: `mcp`, `cli`, or empty for a caller in process."""
        self.session = ""
        """The daemon session every line is written under; set by the toolbox."""
        self._guard = threading.Lock()

    @classmethod
    def beside(cls, output: str, via: str = "") -> AuditLog:
        return cls(os.path.join(output, LOG_NAME) if output else "", via)

    def write(self, *, tool: str, arguments: Mapping[str, object], seconds: float,
              result: object = None, error: str | None = None,
              request: Mapping[str, str] | None = None,
              extra: Mapping[str, object] | None = None) -> None:
        """One line. `extra` adds keys of one tool's own after the common ones: a
        `series` line's `plan_sha256` (lab record, task 166)."""
        if not self.path:
            return
        line = {
            "time": _dt.datetime.now().isoformat(timespec="seconds"),
            "tool": tool,
            "arguments": hashed(dict(arguments)),
            "request": dict(request) if request else None,
            "result": summarised(result) if error is None else None,
            "error": error,
            "seconds": round(seconds, 3),
            "session": self.session or None,
            "via": self.via or None,
            **(extra or {}),
        }
        text = json.dumps(line, ensure_ascii=False, separators=(",", ":"))
        with self._guard:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(text + "\n")

    def acquisitions(self, session: str) -> int:
        """How many acquisitions were accepted in daemon session `session`: one per
        `acquire` call, and a `series` call's `acquisitions`, one per point."""
        return sum(self.acquisitions_by(session).values())

    def acquisitions_by(self, session: str) -> dict[str, int]:
        """`acquisitions`, by the surface that made them (`mcp`, `cli`, or `in process`).

        The count is the daemon session's, whichever surface made them: an agent's
        session reading its own count had no way to see that one was a
        `clockwork routine` at a terminal (lab record, task 96)."""
        counts: dict[str, int] = {}
        if not session:
            return counts
        for line in self.lines():
            if line.get("error") is not None or line.get("session") != session:
                continue
            surface = line.get("via") or "in process"
            if line.get("tool") == "acquire":
                counts[surface] = counts.get(surface, 0) + 1
            elif line.get("tool") == "series":
                spent = (line.get("result") or {}).get("acquisitions")
                counts[surface] = counts.get(surface, 0) + (
                    spent if isinstance(spent, int) and spent > 0 else 1)
        return counts

    def request_for(self, words: str, session: str) -> str | None:
        """The latest request id minted for exactly these words in daemon session
        `session`, or None: how a request begun by one process is continued by the next
        without its id, as the command line's `arm` and `acquire` are."""
        if not words or not session:
            return None
        found = None
        for line in self.lines():
            request = line.get("request")
            if (isinstance(request, dict) and request.get("id")
                    and request.get("text") == words and line.get("session") == session):
                found = str(request["id"])
        return found

    def check_for(self, sha256: str) -> dict[str, object] | None:
        """The latest `series` call made with `check` true, answered without error, on a
        plan whose text hashes to `sha256`: `{time, via, session}`, or None.

        What ties a run to the check that came before it (the window revision's A5):
        the window says "checked through mcp at 14:02, unchanged since" off it, and a
        series' run record keeps it. Every segment is read, so a check made before a
        `seal_calls` is still found."""
        if not sha256:
            return None
        found = None
        for line in self.lines():
            arguments = line.get("arguments")
            if (line.get("tool") == "series" and line.get("error") is None
                    and line.get("plan_sha256") == sha256
                    and isinstance(arguments, Mapping) and arguments.get("check") is True):
                found = {"time": line.get("time"), "via": line.get("via"),
                         "session": line.get("session")}
        return found

    def requests(self) -> dict[str, str]:
        """Every request id this log has seen, with its words, the latest words winning.

        A line that names a request by id alone -- a `note` from a process that never
        saw the words -- records them empty, and never blanks words already read."""
        found: dict[str, str] = {}
        for line in self.lines():
            request = line.get("request")
            if isinstance(request, dict) and request.get("id"):
                words = str(request.get("text") or "")
                if words or str(request["id"]) not in found:
                    found[str(request["id"])] = words
        return found

    def lines(self) -> Iterator[dict]:
        """Every line of every segment, in order, each a mapping; a line that does not
        parse as one is passed over."""
        if not self.path:
            return
        for path in segments(os.path.dirname(self.path)):
            try:
                with open(path, encoding="utf-8") as handle:
                    texts = handle.readlines()
            except OSError:
                continue  # sealed between the listing and the open
            for text in texts:
                try:
                    line = json.loads(text)
                except ValueError:
                    continue
                if isinstance(line, dict):
                    yield line

    def seal(self, wait_s: float = SEAL_WAIT_S) -> dict[str, object] | None:
        """End the live segment: rename it `mcp-calls.<YYYYMMDD-HHMMSS>.log` (`-2` and on
        if that is taken, never over another) and answer its name, size and MD5; None
        when there is no live file, or no log. A rename refused because another process
        holds the file open is retried for `wait_s`, then `SealFailure` with the refusal.

        Sealed under this log's lock, so no line from this process lands in a segment
        once it is named; another process's write meets the rename as Windows' sharing
        refusal and is retried, or opens a new live file after it."""
        if not self.path:
            return None
        directory = os.path.dirname(self.path) or "."
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        deadline = time.monotonic() + wait_s
        with self._guard:
            if not os.path.isfile(self.path):
                return None
            number = 1
            while True:
                name = f"mcp-calls.{stamp}{'' if number == 1 else f'-{number}'}.log"
                target = os.path.join(directory, name)
                if os.path.lexists(target):
                    number += 1
                    continue
                try:
                    os.rename(self.path, target)
                    break
                except FileExistsError:
                    number += 1
                except PermissionError as exc:
                    if time.monotonic() >= deadline:
                        raise SealFailure(
                            f"{LOG_NAME} in {directory} could not be sealed within "
                            f"{wait_s:g} s, since another process holds it open: "
                            f"{exc.strerror or exc}") from exc
                    time.sleep(0.1)
        with open(target, "rb") as handle:
            data = handle.read()
        return {"name": name, "bytes": len(data), "md5": hashlib.md5(data).hexdigest()}
