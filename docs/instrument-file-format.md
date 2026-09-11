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

[calibration]
slope = 0.738123
intercept = 0.07690495
measured = 2026-09-09

[vertical]
full_scale_v = 0.5
offset_v = 0.251
```

- `schema_version` pins the document to the shape this section describes. Clockwork rejects a
  version it does not recognize rather than guess at an unknown layout.
- `instrument.name` is written into the finished file as `InstrumentName`, the standard UIMF
  global parameter, so a tool that has never heard of clockwork still reports which machine
  acquired the data.
- `calibration` is the pair a frame carries as `CalibrationSlope` and `CalibrationIntercept`,
  plus the day they were determined. A calibration with no date cannot be told from one nobody
  has checked, which is why the date is part of the document.
- `vertical` is the full scale and the channel offset the acquisition ran at, in volts.

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

Two properties follow from `t` being counted in bins from the start of the digitizer record, and
both matter when a pair measured on one acquisition chain is carried to another.

**The sample rate does not enter the calibration.** A bin index multiplied by the bin width is a
time, so doubling the sample rate doubles the index, halves the width, and leaves the same ion at
the same `t`. A pair measured at 1 GS/s is the same pair at 2 GS/s.

**The post-trigger delay does.** A UIMF file declares its post-trigger delay as `TimeOffset` and
UIMF-Library then does not apply it, so a delay lengthened by `d` microseconds moves every ion
later by `d` and `intercept` has to fall by `d` to put the mass axis back. A pair carried across a
change of delay is a starting value rather than a calibration.

Digitizer control software often states the same calibration in tenths of a nanosecond instead of
microseconds, as `K / 1e4` and `T0 * 1e4`. The two forms are identical and the lab's calibration
reads as `7.38123E-05` and `769.0495` in one and `0.738123` and `0.07690495` in the other.
`clockwork.instrument.Calibration.from_tenths_of_ns()` converts, and is the only thing that says
which form a pair typed out of such a file is in.

## Vertical settings

Two files acquired through different ranges, or on either side of an attenuator, are otherwise
indistinguishable once they leave the instrument. Recording the window is what tells them apart,
so both numbers are stamped into every file the acquisition writes, under
`ClockworkChannelOffset` and `ClockworkFullScale`.

The offset is the value clockwork sends with its own `vertical` command, and the acquisition
console confirms it. The full scale is a `config.txt` key the console reads at startup and does
not report back, so `full_scale_v` is the value the lab configured rather than one the card
confirmed. The stamp says as much in the parameter's own description. Note that a console running
a different `config.txt` from the one this document describes will acquire through a window the
file does not name; `clockwork.acq.run` compares the offset the console actually holds against
the document and warns when the two disagree, which catches the same drift on the one setting
that is observable.

## Validation

`clockwork.instrument.load()` and `.loads()` collect every problem in a document before raising
`InstrumentError`, so a document with several mistakes reports all of them in one pass. Rejected:
an unrecognized `schema_version`, a key the schema does not define, a value of the wrong type, a
number that is not finite, a negative `slope`, a `full_scale_v` that is not positive, and a
`measured` that is not a date. A negative `offset_v` is accepted, because the offset is a position
within the window rather than a size.

What the SA220P itself accepts for full scale is a property of the card and of the console that
drives it, and it belongs in [console-protocol.md](console-protocol.md). This document does not
restate it, and `clockwork.instrument` does not enforce it.
