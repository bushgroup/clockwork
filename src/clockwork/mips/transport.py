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
        self.error = 0
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

    def _drain(self) -> None:
        """Take whole commands off the input buffer and answer them."""
        while True:
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
            if self._arb(name, argument):
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
        }
        found = counts.get(argument.upper())
        if found is None:
            self._nak(22)  # invalid channel request
            return
        self._value(str(found))

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
        self.mode, self.status = "TBL", "READY"
        self._ack()
        self._status_line("TBLRDY")

    def _do_tblstrt(self, _: str) -> None:
        """Software trigger, then a whole pass, because the fake has no clock."""
        if self.mode != "TBL":
            self._nak(6)
            return
        self._ack()
        self.status = "TRIGGERED"
        self._status_line("TBLTRIG")
        self._status_line("TBLCMPLT")
        if self.trigger in ("EDGE", "POS", "NEG"):
            # §1: under an external trigger the box re-arms itself.
            self.status = "READY"
            self._status_line("TBLRDY")
        else:
            self.status = "IDLE"
            self.mode = "LOC"

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
