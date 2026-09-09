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
acquisition console through a whole acquisition). What does not: the creation of the UIMF file
the console appends to, the step that sums a frame's repetitions, and the window. Both of the
lower layers ship a stand-in for the hardware they talk to, so a clone with no instrument can
run everything the self-check runs. Documents:

- [`docs/mips-wire-format.md`](docs/mips-wire-format.md): how a MIPS box takes a pulse-sequence
  table, times it, and reports back, derived from the public firmware.
- [`docs/console-protocol.md`](docs/console-protocol.md): the ZeroMQ command set of PNNL's
  AqMD3 acquisition console, derived from its source.
- [`docs/method-file-format.md`](docs/method-file-format.md): the flat TOML method a trainee
  loads, and the stamp that traces an acquisition back to it.
- [`docs/glossary.md`](docs/glossary.md): what the terms mean.

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
