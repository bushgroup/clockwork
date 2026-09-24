"""The audit log: one JSON line per tool call, beside the files the calls made.

`<output>/mcp-calls.log`, appended, UTF-8, LF. Each line is one call: when it ended, the
tool, its arguments, the request it served, a summary of what it answered, how long it
took, the error sentence if it failed, the daemon session it was made in, and the
surface that made it (`mcp` or `cli`; lab record, task 73). A person
who was away reads what was done from it, `list_files` reads the words of each request
back from it, since a file stamps only the request's id (lab record, task 69), and the
standing envelope's budget is counted from it: the accepted `acquire` lines of one
daemon session (lab record, task 71).

**A long text argument is hashed, never quoted.** A method's or a template's text is
kilobytes, and what an auditor needs is whether two calls were given the same one; the
line carries its SHA-256 and length instead. A result is summarised to its top-level
scalars and the lengths of its lists, for the same reason: the result itself went to
the caller, and the files hold what matters.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import threading
from collections.abc import Mapping

__all__ = ["LOG_NAME", "LONG_TEXT", "AuditLog", "hashed", "summarised"]

LOG_NAME = "mcp-calls.log"

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
              request: Mapping[str, str] | None = None) -> None:
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
        }
        text = json.dumps(line, ensure_ascii=False, separators=(",", ":"))
        with self._guard:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(text + "\n")

    def acquisitions(self, session: str) -> int:
        """How many `acquire` calls were accepted in daemon session `session`."""
        if not session or not self.path or not os.path.isfile(self.path):
            return 0
        count = 0
        with open(self.path, encoding="utf-8") as handle:
            for text in handle:
                try:
                    line = json.loads(text)
                except ValueError:
                    continue
                if (isinstance(line, dict) and line.get("tool") == "acquire"
                        and line.get("error") is None and line.get("session") == session):
                    count += 1
        return count

    def request_for(self, words: str, session: str) -> str | None:
        """The latest request id minted for exactly these words in daemon session
        `session`, or None: how a request begun by one process is continued by the next
        without its id, as the command line's `arm` and `acquire` are."""
        if not words or not session or not self.path or not os.path.isfile(self.path):
            return None
        found = None
        with open(self.path, encoding="utf-8") as handle:
            for text in handle:
                try:
                    line = json.loads(text)
                except ValueError:
                    continue
                request = line.get("request") if isinstance(line, dict) else None
                if (isinstance(request, dict) and request.get("id")
                        and request.get("text") == words and line.get("session") == session):
                    found = str(request["id"])
        return found

    def requests(self) -> dict[str, str]:
        """Every request id this log has seen, with its words, the latest words winning.

        A line that names a request by id alone -- a `note` from a process that never
        saw the words -- records them empty, and never blanks words already read."""
        found: dict[str, str] = {}
        if not self.path or not os.path.isfile(self.path):
            return found
        with open(self.path, encoding="utf-8") as handle:
            for text in handle:
                try:
                    request = json.loads(text).get("request")
                except (ValueError, AttributeError):
                    continue
                if isinstance(request, dict) and request.get("id"):
                    words = str(request.get("text") or "")
                    if words or str(request["id"]) not in found:
                        found[str(request["id"])] = words
        return found
