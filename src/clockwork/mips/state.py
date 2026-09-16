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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .box import Box, MipsError

__all__ = [
    "ARB_MODULE_GETTERS",
    "COMPRESSOR_GETTERS",
    "BoxState",
    "MAX_ARB_MODULES",
    "RESYNC_AFTER_NAK_S",
    "RfReading",
    "describe",
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

_IDENTITY_GETTERS: tuple[str, ...] = ("GVER", "GNAME")
_COUNT_GETTERS: tuple[str, ...] = ("GCHAN,DCB", "GCHAN,RF", "GCHAN,ARB")
_BANK_GETTERS: tuple[str, ...] = ("GDCBALL", "GDCBALLV")
_SEQUENCER_GETTERS: tuple[str, ...] = ("GTBLFRQ", "GTBLSTA")
_RF_CHANNEL_GETTERS: tuple[str, ...] = ("GRFMODE", "GRFPWR")


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
        lines = [f"{self.name}: {self.version}  {self.identity}".rstrip()]
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
    if listing is None:
        with box.summarised():
            listing = box.command_listing()
    values: dict[str, str] = {}
    skipped: list[str] = []
    refused: dict[str, str] = {}

    def ask(command: str) -> str | None:
        head = command.partition(",")[0]
        if listing and head not in listing:
            skipped.append(command)
            return None
        try:
            answer = box.command(command, value=True)
        except (MipsError, ValueError) as exc:
            # A rejection is a result: half of what a readback learns is which
            # getters a firmware has and which channels a board carries. The
            # drain is not optional -- without it the next getter reads this
            # one's leftover (section 8.4).
            box.resync(settle=RESYNC_AFTER_NAK_S)
            refused[command] = str(exc)
            return None
        if answer is not None:
            values[command] = answer
        return answer

    # Forty-odd round trips that say nothing one at a time. They stay in the wire
    # transcript and out of the send log, where the block this returns goes instead
    # (`Box.summarised`, lab record, task 40).
    with box.summarised():
        for getter in _IDENTITY_GETTERS + _COUNT_GETTERS:
            ask(getter)
        for getter in _SEQUENCER_GETTERS + _BANK_GETTERS:
            ask(getter)

        if ask("GRFALL") is not None:
            channels = _as_int(values.get("GCHAN,RF")) or 0
            for channel in range(1, channels + 1):
                for getter in _RF_CHANNEL_GETTERS:
                    ask(f"{getter},{channel}")

        if modules is None:
            modules = min(_as_int(values.get("GCHAN,ARB")) or 0, MAX_ARB_MODULES)
        if modules:
            for getter in COMPRESSOR_GETTERS:
                ask(getter)
        for module in range(1, modules + 1):
            for getter in ARB_MODULE_GETTERS:
                ask(f"{getter},{module}")

    return BoxState(
        name=box.name,
        values=values,
        skipped=tuple(skipped),
        refused=refused,
        listed=bool(listing),
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


def describe(states: Sequence[BoxState]) -> str:
    """Several boxes' states as one block, in the order given."""
    return "\n".join(state.render() for state in states)
