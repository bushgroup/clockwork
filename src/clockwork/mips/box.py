"""One MIPS controller box, as the rest of clockwork uses it.

Send a command and get its reply, load a pulse-sequence table without stalling,
arm the box, and follow the status lines it emits on its own. Every wire fact
is `docs/mips-wire-format.md`; this module implements that document and decides
nothing about the protocol itself.

Two properties are the whole point of the design:

*Asynchronous lines never get mistaken for a reply.* `TBLRDY`, `TBLTRIG`,
`TBLCMPLT`, `ABORTED` and the stop message arrive unprompted and can land in
the middle of a command's reply. Everything read here goes through one framer
per box, and a status line is routed to `events` wherever it turns up rather
than being read as the answer to whatever was asked.

*A table load cannot stall, and cannot outrun the box either.* The box
abandons a table string if any 3 seconds pass without a token, and drops
characters silently if the host outruns its 4 KB input buffer. There is no
flow control on the link to hold the host back: writing at full speed loses
the tail of any table much past 4 KB, which is a bench measurement and not a
worry (§1). `send_table` therefore writes in small chunks with a short pause
after each, and reports how long the write took so that the margin against the
timeout is a measurement rather than an assumption.

Blocking, and deliberately so: every read waits on a deadline. Nothing here may
be called from the UI thread.

*Everything that crosses the link is offered to a transcript.* `clockwork.mips.wire`
gets a record per write, a record per non-empty read, the `STBLDAT` chunk structure,
and each asynchronous status line as it is classified, so that a bench day leaves a
byte-level record of what a box actually said rather than only what a script chose
to compute from it (`clockwork.transcript`). With no transcript open the level check
fails and no record is built. The one place this is not done inline is the table
send; `send_table` says why. A caller that writes on `box.transport` itself is
transcribed on the read side and not on the write side, and should say what it sent
with `clockwork.transcript.note`.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

from ..transcript import render as _render
from . import table as _table
from .transport import Transport, open_serial
from .wire import (
    ERR_ALREADY_LOCAL,
    TOKEN_TIMEOUT_S,
    Kind,
    ResponseReader,
    TableEvent,
    Token,
    error_text,
    table_event,
)

_LOG = logging.getLogger("clockwork.mips.wire")
"""The transcript's name for this link. Documented in `clockwork.transcript`."""

DEFAULT_TIMEOUT_S = 2.0
"""Long enough for any command that is not a table load, on a USB link."""

DEFAULT_CHUNK_BYTES = 256
"""Bench-measured (lab record, task 04), and a whole number of USB bulk
packets. Every rate from 3.1 kB/s to 39.7 kB/s loaded an 8572-byte table
correctly at this chunk size, so the choice is not delicate; what matters is
that a chunk is far below the box's 4096-byte input buffer and that
`DEFAULT_CHUNK_GAP_S` separates the writes."""

DEFAULT_CHUNK_GAP_S = 0.010
"""The pause after each chunk, which is what actually makes a long table load.

The box has no flow control (§1): `ReadAllSerial()` moves everything the USB
stack is holding into the 4096-byte ring buffer in one pass, and `RB_Put()`
drops silently once that is full, so a host writing at full speed loses the
tail of anything past about 4 KB. A pause hands the processor back to
`GetToken()` between deliveries. 10 ms against the 3-second token timeout
spends three parts in a thousand of the margin and carried 17572 bytes on the
bench, where the same table written with no pause failed."""


class MipsError(Exception):
    """Anything that went wrong talking to a box."""


class BoxTimeout(MipsError):
    """The box did not answer in time.

    On a table load this is ambiguous in a way worth knowing about: a box that
    is going to reject a table only NAKs after flushing the rest of the string,
    which costs it the full 3-second token timeout (§4). A timeout shorter than
    that reports silence where the box was about to report a parse error.
    """


class BoxRejected(MipsError):
    """The box NAKed. `code` is what `GERR` said, when it could be asked."""

    def __init__(self, command: str, code: int | None) -> None:
        self.command = command
        self.code = code
        detail = f"error {code}, {error_text(code)}" if code is not None else "reason unread"
        super().__init__(f"box rejected {command!r} ({detail})")


@dataclass(slots=True)
class TableLoad:
    """What one `STBLDAT` cost, and what the host expected it to build."""

    string: str
    bytes_sent: int
    write_seconds: float
    """Wall clock for the whole string."""

    slowest_chunk_seconds: float
    """Wall clock for the single slowest `write`, which is what the box's
    timeout actually measures: it gives up when 3 seconds pass without a
    token, not when a load takes 3 seconds in total."""

    reply_seconds: float
    predicted: _table.Compiled | None = None
    prediction_error: str | None = None

    @property
    def stall_margin(self) -> float:
        """Seconds between the worst stall and the box abandoning the table.

        Only meaningful measured against a real box; against the in-process
        fake a write costs nothing and the margin is the whole timeout.
        """
        return TOKEN_TIMEOUT_S - self.slowest_chunk_seconds


@dataclass(slots=True)
class Box:
    """A controller box on the other end of a `Transport`."""

    transport: Transport
    name: str = "box"
    timeout: float = DEFAULT_TIMEOUT_S
    events: deque[TableEvent] = field(default_factory=deque)
    """Status lines the box sent on its own, oldest first."""

    _reader: ResponseReader = field(default_factory=ResponseReader, repr=False)
    _tokens: deque[Token] = field(default_factory=deque, repr=False)
    _querying_error: bool = field(default=False, repr=False)

    @classmethod
    def open(
        cls,
        port: str,
        *,
        name: str | None = None,
        baudrate: int | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> Box:
        """Open a COM port and wrap the box on it."""
        transport = (
            open_serial(port) if baudrate is None else open_serial(port, baudrate=baudrate)
        )
        return cls(transport=transport, name=name or port, timeout=timeout)

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> Box:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- reading -----------------------------------------------------------

    def _pump(self, timeout: float) -> None:
        """Read once and frame whatever came."""
        data = self.transport.read_some(timeout)
        if data:
            if _LOG.isEnabledFor(logging.DEBUG):
                _LOG.debug("%s < %s", self.name, _render(data))
            self._tokens.extend(self._reader.feed(data))

    def _file(self, event: TableEvent) -> None:
        """Queue an asynchronous status line, and say so in the transcript.

        The one derived record on this link: the bytes are already transcribed by
        whichever read carried them, and this says how they were classified, which
        is the decision a reader would otherwise have to redo by hand.
        """
        self.events.append(event)
        if _LOG.isEnabledFor(logging.DEBUG):
            _LOG.debug("%s ! %s", self.name, event.value)

    def _sift(self) -> None:
        """Move the status lines out of the token queue and into `events`."""
        keep: deque[Token] = deque()
        for token in self._tokens:
            event = table_event(token.text) if token.kind is Kind.LINE else None
            if event is None:
                keep.append(token)
            else:
                self._file(event)
        self._tokens = keep

    def _next_token(self, deadline: float, what: str, *, verbatim: bool = False) -> Token:
        """The next token, filing status lines on the way past.

        `verbatim` suppresses that filing, for the one line where the
        distinction cannot be made from the text: `GTBLSTA` answers `ABORTED`,
        which is also exactly what an abort's status line says. Position is the
        only thing that separates them, and position is only known here, at the
        point where a get-style command has had its ACK and its value is next.
        """
        while True:
            while self._tokens:
                token = self._tokens.popleft()
                if not verbatim and token.kind is Kind.LINE:
                    event = table_event(token.text)
                    if event is not None:
                        self._file(event)
                        continue
                return token
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                partial = self._reader.partial()
                tail = f"; partial reply {partial!r}" if partial else ""
                raise BoxTimeout(f"{self.name}: no {what} within the timeout{tail}")
            self._pump(min(remaining, 0.25))

    def drain(self, seconds: float = 0.0) -> list[TableEvent]:
        """Collect status lines that have arrived, optionally waiting a little."""
        deadline = time.monotonic() + seconds
        while True:
            self._pump(max(0.0, min(deadline - time.monotonic(), 0.05)))
            self._sift()
            if time.monotonic() >= deadline:
                break
        collected = list(self.events)
        self.events.clear()
        return collected

    def resync(
        self, settle: float = 1.0, limit: float = TOKEN_TIMEOUT_S * 3
    ) -> list[TableEvent]:
        """Wait out a failed table load and drop whatever it left on the wire.

        A table the box cannot parse is NAKed only after the parser has flushed
        the rest of the string, which costs it the inter-token timeout (§4), so
        a host that gave up waiting can still have that NAK arrive afterwards.
        Left alone it would be read as the *next* command's answer, and every
        reply after it would be off by one.

        Reads until the box has said nothing for `settle` seconds, or `limit`
        elapses, then keeps the status lines and discards the rest. Draining to
        silence rather than for a fixed time is what makes this reliable: a
        table that overran the ring buffer leaves its tail in the box, which
        then reads that tail as commands and NAKs each one, so how long the
        noise lasts scales with how far the table overshot and is not a
        constant.

        Returns the status lines seen, which is how an abort that arrived late
        stays visible instead of being thrown away with the rest.
        """
        started = time.monotonic()
        last_heard = started
        while True:
            now = time.monotonic()
            if now - last_heard >= settle or now - started >= limit:
                break
            data = self.transport.read_some(min(0.1, settle))
            if data:
                if _LOG.isEnabledFor(logging.DEBUG):
                    _LOG.debug("%s < %s", self.name, _render(data))
                self._tokens.extend(self._reader.feed(data))
                last_heard = time.monotonic()
        self._sift()
        if _LOG.isEnabledFor(logging.DEBUG):
            _LOG.debug("%s   resync after %.2f s discarded %d token(s), kept %d status line(s)",
                       self.name, time.monotonic() - started, len(self._tokens),
                       len(self.events))
        self._tokens.clear()
        self._reader = ResponseReader()
        collected = list(self.events)
        self.events.clear()
        return collected

    def wait_for(self, *events: TableEvent, timeout: float | None = None) -> TableEvent:
        """Block until one of `events` arrives, and return which.

        Status lines already queued count, so arming and then waiting does not
        race against a `TBLRDY` that came back with the ACK.
        """
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            self._sift()
            for i, queued in enumerate(self.events):
                if queued in events:
                    del self.events[i]
                    return queued
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                wanted = " or ".join(e.value for e in events)
                raise BoxTimeout(f"{self.name}: no {wanted} within the timeout")
            self._pump(min(remaining, 0.25))

    # -- commanding --------------------------------------------------------

    def command(
        self, text: str, *, value: bool = False, timeout: float | None = None
    ) -> str | None:
        """Send one command and wait for its ACK, NAK, or value.

        `value=True` for the get-style commands, which answer with a bare ACK
        byte and then the value on a line of its own (§1).
        """
        payload = text.encode("ascii") + b"\n"
        if _LOG.isEnabledFor(logging.DEBUG):
            _LOG.debug("%s > %s", self.name, _render(payload))
        self.transport.write(payload)
        return self._await_reply(text, value=value, timeout=timeout)

    def _await_reply(
        self, what: str, *, value: bool = False, timeout: float | None = None
    ) -> str | None:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        token = self._next_token(deadline, "reply")
        while token.kind is Kind.LINE:
            # A line before the ACK is unsolicited output the framer did not
            # recognise as a status line. Skip it rather than mistake it for
            # the reply; a real box emits DIO and ADC change reports this way.
            token = self._next_token(deadline, "reply")
        if token.kind is Kind.NAK:
            raise BoxRejected(what, self._read_error_code())
        if not value:
            return None
        return self._next_token(deadline, "value", verbatim=True).text

    def _read_error_code(self) -> int | None:
        """Ask `GERR` what the NAK meant, without recursing on its own NAK."""
        if self._querying_error:
            return None
        self._querying_error = True
        try:
            answer = self.command("GERR", value=True)
            return int(answer) if answer is not None else None
        except (MipsError, ValueError):
            return None
        finally:
            self._querying_error = False

    # -- the commands a sequencer needs ------------------------------------

    def version(self) -> str:
        """`GVER`. Worth logging per box: the protocol is written against 1.263."""
        return self.command("GVER", value=True) or ""

    def box_name(self) -> str:
        """`GNAME`, the box's own idea of its identity."""
        return self.command("GNAME", value=True) or ""

    def table_status(self) -> str:
        """`GTBLSTA`: one of IDLE, READY, TRIGGERED, ABORTED."""
        return self.command("GTBLSTA", value=True) or ""

    def last_error(self) -> tuple[int, str]:
        """`GERR` and its meaning. Never cleared, so only read it after a NAK."""
        code = int(self.command("GERR", value=True) or 0)
        return code, error_text(code)

    def arm(self, mode: str = "TBL", *, timeout: float | None = None) -> None:
        """`SMOD` into table mode and wait for the `TBLRDY` that follows."""
        self.command(f"SMOD,{mode}")
        self.wait_for(TableEvent.READY, timeout=timeout)

    def trigger(self) -> None:
        """`TBLSTRT`, the software trigger. The instrument uses a hardware one."""
        self.command("TBLSTRT")

    def stop(self) -> None:
        """`TBLSTOP`: finish gracefully, stay in table mode."""
        self.command("TBLSTOP")

    def abort(self) -> None:
        """`TBLABRT`: leave table mode now."""
        self.command("TBLABRT")

    def local(self) -> None:
        """`SMOD,LOC`, which a table load needs unless the box is READY.

        Idempotent, though the command itself is not: a box already in LOC mode
        NAKs with `ERR_LOCALREADY` (§4). The only reason to call this is to be
        sure of the mode, and that answer says the mode is already right, so it
        is success. Every other rejection is real and propagates.
        """
        try:
            self.command("SMOD,LOC")
        except BoxRejected as exc:
            if exc.code != ERR_ALREADY_LOCAL:
                raise

    # -- loading a table ---------------------------------------------------

    def send_table(
        self,
        table_string: str,
        *,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        chunk_gap: float = DEFAULT_CHUNK_GAP_S,
        timeout: float | None = None,
    ) -> TableLoad:
        """Stream one `STBLDAT` string and wait for the box to accept it.

        The string is written in chunks with a short pause after each, because
        the box has no flow control and a host writing at full speed silently
        overruns its 4096-byte input buffer (§1). Both numbers are bench
        measurements; `chunk_gap=0` reproduces the unpaced behaviour, which
        loses the tail of any table much past 4 KB.

        The reply is waited for with at least the token timeout on top of
        however long the write took, since a box that means to reject the
        table flushes the rest of it first.

        Raises `BoxRejected` if the box NAKs, after asking `GERR` why.

        **The transcript writes nothing between the chunks.** This is the one
        path in the package where a record is deferred, and `chunk_gap` is the
        reason: a formatted line flushed to a file in the gap would add itself to
        the interval that paces the load, which is a bench measurement and not a
        preference. The loop appends a tuple per chunk -- no formatting and no
        I/O, and only when a transcript is open, which is decided once before the
        loop -- and the chunk lines go out together the instant the string is on
        the wire. Each carries its own offset from the start of the send, because
        the timestamp the handler stamps on those lines is when they were flushed
        and not when the chunk went. What is lost is the detail of a send
        interrupted part way, and what is kept is that the pacing with a
        transcript open is the pacing without one.
        """
        predicted: _table.Compiled | None = None
        prediction_error: str | None = None
        try:
            predicted = _table.compile_table(table_string)
        except _table.TableSyntaxError as exc:
            # Not fatal. The box's parser is the authority, and this one is an
            # unverified model of it (`table.py`); a disagreement is worth
            # recording and is not worth refusing to send over.
            prediction_error = str(exc)

        payload = table_string.encode("ascii")
        if not payload.endswith(b";"):
            raise ValueError("a table string must end with ';' or the box waits for one")
        payload += b"\n"

        chunks = -(-len(payload) // chunk_bytes)
        tracing = _LOG.isEnabledFor(logging.DEBUG)
        marks: list[tuple[int, int, float, float]] = []
        if tracing:
            _LOG.debug(
                "%s > STBLDAT %d bytes in %d chunks of %d, %.1f ms apart; the chunk lines "
                "below are written after the send and carry their own offsets",
                self.name, len(payload), chunks, chunk_bytes, chunk_gap * 1e3,
            )
            # On its own line, and never elided: this string is the evidence any
            # correction to the wire format's section 2 would be argued from.
            _LOG.debug("%s >   string: %s", self.name, table_string)

        # perf_counter, not monotonic: on Windows `time.monotonic()` ticks at
        # about 15.6 ms, which is coarser than a whole table write, so it
        # quantises exactly the number the stall margin is computed from.
        started = time.perf_counter()
        slowest = 0.0
        for at in range(0, len(payload), chunk_bytes):
            chunk_started = time.perf_counter()
            self.transport.write(payload[at : at + chunk_bytes])
            chunk_ended = time.perf_counter()
            slowest = max(slowest, chunk_ended - chunk_started)
            if tracing:
                # A list append, which is the same order as the `max` above it.
                # Nothing is formatted and nothing is written until the loop ends.
                marks.append((at, min(chunk_bytes, len(payload) - at),
                              chunk_started - started, chunk_ended - chunk_started))
            if chunk_gap and at + chunk_bytes < len(payload):
                time.sleep(chunk_gap)
        write_seconds = time.perf_counter() - started

        if tracing:
            for number, (at, size, offset, cost) in enumerate(marks, start=1):
                _LOG.debug("%s >   chunk %d/%d at +%.3f s, bytes %d-%d, write %.2f ms",
                           self.name, number, chunks, offset, at, at + size - 1, cost * 1e3)
            _LOG.debug("%s >   %d bytes on the wire in %.3f s, slowest chunk %.2f ms, "
                       "%.2f s of stall margin",
                       self.name, len(payload), write_seconds, slowest * 1e3,
                       TOKEN_TIMEOUT_S - slowest)

        reply_timeout = timeout
        if reply_timeout is None:
            reply_timeout = max(self.timeout, TOKEN_TIMEOUT_S + write_seconds) + 1.0
        replied = time.perf_counter()
        self._await_reply("STBLDAT", timeout=reply_timeout)
        return TableLoad(
            string=table_string,
            bytes_sent=len(payload),
            write_seconds=write_seconds,
            slowest_chunk_seconds=slowest,
            reply_seconds=time.perf_counter() - replied,
            predicted=predicted,
            prediction_error=prediction_error,
        )

    def report(self, count: int, *, timeout: float | None = None) -> _table.Report:
        """`TBLRPT`: dump `count + 1` bytes of the table buffer.

        There is no ACK (§4), so this reads lines until it has the preamble and
        every byte it asked for. A dump is one line per byte and a real table
        runs to thousands of them, so the default timeout grows with the size
        of the dump rather than staying at the per-command one; a short timeout
        here would report silence in the middle of a reply that was arriving.
        """
        wanted = 5 + count + 1
        if timeout is None:
            timeout = self.timeout + 0.01 * wanted
        deadline = time.monotonic() + timeout
        payload = f"TBLRPT,{count}\n".encode("ascii")
        if _LOG.isEnabledFor(logging.DEBUG):
            _LOG.debug("%s > %s, expecting %d lines", self.name, _render(payload), wanted)
        self.transport.write(payload)
        lines: list[str] = []
        while len(lines) < wanted:
            token = self._next_token(deadline, f"TBLRPT line {len(lines) + 1} of {wanted}")
            if token.kind is not Kind.LINE:
                raise MipsError(f"{self.name}: unexpected {token} in a TBLRPT reply")
            lines.append(token.text)
        return _table.parse_report(lines)

    def verify_table(self, load: TableLoad, *, timeout: float | None = None) -> list[str]:
        """Dump what the box parsed and compare it against the prediction.

        Returns every difference found, so an empty list is the round trip
        passing. Raises if the load carried no usable prediction to compare to.
        """
        if load.predicted is None:
            raise MipsError(
                f"{self.name}: nothing to compare against ({load.prediction_error})"
            )
        report = self.report(load.predicted.byte_size, timeout=timeout)
        problems: list[str] = []
        if not report.packing_matches:
            problems.append(
                "the box packs its table structs as "
                f"{report.struct_sizes}, not the (13, 5, 5) this code assumes; "
                "every comparison below is meaningless until that is resolved"
            )
        if report.tables_loaded != load.predicted.tables_loaded:
            problems.append(
                f"TablesLoaded: predicted {load.predicted.tables_loaded}, "
                f"box says {report.tables_loaded}"
            )
        if report.test_nesting != load.predicted.nesting:
            problems.append(
                f"TestNesting: predicted {load.predicted.nesting}, "
                f"box says {report.test_nesting}"
            )
        problems += _table.differences(load.predicted, _table.decode(report.data))
        if _LOG.isEnabledFor(logging.DEBUG):
            # A conclusion rather than traffic, and in the transcript because it is
            # the one that would be cited: a disagreement here is evidence about a
            # firmware's parser, and §2 is corrected from it.
            _LOG.debug("%s   TBLRPT round trip: %s", self.name,
                       "; ".join(problems) if problems else "clean")
        return problems
