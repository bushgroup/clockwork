"""A library of saved methods on disk, and the two diffs a trainee reads them by.

The Qt-free half of the method library (lab record, task 54, window design decision 10):
a directory scanned for documents, each named, hashed, dated and described; a comparison
of two methods against each other; and a comparison of one method against what the boxes
are holding now. `clockwork.app.librarypanel` draws what this module computes -- nothing
here imports Qt, so a clone with no display exercises the whole of it
(`tools/check_public.py`).

**Both diffs are rendered from `to_dict`, never from the TOML text.** A method saved with
different line wrapping or key order is the same method, and a diff built off the text
would report a difference that changes nothing a box is sent. `stamp()`'s hash is the
same rule already applied to provenance; this module applies it to comparison.

**The method-to-instrument diff adds no new comparison.** Task 51 built the mapping from
a `setup` string to the getter that reads it back -- `declared_commands` for the declared
DC bias and RF tables, `declared_settings` for the ARB module getters and every other
indexed setter the wire format names -- and `clockwork.app.boxstate.state_table` already
marks a box's reading against a method's declarations with it. `instrument_diff` below is
the whole of what this module adds: the same comparison, run against a method picked from
the library rather than the one open in the panes.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import glob
import itertools
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .. import method as method_module
from ..method import Method
from .boxstate import Reading, StateTable, state_table

__all__ = [
    "HASH_DIGITS",
    "PHASES",
    "BoxDiff",
    "FieldDiff",
    "LibraryEntry",
    "LineDiff",
    "MethodDiff",
    "instrument_diff",
    "method_diff",
    "open_entry",
    "scan_library",
]

HASH_DIGITS = 12
"""How much of `stamp()`'s sha256 the browser shows.

Long enough that two methods a trainee saved an hour apart do not collide in a library of
a few dozen documents, short enough to sit in one column of a table beside a name and a
date.
"""

PHASES: tuple[str, ...] = ("setup", "load", "arm")


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    """One document a scan found, or one it could not open.

    `problem` is set instead of raising: a library is read by pointing a directory
    setting at it, and one trainee's typo-laden save must not stop the browser from
    listing every other file in the folder. An entry with a problem carries no hash or
    metadata and cannot be opened, diffed or picked as the second half of a diff.
    """

    path: str
    name: str = ""
    hash: str = ""
    created: _dt.date | None = None
    description: str = ""
    problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem


def scan_library(directory: str) -> list[LibraryEntry]:
    """Every `.toml` document under `directory`, named, hashed, dated and described.

    Recursive, because a golden experiment's method sits beside its `.uimf` files and its
    own `README.md` in a directory of its own, not flat in the library root (lab record,
    task 54's `golden` directory is one such library). An empty or missing directory is
    an empty library, not an error -- the setting defaults to nothing on a clone with no
    lab material.

    Sorted by name, which is what a trainee scans a list of experiments by; two documents
    that declare the same name sort next to each other by path, so a stale copy is easy to
    spot beside the one still in use.
    """
    if not directory or not os.path.isdir(directory):
        return []
    paths = sorted(glob.glob(os.path.join(directory, "**", "*.toml"), recursive=True))
    entries = [_entry(path) for path in paths]
    return sorted(entries, key=lambda entry: (entry.name or entry.path, entry.path))


def _entry(path: str) -> LibraryEntry:
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        loaded = method_module.load(path)
    except (OSError, method_module.MethodError) as exc:
        return LibraryEntry(path=path, name=stem, problem=str(exc))
    digest = method_module.stamp(loaded)["method_hash"][:HASH_DIGITS]
    return LibraryEntry(
        path=path,
        name=loaded.metadata.name,
        hash=digest,
        created=loaded.metadata.created,
        description=loaded.metadata.description,
    )


def open_entry(entry: LibraryEntry) -> Method:
    """Load the document an entry names. Raises where `entry.problem` already says why."""
    return method_module.load(entry.path)


# --- method-to-method ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineDiff:
    """One line of a phase or a method-level sequence, aligned between two methods.

    `kind` is `equal` (both sides have this line), `changed` (both sides have a line
    here and it differs), `added` (only `b` has one) or `removed` (only `a` does). A box
    that exists in one method and not the other is every one of its lines reported
    `added` or `removed`, which falls out of diffing an empty sequence against a full one
    rather than needing a case of its own.
    """

    kind: str
    a: str = ""
    b: str = ""


@dataclass(frozen=True, slots=True)
class FieldDiff:
    """One named value, as both methods state it.

    Used for `[acquisition]`, and for a declared DC bias channel or RF field: the label
    names what it is, `a` and `b` are rendered the way a trainee reads them, and
    `differs` is precomputed so a renderer does not do its own string comparison and risk
    disagreeing with this module about what counts as the same value.
    """

    label: str
    a: str = ""
    b: str = ""
    differs: bool = False


@dataclass(frozen=True, slots=True)
class BoxDiff:
    """One box's three phases and its declared tables, in both methods.

    `in_a`/`in_b` say whether the box exists in each method at all -- a box present in
    only one still gets a full set of phase diffs, every line `added` or `removed`, but a
    renderer showing "box removed" as its own line needs to know which side lost it.
    """

    name: str
    in_a: bool
    in_b: bool
    phases: Mapping[str, tuple[LineDiff, ...]] = field(default_factory=dict)
    declared: tuple[FieldDiff, ...] = ()

    @property
    def changed(self) -> bool:
        return any(row.kind != "equal" for rows in self.phases.values() for row in rows) \
            or any(field_.differs for field_ in self.declared)


@dataclass(frozen=True, slots=True)
class MethodDiff:
    """Two methods, compared field by field and line by line.

    Built from `to_dict`'s shape, never from either document's TOML text: a method saved
    with different wrapping or key order is the same method, and comparing text would
    report a difference that changes nothing a box is sent.
    """

    name_a: str
    name_b: str
    metadata: tuple[FieldDiff, ...]
    acquisition: tuple[FieldDiff, ...]
    start: tuple[LineDiff, ...]
    reset: tuple[LineDiff, ...]
    boxes: tuple[BoxDiff, ...]

    @property
    def identical(self) -> bool:
        """True where nothing a stamp would hash differs -- metadata is not part of that
        hash's business here since it is compared on its own line above."""
        return (
            not any(f.differs for f in self.acquisition)
            and not any(row.kind != "equal" for row in self.start)
            and not any(row.kind != "equal" for row in self.reset)
            and not any(box.changed for box in self.boxes)
        )


def method_diff(a: Method, b: Method) -> MethodDiff:
    """Compare two methods: every box's phases, the acquisition settings, the
    declarations, and the two method-level sequences.

    Order follows `a`: its boxes first in its own order, then any box `b` names that `a`
    does not. The same rule the schema itself uses nowhere -- boxes have no declared
    order -- but a diff has to pick one, and "the order the first method listed them in"
    is the one a trainee comparing "today's method" against "the one that worked" reads
    top to bottom without hunting.
    """
    metadata = (
        FieldDiff("name", a.metadata.name, b.metadata.name,
                  a.metadata.name != b.metadata.name),
        FieldDiff("created", str(a.metadata.created), str(b.metadata.created),
                  a.metadata.created != b.metadata.created),
        FieldDiff("description", a.metadata.description, b.metadata.description,
                  a.metadata.description != b.metadata.description),
    )
    acquisition = _acquisition_diff(a, b)
    start = _line_diff(_steps(a.start), _steps(b.start))
    reset = _line_diff(_steps(a.reset), _steps(b.reset))

    names = [box.name for box in a.boxes]
    names += [box.name for box in b.boxes if box.name not in names]
    boxes = tuple(_box_diff(name, a, b) for name in names)
    return MethodDiff(
        name_a=a.metadata.name, name_b=b.metadata.name,
        metadata=metadata, acquisition=acquisition,
        start=start, reset=reset, boxes=boxes,
    )


def _acquisition_diff(a: Method, b: Method) -> tuple[FieldDiff, ...]:
    aa, ab = a.acquisition, b.acquisition
    rows = [
        FieldDiff("frames", str(aa.frames), str(ab.frames), aa.frames != ab.frames),
        FieldDiff("scans", str(aa.scans), str(ab.scans), aa.scans != ab.scans),
        FieldDiff("accumulations", str(aa.accumulations), str(ab.accumulations),
                  aa.accumulations != ab.accumulations),
        FieldDiff("repetition_mode", aa.repetition_mode, ab.repetition_mode,
                  aa.repetition_mode != ab.repetition_mode),
        FieldDiff("keep_raw", str(aa.keep_raw), str(ab.keep_raw),
                  aa.keep_raw != ab.keep_raw),
        FieldDiff("file_stem", aa.file_stem, ab.file_stem, aa.file_stem != ab.file_stem),
    ]
    enable_a = f"{aa.enable.box},{aa.enable.channel}" if aa.enable else ""
    enable_b = f"{ab.enable.box},{ab.enable.channel}" if ab.enable else ""
    rows.append(FieldDiff("enable", enable_a, enable_b, enable_a != enable_b))
    return tuple(rows)


def _steps(steps: Sequence) -> tuple[str, ...]:
    return tuple(f"{step.box}: {step.command}" for step in steps)


def _box_diff(name: str, a: Method, b: Method) -> BoxDiff:
    box_a = _find(a, name)
    box_b = _find(b, name)
    phases = {
        phase: _line_diff(
            getattr(box_a, phase) if box_a else (),
            getattr(box_b, phase) if box_b else (),
        )
        for phase in PHASES
    }
    declared = _declared_diff(box_a, box_b)
    return BoxDiff(name=name, in_a=box_a is not None, in_b=box_b is not None,
                    phases=phases, declared=declared)


def _find(method: Method, name: str):
    try:
        return method.box(name)
    except KeyError:
        return None


def _declared_diff(box_a, box_b) -> tuple[FieldDiff, ...]:
    rows: list[FieldDiff] = []
    bias_a = dict(box_a.dc_bias) if box_a else {}
    bias_b = dict(box_b.dc_bias) if box_b else {}
    for channel in sorted(set(bias_a) | set(bias_b)):
        va = f"{bias_a[channel]:.2f} V" if channel in bias_a else ""
        vb = f"{bias_b[channel]:.2f} V" if channel in bias_b else ""
        rows.append(FieldDiff(f"DC bias {channel}", va, vb,
                              bias_a.get(channel) != bias_b.get(channel)))
    rf_a = {entry.channel: entry for entry in (box_a.rf if box_a else ())}
    rf_b = {entry.channel: entry for entry in (box_b.rf if box_b else ())}
    for channel in sorted(set(rf_a) | set(rf_b)):
        entry_a, entry_b = rf_a.get(channel), rf_b.get(channel)
        for attr, unit in (
            ("frequency_hz", " Hz"), ("drive_pct", "%"),
            ("voltage_v", " V"), ("mode", ""),
        ):
            va = _rf_field(entry_a, attr, unit)
            vb = _rf_field(entry_b, attr, unit)
            if not va and not vb:
                continue
            rows.append(FieldDiff(
                f"RF {channel} {attr.replace('_', ' ')}", va, vb, va != vb))
    return tuple(rows)


def _rf_field(entry, attr: str, unit: str) -> str:
    if entry is None:
        return ""
    value = getattr(entry, attr)
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}{unit}"
    return f"{value}{unit}"


def _line_diff(a: Sequence[str], b: Sequence[str]) -> tuple[LineDiff, ...]:
    """Line-align two string sequences, `replace` opcodes collapsed to one row per line.

    `autojunk=False`: `SequenceMatcher`'s default heuristic treats a line that recurs
    often as noise and stops matching on it, which is the wrong call on a table of MIPS
    commands where the same head recurs by design (`SMOD,TBL` ends both `arm` phases of
    the CLOCK method). Off, every line is a candidate match regardless of how often it
    repeats.
    """
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    rows: list[LineDiff] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        left, right = a[i1:i2], b[j1:j2]
        if op == "equal":
            rows += [LineDiff("equal", x, y) for x, y in zip(left, right, strict=True)]
        elif op == "delete":
            rows += [LineDiff("removed", x, "") for x in left]
        elif op == "insert":
            rows += [LineDiff("added", "", y) for y in right]
        else:  # replace
            for x, y in itertools.zip_longest(left, right, fillvalue=None):
                if x is None:
                    rows.append(LineDiff("added", "", y))
                elif y is None:
                    rows.append(LineDiff("removed", x, ""))
                else:
                    rows.append(LineDiff("changed", x, y))
    return tuple(rows)


# --- method-to-instrument -------------------------------------------------------------


def instrument_diff(picked: Method, readings: Mapping[str, Reading]) -> dict[str, StateTable]:
    """Every box `picked` names, marked against its last reading in `readings`.

    Thin glue over `clockwork.app.boxstate.state_table`, task 51's declared-versus-read
    comparison: nothing here decides which setter a getter reads back, `state_table`
    already does, and this only points it at a method picked from the library instead of
    the one built from the open panes. A box the picked method names that nothing has
    read yet still gets an entry, `state_table`'s own "not read yet".
    """
    return {box.name: state_table(readings.get(box.name, Reading()), box)
            for box in picked.boxes}
