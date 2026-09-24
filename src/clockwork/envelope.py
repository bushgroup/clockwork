"""The standing envelope: what an agent may send to one instrument, declared once.

A `limits.toml` beside the instrument document says, for the instrument as a whole,
which templates an agent may run and how far each knob may be turned, which knobs stay
where they are, which boxes may be addressed, and how much may be done unattended in one
daemon session. `check` answers why a render may not be sent under those limits, and
`cold_start` what the boxes hold that the method does not declare. The MCP server calls
both before anything reaches `send_phases`, so the refusal is the server's and never the
driving agent's judgement (lab record, task 71). The format is
`docs/instrument-limits.md`.

**The authorization is that a person started the daemon at a terminal.** Nothing here
asks for approval; the limits are what that act authorizes, and the budget is counted
from it, so a budget spent is renewed by a person restarting the daemon and by nothing
else (Matt, 2026-09-24).

**Templates only.** The limits narrow a template's own knob ranges and never widen them;
a hand-written method has no knobs to narrow, so against the instrument it is refused
outright, and the window is the surface for one (lab record, task 71).

**The cold-start check is about the boxes.** A method that declares only what its
experiment varies runs on whatever the last experiment left behind, and a cold instrument
left sixteen DC bias channels at 0 V under one (lab record, task 64). The check reads
what each box holds, before the send, and names every setting the method would leave as
found; which of them refuse depends on the family, by the decision of record (Matt,
2026-09-24): an undeclared DC bias channel, an RF head that is on, or an ARB module's
frequency or range refuses, and the rest of an ARB module's settings caution. The person
at the instrument still owns the sample and the source; nothing here reads either.

No Qt, like the layers beneath it.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .acq.loop import declared_differences, left_as_found_settings
from .method import BoxMethod, Method, declared_commands, is_comment
from .method.template import Rendered, Template
from .mips.state import BoxState, declared_settings

__all__ = [
    "COLD_START_MODES",
    "DEPENDS_ON_ARB",
    "HASH_PREFIX_MIN",
    "HAND_WRITTEN",
    "LIMITS_NAME",
    "NO_LIMITS",
    "SCHEMA_VERSION",
    "Budget",
    "Finding",
    "KnobLimit",
    "Ledger",
    "Limits",
    "LimitsError",
    "TemplateLimits",
    "check",
    "cold_start",
    "default_path",
    "judge",
    "load",
    "loads",
]

SCHEMA_VERSION = 1

LIMITS_NAME = "limits.toml"
"""The file name the limits take beside the instrument document, which is where
`clockwork mcp` looks for them when given no `--limits`."""

COLD_START_MODES = ("refuse", "strict", "caution")
"""`refuse` refuses what the experiment depends on and cautions the rest; `strict`
refuses every finding; `caution` refuses none."""

DEPENDS_ON_ARB = ("GWFREQ", "GWFVRNG")
"""The ARB setup block: a module's frequency and range, which every travelling-wave
region runs at. The module's direction, mode and alternate waveform are the rest."""

HASH_PREFIX_MIN = 8
"""The fewest hex digits of a template's hash that name it in the limits."""

NO_LIMITS = (
    "no standing limits are in force for this instrument, so an agent may not send to "
    "its boxes or acquire: start `clockwork mcp` with --limits, or put a limits.toml "
    "beside the instrument document")
"""The refusal every send to a real owner gets when no limits were loaded."""

HAND_WRITTEN = (
    "a hand-written method has no knobs for the standing limits to bound, so an agent "
    "may send only templates to the instrument; run this method from the window")

_HEX = re.compile(r"[0-9a-f]+\Z")

Number = int | float


class LimitsError(ValueError):
    """A limits document failed to parse or validate; every problem is in `.problems`."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


# -- the document -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KnobLimit:
    """How far one knob may be turned: either bound, or both, within the template's own."""

    name: str
    min: Number | None = None
    max: Number | None = None

    def text(self, unit: str = "") -> str:
        suffix = f" {unit}" if unit else ""
        if self.min is not None and self.max is not None:
            return f"{self.min} to {self.max}{suffix}"
        if self.min is not None:
            return f"at least {self.min}{suffix}"
        return f"at most {self.max}{suffix}"


@dataclass(frozen=True, slots=True)
class TemplateLimits:
    """One template an agent may run, named by a prefix of its hash."""

    key: str
    hash: str
    name: str = ""
    """The template's `metadata.name`, if the author wrote it as a cross-check."""
    knobs: tuple[KnobLimit, ...] = ()
    fixed: tuple[tuple[str, Number], ...] = ()
    cold_start: str = ""
    """This template's own cold-start mode, or empty for the document's."""

    def matches(self, template_hash: str) -> bool:
        return template_hash.lower().startswith(self.hash)

    def problems_against(self, template: Template) -> list[str]:
        """Why this entry cannot be applied to `template`: a knob it does not have, a
        range wider than its own, a fixed value outside it, a name that disagrees."""
        problems: list[str] = []
        where = f"limits for template {self.key!r}"
        if self.name and self.name != template.name:
            problems.append(f"{where} name it {self.name!r} and the template with hash "
                            f"{self.hash} is {template.name!r}")
        own = {knob.name: knob for knob in template.knobs}
        for limit in self.knobs:
            knob = own.get(limit.name)
            if knob is None:
                problems.append(f"{where} bound a knob {limit.name!r} the template does "
                                "not have")
                continue
            if limit.min is not None and limit.min < knob.min:
                problems.append(f"{where} put {limit.name}'s minimum at {limit.min}, below "
                                f"the template's own {knob.min}: limits narrow a range "
                                "and never widen it")
            if limit.max is not None and limit.max > knob.max:
                problems.append(f"{where} put {limit.name}'s maximum at {limit.max}, above "
                                f"the template's own {knob.max}: limits narrow a range "
                                "and never widen it")
        for name, value in self.fixed:
            knob = own.get(name)
            if knob is None:
                problems.append(f"{where} fix a knob {name!r} the template does not have")
            elif not knob.min <= value <= knob.max:
                problems.append(f"{where} fix {name} at {value}, outside the template's "
                                f"own {knob.min} to {knob.max}")
            elif knob.integer and not float(value).is_integer():
                problems.append(f"{where} fix {name} at {value} and it takes whole values")
        return problems


@dataclass(frozen=True, slots=True)
class Budget:
    """What one daemon session may do unattended."""

    max_runs: int
    """Acquisitions accepted, each one `acquire` call however many files it makes."""
    max_replicates_per_run: int
    max_hours: float
    """Hours from the daemon's start after which nothing more is sent."""


@dataclass(frozen=True)
class Limits:
    """A loaded limits document."""

    boxes: tuple[str, ...]
    budget: Budget
    templates: tuple[TemplateLimits, ...] = ()
    cold_start: str = "refuse"
    description: str = ""
    path: str = ""

    def entry_for(self, template_hash: str) -> TemplateLimits | None:
        return next((entry for entry in self.templates if entry.matches(template_hash)),
                    None)

    def mode_for(self, rendered: Rendered | None) -> str:
        entry = self.entry_for(rendered.template_hash) if rendered is not None else None
        return (entry.cold_start if entry is not None and entry.cold_start
                else self.cold_start)

    def against(self, templates: Iterable[Template]) -> list[str]:
        """Every problem applying these limits to a library's templates, and every entry
        no template in it matches."""
        problems: list[str] = []
        templates = list(templates)
        for entry in self.templates:
            found = [template for template in templates if entry.matches(template.hash)]
            if not found:
                problems.append(f"limits for template {entry.key!r} name hash {entry.hash} "
                                "and no template in the library has it")
            for template in found:
                problems += entry.problems_against(template)
        return problems


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _unknown(table: Mapping, where: str, known: Sequence[str], problems: list[str]) -> None:
    for key in table:
        if key not in known:
            problems.append(f"{where}.{key} is not a key the limits define "
                            f"({', '.join(known)})")


def _mode(value: object, where: str, problems: list[str]) -> str:
    if value not in COLD_START_MODES:
        problems.append(f"{where} must be one of {', '.join(COLD_START_MODES)}, not "
                        f"{value!r}")
        return ""
    return str(value)


def _template(key: str, raw: object, problems: list[str]) -> TemplateLimits | None:
    where = f"templates.{key}"
    if not isinstance(raw, dict):
        problems.append(f"{where} must be a table")
        return None
    _unknown(raw, where, ("hash", "name", "knobs", "fixed", "cold_start"), problems)
    digest = raw.get("hash")
    if not isinstance(digest, str) or not _HEX.match(digest.lower()) \
            or len(digest) < HASH_PREFIX_MIN:
        problems.append(f"{where}.hash must be at least {HASH_PREFIX_MIN} hex digits of the "
                        "template's hash, as list_templates shows it")
        digest = ""
    name = raw.get("name", "")
    if not isinstance(name, str):
        problems.append(f"{where}.name must be text")
        name = ""
    knobs: list[KnobLimit] = []
    raw_knobs = raw.get("knobs", {})
    if not isinstance(raw_knobs, dict):
        problems.append(f"{where}.knobs must be a table")
        raw_knobs = {}
    for knob, bounds in raw_knobs.items():
        here = f"{where}.knobs.{knob}"
        if not isinstance(bounds, dict):
            problems.append(f"{here} must be a table with min, max or both")
            continue
        _unknown(bounds, here, ("min", "max"), problems)
        low, high = bounds.get("min"), bounds.get("max")
        if low is None and high is None:
            problems.append(f"{here} bounds nothing: give min, max or both")
            continue
        if any(value is not None and not _number(value) for value in (low, high)):
            problems.append(f"{here}'s min and max must be numbers")
            continue
        if low is not None and high is not None and low > high:
            problems.append(f"{here} has min {low} above max {high}")
            continue
        knobs.append(KnobLimit(name=knob, min=low, max=high))
    fixed: list[tuple[str, Number]] = []
    raw_fixed = raw.get("fixed", {})
    if not isinstance(raw_fixed, dict):
        problems.append(f"{where}.fixed must be a table")
        raw_fixed = {}
    for knob, value in raw_fixed.items():
        if not _number(value):
            problems.append(f"{where}.fixed.{knob} must be a number")
        elif any(limit.name == knob for limit in knobs):
            problems.append(f"{where} both bounds and fixes {knob}; a fixed knob has no "
                            "range")
        else:
            fixed.append((knob, value))
    mode = _mode(raw["cold_start"], f"{where}.cold_start", problems) \
        if "cold_start" in raw else ""
    return TemplateLimits(key=key, hash=digest.lower(), name=name, knobs=tuple(knobs),
                          fixed=tuple(fixed), cold_start=mode)


def from_dict(data: Mapping, path: str = "") -> Limits:
    """Validate a parsed limits document; `LimitsError` with every problem found."""
    problems: list[str] = []
    _unknown(data, "limits", ("schema_version", "description", "cold_start", "allow",
                              "budget", "templates"), problems)
    if data.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}, not "
                        f"{data.get('schema_version')!r}")
    description = data.get("description", "")
    if not isinstance(description, str):
        problems.append("description must be text")
        description = ""
    mode = _mode(data.get("cold_start", "refuse"), "cold_start", problems) or "refuse"

    boxes: tuple[str, ...] = ()
    allow = data.get("allow")
    if not isinstance(allow, dict):
        problems.append("[allow] is required, with boxes = the boxes an agent may address")
    else:
        _unknown(allow, "allow", ("boxes",), problems)
        raw = allow.get("boxes")
        if not isinstance(raw, list) or not raw \
                or not all(isinstance(name, str) and name for name in raw):
            problems.append("allow.boxes must be a list of box names, at least one")
        else:
            boxes = tuple(raw)

    budget = Budget(0, 0, 0.0)
    raw_budget = data.get("budget")
    if not isinstance(raw_budget, dict):
        problems.append("[budget] is required, with max_runs, max_replicates_per_run and "
                        "max_hours")
    else:
        _unknown(raw_budget, "budget", ("max_runs", "max_replicates_per_run", "max_hours"),
                 problems)
        values: dict[str, Number] = {}
        for key, whole in (("max_runs", True), ("max_replicates_per_run", True),
                           ("max_hours", False)):
            value = raw_budget.get(key)
            if not _number(value) or value <= 0 or (whole and not isinstance(value, int)):
                problems.append(f"budget.{key} must be a positive "
                                f"{'whole number' if whole else 'number'}")
            else:
                values[key] = value
        if len(values) == 3:
            budget = Budget(int(values["max_runs"]), int(values["max_replicates_per_run"]),
                            float(values["max_hours"]))

    templates: list[TemplateLimits] = []
    raw_templates = data.get("templates", {})
    if not isinstance(raw_templates, dict):
        problems.append("[templates] must be a table of tables, one per template")
        raw_templates = {}
    for key, raw in raw_templates.items():
        entry = _template(key, raw, problems)
        if entry is not None:
            templates.append(entry)
    for index, first in enumerate(templates):
        for second in templates[index + 1:]:
            if first.hash and second.hash and (first.hash.startswith(second.hash)
                                               or second.hash.startswith(first.hash)):
                problems.append(f"templates.{first.key} and templates.{second.key} name "
                                "hashes one of which begins the other, so one template "
                                "would match both")
    if problems:
        raise LimitsError(problems)
    return Limits(boxes=boxes, budget=budget, templates=tuple(templates), cold_start=mode,
                  description=description, path=path)


def loads(text: str, path: str = "") -> Limits:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LimitsError([f"invalid TOML: {exc}"]) from exc
    return from_dict(data, path)


def load(path: str) -> Limits:
    with open(path, encoding="utf-8") as handle:
        return loads(handle.read(), os.path.abspath(path))


def default_path(instrument_path: str) -> str:
    """Where the limits sit for an instrument document: beside it, as `limits.toml`."""
    if not instrument_path:
        return ""
    return os.path.join(os.path.dirname(os.path.abspath(instrument_path)), LIMITS_NAME)


# -- the check ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ledger:
    """What this daemon session has spent: acquisitions accepted, and since when."""

    runs: int = 0
    started: _dt.datetime | None = None
    now: _dt.datetime | None = None

    @property
    def hours(self) -> float:
        if self.started is None:
            return 0.0
        now = self.now or _dt.datetime.now()
        return max(0.0, (now - self.started).total_seconds() / 3600.0)


def check(method: Method | None, rendered: Rendered | None, limits: Limits | None,
          ledger: Ledger, *, real: bool, replicates: int | None = None) -> list[str]:
    """Why `method` may not be sent under `limits`; empty if it may.

    `rendered` is the render the method came from, or None for a hand-written method;
    `method` None asks only whether anything may be sent at all: the limits' presence
    and the budget. `real` is whether the boxes are the instrument's. `replicates` is
    given by `acquire` and not by `arm`. With no limits a real owner refuses everything
    and a rehearsal nothing. One sentence per reason.
    """
    if limits is None:
        return [NO_LIMITS] if real else []
    problems: list[str] = []
    if method is not None:
        if rendered is None:
            if real:
                problems.append(HAND_WRITTEN)
        else:
            problems += _template_problems(rendered, limits)
        outside = [box.name for box in method.boxes if box.name not in limits.boxes]
        if outside:
            problems.append(
                f"the standing limits do not allow {', '.join(outside)}: an agent may "
                f"address {', '.join(limits.boxes)}")
    budget = limits.budget
    if ledger.runs >= budget.max_runs:
        problems.append(
            f"this daemon session has made its {budget.max_runs} acquisitions, which is "
            "the standing budget: a person restarting clockwork serve renews it")
    if ledger.hours >= budget.max_hours:
        problems.append(
            f"this daemon session started {ledger.hours:.1f} h ago and the standing budget "
            f"is {budget.max_hours:g} h: a person restarting clockwork serve renews it")
    if replicates is not None and replicates > budget.max_replicates_per_run:
        problems.append(
            f"{replicates} replicates is more than the {budget.max_replicates_per_run} the "
            "standing limits allow in one acquisition")
    return problems


def _template_problems(rendered: Rendered, limits: Limits) -> list[str]:
    entry = limits.entry_for(rendered.template_hash)
    template = rendered.template
    label = template.name if template is not None and template.name else "this template"
    if entry is None:
        return [f"{label} (hash {rendered.template_hash[:12]}) is not in the standing "
                "limits, so an agent may not run it"]
    if template is not None:
        invalid = entry.problems_against(template)
        if invalid:
            return [f"the standing limits for {label} cannot be applied: " + "; ".join(invalid)]
    units = {knob.name: knob.unit for knob in template.knobs} if template is not None else {}
    problems: list[str] = []
    for limit in entry.knobs:
        value = rendered.knobs.get(limit.name)
        if value is None:
            continue
        if (limit.min is not None and value < limit.min) \
                or (limit.max is not None and value > limit.max):
            unit = units.get(limit.name, "")
            problems.append(
                f"{limit.name} = {value}{' ' + unit if unit else ''} is outside the standing "
                f"limits for {label}, {limit.text(unit)}")
    for name, value in entry.fixed:
        asked = rendered.knobs.get(name)
        if asked is not None and asked != value:
            unit = units.get(name, "")
            problems.append(
                f"{name} is fixed at {value}{' ' + unit if unit else ''} by the standing "
                f"limits for {label} and was asked for {asked}")
    return problems


# -- the cold-start check -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing a box holds that its method does not declare, or holds differently."""

    box: str
    setting: str
    """`dc_bias`, `rf`, an ARB getter such as `GWFDIR`, `declared`, or `unread`."""
    text: str
    depends: bool
    """Whether the experiment is known to depend on it: `refuse` mode refuses these."""


def _declared_dc_bias(box: BoxMethod, setup: Sequence[str]) -> set[int] | None:
    """The DC bias channels a box's method sets, or None for every channel."""
    if any(command.strip().upper().startswith("SDCBALL") for command in setup):
        return None
    return {channel for channel, _ in box.dc_bias} | set(
        declared_settings(setup).get("GDCB", {}))


def _declared_rf(box: BoxMethod, setup: Sequence[str]) -> set[int]:
    settings = declared_settings(setup)
    channels = {entry.channel for entry in box.rf}
    for getter in ("GRFFRQ", "GRFDRV", "GRFVLT", "GRFMODE"):
        channels |= set(settings.get(getter, {}))
    return channels


def cold_start(method: Method, states: Iterable[BoxState], *,
               declared: bool = False) -> list[Finding]:
    """What the boxes hold that `method` does not declare, box by box.

    `states` are readings of the boxes, keyed by `BoxState.name`; a box the method
    names and no reading covers is itself a finding. Read before a send, this is what
    the send would leave as found. With `declared`, the readings are the ones a send
    took after its `setup` phase, and each box's disagreements with what the method
    declared are findings too (`clockwork.acq.loop.declared_differences`), a
    monitor's disagreement with its setpoint being a caution only.

    A DC bias channel counts whatever it reads, 0 V included, since 0 V on an electrode
    is a setting. An RF head counts only when its drive is above 0: a box with no RF
    board fitted still answers two heads at 0 % drive (`docs/instrument-limits.md`).
    """
    by_name = {state.name: state for state in states}
    findings: list[Finding] = []
    for box in method.boxes:
        state = by_name.get(box.name)
        if state is None:
            findings.append(Finding(box.name, "unread", (
                f"{box.name} was not read back, so what it holds could not be compared "
                "with the method"), depends=True))
            continue
        setup = [command for command in tuple(box.setup) + declared_commands(box)
                 if not is_comment(command)]
        findings += _dc_bias_findings(box, state, setup)
        findings += _rf_findings(box, state, setup)
        findings += [Finding(box.name, entry.getter, entry.text,
                             depends=entry.getter in DEPENDS_ON_ARB)
                     for entry in left_as_found_settings(box, state)]
        if declared:
            for line in declared_differences(box, state):
                if "monitors were not compared" in line:
                    continue
                findings.append(Finding(box.name, "declared", line,
                                        depends=" and monitors " not in line))
    return findings


def _dc_bias_findings(box: BoxMethod, state: BoxState,
                      setup: Sequence[str]) -> list[Finding]:
    covered = _declared_dc_bias(box, setup)
    if covered is None:
        return []
    loose = [(channel, value) for channel, value in
             enumerate(state.dc_bias_setpoints, start=1) if channel not in covered]
    if not loose:
        return []
    holding = ", ".join(f"{channel}: {'?' if value is None else f'{value:.2f}'} V"
                        for channel, value in loose)
    plural = len(loose) > 1
    return [Finding(box.name, "dc_bias", (
        f"{box.name} DC bias channel{'s' if plural else ''} {'are' if plural else 'is'} not "
        f"declared by the method and hold{'' if plural else 's'} {holding}"), depends=True)]


def _rf_findings(box: BoxMethod, state: BoxState, setup: Sequence[str]) -> list[Finding]:
    covered = _declared_rf(box, setup)
    findings: list[Finding] = []
    for reading in state.rf:
        if reading.channel in covered or not (reading.drive_pct or 0) > 0:
            continue
        frequency = (f"{reading.frequency_hz:g} Hz" if reading.frequency_hz is not None
                     else "an unread frequency")
        findings.append(Finding(box.name, "rf", (
            f"{box.name} RF {reading.channel} is on, {frequency} at "
            f"{reading.drive_pct:.2f}% drive, and the method does not declare it"),
            depends=True))
    return findings


def judge(findings: Iterable[Finding], mode: str) -> tuple[list[str], list[str]]:
    """The findings split into refusals and cautions under a cold-start mode."""
    refused: list[str] = []
    cautioned: list[str] = []
    for finding in findings:
        if mode == "strict" or (mode == "refuse" and finding.depends):
            refused.append(finding.text)
        else:
            cautioned.append(finding.text)
    return refused, cautioned
