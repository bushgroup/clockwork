# clockwork

Clockwork loads a method, sends its pulse-sequence strings to every MIPS controller box over USB
serial, acquires on a Keysight/Acqiris SA220P digitizer through PNNL's AqMD3 acquisition console,
and writes UIMF files that [mainspring](https://github.com/bushgroup/mainspring) reads. It is
control software for ion mobility mass spectrometry on instruments built around those two pieces
of hardware, and it does all of it from one window, where a technical replicate is one button.

It was written for SLIMPHONY, the Bush lab's SLIM ion mobility mass spectrometer at the University
of Washington, which has run on clockwork since September 2026. SLIMPHONY exercises most of what
clockwork does: three MIPS boxes coordinated within a single acquisition, one of them raising the
start edge that gates the other two and the digitizer, all eight travelling wave regions driven by
ARB modules, and a library of saved methods a trainee chooses from rather than one fixed sequence.

## What it assumes

Clockwork does not abstract over instruments. It assumes:

- **MIPS controller boxes on USB serial**, one COM port per box, each addressed by name. The
  number of boxes is whatever the method declares.
- **A free-running pusher trigger** clocking both the digitizer and every box's pulse-sequence
  table. Clockwork does not generate it and does not divide it.
- **One box's table raising the start edge**, into the digitizer's Control I/O and into the other
  boxes' R inputs. That edge is what begins an ion mobility experiment, and no other source is
  supported.
- **An SA220P driven through the AqMD3 acquisition console** in zero-suppress mode. The console is
  a C++ ZeroMQ server, which clockwork launches and drives over the socket. The 8-bit U1084A is
  not supported.
- **Windows.** The instrument PCs run Windows 11 LTSC and the installer is the primary
  deliverable. Nothing here is written to break elsewhere, but nothing else is tested.

## A method

A method is a flat TOML document holding the strings each box needs, in three phases, the ordered
cross-box sequences, the acquisition settings and the port map. Clockwork sends strings that were
already written for the box and validates none of them.

```toml
[acquisition]
frames = 1
scans = 5000
accumulations = 100
repetition_mode = "per_repetition"
enable = { box = "box1", channel = "A" }

[[boxes]]
name = "box1"
port = "COM3"
setup = ["STBLCLK,EXT", "STBLTRG,POS"]
load = ["STBLDAT;0:A:1[A:1,783:15:10,806:16:37,5000:];"]
arm = ["SMOD,TBL"]
```

Every acquisition stamps the method that produced it, text and hash, into the file it writes.
[`docs/method-file-format.md`](docs/method-file-format.md) is the whole document.

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
that produced it. A frame is marked complete only once clockwork has finished writing it, so a run
cut short by a power loss opens, and the frame it was writing reads as unfinished rather than as
damaged.

A run also leaves two text files beside the UIMF pair: a send log, `<stem>.sent.txt`, holding every
string the run sent and what each box said back, filtered down to what a trainee reads at the
bench, and a wire transcript, `<stem>-<date>.transcript.log`, the same traffic unfiltered, byte for
byte. The send log is where to start when something goes wrong; the transcript is the forensic
record behind it.

## Documents

- [`docs/mips-wire-format.md`](docs/mips-wire-format.md): how a MIPS box takes a pulse-sequence
  table, times it, and reports back, derived from the public firmware.
- [`docs/console-protocol.md`](docs/console-protocol.md): the ZeroMQ command set of PNNL's
  AqMD3 acquisition console, derived from its source.
- [`docs/method-file-format.md`](docs/method-file-format.md): the flat TOML method a trainee
  loads, and the stamp that traces an acquisition back to it.
- [`docs/instrument-file-format.md`](docs/instrument-file-format.md): the flat TOML document
  beside the method that records the machine's own calibration and vertical settings.
- [`docs/glossary.md`](docs/glossary.md): what the terms mean.
- [`docs/user-guide.md`](docs/user-guide.md): the installed window, from the two vendor installs
  it needs to a first acquisition.

## Installing

On an instrument PC the deliverable is a per-user Windows installer, built from
[`packaging/`](packaging/README.md). One installer carries both clockwork and the acquisition
console, puts them in the same folder, and needs no administrator rights. Two vendor products have
to be on the machine before it runs: Keysight IO Libraries Suite, then Acqiris MD3, in that order.
[`docs/user-guide.md`](docs/user-guide.md) covers both of those, the install, and a first
acquisition.

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
acquisition console. Both lower layers ship a stand-in for the hardware they talk to, so a clone
with no instrument can run everything the self-check runs, the whole UIMF path included, and
`uv run clockwork --fake` opens the real window over those stand-ins.

## If you run a similar instrument

Clockwork is written to be adopted, not just to be read. The wire format and the console protocol
are documented here so that another instrument can be reasoned about without reading the code, and
`clockwork.mips`, `clockwork.acq` and `clockwork.method` import no Qt, so a script can drive one
box without pulling a window in. If you run MIPS boxes and an SA220P and want to try clockwork on
them, open an issue and say what your instrument looks like. No support is promised, and questions
are welcome.

## License

BSD 3-Clause, copyright University of Washington. See `LICENSE`.
