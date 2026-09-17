"""Where the bytes go: a real COM port, or a box simulated in this process.

`Box` talks to a `Transport` and nothing else, so the same sender drives a MIPS
controller on a COM port and the `FakeBox` below. The fake exists so that the
framing, the chunked table load and the `TBLRPT` round trip can be exercised in
`tools/check_public.py` and the tests on a machine with no instrument attached,
which is every machine except the one in the lab.

What the fake is and is not. It reproduces the *byte-level* conventions of §1
faithfully: the LF-CR after a set-style ACK, the bare ACK before a printed
value, the `\\x15?` of a NAK, the doubled newline after an asynchronous status
line. It does not reproduce timing, and it answers `TBLRPT` by re-encoding the
same prediction `table.compile_table` makes, so a round trip against the fake
proves the framing and the plumbing and proves nothing at all about whether
that prediction matches a real box's parser.

One consequence of having no clock is worth stating, because the bench has now
measured the thing it cannot model. On a real box the limit on a table load is
a *rate*: written with no pause between chunks, a string much past 4 KB loses
its tail, and written with a 10 ms pause, 17572 bytes arrive intact (§1). The
fake sees only sizes, so it drops the tail of any single write larger than the
whole ring buffer and lets everything else through. A sender that paces
correctly and one that does not both pass here. Only a bench box separates
them.

The ARB command set of §6 is here on the same terms: a box given a module
count stores what those commands set and reads it back, and models nothing a
module does with the value. A box given no modules NAKs all of them with the
firmware's own error 115. That is enough to send a real method's setup block
somewhere other than the instrument, and enough to rehearse a readback script
against; it is not enough to tell anyone what a module holds at power-up,
which is a question only a box can answer.

pyserial is imported inside `open_serial` rather than at module scope, so
importing `clockwork.mips` costs nothing and needs nothing on a machine that
will never open a port.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from . import table as _table
from .wire import DEFAULT_BAUDRATE, RING_BUFFER_BYTES, error_text


@runtime_checkable
class Transport(Protocol):
    """The whole surface `Box` needs from a link to one controller."""

    def write(self, data: bytes) -> None:
        """Send `data`, blocking until it has left the host."""

    def read_some(self, timeout: float) -> bytes:
        """Whatever arrives within `timeout`; empty if nothing does."""

    def close(self) -> None:
        """Release the link."""


class SerialTransport:
    """A MIPS box on a COM port.

    The baud rate is nominal: the boxes are used over the Due's native USB
    port, which is CDC, so the rate is not a real line rate (`wire.py`).
    """

    __slots__ = ("_baudrate", "_port", "port_name")

    def __init__(self, port: str, *, baudrate: int = DEFAULT_BAUDRATE) -> None:
        import serial  # deferred: see the module docstring

        self.port_name = port
        self._baudrate = baudrate
        self._port = serial.Serial(port, baudrate, timeout=0.05)

    def write(self, data: bytes) -> None:
        self._port.write(data)
        self._port.flush()

    def read_some(self, timeout: float) -> bytes:
        self._port.timeout = timeout
        first = self._port.read(1)
        if not first:
            return b""
        waiting = self._port.in_waiting
        return first + (self._port.read(waiting) if waiting else b"")

    def close(self) -> None:
        self._port.close()

    def reset_link(self, settle: float = 3.0) -> None:
        """Make the box reset its USB port, the only way back from a wedge.

        A table written faster than the box can tokenize does not just fail:
        it can leave the serial interface silent indefinitely, with no NAK and
        no output (§1). Dropping DTR after it has been asserted makes the
        firmware reset its own USB port, so closing and reopening the port
        recovers the box. The device re-enumerates, which takes it off the bus
        for a moment, so reopening retries until it is back.

        Nothing calls this automatically. A sender that paces its writes never
        needs it, and a caller that has just wedged a box is better placed than
        this module to decide whether resetting is the right answer.
        """
        import serial  # deferred: see the module docstring

        self._port.close()
        deadline = time.monotonic() + settle + 10.0
        last: Exception | None = None
        time.sleep(min(settle, 1.0))
        while time.monotonic() < deadline:
            try:
                self._port = serial.Serial(self.port_name, self._baudrate, timeout=0.05)
                return
            except Exception as exc:  # pyserial raises several unrelated types
                last = exc
                time.sleep(0.5)
        raise OSError(f"{self.port_name} did not come back after a link reset") from last


def open_serial(port: str, *, baudrate: int = DEFAULT_BAUDRATE) -> SerialTransport:
    """Open a COM port. Raises whatever pyserial raises if there is nothing there."""
    return SerialTransport(port, baudrate=baudrate)


# --------------------------------------------------------------------------
# The simulated box
# --------------------------------------------------------------------------

_ACK = b"\x06\n\r"
_ACK_ONLY = b"\x06"
_NAK = b"\x15?\n\r"

# --------------------------------------------------------------------------
# The ARB command surface of §6, as far as a stand-in can carry it
# --------------------------------------------------------------------------
#
# Every command below stores what it is given and hands it back, and models
# nothing a module does with it. That is worth having anyway: the instrument's
# ARB boxes take a block of these once (the lab record's instrument map) and
# read them back afterwards, so a script that sends a block and compares the
# readback has something to run against before it meets a box.
#
# The split between the two tables is the document's, not a convenience. §6
# gives a `G` counterpart for the rows below and gives none for
# `_ARB_SET_ONLY`, so those are settable here and unreadable, exactly as they
# are on a box. Which of them a real firmware will in fact answer is one of
# the things the lab record's task 10 goes to find out, and a fake that
# answered them all would teach a probe the wrong lesson.

_ARB_GETTABLE: dict[str, str] = {
    "SARBMODE": "GARBMODE",
    "SWFREQ": "GWFREQ",
    "SWFVRNG": "GWFVRNG",
    "SWFVOFF": "GWFVOFF",
    "SWFVAUX": "GWFVAUX",
    "SWFDIR": "GWFDIR",
    "SWFTYP": "GWFTYP",
    "SWFVRAMP": "GWFVRAMP",
    "SARBOFFA": "GARBOFFA",
    "SARBOFFB": "GARBOFFB",
    "SARBPPP": "GARBPPP",
    "SARBADD": "GARBADD",
    "SALTENA": "GALTENA",
    "SALTTRG": "GALTTRG",
    "SALTHWD": "GALTHWD",
    "SALTTMODE": "GALTTMODE",
    "SALTWFM": "GALTWFM",
    "SALTDLY": "GALTDLY",
    "SALTPLY": "GALTPLY",
    "SALTRENA": "GALTRENA",
    "SALTRNG": "GALTRNG",
    "SALTFENA": "GALTFENA",
    "SALTFRQ": "GALTFRQ",
}
"""Per-module `S…`/`G…` pairs (§6.2, §6.4). First argument is the module."""

_ARB_SET_ONLY: tuple[str, ...] = (
    "SARBCCLK",
    "SARBEXT",
    "SARBSYNLN",
    "SARBCMPLN",
    "SARBCPEX",
    "SARBHISR",
    "SARBDBRD",
    "SWFENA",
    "SWFDIS",
)
"""Per-module commands §6 documents with no getter at all.

`SARBCCLK` is the one that matters most: it selects which module the common
clock freezes, so which region a compression table's `s` stops, and the only
record of how a box is set is the string that set it."""

_ARB_DEFAULTS: dict[str, str] = {
    "SARBMODE": "TWAVE",
    "SWFREQ": "0",
    "SWFVRNG": "0",
    "SWFVOFF": "0",
    "SWFVAUX": "0",
    "SWFDIR": "FWD",
    "SWFTYP": "SIN",
    "SWFVRAMP": "0",
    "SARBOFFA": "0",
    "SARBOFFB": "0",
    "SARBPPP": "32",
    "SARBADD": "0",
    "SALTENA": "FALSE",
    "SALTTRG": "NA",
    "SALTHWD": "FALSE",
    "SALTTMODE": "LEVEL",
    "SALTWFM": "COMP",
    "SALTDLY": "0",
    "SALTPLY": "0",
    "SALTRENA": "FALSE",
    "SALTRNG": "0",
    "SALTFENA": "FALSE",
    "SALTFRQ": "0",
}
"""What a module here holds before anything sets it.

§6 states two of these: `SARBPPP` is 32 by default and `SALTWFM`'s `COMP` is
marked the default type. The rest are a stand-in so that a readback has
something to return, and carry no authority whatever. What a real module holds
at power-up is exactly what the lab record's task 10 goes to the box to read."""

_COMPRESSOR_GETTABLE: dict[str, str] = {
    "SARBCTBL": "GARBCTBL",
    "SARBCMODE": "GARBCMODE",
    "SARBCORDER": "GARBCORDER",
    "SARBCTD": "GARBCTD",
    "SARBCTC": "GARBCTC",
    "SARBCTN": "GARBCTN",
    "SARBCTNC": "GARBCTNC",
    "SARBCSW": "GARBCSW",
    "SARBCDIS": "GARBCDIS",
}
"""Box-wide compressor state (§6.6). One state machine per box, not per module."""

_COMPRESSOR_SET_ONLY: tuple[str, ...] = ("SARBCMP", "SARBCOFF", "SARBALTTS", "SARBDISCI")

_DIO_OUTPUTS = "ABCDEFGHIJKLMNOP"
"""The digital outputs `SDIO` can drive. `Q`-`X` are inputs and alias onto `I`-`P`."""

_RF_GETTABLE: dict[str, str] = {
    "SRFFRQ": "GRFFRQ",
    "SRFDRV": "GRFDRV",
    "SRFVLT": "GRFVLT",
    "SRFMODE": "GRFMODE",
    "SRFPL": "GRFPL",
}
"""Per-channel RF `S…`/`G…` pairs (§8.3). First argument is the channel."""

_RF_READINGS: tuple[str, ...] = ("GRFPPVP", "GRFPPVN", "GRFPWR")
"""Per-channel measurements, with no setter: the two peaks and the head power."""

_RF_DEFAULTS: dict[str, str] = {
    "SRFFRQ": "1000000",
    "SRFDRV": "0.00",
    "SRFVLT": "0.00",
    "SRFMODE": "MANUAL",
    "SRFPL": "50",
    "GRFPPVP": "0.00",
    "GRFPPVN": "0.00",
    "GRFPWR": "0.00",
}
"""What a channel holds before anything sets it.

`MANUAL` rather than `AUTO` because that is what both of AUKLET's heads read on
2026-09-15, and what a head set by hand at the front panel looks like. The three
measurements are stand-ins on the same footing as `_ARB_DEFAULTS`: a stand-in
cannot model an RF head, and a test that needs a peak reading writes one in.
"""

_RF_GET_TO_SET: dict[str, str] = {get: put for put, get in _RF_GETTABLE.items()}

_COMPRESSOR_DEFAULTS: dict[str, str] = {
    "SARBCTBL": "",
    "SARBCMODE": "Normal",
    "SARBCORDER": "0",
    "SARBCTD": "0",
    "SARBCTC": "0",
    "SARBCTN": "0",
    "SARBCTNC": "0",
    "SARBCSW": "Open",
    "SARBCDIS": "FALSE",
}
"""Stand-ins on the same footing as `_ARB_DEFAULTS`, and with the same caveat."""

_ARB_NO_ARGUMENT: tuple[str, ...] = ("ARBSYNC", "TARBTRG")

_ARB_GET_TO_SET: dict[str, str] = {get: put for put, get in _ARB_GETTABLE.items()}
_COMPRESSOR_GET_TO_SET: dict[str, str] = {
    get: put for put, get in _COMPRESSOR_GETTABLE.items()
}


class FakeBox:
    """A MIPS controller simulated well enough to develop a sender against.

    Commands are processed synchronously inside `write`, so a status line the
    real box would emit some milliseconds later is already waiting by the time
    `read_some` is called. Everything a caller can observe about *ordering* is
    right; nothing about timing is.
    """

    def __init__(
        self,
        *,
        name: str = "SLIMbox",
        version: str = "1.263, June 20, 2026",
        trigger: str = "EDGE",
        ring_buffer_bytes: int = RING_BUFFER_BYTES,
        strict: bool = True,
        arb_modules: int = 0,
        arb_version: str = "2.21",
        do_channels: int = 16,
        dcb_channels: int = 16,
        rf_channels: int = 0,
    ) -> None:
        self.name = name
        self.version = version
        self.trigger = trigger
        self.ring_buffer_bytes = ring_buffer_bytes
        self.strict = strict
        """Whether a command this stand-in does not implement is rejected.

        A real box NAKs what its firmware does not have, and so does this by
        default, which is what makes a typo in a method show up here. Set it
        false to acknowledge anything instead. That is a blunt instrument and
        the last resort: a method whose boxes carry ARB modules wants
        `arb_modules` rather than this, because section 6 is modelled below and
        an acknowledgement from that path at least stores what it was given.
        What this is for is a method carrying a command no version of this
        stand-in has heard of, where the alternative is not being able to send
        it anywhere but the instrument. An acknowledgement then says exactly
        what it says -- the string was well formed and went out -- and nothing
        about what a box would have done with it.
        """

        self.clocked = True
        """Whether this box's table clock input has a clock on it.

        True is the ordinary box. False is the one failure this stand-in models from
        the outside in: with nothing on Q the table engine executes tick 0 at `TBLSTRT`
        and stops there, so the box prints `TBLTRIG` and never `TBLCMPLT`, never
        re-arms, and leaves `GTBLSTA` reading `TRIGGERED` and whatever tick 0 drove
        still driven. That is not a hypothesis: it is what AUKLET did across 442
        `TBLSTRT` from 2026-09-15 to the night of 2026-09-16 with its level converter
        unplugged, and every frame of it counted out, folded and verified as though
        nothing were wrong (lab record, tasks 42 and 46).

        A knob rather than the default, and added only once the cause was known --
        task 44 declined it while it was still a guess about what a stalled table
        looks like. What it is for is the third part of the enable-gate guard, which
        exists to catch exactly this and can be rehearsed no other way: nothing here
        runs a table, so a stand-in's output pins never move and the gate cannot be
        modelled from the card's side.
        """

        self.arb_modules = arb_modules
        """How many ARB modules this box answers for, and `GCHAN,ARB`.

        Zero by default, which is the box the bench run of the lab record's
        task 04 met: every §6 command then NAKs error 115, *no ARB module in
        system*, which is the firmware's own code for it and not a guess. Give
        it a count to stand in for one of the instrument's ARB boxes.

        Whether `GCHAN,ARB` counts modules or dual-output boards is itself
        open, and this reports whatever it was given either way."""

        self.arb_version = arb_version
        """What every module answers to `GARBVER`. A knob, not a claim: §6.4
        gates the alternate waveform on >= 2.1 and its `CUR` type on >= 2.21,
        and the default here clears both so that a rehearsal exercises the
        path a capable module takes."""

        self.do_channels = do_channels
        self.dcb_channels = dcb_channels
        """`GCHAN,DO` and `GCHAN,DCB`; 16 each on the bench box (task 04)."""

        self.rf_channels = rf_channels
        """`GCHAN,RF`, and how many channels `GRFALL` reports.

        Zero by default: most boxes on this instrument carry no RF driver board,
        and a channel past the count is refused with error 2, which is how
        AUKLET's two heads were told from the four the firmware allows for (§8.1).
        """

        self.dc_bias: list[float] = [0.0] * dcb_channels
        """Every DC bias channel's setpoint, index 0 being channel 1 (§8.2)."""

        self.dc_bias_error: float = 0.0
        """How far the monitor reading sits from the setpoint, on every channel.

        Zero by default, which is a stand-in and not a claim: a real board reads
        back within about 0.25 V of its setpoint and never exactly on it. A test
        that cares about the difference between `GDCBALL` and `GDCBALLV` sets
        this; everything else is better off with two numbers that agree.
        """

        self.dc_bias_monitor: list[float] = [0.0] * dcb_channels
        """What `GDCBV` and `GDCBALLV` answer, which is not read on demand.

        The real box's monitors are an array a 100 ms service task maintains,
        and those two commands print it without converting anything. The task
        does not run in table mode, so from `SMOD,TBL` until the box is local
        again the array is frozen where it was (§8.2). `_service` below is that
        task, and this is that array.

        Frozen here at the last value the task reached, where a real box freezes
        part way through its filter's approach to it: modelling a one-pole
        filter would put a number in this stand-in that no test could check
        against anything. What both have in common is the fact worth modelling,
        which is that a monitor read with the box armed is not the output.
        """

        self.rf: dict[int, dict[str, str]] = {
            channel: dict(_RF_DEFAULTS) for channel in range(1, rf_channels + 1)
        }
        """Per-channel RF driver state, by 1-based channel and set-command stem."""

        self.arb: dict[int, dict[str, str]] = {
            module: dict(_ARB_DEFAULTS) for module in range(1, arb_modules + 1)
        }
        """Per-module settings, by module index and set-command stem."""

        self.compressor: dict[str, str] = dict(_COMPRESSOR_DEFAULTS)
        """Box-wide compressor state (§6.6), including the compression table."""

        self.compressor_triggers = 0
        """How many times `TARBTRG` has been sent. The state machine it starts
        is not modelled at all, so this is a count of asks, not of passes."""

        self.mode = "LOC"
        self.status = "IDLE"
        self.passes_left = 0
        """Passes still to run before the box drops back to local, 0 for forever.

        `TableN` in `ProcessTables()`, set by `SMOD`: 0 for `TBL`, 1 for `ONCE`,
        n for `SMOD,<n>`. Decremented by the outer loop, which is the `SW` path
        (§1), so a count only bites under a software trigger.
        """

        self.error = 0
        self.dio_image: dict[str, bool] = dict.fromkeys(_DIO_OUTPUTS, False)
        """What the box believes its digital outputs are, and what `GDIO` answers."""

        self.dio_pins: dict[str, bool] = dict.fromkeys(_DIO_OUTPUTS, False)
        """What a scope on those outputs would see.

        The two differ whenever an `SDIO` arrives in table mode, where the image
        changes and the latch that would apply it belongs to the table timer
        (§4), and from `SMOD,TBL` onwards, where arming stages the table's first
        time point into the image with the same latch pending (`_arm`). Nothing
        here runs a table, so a staged write stays staged; what this models is
        that the host cannot see the difference."""

        self.table_buffer = 1
        """`STBLNUM`/`GTBLNUM`, the active table buffer. Stored, never acted on:
        this stand-in holds one table."""

        self.ext_freq = 0
        """The declared external clock frequency, 0 until `SEXTFREQ` says otherwise."""
        self.replies_enabled = True
        self.loaded: _table.Compiled | None = None
        self.dropped_bytes = 0
        """Characters lost to ring-buffer overflow, which a real box would lose
        silently and which nothing on the wire would report."""
        self.written: list[bytes] = []
        """Every chunk the sender wrote, so a test can see how it paced itself."""
        self._in = bytearray()
        self._out = bytearray()
        self._closed = False

    # -- Transport ---------------------------------------------------------

    def write(self, data: bytes) -> None:
        if self._closed:
            raise ValueError("write to a closed FakeBox")
        self.written.append(bytes(data))
        if len(data) > self.ring_buffer_bytes:
            # §1: `PutCh()` throws away `RB_Put()`'s full-buffer return, so an
            # overflow is invisible to the host. On a real box the loss depends
            # on how far the sender outruns the parser, which nothing here can
            # model; the stand-in is that a single write larger than the whole
            # buffer certainly loses its tail. It makes a sender that does not
            # chunk a long table fail here rather than only in the lab.
            self.dropped_bytes += len(data) - self.ring_buffer_bytes
            data = data[: self.ring_buffer_bytes]
        self._in += data
        self._drain()

    def read_some(self, timeout: float) -> bytes:
        if self._out:
            out = bytes(self._out)
            self._out.clear()
            return out
        # Nothing to say. Sleep a little so a caller waiting for a status line
        # that will never come does not spin a core.
        time.sleep(min(max(timeout, 0.0), 0.01))
        return b""

    def close(self) -> None:
        self._closed = True

    # -- the box -----------------------------------------------------------

    def _emit(self, data: bytes) -> None:
        self._out += data

    def _status_line(self, text: str) -> None:
        """An asynchronous status line, doubled newline and all (§1)."""
        if self.replies_enabled:
            self._emit(text.encode("ascii") + b"\n\r\n")

    def _ack(self) -> None:
        self._emit(_ACK)

    def _value(self, text: str) -> None:
        self._emit(_ACK_ONLY + text.encode("ascii") + b"\r\n")

    def _nak(self, code: int) -> None:
        self.error = code
        self._emit(_NAK)

    def _service(self) -> None:
        """One pass of the box's 100 mS service loop, which runs only in LOC.

        Called before each command rather than on a clock: nothing in this
        stand-in models time, and the fact worth modelling is not the 100 mS.
        It is that the loop stops dead in table mode, so a monitor read with the
        box armed answers whatever the last pass in local mode left there,
        however long ago that was (§8.2). The lag between an `SDCB` and the
        output it asks for is the part left out, and a test that needs it writes
        `dc_bias_monitor` itself.
        """
        if self.mode != "LOC":
            return
        self.dc_bias_monitor = [volts + self.dc_bias_error for volts in self.dc_bias]

    def _drain(self) -> None:
        """Take whole commands off the input buffer and answer them."""
        while True:
            self._service()
            data = bytes(self._in)
            # A table load is terminated by its second semicolon, everything
            # else by a newline. Find whichever ends the command in hand.
            if data[:8].upper().startswith(b"STBLDAT;"):
                end = data.find(b";", 8)
                if end < 0:
                    return
                command, rest = data[: end + 1], data[end + 1 :]
                del self._in[:]
                self._in += rest.lstrip(b"\r\n")
                self._table_load(command.decode("ascii", "replace"))
                continue
            cut = data.find(b"\n")
            if cut < 0:
                return
            command, rest = data[:cut], data[cut + 1 :]
            del self._in[:]
            self._in += rest
            text = command.decode("ascii", "replace").strip()
            if text:
                self._command(text)

    def _table_load(self, command: str) -> None:
        if self.mode == "TBL" and self.status != "READY":
            self._nak(27)  # not in LOC mode
            return
        try:
            self.loaded = _table.compile_table(command)
        except _table.TableSyntaxError as exc:
            self.loaded = None
            self._nak(exc.error_code or 2)
            return
        self._ack()

    def _command(self, text: str) -> None:
        name, _, argument = text.partition(",")
        name = name.strip().upper()
        argument = argument.strip()
        handler = getattr(self, "_do_" + name.lower(), None)
        if handler is None:
            if self._arb(name, argument) or self._rf(name, argument):
                return
            if self.strict:
                self._nak(1)  # invalid command
            else:
                self._ack()
            return
        handler(argument)

    # Each of these is one row of §4's command table.

    def _do_gver(self, _: str) -> None:
        self._value(self.version)

    def _do_gname(self, _: str) -> None:
        self._value(self.name)

    def _do_gerr(self, _: str) -> None:
        self._value(str(self.error))

    def _do_gtblsta(self, _: str) -> None:
        self._value(self.status)

    def _do_gtblfrq(self, _: str) -> None:
        self._value("42000000")

    def _do_gchan(self, argument: str) -> None:
        """`GCHAN,<DO|DCB|ARB>`: how much of each kind this box is fitted with.

        The cheapest thing a host can ask a box it has never met, and the one
        that says whether a method written for an ARB box can run on it at all.
        """
        counts = {
            "DO": self.do_channels,
            "DCB": self.dcb_channels,
            "ARB": self.arb_modules,
            "RF": self.rf_channels,
        }
        found = counts.get(argument.upper())
        if found is None:
            self._nak(22)  # invalid channel request
            return
        self._value(str(found))

    # -- section 8: DC bias and RF channel state ---------------------------

    def _dc_channel(self, text: str) -> int | None:
        """A 1-based DC bias channel, or a NAK for one this box does not hold."""
        try:
            channel = int(text.strip())
        except ValueError:
            self._nak(2)
            return None
        if not 1 <= channel <= self.dcb_channels:
            self._nak(2)  # invalid argument, as a real board answers (section 8.1)
            return None
        return channel

    def _do_sdcb(self, argument: str) -> None:
        """`SDCB,<ch>,<volts>`: store a setpoint, in any mode (§8.2).

        In any mode deliberately. This is the one command that does **not**
        share `SDIO`'s latch problem: the DC bias service loop writes the DAC
        with the AD5668's update-on-write command rather than waiting for the
        `LDAC` the table timer owns, so a host may set a channel while a table
        runs. The delay between the ACK and the voltage is not modelled here,
        and on a real box it is one pass of that service loop.
        """
        index, _, value = argument.partition(",")
        channel = self._dc_channel(index)
        if channel is None:
            return
        try:
            volts = float(value.strip())
        except ValueError:
            self._nak(2)
            return
        self.dc_bias[channel - 1] = volts
        self._ack()

    def _do_gdcb(self, argument: str) -> None:
        channel = self._dc_channel(argument)
        if channel is not None:
            self._value(f"{self.dc_bias[channel - 1]:.2f}")

    def _do_gdcbv(self, argument: str) -> None:
        """`GDCBV`: the monitor reading, which is not the setpoint (§8.2)."""
        channel = self._dc_channel(argument)
        if channel is not None:
            self._value(f"{self.dc_bias_monitor[channel - 1]:.2f}")

    def _do_gdcball(self, _: str) -> None:
        self._value(",".join(f"{volts:.2f}" for volts in self.dc_bias))

    def _do_gdcballv(self, _: str) -> None:
        self._value(",".join(f"{volts:.2f}" for volts in self.dc_bias_monitor))

    def _do_sdcball(self, argument: str) -> None:
        """`SDCBALL`: the whole bank in one command, all or nothing (§8.2)."""
        fields = [field.strip() for field in argument.split(",") if field.strip()]
        if len(fields) != self.dcb_channels:
            self._nak(2)
            return
        try:
            volts = [float(field) for field in fields]
        except ValueError:
            self._nak(2)
            return
        self.dc_bias = volts
        self._ack()

    def _rf_channel(self, argument: str) -> tuple[int | None, str]:
        """Split `<ch>[,<value>]`, rejecting a channel this box does not hold.

        Error 2 rather than a board-missing code, because that is what AUKLET
        answered for `GRF*,3` and `GRF*,4` on a box with two heads (§8.1).
        """
        index, _, value = argument.partition(",")
        try:
            channel = int(index.strip())
        except ValueError:
            self._nak(2)
            return None, ""
        if not 1 <= channel <= self.rf_channels:
            self._nak(2)
            return None, ""
        return channel, value.strip()

    def _do_grfall(self, _: str) -> None:
        """`GRFALL`: four fields per channel, not the three its help text says.

        Frequency, drive level, positive peak, negative peak, every channel on
        one line (§8.3). A box with no RF board answers an empty line rather
        than rejecting, which is what the firmware's loop does when the first
        channel is invalid.
        """
        fields: list[str] = []
        for channel in range(1, self.rf_channels + 1):
            state = self.rf[channel]
            fields += [state["SRFFRQ"], state["SRFDRV"],
                       state["GRFPPVP"], state["GRFPPVN"]]
        self._value(",".join(fields))

    def _rf(self, name: str, argument: str) -> bool:
        """Handle one §8.3 command, or say it is none of them."""
        if name in _RF_GETTABLE:
            channel, value = self._rf_channel(argument)
            if channel is not None:
                self.rf[channel][name] = value
                self._ack()
            return True
        if name in _RF_GET_TO_SET:
            channel, _ = self._rf_channel(argument)
            if channel is not None:
                self._value(self.rf[channel][_RF_GET_TO_SET[name]])
            return True
        if name in _RF_READINGS:
            channel, _ = self._rf_channel(argument)
            if channel is not None:
                self._value(self.rf[channel][name])
            return True
        return False

    def _do_gcmds(self, _: str) -> None:
        """`GCMDS`: every command this stand-in answers, one per line.

        Unframed, after a bare ACK, terminated `\\r\\r\\n` a line, which is how
        the real listing arrives (§8.4). It is generated from the dispatch rather
        than written out, so a stand-in that gains a command gains it here too
        and a readback filtering on this listing cannot drift from what the
        stand-in will actually answer.
        """
        names = {name[4:].upper() for name in dir(self)
                 if name.startswith("_do_")}
        # `GARBVER` and `STBLDAT` are answered off the dispatch rather than from a
        # `_do_` method, so neither is found by the sweep above and both would look
        # absent to a caller filtering on this listing.
        names.update({"GARBVER", "STBLDAT", "TBLRPT"})
        names.update(_ARB_SET_ONLY)
        names.update(_ARB_NO_ARGUMENT)
        names.update(_ARB_GETTABLE)
        names.update(_ARB_GETTABLE.values())
        names.update(_COMPRESSOR_GETTABLE)
        names.update(_COMPRESSOR_GETTABLE.values())
        names.update(_COMPRESSOR_SET_ONLY)
        names.update(_RF_GETTABLE)
        names.update(_RF_GETTABLE.values())
        names.update(_RF_READINGS)
        self._emit(_ACK_ONLY + "".join(f"{name}\r\r\n" for name in sorted(names))
                   .encode("ascii"))

    def _do_stblnum(self, argument: str) -> None:
        try:
            buffer = int(argument)
        except ValueError:
            self._nak(2)
            return
        if not 1 <= buffer <= 5:
            self._nak(2)
            return
        self.table_buffer = buffer
        self._ack()

    def _do_gtblnum(self, _: str) -> None:
        self._value(str(self.table_buffer))

    def _do_sextfreq(self, argument: str) -> None:
        """§4: declare the external clock's frequency. Stored, never executed on.

        The firmware backs this with a plain integer variable, so it takes any
        int and range-checks nothing; it changes what `TBLCHK` and the idle-task
        mode can work out and changes nothing about how a table runs. A host
        that sets it and a host that does not both get the same sequence.
        """
        try:
            self.ext_freq = int(argument)
        except ValueError:
            self._nak(2)
            return
        self._ack()

    def _do_gextfreq(self, _: str) -> None:
        self._value(str(self.ext_freq))

    def _do_sdio(self, argument: str) -> None:
        """`SDIO`, with the three ways it surprises a host, all modelled (§4).

        Accepted in any mode and applied to the image in any mode, but the pins
        only follow in LOC: in table mode the latch belongs to the table timer,
        so the write is staged and the line does not move. `dio_pins` is what a
        scope would see and `dio_image` is what the box believes.
        """
        channel, _, value = argument.partition(",")
        channel, value = channel.strip().upper(), value.strip()
        if len(channel) != 1 or not ("A" <= channel <= "X") or value not in ("0", "1"):
            self._nak(2)
            return
        self._ack()
        # The aliasing the firmware does and does not report: Q-X wrap onto
        # I-P, so a host that sends one corrupts an output it did not name.
        written = _DIO_OUTPUTS[(ord(channel) - ord("A")) % 8 + (
            8 if channel >= "I" else 0)]
        self.dio_image[written] = value == "1"
        if self.mode == "LOC":
            self.dio_pins[written] = self.dio_image[written]

    def _do_gdio(self, argument: str) -> None:
        """`GDIO`: an output from the image, an input from the hardware.

        Reading back an output therefore cannot tell a latched write from a
        pending one, which is the reason a host cannot confirm `SDIO` worked.
        """
        channel = argument.strip().upper()
        if len(channel) != 1 or not ("A" <= channel <= "X"):
            self._nak(2)
            return
        if channel >= "Q":
            self._value("0")  # no digital input is driven on this stand-in
            return
        self._value("1" if self.dio_image.get(channel) else "0")

    def _do_stblclk(self, argument: str) -> None:
        if self.mode != "LOC":
            self._nak(27)
        elif argument.upper() not in ("EXT", "EXTN", "EXTS", "42000000", "10500000",
                                      "2625000", "656250"):
            self._nak(2)
        else:
            self._ack()

    def _do_stbltrg(self, argument: str) -> None:
        if self.mode != "LOC":
            self._nak(27)
        elif argument.upper() not in ("SW", "POS", "NEG", "EDGE"):
            self._nak(2)
        else:
            self.trigger = argument.upper()
            self._ack()

    def _do_stblreply(self, argument: str) -> None:
        if argument.upper() not in ("TRUE", "FALSE"):
            self._nak(2)
            return
        self.replies_enabled = argument.upper() == "TRUE"
        self._ack()

    def _do_smod(self, argument: str) -> None:
        mode = argument.upper()
        if mode == "LOC":
            if self.mode == "LOC":
                self._nak(3)
                return
            self.mode, self.status = "LOC", "IDLE"
            self._ack()
            return
        if mode not in ("TBL", "ONCE") and not mode.isdigit():
            self._nak(2)
            return
        if self.mode == "TBL":
            self._nak(4)
            return
        if self.loaded is None:
            self._nak(5)
            return
        # `TableN`: forever for `TBL`, one pass for `ONCE`, n for a bare count.
        self.passes_left = 0 if mode == "TBL" else (1 if mode == "ONCE" else int(mode))
        self.mode, self.status = "TBL", "READY"
        self._ack()
        self._arm()

    def _arm(self) -> None:
        """`SetupTimer()`: stage the table's first time point, then say `TBLRDY`.

        Called by `SMOD` and again by every re-arm that goes round the outer
        loop, which is the `SW` path (§3 step 5). What is worth modelling is the
        staging: `SetupNextEntry()` writes the first time point's digital
        outputs into the image and leaves the latch pending, so from the
        `TBLRDY` onwards `GDIO` answers the value the table is about to drive
        while the pin still holds the old one (§3 step 2, §4).

        Measured on AUKLET at 1.211t: `GDIO,A` and `GDIO,B` both 0 with the
        table loaded and the box local, both 1 after `SMOD,TBL`, before any
        trigger (lab record, task 42).
        """
        tables = self.loaded.tables if self.loaded is not None else ()
        first = tables[0].points[0].entries if tables and tables[0].points else ()
        for entry in first:
            if ord("A") <= entry.chan <= ord("P") and entry.value is not None:
                self.dio_image[chr(entry.chan)] = entry.value == ord("1")
        self._status_line("TBLRDY")

    def _do_tblstrt(self, _: str) -> None:
        """Software trigger, then a whole pass, because the fake has no clock.

        The pass is the part this cannot model: nothing here runs a table, so
        `TBLCMPLT` follows `TBLTRIG` in the same handler. What it does model is
        the state the box is left in, which is what a sequencer depends on.

        **The box re-arms, under `SW` as much as under an edge** (§1). Both of
        `ProcessTables()`'s loops re-arm; only `SMOD,ONCE` or `SMOD,<n>` with
        its passes spent leaves table mode. So a caller that sends `TBLSTRT` per
        repetition gets what the instrument gives it: 100 consecutive `TBLSTRT`
        on one load, no `SMOD` round trip between them, firmware 1.211t on two
        separate days, and `TRIGGERED, COMPLETE, READY` twice on one load at
        1.163t (lab record, tasks 28 and 44).

        An earlier stand-in dropped to local here, which was this file having an
        opinion about a question the wire format left open, and it refused every
        `--fake` replicate with error 6 -- a failure no box produces.

        **A box with `clocked` false stops after the trigger**, because the table
        engine advances on the clock input and there is nothing on it: `TBLTRIG` and
        then silence, no re-arm, `TRIGGERED` until something ends table mode, and the
        next `TBLSTRT` accepted and answered exactly the same way. Whatever tick 0
        drove stays driven, which is how an enable that never comes down happens.
        """
        if self.mode != "TBL":
            self._nak(6)
            return
        self._ack()
        self.status = "TRIGGERED"
        self._status_line("TBLTRIG")
        if not self.clocked:
            return
        self._status_line("TBLCMPLT")
        if self.trigger in ("EDGE", "POS", "NEG"):
            # The inner loop re-arms in place: no timer setup, so no re-staging,
            # and the pass count is never reached and never spent.
            self.status = "READY"
            self._status_line("TBLRDY")
            return
        if self.passes_left:
            self.passes_left -= 1
            if self.passes_left == 0:
                # `SMOD,ONCE`, or a count that has run out: out of the outer
                # loop and back to local, the only way table mode ends by itself.
                self.mode, self.status = "LOC", "IDLE"
                return
        self.status = "READY"
        self._arm()

    def _do_tblstop(self, _: str) -> None:
        if self.mode != "TBL":
            self._nak(6)
            return
        self._ack()
        if self.replies_enabled:
            self._emit(b"Table stoped by user\r\n")

    def _do_tblabrt(self, _: str) -> None:
        if self.mode != "TBL":
            self._nak(6)
            return
        self._ack()
        self.mode, self.status = "LOC", "ABORTED"
        if self.replies_enabled:
            self._emit(b"ABORTED\n")

    def _do_tblrpt(self, argument: str) -> None:
        """No ACK, a five-line preamble, then one byte per line (§4)."""
        try:
            count = int(argument)
        except ValueError:
            self._nak(2)
            return
        if self.loaded is None:
            self._nak(5)
            return
        data = _table.encode(self.loaded)
        wanted = data[: count + 1]
        # A real box dumps whatever is in the buffer, including stale bytes
        # past the end marker; asking for more than was loaded gets padding.
        wanted += bytes(max(0, count + 1 - len(wanted)))
        lines = [
            f"TestNesting = {self.loaded.nesting}",
            f"TablesLoaded = {self.loaded.tables_loaded}",
            f"Size of TableHeader = {_table.TABLE_HEADER_BYTES}",
            f"Size of TableEntryHeader = {_table.TIME_POINT_BYTES}",
            f"Size of TableEntry = {_table.ENTRY_BYTES}",
        ]
        lines += [f"{byte:x}" for byte in wanted]
        self._emit(("\n".join(lines) + "\n").encode("ascii"))

    # -- the ARB modules (§6) ----------------------------------------------

    def _arb_present(self) -> bool:
        """Answer for a box with no ARB modules fitted, and say so on the wire."""
        if self.arb_modules:
            return True
        self._nak(115)  # no ARB module in system
        return False

    def _arb_module(self, argument: str) -> tuple[int | None, str]:
        """Split `<mod>[,<value>]`, rejecting a module this box does not hold.

        The error codes are the firmware's own for a board that is not there
        (14 and 15). Whether a real controller range-checks a module index at
        all is unverified, and a probe that asks for a module past the last one
        is how that gets settled.
        """
        index, _, value = argument.partition(",")
        try:
            module = int(index.strip())
        except ValueError:
            self._nak(2)
            return None, ""
        if module < 1:
            self._nak(14)
            return None, ""
        if module > self.arb_modules:
            self._nak(15)
            return None, ""
        return module, value.strip()

    def _arb(self, name: str, argument: str) -> bool:
        """Handle one §6 command, or say it is none of them.

        Returns whether the command was recognised, so an unrecognised one
        still falls through to `strict`'s NAK or acknowledgement.
        """
        if name in _ARB_NO_ARGUMENT:
            if self._arb_present():
                if name == "TARBTRG":
                    self.compressor_triggers += 1
                self._ack()
            return True

        if name in _COMPRESSOR_GETTABLE or name in _COMPRESSOR_SET_ONLY:
            if self._arb_present():
                if name in _COMPRESSOR_GETTABLE:
                    self.compressor[name] = argument
                self._ack()
            return True

        if name in _COMPRESSOR_GET_TO_SET:
            if self._arb_present():
                self._value(self.compressor[_COMPRESSOR_GET_TO_SET[name]])
            return True

        if name == "GARBVER":
            if self._arb_present():
                module, _ = self._arb_module(argument)
                if module is not None:
                    self._value(self.arb_version)
            return True

        if name in _ARB_GETTABLE or name in _ARB_SET_ONLY:
            if self._arb_present():
                module, value = self._arb_module(argument)
                if module is not None:
                    if name in _ARB_GETTABLE:
                        self.arb[module][name] = value
                    self._ack()
            return True

        if name in _ARB_GET_TO_SET:
            if self._arb_present():
                module, _ = self._arb_module(argument)
                if module is not None:
                    self._value(self.arb[module][_ARB_GET_TO_SET[name]])
            return True

        return False

    def describe_error(self) -> str:
        """The last error as words, for a test's failure message."""
        return error_text(self.error)
