# AqMD3 Acquisition Console: the ZeroMQ protocol

PNNL's [AqMD3 Acquisition Console](https://github.com/PNNL-Comp-Mass-Spec/AqMD3-Acquisition-Console)
(BSD 2-Clause, Battelle Memorial Institute, 2021; C++20) runs the Keysight/Acqiris SA220P digitizer
for ion mobility acquisitions. It is a server. A client sends it commands over ZeroMQ, it streams
the digitizer in zero-suppress mode between pusher pulses, appends scans to a UIMF file the client
has created, and publishes a live summary of each batch of scans. Clockwork is such a client. This
document is what a client has to know, derived from the console's source at its 2025-12-12 head
(`aqmd3_console.cpp`, `server.cpp`, `message.proto`, `config.txt`, `libaqmd3/`, `UIMFWriter/`).
It is not a specification the console's authors wrote; where it says "hardcoded", the value is in
the console's source and changes only by rebuilding it. Six of those values are read from a file
by the fork this project runs, and are listed under Configuration.

## Sockets

| Socket | Address | Pattern | Carries |
|---|---|---|---|
| Command | `tcp://*:5555` | ROUTER; client uses REQ or DEALER | Commands as multipart string frames; one reply frame, or two for `acquire` and `tof width` |
| Data | `tcp://*:5554` | PUB; client uses SUB | Two frames per message: a topic string, then the payload |

The command loop polls with a 1 ms timeout and handles one request at a time. Every reply to a
setting command is the string `ack`.

The data socket carries **two topics**, and a client needs both:

| Topic | Payload | Published by |
|---|---|---|
| `data` | A Snappy-compressed protobuf `Message`, one per batch of `NotifyOnScansCount` scans | `ZmqAcquiredDataSubscriber` |
| `status` | The plain, uncompressed string `finished`, `finished acquire`, or (the fork only) `error <what>` | `AcquirePublisher` |

Subscribing to `data` alone is the mistake to avoid: it is the topic named in the console's own
test client, and a client that subscribes to it and waits for `finished` waits forever. ZeroMQ
matches a subscription by prefix, so subscribing to the empty string takes both.

Two further properties of this socket follow from where it is created. The console binds
`tcp://*:5554` inside the handler for the **first `acquire`**, not at startup, so nothing is
listening on it until a client has acquired once; a subscriber that connects before then is
fine, because ZeroMQ reconnects on its own. It is then kept: the console caches the socket by
address and the acquisition chain holds a reference to it, so a later `acquire` reuses the same
socket rather than rebinding, and a subscriber survives the whole session. And PUB drops what it
cannot deliver, so a subscription that has not yet reached the console loses the first messages
published after it: subscribe well before `acquire`, and treat a missing first batch as normal
rather than as a fault.

## Command set

Frames are plain strings; a command with an argument is two frames.

| Frames | Effect | Reply |
|---|---|---|
| `num instruments` | Counts VISA resources matching `PXI?*::INSTR` | The count |
| `info` | Model, serial, firmware, console version and commit; the fork adds its repository and branch and the full scale in force | One descriptive string |
| `firmware`, `serial` | The single field | The string |
| `init` | Trigger source `External1`, level 2.0 V †, rising edge †, post-trigger delay from config (default 10 µs) | `ack` |
| `horizontal`, `<seconds per sample>` | Sample rate = 1/value (0.5 ns → 2 GS/s) | `ack` |
| `vertical`, `<offset V>` | Channel 1, full scale **0.5 V** †, given offset, DC coupling | `ack` |
| `invert`, `true`/`false` | Channel 1 data inversion | `ack` |
| `enable io port`, `<n>` / `disable io port`, `<n>` | Sets Control I/O **2** † (the argument is ignored) to `In-TriggerEnable` or `Disabled` | `ack` |
| `tof width` | Measures the pusher period from 20 trigger timestamps, sizes the record, sets it | `TofWidthMessage` protobuf, then its SHA-256 hex |
| `acquire` | As `tof width`, then configures zero-suppress streaming and starts an **open-ended** acquisition that publishes data but writes no file | `TofWidthMessage` protobuf, then its SHA-256 hex |
| `acquire frame`, `<snappy(UimfRequestMessage)>` | Starts acquiring one frame into the named UIMF file; returns immediately | `ack` |
| `stop`, `acquire` | Stops the running acquisition and tears down the acquisition chain | `ack` |
| `stop`, `<anything else>` | Stops the running frame, keeps the chain for the next `acquire frame` | `ack` |
| `setup array` | Accepted and ignored (TODO in the source) | `ack` |
| `trig class`, `trig source`, `mode`, `config digitizer`, `post samples`, `pre samples`, `reset timestamps` | Accepted and ignored (TODO in the source) | none |

† Hardcoded upstream, read from `config.txt` by the fork. See Configuration.

**Some requests answer nothing at all**, and a client that waits on one of them waits forever:
each of the seven ignored commands in the last row above, and `stop` sent with anything other
than exactly two frames. Everything else in the table replies, and `acquire` and `tof width`
reply twice.

## The order commands have to come in

The console's own test client shows `init`, `horizontal`, `vertical`, `invert`, then `acquire`,
then `stop acquire`. It never asks for a frame, so it does not show the part that matters, and
the part that matters is strict. Every rule below is read off the source rather than provoked on
an instrument, and every one of them has the same shape: the console does not refuse a command
sent out of order, it either ignores it or dies.

```
init, horizontal, vertical, invert, enable io port
acquire                     the chain is built; an open-ended acquisition starts
stop frame                  that acquisition ends, the chain stays
    acquire frame <request> one frame
    (wait for finished)
    stop frame              ends the frame, or joins the thread that has ended
stop acquire                the chain is torn down
```

**One acquisition at a time, and each one is ended with a `stop` before the next begins.** The
console runs an acquisition on a thread it holds in a `std::unique_ptr`, and `stop` in either
form is the only thing that joins that thread. Starting another acquisition replaces the
pointer, which destroys a thread that has not been joined, which in C++ calls `std::terminate`.
The consequences, exactly:

| Sent | While | The console does |
|---|---|---|
| `acquire frame` | no `acquire` since startup | reads through a null pointer, and dies |
| `acquire frame` | an acquisition is still running | replies `ack`, logs a warning, and starts nothing |
| `acquire frame` | the previous acquisition ended and was not stopped | destroys a joinable thread, and dies |
| `acquire` | any acquisition has run and not been stopped | the same, and dies |
| `stop`, anything | nothing has ever acquired | replies `ack`, harmlessly |

**`acquire` starts an acquisition, not just a chain.** It begins an open-ended one:
`frame_length` is the largest 64-bit value and the file name is empty, so it streams and
publishes until it is stopped. This is what binds the data socket and what measures the pusher
period. Until it is stopped with `stop frame`, an `acquire frame` is the second row of the table
above: acknowledged, logged, and ignored.

**Every acquisition ends with exactly one `finished`.** The open-ended one publishes it when it
is stopped; a frame publishes it when its `frame_length` scans are in, or when it is stopped
early. So a client that acquires and then stops the open-ended acquisition sees a `finished`
before the first frame has been asked for, and has to expect it. A `stop frame` sent after a
frame's own `finished` has arrived publishes nothing further; it is sent for the join alone.

A `stop acquire` that cuts a running acquisition short publishes both messages, and their order
is not determined: the acquisition thread publishes its `finished` as its last act while the
command thread publishes `finished acquire`, and nothing sequences the two. A client waiting for
one should ignore the other rather than assume which arrives first.

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

`Message`, published on topic `data` per batch of `NotifyOnScansCount` scans: `mz` (a dense
summed spectrum over the batch, one entry per sample in the record), `tic` (one per scan) and
`time_stamps` (one per scan, in samples of the digitizer clock). One `Message` is published per
batch in both modes, whether or not the request named a file.

On topic `status`, the plain string `finished` when a frame's scans are all in, and
`finished acquire` after `stop acquire`. What `finished` means is narrower than it looks. The
acquisition thread publishes it once it has counted `frame_length` scans off the digitizer and
stopped the card, and the subscriber that writes them to the UIMF file runs on its own thread
behind a queue, so `finished` says the digitizer is done and not that the file is complete. A
client that needs the rows themselves has to wait on the file. `finished acquire` is the
stronger of the two: `stop acquire` waits for every subscriber to drain before publishing it.

**Most of a frame's batches can arrive after the `finished` of that frame.** Both topics come
off one socket, so a client sees messages in the order they were published and never out of it,
but the two are not published by the same thread: the acquisition thread hands each batch to a
subscriber that publishes it from its own thread on a 10 ms poll, and publishes `finished`
itself, directly. The gap this opens is large. Measured on an SA220P at 2 GS/s with a 233 888
sample record, 5000 scan frames in which no sample was suppressed delivered half or all of
their scans after their own `finished`, the first trailing batch arriving 0.4 to 0.9 s after it
and the last as much as 9.5 s after it. A lightly occupied frame of the same length delivers
most of its batches in time. Two consequences: a client drawing a live trace is always drawing
a frame that has already ended, and a client deciding that a frame produced nothing has to keep
listening for several seconds before it may say so.

**`finished` says a frame ended and not that it succeeded.** The acquisition loop catches an
error, logs it, stops, and publishes the same `finished` it would have published on success, so
a frame that failed on its first fetch is indistinguishable on the wire from a frame that
acquired everything asked of it. Upstream that is all there is; the fork adds the message
below.

## What the console does with the digitizer

- Continuous streaming, triggered mode, one record per external trigger: the pusher pulse.
- Record size = measured pusher period − post-trigger delay − rearm time (default 2.048 µs),
  rounded down to a multiple of 32 samples. A pusher period that drifts after `acquire` is not
  re-measured.
- Zero-suppress (the ZS1 option) with threshold **−32667** and hysteresis **100** on the signed
  16-bit sample scale, no pre- or post-gate samples. Threshold and hysteresis are hardcoded
  upstream and read from `config.txt` by the fork; the gate samples are hardcoded in both.
  Samples are shifted by +32768 into an unsigned range before storage.
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

`BPI_MZ` holds the m/z of the base peak's bin, which the fork computes from the calibration the
frame's own `Frame_Params` state and the `BinWidth` in `Global_Params`. Both are in the file
before `acquire frame`, because the client writes a frame's parameters and then asks for the
frame. A frame whose `CalibrationSlope` is zero or absent has no mass axis, and the column holds
the base peak's bin index on those rows instead. The upstream console stores the bin index on
every row, calibrated or not, so a file acquired through a stock build holds bin indices in a
column defined as m/z.

## Accumulations

The console writes **one scan row per trigger**. A frame acquired with `frame_length` = 5000 and
`nbr_accumulations` = 100 yields 5000 rows, each one pusher pulse, and stores 100 as a number.
Files written today by FALKOR through the older U1084A hold, per scan, the sum over 100 pushes.
Summing passes per scan is therefore the client's or a modified console's job; the console has
commented-out code that once cloned a frame per accumulation, so the authors met the same
question.

## Configuration (`config.txt` beside the executable)

The console reads a flat `key=value` file from its working directory at startup and logs every
value it resolved, with the ones it fell back on marked as defaulted. Logs go to `logs/` beside
the executable.

`PostTriggerDelay` (s, default 1e-5), `TriggerRearmDeadTime` (s, default 2.048e-6),
`ResourceName` (VISA, default `PXI0::0::0::INSTR`), `NotifyOnScansCount` (scans per batch and
per write, default 500), `AcquisitionTimeoutMs` (0 = wait forever, default 100),
`AcquisitionInitialBufferCount`, `AcquisitionMaxBufferCount`,
`AcquisitionBufferReserveElementsCount`, `LogLevel`.

### Settings the fork moves out of the source

Six settings that the upstream console compiles in are read from the same file by the fork at
[bushgroup/AqMD3-Acquisition-Console](https://github.com/bushgroup/AqMD3-Acquisition-Console).
Every default below is the literal the upstream source carries, so a fork given none of these
keys does what the stock console does.

| Key | Default | Replaces |
|---|---|---|
| `TriggerLevel` | `2.0` (V) | the trigger level set by `init` |
| `TriggerSlope` | `rising` | the edge set by `init`; `rising` or `falling` |
| `FullScaleRange` | `0.5` (V) | the full scale set by `vertical` |
| `ZeroSuppressThreshold` | `-32667` | the zero-suppress threshold used by `acquire` |
| `ZeroSuppressHysteresis` | `100` | the zero-suppress hysteresis used by `acquire` |
| `ControlIoPort` | `2` | the fixed Control I/O 2 in `enable io port` and `disable io port` |

A stock console ignores all six without saying so. The reply to `info` is how a client tells the
two builds apart: the fork appends its repository and branch to the version string the stock
console ends on.

The fork also reports the full scale in force, appending ` / Full Scale: <volts>` to the same
string. `FullScaleRange` is read at startup and applied by `vertical`, and nothing else in the
protocol reports it back, so `info` is how a client records the vertical window a file was
acquired with rather than the window the configuration file was last edited to say.

The fork refuses five of these before they reach the driver, naming the key that is wrong: a
threshold outside the signed 16-bit range, a hysteresis outside 100 to 1023, a full scale other
than 0.5 or 2.5, a port other than 1, 2 or 3, and a slope other than `rising` or `falling`. The
hysteresis bound and the full-scale pair are the SA220P's own documented limits: the card offers
exactly two full-scale ranges, and it accepts a hysteresis only in [100, 1023], so every other
value in the span the fork used to allow would have reached the driver and been refused there.
Trigger level is still passed through, because the level a card accepts depends on how its input
is terminated, and the driver refuses what it will not take.

The threshold is checked against the signed 16-bit range rather than against the card's own,
which is [hysteresis - 32768, +32767] and so moves with the hysteresis. That is why the console's
-32667 sits one code above the minimum at a hysteresis of 100 and would be refused by the driver
at a hysteresis of 1023.

### One status message the fork adds

An acquisition that fails publishes

    error <what went wrong>

on the `status` topic, one line, with any newlines and tabs in the text flattened to spaces.
The frame's own `finished` follows it, so the ordinary end of a frame is unchanged and arrives
whether or not an error came first; a frame that fails before it starts publishes the error and
no `finished` at all. Nothing else about the protocol changes, and a client that matches
`finished` and `finished acquire` exactly ignores this and behaves as it did.

It exists because there was otherwise no way for a client to learn that an acquisition had
failed. The error went to a log file on the acquisition machine and the frame ended with the
`finished` of a frame that had worked.

Against a stock console, or a fork older than this, the only sign of a failed acquisition is
still a frame that ends carrying fewer scans than it asked for, or none at all. Fewer is not
reliable, since the data socket drops messages when a client falls behind, but none at all is
worth treating as a failure: the console publishes a batch per `NotifyOnScansCount` scans
whether or not anything crossed the zero-suppress threshold, so a frame with no batches
acquired nothing.

The ZeroMQ protocol is unchanged in every command and in both replies. A client works against
either build.

### One reply the fork adds

A command that fails inside the console answers

    error <what went wrong>

as a single frame on the command socket, in place of whatever that command normally replies
with, its newlines and tabs flattened to spaces the same way. The command did nothing: no
acquisition was started or stopped by it, and the card is in whatever state the failed command
left it in. Every successful reply is exactly what it was, so a client that never meets a
failure sees no difference.

A stock console has no error boundary around its command handlers at all. An exception from any
of them unwinds out of the server's poll loop and out of `main`, which logs two `critical` lines
and exits, so the client learns of the failure as a request that timed out because the process
is gone. That is not a hypothetical: a `tof width` or an `acquire` whose period measurement had
been spoiled computed a record size the driver refused, and the refusal took the console down
every time.

A client should treat a one-frame reply beginning `error` as a failure of the command rather
than as its answer. The reply is one frame where `acquire` and `tof width` answer with two, so a
client that checks the frame count meets it as a malformed reply rather than as a period; the
prefix is what tells the two apart.

## Building it

Visual Studio 2019 16.9 or newer (the source uses `std::format`, so in practice 2022), CMake,
and vcpkg packages `zeromq`, `sqlitecpp`, `sqlite3`, `snappy`, `protobuf`, `picosha2`,
`cppzmq`, `spdlog`; Acqiris MD3 software and Keysight IO Libraries (VISA) installed. There are no
binary releases.
