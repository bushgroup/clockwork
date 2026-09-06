# AqMD3 Acquisition Console: the ZeroMQ protocol

PNNL's [AqMD3 Acquisition Console](https://github.com/PNNL-Comp-Mass-Spec/AqMD3-Acquisition-Console)
(BSD 2-Clause, Battelle Memorial Institute, 2021; C++20) runs the Keysight/Acqiris SA220P digitizer
for ion mobility acquisitions. It is a server. A client sends it commands over ZeroMQ, it streams
the digitizer in zero-suppress mode between pusher pulses, appends scans to a UIMF file the client
has created, and publishes a live summary of each batch of scans. Clockwork is such a client. This
document is what a client has to know, derived from the console's source at its 2025-12-12 head
(`aqmd3_console.cpp`, `server.cpp`, `message.proto`, `config.txt`, `libaqmd3/`, `UIMFWriter/`).
It is not a specification the console's authors wrote; where it says "hardcoded", the value is in
the console's source and changes only by rebuilding it.

## Sockets

| Socket | Address | Pattern | Carries |
|---|---|---|---|
| Command | `tcp://*:5555` | ROUTER; client uses REQ or DEALER | Commands as multipart string frames; one reply frame, or two for `acquire` and `tof width` |
| Data | `tcp://*:5554` | PUB; client uses SUB on topic `data` | Two frames per message: the topic string, then a Snappy-compressed protobuf `Message` |

The command loop polls with a 1 ms timeout and handles one request at a time. Every reply to a
setting command is the string `ack`.

## Command set

Frames are plain strings; a command with an argument is two frames.

| Frames | Effect | Reply |
|---|---|---|
| `num instruments` | Counts VISA resources matching `PXI?*::INSTR` | The count |
| `info` | Model, serial, firmware, console version and commit | One descriptive string |
| `firmware`, `serial` | The single field | The string |
| `init` | Trigger source `External1`, level 2.0 V, rising edge, post-trigger delay from config (default 10 µs) | `ack` |
| `horizontal`, `<seconds per sample>` | Sample rate = 1/value (0.5 ns → 2 GS/s) | `ack` |
| `vertical`, `<offset V>` | Channel 1, full scale **0.5 V** (hardcoded), given offset, DC coupling | `ack` |
| `invert`, `true`/`false` | Channel 1 data inversion | `ack` |
| `enable io port`, `<n>` / `disable io port`, `<n>` | Sets Control I/O **2** (the argument is ignored) to `In-TriggerEnable` or `Disabled` | `ack` |
| `tof width` | Measures the pusher period from 20 trigger timestamps, sizes the record, sets it | `TofWidthMessage` protobuf, then its SHA-256 hex |
| `acquire` | As `tof width`, then configures zero-suppress streaming and starts an **open-ended** acquisition that publishes data but writes no file | `TofWidthMessage` protobuf, then its SHA-256 hex |
| `acquire frame`, `<snappy(UimfRequestMessage)>` | Starts acquiring one frame into the named UIMF file; returns immediately | `ack` |
| `stop`, `acquire` | Stops the running acquisition and tears down the acquisition chain | `ack` |
| `stop`, `<anything else>` | Stops the running frame, keeps the chain for the next `acquire frame` | `ack` |
| `trig class`, `trig source`, `mode`, `config digitizer`, `post samples`, `pre samples`, `setup array`, `reset timestamps` | Accepted and ignored (TODO in the source); `setup array` replies `ack` | — |

The order the console's own test client uses: `init`, `horizontal`, `vertical`, `invert`, then
`acquire`; then per frame `acquire frame`; then `stop acquire`.

## Messages (`message.proto`, proto3)

`UimfRequestMessage`, the argument of `acquire frame`, Snappy-compressed:

| Field | Type | Meaning in the console |
|---|---|---|
| `file_name` | string | Path of an **existing** UIMF file; empty means publish only, write nothing |
| `frame_number` | uint32 | Written into every `Frame_Scans` row |
| `frame_length` | uint64 | Number of scans to acquire for this frame |
| `nbr_accumulations` | uint64 | Stored on the frame parameters; **not applied** (see below) |
| `start_trigger` | uint64 | Scans before this index are dropped; `ScanNum` is renumbered from it |
| `offset_bins` | uint32 | Added to the leading zero run of the first gate in each scan |
| `nbr_samples` | uint64 | Carried, unused |
| `frame_type` | enum `MS`, `MSMS`, `Calibration`, `Prescan` | Carried, unused |

`TofWidthMessage`: `pusher_pulse_width` (measured samples between triggers) and `num_samples`
(record size plus post-trigger samples).

`Message`, published on the data socket per batch of `NotifyOnScansCount` scans: `mz` (a dense
summed spectrum over the batch, one entry per sample in the record), `tic` (one per scan) and
`time_stamps` (one per scan, in samples of the digitizer clock). After a frame completes the
console also publishes the plain string `finished`; after `stop acquire`, `finished acquire`.

## What the console does with the digitizer

- Continuous streaming, triggered mode, one record per external trigger: the pusher pulse.
- Record size = measured pusher period − post-trigger delay − rearm time (default 2.048 µs),
  rounded down to a multiple of 32 samples. A pusher period that drifts after `acquire` is not
  re-measured.
- Zero-suppress (the ZS1 option) with threshold **−32667** and hysteresis **100** on the signed
  16-bit sample scale, no pre- or post-gate samples: hardcoded. Samples are shifted by +32768 into
  an unsigned range before storage.
- The markers stream is parsed per the Acqiris `CPP_IVIC_StreamingZeroSuppress` example; the
  samples stream is fetched in the amount the gates describe. Each trigger becomes one scan row:
  the gated samples with negative run-length entries for the zero gaps, plus `TIC`, `BPI`,
  `NonZeroCount` and the trigger timestamp.
- Self-calibration runs when the driver asks for it, at configuration time.

## Division of UIMF writing

The console's `UimfWriter` opens the file the request names **read-write, without creating it**,
and inserts `Frame_Scans` rows only, in one transaction per batch with `synchronous=0`. It does
not create the schema, `Global_Params`, `Frame_Param_Keys` or `Frame_Params`; the only other write
is an update of a frame's duration, which is currently a stub. The client therefore creates the
file with the full schema and parameters before the first `acquire frame`, and owns every
parameter value. SQLite's journal mode is a property of the file, so a client that creates it in
WAL mode gets WAL on the console's connection too.

## Accumulations

The console writes **one scan row per trigger**. A frame acquired with `frame_length` = 5000 and
`nbr_accumulations` = 100 yields 5000 rows, each one pusher pulse, and stores 100 as a number.
Files written today by FALKOR through the older U1084A hold, per scan, the sum over 100 pushes.
Summing passes per scan is therefore the client's or a modified console's job; the console has
commented-out code that once cloned a frame per accumulation, so the authors met the same
question.

## Configuration (`config.txt` beside the executable)

`PostTriggerDelay` (s, default 1e-5), `TriggerRearmDeadTime` (s, default 2.048e-6),
`ResourceName` (VISA, default `PXI0::0::0::INSTR`), `NotifyOnScansCount` (scans per batch and
per write, default 500), `AcquisitionTimeoutMs` (0 = wait forever, default 100),
`AcquisitionInitialBufferCount`, `AcquisitionMaxBufferCount`,
`AcquisitionBufferReserveElementsCount`, `LogLevel`. Logs go to `logs/` beside the executable.

## Building it

Visual Studio 2019 16.9 or newer (the source uses `std::format`, so in practice 2022), CMake,
and vcpkg packages `zeromq`, `sqlitecpp`, `sqlite3`, `snappy`, `protobuf`, `picosha2`,
`cppzmq`, `spdlog`; Acqiris MD3 software and Keysight IO Libraries (VISA) installed. There are no
binary releases.
