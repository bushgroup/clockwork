"""Which COM ports have a MIPS box behind them, asked rather than assumed.

A window opens with no idea what is plugged into the machine, and the method's port map
is a hint and not an answer: a trainee edits a document written on another instrument,
boxes move between ports across a reboot, and a port survives its box being switched
off. So the window asks.

**Port presence is not evidence of a powered box.** All four MIPS-class ports on the
instrument PC enumerate and report `Status OK` with every box powered off, because
Windows keeps a CDC port for a device it has seen before. A port map is confirmed by
`GNAME` or not at all (lab record, task 37), which is why this module opens each
candidate and asks it rather than reading the enumeration and stopping there.

**Opening and closing a port resets the box behind it.** Dropping DTR makes the
firmware run `SerialPortReset()` and the device re-enumerates, and that happens on
*any* close, so a scan that opened every port and closed it again would reset every box
in the rack every time a trainee pressed the button. `discover` therefore hands back
the ports it opened still open, wrapped as `Box` objects the caller owns: a box that
answered is kept, and only a port that did not answer is closed. A caller that wants
the identities and nothing else passes `keep=False` and accepts the cost.

The two classes of candidate are separated because they fail differently. A
**MIPS-class** port is one whose USB identity says so -- vendor 0x2341 (the Due's
Arduino vendor id) or an `iProduct` of `MIPS` -- and a scan of those is cheap and
certain. Any *other* serial port may still be a box behind a hub or an adapter that
reports nothing useful, so `mips_ports(strict=False)` includes them, at the cost of
opening ports that belong to something else entirely; nothing here writes to a port
before it has answered `GNAME`, so the cost is a timeout and not a disturbance.

    from clockwork.mips import discover

    found = discover()
    for box in found.boxes.values():        # keyed by GNAME
        print(box.name, box.transport.port_name)
    for entry in found.silent:
        print(entry.text)

Filled by the lab record's task 50.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from .box import DEFAULT_TIMEOUT_S, Box, MipsError

__all__ = [
    "DISCOVERY_TIMEOUT_S",
    "MIPS_PRODUCT",
    "MIPS_VENDOR_ID",
    "REOPEN_ATTEMPTS",
    "REOPEN_DELAY_S",
    "Discovery",
    "Found",
    "PortInfo",
    "Silent",
    "discover",
    "mips_ports",
]

MIPS_VENDOR_ID = 0x2341
"""The USB vendor id a MIPS box enumerates under: Arduino's, the Due's.

Read off the instrument PC, where the box on COM4 came up as `USB Serial Device
(COM4)` on `USB\\VID_2341&PID_003E`, bound to Microsoft's inbuilt `usbser.inf` (lab
record, task 37). It identifies the microcontroller and not the instrument, so a
different Arduino on the same machine is MIPS-class by this test and is separated from
a real box by `GNAME` below, which is the only thing that ever settles it.
"""

MIPS_PRODUCT = "MIPS"
"""The USB `iProduct` string the firmware sets, matched case-insensitively as a
substring of the port's description. The surer of the two tests where the OS surfaces
it; Windows often reports the generic `USB Serial Device` instead, which is why the
vendor id is checked as well."""

DISCOVERY_TIMEOUT_S = 2.0
"""How long one port is given to answer `GNAME`.

A `GVER` round trip is 2.0 ms in steady state and about 16 ms on the first command
after a port opens, so two seconds is three orders of magnitude of headroom and is
sized for the one case that needs it: a box that has just been dropped out of table
mode answers its next string after about 1.1 s (lab record, task 37). Below that a
scan run straight after an acquisition would report a live box as absent.
"""

REOPEN_ATTEMPTS = 5
"""How many times a port that refuses to open is retried before it counts as
could-not-open.

Windows does not always make a USB CDC port reusable the instant its handle is
closed: a scan that follows one that closed every box can meet `CreateFile`
failing on a device node mid-teardown, and the very next scan succeeds because
the teardown finished meanwhile. That is what a trainee pressing `Find boxes`
twice in a row was actually working around (lab record, task 62). Retrying
inside one scan costs a first scan nothing -- every port that is going to open
does so on the first attempt -- and spares a genuinely absent port only a few
hundred milliseconds.
"""

REOPEN_DELAY_S = 0.05
"""The pause between reopen attempts. `REOPEN_ATTEMPTS` of these is a few
hundred milliseconds, chosen with no measurement of how long a Windows CDC
teardown actually takes -- the lab record carries what the bench settles it
to."""


@dataclass(frozen=True, slots=True)
class PortInfo:
    """One serial port as the operating system describes it.

    `mips_class` is the USB identity test and nothing more -- it says a port is worth
    asking, never that a box is there.
    """

    port: str
    description: str = ""
    hwid: str = ""
    vendor_id: int | None = None
    product_id: int | None = None
    mips_class: bool = False

    @property
    def text(self) -> str:
        """The one line a window's port list shows."""
        return f"{self.port}  {self.description}".rstrip()


@dataclass(frozen=True, slots=True)
class Found:
    """A box that answered, and the port it answered on."""

    name: str
    """What the box calls itself (`GNAME`), or `""` for a box that has never been
    given one. This is the key a method's box names are matched against: the
    firmware name is the one link between a file and the hardware that wrote it
    (lab record, task 13)."""

    port: str
    version: str = ""
    """`GVER`, the firmware revision, for the pane's header."""

    box: Box | None = None
    """The open `Box`, or None where `discover(keep=False)` closed it again."""


@dataclass(frozen=True, slots=True)
class Silent:
    """One candidate port that did not become a box, and why.

    `could_not_open` is the fact `discover`'s old, single-word "silent" used to
    flatten away: a port that refused to open -- retried `REOPEN_ATTEMPTS` times
    and still no `CreateFile` -- has nothing to do with a box at all and cannot be
    called "off or absent", while a port that opened and stayed quiet through
    `GNAME` is exactly that. A window says something different for each (lab
    record, task 62).
    """

    port: str
    could_not_open: bool
    why: str
    """The exception's own message, or `"no reply"` for a port that opened and
    stayed quiet."""

    @property
    def text(self) -> str:
        """One line a trainee can act on; the exception's own text stays the
        parenthetical, not the whole line."""
        if self.could_not_open:
            return f"{self.port}: could not open the port ({self.why})"
        return f"{self.port}: opened but nothing answered -- off or absent ({self.why})"


@dataclass(frozen=True, slots=True)
class Discovery:
    """What one scan found, in the three shapes a window needs.

    Built so that a window can render the whole result without a second pass: the
    boxes to drive, the ports that answered nothing, and the ports that were never
    candidates in the first place.
    """

    found: tuple[Found, ...] = ()
    silent: tuple[Silent, ...] = ()
    """Every candidate that did not become a box. See `Silent`."""

    skipped: tuple[PortInfo, ...] = ()
    """Ports present on the machine that were not asked, because their USB identity is
    not MIPS-class. Kept so that a window can say "and eleven other ports" rather than
    leaving a trainee to wonder whether the scan saw them."""

    seconds: float = 0.0
    """What the scan cost, for the status line. A silent candidate is the whole of
    it: an answering box costs milliseconds and a dead port costs the timeout."""

    _boxes: dict[str, Box] = field(default_factory=dict, compare=False, repr=False)

    @property
    def boxes(self) -> dict[str, Box]:
        """The open boxes, keyed by `GNAME`, ready to hand to `send_phases`.

        A box that answered no name at all, and a second box answering a name
        already taken, are both in `found` and neither is here: `send_phases` is
        keyed by name, so a duplicate would silently drive one box twice, and a
        box with no name cannot be addressed by a method at all. `unusable` names
        them.
        """
        return dict(self._boxes)

    @property
    def unusable(self) -> tuple[tuple[str, str], ...]:
        """`(port, why)` for every box found that a method cannot address.

        Two boxes answering one `GNAME` is a firmware misconfiguration and not
        something to route around -- a method cannot say which of them it meant --
        so both are reported and neither is in `boxes`. A box that answers no name
        is the same problem in a different shape. `SNAME` on the box is the remedy
        for both, and the window says so rather than picking one.
        """
        counts: dict[str, int] = {}
        for entry in self.found:
            counts[entry.name] = counts.get(entry.name, 0) + 1
        rows: list[tuple[str, str]] = []
        for entry in self.found:
            if not entry.name:
                rows.append((entry.port,
                             "it answered GNAME with no name; give it one with SNAME"))
            elif counts[entry.name] > 1:
                rows.append((entry.port,
                             f"a second box also calls itself {entry.name!r}, so a "
                             "method cannot say which of them it means"))
        return tuple(rows)

    def close(self) -> None:
        """Close every box this scan opened. For a caller that kept none of them."""
        for entry in self.found:
            if entry.box is not None:
                entry.box.close()

    @property
    def text(self) -> str:
        """One line summarising the scan, for a status bar."""
        parts = [f"{len(self.found)} box(es)"]
        unopened = sum(1 for entry in self.silent if entry.could_not_open)
        quiet = len(self.silent) - unopened
        if unopened:
            parts.append(f"{unopened} port(s) could not open")
        if quiet:
            parts.append(f"{quiet} port(s) silent")
        if self.skipped:
            parts.append(f"{len(self.skipped)} other port(s)")
        return ", ".join(parts) + f" in {self.seconds:.1f} s"


def mips_ports(strict: bool = True) -> tuple[PortInfo, ...]:
    """Every serial port on this machine, MIPS-class first.

    `strict` returns only the MIPS-class ones, which is what a scan asks. False
    returns every port, with `mips_class` set on each, which is what a settings
    dialog offering a manual port shows.

    pyserial is imported here rather than at module scope for the same reason
    `transport.py` defers it: a clone with no serial support still imports
    `clockwork.mips`, and nothing that does not scan pays for the import. A machine
    where the enumeration itself fails comes back empty rather than raising, because
    an empty list and a failed list lead a window to the same place -- ask the
    trainee for a port -- and a traceback on launch does not.
    """
    try:
        from serial.tools import list_ports
    except ImportError:
        return ()
    try:
        entries = list(list_ports.comports())
    except Exception:  # noqa: BLE001 -- an enumeration that fails is "no ports"
        return ()
    ports: list[PortInfo] = []
    for entry in entries:
        description = (entry.description or "").strip()
        product = (getattr(entry, "product", None) or "").strip()
        vendor = getattr(entry, "vid", None)
        mips_class = vendor == MIPS_VENDOR_ID or any(
            MIPS_PRODUCT in text.upper() for text in (description, product)
        )
        ports.append(PortInfo(
            port=entry.device,
            description=description or product,
            hwid=(entry.hwid or "").strip(),
            vendor_id=vendor,
            product_id=getattr(entry, "pid", None),
            mips_class=mips_class,
        ))
    ports.sort(key=lambda info: (not info.mips_class, info.port))
    return tuple(info for info in ports if info.mips_class) if strict else tuple(ports)


def discover(
    ports: Sequence[str] | Iterable[PortInfo] | None = None,
    *,
    strict: bool = True,
    keep: bool = True,
    timeout: float = DISCOVERY_TIMEOUT_S,
    opener: Callable[..., Box] = Box.open,
    clock: Callable[[], float] = time.perf_counter,
    reopen_attempts: int = REOPEN_ATTEMPTS,
    reopen_delay: float = REOPEN_DELAY_S,
    sleep: Callable[[float], None] = time.sleep,
) -> Discovery:
    """Ask every candidate port what box is behind it.

    `ports` is the list to ask: port names, `PortInfo`s, or None to scan this machine
    (`mips_ports(strict)`). Each is opened, asked `GNAME` and `GVER`, and kept open if
    it answered -- see the module docstring for why closing it would be the wrong
    thing to do to a box.

    Nothing here writes a setting, changes a mode or sends anything but two getters,
    so a scan on a machine whose boxes are mid-experiment costs each of them two
    round trips and disturbs none of them. It is still the wrong moment to run one:
    a box in table mode is busy, and the window runs a scan on launch and on demand
    and never during a run.

    **A port that refuses to open is retried before it counts as silent.** A close
    right before this call -- which is every rescan, since a caller closes its old
    boxes first -- can leave a Windows CDC port mid-teardown, and opening it again
    within milliseconds fails with an `OSError` that has nothing to do with a box
    being off (lab record, task 62). `reopen_attempts` and `reopen_delay` are that
    retry's shape; a port that is going to open at all almost always does so on the
    first or second attempt, so a normal scan pays nothing and only a port that is
    genuinely gone pays the last one's timeout in addition to the retries.

    `opener` is the injection point the tests drive: anything with `Box.open`'s
    signature, so a stand-in rack is the same code path as a real one. `sleep` is
    the same for the retry pause.

    Raises nothing. Every failure is a row in `silent` with the reason it gave,
    because the one thing a discovery must not do is refuse to report the boxes it
    did find because of a port it could not open.
    """
    started = clock()
    candidates: list[PortInfo] = []
    if ports is None:
        candidates = list(mips_ports(strict=strict))
        skipped = tuple(info for info in mips_ports(strict=False) if not info.mips_class)
    else:
        candidates = [
            PortInfo(port=item, mips_class=True) if isinstance(item, str) else item
            for item in ports
        ]
        skipped = ()

    found: list[Found] = []
    silent: list[Silent] = []
    for info in candidates:
        box, open_error = _open_with_retries(
            opener, info.port, min(timeout, DEFAULT_TIMEOUT_S),
            attempts=reopen_attempts, delay=reopen_delay, sleep=sleep)
        if open_error is not None:
            # A port that never opened cannot be "off or absent" -- that word is
            # for a box that had its chance to answer and did not. See `Silent`.
            silent.append(Silent(info.port, could_not_open=True,
                                 why=str(open_error) or "no reply"))
            continue
        assert box is not None
        try:
            with box.summarised():
                name = box.box_name().strip()
                version = box.version().strip()
        except (MipsError, OSError, ValueError) as exc:
            # Every reason a box does not answer lands here and is a row, not a
            # raise: the box is off, another process holds the port, or what
            # answered is not a box at all. Which of those it was is the
            # exception's to say and not this function's to guess.
            _shut(box)
            silent.append(Silent(info.port, could_not_open=False,
                                 why=str(exc) or "no reply"))
            continue
        box.name = name or info.port
        found.append(Found(name=name, port=info.port, version=version,
                           box=box if keep else None))
        if not keep:
            _shut(box)

    boxes: dict[str, Box] = {}
    counts: dict[str, int] = {}
    for entry in found:
        counts[entry.name] = counts.get(entry.name, 0) + 1
    for entry in found:
        if entry.box is not None and entry.name and counts[entry.name] == 1:
            boxes[entry.name] = entry.box
    return Discovery(found=tuple(found), silent=tuple(silent), skipped=skipped,
                     seconds=clock() - started, _boxes=boxes)


def _open_with_retries(
    opener: Callable[..., Box],
    port: str,
    timeout: float,
    *,
    attempts: int,
    delay: float,
    sleep: Callable[[float], None],
) -> tuple[Box | None, Exception | None]:
    """Open `port`, retrying only the failure a mid-teardown CDC device gives.

    A `MipsError` or `ValueError` out of `opener` means something about the
    call itself was wrong -- a bad argument, not a device that will come back
    -- so those are not retried. Only `OSError`, what pyserial raises when
    `CreateFile` itself fails, gets another try.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return opener(port, timeout=timeout), None
        except OSError as exc:
            last = exc
            if attempt + 1 < attempts:
                sleep(delay)
        except (MipsError, ValueError) as exc:
            return None, exc
    return None, last


def _shut(box: Box) -> None:
    """Close a box, swallowing what closing a port that is already gone raises."""
    try:
        box.close()
    except Exception:  # noqa: BLE001 -- a close that fails has nothing left to fail
        pass
