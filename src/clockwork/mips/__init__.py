"""USB-serial link to MIPS controller boxes.

Sends command strings and `STBLDAT` pulse-sequence tables, and parses what comes
back: the ACK (0x06) / NAK (0x15) bytes, `?` error lines, and the asynchronous
`TBLRDY` / `TBLTRIG` / `TBLCMPLT` / `ABORTED` status lines a table run emits.
Wire facts are in `docs/mips-wire-format.md`; this package cites it and never
redefines it. Filled by the lab record's task 04. No Qt here.
"""
