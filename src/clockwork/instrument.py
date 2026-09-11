"""The instrument's own settings: what a file records about the machine, not the experiment.

A method is a trainee's strings, and three things a UIMF file has to state are not in them.
The m/z **calibration** turns the bin axis into a mass axis. The **full scale** and the
**channel offset** say what window the digitizer acquired through. None of the three is a
property of the experiment: the same strings run after a recalibration, or through a
different attenuator, are the same method and a different file. So none of them is a method
field (lab record, task 06), and this is where they live instead.

The predecessor software keeps exactly this document and no other: one dated digitizer
properties file per chain configuration, carrying the calibration pair and the channel's
full scale and offset together, with the method nowhere near it. Clockwork keeps the same
document in the same flat TOML the method uses (lab record, task 25, decided with Matt):

    schema_version = 1

    [instrument]
    name = "SLIM3"
    description = "SLIMPHONY, 20 dB after the preamplifier"

    [calibration]
    slope = 0.738123
    intercept = 0.07690495
    measured = 2026-09-09

    [vertical]
    full_scale_v = 0.5
    offset_v = 0.251

Every table is optional and so is the document: `UNCALIBRATED` is what an acquisition uses
when nobody supplies one, and it writes the file this code wrote before this module existed,
`CalibrationDone = 0` and no vertical settings in the stamp. A file acquired that way is
calibratable afterwards from its own parameters, which is why nothing ever blocked on this.

**The calibration is stated against the file's own bin axis**, `mz = (slope * (t - intercept))^2`
with `t = bin * BinWidth_ns / 1000` in microseconds -- UIMF-Library's formula, implemented
once in `mainspring.uimf.Calibration` and not again here. Two consequences follow from `t`
being counted in bins from the start of the record rather than from the pusher pulse, and
both matter when a pair is carried from one chain to another:

- **The sample rate does not enter it.** `bin * BinWidth_ns` is a time, so doubling the rate
  doubles the bin index and halves the bin width, and the same ion lands at the same `t`. A
  pair measured at 1 GS/s is the same pair at 2 GS/s.
- **The post-trigger delay does.** `TimeOffset` is declared in the file and then not applied
  (`../mainspring-lab/notes/uimf-format.md`), so a delay changed by `d` microseconds moves
  every ion by `d` and `intercept` has to move by `-d` to put it back. A pair carried across a
  chain change is a starting value, not a calibration.

No Qt here, and nothing in this module talks to hardware or reads a UIMF file. It parses one
document and hands back what is in it.
"""

from __future__ import annotations

import datetime as _dt
import math
import tomllib
from dataclasses import dataclass, field

import tomli_w

SCHEMA_VERSION = 1

TENTHS_OF_NS = 1e4
"""Between the two forms of the same calibration, and the reason a pair can look wrong.

UIMF-Library carries the m/z formula twice, once in microseconds and once in tenths of a
nanosecond, with `K / 1e4` and `T0 * 1e4`; the two are identical. A digitizer properties
file states the pair in the second form and a UIMF file stores it in the first, so the lab's
calibration reads as `7.38123E-05 / 769.0495` in one place and `0.738123 / 0.07690495` in
the other. `Calibration.from_tenths_of_ns` is the conversion, verified against all four
golden files, whose stored pair it reproduces exactly (lab record, task 25).
"""


class InstrumentError(ValueError):
    """An instrument document failed to parse or validate.

    Carries every problem found, not just the first, in `.problems`.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True, slots=True)
class Calibration:
    """The m/z calibration a frame carries: `CalibrationSlope` and `CalibrationIntercept`.

    In microseconds, which is the form a UIMF file stores and the form
    `mainspring.uimf.Calibration` computes with. Zeros mean uncalibrated, which is written
    as `CalibrationDone = 0` and read by every tool as a file with a bin axis and no mass
    axis -- an honest statement, and the default.

    `measured` is the day the pair was determined, and is why this is a document rather than
    two numbers: a calibration that has no date cannot be told from one that was never
    checked. It is not written into the file, which has `DateStarted` for when the run
    happened; it is for whoever opens the document next.
    """

    slope: float = 0.0
    intercept: float = 0.0
    measured: _dt.date | None = None

    @property
    def usable(self) -> bool:
        """Whether this pair produces a mass axis, on `mainspring.uimf`'s own test.

        A zero or negative slope does not, and is the state a file created with no
        calibration is in.
        """
        return self.slope > 0.0

    @classmethod
    def from_tenths_of_ns(
        cls, slope: float, intercept: float, measured: _dt.date | None = None
    ) -> Calibration:
        """The same calibration read off a digitizer properties file, converted.

        `CalibrationA` and `CalibrationT0` there are the tenths-of-nanosecond form, so the
        slope multiplies by `TENTHS_OF_NS` and the intercept divides by it. Both fields are
        untyped and unlabelled in that document, so this conversion is the only thing that
        says which form a pair typed out of it is in.
        """
        return cls(slope=float(slope) * TENTHS_OF_NS,
                   intercept=float(intercept) / TENTHS_OF_NS,
                   measured=measured)


@dataclass(frozen=True, slots=True)
class Vertical:
    """The window channel 1 acquires through: full scale, and where the offset puts it.

    Both are `None` when unknown, and unknown is not a failure. The offset is clockwork's
    own `vertical` command and is always knowable; the full scale is a `config.txt` key the
    console reads at startup and does not report back, so what this holds is the value the
    lab configured rather than one the card confirmed (lab record, task 25, routed to
    task 24).

    Recorded at all because two files acquired through different ranges, or either side of
    an attenuator, are otherwise indistinguishable -- and the chain in front of this
    digitizer changed on the day it was cabled up.
    """

    full_scale_v: float | None = None
    offset_v: float | None = None

    @property
    def stated(self) -> bool:
        """Whether this says anything worth stamping into a file."""
        return self.full_scale_v is not None or self.offset_v is not None


@dataclass(frozen=True, slots=True)
class Instrument:
    """One instrument's settings, as loaded from its document.

    `name` is written into `Global_Params` as PNNL's own `InstrumentName`, which is what a
    file already had a place for and what every UIMF tool reads.
    """

    name: str = ""
    description: str = ""
    calibration: Calibration = field(default_factory=Calibration)
    vertical: Vertical = field(default_factory=Vertical)
    schema_version: int = SCHEMA_VERSION


UNCALIBRATED = Instrument()
"""What an acquisition given no document uses.

Not an error and not a placeholder to be filled in later: a public clone has no instrument,
a bench rig in front of a function generator has no mass axis to calibrate, and both write
files that say so. Everything this module adds is additive to the file clockwork wrote
before it existed.
"""


# --- parsing -------------------------------------------------------------------------


def _table(data: dict, key: str, problems: list[str]) -> dict:
    """One of the document's optional tables, or an empty one."""
    value = data.get(key, {})
    if not isinstance(value, dict):
        problems.append(f"{key}: expected a table")
        return {}
    return value


def _no_unknown_keys(
    table: dict, path: str, known: tuple[str, ...], problems: list[str]
) -> None:
    """Reject a key this schema does not define.

    The same rule the method document follows, for the same reason: a misspelled
    `full_scale` would otherwise be silently ignored and the file would claim a window
    nobody set.
    """
    for key in table:
        if key not in known:
            problems.append(f"{path}.{key}: not a key of schema {SCHEMA_VERSION}")


def _number(
    table: dict, key: str, path: str, problems: list[str], *, positive: bool = False
) -> float | None:
    """An optional finite number, absent as None."""
    if key not in table:
        return None
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        problems.append(f"{path}.{key}: expected a number")
        return None
    value = float(value)
    if not math.isfinite(value):
        problems.append(f"{path}.{key}: expected a finite number, not {value}")
        return None
    if positive and value <= 0:
        problems.append(f"{path}.{key}: expected a positive number, not {value}")
        return None
    return value


def _text(table: dict, key: str, path: str, problems: list[str]) -> str:
    value = table.get(key, "")
    if not isinstance(value, str):
        problems.append(f"{path}.{key}: expected a string")
        return ""
    return value


def from_dict(data: dict) -> Instrument:
    """Validate a parsed instrument document and build the `Instrument`.

    Every problem is collected before raising, so a document with three mistakes in it
    reports three. An empty document is valid and means `UNCALIBRATED`: the tables are the
    point of the file and a file that states none of them states none of them.
    """
    problems: list[str] = []
    if not isinstance(data, dict):
        raise InstrumentError(["document: expected a table"])

    version = data.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        problems.append(
            f"schema_version: {version!r} is not schema {SCHEMA_VERSION}, the only shape "
            "this version of clockwork reads"
        )
    _no_unknown_keys(data, "document",
                     ("schema_version", "instrument", "calibration", "vertical"), problems)

    identity = _table(data, "instrument", problems)
    _no_unknown_keys(identity, "instrument", ("name", "description"), problems)
    name = _text(identity, "name", "instrument", problems)
    description = _text(identity, "description", "instrument", problems)

    raw_calibration = _table(data, "calibration", problems)
    _no_unknown_keys(raw_calibration, "calibration",
                     ("slope", "intercept", "measured"), problems)
    slope = _number(raw_calibration, "slope", "calibration", problems)
    intercept = _number(raw_calibration, "intercept", "calibration", problems)
    if slope is not None and slope < 0:
        problems.append(
            f"calibration.slope: {slope} is negative; a calibration is unusable at zero "
            "or below and zero is how a document says it has none"
        )
    measured = raw_calibration.get("measured")
    if measured is not None and not isinstance(measured, _dt.date):
        problems.append("calibration.measured: expected a date, as 2026-09-09")
        measured = None

    raw_vertical = _table(data, "vertical", problems)
    _no_unknown_keys(raw_vertical, "vertical", ("full_scale_v", "offset_v"), problems)
    # Full scale is positive by definition; the offset is a position within it and is
    # negative as readily as positive. Neither is checked against the card's own list of
    # ranges here: what the SA220P accepts is a protocol fact and belongs in
    # `docs/console-protocol.md` before it belongs in code (lab record, task 24).
    full_scale = _number(raw_vertical, "full_scale_v", "vertical", problems, positive=True)
    offset = _number(raw_vertical, "offset_v", "vertical", problems)

    if problems:
        raise InstrumentError(problems)
    return Instrument(
        name=name,
        description=description,
        calibration=Calibration(slope=slope or 0.0, intercept=intercept or 0.0,
                                measured=measured),
        vertical=Vertical(full_scale_v=full_scale, offset_v=offset),
        schema_version=SCHEMA_VERSION,
    )


def to_dict(instrument: Instrument) -> dict:
    """The document an `Instrument` came from, or would have come from.

    Only what is set is written. A table whose every field is absent is left out, so a
    round trip through `dumps` and `loads` does not grow a `[vertical]` table full of
    nothing for a rig that has no vertical settings to state.
    """
    data: dict = {"schema_version": instrument.schema_version}
    identity = {}
    if instrument.name:
        identity["name"] = instrument.name
    if instrument.description:
        identity["description"] = instrument.description
    if identity:
        data["instrument"] = identity

    calibration = instrument.calibration
    if calibration.usable or calibration.measured is not None:
        table: dict = {"slope": calibration.slope, "intercept": calibration.intercept}
        if calibration.measured is not None:
            table["measured"] = calibration.measured
        data["calibration"] = table

    vertical = instrument.vertical
    if vertical.stated:
        table = {}
        if vertical.full_scale_v is not None:
            table["full_scale_v"] = vertical.full_scale_v
        if vertical.offset_v is not None:
            table["offset_v"] = vertical.offset_v
        data["vertical"] = table
    return data


def loads(text: str) -> Instrument:
    """Parse and validate an instrument document from TOML text."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InstrumentError([f"invalid TOML: {exc}"]) from exc
    return from_dict(data)


def load(path: str) -> Instrument:
    """Parse and validate an instrument document from a TOML file."""
    with open(path, "rb") as handle:
        try:
            data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise InstrumentError([f"invalid TOML: {exc}"]) from exc
    return from_dict(data)


def dumps(instrument: Instrument) -> str:
    """Serialize an instrument to TOML text."""
    return tomli_w.dumps(to_dict(instrument))


def save(instrument: Instrument, path: str) -> None:
    """Write an instrument document to a TOML file."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(dumps(instrument))


__all__ = [
    "SCHEMA_VERSION",
    "TENTHS_OF_NS",
    "UNCALIBRATED",
    "Calibration",
    "Instrument",
    "InstrumentError",
    "Vertical",
    "dumps",
    "from_dict",
    "load",
    "loads",
    "save",
    "to_dict",
]
