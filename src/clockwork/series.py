"""A series plan: load it, check it, expand it into points, and draw the order acquired.

A plan names a template, then either a `[grid]` of values per knob (the cross product) or
a list of `[[points]]` whose knobs move together, the replicates per point and the
references -- the template's defaults -- at the start, the end or both
(`docs/series-file-format.md`). `expand` turns it into `Point`s in planned order, each
numbered from 1 as its planned index; `order` gives the order they are acquired in, the
points between the references shuffled under a seed and the references left where they
are. Nothing here renders, sends or acquires: the `series` tool renders every point and
checks it against the limits, and the owner's `SeriesJob` acquires them (lab record,
task 91).

The seed is drawn as the window's randomised queue draws its own (`secrets`, lab record,
task 86), under `2**31` because the file stamps it as an Int32 (`clockwork.acq.Series`).

Qt-free, beside `clockwork.envelope`.
"""

from __future__ import annotations

import itertools
import os
import random
import secrets
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .method.template import Template, format_number

__all__ = ["PLAN_SCHEMA", "REFERENCES", "SEED_LIMIT", "Plan", "PlanError", "Point",
           "acquisitions", "describe", "draw_seed", "expand", "files", "from_dict", "load",
           "loads", "looks_inline", "order", "resolve"]

PLAN_SCHEMA = 1

REFERENCES = ("start", "end")

SEED_LIMIT = 2**31
"""Seeds are drawn from 0 up to this, which is what the file's Int32 holds."""

_KEYS = ("plan_schema", "description", "template", "labels", "replicates", "references",
         "grid", "points", "shuffle", "seed")

Number = int | float


class PlanError(ValueError):
    """A plan that does not load: every problem found, one sentence each."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Plan:
    """A loaded plan. `text` is the document as given, line endings normalized to LF."""

    template: str
    labels: dict[str, str] = field(default_factory=dict)
    replicates: int = 1
    references: tuple[str, ...] = ()
    grid: tuple[tuple[str, tuple[Number, ...]], ...] = ()
    """Each knob of a grid with its values, in the order the plan lists them."""
    points: tuple[dict[str, Number], ...] = ()
    """Each coupled point's knob values; empty for a grid."""
    shuffle: bool = True
    seed: int | None = None
    description: str = ""
    text: str = ""


@dataclass(frozen=True)
class Point:
    """One point of a plan: its planned index, the knobs it sets, and how many files."""

    index: int
    """Its place in the plan, from 1: the start reference, the grid or points, the end."""
    knobs: dict[str, Number]
    """The knobs the plan sets for it; the rest render at the template's defaults."""
    replicates: int = 1
    reference: str = ""
    """`start` or `end` for a reference, empty for any other point."""


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def looks_inline(plan: str) -> bool:
    """Whether `plan` is a plan's text rather than the path of one: a path holds no
    line break and no `=`, and every plan holds both."""
    return "\n" in plan or "=" in plan


def loads(text: str) -> Plan:
    """A plan from its text; `PlanError` with every problem found."""
    text = text.replace("\r\n", "\n")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PlanError([f"the plan is not valid TOML: {exc}"]) from exc
    return from_dict(data, text)


def load(path: str) -> Plan:
    with open(path, encoding="utf-8") as handle:
        return loads(handle.read())


def from_dict(data: Mapping[str, object], text: str = "") -> Plan:
    """A plan from its parsed document; `PlanError` with every problem found."""
    problems: list[str] = []
    for key in data:
        if key not in _KEYS:
            problems.append(f"{key} is not a key of a series plan ({', '.join(_KEYS)})")
    if data.get("plan_schema") != PLAN_SCHEMA:
        problems.append(f"plan_schema must be {PLAN_SCHEMA}, not {data.get('plan_schema')!r}")

    template = data.get("template")
    if not isinstance(template, str) or not template.strip():
        problems.append("template must name the template's path in the library")
        template = ""
    description = data.get("description", "")
    if not isinstance(description, str):
        problems.append("description must be text")
        description = ""

    labels: dict[str, str] = {}
    raw_labels = data.get("labels", {})
    if not isinstance(raw_labels, dict) or not all(
            isinstance(value, str) for value in raw_labels.values()):
        problems.append("labels must be a table of text, such as { sample = \"bradykinin\" }")
    else:
        labels = dict(raw_labels)

    replicates = data.get("replicates", 1)
    if not isinstance(replicates, int) or isinstance(replicates, bool) or replicates < 1:
        problems.append("replicates counts files per point, a whole number from 1")
        replicates = 1

    references: tuple[str, ...] = ()
    raw_references = data.get("references", [])
    if (not isinstance(raw_references, list)
            or not all(entry in REFERENCES for entry in raw_references)
            or len(set(raw_references)) != len(raw_references)):
        problems.append('references must be a list of "start", "end" or both, each once')
    else:
        references = tuple(entry for entry in REFERENCES if entry in raw_references)

    grid: list[tuple[str, tuple[Number, ...]]] = []
    points: list[dict[str, Number]] = []
    has_grid, has_points = "grid" in data, "points" in data
    if has_grid == has_points:
        problems.append("a plan has a [grid] or a list of [[points]], one of the two")
    if has_grid:
        raw_grid = data["grid"]
        if not isinstance(raw_grid, dict) or not raw_grid:
            problems.append("[grid] must be a table of knobs, each with a list of values")
        else:
            for knob, values in raw_grid.items():
                if (not isinstance(values, list) or not values
                        or not all(_number(value) for value in values)):
                    problems.append(f"grid.{knob} must be a list of numbers, at least one")
                elif len(set(values)) != len(values):
                    problems.append(f"grid.{knob} repeats a value")
                else:
                    grid.append((knob, tuple(values)))
    if has_points:
        raw_points = data["points"]
        if not isinstance(raw_points, list) or not raw_points:
            problems.append("[[points]] must be one table per point, at least one")
        else:
            for number, point in enumerate(raw_points, start=1):
                if not isinstance(point, dict) or not all(
                        _number(value) for value in point.values()):
                    problems.append(f"point {number} of [[points]] must be a table of knob "
                                    "values, each a number")
                else:
                    points.append(dict(point))

    shuffle = data.get("shuffle", True)
    if not isinstance(shuffle, bool):
        problems.append("shuffle must be true or false")
        shuffle = True
    seed = data.get("seed")
    if seed is not None:
        if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < SEED_LIMIT:
            problems.append(f"seed must be a whole number from 0 to {SEED_LIMIT - 1}")
            seed = None
        elif not shuffle:
            problems.append("a seed orders a shuffle, and this plan has shuffle = false")
    if problems:
        raise PlanError(problems)
    return Plan(template=template, labels=labels, replicates=replicates,
                references=references, grid=tuple(grid), points=tuple(points),
                shuffle=shuffle, seed=seed, description=description.strip(), text=text)


def expand(plan: Plan, template: Template) -> list[Point]:
    """Every point of `plan`, in planned order, indexed from 1.

    `PlanError` for a knob `template` does not have, naming every one. Ranges, labels
    and everything else a render checks are the render's to refuse."""
    known = {knob.name for knob in template.knobs}
    named = [name for name, _ in plan.grid] + [name for point in plan.points for name in point]
    unknown = sorted({name for name in named if name not in known})
    if unknown:
        raise PlanError([
            f"{template.name or 'the template'} has no knob called {name}; its knobs are "
            f"{', '.join(sorted(known)) or 'none'}" for name in unknown])
    if plan.grid:
        names = [name for name, _ in plan.grid]
        middle = [dict(zip(names, values, strict=True))
                  for values in itertools.product(*(values for _, values in plan.grid))]
    else:
        middle = [dict(point) for point in plan.points]
    made: list[Point] = []

    def add(knobs: dict[str, Number], reference: str = "") -> None:
        made.append(Point(index=len(made) + 1, knobs=knobs, replicates=plan.replicates,
                          reference=reference))

    if "start" in plan.references:
        add({}, "start")
    for knobs in middle:
        add(knobs)
    if "end" in plan.references:
        add({}, "end")
    return made


def draw_seed() -> int:
    """A fresh seed, under `SEED_LIMIT`."""
    return secrets.randbelow(SEED_LIMIT)


def order(points: Sequence[Point], seed: int | None) -> list[Point]:
    """The points in the order they are acquired.

    References stay first and last; the points between them are shuffled with
    `random.Random(seed)`, or left in planned order when `seed` is None."""
    first = [point for point in points if point.reference == "start"]
    last = [point for point in points if point.reference == "end"]
    middle = [point for point in points if not point.reference]
    if seed is not None:
        random.Random(seed).shuffle(middle)
    return first + middle + last


def acquisitions(points: Sequence[Point]) -> int:
    """What the series spends of the standing budget: one per point, as one `acquire`
    of its replicates would."""
    return len(points)


def files(points: Sequence[Point]) -> int:
    return sum(point.replicates for point in points)


def describe(point: Point) -> str:
    """`point 3 (amplitude_v = 45, duration_ms = 100)`, or `the start reference (point
    1, the defaults)`: how a refusal names a point."""
    if point.reference:
        return f"the {point.reference} reference (point {point.index}, the defaults)"
    values = ", ".join(f"{name} = {format_number(value)}" for name, value in point.knobs.items())
    return f"point {point.index} ({values or 'the defaults'})"


def resolve(plan: str) -> tuple[Plan, str]:
    """The plan `plan` gives, as its text or as a path, and the path ('' for text).

    A path is taken as given when absolute and against the working directory
    otherwise, as a command line's file argument is."""
    if looks_inline(plan):
        return loads(plan), ""
    path = os.path.abspath(plan)
    try:
        return load(path), path
    except OSError as exc:
        raise PlanError([f"the plan {plan!r} is neither a plan's text nor a file that can "
                         f"be read: {exc.strerror or exc}"]) from exc
