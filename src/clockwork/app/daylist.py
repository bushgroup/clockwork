"""The day's list: what the Day tab shows, read off the day folder and nothing else.

The window revision's D2 and D3 (lab record, task 169): one line per request begun on the
day in the output directory, from its run record (`docs/run-record.md`), in the order
they were opened, each with its route and person, its purpose, its files, how it ended,
a routine's own result and a person's newest verdict, **as the record words them**; and a
line for each entry of the day record (a console restart, a note typed with nothing
running, a change to the beam guard, the daemon's starts). The window adds the daemon's
live jobs, which have no record yet or whose record is still being written.

**It computes nothing** (D3; the window design's decision 3): no verdict of its own, no
judgement of a file. How a request ended is the record's own words: how many of its files
say they are complete or were stopped early, a stop's intervention and the daemon's last
note. Qt-free, so `check_public.py` reads a day folder with it on a clone with no display.
"""

from __future__ import annotations

import datetime as _dt
import glob
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .. import dayrecord, record

__all__ = ["DayLine", "day_lines", "request_line"]


@dataclass(frozen=True, slots=True)
class DayLine:
    """One line of the Day tab."""

    time: str
    """`HH:MM:SS`, local, when the request was opened or the day record's line written."""
    route: str
    """The request's `source` (`window`, `mcp`, `cli`, `agent`), or `day record`."""
    person: str = ""
    """Who: the person at the window, the file initials, or the day record's `by`."""
    purpose: str = ""
    """The request's own words, or what the day record's line says happened."""
    files: tuple[str, ...] = ()
    ended: str = ""
    """How it ended, as the record words it."""
    result: str = ""
    """A routine's own result, as the routine words it."""
    verdict: str = ""
    """A person's newest verdict on each file, as recorded."""
    state: str = ""
    """`stopped`, `failed` or `held` where the record says so, for the colour (D5);
    empty otherwise."""
    request_id: str = ""
    path: str = ""


def _clock(stamp: object) -> str:
    text = str(stamp or "")
    return text[11:19] if len(text) >= 19 else text


def _entries(data: Mapping[str, Any], section: str) -> list[dict[str, Any]]:
    return [entry for entry in data.get(section) or [] if isinstance(entry, dict)]


def request_line(data: Mapping[str, Any], path: str = "") -> DayLine:
    """One run record as a line of the day's list."""
    request = data.get("request") or {}
    files = _entries(data, "files")
    stems = tuple(str(entry.get("stem") or "") for entry in files if entry.get("stem"))
    complete = sum(1 for entry in files if entry.get("complete"))
    early = sum(1 for entry in files if entry.get("stopped_early"))
    parts: list[str] = []
    if files:
        counts = [f"{complete} complete"] if complete else []
        if early:
            counts.append(f"{early} stopped early")
        rest = len(files) - complete - early
        if rest > 0:
            counts.append(f"{rest} neither")
        parts.append(f"{len(files)} file{'s' if len(files) != 1 else ''}: "
                     + ", ".join(counts))
    else:
        parts.append("no files")
    interventions = _entries(data, "interventions")
    stops = [entry for entry in interventions if entry.get("kind") == "stop"]
    state = ""
    if stops:
        stop = stops[-1]
        parts.append(f"stopped by {stop.get('by') or stop.get('client') or 'a client'}"
                     + (" while paused" if stop.get("while_paused") else "")
                     + (f": {stop['reason']}" if stop.get("reason") else ""))
        state = "stopped"
    pauses = [entry for entry in interventions if entry.get("kind") == "pause"]
    resumes = [entry for entry in interventions if entry.get("kind") == "resume"]
    if len(pauses) > len(resumes) and not stops:
        pause = pauses[-1]
        parts.append(f"paused by {pause.get('by') or 'a person'}"
                     + (f" ({pause['reason']})" if pause.get("reason") else ""))
        state = state or "held"
    server = [entry for entry in _entries(data, "notes") if entry.get("by") == "server"]
    if server:
        said = str(server[-1].get("text") or "")
        parts.append(said)
        if "failed" in said.lower():
            state = "failed"
    if not state and early:
        state = "stopped"
    routines = _entries(data, "routines")
    result = ""
    if routines:
        newest = routines[-1]
        result = f"{newest.get('routine') or 'routine'}: {newest.get('verdict') or '?'}"
        if newest.get("reason"):
            result += f" ({newest['reason']})"
    verdicts = []
    for stem in stems:
        given = record.newest_verdict(data, stem)
        if given is not None:
            words = f", {given['words']}" if given.get("words") else ""
            verdicts.append(f"{stem}: {given.get('verdict')}{words}"
                            + (f" ({given['by']})" if given.get("by") else ""))
    return DayLine(
        time=_clock(request.get("opened")), route=str(request.get("source") or ""),
        person=str(request.get("at_window") or request.get("initials") or ""),
        purpose=str(request.get("text") or ""), files=stems, ended="; ".join(parts),
        result=result, verdict="; ".join(verdicts), state=state,
        request_id=str(request.get("id") or ""), path=path)


def _day_record_line(line: Mapping[str, Any]) -> DayLine | None:
    kind = str(line.get("kind") or "")
    by = str(line.get("by") or "")
    if kind == "sitting":
        documents = [f"{name} {os.path.basename(str((line.get(name) or {}).get('path') or ''))}"
                     for name in ("instrument", "limits", "project_limits")
                     if isinstance(line.get(name), dict)]
        what = ("the sitting: " + ", ".join(documents) if documents
                else "the sitting: no instrument document and no limits")
    elif kind == "daemon":
        what = f"clockwork serve {line.get('event') or ''}" + (
            f" ({line['reason']})" if line.get("reason") else "")
    elif kind == "console":
        what = f"the acquisition console {line.get('event') or ''}" + (
            f": {line['detail']}" if line.get("detail") else "")
    elif kind == "note":
        what = f"note: {line.get('text') or ''}"
    elif kind == "guard":
        what = ("beam guard armed" if line.get("armed") else "beam guard off") + (
            f", floor {line['floor']}" if line.get("floor") is not None else "")
    else:
        return None
    return DayLine(time=_clock(line.get("time")), route="day record", person=by,
                   purpose=what, state="failed" if line.get("event") == "failed" else "")


def day_lines(directory: str, day: _dt.date | None = None) -> list[DayLine]:
    """Every request opened on `day` (today by default) in `directory`, and every line
    of its day record from that day, in time order. A record that will not read is a
    line saying so, never dropped silently."""
    if not directory or not os.path.isdir(directory):
        return []
    prefix = (day or _dt.date.today()).isoformat()
    found: list[tuple[str, DayLine]] = []
    pattern = os.path.join(glob.escape(directory), "*" + record.RECORD_SUFFIX)
    for path in sorted(glob.glob(pattern)):
        try:
            data = record.load(path)
        except (OSError, ValueError) as exc:
            found.append(("", DayLine(time="", route="?", path=path,
                                      purpose=f"{os.path.basename(path)} could not be read: "
                                              f"{exc}")))
            continue
        if not isinstance(data, dict):
            continue
        opened = str((data.get("request") or {}).get("opened") or "")
        if opened.startswith(prefix):
            found.append((opened, request_line(data, path)))
    for segment in dayrecord.segments(directory):
        try:
            with open(segment, encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for text in lines:
            try:
                line = json.loads(text)
            except ValueError:
                continue
            if not isinstance(line, dict) or not str(line.get("time") or "").startswith(
                    prefix):
                continue
            made = _day_record_line(line)
            if made is not None:
                found.append((str(line.get("time")), made))
    found.sort(key=lambda pair: pair[0])
    return [line for _, line in found]
