"""The standing envelope: what an agent may send to one instrument, declared once.

A `limits.toml` beside the instrument document says, for the instrument as a whole,
which templates an agent may run and how far each knob may be turned, which knobs stay
where they are, the caps no template's knob may pass, which boxes may be addressed, and
how far apart any two electrodes may be. A project may add a file of its own, given
beside the standing one, that admits its own templates and narrows the standing entries
but never widens them (`combine`; lab record, task 159). `check` answers why a render
may not be sent under those limits, and `cold_start` what the boxes hold that the
method does not declare. The MCP server calls both before anything reaches
`send_phases`, so the refusal is the server's and never the driving agent's judgement
(lab record, task 71). The format is `docs/instrument-limits.md`.

**The authorization is that a person started the daemon at a terminal.** Nothing here
asks for approval, and nothing counts what is done under it: schema 1's budget of runs,
replicates and hours per daemon session refused only work that was wanted, and is gone
(Matt, 2026-11-09).

**Templates, and hand-written methods where the potentials are bounded.** The limits
narrow a template's own knob ranges and never widen them; a hand-written method has no
knobs to narrow, so against the instrument it is admitted only where the standing limits
bound every method's potentials, with `[electrodes]` and `[[rf_drive]]`, and refused
where they do not (Matt, 2026-11-10). The boxes it may address and the cold-start check
apply as to any method.

**The cold-start check is about the boxes.** A method that declares only what its
experiment varies runs on whatever the last experiment left behind, and a cold instrument
left sixteen DC bias channels at 0 V under one (lab record, task 64). The check reads
what each box holds, before the send, and names every setting the method would leave as
found; which of them refuse depends on the family, by the decision of record (Matt,
2026-09-24): an undeclared DC bias channel, an RF head that is on, or an ARB module's
frequency or range refuses, and the rest of an ARB module's settings caution. The person
at the instrument still owns the sample and the source; nothing here reads either.

**The electrodes are bounded on the method, not the knobs.** An `[electrodes]` table
names the nets that share the instrument's ion-optical volume and the largest
difference allowed between any two; every method, rendered or hand-written, is refused
if at any moment of its strings two nets would be further apart (`clockwork.electrodes`;
lab record, task 159). Until an RF head's volts are known, `rf_drive` caps its drive at
what the instrument has run at instead (Matt, 2026-11-10).

No Qt, like the layers beneath it.
"""

from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from . import electrodes as _electrodes
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
    "PROJECT_KEYS",
    "SCHEMA_VERSION",
    "STANDING_KEYS",
    "Finding",
    "KnobLimit",
    "Limits",
    "LimitsError",
    "TemplateLimits",
    "check",
    "cold_start",
    "combine",
    "default_path",
    "judge",
    "load",
    "loads",
]

SCHEMA_VERSION = 2
"""Schema 1 had a `[budget]` and `allow.hand_written`; schema 2 has neither, and adds
`[caps]` and the project file."""

STANDING_KEYS = ("schema_version", "description", "cold_start", "allow", "caps",
                 "templates", "electrodes", "rf_drive")
"""The tables and keys of the standing limits, the instrument's."""

PROJECT_KEYS = ("schema_version", "description", "templates")
"""The tables and keys of a project's limits: its own templates' entries and the
standing entries it narrows, nothing about the boxes or the electrodes."""

RETIRED = {
    "budget": ("[budget] was removed in schema 2: nothing counts the runs, replicates or "
               "hours of a daemon session, since a person starting clockwork serve is the "
               "authorization; delete the table"),
    "hand_written": ("allow.hand_written was removed in schema 2: a hand-written method "
                     "is admitted where the standing limits carry [electrodes] and "
                     "[[rf_drive]], which bound every method, and refused where they do "
                     "not; delete the key"),
}
"""Schema 1's keys, each refused with why, so that an old file fails loudly rather than
being read for less than its author meant."""

_MODE_ORDER = {"caution": 0, "refuse": 1, "strict": 2}

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
    "beside the instrument document, which with no --instrument is instrument.toml in "
    "the folder CLOCKWORK_INSTRUMENT names")
"""The refusal every send to a real owner gets when no limits were loaded. It names the
variable because the commonest cause is an instrument directory that did not resolve
(`clockwork.instrument_unresolved`), which nothing else says at the moment of refusal."""

HAND_WRITTEN = (
    "a hand-written method has no knobs for the limits to bound, so an agent may send "
    "one to the instrument only where the standing limits bound every method's "
    "potentials with [electrodes] and [[rf_drive]], and these do not; run this method "
    "from the window")

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

    def holds(self, value: Number) -> bool:
        return ((self.min is None or value >= self.min)
                and (self.max is None or value <= self.max))

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
    origin: str = "standing"
    """`standing`, or `project` for an entry a project's limits added or narrowed."""

    @property
    def whose(self) -> str:
        """The limits this entry speaks for, as a refusal names them."""
        return "the project's limits" if self.origin == "project" else "the standing limits"

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


@dataclass(frozen=True)
class Limits:
    """A loaded limits document: the standing limits, with a project's combined in
    where one was given (`combine`), or a project's file alone as `load` reads it."""

    boxes: tuple[str, ...]
    templates: tuple[TemplateLimits, ...] = ()
    cold_start: str = "refuse"
    description: str = ""
    path: str = ""
    caps: tuple[KnobLimit, ...] = ()
    """The range no template's knob of that name may leave, whatever its entry says."""
    electrodes: _electrodes.Electrodes | None = None
    """The nets that share the volume and the largest difference between them, or None
    where the limits bound no potentials."""
    rf_drive: tuple[_electrodes.RfCap, ...] | None = None
    """The highest drive each RF head may be set to, or None where the limits bound no
    RF drive. Where given, a head it does not name may not be driven at all."""
    project: str = ""
    """The project's limits combined into these, by path, or empty for none."""
    project_description: str = ""

    @property
    def hand_written(self) -> bool:
        """Whether a hand-written method may be sent to the instrument: where the
        limits bound every method's potentials, electrodes and RF both."""
        return self.electrodes is not None and self.rf_drive is not None

    def cap_for(self, knob: str) -> KnobLimit | None:
        return next((cap for cap in self.caps if cap.name == knob), None)

    def entry_for(self, template_hash: str) -> TemplateLimits | None:
        return next((entry for entry in self.templates if entry.matches(template_hash)),
                    None)

    def mode_for(self, rendered: Rendered | None) -> str:
        entry = self.entry_for(rendered.template_hash) if rendered is not None else None
        return (entry.cold_start if entry is not None and entry.cold_start
                else self.cold_start)

    def against(self, templates: Iterable[Template], *,
                unmatched: bool = True) -> list[str]:
        """Every problem applying these limits to a library's templates, and, unless
        `unmatched` is false, every entry no template in it matches.

        The two are different kinds of finding. An entry that cannot be applied to its
        template is a mistake in the limits. An entry with no template is a mistake only
        where the library is meant to hold every template the limits name: one
        instrument's limits name several projects' templates, a daemon serves one
        project's, and the others' entries are then unmatched by design (`unmatched`
        lists them apart)."""
        problems: list[str] = []
        templates = list(templates)
        absent = set(self.unmatched(templates))
        for entry in self.templates:
            if entry.key in absent:
                if unmatched:
                    problems.append(f"limits for template {entry.key!r} name hash "
                                    f"{entry.hash} and no template in the library has it")
                continue
            for template in templates:
                if entry.matches(template.hash):
                    problems += entry.problems_against(template)
        return problems

    def unmatched(self, templates: Iterable[Template]) -> list[str]:
        """The key of every entry whose hash no template in a library has."""
        hashes = [template.hash for template in templates]
        return [entry.key for entry in self.templates
                if not any(entry.matches(digest) for digest in hashes)]


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


def _bounds(where: str, raw: object, problems: list[str]) -> list[KnobLimit]:
    """A table of knob names, each with `min`, `max` or both: an entry's `knobs`, or
    the standing `caps`."""
    if not isinstance(raw, dict):
        problems.append(f"{where} must be a table")
        return []
    knobs: list[KnobLimit] = []
    for knob, bounds in raw.items():
        here = f"{where}.{knob}"
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
    return knobs


def _template(key: str, raw: object, problems: list[str],
              origin: str = "standing") -> TemplateLimits | None:
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
    knobs = _bounds(f"{where}.knobs", raw.get("knobs", {}), problems)
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
                          fixed=tuple(fixed), cold_start=mode, origin=origin)


def _overlap(first: str, second: str) -> bool:
    """Whether two hash prefixes could name one template."""
    return bool(first and second and (first.startswith(second) or second.startswith(first)))


def _beyond_caps(entry: TemplateLimits, caps: Sequence[KnobLimit]) -> list[str]:
    """Where an entry's own bounds or fixed values pass a cap. A side the entry leaves
    open is the template's own, and a render past the cap there is refused instead."""
    problems: list[str] = []
    where = f"limits for template {entry.key!r}"
    for cap in caps:
        for limit in entry.knobs:
            if limit.name != cap.name:
                continue
            if cap.min is not None and limit.min is not None and limit.min < cap.min:
                problems.append(f"{where} put {cap.name}'s minimum at {limit.min}, below the "
                                f"standing cap of {cap.min}")
            if cap.max is not None and limit.max is not None and limit.max > cap.max:
                problems.append(f"{where} put {cap.name}'s maximum at {limit.max}, above the "
                                f"standing cap of {cap.max}")
        for name, value in entry.fixed:
            if name == cap.name and not cap.holds(value):
                problems.append(f"{where} fix {name} at {value}, outside the standing cap "
                                f"of {cap.text()}")
    return problems


def from_dict(data: Mapping, path: str = "", *, project: bool = False) -> Limits:
    """Validate a parsed limits document; `LimitsError` with every problem found.

    `project` reads it as a project's file, which carries only `PROJECT_KEYS`; it is
    then combined into the standing limits with `combine`, and on its own bounds no
    box and admits nothing."""
    problems: list[str] = []
    known = PROJECT_KEYS if project else STANDING_KEYS
    for key in data:
        if key in RETIRED:
            problems.append(RETIRED[key])
        elif project and key in STANDING_KEYS and key not in known:
            problems.append(f"{key} is the instrument's, so a project's limits may not "
                            "carry it: they admit the project's own templates and narrow "
                            "the standing entries, and nothing else")
        elif key not in known:
            problems.append(f"limits.{key} is not a key the limits define "
                            f"({', '.join(known)})")
    version = data.get("schema_version")
    if version == 1:
        problems.append("schema_version is 1, the shape with a [budget] and "
                        f"allow.hand_written: these limits are schema {SCHEMA_VERSION}")
    elif version != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}, not {version!r}")
    description = data.get("description", "")
    if not isinstance(description, str):
        problems.append("description must be text")
        description = ""
    mode = "refuse"
    if not project:
        mode = _mode(data.get("cold_start", "refuse"), "cold_start", problems) or "refuse"

    boxes: tuple[str, ...] = ()
    allow = data.get("allow")
    if project:
        pass
    elif not isinstance(allow, dict):
        problems.append("[allow] is required, with boxes = the boxes an agent may address")
    else:
        for key in allow:
            if key in RETIRED:
                problems.append(RETIRED[key])
            elif key != "boxes":
                problems.append(f"allow.{key} is not a key the limits define (boxes)")
        raw = allow.get("boxes")
        if not isinstance(raw, list) or not raw \
                or not all(isinstance(name, str) and name for name in raw):
            problems.append("allow.boxes must be a list of box names, at least one")
        else:
            boxes = tuple(raw)

    caps = (tuple(_bounds("caps", data["caps"], problems))
            if "caps" in data and not project else ())

    templates: list[TemplateLimits] = []
    raw_templates = data.get("templates", {})
    if not isinstance(raw_templates, dict):
        problems.append("[templates] must be a table of tables, one per template")
        raw_templates = {}
    for key, raw in raw_templates.items():
        entry = _template(key, raw, problems, "project" if project else "standing")
        if entry is not None:
            templates.append(entry)
            problems += _beyond_caps(entry, caps)
    for index, first in enumerate(templates):
        for second in templates[index + 1:]:
            if _overlap(first.hash, second.hash):
                problems.append(f"templates.{first.key} and templates.{second.key} name "
                                "hashes one of which begins the other, so one template "
                                "would match both")
    electrodes = (_electrodes.from_dict(data["electrodes"], problems)
                  if "electrodes" in data and not project else None)
    rf_drive = (_electrodes.rf_drive_from_list(data["rf_drive"], problems)
                if "rf_drive" in data and not project else None)
    if problems:
        raise LimitsError(problems)
    return Limits(boxes=boxes, templates=tuple(templates), cold_start=mode,
                  description=description, path=path, caps=caps, electrodes=electrodes,
                  rf_drive=rf_drive)


def _narrow(base: TemplateLimits, entry: TemplateLimits) -> tuple[TemplateLimits, list[str]]:
    """A standing entry narrowed by a project's entry for the same template, and every
    way the project's entry would widen it instead."""
    problems: list[str] = []
    where = f"the project's limits for template {entry.key!r}"
    if entry.name and base.name and entry.name != base.name:
        problems.append(f"{where} name it {entry.name!r} and the standing entry "
                        f"{base.key!r} for the same hash names it {base.name!r}")
    held = dict(base.fixed)
    bounds = {limit.name: limit for limit in base.knobs}
    for limit in entry.knobs:
        if limit.name in held:
            problems.append(f"{where} bound {limit.name}, which the standing limits fix at "
                            f"{held[limit.name]}: a project narrows and never widens")
            continue
        own = bounds.get(limit.name)
        low, high = limit.min, limit.max
        if own is not None and own.min is not None:
            if low is None:
                low = own.min
            elif low < own.min:
                problems.append(f"{where} put {limit.name}'s minimum at {low}, below the "
                                f"standing {own.min}: a project narrows and never widens")
        if own is not None and own.max is not None:
            if high is None:
                high = own.max
            elif high > own.max:
                problems.append(f"{where} put {limit.name}'s maximum at {high}, above the "
                                f"standing {own.max}: a project narrows and never widens")
        bounds[limit.name] = KnobLimit(limit.name, low, high)
    fixed = dict(base.fixed)
    for name, value in entry.fixed:
        if name in held:
            if value != held[name]:
                problems.append(f"{where} fix {name} at {value} and the standing limits "
                                f"fix it at {held[name]}")
            continue
        own = next((limit for limit in base.knobs if limit.name == name), None)
        if own is not None and not own.holds(value):
            problems.append(f"{where} fix {name} at {value}, outside the standing "
                            f"{own.text()}")
        bounds.pop(name, None)
        fixed[name] = value
    return dataclasses.replace(
        entry, hash=max(base.hash, entry.hash, key=len), name=entry.name or base.name,
        knobs=tuple(bounds.values()), fixed=tuple(fixed.items()),
        cold_start=entry.cold_start or base.cold_start), problems


def combine(standing: Limits, project: Limits) -> Limits:
    """The standing limits with a project's combined in; `LimitsError` with every way
    the project's would widen them.

    A project's entry for a template the standing limits list narrows that entry: a
    bound inside the standing one, a side it leaves open kept from the standing, a
    standing fixed value kept, a standing range fixed at a value inside it. An entry
    for any other template admits it. Either way the standing caps hold, and a
    template's cold-start mode may be stricter than the standing one and never looser.
    """
    problems: list[str] = []
    merged = list(standing.templates)
    where = "the project's limits"
    for entry in project.templates:
        shared = [base for base in standing.templates if _overlap(base.hash, entry.hash)]
        clash = [base for base in standing.templates
                 if base.key == entry.key and base not in shared]
        if clash:
            problems.append(f"{where} name an entry {entry.key!r} for hash {entry.hash} and "
                            f"the standing limits' {entry.key!r} is hash {clash[0].hash}: "
                            "give the project's entry a name of its own")
        if len(shared) > 1:
            problems.append(f"{where} for template {entry.key!r} name hash {entry.hash}, "
                            "which begins more than one standing entry's")
            continue
        floor = (shared[0].cold_start or standing.cold_start) if shared \
            else standing.cold_start
        if entry.cold_start and _MODE_ORDER[entry.cold_start] < _MODE_ORDER[floor]:
            problems.append(f"{where} for template {entry.key!r} set cold_start "
                            f"{entry.cold_start!r}, looser than the standing {floor!r}")
        if shared:
            narrowed, found = _narrow(shared[0], entry)
            problems += found
            merged[merged.index(shared[0])] = narrowed
        else:
            narrowed = entry
            merged.append(entry)
        problems += [f"the project's {line}" for line in _beyond_caps(narrowed, standing.caps)]
    if problems:
        raise LimitsError(problems)
    return dataclasses.replace(standing, templates=tuple(merged), project=project.path,
                               project_description=project.description)


def loads(text: str, path: str = "", *, project: bool = False) -> Limits:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LimitsError([f"invalid TOML: {exc}"]) from exc
    return from_dict(data, path, project=project)


def load(path: str, *, project: bool = False) -> Limits:
    with open(path, encoding="utf-8") as handle:
        return loads(handle.read(), os.path.abspath(path), project=project)


def default_path(instrument_path: str) -> str:
    """Where the limits sit for an instrument document: beside it, as `limits.toml`."""
    if not instrument_path:
        return ""
    return os.path.join(os.path.dirname(os.path.abspath(instrument_path)), LIMITS_NAME)


# -- the check ----------------------------------------------------------------------


def check(method: Method | None, rendered: Rendered | None, limits: Limits | None, *,
          real: bool) -> list[str]:
    """Why `method` may not be sent under `limits`; empty if it may.

    `rendered` is the render the method came from, or None for a hand-written method;
    `method` None asks only whether anything may be sent at all, which is whether there
    are limits. `real` is whether the boxes are the instrument's. With no limits a real
    owner refuses everything and a rehearsal nothing. One sentence per reason.
    """
    if limits is None:
        return [NO_LIMITS] if real else []
    problems: list[str] = []
    if method is not None:
        if rendered is None:
            if real and not limits.hand_written:
                problems.append(HAND_WRITTEN)
        else:
            problems += _template_problems(rendered, limits)
        outside = [box.name for box in method.boxes if box.name not in limits.boxes]
        if outside:
            problems.append(
                f"the standing limits do not allow {', '.join(outside)}: an agent may "
                f"address {', '.join(limits.boxes)}")
        if limits.electrodes is not None:
            problems += _electrodes.refusals(method, limits.electrodes)
        if limits.rf_drive is not None:
            problems += _electrodes.rf_refusals(method, limits.rf_drive)
    return problems


def _template_problems(rendered: Rendered, limits: Limits) -> list[str]:
    entry = limits.entry_for(rendered.template_hash)
    template = rendered.template
    label = template.name if template is not None and template.name else "this template"
    if entry is None:
        files = [path for path in (limits.path, limits.project) if path]
        where = f" ({', '.join(files)})" if files else ""
        return [f"{label} (hash {rendered.template_hash[:12]}) is not in the limits in "
                f"force{where}, so an agent may not run it: a [templates] entry with this "
                "hash, in the standing limits or a project's, admits it, and that is added "
                "by whoever keeps those limits, never by the session it was refused to"]
    if template is not None:
        invalid = entry.problems_against(template)
        if invalid:
            return [f"{entry.whose} for {label} cannot be applied: " + "; ".join(invalid)]
    units = {knob.name: knob.unit for knob in template.knobs} if template is not None else {}
    problems: list[str] = []
    outside: set[str] = set()
    for limit in entry.knobs:
        value = rendered.knobs.get(limit.name)
        if value is None or limit.holds(value):
            continue
        unit = units.get(limit.name, "")
        outside.add(limit.name)
        problems.append(
            f"{limit.name} = {value}{' ' + unit if unit else ''} is outside {entry.whose} "
            f"for {label}, {limit.text(unit)}")
    for name, value in entry.fixed:
        asked = rendered.knobs.get(name)
        if asked is not None and asked != value:
            unit = units.get(name, "")
            outside.add(name)
            problems.append(
                f"{name} is fixed at {value}{' ' + unit if unit else ''} by {entry.whose} "
                f"for {label} and was asked for {asked}")
    for cap in limits.caps:
        value = rendered.knobs.get(cap.name)
        if value is None or cap.name in outside or cap.holds(value):
            continue
        unit = units.get(cap.name, "")
        problems.append(
            f"{cap.name} = {value}{' ' + unit if unit else ''} is outside the standing cap "
            f"on {cap.name}, {cap.text(unit)}, which holds for every template")
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
