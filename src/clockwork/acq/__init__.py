"""Acquisition through PNNL's AqMD3 acquisition console.

A ZeroMQ client for the console's command socket and its live data stream
(`docs/console-protocol.md`), and the creation of the UIMF file -- schema,
`Global_Params`, `Frame_Params` -- that the console appends `Frame_Scans` rows
to. Filled by the lab record's tasks 03 and 06. No Qt here.
"""
