"""A box's last state reading as a table, with what the method declared beside it.

The Qt-free half of the state panel (`clockwork.app.statepanel`, lab record,
task 50, decision 6). `read_state` hands back what a box answered; this turns that into
rows a trainee reads down, and against every row that a method *could* have named it
puts one of three marks: the method declared this and the box agrees, the method
declared this and the box disagrees, or the method did not name it and the box is
holding whatever it was holding.

**Left as found is the mark that matters.** The instrument day of 2026-09-15 produced
two files nobody could tell apart because seven of the eight ARB modules were at
whatever the method before had left them at, and `SWFDIR` REV on two of them was
invisible to the method that ran next (lab record, task 40). The run log already says
*that* something was left as found; this says *what*, which is where a trainee looks
when the answer matters.

**Three things this module refuses to pretend.** A monitor read while the box is in
table mode is not the output and is not shown as a number (§8.2, task 43). `GTBLFRQ`
under an external clock is an uninitialised local and is not shown as a frequency (§4).
And a two-getter `read_sequencer` updates the sequencer rows and nothing else, so a
panel refreshed during a run says which of its rows are minutes old.

**A mark compares what the method asked for with what the box can give.** An ARB
module's waveform frequency comes off an integer divider, so `SWFREQ,n,15000` is
acknowledged and read back as 14914 (§6.2), and a panel that compared the two strings
marked **eight settings on each ARB box as disagreeing on every run** -- the standing
false alarm decision 6 was written to avoid, and a warning that fires every time is a
warning nobody reads. Four of the eight were the frequency and four were `SWFVRNG,n,15`
against a box that answers in volts to two places. Numbers are compared as numbers here
and the frequency against `arb_frequency`, so `DIFFERS` means a module is holding
something nobody asked it for (lab record, task 56).

Nothing here imports Qt, so `tools/check_public.py` exercises the whole judgement on a
clone with no display.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..acq.loop import (
    DC_BIAS_TOLERANCE_V,
    RF_DRIVE_TOLERANCE_PCT,
    RF_FREQUENCY_TOLERANCE,
)
from ..method import BoxMethod, declared_commands, is_comment
from ..mips import (
    ARB_MODULE_GETTERS,
    ARB_POINTS_PER_PERIOD,
    COMPRESSOR_GETTERS,
    BoxState,
    arb_frequency,
    arb_points_per_period,
    declared_settings,
)

__all__ = [
    "AGREES",
    "DIFFERS",
    "EXTERNAL_CLOCKS",
    "FOUND",
    "PLAIN",
    "Reading",
    "Row",
    "Section",
    "StateTable",
    "state_table",
]

PLAIN = ""
"""A row a method cannot declare: an identity, a channel count, a live measurement."""

AGREES = "as declared"
DIFFERS = "differs"
FOUND = "left as found"
"""The three marks a declarable row carries.

`DIFFERS` is the only one drawn in the warning colour. `FOUND` is not a fault -- both
golden methods leave most of a rack as found on purpose -- and a panel that coloured it
would be shouting through a whole acquisition day.
"""

EXTERNAL_CLOCKS = frozenset({"EXT", "EXTN", "EXTS"})
"""`STBLCLK` values under which `GTBLFRQ` reports nothing.

`TableFreq()` fills its answer only in the four internal-clock branches and prints an
uninitialised local otherwise -- 42000000 has been seen, and it measures nothing
(wire format §4). There is no getter for the clock *source*, so the only way to know
which of the two a number is, is the `STBLCLK` the method sent.
"""

_ARB_LABELS = {
    "GWFREQ": "frequency",
    "GWFVRNG": "range",
    "GWFDIR": "direction",
    "GARBMODE": "mode",
    "GALTWFM": "alt waveform",
    "GALTENA": "alt enabled",
    "GALTHWD": "alt hardware",
}
"""What each per-module getter is called in the table, since `GALTHWD` is not a word.

The getter itself stays in the row's `getter` field and in the tooltip: a trainee
reading the panel beside a MIPS_QT6 screenshot or the wire format needs the command
name, and a trainee reading it to see what changed needs the English.
"""

_COMPRESSOR_LABELS = {
    "GARBCTBL": "compression table",
    "GARBCTD": "trigger delay",
}


@dataclass(frozen=True, slots=True)
class Row:
    """One fact about a box: what it reads, what the method asked for, and the mark."""

    label: str
    value: str
    mark: str = PLAIN
    declared: str = ""
    """What the method declared, rendered the way the box renders its own answer, or
    empty where the method named nothing."""

    note: str = ""
    """The second sentence the row needs: a monitor reading, or why there is not one."""

    getter: str = ""
    """The command this row was read with, for the tooltip. Empty for a derived row."""


@dataclass(frozen=True, slots=True)
class Section:
    """One subsystem's rows, with a summary that survives the section being collapsed."""

    title: str
    rows: tuple[Row, ...] = ()
    note: str = ""

    @property
    def differing(self) -> int:
        return sum(1 for row in self.rows if row.mark == DIFFERS)

    @property
    def found(self) -> int:
        return sum(1 for row in self.rows if row.mark == FOUND)

    @property
    def summary(self) -> str:
        """What the section header says when the section is shut.

        The disagreements first and always, because a collapsed section that hid one
        would be the panel doing the thing it exists to stop.
        """
        parts = [f"{len(self.rows)} row{'s' if len(self.rows) != 1 else ''}"]
        if self.differing:
            parts.append(f"{self.differing} differ{'s' if self.differing == 1 else ''} "
                         "from the method")
        if self.found:
            parts.append(f"{self.found} left as found")
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class Reading:
    """What the panel holds for one box: a whole-state reading, and maybe a later
    sequencer-only one.

    Two fields rather than one because the two cost different things and say different
    amounts. `state` is `read_state`: forty round trips, every subsystem, and the only
    reading whose DC bias monitors mean anything, which is why the loop takes it between
    `setup` and `load` while the box is still local. `sequencer` is `read_sequencer`:
    two getters taken with the box armed, which says the table engine's state and
    nothing else. A panel that overwrote the first with the second would claim a whole
    instrument had been re-read when two commands had been sent (task 43, task 51).
    """

    state: BoxState | None = None
    when: str = ""
    sequencer: BoxState | None = None
    sequencer_when: str = ""
    converting: BoxState | None = None
    """The last reading whose DC bias monitors were actually converting, kept when a
    later one supersedes it.

    A reading taken with the box armed is a true reading whose monitors mean nothing:
    the 100 ms service task that maintains them does not run in table mode (§8.2, task
    43). So pressing Read state during a run replaced the between-`setup`-and-`load`
    reading -- the one taken while the box was still local, and the only one whose
    monitors were a measurement of anything -- with one where they are frozen. Every
    statement the panel then made was true and the evidence had gone. It is kept here
    instead: the rows carry the newest setpoints and the monitor beside each falls back,
    dated, to the last figures that were real (lab record, tasks 51 and 56)."""

    converting_when: str = ""

    def __bool__(self) -> bool:
        return self.state is not None or self.sequencer is not None

    def with_state(self, state: BoxState, when: str) -> Reading:
        """This box's panel after a whole-state reading, keeping what it supersedes.

        A reading replaces everything except the last converting monitors, which it
        replaces only by converting itself. The sequencer overlay goes, because a whole
        reading is a whole reading and the two getters it covers are part of it.
        """
        if state.monitors_converting:
            return Reading(state=state, when=when, converting=state,
                           converting_when=when)
        return Reading(state=state, when=when, converting=self.converting,
                       converting_when=self.converting_when)


@dataclass(frozen=True, slots=True)
class StateTable:
    """Everything the panel draws for one box."""

    caption: str
    sections: tuple[Section, ...] = ()
    empty: bool = False
    """True for a box nothing has been read off yet, where the caption is the whole of
    it and there is nothing to expand."""

    differing: tuple[Row, ...] = field(default_factory=tuple)
    """Every row whose reading disagrees with the method, across all sections, so a
    caller can open exactly the sections worth opening."""


def state_table(reading: Reading, box: BoxMethod | None = None) -> StateTable:
    """One box's reading as sections of rows, marked against what its method declares.

    `box` is the method's entry for this box, or None for a window whose panes do not
    name it -- a box found by `GNAME` that the open method says nothing about. With no
    method entry every declarable row is `FOUND`, which is the truth: nothing about to
    be sent names any of it.
    """
    if not reading:
        return StateTable(caption="not read yet", empty=True)
    declared = _declared(box)
    state = reading.state
    sections: list[Section] = []
    if state is not None:
        sections.append(_identity(state))
    sections.append(_sequencer(reading, box))
    if state is not None:
        sections += [
            _dc_bias(state, declared, reading),
            _rf(state, declared),
            _arb(state, declared),
        ]
        unread = _unread(state)
        if unread.rows:
            sections.append(unread)
    kept = tuple(section for section in sections if section.rows or section.note)
    return StateTable(
        caption=_caption(reading),
        sections=kept,
        differing=tuple(row for section in kept for row in section.rows
                        if row.mark == DIFFERS),
    )


def _caption(reading: Reading) -> str:
    """What was read, when, and how much of it -- in that order.

    The sentence a trainee needs before they believe a number on the panel. A sequencer
    reading names itself as two getters, because the rows above it are then as old as
    the send that took them and the panel must not look freshly refreshed.
    """
    parts: list[str] = []
    if reading.state is not None:
        parts.append(f"read {reading.when}" if reading.when else "read")
    if reading.sequencer is not None:
        when = f" {reading.sequencer_when}" if reading.sequencer_when else ""
        parts.append(f"table engine re-read{when} (two getters; "
                     "everything else is the reading above)"
                     if reading.state is not None else
                     f"table engine only{when} (two getters)")
    return "; ".join(parts)


def _declared(box: BoxMethod | None) -> dict[str, dict[int, str]]:
    """Every indexed setting the method would put on this box, keyed by its getter.

    The `setup` phase and the declarations it generates, which is exactly what
    `send_phases` sends and what `left_as_found` reads: a method that gains an `SWFDIR`
    line stops being marked as leaving it found, with nothing to edit here. The `load`
    and `arm` phases are left out on purpose -- they are the table and the mode change,
    and neither sets a setting a getter reads back.
    """
    if box is None:
        return {}
    return declared_settings(
        command for command in tuple(box.setup) + declared_commands(box)
        if not is_comment(command))


# -- the sections --------------------------------------------------------------------


def _identity(state: BoxState) -> Section:
    """What the box says it is. Nothing here is declarable and nothing is marked."""
    rows = []
    if state.identity:
        rows.append(Row("name", state.identity, getter="GNAME"))
    if state.version:
        rows.append(Row("firmware", state.version, getter="GVER"))
    for subsystem, label in (("DCB", "DC bias channels"), ("RF", "RF channels"),
                             ("ARB", "ARB modules")):
        count = state.count(subsystem)
        if count is not None:
            rows.append(Row(label, str(count), getter=f"GCHAN,{subsystem}"))
    return Section("Box", tuple(rows))


def _sequencer(reading: Reading, box: BoxMethod | None) -> Section:
    """The table engine: its status, and the clock frequency where there is one.

    Taken from the sequencer-only reading when there is one, since that is the later of
    the two and is the only part of the state a `read_sequencer` is entitled to update.
    """
    source = reading.sequencer if reading.sequencer is not None else reading.state
    if source is None:
        return Section("Table engine")
    rows = [Row("status", source.table_status or "did not answer", getter="GTBLSTA")]
    clock = _clock_source(box)
    frequency = source.values.get("GTBLFRQ", "")
    if clock in EXTERNAL_CLOCKS:
        rows.append(Row("clock", f"external, {clock}",
                        mark=AGREES, declared=clock, getter="STBLCLK"))
    elif frequency:
        mark, declared = PLAIN, ""
        if clock:
            declared = clock
            mark = AGREES if _same_number(frequency, clock, RF_FREQUENCY_TOLERANCE) \
                else DIFFERS
        rows.append(Row("clock", f"{frequency} Hz internal", mark=mark,
                        declared=f"{declared} Hz" if declared else "",
                        note="" if clock else
                             "the method sends no STBLCLK and no getter reports the "
                             "clock source, so this is the internal clock's frequency "
                             "whether or not the internal clock is the one running",
                        getter="GTBLFRQ"))
    note = ""
    if clock in EXTERNAL_CLOCKS:
        note = ("GTBLFRQ is not shown: under an external clock the firmware prints an "
                "uninitialised local, which measures nothing (§4).")
    return Section("Table engine", tuple(rows), note=note)


def _clock_source(box: BoxMethod | None) -> str:
    """The `STBLCLK` value the method's `setup` sends, upper-cased, or empty.

    The last one wins, as it does on the box. There is no `GTBLCLK`, so this string is
    the only thing that says which clock a reading's `GTBLFRQ` is about.
    """
    if box is None:
        return ""
    found = ""
    for command in box.setup:
        if is_comment(command):
            continue
        head, _, rest = command.partition(",")
        if head.strip().upper() == "STBLCLK":
            found = rest.strip().upper()
    return found


def _dc_bias(state: BoxState, declared: dict[str, dict[int, str]],
             reading: Reading) -> Section:
    """One row per channel: the setpoint, with the monitor as its note.

    The setpoint is what the box says it was told and is what a declaration is compared
    against; the monitor is a measurement of the output through the board's calibration
    and is a different number on a healthy channel. In table mode it is neither, and
    the note says so instead of showing it (§8.2, lab record, task 43).

    **A frozen reading falls back to the last live one rather than to nothing.** The
    setpoints are always this reading's -- they are what the box says it was told, and a
    box in table mode answers that truthfully -- and only the monitor beside each falls
    back, dated, to the last reading taken while they were converting
    (`Reading.converting`, task 56).
    """
    setpoints = state.dc_bias_setpoints
    if not setpoints:
        return Section("DC bias")
    wanted = declared.get("GDCB", {})
    converting = state.monitors_converting
    earlier = None if converting or reading.converting is state else reading.converting
    rows = []
    for index, volts in enumerate(setpoints):
        channel = index + 1
        asked = wanted.get(channel)
        mark, shown = FOUND, ""
        if asked is not None:
            shown = f"{asked} V"
            # Against the setpoint the box reports rather than the two decimals it is
            # rendered at, which is the number `declared_differences` compares.
            mark = AGREES if _same_number(volts, asked,
                                          absolute=DC_BIAS_TOLERANCE_V) else DIFFERS
        monitor = state.dc_bias_readback(channel)
        if converting:
            note = "" if monitor is None else f"monitors {monitor:.2f} V"
        else:
            note = _kept_monitor(earlier, channel, reading.converting_when)
        rows.append(Row(f"channel {channel}", _render_volts(volts) + " V",
                        mark=mark, declared=shown, note=note, getter="GDCBALL"))
    note = ""
    if not converting:
        note = (f"The monitors are frozen: the 100 ms service task that maintains them "
                f"does not run in table mode, and this box answered {state.table_status}. "
                "What they hold is wherever the box's filter had got to when it armed, "
                "which is neither the output nor the last true reading (§8.2).")
        if earlier is not None:
            note += (" The figures on the rows are the last reading taken while they "
                     f"were converting ({reading.converting_when}), kept because a "
                     "refresh with the box armed would otherwise replace the only "
                     "measurement of these outputs there is.")
    return Section("DC bias", tuple(rows), note=note)


def _kept_monitor(earlier: BoxState | None, channel: int, when: str) -> str:
    """The monitor note for a channel whose own reading is frozen."""
    if earlier is None:
        return "not converting in table mode"
    monitor = earlier.dc_bias_readback(channel)
    if monitor is None:
        return "not converting in table mode"
    return f"monitored {monitor:.2f} V when last live, {when}"


def _rf(state: BoxState, declared: dict[str, dict[int, str]]) -> Section:
    """Per RF head: the three settings a method may declare, then the live readings.

    Split a row per setting rather than one compound row per channel, because the marks
    are per setting -- a method that declares a drive level and leaves the frequency
    where the head was tuned is the ordinary case, and one row could not say both.
    """
    rows: list[Row] = []
    for reading in state.rf:
        channel = reading.channel
        head = f"RF {channel}"
        rows.append(_declarable(f"{head} frequency", _render_hertz(reading.frequency_hz),
                                declared.get("GRFFRQ", {}).get(channel),
                                unit=" Hz", relative=RF_FREQUENCY_TOLERANCE,
                                getter="GRFALL"))
        rows.append(_declarable(f"{head} drive", _render_fixed(reading.drive_pct),
                                declared.get("GRFDRV", {}).get(channel),
                                unit="%", absolute=RF_DRIVE_TOLERANCE_PCT,
                                getter="GRFALL"))
        if reading.mode:
            asked = declared.get("GRFMODE", {}).get(channel)
            mark = FOUND if asked is None else (
                AGREES if asked.upper() == reading.mode.upper() else DIFFERS)
            rows.append(Row(f"{head} mode", reading.mode, mark=mark,
                            declared=asked or "", getter=f"GRFMODE,{channel}"))
        rows.append(Row(
            f"{head} peaks",
            f"{_render_fixed(reading.peak_positive_v)} / "
            f"{_render_fixed(reading.peak_negative_v)} V",
            note="a live measurement of a resonant head, not a setting: it moves "
                 "between readings and no method declares it",
            getter="GRFALL"))
        if reading.power_w is not None:
            rows.append(Row(f"{head} power", f"{_render_fixed(reading.power_w)} W",
                            getter=f"GRFPWR,{channel}"))
    return Section("RF", tuple(rows))


def _arb(state: BoxState, declared: dict[str, dict[int, str]]) -> Section:
    """The compressor, then a row per module per setting.

    This is the section the task exists for: all seven of a module's getters, each
    marked, so that `SWFDIR` REV left behind by the method before is a row a trainee
    can point at rather than a difference between two files.
    """
    rows: list[Row] = []
    for getter in COMPRESSOR_GETTERS:
        if getter in state.values:
            rows.append(Row(_COMPRESSOR_LABELS.get(getter, getter[1:]),
                            state.values[getter] or "(empty)", getter=getter))
    for module in state.modules:
        answers = state.module(module)
        for getter in ARB_MODULE_GETTERS:
            if getter not in answers:
                continue
            asked = declared.get(getter, {}).get(module)
            if getter == "GWFREQ":
                rows.append(_frequency_row(module, answers, asked))
                continue
            mark = FOUND if asked is None else (
                # As numbers where both are numbers, falling back to text: `SWFVRNG,n,15`
                # is answered in volts to two places and FWD is answered FWD.
                AGREES if _same_number(answers[getter], asked) else DIFFERS)
            rows.append(Row(
                f"module {module} {_ARB_LABELS.get(getter, getter[1:])}",
                answers[getter], mark=mark, declared=asked or "",
                getter=f"{getter},{module}"))
    return Section("ARB", tuple(rows))


def _frequency_row(module: int, answers: dict[str, str], asked: str | None) -> Row:
    """One module's waveform frequency, marked against what its divider can produce.

    The divider is the whole of this: a request the module cannot hit is quantised down
    to the nearest step it can, so `GWFREQ` answering something other than `SWFREQ` sent
    it is the ordinary case and not a fault (§6.2, `clockwork.mips.arb_frequency`).

    Read at `ARB_POINTS_PER_PERIOD` first, which every module on this instrument is at.
    A module at some other points-per-period is not marked as disagreeing either: the
    reading is back-solved, and a points-per-period that explains it makes the row agree
    and says which one it must be at -- `SARBPPP` is not read, and a row that guessed
    wrong about it would be the false alarm back under another name.
    """
    label = f"module {module} {_ARB_LABELS['GWFREQ']}"
    reading = answers["GWFREQ"]
    if asked is None:
        return Row(label, reading, mark=FOUND, getter=f"GWFREQ,{module}")
    mode = answers.get("GARBMODE", "TWAVE")
    requested = _as_float(asked)
    achieved = None if requested is None else arb_frequency(requested, mode=mode)
    note, mark = "", DIFFERS
    if achieved is not None and _same_number(reading, str(achieved)):
        mark = AGREES
        if str(achieved) != asked.strip():
            note = (f"{asked} is not a step the divider can make; {achieved} Hz is the "
                    "nearest it can, and is what the module is running")
    elif requested is not None:
        found = _as_float(reading)
        period = None if found is None else arb_points_per_period(
            requested, found, mode=mode)
        if period is not None:
            mark = AGREES
            note = (f"{asked} quantises to {reading} Hz at {period} points per period, "
                    f"so this module is not at the {ARB_POINTS_PER_PERIOD} the rest of "
                    "the rack is")
    return Row(label, reading, mark=mark, declared=asked, note=note,
               getter=f"GWFREQ,{module}")


def _unread(state: BoxState) -> Section:
    """What the reading does not cover, and why -- never left to be inferred.

    A getter this firmware does not list and a getter the box refused are two different
    silences, and a panel that showed neither would let a row that is simply absent read
    as a setting that is not there.
    """
    rows: list[Row] = []
    for command in state.skipped:
        rows.append(Row(command, "not asked",
                        note="this firmware's GCMDS listing does not name it"))
    for command, why in state.refused.items():
        rows.append(Row(command, "refused", note=why))
    note = ""
    if not state.listed:
        note = ("GCMDS would not answer, so every getter above was sent without knowing "
                "the box has it.")
    return Section("Not read", tuple(rows), note=note)


# -- comparisons and rendering -------------------------------------------------------


def _declarable(label: str, value: str, asked: str | None, *, unit: str = "",
                absolute: float = 0.0, relative: float = 0.0,
                getter: str = "") -> Row:
    """One numeric row, marked against the method's own string for it."""
    if asked is None:
        return Row(label, value + unit, mark=FOUND, getter=getter)
    same = _same_number(value, asked, relative=relative, absolute=absolute)
    return Row(label, value + unit, mark=AGREES if same else DIFFERS,
               declared=asked + unit, getter=getter)


def _same_number(read: str | float | None, asked: str, relative: float = 0.0,
                 absolute: float = 0.0) -> bool:
    """Whether two of the box's own strings are the same number within tolerance.

    The tolerances are `clockwork.acq.loop`'s, imported rather than restated: the panel
    marks a row `DIFFERS` exactly where the run log would emit a declared-versus-read
    line about it, and two sets of numbers that could drift apart would be a window
    contradicting its own warnings (`test_app.py` pins the two together).

    Two strings that will not parse fall back to comparing them as text, which is what
    `MANUAL` against `MANUAL` needs and what a box answering something unexpected gets.
    """
    left = read if isinstance(read, float) else _as_float(read)
    right = _as_float(asked)
    if left is None or right is None:
        return _same_text(str(read), asked)
    allowed = max(absolute, abs(right) * relative)
    return abs(left - right) <= allowed


def _same_text(read: str, asked: str) -> bool:
    return read.strip().upper() == asked.strip().upper()


def _as_float(text: str | None) -> float | None:
    try:
        return float(text.strip())
    except (TypeError, ValueError):
        return None


def _render_volts(value: float | None) -> str:
    return "?" if value is None else f"{value:.2f}"


def _render_fixed(value: float | None) -> str:
    return "?" if value is None else f"{value:.2f}"


def _render_hertz(value: float | None) -> str:
    """Whole hertz, which is how a front panel and `SRFFRQ` both write a frequency.

    `%g` renders a head's 1 MHz as `1e+06`, and a trainee comparing the panel with the
    number in their method should not have to translate (`clockwork.mips.state`).
    """
    return "?" if value is None else f"{value:.0f}"
