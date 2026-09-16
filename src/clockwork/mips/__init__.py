"""USB-serial link to MIPS controller boxes.

Sends command strings and `STBLDAT` pulse-sequence tables, and parses what comes
back: the ACK (0x06) / NAK (0x15) bytes, the `GERR` codes behind a rejection,
and the asynchronous `TBLRDY` / `TBLTRIG` / `TBLCMPLT` / `ABORTED` status lines
a table run emits. Wire facts are in `docs/mips-wire-format.md`; this package
cites it and never redefines it. No Qt here, and every call blocks, so nothing
in it may run on the UI thread.

    box.py         one controller: commands, table loads, arming, status
    table.py       the compiled table layout, predicted and read back
    compressor.py  an ARB compression table, read as far as its loop counts
    state.py       what a box is holding, read back with getters only
    transport.py   a COM port, or a box simulated in this process
    wire.py        framing, error codes, the asynchronous status lines

A minimal session against a real box:

    from clockwork.mips import Box, TableEvent

    with Box.open("COM3") as box:
        print(box.version())
        box.local()
        box.command("STBLCLK,EXT")
        box.command("STBLTRG,POS")
        load = box.send_table("STBLDAT;25:[A:10,10:A:1,25:A:0,100:];")
        print(box.verify_table(load) or "table round-trips")
        box.arm()
        box.wait_for(TableEvent.COMPLETE, timeout=30)

and the same against no box at all, which is what the self-check runs:

    from clockwork.mips import Box, FakeBox

    box = Box(transport=FakeBox())

Filled by the lab record's task 04.
"""

from __future__ import annotations

from .box import (
    DEFAULT_CHUNK_BYTES,
    DEFAULT_CHUNK_GAP_S,
    DEFAULT_TIMEOUT_S,
    Box,
    BoxRejected,
    BoxTimeout,
    MipsError,
    TableLoad,
)
from .compressor import (
    COMPRESSION_COMMAND,
    compression_passes,
    compression_table,
)
from .table import (
    UNNAMED,
    Compiled,
    Entry,
    Report,
    Table,
    TableSyntaxError,
    TimePoint,
    ValueKind,
    compile_table,
    decode,
    differences,
    digital_events,
    encode,
    parse_report,
)
from .state import (
    ARB_MODULE_GETTERS,
    MAX_ARB_MODULES,
    BoxState,
    RfReading,
    describe,
    read_state,
)
from .transport import FakeBox, SerialTransport, Transport, open_serial
from .wire import (
    ACK,
    DIO_OUTPUTS,
    ERR_ALREADY_LOCAL,
    NAK,
    RING_BUFFER_BYTES,
    TOKEN_TIMEOUT_S,
    Kind,
    ResponseReader,
    TableEvent,
    Token,
    dio_command,
    error_text,
    table_event,
)

__all__ = [
    "ACK",
    "ARB_MODULE_GETTERS",
    "COMPRESSION_COMMAND",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_CHUNK_GAP_S",
    "DEFAULT_TIMEOUT_S",
    "ERR_ALREADY_LOCAL",
    "MAX_ARB_MODULES",
    "NAK",
    "RING_BUFFER_BYTES",
    "TOKEN_TIMEOUT_S",
    "Box",
    "BoxRejected",
    "BoxState",
    "BoxTimeout",
    "Compiled",
    "Entry",
    "FakeBox",
    "Kind",
    "MipsError",
    "Report",
    "ResponseReader",
    "RfReading",
    "SerialTransport",
    "Table",
    "TableEvent",
    "TableLoad",
    "TableSyntaxError",
    "TimePoint",
    "Token",
    "Transport",
    "UNNAMED",
    "DIO_OUTPUTS",
    "ValueKind",
    "compile_table",
    "compression_passes",
    "compression_table",
    "decode",
    "describe",
    "dio_command",
    "differences",
    "digital_events",
    "encode",
    "error_text",
    "open_serial",
    "parse_report",
    "read_state",
    "table_event",
]
