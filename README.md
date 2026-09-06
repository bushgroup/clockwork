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

Pre-alpha. The package skeleton, the self-check and the protocol documents exist; the sender, the
acquisition client and the window do not yet. Documents:

- [`docs/mips-wire-format.md`](docs/mips-wire-format.md): how a MIPS box takes a pulse-sequence
  table, times it, and reports back, derived from the public firmware.
- [`docs/console-protocol.md`](docs/console-protocol.md): the ZeroMQ command set of PNNL's
  AqMD3 acquisition console, derived from its source.

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
