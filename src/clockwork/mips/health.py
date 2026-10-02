"""Whether a box has kept working, as opposed to kept answering, read with getters.

A state readback (`clockwork.mips.state`) reports what a box *holds*: setpoints,
the table engine's state, the ARB modules' settings. A box whose controller has
reset on its own, or has been failing to reach its own DACs and ADCs over the
board's TWI bus, goes on reporting exactly the same setpoints. Twice on
2026-09-30 the ion signal fell while every setpoint read back right (lab
record, task 97), and nothing on record could say whether a box had reset or its
bus had failed.

So `read_health` asks the getters that report on the controller itself (§8.5):

- `UPTIME`, minutes since the box last started;
- `STATUS`, the last reset's cause and the TWI failure / reset count;
- `THREADS`, the scheduler's threads and whether each is enabled;
- `GDCPWR` and `GERR`, the DC bias supply and the last error code.

The first three answer a bare ACK and then unframed text, so they are read the
way `GCMDS` is, through `Box.read_unframed`. Everything is filtered on the box's
`GCMDS` listing as a state readback is (§8.4), the raw text is kept whatever it
says, and a field that does not parse is None rather than an error.

    from clockwork.mips import Box, read_health

    with Box.open("COM6", name="auklet") as box:
        print(read_health(box).render())

Nothing here is a setter, nothing changes the box's mode, and nothing is Qt.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .box import Box, MipsError
from .state import RESYNC_AFTER_NAK_S, _listing

__all__ = ["BoxHealth", "HEALTH_GETTERS", "HEALTH_REPORTS", "ThreadRow", "read_health"]

HEALTH_REPORTS: tuple[str, ...] = ("UPTIME", "STATUS", "THREADS")
"""The unframed reports, in the order they are asked (§8.5)."""

HEALTH_GETTERS: tuple[str, ...] = ("GDCPWR", "GERR")
"""The framed getters asked after them (§8.2, §1). `GERR` last, so it reports the
box's last error from before this reading, which asks only for what is listed."""

_NAK = "\x15"
_UPTIME = re.compile(r"up for at least:\s*([-+\d.]+)\s*minutes")
_RESET = re.compile(r"^\s*(?P<cause>[^,\r\n]+?),\s*(?P<millis>\d+)\s*$", re.M)
_TWI = re.compile(r"TWI failure / reset count:\s*(\d+)")
_THREAD = re.compile(
    r"^\s*(?P<name>[^,\r\n]+?),\s*(?P<id>-?\d+),\s*(?P<interval>-?\d+),\s*"
    r"(?P<enabled>Enabled|Disabled)\s*,\s*(?P<run>\d+)\s*$", re.M)


@dataclass(frozen=True, slots=True)
class ThreadRow:
    """One line of `THREADS` (§8.5)."""

    name: str
    ident: int
    interval_ms: int
    enabled: bool
    last_run_ms: int
    """How long the thread's most recent run took, not when it ran (§8.5)."""


@dataclass(frozen=True, slots=True)
class BoxHealth:
    """One box's controller health at one moment, as its getters answered.

    `reports` holds each unframed report's text and `values` each framed getter's
    answer, keyed by the command as sent and kept verbatim; the properties parse
    them and answer None for anything missing or unrecognised.
    """

    name: str
    reports: Mapping[str, str] = field(default_factory=dict)
    values: Mapping[str, str] = field(default_factory=dict)
    skipped: tuple[str, ...] = ()
    """Commands not sent because the box's `GCMDS` listing does not name them."""
    refused: Mapping[str, str] = field(default_factory=dict)
    """Commands the box rejected, and what was said."""
    listed: bool = True
    """Whether a `GCMDS` listing was available to filter on."""

    @property
    def uptime_min(self) -> float | None:
        """`UPTIME`, in minutes."""
        found = _UPTIME.search(self.reports.get("UPTIME", ""))
        try:
            return float(found.group(1)) if found else None
        except ValueError:
            return None

    @property
    def reset_cause(self) -> str | None:
        """`STATUS`'s reset cause, as the box words it."""
        found = _RESET.search(self.reports.get("STATUS", ""))
        return found.group("cause").strip() if found else None

    @property
    def status_millis(self) -> int | None:
        """`STATUS`'s `millis()` count at the moment it answered."""
        found = _RESET.search(self.reports.get("STATUS", ""))
        return int(found.group("millis")) if found else None

    @property
    def twi_fails(self) -> int | None:
        """`STATUS`'s TWI failure / reset count, None where the line is absent: an
        absent line is a firmware that does not print it, never a count of zero."""
        found = _TWI.search(self.reports.get("STATUS", ""))
        return int(found.group(1)) if found else None

    @property
    def threads(self) -> tuple[ThreadRow, ...]:
        """`THREADS`, one row per line that parses."""
        return tuple(
            ThreadRow(name=row["name"].strip(), ident=int(row["id"]),
                      interval_ms=int(row["interval"]),
                      enabled=row["enabled"] == "Enabled", last_run_ms=int(row["run"]))
            for row in _THREAD.finditer(self.reports.get("THREADS", "")))

    @property
    def dc_power(self) -> str | None:
        """`GDCPWR`, `ON` or `OFF`."""
        return self.values.get("GDCPWR")

    @property
    def last_error(self) -> int | None:
        """`GERR`, which never clears (§1)."""
        try:
            return int(self.values["GERR"])
        except (KeyError, ValueError):
            return None

    def render(self) -> str:
        """The reading as a block of lines, in the shape of `BoxState.render`."""
        lines = [f"{self.name}: health"]
        uptime = self.uptime_min
        if uptime is not None:
            lines.append(f"  uptime      {uptime:.1f} min ({uptime / 1440:.2f} days)")
        elif "UPTIME" in self.reports:
            lines.append(f"  uptime      {_one_line(self.reports['UPTIME'])}")
        cause = self.reset_cause
        if cause is not None:
            lines.append(f"  last reset  {cause}; {self.status_millis} ms since it, "
                         "as STATUS answered")
        elif "STATUS" in self.reports:
            lines.append(f"  status      {_one_line(self.reports['STATUS'])}")
        if "STATUS" in self.reports:
            twi = self.twi_fails
            lines.append(f"  TWI fails   {twi if twi is not None else '(not reported)'}")
        if self.dc_power is not None:
            lines.append(f"  DC power    {self.dc_power}")
        if "GERR" in self.values:
            lines.append(f"  last error  {self.values['GERR']}")
        rows = self.threads
        for row in rows:
            lines.append(f"  thread      {row.name} (ID {row.ident}), every {row.interval_ms} ms, "
                         f"{'enabled' if row.enabled else 'DISABLED'}, "
                         f"last run took {row.last_run_ms} ms")
        if "THREADS" in self.reports and not rows:
            lines.append(f"  threads     {_one_line(self.reports['THREADS'])}")
        if self.skipped:
            lines.append("  not asked   " + ", ".join(self.skipped)
                         + " (this firmware does not list them)")
        for command, why in self.refused.items():
            lines.append(f"  refused     {command}: {why}")
        if not self.listed:
            lines.append("  note        GCMDS would not answer, so every getter above "
                         "was sent without knowing the box has it")
        return "\n".join(lines)


def read_health(box: Box, *, listing: frozenset[str] | None = None) -> BoxHealth:
    """Read one box's controller health, getters only, nothing written (§8.5).

    `listing` is the box's `GCMDS` set, read here if not supplied, exactly as
    `read_state` takes it; a caller that has just taken a state readback passes
    the one it used. The box's mode is not touched, so a box read while armed is
    still armed afterwards.
    """
    listing = _listing(box, listing)
    reports: dict[str, str] = {}
    values: dict[str, str] = {}
    skipped: list[str] = []
    refused: dict[str, str] = {}
    with box.summarised():
        for command in HEALTH_REPORTS:
            if listing and command not in listing:
                skipped.append(command)
                continue
            text = box.read_unframed(command)
            body = text.lstrip("\x06")
            if body.startswith(_NAK) or not body.strip():
                refused[command] = "rejected" if body.startswith(_NAK) else "no answer"
                continue
            reports[command] = body.replace("\r\r\n", "\n").replace("\r\n", "\n").strip()
        for command in HEALTH_GETTERS:
            if listing and command not in listing:
                skipped.append(command)
                continue
            try:
                answer = box.command(command, value=True)
            except (MipsError, ValueError) as exc:
                box.resync(settle=RESYNC_AFTER_NAK_S)
                refused[command] = str(exc)
                continue
            if answer is not None:
                values[command] = answer
    return BoxHealth(name=box.name, reports=reports, values=values,
                     skipped=tuple(skipped), refused=refused, listed=bool(listing))


def _one_line(text: str) -> str:
    return " / ".join(part.strip() for part in text.splitlines() if part.strip())
