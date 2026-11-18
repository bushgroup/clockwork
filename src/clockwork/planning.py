"""A series plan from its text to the owner's job: one path, the `series` tool's and the window's.

Everything between a plan's text and a `SeriesJob` used to live in `Toolbox.series`: the
render of every point, the acquisition loop's `refusals` and the limits' `envelope.check`
on each, each point's minutes, the order drawn under the seed, the job, and the run
record's `plans` entry. The window runs plans too now (the window revision's R2; lab
record, task 166), so that path is here, with the limits' verdict **returned rather than
raised**: the tool refuses on it, as it always has, and the window marks the points and
runs them, since a person at the window is never held to an agent's limits (R10). The
template's ranges and the loop's refusals refuse on every route.

`check` is the one judgement; `Checked` answers what each caller needs from it -- the
tool's refusal sentences, the points as a `check` answers them, the job, and the record's
entry. The daemon calls `check` again on the plan's text when it begins a window series'
record (`clockwork.owner.keeper.begin`), so the entry it writes is the one the tool would
have written for the same plan and seed.

**What is hashed** (A5): the plan's text as it is read, with Windows line endings made
Unix ones (`series.loads`), whichever way it arrived. `plan_sha256` is what every `series`
line of the audit log carries and what the record keeps; `custody` words the audit log's
matching `check` line for the window.

Qt-free, beside `clockwork.series`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import envelope
from . import series as series_module
from .acq.loop import refusals
from .envelope import Limits
from .instrument import UNCALIBRATED, Instrument
from .method.template import Rendered, Template, TemplateError, render
from .series import Plan, PlanError, Point

__all__ = ["Checked", "PointCheck", "check", "custody", "locate", "plan_sha256", "sha256",
           "text_of"]


text_of = series_module.text_of


def sha256(text: str) -> str:
    """The SHA-256 of a plan's text as read (LF line endings), as hex."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def plan_sha256(plan: object) -> str | None:
    """The hash a `series` call's audit line carries for its `plan` argument, however it
    was passed; None where it names a file that cannot be read, or is not text."""
    if not isinstance(plan, str):
        return None
    try:
        text, _ = text_of(plan)
    except PlanError:
        return None
    return sha256(text)


def locate(name: str, libraries: Sequence[str]) -> str:
    """A plan's `template`, found in the library's directories in order, as the tool's
    `_in_library` finds one: as given when absolute, refused when in two directories.
    `PlanError` for a template in none of them."""
    if os.path.isabs(name):
        return name
    found = [os.path.join(library, name) for library in libraries
             if os.path.isfile(os.path.join(library, name))]
    if len(found) > 1:
        raise PlanError([f"the plan's template {name!r} is in more than one library, "
                         + " and ".join(found)])
    if not found:
        where = ", ".join(libraries) or "no library is set"
        raise PlanError([f"the plan's template {name!r} is not in the library ({where})"])
    return found[0]


def custody(found: Mapping[str, object] | None, *, today: _dt.date | None = None) -> str:
    """The window's line for the audit log's `check` of a plan's hash, or for none:
    `checked through mcp at 14:02, unchanged since`. The date is said when it is not
    today's."""
    if not found:
        return "no check of this text on record"
    try:
        when = _dt.datetime.fromisoformat(str(found.get("time")))
    except ValueError:
        stamp = str(found.get("time") or "an unknown time")
    else:
        stamp = when.strftime("%H:%M")
        if when.date() != (today or _dt.date.today()):
            stamp = when.strftime("%Y-%m-%d %H:%M")
    return f"checked through {found.get('via') or 'an agent'} at {stamp}, unchanged since"


@dataclass(frozen=True)
class PointCheck:
    """One point of a plan, rendered and judged."""

    point: Point
    rendered: Rendered | None
    problem: str = ""
    """The render's refusal, e.g., a knob outside the template's range; empty when it
    rendered. Refuses on every route."""
    refusals: tuple[str, ...] = ()
    """The acquisition loop's refusals of the rendered method. Refuse on every route."""
    limits: tuple[str, ...] = ()
    """`envelope.check`: why an agent's send of this point would be refused."""
    outside: tuple[str, ...] | None = None
    """`envelope.outside_limits`: where a person's point lies outside the limits; None
    with no limits in force."""
    seconds: float | None = None

    @property
    def name(self) -> str:
        return series_module.describe(self.point)

    @property
    def minutes(self) -> float | None:
        return None if self.seconds is None else round(self.seconds / 60, 1)

    @property
    def blocked(self) -> str:
        """Why no route may run this point, or empty."""
        if self.problem:
            return self.problem
        return "; ".join(self.refusals)

    def entry(self) -> dict[str, Any]:
        """The point as a `check` answers it and the record keeps it."""
        return {"index": self.point.index, "knobs": dict(self.point.knobs),
                "repetitions": (self.rendered.method.acquisition.accumulations
                                if self.rendered is not None else None),
                "replicates": self.point.replicates, "reference": self.point.reference,
                "minutes": self.minutes}


@dataclass(frozen=True)
class Checked:
    """A plan, its template and every point judged: what `check` answers."""

    plan: Plan
    template: Template
    template_path: str
    path: str
    """The plan's file, or empty for a plan passed as text."""
    points: tuple[PointCheck, ...]
    limits: Limits | None

    @property
    def sha256(self) -> str:
        return sha256(self.plan.text)

    def agent_refusals(self) -> list[str]:
        """One sentence a point for every point the tool refuses, in planned order:
        `point 3 (...): <why>`. Empty when an agent may run the whole plan."""
        said = []
        for checked in self.points:
            if checked.problem:
                said.append(f"{checked.name}: {checked.problem}")
            elif checked.refusals or checked.limits:
                said.append(f"{checked.name}: " + "; ".join(checked.refusals + checked.limits))
        return said

    def blocked(self) -> list[str]:
        """The same for what refuses a person too: the template's ranges and the loop's
        refusals, never the limits."""
        return [f"{checked.name}: {checked.blocked}" for checked in self.points
                if checked.blocked]

    def outside(self) -> list[dict[str, Any]] | None:
        """Every point outside the limits, `{index, problems}`; None with no limits."""
        if self.limits is None:
            return None
        return [{"index": checked.point.index, "problems": list(checked.outside or ())}
                for checked in self.points if checked.outside]

    def entries(self) -> list[dict[str, Any]]:
        return [checked.entry() for checked in self.points]

    @property
    def acquisitions(self) -> int:
        return series_module.acquisitions([checked.point for checked in self.points])

    @property
    def files(self) -> int:
        return series_module.files([checked.point for checked in self.points])

    @property
    def minutes(self) -> float | None:
        """About how long the plan acquires for; None where any point's period is unknown."""
        seconds = [checked.seconds for checked in self.points]
        if None in seconds:
            return None
        return round(sum(s for s in seconds if s is not None) / 60, 1)

    def executed(self, seed: int | None) -> list[PointCheck]:
        """The points in the order acquired under `seed`."""
        by_index = {checked.point.index: checked for checked in self.points}
        return [by_index[point.index] for point in
                series_module.order([checked.point for checked in self.points], seed)]

    def job(self, *, seed: int | None, first_position: int = 1, label: str,
            series: str = "", setup: bool = True, conditions: str = "", directory: str = "",
            initials: str = "", request: str = "", instrument: Instrument = UNCALIBRATED,
            instrument_path: str = "", cold_start: str = "", source: str = "",
            at_window: str = "", with_plan: bool = False) -> Any:
        """The owner's `SeriesJob` for this plan, its points in the order `seed` draws and
        indexed from `first_position`. `with_plan` carries the plan's text and path, for a
        job whose record the owner begins (`source`). `ValueError` for a point that did not
        render."""
        from .owner.jobs import PlannedPoint, SeriesJob  # the owner imports this module

        offset = first_position - 1
        points = []
        for checked in self.executed(seed):
            if checked.rendered is None:
                raise ValueError(f"{checked.name} did not render: {checked.problem}")
            points.append(PlannedPoint(
                index=offset + checked.point.index, method=checked.rendered.method,
                knobs=dict(checked.rendered.knobs), replicates=checked.point.replicates,
                reference=checked.point.reference, minutes=checked.minutes))
        return SeriesJob(
            label=label, points=tuple(points), series=series, seed=seed,
            first_position=first_position, setup=setup, conditions=conditions,
            directory=directory, initials=initials, request=request, instrument=instrument,
            instrument_path=instrument_path, method_path=self.template_path,
            template=self.template.text, labels=dict(self.plan.labels),
            cold_start=cold_start, source=source, at_window=at_window,
            plan=self.plan.text if with_plan else "",
            plan_path=self.path if with_plan else "")

    def plans_entry(self, *, job: int, seed: int | None, first_place: int,
                    checked: Mapping[str, object] | None) -> dict[str, Any]:
        """The run record's `plans` entry for this plan acquired as job `job`: the keys
        the tool has always written, in its order, then the hash, the check it matched
        and the points outside the limits."""
        offset = first_place - 1
        return {
            "text": self.plan.description, "job": job, "plan": self.plan.text,
            "path": self.path, "template": self.template_path,
            "template_hash": self.template.hash, "labels": dict(self.plan.labels),
            "seed": seed, "shuffled": seed is not None, "first_place": first_place,
            "acquisitions": self.acquisitions, "files": self.files,
            "minutes": self.minutes,
            "points": [{**entry, "index": offset + entry["index"]}
                       for entry in self.entries()],
            "order": [offset + checked.point.index for checked in self.executed(seed)],
            "sha256": self.sha256,
            "checked": dict(checked) if checked else None,
            "outside_limits": (None if (outside := self.outside()) is None else
                               [{**entry, "index": offset + entry["index"]}
                                for entry in outside]),
        }

    def acquisitions_entry(self, *, job: int, first_place: int) -> dict[str, Any]:
        """The run record's `acquisitions` entry, before the tool adds its `cold_start`."""
        return {"job": job, "series": True, "replicates": self.plan.replicates,
                "first_place": first_place, "points": len(self.points),
                "method": self.template.name}


def check(planned: Plan, template: Template, *, template_path: str = "", path: str = "",
          limits: Limits | None = None, real: bool = True,
          pusher_period_us: float | None = None) -> Checked:
    """Every point of `planned` rendered from `template` and judged: by the template's
    ranges, the acquisition loop's refusals, the limits as an agent's send is checked
    against them (`real` is whether the boxes are the instrument's), and where each lies
    outside the limits; and its seconds at `pusher_period_us`, else the template's own
    tick, else None rather than a guess.

    `PlanError` for a plan that does not expand against the template (a knob it does not
    have); a point that does not render is judged, not raised."""
    judged = []
    for point in series_module.expand(planned, template):
        try:
            rendered = render(template, dict(point.knobs), dict(planned.labels))
        except TemplateError as exc:
            judged.append(PointCheck(point=point, rendered=None, problem=str(exc)))
            continue
        period = pusher_period_us or rendered.tick_us
        judged.append(PointCheck(
            point=point, rendered=rendered,
            refusals=tuple(refusals(rendered.method)),
            limits=tuple(envelope.check(rendered.method, rendered, limits, real=real)),
            outside=(tuple(envelope.outside_limits(rendered, limits))
                     if limits is not None else None),
            seconds=(series_module.seconds(rendered.method, point.replicates, period)
                     if period is not None else None)))
    return Checked(plan=planned, template=template, template_path=template_path, path=path,
                   points=tuple(judged), limits=limits)
