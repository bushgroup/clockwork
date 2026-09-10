# clockwork

Control software for the Bush lab's SLIMPHONY-style ion mobility mass spectrometer. Clockwork
sends pulse-sequence strings to the MIPS controller boxes that drive the SLIM boards, runs the
Keysight/Acqiris SA220P digitizer through PNNL's AqMD3 acquisition console, and writes UIMF files
that [mainspring](https://github.com/bushgroup/mainspring) reads. It replaces FALKOR for the
experiments this lab runs.

The first version does three things and does them from one window: load a method (the strings each
box needs and the acquisition settings), send it to every box, and acquire. A technical replicate
is one button.

## Status

Pre-alpha, and nothing has been acquired with it yet. What exists: the protocol documents, the
self-check, `clockwork.mips` (send strings and pulse-sequence tables to a box, follow what it
reports), `clockwork.method` (load, validate and stamp a method) and `clockwork.acq` (drive the
acquisition console through a whole acquisition, create the UIMF file it appends to, and sum a
method frame's repetitions into a companion file). What does not: the window. Both of the lower
layers ship a stand-in for the hardware they talk to, so a clone with no instrument can run
everything the self-check runs, the whole UIMF path included. Documents:

- [`docs/mips-wire-format.md`](docs/mips-wire-format.md): how a MIPS box takes a pulse-sequence
  table, times it, and reports back, derived from the public firmware.
- [`docs/console-protocol.md`](docs/console-protocol.md): the ZeroMQ command set of PNNL's
  AqMD3 acquisition console, derived from its source.
- [`docs/method-file-format.md`](docs/method-file-format.md): the flat TOML method a trainee
  loads, and the stamp that traces an acquisition back to it.
- [`docs/glossary.md`](docs/glossary.md): what the terms mean.

## The two files an acquisition writes

Clockwork acquires one UIMF frame per ion mobility experiment, so a method frame of 100
repetitions is 100 frames in the file. That file keeps the plain name and grows during the run.
After each method frame, clockwork sums its repetitions scan by scan and writes the total as one
frame of a companion file, which is the shape older software wrote and the shape PNNL's tools
open.

```
260910_BK_001.uimf          one frame per repetition, Accumulations = 1
260910_BK_001.summed.uimf   one frame per method frame, Accumulations = 100
```

Keeping both is the default. The per-repetition file is the only record of how one repetition
differed from the next, which is what a correction for arrival-time drift between repetitions
would need, so discarding it is a setting a method has to ask for. Every frame of both files
carries the method frame and repetition it came from, the digitizer's bit depth, and the method
that produced it, text and hash. A frame is marked complete only once clockwork has finished
writing it, so a run cut short by a power loss opens, and the frame it was writing reads as
unfinished rather than as damaged.

## Running from source

Requires [uv](https://docs.astral.sh/uv/) and Windows.

```
git clone https://github.com/bushgroup/clockwork
cd clockwork
git config core.hooksPath .githooks
uv sync --group dev
uv run tools/check_public.py
uv run pytest
```

The self-check needs no hardware: it passes on a machine with no MIPS box, no digitizer and no
acquisition console.

## License

BSD 3-Clause, copyright University of Washington. See `LICENSE`.
