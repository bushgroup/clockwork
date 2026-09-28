"""Warm-up and stand-down: the two ends of an instrument day, ramped rather than stepped.

`warm_up` sends a method's `setup` phase and nothing after it -- no table, no mode change
-- so that the boxes reach the instrument's standing stack in the morning and have the
time before anyone acquires to settle into it. `stand_down` takes every box out of table
mode, brings AUKLET's DC bias and RF drive and every ARB module's traveling-wave range to
zero, and lowers every digital output: what the instrument should hold overnight (lab
record, task 95).

**The analog settings that put voltage on the instrument are ramped, in both
directions.** Every DC bias channel, every RF head's drive and every ARB module's range
moves in `steps` equal steps from **what the box read back** to its target, and waits
`dwell_s` after each. A hand at the front panel does the same, and the firmware does not:
`SRFDRV` writes the drive level and the next service pass applies it (wire format §8.3).
Because every ramp starts from a reading and not from a remembered value, one cut short
-- a Ctrl-C, a lost port -- is finished by running it again, and a box already at its
target is sent nothing.

A step is one `SDCBALL` per box with a DC bias bank, which range-checks the whole bank
as a set and latches it with one `LDAC` pulse so that every channel moves together
(§8.2); one `SRFDRV` per RF head; and one `SWFVRNG` per ARB module. A channel the method
does not declare keeps what it read back in every `SDCBALL`. RF frequency and mode are
never ramped: warm-up sends them as declared, before the ramp, and stand-down leaves them
alone.

Everything goes through the loop's own sender, so each string, each rejection and each
reading lands in the send log and the wire transcript the owner opened around the work,
and each ramp step is an event of its own (`RampStepped`). Getters only in the readbacks,
as everywhere: `GDIO` is not asked, because it reads the output image rather than the pin
and cannot confirm an `SDIO` moved anything (§4).

No Qt here; every call blocks, and belongs on the owner's thread.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from ..method import BoxMethod, Method, declared_commands, is_comment
from ..method.text import head as _head
from ..mips import Box, BoxState, MipsError, declared_settings, dio_command
from .loop import (
    ARM_TIMEOUT_S,
    AcqError,
    BoxReady,
    Event,
    Snapshot,
    Warned,
    _guarded,
    _read_one,
    _reporter,
    _send,
    _walk_phases,
)

__all__ = [
    "DIGITAL_OUTPUTS",
    "RAMPED",
    "WHEN_AFTER_STAND_DOWN",
    "WHEN_AFTER_WARM_UP",
    "WHEN_BEFORE_STAND_DOWN",
    "WHEN_BEFORE_WARM_UP",
    "RampStepped",
    "StandDownDone",
    "ramp_values",
    "stand_down",
    "warm_up",
]

WHEN_BEFORE_WARM_UP = "before warm-up"
WHEN_AFTER_WARM_UP = "after warm-up"
WHEN_BEFORE_STAND_DOWN = "before stand-down"
WHEN_AFTER_STAND_DOWN = "after stand-down"

RAMPED = ("SDCB", "SDCBALL", "SRFDRV", "SWFVRNG")
"""The setters warm-up holds out of a `setup` phase and ramps instead of sending."""

DIGITAL_OUTPUTS = "ABCDEFGHIJKLMNOP"
"""`SDIO`'s outputs in order; `GCHAN,DO` says how many of them a box has (§4)."""

AT_TARGET = 0.005
"""Within this of its target a setting is at it: every one ramped here is written and
read back to two decimals."""


@dataclass(frozen=True, slots=True)
class RampStepped(Event):
    """One step of a ramp sent to every box, before its dwell."""

    phase: str
    step: int
    steps: int
    dwell_s: float
    moved: tuple[str, ...] = ()
    """Each box that step moved, with what it moved: `auklet: 16 DC, 2 RF`."""

    @property
    def text(self) -> str:
        wait = f", then {self.dwell_s:g} s" if self.dwell_s else ""
        return (f"{self.phase}: ramp step {self.step} of {self.steps} "
                f"({'; '.join(self.moved) or 'nothing'}){wait}")


@dataclass(frozen=True, slots=True)
class StandDownDone(Event):
    """The digital outputs a stand-down lowered, which no getter can confirm."""

    lowered: tuple[str, ...] = ()
    """`auklet A-P`, one entry per box."""

    @property
    def text(self) -> str:
        return ("stand-down: outputs lowered by SDIO, which no getter confirms: "
                + ("; ".join(self.lowered) or "none"))


def ramp_values(start: float, target: float, steps: int) -> list[float]:
    """The value after each of `steps` equal steps from `start` to `target`, the last
    exactly `target`."""
    steps = max(1, int(steps))
    return [target if k == steps else start + (target - start) * k / steps
            for k in range(1, steps + 1)]


@dataclass
class _Plan:
    """One box's ramp: where each setting starts, and where it is going."""

    name: str
    box: Box
    dc_start: tuple[float, ...] = ()
    dc_target: tuple[float, ...] = ()
    dc_single: dict[int, float] = field(default_factory=dict)
    """Declared channels on a bank that did not read back whole, sent once at the last
    step with `SDCB`: an `SDCBALL` must name every channel, and an unread one cannot be
    held where it was."""
    rf: dict[int, tuple[float | None, float]] = field(default_factory=dict)
    arb: dict[int, tuple[float | None, float]] = field(default_factory=dict)
    """`(start, target)` per RF head and per ARB module. A start of None did not read
    back, and is set to its target at the first step: nothing can be ramped from a value
    nobody knows."""

    def commands(self, step: int, steps: int) -> tuple[list[str], str]:
        """This step's strings and a few words for what they move."""
        sent: list[str] = []
        moved: list[str] = []
        channels = sum(abs(start - target) > AT_TARGET
                       for start, target in zip(self.dc_start, self.dc_target, strict=True))
        if channels:
            bank = [ramp_values(start, target, steps)[step - 1]
                    for start, target in zip(self.dc_start, self.dc_target, strict=True)]
            sent.append("SDCBALL," + ",".join(f"{volts:.2f}" for volts in bank))
            moved.append(f"{channels} DC")
        if self.dc_single and step == steps:
            sent += [f"SDCB,{channel},{volts:.2f}"
                     for channel, volts in sorted(self.dc_single.items())]
            moved.append(f"{len(self.dc_single)} DC at once")
        for setter, settings, words in (("SRFDRV", self.rf, "RF"),
                                        ("SWFVRNG", self.arb, "ARB range")):
            count = 0
            for index, (start, target) in sorted(settings.items()):
                if start is None:
                    value = target if step == 1 else None
                elif abs(start - target) > AT_TARGET:
                    value = ramp_values(start, target, steps)[step - 1]
                else:
                    value = None
                if value is not None:
                    sent.append(f"{setter},{index},{value:.2f}")
                    count += 1
            if count:
                moved.append(f"{count} {words}")
        return sent, ", ".join(moved)

    @property
    def moves(self) -> bool:
        """Whether any step sends this box anything."""
        return bool(self.commands(1, 1)[0])


def _number(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _plan(name: str, box: Box, state: BoxState | None, *, dc: Mapping[int, float],
          rf: Mapping[int, float], arb: Mapping[int, float],
          report: Callable[[Event], None]) -> _Plan:
    """A box's ramp from `state` towards the targets given, which may be partial: a
    setting with no target is not moved."""
    plan = _Plan(name=name, box=box)
    if dc:
        bank = state.dc_bias_setpoints if state is not None else ()
        if bank and all(value is not None for value in bank) \
                and all(1 <= channel <= len(bank) for channel in dc):
            plan.dc_start = tuple(float(value) for value in bank)  # type: ignore[arg-type]
            plan.dc_target = tuple(dc.get(index + 1, start)
                                   for index, start in enumerate(plan.dc_start))
        else:
            plan.dc_single = dict(dc)
            report(Warned(f"{name}: the DC bias bank did not read back whole, so its "
                          f"{len(dc)} channel(s) are set once, at the last step, and not "
                          "ramped"))
    readings = {reading.channel: reading for reading in state.rf} if state else {}
    for channel, target in rf.items():
        held = readings.get(channel)
        start = held.drive_pct if held is not None else None
        if start is None:
            report(Warned(f"{name}: RF {channel}'s drive did not read back, so it is set "
                          "to its target at the first step and not ramped"))
        plan.rf[channel] = (start, target)
    for module, target in arb.items():
        held = (state.module(module).get("GWFVRNG")
                if state is not None and module in state.modules else None)
        start = _number(held) if held is not None else None
        if start is None:
            report(Warned(f"{name}: ARB module {module}'s range did not read back, so it "
                          "is set to its target at the first step and not ramped"))
        plan.arb[module] = (start, target)
    return plan


def _ramp(plans: Sequence[_Plan], *, phase: str, steps: int, dwell_s: float,
          report: Callable[[Event], None], stop: Callable[[], str | None] | None,
          sleep: Callable[[float], None]) -> int:
    """Every plan, one step at a time, every box in each step. The steps sent."""
    steps = max(1, int(steps))
    if not any(plan.moves for plan in plans):
        report(Warned(f"{phase}: every ramped setting already reads its target; "
                      "nothing ramped"))
        return 0
    for step in range(1, steps + 1):
        moved = []
        for plan in plans:
            commands, words = plan.commands(step, steps)
            for command in commands:
                _send(plan.box, plan.name, phase, command, report,
                      arm_timeout=ARM_TIMEOUT_S)
            if words:
                moved.append(f"{plan.name}: {words}")
        report(RampStepped(phase=phase, step=step, steps=steps, dwell_s=dwell_s,
                           moved=tuple(moved)))
        if dwell_s > 0:
            sleep(dwell_s)
        reason = stop() if stop is not None else None
        if reason and step < steps:
            raise AcqError(f"{phase} stopped after ramp step {step} of {steps} ({reason}); "
                           "the boxes hold that step, and running it again ramps on from "
                           "what they read back")
    return steps


def _read(boxes: Mapping[str, Box], names: Sequence[str],
          listings: dict[str, frozenset[str]], when: str,
          report: Callable[[Event], None]) -> tuple[BoxState, ...]:
    states = [_read_one(boxes[name], name, listings, when, report) for name in names]
    return tuple(state for state in states if state is not None)


def _held_out(entry: BoxMethod) -> tuple[list[str], dict[int, float], dict[int, float],
                                          dict[int, float]]:
    """A box's `setup` strings, its declared ones last, split into what is sent as it
    stands and the DC bias, RF drive and ARB range targets that are ramped instead.

    A later string for the same channel wins, which is the declaration over a
    hand-written line, as it is when `send_phases` sends both (`declared_commands`)."""
    commands = [command for command in tuple(entry.setup) + declared_commands(entry)
                if not is_comment(command)]
    sent = [command for command in commands if _head(command) not in RAMPED]
    held = [command for command in commands if _head(command) in RAMPED]
    dc: dict[int, float] = {}
    for command in held:
        if _head(command) == "SDCBALL":
            values = [_number(text) for text in command.partition(",")[2].split(",")]
            dc.update({index + 1: value for index, value in enumerate(values)
                       if value is not None})
        else:
            settings = declared_settings([command])
            for channel, text in settings.get("GDCB", {}).items():
                if _number(text) is not None:
                    dc[channel] = _number(text)  # type: ignore[assignment]
    settings = declared_settings(held)
    rf = {channel: value for channel, text in settings.get("GRFDRV", {}).items()
          if (value := _number(text)) is not None}
    arb = {module: value for module, text in settings.get("GWFVRNG", {}).items()
           if (value := _number(text)) is not None}
    return sent, dc, rf, arb


def warm_up(
    method: Method,
    boxes: Mapping[str, Box],
    *,
    steps: int,
    dwell_s: float,
    progress: Callable[[Event], None] | None = None,
    listings: dict[str, frozenset[str]] | None = None,
    stop: Callable[[], str | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Snapshot, int]:
    """Send every box `method`'s `setup` phase, ramping what puts voltage on a head.

    Each box is read back first; then its `setup` strings go as they stand, declared RF
    frequency and mode included, with every DC bias setpoint, RF drive level and ARB
    range held out (`RAMPED`); then all of those are ramped together from the reading to
    the declaration; then every box is read back again. Nothing is loaded and nothing is
    armed. A box the method names and `boxes` does not is a `KeyError`, as it is for
    `send_phases`. Answers the two readings, which are what a caller compares with the
    declaration, and the ramp steps sent; `stop` is asked after every step and a reason
    ends it.
    """
    report = _reporter(progress)
    missing = [entry.name for entry in method.boxes if entry.name not in boxes]
    if missing:
        raise KeyError(f"the method names {len(missing)} box(es) with no open port: "
                       + ", ".join(sorted(missing)))
    listings = {} if listings is None else listings
    names = [entry.name for entry in method.boxes]
    before = _read(boxes, names, listings, WHEN_BEFORE_WARM_UP, report)
    found = {state.name: state for state in before}
    plans = []
    for entry in method.boxes:
        box = boxes[entry.name]
        report(BoxReady(entry.name, entry.port, box.version(), box.box_name()))
        sent, dc, rf, arb = _held_out(entry)
        _walk_phases(box, entry.name, _guarded((("setup", sent),)), report,
                     arm_timeout=ARM_TIMEOUT_S, verify_tables=False)
        plans.append(_plan(entry.name, box, found.get(entry.name), dc=dc, rf=rf, arb=arb,
                           report=report))
    sent = _ramp(plans, phase="warm-up", steps=steps, dwell_s=dwell_s, report=report,
                 stop=stop, sleep=sleep)
    after = _read(boxes, names, listings, WHEN_AFTER_WARM_UP, report)
    return Snapshot(before=before, after=after), sent


def stand_down(
    boxes: Mapping[str, Box],
    *,
    steps: int,
    dwell_s: float,
    progress: Callable[[Event], None] | None = None,
    listings: dict[str, frozenset[str]] | None = None,
    stop: Callable[[], str | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Snapshot, int, tuple[str, ...]]:
    """Every box out of table mode, zeroed and its outputs low, and what it read back.

    `SMOD,LOC` on every box (a box already local is success, `Box.local`); a reading;
    every DC bias channel, RF drive and ARB range the reading found ramped to zero; then
    `SDIO,<ch>,0` on each digital output `A` onwards, as many as `GCHAN,DO` counts, which
    in LOC moves the line at once (§4); then a second reading. RF frequency and mode are
    left as they are. Answers the two readings, the ramp steps sent and, per box, the
    outputs lowered.
    """
    report = _reporter(progress)
    listings = {} if listings is None else listings
    names = list(boxes)
    for name in names:
        _send(boxes[name], name, "stand-down", "SMOD,LOC", report,
              arm_timeout=ARM_TIMEOUT_S)
    before = _read(boxes, names, listings, WHEN_BEFORE_STAND_DOWN, report)
    found = {state.name: state for state in before}
    plans = []
    for name in names:
        state = found.get(name)
        bank = state.dc_bias_setpoints if state is not None else ()
        dc = {index + 1: 0.0 for index in range(len(bank))}
        rf = {reading.channel: 0.0 for reading in state.rf} if state is not None else {}
        arb = ({module: 0.0 for module in state.modules
                if "GWFVRNG" in state.module(module)} if state is not None else {})
        plans.append(_plan(name, boxes[name], state, dc=dc, rf=rf, arb=arb, report=report))
    sent = _ramp(plans, phase="stand-down", steps=steps, dwell_s=dwell_s, report=report,
                 stop=stop, sleep=sleep)
    lowered = []
    for name in names:
        box = boxes[name]
        try:
            count = int(box.command("GCHAN,DO", value=True) or 0)
        except (MipsError, ValueError) as exc:
            report(Warned(f"{name}: GCHAN,DO would not answer ({exc}), so no output was "
                          "lowered"))
            continue
        channels = DIGITAL_OUTPUTS[:max(0, min(count, len(DIGITAL_OUTPUTS)))]
        for channel in channels:
            _send(box, name, "stand-down", dio_command(channel, False), report,
                  arm_timeout=ARM_TIMEOUT_S)
        if channels:
            lowered.append(f"{name} {channels[0]}-{channels[-1]}" if len(channels) > 1
                           else f"{name} {channels}")
    report(StandDownDone(lowered=tuple(lowered)))
    after = _read(boxes, names, listings, WHEN_AFTER_STAND_DOWN, report)
    return Snapshot(before=before, after=after), sent, tuple(lowered)
