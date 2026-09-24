"""Instrument routines: experiments an instrument needs run anyway, with nothing left to choose.

A routine is a small TOML document (`docs/routines.md`): a template at fixed knob values and a
replicate count, or a list of documents to read the boxes back against; the summary functions
to run on each file and their arguments; the criteria that turn the numbers into a verdict;
and what else the report shows. It is **a request with no free parameters** (lab record,
task 76): it is sent through the same tools as any other request, so the interlock, the
standing limits, the cold-start check, the budget, the audit log and the run record all apply
to it unchanged, and the only thing a routine adds is the judgement at the end.

This module is the half that needs no owner: loading and checking a routine, comparing what
the boxes hold with what a document declares (`audit`), running the summary functions on a
run's files (`measure`), judging the numbers (`judge`), finding the last run that passed
(`last_passing`) and shaping the report. `clockwork.mcp.tools.Toolbox.run_routine` does the
rest, through its own `arm` and `acquire`.

**The verdict** is `pass`, `fail`, or `could not judge` with a reason. A criterion is met or
not; one whose `unmet` names a reason (such as "no beam") says that the others mean nothing
when it is not met, so it is checked first and its reason is the verdict's. A criterion that
cannot be evaluated at all -- a number missing from a result, a reference file that will not
open -- makes the verdict `could not judge` next, since a routine that cannot look must not
say pass; only then do the ordinary criteria decide between pass and fail. A criterion
against the last passing run is skipped, not failed, when there is none, and one marked
`judge = false` is shown and never decides anything.

Qt-free, under the seam with the three lower layers.
"""

from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import json
import math
import numbers
import os
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import summary
from .acq.loop import DC_BIAS_TOLERANCE_V, RF_DRIVE_TOLERANCE_PCT, RF_FREQUENCY_TOLERANCE
from .method import Method, declared_commands, is_comment
from .mips.state import ARB_MODULE_GETTERS, BoxState, declared_settings
from .record import RECORD_SUFFIX

__all__ = [
    "FUNCTIONS",
    "REFERENCES",
    "ROUTINES_NAME",
    "ROUTINE_SCHEMA",
    "SETTINGS",
    "UNMET_FAIL",
    "VERDICTS",
    "Acquisition",
    "AuditEntry",
    "Criterion",
    "Measure",
    "Routine",
    "RoutineError",
    "audit",
    "default_directory",
    "is_routine",
    "judge",
    "last_passing",
    "load",
    "loads",
    "measure",
    "report_text",
    "request_words",
    "scan",
    "value_at",
]

ROUTINE_SCHEMA = 1

ROUTINES_NAME = "routines"
"""The directory routines are found in when none is named: beside the method library."""

FUNCTIONS: Mapping[str, Callable[..., dict]] = {
    "summarize": summary.summarize,
    "windowed": summary.windowed,
    "atd": summary.atd,
    "ion_events": summary.ion_events,
}
"""The summary functions a measure may name, as `clockwork.summary` has them."""

FILES = ("raw", "summed")
"""Which file of a run's pair a measure reads."""

REFERENCES = ("golden", "last-passing", "pair", "file")
"""What a criterion's number is compared with, besides a bound of its own."""

SETTINGS = ("dc_bias", "rf", "arb")
"""The families of box setting an audit compares."""

UNMET_FAIL = "fail"

VERDICTS = ("pass", "fail", "could not judge")

ARB_TOLERANCE = RF_FREQUENCY_TOLERANCE
"""How far, as a fraction, a numeric ARB setting may read back from what was declared.

The same fraction as an RF frequency's, for the same reason: what these boxes do to a
requested frequency is quantise it (`docs/mips-wire-format.md`, section 6.2)."""


class RoutineError(ValueError):
    """A routine document that will not load, with every problem found."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


# -- the document -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Acquisition:
    """What a routine acquires: a template at fixed values, some number of times."""

    template: str
    """A path in the method library, or an absolute path."""
    knobs: Mapping[str, float] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    replicates: int = 1
    conditions: str = ""


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One document a routine reads the boxes back against."""

    name: str
    document: str
    """A method or a template in the library; a template is rendered at its defaults."""
    settings: tuple[str, ...] = SETTINGS
    knobs: Mapping[str, float] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Measure:
    """One summary function run on each file of the run."""

    name: str
    function: str
    file: str = "summed"
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Criterion:
    """One number, one comparison."""

    name: str
    value: str
    """A dotted path: a measure's or an audit entry's name, then keys into its result."""
    at_least: float | None = None
    at_most: float | None = None
    reference: str = ""
    """One of `REFERENCES`, or empty for a bound of the criterion's own."""
    golden: float | None = None
    file: str = ""
    factor: float | None = None
    """For a reference: the value must lie within this factor of it, either way."""
    unmet: str = UNMET_FAIL
    """What it means when the criterion is not met: `fail`, or a reason the routine could
    not judge, such as `no beam`."""
    judge: bool = True
    """False for a comparison the report shows and the verdict ignores."""
    source: str = ""
    """Where the numbers came from, in words."""

    def describe(self) -> str:
        """The comparison in words, such as "at least 0.02" or "within 1.25x of 5240"."""
        parts = []
        if self.at_least is not None:
            parts.append(f"at least {self.at_least:g}")
        if self.at_most is not None:
            parts.append(f"at most {self.at_most:g}")
        if self.reference == "golden":
            parts.append(f"within {self.factor:g}x of {self.golden:g}")
        elif self.reference == "last-passing":
            parts.append(f"within {self.factor:g}x of the last passing run")
        elif self.reference == "pair":
            parts.append(f"the run's files within {self.factor:g}x of each other")
        elif self.reference == "file":
            parts.append(f"within {self.factor:g}x of {os.path.basename(self.file)}")
        return " and ".join(parts)


@dataclass(frozen=True, slots=True)
class Routine:
    """A routine document, loaded and checked."""

    name: str
    description: str
    unattended: bool
    """Whether a session with nobody at the instrument may run it (Matt, 2026-09-24). The
    server does not enforce it; the session that runs routines is told to."""
    acquire: Acquisition | None
    measures: tuple[Measure, ...]
    audit: tuple[AuditEntry, ...]
    criteria: tuple[Criterion, ...]
    report: tuple[str, ...]
    """Further values the report shows, as dotted paths, judged by nothing."""
    path: str = ""
    text: str = ""

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def directory(self) -> str:
        return os.path.dirname(self.path) if self.path else os.getcwd()

    def reference_path(self, criterion: Criterion) -> str:
        """A `file` criterion's reference, resolved against the routine's own directory."""
        if not criterion.file or os.path.isabs(criterion.file):
            return criterion.file
        return os.path.normpath(os.path.join(self.directory, criterion.file))


def is_routine(data: Mapping[str, object]) -> bool:
    """Whether a parsed TOML document is a routine rather than a method or a template."""
    return "routine_schema" in data


def default_directory(library: str) -> str:
    """Where routines are found for a method library: `routines` beside it."""
    if not library:
        return ""
    return os.path.join(os.path.dirname(os.path.abspath(library)), ROUTINES_NAME)


def _number(value: object) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool) \
        and math.isfinite(float(value))


def _unknown(table: Mapping, where: str, known: Sequence[str], problems: list[str]) -> None:
    for key in table:
        if key not in known:
            problems.append(f"{where}: unknown key {key!r} (known: {', '.join(known)})")


def _table(data: Mapping, key: str, where: str, problems: list[str]) -> Mapping:
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        problems.append(f"{where}.{key}: expected a table")
        return {}
    return value


def _text(data: Mapping, key: str, where: str, problems: list[str], *,
          required: bool = False) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        problems.append(f"{where}.{key}: expected text")
        return ""
    if required and not value.strip():
        problems.append(f"{where}.{key}: required")
    return value.strip()


def _knobs(data: Mapping, where: str, problems: list[str]) -> dict[str, float]:
    knobs = _table(data, "knobs", where, problems)
    out = {}
    for name, value in knobs.items():
        if not _number(value):
            problems.append(f"{where}.knobs.{name}: expected a number")
            continue
        out[str(name)] = value
    return out


def _labels(data: Mapping, where: str, problems: list[str]) -> dict[str, str]:
    labels = _table(data, "labels", where, problems)
    out = {}
    for name, value in labels.items():
        if not isinstance(value, str):
            problems.append(f"{where}.labels.{name}: expected text")
            continue
        out[str(name)] = value
    return out


def _acquisition(data: object, problems: list[str]) -> Acquisition | None:
    if not isinstance(data, Mapping):
        problems.append("acquire: expected a table")
        return None
    _unknown(data, "acquire", ("template", "knobs", "labels", "replicates", "conditions"),
             problems)
    replicates = data.get("replicates", 1)
    if not isinstance(replicates, int) or isinstance(replicates, bool) or replicates < 1:
        problems.append("acquire.replicates: a whole number of files, from 1")
        replicates = 1
    return Acquisition(
        template=_text(data, "template", "acquire", problems, required=True),
        knobs=_knobs(data, "acquire", problems), labels=_labels(data, "acquire", problems),
        replicates=replicates, conditions=_text(data, "conditions", "acquire", problems))


def _audit(data: object, problems: list[str]) -> tuple[AuditEntry, ...]:
    if not isinstance(data, list):
        problems.append("audit: expected an array of tables, [[audit]]")
        return ()
    entries = []
    for number, raw in enumerate(data, start=1):
        where = f"audit {number}"
        if not isinstance(raw, Mapping):
            problems.append(f"{where}: expected a table")
            continue
        _unknown(raw, where, ("name", "document", "settings", "knobs", "labels"), problems)
        settings = raw.get("settings", list(SETTINGS))
        if (not isinstance(settings, list) or not settings
                or any(item not in SETTINGS for item in settings)):
            problems.append(f"{where}.settings: a list of {', '.join(SETTINGS)}")
            settings = list(SETTINGS)
        entries.append(AuditEntry(
            name=_text(raw, "name", where, problems, required=True),
            document=_text(raw, "document", where, problems, required=True),
            settings=tuple(dict.fromkeys(settings)),
            knobs=_knobs(raw, where, problems), labels=_labels(raw, where, problems)))
    return tuple(entries)


def _measures(data: object, problems: list[str]) -> tuple[Measure, ...]:
    if not isinstance(data, list):
        problems.append("measure: expected an array of tables, [[measure]]")
        return ()
    found = []
    for number, raw in enumerate(data, start=1):
        where = f"measure {number}"
        if not isinstance(raw, Mapping):
            problems.append(f"{where}: expected a table")
            continue
        _unknown(raw, where, ("name", "function", "file", "arguments"), problems)
        function = _text(raw, "function", where, problems, required=True)
        if function and function not in FUNCTIONS:
            problems.append(f"{where}.function: {function!r} is not one of "
                            f"{', '.join(FUNCTIONS)}")
        chosen = _text(raw, "file", where, problems) or "summed"
        if chosen not in FILES:
            problems.append(f"{where}.file: one of {', '.join(FILES)}")
        found.append(Measure(name=_text(raw, "name", where, problems, required=True),
                             function=function, file=chosen,
                             arguments=dict(_table(raw, "arguments", where, problems))))
    return tuple(found)


def _criteria(data: object, problems: list[str]) -> tuple[Criterion, ...]:
    if not isinstance(data, list):
        problems.append("criterion: expected an array of tables, [[criterion]]")
        return ()
    found = []
    known = ("name", "value", "at_least", "at_most", "reference", "golden", "file", "factor",
             "unmet", "judge", "source")
    for number, raw in enumerate(data, start=1):
        where = f"criterion {number}"
        if not isinstance(raw, Mapping):
            problems.append(f"{where}: expected a table")
            continue
        _unknown(raw, where, known, problems)
        numbers_given = {}
        for key in ("at_least", "at_most", "golden", "factor"):
            if key in raw:
                if _number(raw[key]):
                    numbers_given[key] = float(raw[key])
                else:
                    problems.append(f"{where}.{key}: expected a number")
        reference = _text(raw, "reference", where, problems)
        value = _text(raw, "value", where, problems, required=True)
        name = _text(raw, "name", where, problems) or value
        if reference and reference not in REFERENCES:
            problems.append(f"{where}.reference: one of {', '.join(REFERENCES)}")
        factor = numbers_given.get("factor")
        if reference:
            if factor is None or factor < 1.0:
                problems.append(f"{where}.factor: a reference needs a factor of at least 1")
        elif factor is not None:
            problems.append(f"{where}.factor: a factor needs a reference")
        if reference == "golden" and "golden" not in numbers_given:
            problems.append(f"{where}.golden: a golden reference needs its number")
        if "golden" in numbers_given and reference != "golden":
            problems.append(f"{where}.golden: only a golden reference takes a number")
        if reference == "golden" and numbers_given.get("golden") == 0.0:
            problems.append(f"{where}.golden: a factor of zero means nothing")
        chosen_file = _text(raw, "file", where, problems)
        if reference == "file" and not chosen_file:
            problems.append(f"{where}.file: a file reference needs the file")
        if chosen_file and reference != "file":
            problems.append(f"{where}.file: only a file reference takes a file")
        if not reference and "at_least" not in numbers_given and "at_most" not in numbers_given:
            problems.append(f"{where}: give at_least, at_most, or a reference with a factor")
        judged = raw.get("judge", True)
        if not isinstance(judged, bool):
            problems.append(f"{where}.judge: true or false")
            judged = True
        unmet = _text(raw, "unmet", where, problems) or UNMET_FAIL
        found.append(Criterion(
            name=name, value=value, at_least=numbers_given.get("at_least"),
            at_most=numbers_given.get("at_most"), reference=reference,
            golden=numbers_given.get("golden"), file=chosen_file, factor=factor,
            unmet=unmet, judge=judged, source=_text(raw, "source", where, problems)))
    return tuple(found)


def from_dict(data: Mapping, path: str = "", text: str = "") -> Routine:
    """A routine from its parsed document, or `RoutineError` with every problem."""
    problems: list[str] = []
    schema = data.get("routine_schema")
    if schema != ROUTINE_SCHEMA:
        problems.append(f"routine_schema: this clockwork reads {ROUTINE_SCHEMA}, not {schema!r}")
    _unknown(data, "routine", ("routine_schema", "routine", "acquire", "audit", "measure",
                               "criterion", "report"), problems)
    head = _table(data, "routine", "routine", problems)
    _unknown(head, "routine", ("name", "description", "unattended"), problems)
    name = _text(head, "name", "routine", problems, required=True)
    unattended = head.get("unattended", False)
    if not isinstance(unattended, bool):
        problems.append("routine.unattended: true or false")
        unattended = False
    acquisition = _acquisition(data["acquire"], problems) if "acquire" in data else None
    entries = _audit(data["audit"], problems) if "audit" in data else ()
    measures = _measures(data["measure"], problems) if "measure" in data else ()
    criteria = _criteria(data.get("criterion", []), problems)
    shape = _table(data, "report", "report", problems)
    _unknown(shape, "report", ("show",), problems)
    report = shape.get("show", [])
    if not isinstance(report, list) or not all(isinstance(item, str) for item in report):
        problems.append("report.show: a list of dotted value paths")
        report = []
    if (acquisition is None) == (not entries):
        problems.append("a routine either acquires ([acquire]) or reads the boxes back "
                        "([[audit]]), one of the two")
    if entries and measures:
        problems.append("measure: an audit has no files to measure")
    if acquisition is not None and not measures:
        problems.append("measure: a routine that acquires names at least one measure")
    sources = [entry.name for entry in entries] + [item.name for item in measures]
    for duplicate in sorted({item for item in sources if sources.count(item) > 1}):
        problems.append(f"the name {duplicate!r} is given to two measures or audits")
    for criterion in criteria:
        if criterion.value and criterion.value.split(".", 1)[0] not in sources:
            problems.append(f"criterion {criterion.name!r}: {criterion.value!r} names no "
                            f"measure or audit (there are {', '.join(sources) or 'none'})")
        if criterion.reference == "pair" and (acquisition is None
                                              or acquisition.replicates < 2):
            problems.append(f"criterion {criterion.name!r}: a pair reference needs at least "
                            "two replicates")
        if criterion.reference in ("pair", "file", "last-passing") and entries:
            problems.append(f"criterion {criterion.name!r}: an audit is judged by bounds or a "
                            "golden number")
    for item in report:
        if item.split(".", 1)[0] not in sources:
            problems.append(f"report.show: {item!r} names no measure or audit")
    if not criteria:
        problems.append("criterion: a routine judges something; give at least one")
    if problems:
        raise RoutineError(problems)
    return Routine(name=name, description=_text(head, "description", "routine", problems),
                   unattended=unattended, acquire=acquisition, measures=measures,
                   audit=entries, criteria=criteria, report=tuple(report),
                   path=os.path.abspath(path) if path else "", text=text)


def loads(text: str, path: str = "") -> Routine:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RoutineError([f"not TOML: {exc}"]) from exc
    return from_dict(data, path, text)


def load(path: str) -> Routine:
    with open(path, encoding="utf-8") as handle:
        return loads(handle.read(), path)


def scan(directory: str) -> list[tuple[str, Routine | RoutineError]]:
    """Every routine document in `directory`, loaded or with what stops it, by path."""
    if not directory or not os.path.isdir(directory):
        return []
    found: list[tuple[str, Routine | RoutineError]] = []
    for path in sorted(glob.glob(os.path.join(glob.escape(directory), "*.toml"))):
        try:
            with open(path, "rb") as handle:
                if not is_routine(tomllib.load(handle)):
                    continue
        except (OSError, tomllib.TOMLDecodeError):
            continue
        try:
            found.append((path, load(path)))
        except (OSError, RoutineError) as exc:
            found.append((path, exc if isinstance(exc, RoutineError)
                          else RoutineError([str(exc)])))
    return found


def request_words(routine: Routine, when: _dt.datetime | None = None) -> str:
    """The words a routine's run is requested with: its name, what it asks, and when.

    The time makes every run its own request, where the same words twice in one daemon
    session would otherwise continue one request."""
    moment = (when or _dt.datetime.now()).isoformat(sep=" ", timespec="seconds")
    about = f": {routine.description}" if routine.description else ""
    return f"routine {routine.name}{about} (run {moment})"


# -- reading the boxes back against a document --------------------------------------


def _numeric(text: object) -> float | None:
    try:
        return float(str(text).strip())
    except ValueError:
        return None


def _row(box: str, setting: str, index: int, declared: object, held: object) -> dict:
    return {"box": box, "setting": setting, "index": index, "declared": declared,
            "held": held}


def audit(method: Method, states: Iterable[BoxState],
          settings: Sequence[str] = SETTINGS) -> dict:
    """What the boxes hold against what `method` declares, as a table of differences.

    Compared: every DC bias channel the method sets, against the box's setpoint, to
    `DC_BIAS_TOLERANCE_V`; every RF head's declared frequency, drive and mode, as the
    acquisition loop compares them after a send; every ARB module setting its `setup`
    names, numerically to `ARB_TOLERANCE` where both sides are numbers and as text
    otherwise. `settings` narrows the families compared. A box the method names and no
    reading covers is listed under `unread` and counts as a difference.

    Answers `differences` (rows plus unread boxes), `rows` (box, setting, index, declared,
    held), `unread` and `compared` (how many declared settings were looked at).
    """
    by_name = {state.name: state for state in states}
    rows: list[dict] = []
    unread: list[str] = []
    compared = 0
    for box in method.boxes:
        state = by_name.get(box.name)
        commands = [command for command in tuple(box.setup) + declared_commands(box)
                    if not is_comment(command)]
        declared = declared_settings(commands)
        if state is None:
            if any(declared.get(getter) for getter in _getters(settings)):
                unread.append(box.name)
            continue
        if "dc_bias" in settings:
            for channel, text in sorted(declared.get("GDCB", {}).items()):
                want = _numeric(text)
                held = state.dc_bias(channel)
                compared += 1
                if want is None or held is None or abs(held - want) > DC_BIAS_TOLERANCE_V:
                    rows.append(_row(box.name, "dc_bias", channel, want if want is not None
                                     else text, held))
        if "rf" in settings:
            readings = {reading.channel: reading for reading in state.rf}
            for getter, attribute in (("GRFFRQ", "frequency_hz"), ("GRFDRV", "drive_pct"),
                                      ("GRFMODE", "mode")):
                for channel, text in sorted(declared.get(getter, {}).items()):
                    reading = readings.get(channel)
                    held = getattr(reading, attribute) if reading is not None else None
                    compared += 1
                    if not _rf_agrees(attribute, text, held):
                        rows.append(_row(box.name, f"rf {attribute}", channel,
                                         _numeric(text) if attribute != "mode" else text,
                                         held if held != "" else None))
        if "arb" in settings:
            for getter in ARB_MODULE_GETTERS:
                for module, text in sorted(declared.get(getter, {}).items()):
                    held = state.module(module).get(getter) if module in state.modules \
                        else None
                    compared += 1
                    if not _arb_agrees(text, held):
                        rows.append(_row(box.name, getter, module, text, held))
    return {"differences": len(rows) + len(unread), "rows": rows, "unread": unread,
            "compared": compared}


def _getters(settings: Sequence[str]) -> list[str]:
    getters = []
    if "dc_bias" in settings:
        getters.append("GDCB")
    if "rf" in settings:
        getters += ["GRFFRQ", "GRFDRV", "GRFMODE"]
    if "arb" in settings:
        getters += list(ARB_MODULE_GETTERS)
    return getters


def _rf_agrees(attribute: str, declared: str, held: object) -> bool:
    if held is None or held == "":
        return False
    if attribute == "mode":
        return str(held).strip().upper() == declared.strip().upper()
    want = _numeric(declared)
    if want is None or not isinstance(held, (int, float)):
        return False
    if attribute == "frequency_hz":
        return abs(held - want) <= abs(want) * RF_FREQUENCY_TOLERANCE
    return abs(held - want) <= RF_DRIVE_TOLERANCE_PCT


def _arb_agrees(declared: str, held: str | None) -> bool:
    if held is None:
        return False
    want, got = _numeric(declared), _numeric(held)
    if want is not None and got is not None:
        return abs(got - want) <= max(abs(want) * ARB_TOLERANCE, 1e-9)
    return declared.strip().upper() == str(held).strip().upper()


# -- measuring a run's files ---------------------------------------------------------


def _file_of(run: Mapping[str, object], chosen: str) -> str:
    path = str(run.get("raw_path" if chosen == "raw" else "summed_path") or "")
    return path if path and os.path.isfile(path) else ""


def measure(routine: Routine, runs: Sequence[Mapping[str, object]]) -> list[dict]:
    """Every measure of `routine` on every run, in order: one dict per run, by measure name.

    `runs` carry `raw_path` and `summed_path`. A measure that could not run -- its file
    not on disk, the function refusing the file -- is `{"problem": sentence}` in place of
    its result, which a criterion reading it reports as not evaluated."""
    results = []
    for run in runs:
        found: dict[str, Any] = {}
        for item in routine.measures:
            path = _file_of(run, item.file)
            if not path:
                found[item.name] = {"problem": f"the run left no {item.file} file"}
                continue
            found[item.name] = _call(item, path)
        results.append(found)
    return results


def _call(item: Measure, path: str) -> dict:
    try:
        return FUNCTIONS[item.function](path, **dict(item.arguments))
    except (summary.SummaryError, TypeError, ValueError, OSError) as exc:
        return {"problem": f"{item.function} on {os.path.basename(path)}: {exc}"}


def value_at(results: Mapping[str, object], path: str) -> float:
    """The number at a dotted path into one run's results, or `KeyError` with a sentence."""
    head, _, rest = path.partition(".")
    current: object = results.get(head)
    if isinstance(current, Mapping) and "problem" in current and len(current) == 1:
        raise KeyError(str(current["problem"]))
    walked = head
    for key in rest.split(".") if rest else []:
        if not isinstance(current, Mapping) or key not in current:
            raise KeyError(f"{walked} has no {key!r}")
        current = current[key]
        walked += "." + key
    if not _number(current):
        raise KeyError(f"{path} is {current!r}, not a number")
    return float(current)


# -- the judgement -------------------------------------------------------------------


def last_passing(directory: str, name: str, *, before: str = "") -> dict | None:
    """The latest passing run of routine `name` among the run records in `directory`.

    `before`, a request id, is left out: the run being judged. Answers that run's report
    entry as its record holds it, or None."""
    best: dict | None = None
    for path in glob.glob(os.path.join(glob.escape(directory or "."), "*" + RECORD_SUFFIX)):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(data, Mapping):
            continue
        if before and data.get("request", {}).get("id") == before:
            continue
        for entry in data.get("routines", []) or []:
            if (isinstance(entry, Mapping) and entry.get("routine") == name
                    and entry.get("verdict") == "pass"
                    and (best is None or str(entry.get("time", "")) > str(best.get("time", "")))):
                best = dict(entry, request_id=data.get("request", {}).get("id"))
    return best


def _within(value: float, reference: float, factor: float) -> bool:
    if reference == 0.0:
        return value == 0.0
    if value == 0.0 or (value > 0) != (reference > 0):
        return False
    ratio = value / reference
    return 1.0 / factor <= ratio <= factor


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def judge(routine: Routine, results: Sequence[Mapping[str, object]], *,
          last: Mapping[str, object] | None = None) -> dict:
    """The verdict on `results` -- one mapping per file of the run, or one for an audit.

    Answers `verdict`, `reason`, `criteria` (one entry each: its values, what it was
    compared with, `met` true, false or None for not evaluated or skipped, and why) and
    `values` (every criterion's and report path's number per file, which a later run's
    last-passing comparison reads)."""
    entries: list[dict] = []
    values: dict[str, list[float | None]] = {}

    def read(path: str) -> tuple[list[float], str]:
        found: list[float] = []
        problems: list[str] = []
        for result in results:
            try:
                found.append(value_at(result, path))
            except KeyError as exc:
                problems.append(str(exc.args[0]) if exc.args else str(exc))
        values[path] = found if not problems else [None] * len(results)
        return found, "; ".join(dict.fromkeys(problems))

    for criterion in routine.criteria:
        found, problem = read(criterion.value)
        entry: dict[str, Any] = {
            "name": criterion.name, "value": criterion.value, "values": found,
            "comparison": criterion.describe(), "reference": criterion.reference or None,
            "judged": criterion.judge, "unmet": criterion.unmet, "source": criterion.source,
        }
        if problem or not found:
            entry.update(met=None, why=f"not evaluated: {problem or 'no files'}")
            entries.append(entry)
            continue
        met = True
        why: list[str] = []
        if criterion.at_least is not None and min(found) < criterion.at_least:
            met = False
            why.append(f"{min(found):g} is below {criterion.at_least:g}")
        if criterion.at_most is not None and max(found) > criterion.at_most:
            met = False
            why.append(f"{max(found):g} is above {criterion.at_most:g}")
        reference_value = None
        if criterion.reference == "golden":
            reference_value = criterion.golden
        elif criterion.reference == "last-passing":
            earlier = (last or {}).get("values", {}).get(criterion.value) if last else None
            usable = [item for item in (earlier or []) if _number(item)]
            if not usable:
                entry.update(met=None, why="skipped: no earlier passing run to compare with")
                entries.append(entry)
                continue
            reference_value = _mean(usable)
            entry["reference_request"] = (last or {}).get("request_id")
        elif criterion.reference == "file":
            reference_value, trouble = _reference(routine, criterion)
            if trouble:
                entry.update(met=None, why=f"not evaluated: {trouble}")
                entries.append(entry)
                continue
        elif criterion.reference == "pair":
            low, high = min(found), max(found)
            spread = (high / low) if low > 0 else math.inf
            entry["spread"] = spread
            if spread > criterion.factor:
                met = False
                why.append(f"the files differ {spread:.3g}x, more than {criterion.factor:g}x")
        if reference_value is not None:
            entry["reference_value"] = reference_value
            outside = [item for item in found
                       if not _within(item, reference_value, criterion.factor or 1.0)]
            if outside:
                met = False
                why.append(", ".join(f"{item:g}" for item in outside)
                           + f" is outside {criterion.factor:g}x of {reference_value:g}")
        entry.update(met=met, why="; ".join(why))
        entries.append(entry)

    for path in routine.report:
        if path not in values:
            read(path)

    verdict, reason = "pass", ""
    blocking = [entry for entry in entries if entry["judged"]
                and entry["unmet"] != UNMET_FAIL and entry["met"] is False]
    unevaluated = [entry for entry in entries if entry["judged"] and entry["met"] is None
                   and not entry["why"].startswith("skipped")]
    failed = [entry for entry in entries if entry["judged"] and entry["met"] is False]
    if blocking:
        verdict = "could not judge"
        reason = "; ".join(dict.fromkeys(entry["unmet"] for entry in blocking))
    elif unevaluated:
        verdict = "could not judge"
        reason = "; ".join(f"{entry['name']} {entry['why']}" for entry in unevaluated)
    elif failed:
        verdict = "fail"
        reason = "; ".join(f"{entry['name']}: {entry['why']}" for entry in failed)
    return {"verdict": verdict, "reason": reason, "criteria": entries, "values": values}


def _reference(routine: Routine, criterion: Criterion) -> tuple[float | None, str]:
    """A `file` criterion's number, measured on the reference file the way the run's
    files are."""
    path = routine.reference_path(criterion)
    if not os.path.isfile(path):
        return None, f"the reference file {path} is not on disk"
    head = criterion.value.split(".", 1)[0]
    item = next((entry for entry in routine.measures if entry.name == head), None)
    if item is None:
        return None, f"{head} is not a measure"
    try:
        return value_at({head: _call(item, path)}, criterion.value), ""
    except KeyError as exc:
        return None, f"on the reference file, {exc.args[0] if exc.args else exc}"


def report_text(routine: Routine, judged: Mapping[str, object]) -> str:
    """The report in a few lines a person reads: the verdict, then each criterion."""
    head = f"{routine.name}: {judged['verdict']}"
    if judged.get("reason"):
        head += f" ({judged['reason']})"
    lines = [head]
    for entry in judged.get("criteria", []):
        shown = ", ".join(f"{item:.4g}" for item in entry.get("values") or []) or "none"
        state = {True: "met", False: "NOT met", None: "not judged"}[entry.get("met")]
        if not entry.get("judged"):
            state += ", shown only"
        lines.append(f"  {entry['name']}: {shown}; {entry['comparison']}: {state}"
                     + (f" ({entry['why']})" if entry.get("why") else ""))
    for path in routine.report:
        shown = ", ".join("none" if item is None else f"{item:.4g}"
                          for item in judged.get("values", {}).get(path, []))
        lines.append(f"  {path}: {shown or 'none'}")
    return "\n".join(lines)
