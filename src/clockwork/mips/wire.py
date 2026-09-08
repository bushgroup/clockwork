"""Serial framing for MIPS controller boxes: bytes in, tokens out.

`docs/mips-wire-format.md` §1 is the source for every constant and rule here;
nothing in this module decides protocol, it only implements what that section
states. In particular the four terminator conventions the firmware uses (LF-CR
after a set-style ACK, CR-LF after a printed value, a doubled newline after an
asynchronous status line, and a bare LF after one of the two abort forms) all
collapse to a single token stream under the rule the document gives: drop every
CR, split on LF, treat 0x06 and 0x15 as tokens of their own, discard blanks.

No Qt, no hardware, no I/O: this module is pure byte handling, which is what
lets both the real link and the simulated box in `transport.py` share it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

ACK = 0x06
"""Command accepted. Written bare before a value, or as `\\x06\\n\\r` alone."""

NAK = 0x15
"""Command rejected, written as `\\x15?\\n\\r`. `GERR` says why."""

TOKEN_TIMEOUT_S = 3.0
"""`Table.cpp: NextToken()`'s inter-token timeout while reading `STBLDAT`.

Two things are measured against it: a stall this long anywhere in a table
string abandons the load, and a table that fails to parse is NAKed only after
the parser has flushed the rest of the command, which costs the same 3 seconds.
"""

RING_BUFFER_BYTES = 4096
"""`Serial.h: RB_BUF_SIZE`, the box's whole serial input buffer.

Overflowing it drops characters silently (`PutCh()` discards `RB_Put()`'s
full-buffer return), so a long table has to be written in pieces rather than in
one call. See the ring-buffer section of §1.
"""

DEFAULT_BAUDRATE = 115200
"""Nominal only.

The boxes are used over the Arduino Due's native USB port, which is CDC: the
firmware never calls `begin()` on it and the rate is not a real line rate.
pyserial still wants a number, and this is a conventional one.
"""


class Kind(enum.Enum):
    """What a framing token is."""

    ACK = "ack"
    NAK = "nak"
    LINE = "line"


@dataclass(frozen=True, slots=True)
class Token:
    kind: Kind
    text: str = ""

    def __str__(self) -> str:
        if self.kind is Kind.LINE:
            return f"line {self.text!r}"
        return self.kind.value.upper()


ACK_TOKEN = Token(Kind.ACK)
NAK_TOKEN = Token(Kind.NAK)


class ResponseReader:
    """Incremental framer: feed it whatever arrived, get whole tokens back.

    Stateful across calls because a read can split a line, a CR-LF pair, or the
    `?` that follows a NAK. Feed everything through one reader per port.
    """

    __slots__ = ("_line", "_swallow_question")

    def __init__(self) -> None:
        self._line = bytearray()
        self._swallow_question = False

    def feed(self, data: bytes) -> list[Token]:
        """Frame `data`, returning the tokens it completed (possibly none)."""
        tokens: list[Token] = []
        for byte in data:
            if self._swallow_question:
                self._swallow_question = False
                if byte == 0x3F:  # '?', the second byte of the NAK sequence
                    continue
            if byte == ACK:
                tokens.append(ACK_TOKEN)
            elif byte == NAK:
                tokens.append(NAK_TOKEN)
                self._swallow_question = True
            elif byte == 0x0D:  # CR: never significant, in either convention
                continue
            elif byte == 0x0A:
                text = self._line.decode("ascii", "replace").strip()
                self._line.clear()
                if text:
                    tokens.append(Token(Kind.LINE, text))
            else:
                self._line.append(byte)
        return tokens

    def partial(self) -> str:
        """The unterminated tail, for reporting a timeout usefully."""
        return self._line.decode("ascii", "replace")


class TableEvent(enum.Enum):
    """An unsolicited table-status line (§1, asynchronous messages)."""

    READY = "TBLRDY"
    TRIGGERED = "TBLTRIG"
    COMPLETE = "TBLCMPLT"
    ABORTED = "ABORTED"
    STOPPED = "Table stoped by user"


def table_event(line: str) -> TableEvent | None:
    """Classify a text token, or None if it is not a status line.

    `ABORTED` matches on prefix because the firmware has two spellings of it,
    `ABORTED` from `TBLABRT` and the low-voltage check, and `ABORTED by user`
    from the front-panel button. Both mean table mode ended.
    """
    if line.startswith(TableEvent.ABORTED.value):
        return TableEvent.ABORTED
    for event in TableEvent:
        if line == event.value:
            return event
    return None


TABLE_STATES = ("IDLE", "READY", "TRIGGERED", "ABORTED")
"""What `GTBLSTA` can answer.

Not on every box: `GTBLSTA` is absent from firmware 1.163t and NAKs there
as an invalid command (§4). Code that needs the state on any firmware has
to follow the asynchronous status lines instead.
"""

ERR_ALREADY_LOCAL = 3
"""`SMOD,LOC` answering "the box was already local", which is a success
for any caller that sent it to be sure of the mode. §4."""


ERROR_CODES: dict[int, str] = {
    1: "invalid command",
    2: "invalid argument",
    3: "already in LOC mode",
    4: "already in TBL mode",
    5: "no tables loaded",
    6: "not in table mode",
    7: "table not ready",
    8: "timed out waiting for a token",
    9: "expected a colon",
    10: "table too big",
    11: "channel number too low, or board not present",
    12: "channel number too high, or board not present",
    13: "channel number too high",
    14: "board number too low, or board not present",
    15: "board number too high, or board not present",
    16: "board number too high",
    17: "command not supported on this board revision",
    18: "invalid baud rate",
    19: "expected a comma",
    20: "table nesting too deep",
    21: "] without a matching [",
    22: "invalid channel request",
    23: "DIO hardware not found",
    27: "not in LOC mode",
    28: "wrong trigger mode",
    29: "cannot locate the requested table entry",
    101: "requested value out of range",
    107: "not supported in this MIPS controller revision",
    112: "internal error, such as a resource that could not be allocated",
    115: "no ARB module in system",
    116: "cannot allocate the memory needed",
    122: "not supported by the hardware",
}
"""`GERR` codes a table-mode sequencer can meet, from `include/Errors.h`.

Not the whole list: the codes about temperature control, ESI, SD cards, EEPROM
and auto-tune belong to subsystems clockwork does not drive. `error_text`
degrades gracefully for anything missing.
"""


def error_text(code: int) -> str:
    """Describe a `GERR` code, without pretending to recognise all of them."""
    return ERROR_CODES.get(code, f"error {code}")
