# The instrument file

An instrument file records what a UIMF file has to state about the machine rather than about the
experiment: the m/z calibration that turns the bin axis into a mass axis, and the window channel 1
of the digitizer acquires through. None of those three numbers belongs to a method. The same
strings run after a recalibration, or through a different attenuator, are the same method and a
different file. Clockwork stores them as a flat [TOML](https://toml.io) document beside the method
and reads it with `clockwork.instrument`.

## Document

```toml
schema_version = 1

[instrument]
name = "SLIM3"
description = "SLIMPHONY, 20 dB attenuation after the preamplifier."
pusher_period_us = 129.0

[calibration]
slope = 0.738123
intercept = 0.07690495
measured = 2026-09-09

[vertical]
full_scale_v = 0.5
offset_v = 0.251
inverted = false

[standing]
method = "detection-response/method.toml"
steps = 5
dwell_s = 3
```

- `schema_version` pins the document to the shape this section describes. Clockwork rejects a
  version it does not recognize rather than guess at an unknown layout.
- `instrument.name` is written into the finished file as `InstrumentName`, the standard UIMF
  global parameter, so a tool that has never heard of clockwork still reports which machine
  acquired the data.
- `instrument.pusher_period_us` is the pusher period every run on this instrument expects, in
  microseconds. A method's tables count pusher triggers, so a method written for 129 µs runs
  every event at half its time behind a pusher set to 62 µs. Before the first frame the
  acquisition compares the period the console measured with the tick a rendered method declares,
  or with this value for a method written by hand, and refuses the run beyond a 10% difference
  and warns beyond 2%. Without this key a hand-written method's run is not checked, and the send
  log says so. The value is stamped into every file as `ClockworkPusherPeriodUs`, so the same
  comparison can be made on a file long after it was acquired.
- `calibration` is the pair a frame carries as `CalibrationSlope` and `CalibrationIntercept`,
  plus the day they were determined. A calibration with no date cannot be told from one nobody
  has checked, which is why the date is part of the document.
- `vertical` is the full scale and the channel offset the acquisition ran at, in volts, plus
  whether channel 1's data was inverted.
- `standing` is what the instrument holds between experiments and how it is brought there each
  morning and each evening ([below](#standing)).

Every table is optional, and so is the whole document. An acquisition given no instrument file
writes `CalibrationDone = 0` and stamps no vertical settings, which is what clockwork wrote before
this document existed. A file acquired that way carries a bin axis and no mass axis, and it can be
calibrated afterwards from its own parameters.

## Calibration

UIMF-Library computes m/z from the frame's pair as

```
mz = (slope * (t - intercept))^2,  t = bin * BinWidth_ns / 1000
```

with `t` in microseconds. Clockwork implements that formula nowhere. The calibration reaches a
file as two numbers and is applied by the reader, which for this lab is
[mainspring](https://github.com/bushgroup/mainspring).

**Bin 0 is the trigger, not the first digitized sample.** A digitizer given a post-trigger delay
waits that long after each trigger before it records anything, and the writers put the wait into
the axis rather than leaving it out. The file declares it as `TimeOffset`, `Bins` counts the record
plus the delay, and every scan's leading zero run is `TimeOffset / BinWidth` bins longer than the
gap the card actually found. A stored bin index therefore counts from the trigger already, and
`t = bin * BinWidth_ns / 1000` is time since the trigger. This is why UIMF-Library declares
`TimeOffset` and then does not apply it: applying it would count the delay a second time.

Two properties follow, and both matter when a pair measured on one acquisition chain is carried to
another.

**The sample rate does not enter the calibration.** A bin index multiplied by the bin width is a
time, so doubling the sample rate doubles the index, halves the width, and leaves the same ion at
the same `t`. A pair measured at 1 GS/s is the same pair at 2 GS/s.

**Neither does the post-trigger delay.** Lengthening it by `d` microseconds trades `d` of record
for `d` more leading bins and leaves every ion on the bin it was already on. What the pair does
carry is the rest of the chain: where the trigger edge sits relative to the pusher pulse, and how
long the digitizer takes to start on it. The intercept absorbs both, so two chains that differ in
either need their own pair, and a pair carried across a change of digitizer is a starting value
rather than a calibration. One ion measured on two of them, one at a 10 microsecond delay and
2 GS/s and the other at 20 microseconds and 1 GS/s, came out 20 nanoseconds apart rather than the
10 microseconds the delays differ by (lab record, task 45).

Digitizer control software often states the same calibration in tenths of a nanosecond instead of
microseconds, as `K / 1e4` and `T0 * 1e4`. The two forms are identical and the lab's calibration
reads as `7.38123E-05` and `769.0495` in one and `0.738123` and `0.07690495` in the other.
`clockwork.instrument.Calibration.from_tenths_of_ns()` converts, and is the only thing that says
which form a pair typed out of such a file is in.

## Vertical settings

Two files acquired through different ranges, on either side of an attenuator, or through
opposite inversions, are otherwise indistinguishable once they leave the instrument. Recording
the window is what tells them apart, so all three are stamped into every file the acquisition
writes, under `ClockworkChannelOffset`, `ClockworkFullScale` and `ClockworkInverted`.

The offset and the inversion are the values clockwork sends with its own `vertical` and `invert`
commands; the acquisition console acknowledges both and reports neither back, so the document is
the only record of them and the stamp is written from it directly. The full scale is a
`config.txt` key the console reads at startup and does not report back either, so `full_scale_v`
is the value the lab configured rather than one the card confirmed — except that since task 24 a
forked console's `info` reply does carry the full scale actually in force, and that value is
preferred over the document's when it is available. The stamp says which source each field came
from in the parameter's own description. Note that a console running a different `config.txt`
from the one this document describes will acquire through a window the file does not name;
`clockwork.acq.loop._vertical_warnings` compares the offset and the inversion the console was
actually sent, and the full scale it reports, against the document and warns when any of them
disagree, which catches drift on the settings that are observable.

Inversion happens in the digitizer's own channel path, ahead of the zero-suppress gate
(`console-protocol.md`), so the fixed zero-suppress threshold keeps the same meaning — the same
excursion, positive-going after inversion — whichever way this is set. What changes is which side
of zero the acquisition was looking at, and `inverted` is the only place a file says which.

## Standing

`standing.method` names the method whose `setup` phase is the instrument's standard stack: the DC
bias setpoints, the RF heads and the traveling-wave ranges the instrument runs on. It is a path in
the method library or an absolute path. `clockwork warm-up` sends that phase, and only that phase,
each morning, and `clockwork stand-down` brings the same settings to zero each evening
([command line](command-line.md#the-two-ends-of-a-day)). Naming an existing method rather than
restating its numbers keeps one document for the stack, so the morning cannot disagree with the
method a stack audit ([routines](routines.md)) judges the boxes against.

Both verbs ramp every DC bias channel, RF drive level and ARB range rather than stepping it. Each
of `steps` equal steps moves every setting a fraction `1/steps` of the way from what its box read
back to its target, and is followed by a wait of `dwell_s` seconds, so the defaults of 5 steps and
3 s take about 15 s in each direction. A step is under a second of serial commands, which makes
the dwell the ramp. Both numbers are the document's so that the lab can change them without a new
build. Without a `standing` table, or with one that names no method, `warm-up` is refused and
`stand-down` still works: it zeroes whatever the boxes report having.

## Validation

`clockwork.instrument.load()` and `.loads()` collect every problem in a document before raising
`InstrumentError`, so a document with several mistakes reports all of them in one pass. Rejected:
an unrecognized `schema_version`, a key the schema does not define, a value of the wrong type, a
number that is not finite, a negative `slope`, a `full_scale_v` or `pusher_period_us` that is not
positive, a `measured` that is not a date, a `steps` that is not a whole number of at least 1,
and a negative `dwell_s`. A negative `offset_v` is accepted, because the offset is a position
within the window rather than a size.

What the SA220P itself accepts for full scale is a property of the card and of the console that
drives it, and it belongs in [console-protocol.md](console-protocol.md). This document does not
restate it, and `clockwork.instrument` does not enforce it.
