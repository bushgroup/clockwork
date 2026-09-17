"""What a box is holding, read back with getters and nothing else.

The strings a method sends are not the experiment. A pulse sequence moves a
few DC bias channels between values the box already holds; the sixteen DC
biases themselves, the RF heads, and seven of the eight ARB modules' frequency
and range were set at a front panel or left behind by the method before, and
appear in no file at all. Two acquisitions taken either side of a front-panel
change were indistinguishable until this module existed (lab record, task 40).

So `read_state` asks one box every getter that describes its persistent state,
writes nothing, and hands back a `BoxState`: the answers as the box gave them,
the getters it does not have, and the ones it refused. What the caller does
with it -- a block of `#` lines in the send log, a `Global_Params` stamp, a
diff against what a method declared -- is `clockwork.acq.loop`'s business.

    from clockwork.mips import Box, read_state

    with Box.open("COM6", name="auklet") as box:
        state = read_state(box)
        print(state.render())

Two rules this module exists to keep, both from §8.4 of the wire format:

- **Only what `GCMDS` lists is sent.** A getter this firmware does not have is
  rejected, and a rejection leaves something on the wire that arrives after it,
  so every later answer is shifted by one and still looks plausible. Filtering
  on the box's own listing is what stops a readback from inventing a plausible
  record of an instrument.
- **Resync after every rejection** that gets through anyway -- a channel past
  the count, a getter refused for a mode reason -- before the next command.

Nothing here is a setter and nothing here is Qt. Every call blocks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .box import Box, MipsError

__all__ = [
    "ARB_MODULE_GETTERS",
    "COMPRESSOR_GETTERS",
    "BoxState",
    "MAX_ARB_MODULES",
    "RESYNC_AFTER_NAK_S",
    "SEQUENCER_GETTERS",
    "RfReading",
    "declared_settings",
    "describe",
    "read_sequencer",
    "read_state",
]

RESYNC_AFTER_NAK_S = 0.25
"""Silence that ends the drain after a rejection.

`Box.resync` drops what the rejection left on the wire; draining only status
lines leaves the stray token in place, which is not enough. Measured on
BUFFLEHEAD 2026-09-14 (lab record, task 10).
"""

MAX_ARB_MODULES = 4
"""Modules per box the readback will ask about.

Four is the hardware maximum (§6.1) and is what both ARB boxes carry. The
readback asks `GCHAN,ARB` first and only walks that many, so a box with fewer
costs no rejections; this is the ceiling for a box that will not say.
"""

ARB_MODULE_GETTERS: tuple[str, ...] = (
    "GWFREQ",
    "GWFVRNG",
    "GWFDIR",
    "GARBMODE",
    "GALTWFM",
    "GALTENA",
    "GALTHWD",
)
"""The per-module getters, each sent as `<getter>,<module>`.

The first four are the waveform state a transmission or compression experiment
depends on; the last three are the alternate-waveform system (§6.4), whose `REV`
on one module per box is a persistent setting neither golden method sets.

`GARBCORDER` is deliberately absent. It is refused with error 3 on every module
of both boxes -- a getter refused for a mode reason, the message naming the mode
the box is already in -- and nothing clockwork does needs the compression order
(lab record, task 40). `SARBCCLK` has no getter at all on this firmware (§7).
"""

COMPRESSOR_GETTERS: tuple[str, ...] = ("GARBCTBL", "GARBCTD")
"""The box-wide ARB state, which takes no module argument (§6.6).

One compressor per box, not one per module. `GARBCTBL` is the compression table
currently loaded, which is volatile and is the one way to tell a box that has
had a compression method run on it from one that has not; `GARBCTD` is the
saved ARB trigger delay, which `notes/sync-design.md` reads the start list's
step gap against.
"""

SEQUENCER_GETTERS: tuple[str, ...] = ("GTBLFRQ", "GTBLSTA")
"""The table engine's own state, which is the one part of a readback that
changes when a box is armed.

Public because it is also the whole of `read_sequencer`, the cheap reading a
caller takes after the `arm` phase when the expensive one had to be taken
before it (§8.2, and `clockwork.acq.loop.send_phases`).
"""

_IDENTITY_GETTERS: tuple[str, ...] = ("GVER", "GNAME")
_COUNT_GETTERS: tuple[str, ...] = ("GCHAN,DCB", "GCHAN,RF", "GCHAN,ARB")
_BANK_GETTERS: tuple[str, ...] = ("GDCBALL", "GDCBALLV")
_RF_CHANNEL_GETTERS: tuple[str, ...] = ("GRFMODE", "GRFPWR")

_TABLE_IDLE: frozenset[str] = frozenset({"IDLE", "ABORTED"})
"""`GTBLSTA` answers that mean no table owns the box's service loop.

`READY` and `TRIGGERED` are both inside table mode: the first is a box that
has armed and not yet run, the second one whose timer has been triggered
(§4). Only `IDLE` and `ABORTED` are reached by leaving it.
"""


@dataclass(frozen=True, slots=True)
class RfReading:
    """One RF channel as `GRFALL` reports it, plus the two fields it omits.

    `GRFALL` carries **four** fields per channel -- frequency, drive level,
    positive peak, negative peak -- where its own help text and the firmware's
    dispatch comment both say three (§8.3). The peaks are live measurements and
    move between reads; the first two are settings and do not.
    """

    channel: int
    frequency_hz: float | None = None
    drive_pct: float | None = None
    peak_positive_v: float | None = None
    peak_negative_v: float | None = None
    mode: str = ""
    power_w: float | None = None


@dataclass(frozen=True, slots=True)
class BoxState:
    """One box's persistent state at one moment, as its getters answered.

    `values` is keyed by the command as it was sent -- `GDCBALL`, `GWFREQ,2` --
    and holds the answer verbatim, because a string the box produced is the
    evidence and a float this host parsed out of it is a derivation. The typed
    accessors below do the parsing, and every one of them tolerates a missing or
    unparseable answer rather than raising: a readback whose job is to record
    what happened must survive a box that answered oddly.
    """

    name: str
    values: Mapping[str, str] = field(default_factory=dict)
    skipped: tuple[str, ...] = ()
    """Getters not sent because the box's `GCMDS` listing does not name them."""

    refused: Mapping[str, str] = field(default_factory=dict)
    """Getters the box rejected, and what it said. Sent and answered `no`."""

    listed: bool = True
    """Whether a `GCMDS` listing was available to filter on.

    False means the box would not list its commands, so every getter was sent
    blind and `skipped` is empty for a reason that is not "the box has them all".
    """

    # -- identity ----------------------------------------------------------

    @property
    def version(self) -> str:
        """`GVER`."""
        return self.values.get("GVER", "")

    @property
    def identity(self) -> str:
        """`GNAME`, the box's own idea of what it is called."""
        return self.values.get("GNAME", "")

    def count(self, subsystem: str) -> int | None:
        """`GCHAN,<subsystem>` as an integer, or None if it did not answer."""
        return _as_int(self.values.get(f"GCHAN,{subsystem}"))

    # -- the table engine --------------------------------------------------

    @property
    def table_status(self) -> str:
        """`GTBLSTA`, upper-cased: `IDLE`, `READY`, `TRIGGERED`, `ABORTED`.

        Empty for a box that was not asked or would not answer, which is a real
        case rather than a defensive one: `GTBLSTA` is absent from v1.163t and
        NAKs there as an invalid command (§4).
        """
        return self.values.get("GTBLSTA", "").strip().upper()

    @property
    def monitors_converting(self) -> bool:
        """Whether `GDCBALLV` was answered by a monitor that is still reading.

        False while a table owns the box. The DC bias monitors are maintained by
        a 100 ms service task that does not run in table mode, so from `SMOD,TBL`
        until the box is local again `GDCBALLV` reports a frozen array: not the
        output, and not the last true reading either, but wherever the box's
        filter had got to when it armed. Measured on AUKLET 2026-09-16 at 0.750
        of setpoint on fifteen of sixteen channels, bit-identical over 40 s
        (§8.2, lab record, task 43).

        A box that did not answer `GTBLSTA` is treated as converting. The
        readback is meant to be taken in local mode in the first place, and a
        firmware too old to have the getter is also too old to be told apart
        from one that answered `IDLE`; assuming the worst there would silently
        drop the comparison on exactly the boxes nothing else knows about.
        """
        return self.table_status in _TABLE_IDLE or not self.table_status

    # -- DC bias -----------------------------------------------------------

    @property
    def dc_bias_setpoints(self) -> tuple[float | None, ...]:
        """`GDCBALL`, one value per channel in channel order, 1-based on read.

        Index 0 is channel 1. The firmware stops at the first channel it has no
        data for, so the length is the channel count (§8.2).
        """
        return _as_floats(self.values.get("GDCBALL"))

    @property
    def dc_bias_readbacks(self) -> tuple[float | None, ...]:
        """`GDCBALLV`, the monitor readings behind the setpoints above."""
        return _as_floats(self.values.get("GDCBALLV"))

    def dc_bias(self, channel: int) -> float | None:
        """The setpoint of one 1-based channel, or None if it was not read."""
        values = self.dc_bias_setpoints
        return values[channel - 1] if 1 <= channel <= len(values) else None

    def dc_bias_readback(self, channel: int) -> float | None:
        """The monitor reading of one 1-based channel, or None."""
        values = self.dc_bias_readbacks
        return values[channel - 1] if 1 <= channel <= len(values) else None

    # -- RF ----------------------------------------------------------------

    @property
    def rf(self) -> tuple[RfReading, ...]:
        """Every RF channel `GRFALL` reported, with its mode and power folded in."""
        fields = _as_floats(self.values.get("GRFALL"))
        readings: list[RfReading] = []
        for index in range(len(fields) // 4):
            channel = index + 1
            frequency, drive, positive, negative = fields[index * 4:index * 4 + 4]
            readings.append(RfReading(
                channel=channel,
                frequency_hz=frequency,
                drive_pct=drive,
                peak_positive_v=positive,
                peak_negative_v=negative,
                mode=self.values.get(f"GRFMODE,{channel}", ""),
                power_w=_as_float(self.values.get(f"GRFPWR,{channel}")),
            ))
        return tuple(readings)

    # -- ARB modules -------------------------------------------------------

    @property
    def modules(self) -> tuple[int, ...]:
        """Which ARB modules answered at least one getter, in order."""
        found = {
            int(command.rpartition(",")[2])
            for command in self.values
            if command.partition(",")[0] in ARB_MODULE_GETTERS
            and command.rpartition(",")[2].isdigit()
        }
        return tuple(sorted(found))

    def module(self, module: int) -> dict[str, str]:
        """One module's answers, keyed by the bare getter name."""
        return {
            getter: self.values[f"{getter},{module}"]
            for getter in ARB_MODULE_GETTERS
            if f"{getter},{module}" in self.values
        }

    # -- rendering ---------------------------------------------------------

    def render(self) -> str:
        """The whole state as lines a person reads, one fact per line.

        This is what goes into the send log and into the file's stamp, and it is
        the same text in both so that the two cannot disagree. Laid out to be
        read down a page beside the strings that were sent: the box, then what
        it is, then the bank, then the heads, then a row per module.
        """
        # A reading too narrow to have asked for either is `read_sequencer`'s, and
        # a bare `auklet:` reads as a box that would not say what it is.
        described = f"{self.version}  {self.identity}".strip()
        lines = [f"{self.name}: {described}" if described else self.name]
        counts = ", ".join(
            f"{count} {subsystem}" for subsystem in ("DCB", "RF", "ARB")
            if (count := self.count(subsystem)) is not None
        )
        if counts:
            lines.append(f"  channels    {counts}")
        for label, getter in (("clock", "GTBLFRQ"), ("table", "GTBLSTA")):
            if getter in self.values:
                lines.append(f"  {label:<11} {self.values[getter]}")
        setpoints, readbacks = self.dc_bias_setpoints, self.dc_bias_readbacks
        for index, value in enumerate(setpoints):
            monitor = readbacks[index] if index < len(readbacks) else None
            lines.append(
                f"  DCB {index + 1:<7} {_volts(value):>9}"
                + (f"   (reads {_volts(monitor):>9})" if monitor is not None else "")
            )
        if readbacks and not self.monitors_converting:
            # Said here rather than left for a reader to infer from the `table`
            # line six rows up, because these numbers go into the file's stamp
            # and a stamp is read years later by somebody who has not read §8.2.
            lines.append(f"  note        the monitor readings above were taken with the "
                         f"table {self.table_status.lower()}, where they do not convert; "
                         "they are not what the channels are at")
        for reading in self.rf:
            lines.append(
                f"  RF {reading.channel:<8} {_hertz(reading.frequency_hz)} Hz, "
                f"drive {_fixed(reading.drive_pct)}%"
                + (f", {reading.mode}" if reading.mode else "")
                + (f", {_fixed(reading.power_w)} W" if reading.power_w is not None else "")
                + f", peaks {_fixed(reading.peak_positive_v)}/"
                  f"{_fixed(reading.peak_negative_v)} V"
            )
        for getter in COMPRESSOR_GETTERS:
            if getter in self.values:
                lines.append(f"  {getter[1:]:<11} {self.values[getter] or '(empty)'}")
        for module in self.modules:
            answers = self.module(module)
            lines.append(
                f"  module {module:<4} "
                + ", ".join(f"{getter[1:]} {answers[getter]}"
                            for getter in ARB_MODULE_GETTERS if getter in answers)
            )
        if self.skipped:
            lines.append("  not asked   " + ", ".join(self.skipped)
                         + " (this firmware does not list them)")
        for command, why in self.refused.items():
            lines.append(f"  refused     {command}: {why}")
        if not self.listed:
            lines.append("  note        GCMDS would not answer, so every getter above "
                         "was sent without knowing the box has it")
        return "\n".join(lines)


def read_state(
    box: Box,
    *,
    listing: frozenset[str] | None = None,
    modules: int | None = None,
) -> BoxState:
    """Read one box's persistent state back, getters only, nothing written.

    `listing` is the box's `GCMDS` set; it is read here if not supplied, which
    costs one round trip of a few hundred lines and is what makes the rest safe
    (§8.4). Pass a listing already in hand -- a script that probed the box
    earlier in the same session -- to skip it. An empty listing means the box
    would not answer `GCMDS`, and every getter is then sent blind.

    `modules` is how many ARB modules to walk, and defaults to what `GCHAN,ARB`
    answers, capped at `MAX_ARB_MODULES`. A box with no ARB boards answers 0 and
    costs no per-module round trips at all, which is AUKLET.

    **Order matters and is not alphabetical.** Identity first, so that a state
    record names the box even if everything after it fails; then the counts,
    because the RF and module sweeps are sized from them; then the state itself.
    """
    reading = _Reading(box, _listing(box, listing))
    ask = reading.ask
    # Forty-odd round trips that say nothing one at a time. They stay in the wire
    # transcript and out of the send log, where the block this returns goes instead
    # (`Box.summarised`, lab record, task 40).
    with box.summarised():
        for getter in _IDENTITY_GETTERS + _COUNT_GETTERS:
            ask(getter)
        for getter in SEQUENCER_GETTERS + _BANK_GETTERS:
            ask(getter)

        if ask("GRFALL") is not None:
            channels = _as_int(reading.values.get("GCHAN,RF")) or 0
            for channel in range(1, channels + 1):
                for getter in _RF_CHANNEL_GETTERS:
                    ask(f"{getter},{channel}")

        if modules is None:
            modules = min(_as_int(reading.values.get("GCHAN,ARB")) or 0, MAX_ARB_MODULES)
        if modules:
            for getter in COMPRESSOR_GETTERS:
                ask(getter)
        for module in range(1, modules + 1):
            for getter in ARB_MODULE_GETTERS:
                ask(f"{getter},{module}")

    return reading.state()


def read_sequencer(box: Box, *, listing: frozenset[str] | None = None) -> BoxState:
    """Read back the table engine's state and nothing else: two round trips.

    The reading a caller takes **after** the `arm` phase, when the whole-state
    reading had to be taken before it. A `read_state` taken with the box armed
    reports DC bias monitors that have stopped converting (§8.2), so
    `clockwork.acq.loop.send_phases` takes the expensive reading between `setup`
    and `load` and this one at the end, and the record still says the box was
    left armed rather than idle.

    Getters only, like `read_state`, and a `BoxState` in the same shape so that
    the same `render` writes it into the same send log. It names the box, says
    what the table engine answered, and is silent about everything a two-getter
    reading cannot know.

    `listing` is the box's `GCMDS` set and is read here if not supplied, which
    costs more than the reading itself; a caller that has just taken a
    `read_state` has one in hand and should pass it.
    """
    reading = _Reading(box, _listing(box, listing))
    with box.summarised():
        for getter in SEQUENCER_GETTERS:
            reading.ask(getter)
    return reading.state()


def _listing(box: Box, listing: frozenset[str] | None) -> frozenset[str]:
    """The box's `GCMDS` set, read here if the caller has not got one already.

    One unframed round trip of a few hundred lines, and what makes every getter
    after it safe (§8.4). A caller holding one from earlier in the same session
    passes it and pays nothing; an empty set is a box that would not list its
    commands, and every getter then goes out blind.
    """
    if listing is not None:
        return listing
    with box.summarised():
        return box.command_listing()


class _Reading:
    """One box's answers as they accumulate, keeping the two rules of §8.4.

    A getter the box's `GCMDS` listing does not name is never sent, and a
    rejection that gets through anyway is drained before the next command. An
    empty listing means nothing is known about what the box has, so everything
    is sent blind; that is a `read_state` whose `GCMDS` failed, and it is also
    every `read_sequencer`, whose two getters cost less than a listing would.
    """

    def __init__(self, box: Box, listing: frozenset[str]) -> None:
        self.box = box
        self.listing = listing
        self.values: dict[str, str] = {}
        self.skipped: list[str] = []
        self.refused: dict[str, str] = {}

    def ask(self, command: str) -> str | None:
        head = command.partition(",")[0]
        if self.listing and head not in self.listing:
            self.skipped.append(command)
            return None
        try:
            answer = self.box.command(command, value=True)
        except (MipsError, ValueError) as exc:
            # A rejection is a result: half of what a readback learns is which
            # getters a firmware has and which channels a board carries. The
            # drain is not optional -- without it the next getter reads this
            # one's leftover (section 8.4).
            self.box.resync(settle=RESYNC_AFTER_NAK_S)
            self.refused[command] = str(exc)
            return None
        if answer is not None:
            self.values[command] = answer
        return answer

    def state(self) -> BoxState:
        return BoxState(
            name=self.box.name,
            values=self.values,
            skipped=tuple(self.skipped),
            refused=self.refused,
            listed=bool(self.listing),
        )


# -- parsing the box's own strings ---------------------------------------------------


def _as_int(text: str | None) -> int | None:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _as_float(text: str | None) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _as_floats(text: str | None) -> tuple[float | None, ...]:
    """A comma-separated reply as floats, with `None` for a field that is not one.

    A field that will not parse becomes `None` rather than dropping out, because
    the position in these replies *is* the channel number: dropping one would
    renumber every channel after it (§8.2).
    """
    if not text:
        return ()
    return tuple(_as_float(field) for field in text.split(","))


def _hertz(value: float | None) -> str:
    """A drive frequency in whole hertz.

    Not `%g`, which renders an RF head's 1 MHz as `1e+06`: these numbers are read
    beside a front panel that shows them as integers, and a reader comparing the
    two should not have to translate.
    """
    if value is None:
        return "?"
    return f"{value:.0f}"


def _fixed(value: float | None) -> str:
    """Two decimal places, which is what every one of these the box prints has."""
    if value is None:
        return "?"
    return f"{value:.2f}"


def _volts(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:.2f} V"


def declared_settings(commands: Iterable[str]) -> dict[str, dict[int, str]]:
    """Which indexed settings a sequence of strings sets, keyed by the getter that
    reads each one back.

    `SWFDIR,2,FWD` is read back by `GWFDIR,2` and `SDCB,3,-12.50` by the third field
    of `GDCBALL`, so a host that wants to say which of a box's settings its method
    named -- and which it left as it found -- needs the same mapping twice: once to
    build the warning list (`clockwork.acq.loop.left_as_found`) and once to mark a
    row of a state table. It is one mapping and it lives here, beside the getters it
    names.

    The rule is the firmware's own naming and nothing cleverer: a command whose word
    starts with `S` and whose first argument is a number sets the thing `G` + the rest
    of the word reads at that index. Returns `{getter: {index: value}}`, the value
    being the rest of the string with nothing parsed out of it, since what a value
    means is the caller's business and `FWD` and `-12.50` are both just what was
    written.

    A command with no numeric first argument -- `SDCBALL`, `SMOD,TBL`, a table load --
    is not an indexed setting and is left out. Comments are the caller's to filter:
    what counts as one is a method-document fact and not a wire fact.
    """
    settings: dict[str, dict[int, str]] = {}
    for command in commands:
        head, _, rest = command.partition(",")
        head = head.strip().upper()
        target, _, value = rest.partition(",")
        target = target.strip()
        if not head.startswith("S") or not target.isdigit():
            continue
        settings.setdefault("G" + head[1:], {})[int(target)] = value.strip()
    return settings


def describe(states: Sequence[BoxState]) -> str:
    """Several boxes' states as one block, in the order given."""
    return "\n".join(state.render() for state in states)
