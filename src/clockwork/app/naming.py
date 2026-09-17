"""What a run's files are called, and where the number comes from.

`YYMMDD_INITIALS_NNN` is the lab's own convention, carried over from FALKOR whole:
`260825_BK_025.uimf` and `260904_BK_094.uimf` are two of the golden experiments. The
window names every file this way without asking, because the alternative -- a trainee
typing a stem per acquisition, in the middle of an acquisition day, with a replicate
due every ninety seconds -- is exactly what the window design's decision 5 removes
(lab record, task 50).

Three choices are worth stating, because none of them is the obvious one.

**The counter is scanned off the output directory every time and never stored.** A
number kept in settings is wrong the moment a file is copied in by hand, a directory is
shared between two machines, or a trainee deletes a bad run; a number read off the
directory is right in all three cases and costs one listing. It is the highest number
already there plus one, so a gap left by a deleted file stays a gap rather than being
filled with a name a notebook already uses.

**The counter does not reset per day.** The golden files run 025, 037 and 057 on one
date and reach 094 nine days later, so the number is the operator's running count and
not the day's. Scanning ignores the date part for that reason, and matches on the
initials alone.

**The middle field is whatever the trainee put in the setting.** FALKOR's files use the
ion's code there (`BK` for bradykinin) and the window design calls it the operator's
initials (lab record, task 50); both are two or three characters that identify who or
what the day is about, and this module takes no view. It is remembered per machine and
is editable, as is the whole stem, before Acquire.

Qt-free and tested: a window is not needed to ask what a run will be called, and the
one thing that must never happen -- two acquisitions of an afternoon given one name --
is a property of this function rather than of the button that calls it.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from collections.abc import Iterable

__all__ = [
    "COUNTER_DIGITS",
    "clean_initials",
    "next_number",
    "next_stem",
    "parse_stem",
    "stem",
]

COUNTER_DIGITS = 3
"""Zero-padded width of the counter, from the lab's own files (`260825_BK_025`).

A run past 999 gets a four-digit number rather than wrapping: the padding is a
minimum, not a field width, because a name that collides is worse than a name that
sorts oddly, and `Recording.create` refuses a collision rather than overwriting.
"""

_STEM = re.compile(r"(?P<date>\d{6})_(?P<initials>[^_]+)_(?P<number>\d+)\Z")

_INITIALS_OK = re.compile(r"[^A-Za-z0-9]+")


def clean_initials(text: str) -> str:
    """The initials as they may appear in a file name: alphanumerics, upper case.

    A stem is parsed by splitting on `_`, so an underscore in the initials would make
    the name unreadable by `parse_stem` and unfindable by `next_number`; everything
    else that is not a letter or a digit is dropped for the same reason a file name
    should not carry it. Empty in gives empty out, which the window reports as "set
    your initials" rather than silently naming a file `260917__001`.
    """
    return _INITIALS_OK.sub("", text).upper()


def parse_stem(name: str) -> tuple[str, str, int] | None:
    """`(date, initials, number)` for a name in this shape, or None.

    The name may carry any extension or any of the suffixes a run leaves beside its
    file -- `.uimf`, `.summed.uimf`, `.sent.txt` -- and all of them are stripped, so
    that a directory holding one acquisition's four files reports one number and not
    four.
    """
    base = os.path.basename(name)
    while True:
        root, extension = os.path.splitext(base)
        if not extension or root == base:
            break
        base = root
    found = _STEM.match(base)
    if found is None:
        return None
    return found["date"], found["initials"], int(found["number"])


def next_number(
    directory: str | os.PathLike[str],
    initials: str,
    *,
    taken: Iterable[str] = (),
) -> int:
    """One past the highest number these initials already use in this directory.

    `taken` is stems this session has reserved but may not have written yet, which is
    what keeps an N-replicate series from naming two files the same: the first
    acquisition's raw file appears on disk as soon as `Recording.create` runs, but the
    window asks for every name in the series before any of them exists.

    A directory that cannot be listed -- it has not been created yet, or it is a
    network share that is away -- answers 1 rather than raising. The caller is about
    to write into it and will find out then, with a message about the directory
    instead of about the counter.
    """
    wanted = clean_initials(initials)
    highest = 0
    names: list[str] = list(taken)
    try:
        names += os.listdir(os.fspath(directory))
    except OSError:
        pass
    for name in names:
        parsed = parse_stem(name)
        if parsed is not None and clean_initials(parsed[1]) == wanted:
            highest = max(highest, parsed[2])
    return highest + 1


def stem(initials: str, number: int, when: _dt.date | None = None) -> str:
    """`YYMMDD_INITIALS_NNN` for one number, with no directory involved."""
    day = (when or _dt.date.today()).strftime("%y%m%d")
    return f"{day}_{clean_initials(initials)}_{number:0{COUNTER_DIGITS}d}"


def next_stem(
    directory: str | os.PathLike[str],
    initials: str,
    when: _dt.date | None = None,
    *,
    taken: Iterable[str] = (),
) -> str:
    """What the next acquisition into this directory should be called.

    Today's date, the operator's initials and one past the highest number those
    initials already use there. This is the whole of decision 5, and it is a pure
    function of the directory's contents so that a window can call it to fill a field
    a trainee may then edit.
    """
    return stem(initials, next_number(directory, initials, taken=taken), when)
