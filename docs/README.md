# Documentation

- [**MIPS wire format**](mips-wire-format.md). How a MIPS controller box takes a pulse-sequence
  table over its serial link, how it times and releases it, what it reports back, and how the ARB
  modules that drive the traveling-wave regions are switched from a table. Derived from the
  public firmware and cross-checked against the manufacturer's documents.
- [**Acquisition console protocol**](console-protocol.md). The ZeroMQ command set, message
  formats and data stream of PNNL's AqMD3 Acquisition Console for the SA220P digitizer, and the
  division of UIMF writing between the console and its client. Derived from the console's source.
- [**Method file and provenance**](method-file-format.md). The flat TOML document a trainee loads
  to run an experiment, and the stamp that traces every acquisition back to the method that
  produced it.

The top-level [`README.md`](../README.md) covers running from source.
