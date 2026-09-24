r"""A pre-filled bug report: the facts a trainee would otherwise be asked for, in a URL.

The lab takes reports as issues on its tracker, through a form whose fields have ids
(`version`, `pc`, `did`, `happened`, `expected`, `lost`, `attachments`), and GitHub fills
a form's fields from the query string by those ids. So a report opened from here arrives
with the version and build commit, the PC, the operating system, the method's name and
the tails of the window's error log and the run's wire transcript already written in,
and the trainee writes only what they did, what happened and what they expected (lab
record, task 79).

**What stays out.** No method contents: a string sent to a box or a command sent to the
console is dropped from the transcript tail line by line (`outbound`), and the method is
named, never read. The URL is budgeted (`BUDGET`), because a browser or GitHub refuses a
long one; a tail that does not fit is shortened from its oldest line, and one that still
does not fit is replaced by the file's path and a request to drag the file in.

One module for both callers, and Qt-free: the window opens the URL with
`QDesktopServices`, `clockwork report` with `webbrowser`.
"""

from __future__ import annotations

import glob
import os
import platform
import re
import sys
from collections.abc import Sequence
from urllib.parse import urlencode

__all__ = ["BUDGET", "ISSUES", "Report", "outbound", "tail", "transcript_beside", "url"]

ISSUES = "https://github.com/bushgroup/clockwork-lab/issues/new"
"""The lab's issue tracker. `$CLOCKWORK_ISSUES` points a report elsewhere, for a group
running clockwork with a tracker of its own."""

TEMPLATE = "bug.yml"

BUDGET = 8000
"""Characters in the whole URL. GitHub and every browser in use take this comfortably."""

TAIL_LINES = 40
"""The most lines of either file a report carries, before the budget trims further."""

MIN_TRANSCRIPT_LINES = 5
"""Fewer transcript lines than this are not worth sending; the path is sent instead."""

_TIMESTAMPED = re.compile(r"^\d\d:\d\d:\d\d\.\d{3} (\S+)\s+(.*)$")


def outbound(line: str) -> bool:
    """Whether a transcript line is a string this host put on a link.

    The box lines are `<time> mips.wire <box> > <payload>` (the table string and its
    chunk bookkeeping included), the console's `<time> acq.wire > <command>`. Both are
    the method, and a report never carries the method. A line that is not a timestamped
    record -- the preamble, the run header with its method hash and box rows -- counts as
    outbound too: nothing in it is needed to read the tail, and the header can carry the
    trainee's free text.
    """
    matched = _TIMESTAMPED.match(line)
    if matched is None:
        return True
    words = matched.group(2).split()
    if not words:
        return False
    return words[0] == ">" or (len(words) > 1 and words[1] == ">")


def tail(path: str | os.PathLike[str] | None, lines: int = TAIL_LINES, *,
         keep: object = None) -> list[str]:
    """The last `lines` lines of a text file that `keep` accepts, or none.

    Reads only the end of the file: an errors log is appended to forever and a
    transcript runs to megabytes. Never raises; an unreadable or missing file is empty.
    """
    if not path:
        return []
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 256 * 1024))
            text = stream.read().decode("utf-8", "replace")
    except OSError:
        return []
    found = text.splitlines()
    if size > 256 * 1024:
        found = found[1:]  # the first line read is a fragment
    if keep is not None:
        found = [line for line in found if keep(line)]
    found = [line for line in found if line.strip()]
    return found[-lines:] if lines > 0 else []


def transcript_beside(directory: str, stem: str) -> str:
    """The newest `<stem>-<date>.transcript.log` in `directory`, else the empty string.

    By pattern rather than by `transcript.default_name`, whose date is today's: a run
    from last night is reported this morning.
    """
    if not directory or not stem:
        return ""
    matches = glob.glob(os.path.join(glob.escape(directory),
                                     glob.escape(stem) + "-*.transcript.log"))
    if not matches:
        return ""
    return max(matches, key=os.path.getmtime)


class Report:
    """What goes into one report. Every field has a default that describes this process."""

    def __init__(self, *, version: str = "", commit: str | None = "", pc: str = "",
                 system: str = "", method: str = "", errors_log: str = "",
                 transcript: str = "", issues: str = "") -> None:
        import clockwork

        self.version = version or clockwork.__version__
        self.commit = clockwork.built_commit() if commit == "" else commit
        self.pc = pc or platform.node()
        self.system = system or platform.platform()
        self.method = os.path.basename(method)
        self.errors_log = errors_log
        self.transcript = transcript
        self.issues = issues or os.environ.get("CLOCKWORK_ISSUES") or ISSUES

    def url(self, budget: int = BUDGET) -> str:
        """The pre-filled form's URL, no longer than `budget` characters."""
        errors = tail(self.errors_log)
        chunk = tail(self.transcript, keep=lambda line: not outbound(line))
        # Both tails shrink together, so neither is given up for the other while a
        # shorter pair would fit; the transcript goes first, having the longer floor.
        error_sizes = _shrinking(len(errors), 0)
        transcript_sizes = _shrinking(len(chunk), MIN_TRANSCRIPT_LINES)
        for step in range(max(len(error_sizes), len(transcript_sizes))):
            error_lines = error_sizes[min(step, len(error_sizes) - 1)]
            transcript_lines = transcript_sizes[min(step, len(transcript_sizes) - 1)]
            made = self._url(errors[len(errors) - error_lines:],
                             chunk[len(chunk) - transcript_lines:],
                             cut_errors=error_lines < len(errors),
                             cut_transcript=transcript_lines < len(chunk))
            if len(made) <= budget:
                return made
        return self._url([], [], cut_errors=bool(errors), cut_transcript=bool(chunk))

    def _url(self, errors: Sequence[str], chunk: Sequence[str], *, cut_errors: bool,
             cut_transcript: bool) -> str:
        build = "installed" if getattr(sys, "frozen", False) else "from source"
        version = f"{self.version} ({self.commit or 'unknown commit'}, {build})"
        block = ["Filled in by clockwork:", "", f"- Operating system: {self.system}"]
        if self.method:
            block.append(f"- Method: {self.method}")
        if self.errors_log and os.path.isfile(self.errors_log):
            block.append(f"- Error log: `{self.errors_log}`")
        if self.transcript and os.path.isfile(self.transcript):
            block.append(f"- Run transcript: `{self.transcript}`")
        if errors:
            block += ["", f"Last {len(errors)} lines of the error log:", "", "```",
                      *errors, "```"]
        elif cut_errors:
            block += ["", "The error log is too long to include here; please drag it in."]
        if chunk:
            block += ["", f"Last {len(chunk)} lines of the run transcript "
                          "(strings sent to the boxes and the console left out):", "",
                      "```", *chunk, "```"]
        elif cut_transcript:
            block += ["", "The run transcript is too long to include here; please drag "
                          "it in from the path above."]
        block += ["", "Add a screenshot or the method file below if you have one."]
        fields = {"template": TEMPLATE, "version": version, "pc": self.pc,
                  "attachments": "\n".join(block)}
        return f"{self.issues}?{urlencode(fields)}"


def _shrinking(start: int, floor: int) -> list[int]:
    """`start`, then smaller counts down to `floor`, then 0: the sizes a tail is tried at."""
    sizes, size = ([start] if start else []), start
    while True:
        size = size * 3 // 4 if size > 8 else size - 1
        if size < max(floor, 1):
            return sizes + [0]
        sizes.append(size)


def url(**fields: str) -> str:
    """`Report(**fields).url()`, for a caller with nothing else to do with the report."""
    return Report(**fields).url()
