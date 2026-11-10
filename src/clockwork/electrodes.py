"""The potentials a method puts on the electrodes that share one volume, and how far apart.

The one irreversible harm a method can do through the boxes is electrical: two
electrodes far enough apart in potential, across a gap at the operating pressure, to
break down. A template's knob ranges bound only the knobs it exposes, and a
hand-written method, or a template whose fixed strings set a bias, is bounded by
nothing but its author. So the standing limits bound the method itself, on the
setpoints its rendered strings would set (lab record, task 159).

The limits name the **nets** that share the volume, each by what drives it: a DC bias
channel or a fixed potential, with at most an ARB module's travelling wave or an RF
head riding on it. Every pair of nets is treated as adjacent, under one maximum
(Matt, 2026-11-09): at each moment of the method, the highest potential any net
reaches and the lowest any net reaches may differ by no more than `max_v`. The format
is `docs/instrument-limits.md`.

**What a moment is.** A box running a `STBLDAT` table moves its DC bias channels
between values, pulses included, so each state its table leaves the outputs in is a
moment of its own (`clockwork.mips.run_order`), and so is each state its strings leave
them in before the table runs. The ARB and RF terms are not timed against the table:
a module or head is taken at the widest it is set to anywhere in the method, which
over-states a moment by at most the difference between two amplitudes. Boxes with
tables of their own are combined every way, since nothing here knows their relative
timing.

**What is counted.** A potential the method sets. A net whose DC channel, ARB range or
RF drive the method never sets is left out of every moment, because what the box holds
there is the cold-start check's to find (`clockwork.envelope.cold_start`), not this
one's to guess. An ARB module's offsets (`SWFVOFF`, `SARBOFFA`/`B`, table channels
105 to 108) count at every value the method gives them and as 0 V where it gives
none. A table this cannot read, or one that ramps a DC channel, refuses: a bound that
cannot be computed is not met.

No Qt, no hardware, no I/O.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .method import BoxMethod, Method, declared_commands, is_comment
from .mips.compressor import COMPRESSION_COMMAND, compression_voltages
from .mips.table import TableSyntaxError, ValueKind, compile_table, run_order

__all__ = [
    "MAX_ARB_MODULE",
    "MAX_DC_CHANNEL",
    "MAX_RF_CHANNEL",
    "Electrodes",
    "Net",
    "RfCap",
    "Source",
    "Spread",
    "Unreadable",
    "from_dict",
    "refusals",
    "rf_drive_from_list",
    "rf_refusals",
    "worst",
]

MAX_DC_CHANNEL = 32
"""The highest DC bias channel a table addresses (§2)."""

MAX_ARB_MODULE = 6
"""The most ARB modules a box holds (§6.1)."""

MAX_RF_CHANNEL = 4
"""The most RF heads a table addresses, channels 33 to 36 (§2)."""

_RF_TABLE_CHANNEL = 32
"""RF head *n* is table channel 32 + *n* (§2)."""

_ARB_OFFSET_CHANNELS = {105: (1, "A"), 106: (1, "B"), 107: (2, "A"), 108: (2, "B")}
"""The table channels that move an ARB module's A or B offset (§2)."""


class Unreadable(ValueError):
    """A string the check needs and cannot read; the message names it."""


# -- the document -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Source:
    """One output of one box: a DC bias channel, an ARB module or an RF head."""

    box: str
    channel: int


@dataclass(frozen=True, slots=True)
class Net:
    """One set of electrodes at one potential, and what drives it.

    Its base is a DC bias channel (`dc`) or a fixed potential (`volts`, ground being
    0). On it rides at most one of an ARB module's travelling wave, ± half the module's
    p-p range plus its offsets, or an RF head's, ± `rf_peak_v_per_pct` volts per
    percent of drive.
    """

    name: str
    dc: Source | None = None
    volts: float | None = None
    arb: Source | None = None
    rf: Source | None = None
    rf_peak_v_per_pct: float | None = None


@dataclass(frozen=True)
class Electrodes:
    """The `[electrodes]` table of a limits document."""

    max_v: float
    """The largest difference allowed between any two nets at any moment."""
    nets: tuple[Net, ...] = field(default_factory=tuple)


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _source(raw: object, where: str, index: str, top: int,
            problems: list[str]) -> Source | None:
    if not isinstance(raw, dict):
        problems.append(f"{where} must be a table with box and {index}")
        return None
    unknown = [key for key in raw if key not in ("box", index)]
    if unknown:
        problems.append(f"{where}.{unknown[0]} is not a key the limits define "
                        f"(box, {index})")
    box, number = raw.get("box"), raw.get(index)
    if not isinstance(box, str) or not box:
        problems.append(f"{where}.box must be a box name")
        return None
    if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= top:
        problems.append(f"{where}.{index} must be a whole number from 1 to {top}")
        return None
    return Source(box, number)


_NET_KEYS = ("name", "dc", "volts", "arb", "rf", "rf_peak_v_per_pct")


def _net(raw: object, where: str, problems: list[str]) -> Net | None:
    if not isinstance(raw, dict):
        problems.append(f"{where} must be a table")
        return None
    for key in raw:
        if key not in _NET_KEYS:
            problems.append(f"{where}.{key} is not a key the limits define "
                            f"({', '.join(_NET_KEYS)})")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append(f"{where}.name must be text")
        return None
    where = f"electrodes net {name!r}"
    before = len(problems)
    if ("dc" in raw) == ("volts" in raw):
        problems.append(f"{where} needs one base, dc or volts, and not both")
    if "arb" in raw and "rf" in raw:
        problems.append(f"{where} carries both arb and rf: one wave rides on a net")
    dc = _source(raw["dc"], f"{where}.dc", "channel", MAX_DC_CHANNEL, problems) \
        if "dc" in raw else None
    volts = raw.get("volts")
    if volts is not None and not _number(volts):
        problems.append(f"{where}.volts must be a number")
    arb = _source(raw["arb"], f"{where}.arb", "module", MAX_ARB_MODULE, problems) \
        if "arb" in raw else None
    rf = _source(raw["rf"], f"{where}.rf", "channel", MAX_RF_CHANNEL, problems) \
        if "rf" in raw else None
    peak = raw.get("rf_peak_v_per_pct")
    if ("rf" in raw) != (peak is not None):
        problems.append(f"{where} gives rf and rf_peak_v_per_pct together or neither")
    elif peak is not None and (not _number(peak) or peak <= 0):
        problems.append(f"{where}.rf_peak_v_per_pct must be a positive number")
    if len(problems) > before:
        return None
    return Net(name=name, dc=dc, volts=None if volts is None else float(volts), arb=arb,
               rf=rf, rf_peak_v_per_pct=None if peak is None else float(peak))


def from_dict(raw: object, problems: list[str]) -> Electrodes | None:
    """Validate an `[electrodes]` table, adding every problem to `problems`."""
    if not isinstance(raw, dict):
        problems.append("[electrodes] must be a table with max_v and nets")
        return None
    for key in raw:
        if key not in ("max_v", "nets"):
            problems.append(f"electrodes.{key} is not a key the limits define "
                            "(max_v, nets)")
    max_v = raw.get("max_v")
    if not _number(max_v) or max_v <= 0:
        problems.append("electrodes.max_v must be a positive number of volts")
        max_v = None
    raw_nets = raw.get("nets")
    if not isinstance(raw_nets, list) or len(raw_nets) < 2:
        problems.append("electrodes.nets must list at least two nets, since a "
                        "difference takes two")
        return None
    nets = [_net(entry, f"electrodes.nets[{index}]", problems)
            for index, entry in enumerate(raw_nets)]
    names = [entry["name"] for entry in raw_nets
             if isinstance(entry, dict) and isinstance(entry.get("name"), str)]
    for name in sorted({name for name in names if names.count(name) > 1}):
        problems.append(f"electrodes names the net {name!r} more than once")
    if max_v is None or any(net is None for net in nets):
        return None
    return Electrodes(max_v=float(max_v), nets=tuple(net for net in nets if net is not None))


@dataclass(frozen=True, slots=True)
class RfCap:
    """The highest drive one RF head may be set to, in percent of full scale.

    Stands in for the RF term of the electrode check while a head's volts per percent
    are unknown: the drives an instrument has run at are the ones known to be safe
    (Matt, 2026-11-10; lab record, task 159)."""

    box: str
    channel: int
    max_pct: float


def rf_drive_from_list(raw: object, problems: list[str]) -> tuple[RfCap, ...]:
    """Validate a `rf_drive` list, adding every problem to `problems`."""
    if not isinstance(raw, list) or not raw:
        problems.append("rf_drive must list at least one head, as { box, channel, max_pct }")
        return ()
    caps: list[RfCap] = []
    for index, entry in enumerate(raw):
        where = f"rf_drive[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} must be a table with box, channel and max_pct")
            continue
        for key in entry:
            if key not in ("box", "channel", "max_pct"):
                problems.append(f"{where}.{key} is not a key the limits define "
                                "(box, channel, max_pct)")
        source = _source({key: entry[key] for key in ("box", "channel") if key in entry},
                         where, "channel", MAX_RF_CHANNEL, problems)
        cap = entry.get("max_pct")
        if not _number(cap) or not 0 <= cap <= 100:
            problems.append(f"{where}.max_pct must be a percent of full scale, 0 to 100")
            continue
        if source is None:
            continue
        if any((known.box, known.channel) == (source.box, source.channel) for known in caps):
            problems.append(f"rf_drive names {source.box} RF {source.channel} more than once")
            continue
        caps.append(RfCap(source.box, source.channel, float(cap)))
    return tuple(caps)


# -- what a method sets -------------------------------------------------------------


@dataclass
class _Box:
    """What one box's strings set: its DC bias states in order, and the rest whole."""

    states: list[tuple[str, dict[int, float]]] = field(default_factory=list)
    rf: dict[int, list[float]] = field(default_factory=dict)
    ranges: dict[int, list[float]] = field(default_factory=dict)
    offsets: dict[int, list[float]] = field(default_factory=dict)
    sets: dict[tuple[int, str], list[float]] = field(default_factory=dict)
    rf_auto: set[int] = field(default_factory=set)
    """RF heads the strings put in `AUTO`, where the box chooses the drive (§8.3)."""


def _float(text: str) -> float | None:
    try:
        return float(text.strip())
    except ValueError:
        return None


def _commands(method: Method, box: BoxMethod) -> list[str]:
    """Every string sent to the box, in the order sent, comments and tables out."""
    commands = [*box.setup, *declared_commands(box), *box.load, *box.arm]
    commands += [step.command for step in (*method.start, *method.reset)
                 if step.box == box.name]
    return [command for command in commands if not is_comment(command)]


def _head(command: str) -> tuple[str, list[str]]:
    head, _, rest = command.strip().partition(",")
    return head.strip().upper(), [arg.strip() for arg in rest.split(",")] if rest else []


def _read_box(method: Method, box: BoxMethod) -> _Box:
    read = _Box()
    dc: dict[int, float] = {}
    alternate: set[int] = set()
    alternate_ranges: dict[int, list[float]] = {}
    swaps = False
    for command in _commands(method, box):
        head, args = _head(command)
        if head.startswith("STBLDAT"):
            continue
        if head == COMPRESSION_COMMAND:
            levels, swapping = compression_voltages(command)
            for module, values in levels.items():
                read.ranges.setdefault(module, []).extend(values)
            swaps |= swapping
            continue
        index = int(args[0]) if args and args[0].isdigit() else None
        value = _float(args[1]) if len(args) > 1 else None
        if head == "SDCBALL":
            values = [_float(arg) for arg in args]
            dc.update({channel: volts for channel, volts in enumerate(values, start=1)
                       if volts is not None})
            read.states.append((f"while {box.name}'s strings are sent", dict(dc)))
        elif index is None:
            continue
        elif head == "SRFMODE" and len(args) > 1 and args[1].upper() == "AUTO":
            read.rf_auto.add(index)
        elif head == "SALTRENA" and len(args) > 1 and args[1].upper() == "TRUE":
            alternate.add(index)
        elif value is None:
            continue
        elif head == "SDCB":
            dc[index] = value
            read.states.append((f"while {box.name}'s strings are sent", dict(dc)))
        elif head == "SRFDRV":
            read.rf.setdefault(index, []).append(value)
        elif head == "SWFVRNG":
            read.ranges.setdefault(index, []).append(value)
        elif head == "SALTRNG":
            alternate_ranges.setdefault(index, []).append(value)
        elif head == "SWFVOFF":
            read.offsets.setdefault(index, []).append(value)
        elif head in ("SARBOFFA", "SARBOFFB"):
            read.sets.setdefault((index, head[-1]), []).append(value)
    for module in alternate:
        read.ranges.setdefault(module, []).extend(alternate_ranges.get(module, ()))
    if swaps:
        both = read.ranges.get(1, []) + read.ranges.get(2, [])
        for module in (1, 2):
            if both:
                read.ranges[module] = list(both)
    rest = dict(dc)
    tables = [command for command in box.load
              if not is_comment(command) and _head(command)[0].startswith("STBLDAT")]
    label = (f"at rest, before {box.name}'s table runs" if tables
             else f"as {box.name}'s strings leave it")
    if read.states:
        read.states[-1] = (label, read.states[-1][1])
    else:
        read.states.append((label, {}))
    for command in tables:
        read.states += _table_states(box.name, command, rest, read)
    return read


def _table_states(name: str, command: str, rest: Mapping[int, float],
                  read: _Box) -> list[tuple[str, dict[int, float]]]:
    try:
        compiled = compile_table(command)
    except TableSyntaxError as exc:
        raise Unreadable(f"{name}'s table cannot be read ({exc})") from None
    dc = dict(rest)
    states: list[tuple[str, dict[int, float]]] = []
    for _, point in run_order(compiled):
        moved = False
        for entry in point.entries:
            if entry.written is None:
                continue
            if entry.kind is ValueKind.DAC:
                channel = entry.chan + 1
                if channel > MAX_DC_CHANNEL:
                    raise Unreadable(f"{name}'s table ramps DC bias channel "
                                     f"{channel & 0x3F}, which the check does not model")
                dc[channel] = entry.written
                moved = True
            elif _RF_TABLE_CHANNEL < entry.chan <= _RF_TABLE_CHANNEL + MAX_RF_CHANNEL:
                read.rf.setdefault(entry.chan - _RF_TABLE_CHANNEL, []).append(entry.written)
            elif entry.chan in _ARB_OFFSET_CHANNELS:
                read.sets.setdefault(_ARB_OFFSET_CHANNELS[entry.chan], []).append(
                    entry.written)
        if moved:
            states.append((f"at tick {point.count} of {name}'s table", dict(dc)))
    return states


# -- the check ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Spread:
    """The widest moment: the net highest and the net lowest, and when."""

    volts: float
    high: str
    high_v: float
    low: str
    low_v: float
    when: str

    def text(self, max_v: float) -> str:
        if self.high == self.low:
            pair = f"{self.high} spans {self.low_v:g} to {self.high_v:g} V"
        else:
            pair = f"{self.high} reaches {self.high_v:g} V and {self.low} {self.low_v:g} V"
        return (f"{pair} {self.when}, {self.volts:g} V apart, more than the {max_v:g} V "
                "the standing limits allow between any two electrodes")


def _span(net: Net, boxes: Mapping[str, _Box],
          dc: Mapping[str, Mapping[int, float]]) -> tuple[float, float] | None:
    """The lowest and highest potential a net reaches at one moment, or None for a
    net the method does not set."""
    if net.dc is not None:
        base = dc.get(net.dc.box, {}).get(net.dc.channel)
        if base is None:
            return None
    else:
        base = net.volts or 0.0
    if net.arb is not None:
        box = boxes.get(net.arb.box)
        ranges = box.ranges.get(net.arb.channel) if box is not None else None
        if not ranges:
            return None
        half = max(abs(value) for value in ranges) / 2
        offset = box.offsets.get(net.arb.channel) or [0.0]
        sets = [value for which in "AB"
                for value in box.sets.get((net.arb.channel, which)) or [0.0]]
        return (base + min(offset) + min(sets) - half,
                base + max(offset) + max(sets) + half)
    if net.rf is not None and net.rf_peak_v_per_pct is not None:
        box = boxes.get(net.rf.box)
        drives = box.rf.get(net.rf.channel) if box is not None else None
        if not drives:
            return None
        peak = max(abs(value) for value in drives) * net.rf_peak_v_per_pct
        return base - peak, base + peak
    return base, base


def _moments(boxes: Mapping[str, _Box],
             timed: Sequence[str]) -> Iterable[tuple[str, dict[str, dict[int, float]]]]:
    """Every combination of the timed boxes' DC states, each distinct state once."""
    per_box = []
    for name in timed:
        seen: dict[tuple, tuple[str, dict[int, float]]] = {}
        for label, state in boxes[name].states:
            seen.setdefault(tuple(sorted(state.items())), (label, state))
        per_box.append([(name, label, state) for label, state in seen.values()])
    for combination in itertools.product(*per_box):
        label = " and ".join(label for _, label, _ in combination) or "throughout"
        yield label, {name: state for name, _, state in combination}


def worst(method: Method, electrodes: Electrodes) -> Spread | None:
    """The moment of `method` at which its nets are furthest apart, or None if fewer
    than two nets are set at any moment. `Unreadable` for a table it cannot read."""
    named = {source.box for net in electrodes.nets
             for source in (net.dc, net.arb, net.rf) if source is not None}
    boxes = {box.name: _read_box(method, box) for box in method.boxes if box.name in named}
    timed = sorted({net.dc.box for net in electrodes.nets
                    if net.dc is not None and net.dc.box in boxes})
    found: Spread | None = None
    for label, dc in _moments(boxes, timed):
        spans = [(net.name, span) for net in electrodes.nets
                 if (span := _span(net, boxes, dc)) is not None]
        if len(spans) < 2:
            continue
        high, (_, high_v) = max(spans, key=lambda item: item[1][1])
        low, (low_v, _) = min(spans, key=lambda item: item[1][0])
        if found is None or high_v - low_v > found.volts:
            found = Spread(high_v - low_v, high, high_v, low, low_v, label)
    return found


def rf_refusals(method: Method, caps: Sequence[RfCap]) -> list[str]:
    """Why `method` may not be sent under `rf_drive`: one sentence per RF head it would
    drive past its cap, put in `AUTO`, or drive at all where no cap names the head."""
    capped = {(cap.box, cap.channel): cap.max_pct for cap in caps}
    problems: list[str] = []
    for box in method.boxes:
        try:
            read = _read_box(method, box)
        except Unreadable as exc:
            problems.append(f"{exc}, so the RF drives it sets cannot be checked against "
                            "the standing limits")
            continue
        for channel in sorted(set(read.rf) | read.rf_auto):
            head = f"{box.name} RF {channel}"
            cap = capped.get((box.name, channel))
            if cap is None:
                problems.append(f"the method sets a drive on {head}, which the standing "
                                "limits' rf_drive does not name, so it may not be driven")
                continue
            if channel in read.rf_auto:
                problems.append(f"the method puts {head} in AUTO, where the box chooses "
                                f"the drive, and the standing limits hold it at {cap:g}% "
                                "or less")
            highest = max(read.rf.get(channel, [0.0]))
            if highest > cap:
                problems.append(f"the method drives {head} at {highest:g}%, more than the "
                                f"{cap:g}% the standing limits allow")
    return problems


def refusals(method: Method, electrodes: Electrodes) -> list[str]:
    """Why `method` may not be sent under `electrodes`: one sentence, or none."""
    try:
        spread = worst(method, electrodes)
    except Unreadable as exc:
        return [f"{exc}, so the potentials it puts on the electrodes cannot be checked "
                "against the standing limits"]
    if spread is None or spread.volts <= electrodes.max_v:
        return []
    return [spread.text(electrodes.max_v)]
