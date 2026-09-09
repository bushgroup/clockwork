# The method file and provenance stamp

A method is what a trainee loads to run an experiment: the per-box command strings sent to each
MIPS box, the acquisition settings, the order in which the boxes are started, and the map from box
name to serial port. It replaces pasting strings into a terminal box by box and remembering which
of them start anything. Clockwork stores it as a flat [TOML](https://toml.io) document and reads it
with `clockwork.method`.

## Document

```toml
schema_version = 2

start = [["box2", "TARBTRG"], ["box3", "TARBTRG"], ["box1", "TBLSTRT"]]
reset = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]

[metadata]
name = "slim3-technical-replicate"
created = 2026-09-08
description = "Standard technical replicate for SLIM3."

[acquisition]
frames = 1
scans = 5000
accumulations = 100
repetition_mode = "per_repetition"
keep_raw = true
file_stem = "replicate"

[[boxes]]
name = "box1"
port = "COM3"
setup = ["STBLCLK,EXT", "STBLTRG,POS"]
load = ["STBLDAT;0:A:1[A:1,783:15:10,806:16:37,5000:];"]
arm = ["SMOD,TBL"]

[[boxes]]
name = "box2"
port = "COM4"
setup = ["SWFREQ,1,15000", "SWFVRNG,1,15", "SARBCCLK,3,TRUE", "ARBSYNC"]
load = ["SARBCTBL,J22[HRsm2CD5m2ND10r]1"]
arm = []

[[boxes]]
name = "box3"
port = "COM5"
setup = ["SWFREQ,1,15000", "SWFVRNG,1,15", "SARBCCLK,4,TRUE", "ARBSYNC"]
load = []
arm = []
```

- `schema_version` pins the document to the shape this section describes. Clockwork rejects a
  method whose `schema_version` it does not recognize rather than guess at an unknown layout.
  Version 1, which stored one flat list of strings per box and had no start sequence, is rejected
  with a message saying so.
- `metadata` names the method for a trainee choosing between saved methods, plus when it was
  written.
- `acquisition` carries the run: `frames`, `scans` and `accumulations`, the two settings below that
  say how those are divided into acquisition console frames and what survives on disk, and
  `file_stem`, the base name the UIMF file is written under.
- `[[boxes]]` is an array of tables, one per box, each naming the box, its COM port, and its
  strings in three phases.
- `start` and `reset` are the two ordered cross-box sequences, written as `[box, command]` pairs.

`start` and `reset` are top-level keys and belong above the first `[[boxes]]` table. TOML gives a
bare key to the table that precedes it, so a `start` written at the foot of the file becomes part
of the last box instead. Clockwork catches that particular mistake and names it, because the
document it produces is otherwise valid TOML.

This is deliberately not the Layer 1 experiment data model (a Pydantic schema, deferred to
version 2): where that will compile an experiment down to table strings, the method file only
stores strings a trainee already wrote. Clockwork validates none of them. The wire format they
must already conform to is [mips-wire-format.md](mips-wire-format.md).

## The three phases

To send a method without re-sending what the boxes already hold, clockwork divides each box's
strings by how long their effect lasts.

| Phase | Sent | Holds |
|---|---|---|
| `setup` | On demand, once per session or after a box is power cycled | The travelling-wave frequency and amplitude, which module follows the common clock, the table clock and trigger source: settings that persist in the box |
| `load` | Once per acquisition | The pulse-sequence table, a compression table: what changes when the experiment changes |
| `arm` | Once per acquisition, after `load` | `SMOD,TBL`, which puts the box in table mode and answers with an asynchronous `TBLRDY` |

Each phase is an array of strings sent in the order written, and any of the three may be empty. A
box that only ever needs its persistent block, as `box3` does above, carries a `setup` and nothing
else. At least one box in a method has something to `load`.

## Starting, and starting again

To begin an acquisition, clockwork walks the `start` list once, in order, sending each command to
the box the pair names. The order is part of the experiment rather than a convenience: an ARB box
told to run its compression table waits at the table's first halt instruction for a release edge,
and a box that has not reached that halt when the first edge arrives misses the whole first
repetition. Putting the two `TARBTRG` commands ahead of `TBLSTRT`, as the example does, is what
guarantees they are waiting.

A technical replicate walks `reset` and then `start` again, and sends neither `setup` nor `load`.
On the boxes this lab runs, `reset` returns the sequencer box to local mode and arms it again,
which leaves its table loaded and costs one round trip rather than a table upload. Note that
`SMOD,LOC` on a box that is already local answers with error 3; `clockwork.mips` treats that as
success, so a `reset` is safe to send whatever state the box is in.

`reset` may be empty or absent, which is the right shape for a method with nothing to repeat. An
empty `start` is rejected, because a method with nothing in its start list never begins.

## Two repetition modes

`accumulations` is how many ion mobility experiments are summed into one frame of the finished
file, and `repetition_mode` says how those experiments reach the acquisition console.

| `repetition_mode` | Console `frame_length` | Console frames per method frame | The start sequence runs |
|---|---|---|---|
| `per_repetition` | `scans` | `accumulations` | Once per repetition |
| `single_frame` | `scans * accumulations` | 1 | Once per method frame |

Under `per_repetition`, each ion mobility experiment is its own console frame, released by its own
start edge, and the box's table describes one experiment. Under `single_frame`, one console frame
covers the whole method frame, released once, and the table loops `accumulations` times on the
box. Either shape reduces to the same finished file: the fold step sums the rows by `ScanNum`
modulo `scans` and writes one summed frame.

`clockwork.method` computes `frame_length` and the console frame count from the mode, so the
number sent to the console is derived in one place rather than at each call site. The default is
`per_repetition`, which is the decision on record for this instrument; the measurement that would
change it is the console's per-repetition restart cost.

`keep_raw` decides what the fold step leaves on disk. When it is true, the default, the raw file
with one frame per repetition survives beside its summed companion. Per-repetition rows are the
only record of how one repetition differed from the next, and arrival times are known to shift
slightly between repetitions as chamber pressure fluctuates, so discarding them forecloses that
correction permanently.

## Provenance stamp

Every acquisition a method produces is stamped with the method that made it, so that a UIMF file
traces back to the exact strings sent to every box and the order they went in.
`clockwork.method.stamp()` builds the record:

| Field | Content |
|---|---|
| `method_name` | The method's `metadata.name` |
| `method_hash` | SHA-256 of the method's canonical TOML text |
| `method_text` | The method's canonical TOML text, in full |
| `clockwork_version` | The `clockwork` package version that ran the acquisition |
| `console_version` | The acquisition console's reported version, or `None` if unavailable |

Where these fields land in a finished UIMF file, `Global_Params` or a sidecar file beside it, is
decided by the writer that creates the file. A method's hash changes if and only if some field in
the document changes, which makes it a stable key for grouping acquisitions by the method that
produced them even before that writer exists.

## Validation

`clockwork.method.load()` and `.loads()` collect every problem in a document before raising
`MethodError`, so a method with several mistakes reports all of them in one pass rather than one
per fix. Rejected: an unrecognized `schema_version`, a missing or empty field, a key the schema
does not define, a duplicate box name, a box name or port with whitespace around it, a
`repetition_mode` that is not one of the two, a `keep_raw` that is not a boolean, a `file_stem`
containing a path separator, a `start` or `reset` step naming a box the method does not have, an
empty `start`, and a method in which no box has anything to `load`.

One class of problem is repaired rather than rejected. A command string with whitespace around it
is stripped, and the repair is reported in `Method.warnings`, one line per string, naming the
document path it came from. Trainee strings carry whatever whitespace their source file had, and a
stray tab at the end of a command is invisible in an editor and real on the wire. Stripping it
keeps the method loadable; the warning is what makes the change visible. Warnings take no part in
equality or in the canonical text, so a document that loaded with warnings still round-trips
through `dumps` and stamps to the same hash as the same method written cleanly.
