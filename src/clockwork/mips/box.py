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

*A table load cannot stall.* The box abandons a table string if any 3 seconds
pass without a token, and drops characters silently if the host outruns its
4 KB input buffer. `send_table` writes in chunks below that size with nothing
between them, so the link's own flow control sets the pace, and reports how
long the write took so that the margin against the timeout is a measurement
rather than an assumption.

Blocking, and deliberately so: every read waits on a deadline. Nothing here may
be called from the UI thread.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from . import table as _table
from .transport import Transport, open_serial
from .wire import (
    TOKEN_TIMEOUT_S,
    Kind,
    ResponseReader,
    TableEvent,
    Token,
    error_text,
    table_event,
)

DEFAULT_TIMEOUT_S = 2.0
"""Long enough for any command that is not a table load, on a USB link."""

DEFAULT_CHUNK_BYTES = 512
"""Well under the box's 4096-byte input buffer, and a whole number of USB
bulk packets. The value that actually survives on our link is a bench
measurement (§7); this is the conservative starting point."""


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
            self._tokens.extend(self._reader.feed(data))

    def _sift(self) -> None:
        """Move the status lines out of the token queue and into `events`."""
        keep: deque[Token] = deque()
        for token in self._tokens:
            event = table_event(token.text) if token.kind is Kind.LINE else None
            if event is None:
                keep.append(token)
            else:
                self.events.append(event)
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
                        self.events.append(event)
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
        self.transport.write(text.encode("ascii") + b"\n")
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
        """`SMOD,LOC`, which a table load needs unless the box is READY."""
        self.command("SMOD,LOC")

    # -- loading a table ---------------------------------------------------

    def send_table(
        self,
        table_string: str,
        *,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        timeout: float | None = None,
    ) -> TableLoad:
        """Stream one `STBLDAT` string and wait for the box to accept it.

        The string is written in chunks with no delay between them: small
        enough not to overrun the box's input buffer, large enough that the
        gaps stay far below the 3-second token timeout. The reply is waited for
        with at least that timeout on top of however long the write took, since
        a box that means to reject the table flushes the rest of it first.

        Raises `BoxRejected` if the box NAKs, after asking `GERR` why.
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

        started = time.monotonic()
        slowest = 0.0
        for at in range(0, len(payload), chunk_bytes):
            chunk_started = time.monotonic()
            self.transport.write(payload[at : at + chunk_bytes])
            slowest = max(slowest, time.monotonic() - chunk_started)
        write_seconds = time.monotonic() - started

        reply_timeout = timeout
        if reply_timeout is None:
            reply_timeout = max(self.timeout, TOKEN_TIMEOUT_S + write_seconds) + 1.0
        replied = time.monotonic()
        self._await_reply("STBLDAT", timeout=reply_timeout)
        return TableLoad(
            string=table_string,
            bytes_sent=len(payload),
            write_seconds=write_seconds,
            slowest_chunk_seconds=slowest,
            reply_seconds=time.monotonic() - replied,
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
        self.transport.write(f"TBLRPT,{count}\n".encode("ascii"))
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
        return problems
