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
enable = { box = "box1", channel = "A" }

[[boxes]]
name = "box1"
port = "COM3"
setup = ["STBLCLK,EXT", "STBLTRG,POS"]
load = ["STBLDAT;0:A:1[A:1,783:15:10,806:16:37,5000:];"]
arm = ["SMOD,TBL"]

[boxes.dc_bias]
15 = 0.0
16 = 5.0

[boxes.rf.1]
frequency_hz = 943000
drive_pct = 50.0
mode = "MANUAL"

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

A `[boxes.dc_bias]` or `[boxes.rf.<n>]` table belongs to the `[[boxes]]` table above it, so it goes
between that box and the next one. Box 1 above is the box with the DC bias and RF boards in it; the
ARB boxes have neither.

- `schema_version` pins the document to the shape this section describes. Clockwork rejects a
  method whose `schema_version` it does not recognize rather than guess at an unknown layout.
  Version 1, which stored one flat list of strings per box and had no start sequence, is rejected
  with a message saying so.
- `metadata` names the method for a trainee choosing between saved methods, plus when it was
  written.
- `acquisition` carries the run: `frames`, `scans` and `accumulations`, the two settings below that
  say how those are divided into acquisition console frames and what survives on disk,
  `file_stem`, the base name the UIMF file is written under, and `enable`, which names the digital
  output that gates the digitizer.
- `[[boxes]]` is an array of tables, one per box, each naming the box, its COM port, its
  strings in three phases, and optionally the DC bias and RF settings it should hold.
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

## Comments

A method carries the trainee's own comments as ordinary strings. A string whose first non-blank
character is `#` is a comment: it is stored in the phase array it precedes, clockwork never sends
it to a box, and the provenance stamp hashes it with everything else.

```toml
setup = [
    "# the travelling-wave frequency, all four channels at once",
    "SWFREQ,1,15000",
    "SWFREQ,2,15000",
]
```

A comment's position is what carries its meaning, since what it labels is the block of strings
under it, so it is stored where it was written rather than in a table of its own.
`clockwork.method.is_comment` is the one predicate that decides, and every part of clockwork that
puts a string on a wire skips through it. Whitespace around a comment is stripped without a
warning, whereas whitespace around a command is stripped and reported, because only the command's
reaches a box.

Two consequences follow from a comment being a string. A method whose `load` phase holds nothing
but comments has nothing to load and is rejected, as is one whose `start` list holds nothing but
comments. And two methods that differ only in a comment stamp to different hashes, which is the
right answer: the comment is the trainee's record of what they meant the experiment to be.

## Panes

To write a method, a trainee fills one plain-text pane per box, one string per line, which is what
used to be pasted into the controller's terminal window. `clockwork.method.text` turns a pane into
the phases above and turns the phases back into the same text, so that nobody edits TOML by hand.

Classification decides when a string is sent, not what it means. `classify()` reads the command
word and nothing else: `STBLDAT` and `SARBCTBL` are `load`, `SMOD` is `arm` unless its argument is
`LOC`, `TARBTRG` and `TBLSTRT` are `start`, and everything else is `setup`, including a word
clockwork has never seen. What `setup` does with a string is send it once, in the order written,
so a word this package does not know costs a trainee nothing, whereas guessing it into `load` or
`start` would re-send it once per acquisition or once per frame. Nothing here reads a table's
ticks, a channel number or a compression table's operations. Those belong to the check above and
to the compiler planned for version 2.

Blank lines separate groups and are not kept. A comment labels the strings under it in its own
group, which is how the paste files have always been punctuated.

The cross-box start order is derived rather than typed. `start_order()` puts every `TARBTRG` ahead
of every `TBLSTRT` and keeps the method's box order within each, for the reason the section on
starting gives. Clockwork displays that order and a trainee never writes it.

### The two lines the command word cannot place

A `reset` is the complete list a replicate sends, and no command word says which `SMOD,LOC` is
part of one. So a comment written `# clockwork: reset` tags the group of lines that follows it,
and the same directive naming any other phase overrides the classification for that group.
Clockwork writes the directive itself when it renders a pane, and consumes it when it reads one
back, so a directive is never one of the method's strings. Where a pane carries no directive, a
group that begins `SMOD,LOC` and sits after the last start line is the reset, and clockwork
appends the box's `arm` phase where that group does not already end in it. Note that the appended
string is the only one clockwork supplies that a trainee did not write, and it is there because
the paste files state their reset as prose rather than as strings. A pane that clockwork rendered
carries both the directive and every string, so a method saved from the window says what its reset
is in as many words.

A line that is not a command at all is placed in no phase. Both experiments this format was
written against end in a sentence of prose, and one of them records the settings of the software
clockwork replaces in lines such as `Ion Mobility Scans = 5000`. A line whose first token is not a
bare alphanumeric word comes back as unplaced, for a trainee to tag by hand or to leave where it
is. The alternative is worse than useless: the rule that an unrecognized word is `setup` would
otherwise send a sentence of English to a box.

## The analog state a method may declare

The strings above are the experiment's timing. They say nothing about the DC biases that hold the
ions, or the RF that confines them, because a pulse sequence only moves a channel between values
the box already holds. Those values were set at a front panel, and until a method could declare
them a file said what was sent and nothing about what shaped the beam.

Two optional tables per box say them.

```toml
[boxes.dc_bias]
15 = 0.0
16 = 5.0

[boxes.rf.1]
frequency_hz = 943000
drive_pct = 50.0
mode = "MANUAL"
```

`dc_bias` maps a 1-based channel to a voltage. `rf` is one table per 1-based RF channel, with
`frequency_hz`, `drive_pct`, `voltage_v` and `mode` (`MANUAL` or `AUTO`), each optional.

Both are **partial by design**. A channel a method does not name is left exactly as the box had
it, and reported as left as found rather than silently zeroed. Nothing is range-checked here: the
board checks every value against its own limits and rejects one it cannot reach, and a host that
guessed those limits would refuse a method the instrument would have accepted.

Clockwork turns each declaration into ordinary setter strings and sends them at the **end** of the
`setup` phase, after the trainee's own strings, so the declaration is what the box is left
holding. `SDCB` may be sent in any mode, unlike `SDIO`; the RF setters change the voltage on a head
at the speed a serial command arrives, which is what a hand on the front panel does, so declaring
one is a decision rather than a formality.

Note that a channel a method declares **and** a pulse-sequence table drives ends up wherever the
table last put it. Declare the resting value, not the pulsed one.

## What the boxes were holding

Before a method is sent, clockwork reads every box's persistent state back with getters and
nothing else, and reads it again after the `setup` phase. Both readings go into the send log
beside the UIMF file, under each box's own name, and both are stamped into the file under
`ClockworkBoxState`. What is read is the identity and firmware, the channel counts, the DC bias
bank as both setpoints and monitor readings, every RF channel, the sequencer clock and status, and
each ARB module's frequency, range, direction, mode and alternate-waveform state.

Two things are reported from the comparison, and neither stops an acquisition:

- **Left as found.** Every ARB module setting the method's `setup` does not name, with what the box
  is holding. A setting nobody sets is whatever the last method left behind, which is how a
  transmission run inherited a reversed direction from the run before it and transmitted nothing.
- **Declared but not held.** Every DC bias or RF channel whose readback disagrees with what the
  method declared, with both numbers. A declared bias is compared against the setpoint the box
  reports; the monitor reading is compared against that setpoint instead, because it is a
  measurement of the output and not of the command.

Alongside them travels a free-text conditions note the operator writes, which goes in the send
log's header and into the file under `ClockworkConditions`. It is the part of an experiment no
getter reads: the sample, the MCP voltage, the pusher period, the pDRE setting, the collision
energy.

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

### Naming the gate line

`enable` is optional and names one digital output on one box:

```toml
enable = { box = "box1", channel = "A" }
```

The digitizer takes its acquisition gate from a level on a control input, and on this instrument
that level is DIOA on the sequencer box. Which line carries it is a fact about how the instrument
is cabled rather than anything a method's strings say, so a method that wants clockwork to move the
line declares it here. `box` names one of the method's own boxes and `channel` is a MIPS digital
output, `A` through `P`. A method that names a digital input, `Q` through `X`, is refused before
anything reaches a box: the firmware acknowledges `SDIO,Q,0` and drives output `I` with it, so
nothing downstream would catch the mistake.

Declaring it is what makes `single_frame` with more than one frame acquirable. That mode's table
loops on the box and raises the gate at its first tick, and nothing in it lowers the gate again, so
the second method frame would be released against a gate that is already high and would begin
recording before its start sequence ran. With the line named, clockwork puts the box in local mode,
clears the line and arms the box again between method frames, and the trainee's table runs as
written. Without it, that combination is refused rather than acquired at an offset that is
invisible in the finished file.

The round trip through local mode is not a formality. A host `SDIO` sent while a box is in table
mode is acknowledged and does not move the line: the latch that applies the digital output image
belongs to the table engine's timer, so the write waits for the table's next event and lands as
much as a whole table period later. Measured on the bench, that was 76 ms at a table period of 500
ticks and 351 ms at 5000. Local mode is the only state in which the host decides when the line
moves, and it leaves the loaded table in place.

Declaring the line buys one more thing. It is how clockwork knows which of a table's digital events
to read when it checks a method against its own strings, below, so a method that names the gate has
its table checked for raising and lowering the gate where the mode requires. A method that leaves
the key out keeps the rest of that check and is told which part was skipped.

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

Those five fields land in the finished UIMF file's `Global_Params`, under parameter IDs clockwork
owns. Three more join them there from the [instrument file](instrument-file-format.md),
`ClockworkChannelOffset`, `ClockworkFullScale` and `ClockworkInverted`, which record the window the
acquisition ran through; and two more from the run itself, `ClockworkBoxState` and
`ClockworkConditions`, which record what the boxes were holding and what the operator said about
the rest of the instrument. A method's hash changes if and only if some field in the document changes, which makes it
a stable key for grouping acquisitions by the method that produced them. The file name is not one
of the fields: a technical replicate is the same method written to a different file, so every
replicate of one method stamps to the same hash.

## Validation

`clockwork.method.load()` and `.loads()` collect every problem in a document before raising
`MethodError`, so a method with several mistakes reports all of them in one pass rather than one
per fix. Rejected: an unrecognized `schema_version`, a missing or empty field, a key the schema
does not define, a duplicate box name, a box name or port with whitespace around it, a
`repetition_mode` that is not one of the two, a `keep_raw` that is not a boolean, a `file_stem`
containing a path separator, a `start` or `reset` step naming a box the method does not have, an
empty `start`, a method in which no box has anything to `load`, a `dc_bias` or `rf` key that is not
a channel number or is below 1, an `rf` `mode` that is neither `MANUAL` nor `AUTO`, a
`frequency_hz` that is not a whole number, and an `rf` channel table that declares no setting at
all.

One class of problem is repaired rather than rejected. A command string with whitespace around it
is stripped, and the repair is reported in `Method.warnings`, one line per string, naming the
document path it came from. Trainee strings carry whatever whitespace their source file had, and a
stray tab at the end of a command is invisible in an editor and real on the wire. Stripping it
keeps the method loadable; the warning is what makes the change visible. Warnings take no part in
equality or in the canonical text, so a document that loaded with warnings still round-trips
through `dumps` and stamps to the same hash as the same method written cleanly.

### The document against its own strings

Loading checks the document alone. A second check runs before anything is sent, because the counts
`[acquisition]` states are written a second time inside the strings and the two can part company.
The CLOCK experiment is the example: its sequencer table loops 100 times, each of its two
compression tables ends `]100`, and `accumulations` is 100, while the table's loop covers 5000
ticks and `scans` is 5000. Shortening a run by editing one of those numbers and leaving the rest
used to produce a file that looked ordinary and was not. Under `single_frame` a table that loops
fewer times than the method says fills a frame that never completes, and one that loops more fills
it early, after which the summed file adds the wrong pushes together with nothing reported
anywhere.

So `clockwork.acq.loop.refusals()` reads the counts back out of the strings and refuses a method
whose numbers contradict each other, naming both numbers and the string the second one came from.
It compares the sequencer table's loop count and loop period, and each compression table's pass
count, against `scans`, `accumulations` and the repetition mode: `single_frame` expects the loops
to run once per repetition for a whole method frame, `per_repetition` expects them to run once.

Where the method names the gate line, it also checks what the table does with it: that the table
raises the gate at all, that it raises it at the first tick, and that it lowers it a whole console
batch past the last counted scan, or leaves it up in the one case where nothing is left to offset.
A table that never raises the gate is worth its own sentence, because it reads as though it does.
A loop header is written `[A:1,` and names the table `A`; an event raising DIOA is written `A:1`
in a time point. A table reduced from a longer one by deleting events can lose the second and keep
the first, and acquires nothing at all.

A string clockwork could not read well enough to compare is a different matter, and is reported
through `clockwork.acq.loop.cautions()` as a warning rather than refused. A compression table is
not syntax checked by the box either, and a string that is unusual is not on that account wrong.
