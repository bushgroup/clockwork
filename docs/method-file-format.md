# The method file and provenance stamp

A method is what a trainee loads to run an experiment: the per-box command strings sent to each
MIPS box, the acquisition settings, and the map from box name to serial port. It replaces pasting
two strings per box per acquisition into a terminal by hand. Clockwork stores it as a flat
[TOML](https://toml.io) document and reads it with `clockwork.method`.

## Document

```toml
schema_version = 1

[metadata]
name = "slim3-technical-replicate"
created = 2026-09-06
description = "Standard technical replicate for SLIM3."

[acquisition]
frames = 1
scans = 100
accumulations = 10
file_stem = "replicate"

[[boxes]]
name = "box1"
port = "COM3"
strings = ["STBLCLK,EXT", "STBLDAT;25:[A:10,10:A:1,25:A:0:5:34.5,100:];"]

[[boxes]]
name = "box2"
port = "COM4"
strings = ["STBLCLK,EXT", "STBLDAT;0:[A:10,50:A:1,100:];"]
```

- `schema_version` pins the document to the shape this section describes. Clockwork rejects a
  method whose `schema_version` it does not recognize rather than guess at an unknown layout.
- `metadata` names the method for a trainee choosing between saved methods, plus when it was
  written.
- `acquisition` carries `frames`, `scans` and `accumulations` for the run, and `file_stem`, the
  base name the UIMF file is written under. It says nothing about what a scan or an accumulation
  means on the wire; that is `../clockwork-lab/notes/accumulations.md`'s question, not this
  document's.
- `[[boxes]]` is an array of tables, one per box, in the order clockwork sends them. Each entry
  names the box, its COM port, and the exact command strings sent to it in send order: the two
  strings a trainee pastes today, unchanged. `clockwork.mips` streams each `strings` entry to the
  box named `name` on the port named `port` and validates none of it; the wire format the strings
  must already conform to is `mips-wire-format.md`.

This is deliberately not the Layer 1 experiment data model (a Pydantic schema, deferred to v2):
where that will compile an experiment down to table strings, the v1 method file only stores
strings a trainee already wrote.

## Provenance stamp

Every acquisition a method produces is stamped with the method that made it, so that a UIMF file
traces back to the exact strings sent to every box. `clockwork.method.stamp()` builds the record:

| Field | Content |
|---|---|
| `method_name` | The method's `metadata.name` |
| `method_hash` | SHA-256 of the method's canonical TOML text |
| `method_text` | The method's canonical TOML text, in full |
| `clockwork_version` | The `clockwork` package version that ran the acquisition |
| `console_version` | The acquisition console's reported version, or `None` if unavailable |

Where these fields land in a finished UIMF file, `Global_Params` or a sidecar file beside it, is
decided by the writer that creates the file. A method's hash changes if and only if any field
in the document changes, which makes it a stable key for grouping acquisitions by the method that
produced them even before that writer exists.

## Validation

`clockwork.method.load()` and `.loads()` collect every problem in a document before raising
`MethodError`, so a method with several mistakes reports all of them in one pass rather than one
per fix: an unrecognized `schema_version`, a missing or empty field, a duplicate box name, an
empty `strings` list, or a `file_stem` containing a path separator.
