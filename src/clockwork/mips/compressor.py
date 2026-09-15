"""The ARB compression table, read only as far as its loop counts.

`SARBCTBL` carries a program for the compressor's own state machine, in the
mini-language `docs/mips-wire-format.md` §6.6 documents. A golden method's two
compression tables end `]100`, which is the same accumulation count AUKLET's
`STBLDAT` table loops and `[acquisition]` states, and nothing has ever checked
that the three agree (lab record, task 31). That check is all anything here
needs off the string, so this reads the loop counts and stops.

**It is not a parser of the language and must not become one.** §6.6 is explicit
that the box skips a character it does not know and syntax-checks nothing on
load, so a reader that rejected an unfamiliar op would be stricter than the box
it is standing in for, and a trainee's verbatim string is not ours to reject for
being unusual. What is mirrored is only the scan: an op character, the digits
that may follow it, and the one raw character five of the ops then take. That
last part is why this is a walk rather than a count of brackets -- `H`, `m`, `S`,
`g` and `G` swallow the character after them, and a reader that did not would
read that character as an op.

No Qt, no hardware, no I/O.
"""

from __future__ import annotations

COMPRESSION_COMMAND = "SARBCTBL"
"""The command whose argument is a compression table. §6.6."""

_ALWAYS_TAKES_A_CHARACTER = frozenset("H")
"""`H<input>`: the digital input to halt on, whatever preceded it."""

_TAKES_A_CHARACTER_WITH_A_VALUE = frozenset("m")
"""`m<module><N|C>`: the mode letter, read only once a module number was."""

_TAKES_A_CHARACTER_WITHOUT_A_VALUE = frozenset("SgG")
"""`S`, `g`, `G`: a port character and then a value, where no value was given."""


def compression_table(command: str) -> str:
    """The table argument of a `SARBCTBL` command, without the command word.

    Raises `ValueError` for anything else, including `GARBCTBL`, which is the
    same table coming the other way and is not what a method carries.
    """
    text = command.strip()
    head, separator, table = text.partition(",")
    if not separator or head.strip().upper() != COMPRESSION_COMMAND:
        raise ValueError(f"not a {COMPRESSION_COMMAND} command: {command!r}")
    return table.strip()


def compression_passes(command: str) -> tuple[int, ...]:
    """The pass count of every outermost `[`...`]`*n* loop, in the order written.

    One number per top-level loop, so the two golden tables -- each a single
    loop around one repetition's worth of work -- give `(100,)`. A table with no
    loop at all gives an empty tuple, which is one pass said differently and is
    the caller's to read that way. Nested loops are walked past: their counts
    multiply the body rather than the table's passes, and no string on this
    instrument has one.

    Raises `ValueError` for a string this cannot read: not a `SARBCTBL` command,
    or brackets that do not balance. The box would accept the second and walk it
    to whatever end it reached, so it is a string the host cannot check rather
    than one the box would reject, and the caller is expected to say so rather
    than to refuse the method (lab record, task 31).
    """
    table = compression_table(command)
    counts: list[int] = []
    depth = 0
    at = 0
    while at < len(table):
        op = table[at]
        at += 1
        value, at = _value(table, at)
        if op == "[":
            depth += 1
            continue
        if op == "]":
            depth -= 1
            if depth < 0:
                raise ValueError(
                    f"a ']' with no '[' before it, at character {at - 1} of {table!r}"
                )
            if depth == 0:
                counts.append(DEFAULT_VALUE if value is None else value)
            continue
        if (op in _ALWAYS_TAKES_A_CHARACTER
                or (op in _TAKES_A_CHARACTER_WITH_A_VALUE and value is not None)
                or (op in _TAKES_A_CHARACTER_WITHOUT_A_VALUE and value is None)):
            at += 1
            if op in _TAKES_A_CHARACTER_WITHOUT_A_VALUE:
                _, at = _value(table, at)
    if depth:
        raise ValueError(f"{depth} unclosed '[' in {table!r}")
    return tuple(counts)


DEFAULT_VALUE = 1
"""What an op with no digits after it is worth, so a bare `]` is one pass. §6.6."""


def _value(table: str, at: int) -> tuple[int | None, int]:
    """The digits at `at` read as the firmware reads them, and where they end.

    `None` where there were none, which is the firmware's `valfound` under
    another name and is what three of the ops above key their raw character on.
    An integer part, then a fractional part that is scanned and discarded: every
    quantity this reader cares about is a count, and the firmware keeps the
    integer part in the same `count` a loop end reads.
    """
    start = at
    while at < len(table) and table[at].isdigit():
        at += 1
    if at == start:
        return None, at
    whole = int(table[start:at])
    if at < len(table) and table[at] == ".":
        at += 1
        while at < len(table) and table[at].isdigit():
            at += 1
    return whole, at


__all__ = ["COMPRESSION_COMMAND", "DEFAULT_VALUE", "compression_passes",
           "compression_table"]
