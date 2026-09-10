"""Acquisition through PNNL's AqMD3 acquisition console.

A ZeroMQ client for the console's command socket and its live data stream
(`docs/console-protocol.md`), and the creation of the UIMF file -- schema,
`Global_Params`, `Frame_Params` -- that the console appends `Frame_Scans` rows
to. Filled by the lab record's tasks 03 and 06. No Qt here, and every call
blocks, so nothing in it may run on the UI thread.

    wire.py      command strings, topics, the five protobuf messages, Snappy
    console.py   the command socket: configure, acquire, ask for a frame, stop
    stream.py    the data socket: per-batch summaries and frame status
    session.py   the two sockets in step: open the chain, run one frame
    fake.py      a console simulated in this process, for tests and the
                 hardware-free self-check

The console owns the digitizer and the `Frame_Scans` insert; clockwork owns
everything it leaves to a client, which is the file's existence and every
parameter in it, when frames start and stop, and the strings on the boxes. Two
consequences shape this package. The file has to exist, with its schema and
parameters written, before the first `acquire frame`, because the console opens
it read-write and never creates one; and the console writes one scan row per
pusher pulse and does not sum accumulations, so summing is clockwork's (lab
record, task 02).

A short session against a running console:

    from clockwork.acq import Console, DataStream, FrameRequest, run_frame, start_chain

    with DataStream("tcp://masstro:5554") as stream, Console("tcp://masstro:5555") as console:
        print(console.info().text)
        console.configure(offset_v=0.251)
        width = start_chain(console, stream)
        print(width.period_seconds(console.sample_rate_hz), "s between pushes")
        run_frame(console, stream,
                  FrameRequest(frame_length=5000, file_name=path, offset_bins=20000),
                  timeout=30.0)
        console.stop_acquire()

The order in that example is not a style: a frame asked for out of turn kills
the console process rather than earning an error, which is why `session.py`
exists and why `Console` refuses the calls that would do it.

and the same against no console at all, which is what the self-check runs:

    from clockwork.acq import Console, DataStream, FakeConsole

    with FakeConsole() as fake:
        console = Console(fake.command_endpoint)
        stream = DataStream(fake.data_endpoint)
"""

from __future__ import annotations

from .console import (
    ACQUIRE_TIMEOUT_S,
    DEFAULT_TIMEOUT_S,
    STOP_ACQUIRE_TIMEOUT_S,
    Console,
    ConsoleStateError,
    ConsoleTimeout,
)
from .fake import (
    DEFAULT_NOTIFY_ON_SCANS_COUNT,
    DEFAULT_PERIOD_SAMPLES,
    DEFAULT_POST_TRIGGER_SAMPLES,
    DEFAULT_REARM_SAMPLES,
    FakeConsole,
)
from .session import EMPTY_SETTLE_S, run_frame, start_chain
from .stream import (
    QUEUE_MESSAGES,
    DataStream,
    StreamTimeout,
)
from .wire import (
    ACK,
    COMMAND_PORT,
    DATA_PORT,
    ERROR_PREFIX,
    FINISHED,
    FINISHED_ACQUIRE,
    FRAME_TYPES,
    GATE_GRANULARITY_SAMPLES,
    SCAN_NUM_SMALLINT_MAX,
    SECONDS_PER_SAMPLE_2GSPS,
    SILENT_COMMANDS,
    TOPIC_DATA,
    TOPIC_STATUS,
    AcqError,
    Batch,
    ConsoleAcquisitionError,
    ConsoleInfo,
    ConsoleProtocolError,
    EmptyFrameError,
    FrameRequest,
    Status,
    TofWidth,
    compress,
    decode_batch,
    decode_tof_width,
    decompress,
    encode_batch,
    encode_tof_width,
    record_size_samples,
    samples_at,
)

__all__ = [
    "ACK",
    "ACQUIRE_TIMEOUT_S",
    "AcqError",
    "Batch",
    "COMMAND_PORT",
    "Console",
    "ConsoleAcquisitionError",
    "ConsoleInfo",
    "ConsoleProtocolError",
    "ConsoleStateError",
    "ConsoleTimeout",
    "DATA_PORT",
    "DEFAULT_NOTIFY_ON_SCANS_COUNT",
    "DEFAULT_PERIOD_SAMPLES",
    "DEFAULT_POST_TRIGGER_SAMPLES",
    "DEFAULT_REARM_SAMPLES",
    "DEFAULT_TIMEOUT_S",
    "DataStream",
    "EMPTY_SETTLE_S",
    "ERROR_PREFIX",
    "EmptyFrameError",
    "FINISHED",
    "FINISHED_ACQUIRE",
    "FRAME_TYPES",
    "FakeConsole",
    "FrameRequest",
    "GATE_GRANULARITY_SAMPLES",
    "QUEUE_MESSAGES",
    "SCAN_NUM_SMALLINT_MAX",
    "SECONDS_PER_SAMPLE_2GSPS",
    "SILENT_COMMANDS",
    "STOP_ACQUIRE_TIMEOUT_S",
    "Status",
    "StreamTimeout",
    "TOPIC_DATA",
    "TOPIC_STATUS",
    "TofWidth",
    "compress",
    "decode_batch",
    "decode_tof_width",
    "decompress",
    "encode_batch",
    "encode_tof_width",
    "record_size_samples",
    "run_frame",
    "samples_at",
    "start_chain",
]
