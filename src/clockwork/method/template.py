"""Method templates: a method with holes in its strings, and the knobs that fill them.

A template is a schema-2 method document whose command strings carry `{name}` holes,
plus the few tables that say what fills them: **knobs** a trainee turns, each with a
declared range; **labels**, which name the data and render nothing; **constants**;
**derivations** in a small declared vocabulary; and **marks**, the derived moments a file
records. `render` checks the knob values against their ranges, evaluates the
derivations, fills the holes and returns a plain `Method` -- the one that is sent and
stamped, exactly as a hand-written method is. Nothing downstream of `Method` learns
anything new (lab record, task 65). The format is `docs/template-file-format.md`.

No Qt here, so a script, the campaign surface and the window render the same strings
from the same numbers.

**The vocabulary is names, numbers, `+ - * /`, parentheses and `round`, and nothing
else.** A template is a document trainees edit, and a derivation they cannot read is one
nobody will fix; the evaluator is a parser over exactly that grammar and never `eval`.
Whole numbers stay whole through `+ - *` and `round`, and `/` always gives a fraction,
so a tick computed without `round` is caught where it lands rather than truncated.

**How a hole is written out is the wire format's decision, not the template's.** Every
number is written in its shortest form at four decimals at most -- `208`, not
`208.0000` -- which is what the trainees write by hand. Where the wire format counts in
whole units, a hole must come out whole: a time point's count, a loop's cycle count in
an `STBLDAT` table (wire format §2), and a compression table's loop count (§6.6). A count
must also not be negative, because a negative count is a *dynamic* time point (§2), and
nothing in a compression table may be, because the mini-language's numbers must start
with a digit (§5, which §6.6 extends).
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
import tomllib
from dataclasses import dataclass, field

from . import SCHEMA_VERSION, Method, MethodError
from . import from_dict as _method_from_dict

TEMPLATE_SCHEMA = 1
"""The template document shape this module reads; `template_schema` must equal it."""

TICK_NAME = "tick_us"
"""The name a template gives the pusher period it assumes, in microseconds.

A declared number the instrument can contradict: a sequencer counts pusher pulses while
an ARB box counts its own milliseconds, so if the pusher period moves, one box's events
move in time and the other's do not. `Rendered.tick_us` carries it so a file can record
the period a render assumed beside the one the digitizer measured (lab record, task 66).
A template that declares marks must declare it, since a mark's expected scan is its time
over this period.
"""

DECIMALS = 4
"""The most decimal places a hole is written with.

A tenth of a microsecond on a millisecond wait and a tenth of a millivolt on a voltage,
finer than anything the boxes resolve, and the precision of the waits the trainees
write by hand (`D16.7628`).
"""

FUNCTIONS = ("round",)

# A body key is one the rendered method document carries; `schema_version` is not among
# them because `renders` states it, and a template carrying both could disagree with
# itself.
BODY_KEYS = ("start", "reset", "metadata", "acquisition", "boxes")
TEMPLATE_KEYS = ("template_schema", "renders", "knobs", "labels", "constants", "derive",
                 "marks")
PHASES = ("setup", "load", "arm")

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_HOLE = re.compile(r"\{([^{}]*)\}")
_TOKEN = re.compile(
    r"\s*(?:(?P<number>\d+(?:\.\d+)?)|(?P<name>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>[-+*/()]))"
)


class TemplateError(ValueError):
    """A template failed to parse, validate or render.

    Carries every problem found, not just the first, in `.problems`, as `MethodError`
    does. A problem with the method a template renders to is reported here too, prefixed
    `rendered method:`, so that a caller catches one exception.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


Number = int | float


@dataclass(frozen=True, slots=True)
class Knob:
    """One number a trainee turns: a default and the range it may be turned through.

    An **integer knob** is one whose default, minimum and maximum are all written as
    whole numbers; it takes whole values only, and stays whole in arithmetic.
    """

    name: str
    default: Number
    min: Number
    max: Number
    unit: str
    description: str = ""

    @property
    def integer(self) -> bool:
        return all(isinstance(value, int) for value in (self.default, self.min, self.max))


@dataclass(frozen=True, slots=True)
class Label:
    """A name for the data, such as the sample: stamped and reported, rendering nothing."""

    name: str
    required: bool = False
    description: str = ""


@dataclass(frozen=True, slots=True)
class Mark:
    """A derived moment a rendered run records, in milliseconds and as an expected scan.

    `scan` is `round(ms * 1000 / tick_us)`: scans counted from tick 0 of one ion mobility
    experiment, on the convention that a sequencer event at tick *n* falls in record
    *n*. Whether the event governs record *n* or *n*+1 is not settled on this
    instrument, so a mark's description should say which it assumed.
    """

    name: str
    ms: float
    scan: int
    description: str = ""


@dataclass(frozen=True, slots=True)
class _Derivation:
    name: str
    expression: str
    tree: tuple = field(compare=False)


@dataclass(frozen=True, slots=True)
class _MarkSpec:
    name: str
    expression: str
    description: str
    tree: tuple = field(compare=False)


@dataclass(frozen=True)
class Template:
    """A loaded template. `text` is the document, line endings normalized to `\\n`."""

    text: str
    knobs: tuple[Knob, ...]
    labels: tuple[Label, ...]
    constants: tuple[tuple[str, Number], ...]
    derivations: tuple[_Derivation, ...]
    """In evaluation order: each after every name it uses."""

    marks: tuple[_MarkSpec, ...]
    body: dict = field(compare=False, hash=False, repr=False)

    @property
    def hash(self) -> str:
        """SHA-256 of `text`: what a rendered run stamps as the template it came from."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def name(self) -> str:
        """The `metadata.name` the template's renders carry."""
        return str(self.body.get("metadata", {}).get("name", ""))

    def knob(self, name: str) -> Knob:
        """The knob named `name`. `KeyError` if the template has no such knob."""
        for entry in self.knobs:
            if entry.name == name:
                return entry
        raise KeyError(name)

    def defaults(self) -> dict[str, Number]:
        """Every knob at its default, which is the setting a template anchors on."""
        return {entry.name: entry.default for entry in self.knobs}


@dataclass(frozen=True)
class Rendered:
    """What `render` returns: the method, and everything a run records about its making.

    `knobs` holds every knob, turned or left at its default; `derived` every
    derivation's value; `labels` only the labels the caller gave.
    """

    method: Method
    knobs: dict[str, Number]
    labels: dict[str, str]
    marks: tuple[Mark, ...]
    template_hash: str
    template_text: str
    tick_us: float | None
    derived: dict[str, Number] = field(default_factory=dict)


# --- the derivation vocabulary -----------------------------------------------------------


class _ExpressionError(ValueError):
    pass


_VOCABULARY = "names, numbers, + - * /, parentheses and round()"


def _tokens(text: str) -> list[tuple[str, str, int]]:
    tokens: list[tuple[str, str, int]] = []
    position = 0
    while text[position:].strip():
        match = _TOKEN.match(text, position)
        if match is None:
            while text[position].isspace():
                position += 1
            raise _ExpressionError(
                f"{text[position]!r} at column {position + 1} is not part of the vocabulary "
                f"({_VOCABULARY})"
            )
        kind = match.lastgroup or ""
        tokens.append((kind, match.group(kind), match.start(kind) + 1))
        position = match.end()
    return tokens


class _Parser:
    """Recursive descent over the vocabulary, usual precedence, left to right.

        expression = term { ("+" | "-") term }
        term       = unary { ("*" | "/") unary }
        unary      = "-" unary | primary
        primary    = number | name | "round" "(" expression ")" | "(" expression ")"
    """

    def __init__(self, text: str) -> None:
        self.tokens = _tokens(text)
        self.index = 0

    def parse(self) -> tuple:
        if not self.tokens:
            raise _ExpressionError("is empty")
        tree = self._expression()
        if self.index < len(self.tokens):
            _, value, column = self.tokens[self.index]
            raise _ExpressionError(f"unexpected {value!r} at column {column}")
        return tree

    def _peek(self) -> str | None:
        return self.tokens[self.index][1] if self.index < len(self.tokens) else None

    def _take(self) -> tuple[str, str, int]:
        if self.index >= len(self.tokens):
            raise _ExpressionError("ends where a number, a name or '(' was expected")
        token = self.tokens[self.index]
        self.index += 1
        return token

    def _expect(self, value: str) -> None:
        if self._peek() != value:
            found = self._peek()
            where = (f"found {found!r} at column {self.tokens[self.index][2]}"
                     if found is not None else "the expression ends")
            raise _ExpressionError(f"expected {value!r}, but {where}")
        self.index += 1

    def _expression(self) -> tuple:
        tree = self._term()
        while self._peek() in ("+", "-"):
            op = self._take()[1]
            tree = ("op", op, tree, self._term())
        return tree

    def _term(self) -> tuple:
        tree = self._unary()
        while self._peek() in ("*", "/"):
            op = self._take()[1]
            tree = ("op", op, tree, self._unary())
        return tree

    def _unary(self) -> tuple:
        if self._peek() == "-":
            self._take()
            return ("neg", self._unary())
        return self._primary()

    def _primary(self) -> tuple:
        kind, value, column = self._take()
        if kind == "number":
            return ("number", float(value) if "." in value else int(value))
        if kind == "name":
            if self._peek() == "(":
                if value not in FUNCTIONS:
                    raise _ExpressionError(
                        f"{value}() at column {column} is not a function; round() is the "
                        "only one"
                    )
                self._take()
                inner = self._expression()
                self._expect(")")
                return ("round", inner)
            if value in FUNCTIONS:
                raise _ExpressionError(f"round at column {column} needs an argument, round(x)")
            return ("name", value)
        if value == "(":
            inner = self._expression()
            self._expect(")")
            return inner
        raise _ExpressionError(f"unexpected {value!r} at column {column}")


def _names(tree: tuple) -> set[str]:
    kind = tree[0]
    if kind == "name":
        return {tree[1]}
    if kind == "number":
        return set()
    if kind in ("neg", "round"):
        return _names(tree[1])
    return _names(tree[2]) | _names(tree[3])


def round_half_away(value: Number) -> int:
    """`round` in a derivation: to the nearest whole number, halves away from zero.

    Not Python's `round`, which sends halves to the even neighbour, so that
    `round(2.5)` is 3 as a trainee reading the derivation expects.
    """
    if isinstance(value, int):
        return value
    whole = math.floor(abs(value) + 0.5)
    return whole if value >= 0 else -whole


def _evaluate(tree: tuple, values: dict[str, Number]) -> Number:
    kind = tree[0]
    if kind == "number":
        return tree[1]
    if kind == "name":
        return values[tree[1]]
    if kind == "neg":
        return -_evaluate(tree[1], values)
    if kind == "round":
        return round_half_away(_evaluate(tree[1], values))
    op, left, right = tree[1], _evaluate(tree[2], values), _evaluate(tree[3], values)
    if op == "+":
        return left + right
    if op == "-":
        return left - right
    if op == "*":
        return left * right
    if right == 0:
        raise _ExpressionError("divides by zero")
    return left / right


# --- writing a number into a string -----------------------------------------------------


def format_number(value: Number) -> str:
    """A hole's number as it goes on the wire: shortest form, `DECIMALS` places at most.

    `208.00000000000003` and `208.0` are both `208`, `37.5` stays `37.5`, and a negative
    zero is `0`.
    """
    if isinstance(value, int):
        return str(value)
    text = f"{value:.{DECIMALS}f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _command_word(text: str) -> str:
    match = re.match(r"\s*([A-Za-z]+)", text)
    return match.group(1).upper() if match else ""


def _hole_rule(text: str, start: int) -> tuple[str, str] | None:
    """What the wire format requires of a number written at `text[start]`.

    Returns `(requirement, what)`, where the requirement is `"count"` (whole and not
    negative), `"loop"` (whole and at least 1) or `"unsigned"` (not negative), or None
    where the wire format asks nothing of the number beyond being one. `what` names the
    position for the problem sentence.
    """
    word = _command_word(text)
    before = _HOLE.sub("0", text[:start])
    if word == "STBLDAT":
        if ";" not in before:
            return None
        sequence = before[before.index(";") + 1:]
        cut = max(sequence.rfind(mark) for mark in ";,[]")
        delimiter = sequence[cut] if cut >= 0 else ";"
        colons = sequence[cut + 1:].count(":")
        if delimiter == "[":
            return ("count", "a table's cycle count") if colons == 1 else None
        if delimiter in ";," and colons == 0:
            return ("count", "a time point's count, in ticks")
        return None
    if word in ("SARBCTBL", "STWCTBL"):
        if before.endswith("]"):
            return ("loop", "a compression table's loop count")
        return ("unsigned", "a compression table's number")
    return None


def _fill(
    text: str, values: dict[str, Number], path: str, problems: list[str]
) -> str | None:
    """`text` with every hole replaced by its value, or None where one is refused."""
    out: list[str] = []
    ok = True
    last = 0
    for match in _HOLE.finditer(text):
        out.append(text[last:match.start()])
        last = match.end()
        name = match.group(1)
        if name not in values:
            # Unknown names are reported once, at load; nothing further to say here.
            ok = False
            continue
        value = values[name]
        written = format_number(value)
        rule = _hole_rule(text, match.start())
        if rule is not None:
            requirement, what = rule
            if requirement in ("count", "loop") and "." in written:
                problems.append(
                    f"{path}: {{{name}}} is {what} and must be a whole number, but comes "
                    f"to {written}; round() its derivation"
                )
                ok = False
            elif written.startswith("-"):
                why = ("a negative count makes the time point dynamic (wire format §2)"
                       if requirement == "count" else
                       "the mini-language's numbers start with a digit (wire format §5)")
                problems.append(
                    f"{path}: {{{name}}} is {what} and comes to {written}; {why}"
                )
                ok = False
            elif requirement == "loop" and written == "0":
                problems.append(
                    f"{path}: {{{name}}} is {what} and comes to 0, which never terminates "
                    "(wire format §6.6)"
                )
                ok = False
        out.append(written)
    out.append(text[last:])
    return "".join(out) if ok else None


# --- loading ------------------------------------------------------------------------------


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_name(name: str, where: str, seen: dict[str, str], problems: list[str]) -> bool:
    if not _NAME.match(name):
        problems.append(
            f"{where}: {name!r} is not a name; a name is letters, digits and underscores, "
            "not starting with a digit"
        )
        return False
    if name in FUNCTIONS:
        problems.append(f"{where}: {name!r} is the name of a function and cannot be reused")
        return False
    if name in seen:
        problems.append(f"{where}: {name!r} is already a {seen[name]}; every name is used once")
        return False
    return True


def _unknown_keys(table: dict, where: str, known: tuple[str, ...], problems: list[str]) -> None:
    for key in table:
        if key not in known:
            problems.append(f"{where}.{key}: not a key of template schema {TEMPLATE_SCHEMA}")


def _knobs(raw: object, seen: dict[str, str], problems: list[str]) -> list[Knob]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        problems.append("knobs: expected a table of name = { default, min, max, unit }")
        return []
    knobs: list[Knob] = []
    for name, spec in raw.items():
        where = f"knobs.{name}"
        if not _check_name(name, where, seen, problems):
            continue
        seen[name] = "knob"
        if not isinstance(spec, dict):
            problems.append(f"{where}: expected a table, as in {{ default = 1.0, min = 0.0, "
                            'max = 2.0, unit = "ms" }')
            continue
        _unknown_keys(spec, where, ("default", "min", "max", "unit", "description"), problems)
        numbers = {}
        for key in ("default", "min", "max"):
            value = spec.get(key)
            if not _is_number(value):
                problems.append(f"{where}.{key}: expected a number")
            else:
                numbers[key] = value
        unit = spec.get("unit")
        if not isinstance(unit, str):
            problems.append(f"{where}.unit: expected a string, empty for a pure number")
        description = spec.get("description", "")
        if not isinstance(description, str):
            problems.append(f"{where}.description: expected a string")
        if len(numbers) < 3 or not isinstance(unit, str) or not isinstance(description, str):
            continue
        if not numbers["min"] <= numbers["default"] <= numbers["max"]:
            problems.append(
                f"{where}: the default {numbers['default']} is not within min "
                f"{numbers['min']} to max {numbers['max']}"
            )
            continue
        knobs.append(Knob(name=name, default=numbers["default"], min=numbers["min"],
                          max=numbers["max"], unit=unit, description=description))
    return knobs


def _labels(raw: object, seen: dict[str, str], problems: list[str]) -> list[Label]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        problems.append("labels: expected a table of name = { required = true }")
        return []
    labels: list[Label] = []
    for name, spec in raw.items():
        where = f"labels.{name}"
        if not _check_name(name, where, seen, problems):
            continue
        seen[name] = "label"
        if not isinstance(spec, dict):
            problems.append(f"{where}: expected a table, as in {{ required = true }}")
            continue
        _unknown_keys(spec, where, ("required", "description"), problems)
        required = spec.get("required", False)
        description = spec.get("description", "")
        if not isinstance(required, bool):
            problems.append(f"{where}.required: expected true or false")
        elif not isinstance(description, str):
            problems.append(f"{where}.description: expected a string")
        else:
            labels.append(Label(name=name, required=required, description=description))
    return labels


def _constants(raw: object, seen: dict[str, str], problems: list[str]) -> list[tuple[str, Number]]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        problems.append("constants: expected a table of name = number")
        return []
    constants: list[tuple[str, Number]] = []
    for name, value in raw.items():
        where = f"constants.{name}"
        if not _check_name(name, where, seen, problems):
            continue
        seen[name] = "constant"
        if not _is_number(value):
            problems.append(f"{where}: expected a number")
            continue
        constants.append((name, value))
    return constants


def _parse(expression: object, where: str, problems: list[str]) -> tuple | None:
    if not isinstance(expression, str):
        problems.append(f"{where}: expected an expression written as a string")
        return None
    try:
        return _Parser(expression).parse()
    except _ExpressionError as exc:
        problems.append(f"{where}: {expression!r} {exc}")
        return None


def _undefined(names: set[str], where: str, seen: dict[str, str], problems: list[str]) -> bool:
    ok = True
    for name in sorted(names):
        if seen.get(name) == "label":
            problems.append(f"{where}: {name!r} is a label, and labels render nothing")
            ok = False
        elif name not in seen:
            problems.append(f"{where}: no knob, constant or derivation is named {name!r}")
            ok = False
    return ok


def _derivations(
    raw: object, seen: dict[str, str], problems: list[str]
) -> list[_Derivation]:
    """The `[derive]` table, parsed and put in dependency order.

    A derivation may use any knob, constant or other derivation, written before or after
    it; the order is worked out here, by a topological sort, and a cycle is a problem
    naming every derivation on it.
    """
    if raw is None:
        return []
    if not isinstance(raw, dict):
        problems.append('derive: expected a table of name = "expression"')
        return []
    parsed: dict[str, _Derivation] = {}
    for name, expression in raw.items():
        where = f"derive.{name}"
        if not _check_name(name, where, seen, problems):
            continue
        seen[name] = "derivation"
        tree = _parse(expression, where, problems)
        if tree is not None:
            parsed[name] = _Derivation(name=name, expression=expression, tree=tree)
    usable = {name: entry for name, entry in parsed.items()
              if _undefined(_names(entry.tree), f"derive.{name}", seen, problems)}
    ordered: list[_Derivation] = []
    state: dict[str, str] = {}

    def visit(name: str, trail: list[str]) -> bool:
        if state.get(name) == "done":
            return True
        if state.get(name) == "visiting":
            loop = trail[trail.index(name):] + [name]
            problems.append("derive: " + " -> ".join(loop) + " uses itself")
            return False
        state[name] = "visiting"
        entry = usable[name]
        ok = all(visit(dep, trail + [name]) for dep in sorted(_names(entry.tree))
                 if dep in usable)
        state[name] = "done"
        if ok:
            ordered.append(entry)
        return ok

    for name in usable:
        visit(name, [])
    return ordered


def _marks(raw: object, seen: dict[str, str], problems: list[str]) -> list[_MarkSpec]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        problems.append('marks: expected a table of name = { ms = "expression" }')
        return []
    marks: list[_MarkSpec] = []
    for name, spec in raw.items():
        where = f"marks.{name}"
        if not _NAME.match(name):
            problems.append(f"{where}: {name!r} is not a name")
            continue
        if not isinstance(spec, dict):
            problems.append(f'{where}: expected a table, as in {{ ms = "release_ms" }}')
            continue
        _unknown_keys(spec, where, ("ms", "description"), problems)
        tree = _parse(spec.get("ms"), f"{where}.ms", problems)
        description = spec.get("description", "")
        if not isinstance(description, str):
            problems.append(f"{where}.description: expected a string")
            continue
        if tree is None or not _undefined(_names(tree), f"{where}.ms", seen, problems):
            continue
        marks.append(_MarkSpec(name=name, expression=spec["ms"], description=description,
                               tree=tree))
    return marks


def _strings(body: dict):
    """Every command string in a body, with its document path: `(container, key, path)`."""
    boxes = body.get("boxes")
    if isinstance(boxes, list):
        for i, box in enumerate(boxes):
            if not isinstance(box, dict):
                continue
            for phase in PHASES:
                strings = box.get(phase)
                if isinstance(strings, list):
                    for j, value in enumerate(strings):
                        if isinstance(value, str):
                            yield strings, j, f"boxes[{i}].{phase}[{j}]"
    for key in ("start", "reset"):
        steps = body.get(key)
        if isinstance(steps, list):
            for i, step in enumerate(steps):
                if isinstance(step, list) and len(step) == 2 and isinstance(step[1], str):
                    yield step, 1, f"{key}[{i}][1]"


def _other_strings(value: object, path: str):
    """Every string in the body that is not a command string, for the misplaced-hole check."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, inner in value.items():
            yield from _other_strings(inner, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for i, inner in enumerate(value):
            yield from _other_strings(inner, f"{path}[{i}]")


def _check_holes(body: dict, seen: dict[str, str], problems: list[str]) -> None:
    command_paths = set()
    for container, key, path in _strings(body):
        command_paths.add(path)
        text = container[key]
        stray = _HOLE.sub("", text)
        if "{" in stray or "}" in stray:
            problems.append(
                f"{path}: a brace that is not part of a hole, in {text!r}; a hole is a bare "
                "name in braces, as in {duration_ms}"
            )
        for match in _HOLE.finditer(text):
            name = match.group(1)
            if not _NAME.match(name):
                problems.append(
                    f"{path}: {{{name}}} is not a hole; a hole is a bare name in braces, "
                    "with no spaces or arithmetic (put the arithmetic in [derive])"
                )
            else:
                _undefined({name}, path, seen, problems)
    for path, text in _other_strings(body, ""):
        if path not in command_paths and _HOLE.search(text):
            problems.append(
                f"{path}: holes are filled only in the boxes' setup, load and arm strings and "
                "the start and reset commands"
            )


def from_dict(data: dict, text: str = "") -> Template:
    """Build and validate a `Template` from a parsed TOML document.

    Collects every problem before raising `TemplateError`. A template that loads also
    **renders at its defaults**: the trial render runs here, labels aside, so a template
    whose own defaults make an invalid method is refused when it is opened rather than
    when a trainee first turns a knob.
    """
    problems: list[str] = []
    schema = data.get("template_schema")
    if schema != TEMPLATE_SCHEMA:
        problems.append(f"template_schema: expected {TEMPLATE_SCHEMA}, got {schema!r}")
    renders = data.get("renders")
    if renders != SCHEMA_VERSION:
        problems.append(
            f"renders: expected {SCHEMA_VERSION}, the method schema this clockwork writes, "
            f"got {renders!r}"
        )
    for key in data:
        if key == "schema_version":
            problems.append("schema_version: a template states the method schema it renders "
                            "to in `renders`, not here")
        elif key not in TEMPLATE_KEYS + BODY_KEYS:
            problems.append(f"{key}: not a key of template schema {TEMPLATE_SCHEMA}")

    seen: dict[str, str] = {}
    knobs = _knobs(data.get("knobs"), seen, problems)
    labels = _labels(data.get("labels"), seen, problems)
    constants = _constants(data.get("constants"), seen, problems)
    derivations = _derivations(data.get("derive"), seen, problems)
    marks = _marks(data.get("marks"), seen, problems)
    if marks and seen.get(TICK_NAME) in (None, "label"):
        problems.append(
            f"marks: a mark's expected scan is its time over the pusher period, so a template "
            f"with marks declares {TICK_NAME}, the period it assumes in microseconds"
        )
    body = {key: copy.deepcopy(data[key]) for key in BODY_KEYS if key in data}
    _check_holes(body, seen, problems)
    if problems:
        raise TemplateError(problems)

    template = Template(
        text=text,
        knobs=tuple(knobs),
        labels=tuple(labels),
        constants=tuple(constants),
        derivations=tuple(derivations),
        marks=tuple(marks),
        body=body,
    )
    _render(template, template.defaults(), {}, check_labels=False)
    return template


def loads_template(text: str) -> Template:
    """Parse and validate a template from TOML text."""
    text = text.replace("\r\n", "\n")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TemplateError([f"invalid TOML: {exc}"]) from exc
    return from_dict(data, text)


def load_template(path: str) -> Template:
    """Parse and validate a template from a TOML file.

    The text is read as UTF-8 and its line endings normalized, so that one template
    saved by two editors hashes alike.
    """
    with open(path, "rb") as handle:
        return loads_template(handle.read().decode("utf-8"))


def is_template(data: dict) -> bool:
    """True where a parsed TOML document is a template rather than a method."""
    return "template_schema" in data


# --- rendering ----------------------------------------------------------------------------


def _knob_values(
    template: Template, knobs: dict[str, object], problems: list[str]
) -> dict[str, Number]:
    """The caller's knob values over the defaults, each checked against its range.

    The range check is the one refusal a template adds to a method's own. A knob's
    `min` and `max` are the envelope a template declares for itself, inclusive at both
    ends.
    """
    known = {entry.name for entry in template.knobs}
    for name in knobs:
        if name not in known:
            problems.append(f"knob {name!r}: this template has no such knob"
                            + (f" (it has {', '.join(sorted(known))})" if known else ""))
    values: dict[str, Number] = {}
    for entry in template.knobs:
        value = knobs.get(entry.name, entry.default)
        unit = f" {entry.unit}" if entry.unit else ""
        if not _is_number(value):
            problems.append(f"knob {entry.name!r}: expected a number, got {value!r}")
            continue
        if entry.integer:
            if isinstance(value, float) and not value.is_integer():
                problems.append(f"knob {entry.name!r}: takes whole numbers, got {value}")
                continue
            value = int(value)
        else:
            value = float(value)
        if not entry.min <= value <= entry.max:
            problems.append(
                f"knob {entry.name!r} = {format_number(value)}{unit} is outside the range this "
                f"template declares, {format_number(entry.min)} to "
                f"{format_number(entry.max)}{unit}"
            )
            continue
        values[entry.name] = value
    return values


def _label_values(
    template: Template, labels: dict[str, object], check: bool, problems: list[str]
) -> dict[str, str]:
    known = {entry.name for entry in template.labels}
    for name in labels:
        if name not in known:
            problems.append(f"label {name!r}: this template has no such label")
    values: dict[str, str] = {}
    for entry in template.labels:
        value = labels.get(entry.name)
        if value is None:
            if check and entry.required:
                problems.append(f"label {entry.name!r}: required, and not given")
            continue
        if not isinstance(value, str):
            problems.append(f"label {entry.name!r}: expected text, got {value!r}")
            continue
        if check and entry.required and not value.strip():
            problems.append(f"label {entry.name!r}: required, and empty")
            continue
        values[entry.name] = value
    return values


def _render(
    template: Template, knobs: dict[str, object], labels: dict[str, object], *,
    check_labels: bool,
) -> Rendered:
    problems: list[str] = []
    knob_values = _knob_values(template, knobs, problems)
    label_values = _label_values(template, labels, check_labels, problems)
    if problems:
        raise TemplateError(problems)

    values: dict[str, Number] = dict(template.constants) | knob_values
    derived: dict[str, Number] = {}
    for entry in template.derivations:
        try:
            value = _evaluate(entry.tree, values)
        except _ExpressionError as exc:
            problems.append(f"derive.{entry.name}: {entry.expression!r} {exc}")
            continue
        except KeyError:
            continue  # a name whose own derivation failed, already reported
        values[entry.name] = derived[entry.name] = value
    if problems:
        raise TemplateError(problems)

    body = copy.deepcopy(template.body)
    for container, key, path in list(_strings(body)):
        filled = _fill(container[key], values, path, problems)
        if filled is not None:
            container[key] = filled
    if problems:
        raise TemplateError(problems)

    try:
        method = _method_from_dict({"schema_version": SCHEMA_VERSION, **body})
    except MethodError as exc:
        raise TemplateError([f"rendered method: {problem}" for problem in exc.problems]) from exc

    tick_us = values.get(TICK_NAME)
    if tick_us is not None and tick_us <= 0:
        raise TemplateError([f"{TICK_NAME}: the pusher period must be positive, got {tick_us}"])
    marks: list[Mark] = []
    for spec in template.marks:
        try:
            ms = float(_evaluate(spec.tree, values))
        except _ExpressionError as exc:
            problems.append(f"marks.{spec.name}.ms: {spec.expression!r} {exc}")
            continue
        assert tick_us is not None  # enforced at load
        marks.append(Mark(name=spec.name, ms=ms,
                          scan=round_half_away(ms * 1000 / tick_us),
                          description=spec.description))
    if problems:
        raise TemplateError(problems)

    return Rendered(
        method=method,
        knobs=knob_values,
        labels=label_values,
        marks=tuple(marks),
        template_hash=template.hash,
        template_text=template.text,
        tick_us=None if tick_us is None else float(tick_us),
        derived=derived,
    )


def render(
    template: Template,
    knobs: dict[str, object] | None = None,
    labels: dict[str, object] | None = None,
) -> Rendered:
    """Render `template` at `knobs` (the rest at their defaults) with `labels`.

    Raises `TemplateError` for a knob outside its declared range, an unknown knob or
    label, a required label left out, a derivation that divides by zero, a hole whose
    value the wire format cannot take where it lands, and any problem the rendered
    document has as a method. The returned method is what is sent and stamped.
    """
    return _render(template, dict(knobs or {}), dict(labels or {}), check_labels=True)


__all__ = [
    "DECIMALS",
    "TEMPLATE_SCHEMA",
    "TICK_NAME",
    "Knob",
    "Label",
    "Mark",
    "Rendered",
    "Template",
    "TemplateError",
    "format_number",
    "from_dict",
    "is_template",
    "load_template",
    "loads_template",
    "render",
    "round_half_away",
]
