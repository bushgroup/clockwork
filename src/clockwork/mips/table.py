"""The compiled table layout, predicted from a table string and read back from a box.

`STBLDAT` is parsed on the box into packed structs in RAM, and `TBLRPT` dumps
those bytes back. That makes a byte-level round trip possible: predict what the
box will build from the string about to be sent, send it, dump the buffer, and
compare. It is a much stronger check than comparing strings, because it sees
what the box's parser actually decided rather than what the host meant.

`docs/mips-wire-format.md` §2 is the source for the grammar, the channel codes
and the packed layout, including the three awkward parts a naive reading misses:
a non-zero leading offset costs an entire extra table header, DC bias channels
are stored zero-based, and DC bias *values* are calibration-dependent and so are
not predictable off the box at all.

This is not the deferred v1-to-v2 sequence compiler. It compiles nothing the
instrument runs and generates no strings; it only models the box's parse well
enough to check a round trip. Whether the model agrees with a real box is a
bench measurement (§7), not something the tests here can establish.

No Qt, no hardware, no I/O.
"""

from __future__ import annotations

import enum
import struct
from collections.abc import Iterator
from dataclasses import dataclass

TABLE_HEADER_BYTES = 13
"""`TableHeader`: name, repeat count, max count, entry count. §2."""

TIME_POINT_BYTES = 5
"""`TableEntryHeader`: the tick, and how many channel entries follow. §2."""

ENTRY_BYTES = 5
"""`TableEntry`: one channel byte and one 32-bit value. §2."""

MAX_NESTING = 5
"""`Table.h: MaxNesting`, the depth of the run-time loop stack."""

UNNAMED = 0xFF
"""`TableHeader.TableName` for a table the string did not name."""

END_OF_TABLES = 0x00
"""The one meaningful byte after the last table. §2."""

_DELIMITERS = frozenset("\n;:,][/")
"""`Serial.cpp: GetToken()`. Note that a space is *not* a delimiter."""

_RAMP = 0x80
_INITIAL = 0x40


class ValueKind(enum.Enum):
    """How a `TableEntry`'s 32-bit value is to be read, decided by its channel."""

    DAC = "dac"
    """A pre-formatted DAC/SPI frame. Board-calibration dependent: a host can
    see that one is present but cannot predict or interpret it."""

    FLOAT = "float"
    """Raw IEEE-754 bits, for RF drive levels and ARB aux/offset volts."""

    INT = "int"
    """A plain integer: `t`, `b`, `d`, `p` and the loop-counter comparisons."""

    CHAR = "char"
    """The ASCII code of the value character, for digital outputs and `c`."""


class TableSyntaxError(ValueError):
    """A table string the box's parser would reject.

    `error_code` is the `GERR` code the box would report for the same problem
    where one corresponds, so a local rejection and a NAK can be compared.
    """

    def __init__(self, message: str, *, error_code: int | None = None) -> None:
        self.error_code = error_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Entry:
    """One `Channel:Value` pair as the box stores it."""

    chan: int
    """The stored channel byte, which is *not* always the token as written:
    DC bias channel n is stored as n - 1."""

    value: int | None
    """The stored 32-bit value, or None for a DAC frame no host can predict."""

    kind: ValueKind

    def describe(self) -> str:
        return f"{channel_token(self.chan)}={self.render_value()}"

    def render_value(self) -> str:
        if self.value is None:
            return "<dac>"
        if self.kind is ValueKind.FLOAT:
            return repr(struct.unpack("<f", struct.pack("<i", self.value))[0])
        if self.kind is ValueKind.CHAR:
            return repr(chr(self.value & 0xFF))
        return str(self.value)


@dataclass(frozen=True, slots=True)
class TimePoint:
    """A `TableEntryHeader` and the entries that share its tick."""

    count: int
    entries: tuple[Entry, ...]


@dataclass(frozen=True, slots=True)
class Table:
    """One `TableHeader` and its time points."""

    name: int
    repeat: int
    max_count: int
    points: tuple[TimePoint, ...]

    @property
    def label(self) -> str:
        return "unnamed" if self.name == UNNAMED else repr(chr(self.name))


@dataclass(frozen=True, slots=True)
class Compiled:
    """What a box is predicted to hold after loading one table string.

    `tables_loaded` and `nesting` mirror the two counters `TBLRPT`'s preamble
    reports, so all three numbers in a dump can be checked, not just the bytes.
    """

    tables: tuple[Table, ...]
    tables_loaded: int
    nesting: int

    @property
    def byte_size(self) -> int:
        """Bytes of buffer the tables occupy, not counting the end marker.

        This is also the argument to give `TBLRPT`, which dumps one more byte
        than it is asked for and so covers the marker exactly.
        """
        return sum(
            TABLE_HEADER_BYTES
            + sum(TIME_POINT_BYTES + ENTRY_BYTES * len(p.entries) for p in t.points)
            for t in self.tables
        )


# --------------------------------------------------------------------------
# Channel codes
# --------------------------------------------------------------------------

_INT_CHANNELS = frozenset(ord(c) for c in "tbdp><=")
_CHAR_CHANNELS = frozenset(ord(c) for c in "rsca") | frozenset(range(ord("A"), ord("P") + 1))
_LOOP_END = ord("]")


def value_kind(chan: int) -> ValueKind:
    """How to read the value stored against channel byte `chan`.

    Ambiguous in one corner: a DC bias channel written with the `INITIAL` flag
    (64 + n) is stored as 63 + n, which for n >= 2 lands inside the ASCII range
    of digital outputs A-P. Nothing in the dump distinguishes them. v1 sends no
    ramping channels, so this reads such a byte as a digital output; a caller
    that starts using `STBLVDLT` ramps has to disambiguate from the string.
    """
    if 0 <= chan <= 31:
        return ValueKind.DAC
    if chan == _LOOP_END:
        return ValueKind.INT
    if chan in _CHAR_CHANNELS:
        return ValueKind.CHAR
    if chan in _INT_CHANNELS:
        return ValueKind.INT
    if 33 <= chan <= 36 or 101 <= chan <= 108:
        return ValueKind.FLOAT
    if chan & (_RAMP | _INITIAL):
        return ValueKind.DAC
    return ValueKind.INT


def channel_token(chan: int) -> str:
    """Render a stored channel byte the way §2's channel table names it.

    Classifies in the same order as `value_kind`, so the two never disagree
    about what a byte is.
    """
    if 0 <= chan <= 31:
        return f"DCB{chan + 1}"
    if chan == _LOOP_END:
        return "]"
    if chan in _CHAR_CHANNELS or chan in _INT_CHANNELS:
        return chr(chan)
    if 33 <= chan <= 36:
        return f"RF{chan - 32}"
    if 101 <= chan <= 108:
        return str(chan)
    flags = chan & (_RAMP | _INITIAL)
    if flags:
        which = {_RAMP: "ramp", _INITIAL: "initial", _RAMP | _INITIAL: "ramp start"}[flags]
        return f"DCB{(chan & ~(_RAMP | _INITIAL)) + 1} {which}"
    return f"chan{chan}"


# --------------------------------------------------------------------------
# String -> predicted layout
# --------------------------------------------------------------------------


def _tokenize(payload: str) -> Iterator[str]:
    """Split as `GetToken()` does: delimiters end a token and are tokens too."""
    buffered = ""
    for char in payload:
        if char in _DELIMITERS:
            if buffered:
                yield buffered
                buffered = ""
            yield char
        else:
            buffered += char
    if buffered:
        # The box would sit in NextToken() waiting for the delimiter that never
        # comes, and give up after the inter-token timeout.
        raise TableSyntaxError(
            f"table string ends mid-token ({buffered!r}); it must end with ';'",
            error_code=8,
        )


def _payload(command: str) -> str:
    """Strip `STBLDAT` and the semicolon that dispatched it (§1, two-pass)."""
    text = command.strip()
    head, sep, rest = text.partition(";")
    if not sep:
        raise TableSyntaxError("no ';' in the table string", error_code=2)
    if head.strip().upper() not in ("", "STBLDAT"):
        raise TableSyntaxError(f"not an STBLDAT command: {head.strip()!r}", error_code=1)
    return rest


class _Status(enum.Enum):
    START = "start"
    PROCESSED = "processed"
    NEW_NAMED = "new-named"
    NEW_TABLE = "new-table"
    END = "end"


class _Compiler:
    """A readable mirror of `Table.cpp: ParseTableCommand()` and `ParseEntry()`."""

    def __init__(self, payload: str) -> None:
        self._tokens = _tokenize(payload)
        self.nesting = 0
        self.tables_loaded = 0
        self.tables: list[Table] = []

    # -- token helpers, one per firmware helper of the same name -----------

    def next(self) -> str:
        try:
            return next(self._tokens)
        except StopIteration:
            raise TableSyntaxError(
                "table string ended early; the box would time out waiting", error_code=8
            ) from None

    def expect(self, char: str, code: int) -> None:
        token = self.next()
        if token[0] != char:
            raise TableSyntaxError(f"expected {char!r}, got {token!r}", error_code=code)

    def token_int(self) -> int:
        token = self.next()
        try:
            return int(token, 10)
        except ValueError:
            # sscanf leaves the destination untouched on a failed conversion,
            # which in the firmware means an uninitialised value gets used.
            raise TableSyntaxError(f"expected an integer, got {token!r}", error_code=2) from None

    def token_float(self) -> float:
        token = self.next()
        try:
            return float(token)
        except ValueError:
            raise TableSyntaxError(f"expected a number, got {token!r}", error_code=2) from None

    # -- the parse ---------------------------------------------------------

    def run(self) -> Compiled:
        status = _Status.START
        # `InitialOffset` in the firmware, which initialises it to zero and
        # never assigns it anything else. Kept so the mirror stays readable
        # against the source, not because it can vary.
        offset = 0
        count = 0
        token = ""
        while True:
            name, repeat = UNNAMED, 1
            points: list[TimePoint] = []
            if status is _Status.START:
                count = self.token_int()
                self.expect(":", 9)
                token = self.next()
            if token == "[" or status is _Status.NEW_NAMED:
                if status is _Status.START and count > 0:
                    # A non-zero leading offset becomes a table of its own,
                    # holding one time point that does nothing. §2.
                    self.tables.append(Table(UNNAMED, 1, count, (TimePoint(count, ()),)))
                if status is not _Status.NEW_NAMED:
                    self.nesting += 1
                name = ord(self.next()[0])
                self.expect(":", 9)
                repeat = self.token_int()
                self.expect(",", 19)
                count = self.token_int()
                self.expect(":", 9)
                token = self.next()
            max_count = offset + count

            while True:
                status, token = self._parse_entry(token, count, points)
                if status is _Status.PROCESSED:
                    count = self.token_int()
                    token = self.next()
                    if token != "]":
                        if token != ":":
                            raise TableSyntaxError(
                                f"expected ':' or ']' after a time point, got {token!r}",
                                error_code=9,
                            )
                        token = self.next()
                    max_count = offset + count
                    continue
                self.tables.append(Table(name, repeat, max_count, tuple(points)))
                if status is _Status.END:
                    self.tables_loaded += 1
                    return Compiled(tuple(self.tables), self.tables_loaded, self.nesting)
                self.tables_loaded += 1
                offset = 0
                if status is _Status.NEW_TABLE:
                    count = self.token_int()
                    self.expect(":", 9)
                    token = self.next()
                break

    def _parse_entry(
        self, token: str, count: int, points: list[TimePoint]
    ) -> tuple[_Status, str]:
        """One time point: `ParseEntry()`, which stops at whatever ends it."""
        entries: list[Entry] = []
        while True:
            head = token[0]
            if head == ",":
                points.append(TimePoint(count, tuple(entries)))
                return _Status.PROCESSED, token
            if head == ";":
                points.append(TimePoint(count, tuple(entries)))
                return _Status.END, token
            if head == "W":
                # A bare time point with no action; the parser just skips it.
                token = self.next()
                continue
            if head == "[":
                self.nesting += 1
                if self.nesting > MAX_NESTING:
                    raise TableSyntaxError(
                        f"table nesting deeper than {MAX_NESTING}", error_code=20
                    )
                points.append(TimePoint(count, tuple(entries)))
                return _Status.NEW_NAMED, token
            if head == "]":
                self.nesting -= 1
                if self.nesting < 0:
                    raise TableSyntaxError("']' without a matching '['", error_code=21)
                entries.append(Entry(_LOOP_END, 0, ValueKind.INT))
                points.append(TimePoint(count, tuple(entries)))
                token = self.next()
                if token[0] == ";":
                    return _Status.END, token
                return _Status.NEW_TABLE, token
            entries.append(self._parse_pair(token))
            token = self.next()
            if token[0] == ":":
                token = self.next()

    def _parse_pair(self, token: str) -> Entry:
        """One `Channel:Value`, split the way `ParseEntry()` splits it."""
        head = token[0]
        if head in "ABCDEFGHIJKLMNOPrstbcdpa><=":
            chan = ord(head)
            self.expect(":", 9)
            if head in "tbdp><=":
                return Entry(chan, self.token_int(), ValueKind.INT)
            return Entry(chan, ord(self.next()[0]), ValueKind.CHAR)
        # Everything else is read as a number: DC bias, RF, or ARB 101-108.
        try:
            number = int(token, 10)
        except ValueError:
            # The firmware's sscanf writes nothing here and the channel byte
            # comes from an uninitialised local, so the table is silently
            # wrong. Letters e-l for the ARB channels are the way to hit it.
            raise TableSyntaxError(
                f"unknown channel token {token!r}; ARB channels must be sent "
                "as the numerals 101-108, not as letters",
                error_code=22,
            ) from None
        self.expect(":", 9)
        value = self.token_float()
        if _is_dc_bias(number):
            return Entry((number - 1) & 0xFF, None, ValueKind.DAC)
        return Entry(number & 0xFF, _float_bits(value), ValueKind.FLOAT)

def _is_dc_bias(number: int) -> bool:
    """The firmware's own test for "convert this to DAC counts"."""
    if not (1 <= number <= 32 or number & (_RAMP | _INITIAL)):
        return False
    return ((number - 1) & 0x20) == 0


def _float_bits(value: float) -> int:
    return struct.unpack("<i", struct.pack("<f", value))[0]


def compile_table(command: str) -> Compiled:
    """Predict what a box will hold after loading `command`.

    `command` is a whole `STBLDAT;...;` string, as a method file stores it; a
    bare `;...;` payload is accepted too. Raises `TableSyntaxError` for a string
    the box's parser would reject, carrying the `GERR` code it would report.
    """
    compiler = _Compiler(_payload(command))
    return compiler.run()


# --------------------------------------------------------------------------
# Layout <-> bytes
# --------------------------------------------------------------------------

_HEADER = struct.Struct("<BiiI")
_POINT = struct.Struct("<iB")
_ENTRY = struct.Struct("<Bi")


def encode(compiled: Compiled) -> bytes:
    """Pack a predicted layout into the bytes `TBLRPT` should dump.

    DAC values are written as zero, since no host can know them; comparison
    treats them as unknown rather than as zero. The end-of-tables marker is the
    final byte, matching what the box leaves at the buffer position it stopped.
    """
    out = bytearray()
    for table in compiled.tables:
        out += _HEADER.pack(table.name, table.repeat, table.max_count, len(table.points))
        for point in table.points:
            out += _POINT.pack(point.count, len(point.entries))
            for entry in point.entries:
                out += _ENTRY.pack(entry.chan, entry.value or 0)
    out.append(END_OF_TABLES)
    return bytes(out)


def decode(data: bytes) -> tuple[Table, ...]:
    """Read a `TBLRPT` dump back into tables, stopping at the end marker.

    Trailing bytes after the marker are whatever the previous load left in the
    buffer (§2) and are ignored.
    """
    tables: list[Table] = []
    at = 0
    while at < len(data):
        if data[at] == END_OF_TABLES:
            return tuple(tables)
        if at + TABLE_HEADER_BYTES > len(data):
            raise ValueError(f"dump ends inside a table header at byte {at}")
        name, repeat, max_count, num_points = _HEADER.unpack_from(data, at)
        at += TABLE_HEADER_BYTES
        points: list[TimePoint] = []
        for _ in range(num_points):
            if at + TIME_POINT_BYTES > len(data):
                raise ValueError(f"dump ends inside a time point at byte {at}")
            count, num_entries = _POINT.unpack_from(data, at)
            at += TIME_POINT_BYTES
            entries: list[Entry] = []
            for _ in range(num_entries):
                if at + ENTRY_BYTES > len(data):
                    raise ValueError(f"dump ends inside an entry at byte {at}")
                chan, value = _ENTRY.unpack_from(data, at)
                at += ENTRY_BYTES
                kind = value_kind(chan)
                entries.append(Entry(chan, None if kind is ValueKind.DAC else value, kind))
            points.append(TimePoint(count, tuple(entries)))
        tables.append(Table(name, repeat, max_count, tuple(points)))
    raise ValueError("dump has no end-of-tables marker; ask TBLRPT for more bytes")


@dataclass(frozen=True, slots=True)
class Report:
    """A parsed `TBLRPT` dump: the preamble's three numbers and the bytes."""

    test_nesting: int
    tables_loaded: int
    struct_sizes: tuple[int, int, int]
    data: bytes

    @property
    def packing_matches(self) -> bool:
        """Whether the box's `sizeof`s are the ones §2 documents.

        A box saying anything else is running firmware packed differently, and
        every byte offset below is then wrong. Worth checking once per box.
        """
        return self.struct_sizes == (TABLE_HEADER_BYTES, TIME_POINT_BYTES, ENTRY_BYTES)


_PREAMBLE = (
    "TestNesting",
    "TablesLoaded",
    "Size of TableHeader",
    "Size of TableEntryHeader",
    "Size of TableEntry",
)


def parse_report(lines: list[str]) -> Report:
    """Read `TBLRPT`'s five-line preamble and its one-byte-per-line dump.

    Bytes are `printf("%x")`: lowercase, unpadded, no prefix. §4.
    """
    if len(lines) < len(_PREAMBLE):
        raise ValueError(f"TBLRPT reply has only {len(lines)} lines; the preamble is 5")
    numbers = []
    for label, line in zip(_PREAMBLE, lines, strict=False):
        prefix = label + " = "
        if not line.startswith(prefix):
            raise ValueError(f"expected a {label!r} line, got {line!r}")
        numbers.append(int(line[len(prefix) :]))
    data = bytearray()
    for line in lines[len(_PREAMBLE) :]:
        value = int(line, 16)
        if not 0 <= value <= 0xFF:
            raise ValueError(f"{line!r} is not a byte")
        data.append(value)
    return Report(numbers[0], numbers[1], tuple(numbers[2:5]), bytes(data))


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def differences(expected: Compiled, actual: tuple[Table, ...]) -> list[str]:
    """Every way the box's parse differs from the prediction, in plain words.

    An empty list is the round trip passing. DC bias values are compared only
    for presence, never for content, because they are calibration-dependent.
    """
    problems: list[str] = []
    if len(expected.tables) != len(actual):
        problems.append(f"table count: predicted {len(expected.tables)}, box has {len(actual)}")
    for i, (want, got) in enumerate(zip(expected.tables, actual, strict=False)):
        where = f"table {i} ({want.label})"
        if want.name != got.name:
            problems.append(f"{where}: name predicted {want.label}, box has {got.label}")
        if want.repeat != got.repeat:
            problems.append(f"{where}: repeat predicted {want.repeat}, box has {got.repeat}")
        if want.max_count != got.max_count:
            problems.append(
                f"{where}: max count predicted {want.max_count}, box has {got.max_count}"
            )
        if len(want.points) != len(got.points):
            problems.append(
                f"{where}: predicted {len(want.points)} time points, "
                f"box has {len(got.points)}"
            )
        for j, (wp, gp) in enumerate(zip(want.points, got.points, strict=False)):
            point = f"{where}, time point {j}"
            if wp.count != gp.count:
                problems.append(f"{point}: tick predicted {wp.count}, box has {gp.count}")
            if len(wp.entries) != len(gp.entries):
                problems.append(
                    f"{point}: predicted {len(wp.entries)} channels, box has {len(gp.entries)}"
                )
            for we, ge in zip(wp.entries, gp.entries, strict=False):
                if we.chan != ge.chan:
                    problems.append(
                        f"{point}: channel predicted {channel_token(we.chan)}, "
                        f"box has {channel_token(ge.chan)}"
                    )
                elif we.kind is ValueKind.DAC:
                    if ge.value is not None:
                        problems.append(
                            f"{point}: {channel_token(we.chan)} should hold a DAC frame"
                        )
                elif we.value != ge.value:
                    problems.append(
                        f"{point}: {we.describe()} predicted, box has {ge.describe()}"
                    )
    return problems
