# The method template

A method template is a method with holes in its strings and the knobs that fill them. It exists
for the experiment a laboratory runs every week with a few numbers changed, where a hand edit
of the strings moves one number on one box and forgets the matching number on another.
Clockwork stores a template as a flat [TOML](https://toml.io) document in the same library as
its methods and reads it with `clockwork.method.template`. Rendering a template produces an
ordinary method of schema 2, and that rendered method is what is sent to the boxes and stamped
into the file. Nothing downstream of the method learns that a template was involved.

## Document

```toml
template_schema = 1
renders = 2

start = [["box2", "TARBTRG"], ["box1", "TBLSTRT"]]
reset = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]

[knobs]
pulse_ms = { default = 1.5, min = 0.5, max = 5.0, unit = "ms", description = "how long line A is high" }
cycles = { default = 10, min = 1, max = 100, unit = "", description = "how many times the table repeats" }
wait_ms = { default = 10.0, min = 1.0, max = 50.0, unit = "ms", description = "the compression table's wait" }

[labels]
sample = { required = true, description = "what was sprayed" }

[constants]
tick_us = 100.0
on_tick = 10

[derive]
off_tick = "on_tick + round(pulse_ms * 1000 / tick_us)"

[marks]
off = { ms = "off_tick * tick_us / 1000", description = "line A falls; a table event at tick n is taken to fall in record n" }

[metadata]
name = "two-box-example"
created = 2026-09-22
description = "A minimal two-box template."

[acquisition]
frames = 1
scans = 100
accumulations = 10
repetition_mode = "per_repetition"
keep_raw = true
file_stem = "two-box-example"

[[boxes]]
name = "box1"
port = "COM3"
setup = ["STBLCLK,EXT"]
load = ["STBLDAT;25:[A:{cycles},{on_tick}:A:1,{off_tick}:A:0,100:];"]
arm = ["SMOD,TBL"]

[[boxes]]
name = "box2"
port = "COM4"
setup = ["SWFREQ,1,15000"]
load = ["STWCTBL,J22[HRsm2CD5m2ND{wait_ms}r]10"]
arm = []
```

Everything from `[metadata]` down is a schema 2 method document as
[`method-file-format.md`](method-file-format.md) describes it, with two differences. The
`schema_version` key is replaced by `renders`, which names the method schema the template
renders to, and the command strings may carry holes. `template_schema` names the format of the
template itself; this document describes version 1. The five tables above the body, `knobs`,
`labels`, `constants`, `derive` and `marks`, belong to the template, and a template omits any
it does not need.

## Knobs

A knob is one number the person running the experiment sets: a default, the range it may take,
its unit, and a description in words. Rendering checks every knob against its range, inclusive
at both ends, and refuses a value outside it. This is the one check a template adds to the
method's own. A knob whose default, minimum and maximum are all written as whole numbers is an
integer knob and takes whole values only, so a cycle count cannot be set to 2.5. A knob left
unset renders at its default.

## Labels

A label names the data and renders nothing. The sample is the usual one. A label marked
`required` must be given at render time. A label is recorded with the run rather than sent to
any box, and a derivation that names one is refused.

## Constants and derivations

Constants are the numbers the template holds fixed and gives names to, so that a derivation
reads as the arithmetic a person would do by hand. Derivations are expressions over knobs,
constants and other derivations, written in a vocabulary of names, numbers, `+`, `-`, `*`, `/`,
parentheses and `round()`, and nothing else. Operator precedence is the usual one. The order
in which derivations are written does not matter: clockwork works out the order of evaluation
and refuses a cycle, naming every derivation on it.

Two rules keep whole numbers whole. `round()` rounds to the nearest whole number with halves
away from zero, so `round(2.5)` is 3, whereas the Python function of the same name would give
2. Addition, subtraction, multiplication and `round()` of whole numbers give whole numbers, and
division always gives a fraction, so a tick count computed without `round()` is refused where it
lands rather than silently truncated.

The vocabulary is small on purpose. A template is a document the people who run the instrument
edit, and a derivation they cannot read is one nobody will correct.

One constant has a meaning of its own. `tick_us` is the pusher period in microseconds that the
template assumes when it converts between a box that counts pusher pulses and a box that counts
its own milliseconds. The instrument can contradict it: if the pusher period moves, the events
of one box move in time and those of the other do not. A rendered run carries the value it
assumed so that a reader can set it beside the period the digitizer measured. A template that
declares marks must declare `tick_us`.

## Marks

A mark is a derived moment that a rendered run records, in milliseconds and as an expected
scan, i.e., `round(ms * 1000 / tick_us)` counted from tick 0 of one ion mobility experiment.
The expected scan takes a sequencer event at tick *n* to fall in record *n*. Whether the event
governs record *n* or record *n* + 1 has not been measured on this instrument, so a mark's
description states which convention it assumed, as the example above does.

## Holes

A hole is a bare name in braces, `{duration_ms}`, standing for a knob, a constant or a
derivation. Holes are filled only in the boxes' `setup`, `load` and `arm` strings and in the
`start` and `reset` commands. A hole anywhere else is refused, and so is arithmetic inside the
braces, which belongs in `[derive]`.

How a number is written into a string is the wire format's decision, not the template's. Every
number is written in its shortest form with at most four decimals, so a wait of `208.0000` is
written `208` and `16.7628` stays as it is, which is how the strings are written by hand. Where
the wire format counts in whole units the hole must come out whole: a time point's tick count
and a table's cycle count in an `STBLDAT` table
([`mips-wire-format.md`](mips-wire-format.md) §2), and a compression table's loop count (§6.6).
A count may not be negative, because a negative count makes the time point dynamic (§2), and no
number in a compression table may be negative, because the mini-language's numbers start with a
digit (§5). A loop count of 0 is refused because the table would never terminate.

## Rendering

Loading a template parses the document, checks every name, expression and hole, and renders it
once at the knobs' defaults, so a template whose own defaults produce an invalid method is
refused when it is opened rather than when someone first turns a knob. Rendering at chosen
knob values fills the holes, the unset knobs at their defaults, and returns the method together
with the knob values, the derived values, the marks, the template's SHA-256 hash and text, and
the `tick_us` it assumed. The rendered method passes through the same validation as a
hand-written method, and the acquisition loop's refusals apply to it unchanged.

To trust a template written from an existing method, render it at its defaults and compare the
result with the method it came from, string for string and by the provenance stamp's hash. The
two are equal for a correct template, and the comparison is the first thing to run after any
edit to it.

A template handed to the method loader as if it were a method is refused with a sentence
saying so, since a library directory holds both kinds of document.

## What a rendered run records

The rendered method's text and hash are stamped into every file exactly as they are for a
hand-written method, under the parameters
[`method-file-format.md`](method-file-format.md#provenance-stamp) lists. The template's hash and
text, the knob values, the labels, the marks and the assumed pusher period are the next
addition to that stamp, one typed parameter each, so that a table of a project's files can be
built from the files alone.
