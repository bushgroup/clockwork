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
    uimf.py      the files: the two-phase frame parameters and the fold
    loop.py      a whole acquisition from a method: the boxes, the frames,
                 the fold, a replicate
    process.py   the console as a process: launch it, watch it, restart it
                 after a `config.txt` change, stop it, and its `config.txt`
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

A whole acquisition, which is that sequence with the boxes and the files
around it and is what anything above this package should call:

    from clockwork.acq import run_acquisition, send_phases

    send_phases(method, boxes, progress=print_event)
    run = run_acquisition(method, boxes=boxes, console=console, stream=stream,
                          directory=folder, post_trigger_samples=20000,
                          progress=print_event)
    replicate = run_acquisition(method, boxes=boxes, console=console,
                                stream=stream, directory=folder,
                                post_trigger_samples=20000,
                                stem=run.method.acquisition.file_stem + "-2",
                                replicate=True, progress=print_event)

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
    DEFAULT_SCAN_PERIOD,
    FakeConsole,
)
from .loop import (
    ABORT_AFTER_FAILURES,
    ABORT_AFTER_UNWITNESSED,
    ARM_TIMEOUT_S,
    FRAME_POLL_S,
    FRAME_TIMEOUT_FLOOR_S,
    FRAME_TIMEOUT_SLACK,
    GATE_PUBLISH_ALLOWANCE_S,
    ROW_SETTLE_S,
    SILENCE_S,
    WHEN_AFTER,
    WHEN_ARMED,
    WHEN_BEFORE,
    AcquisitionRefused,
    BatchSeen,
    BoxReady,
    BoxSaid,
    EnableGateError,
    Event,
    Folded,
    FoldRecord,
    FrameBegun,
    FrameEnded,
    FrameRecord,
    GateChecked,
    PhaseSent,
    ReadingBack,
    Run,
    RunBegun,
    Snapshot,
    StateRead,
    Warned,
    cautions,
    declared_differences,
    enable_witness,
    left_as_found,
    refusals,
    run_acquisition,
    send_phases,
)
from .process import (
    CONSOLE_ENV,
    KEYS,
    STARTUP_TIMEOUT_S,
    ConsoleConfig,
    ConsoleProcess,
    ConsoleProcessError,
    ConsoleSupervisor,
    FakeConsoleProcess,
    Prepared,
    find_console,
    prepare_console,
    read_startup_block,
)
from .session import EMPTY_SETTLE_S, run_frame, start_chain
from .stream import (
    QUEUE_MESSAGES,
    DataStream,
    StreamTimeout,
)
from .uimf import (
    PROVENANCE_KEYS,
    RAW_SUFFIX,
    SA220P_DETECTOR_BITS,
    SUMMED_SUFFIX,
    Geometry,
    Recording,
    fold_scans,
    raw_path,
    stamp_globals,
    summed_path,
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
    ConsoleCommandError,
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
    "ABORT_AFTER_FAILURES",
    "ABORT_AFTER_UNWITNESSED",
    "ACK",
    "ACQUIRE_TIMEOUT_S",
    "ARM_TIMEOUT_S",
    "AcqError",
    "AcquisitionRefused",
    "Batch",
    "BatchSeen",
    "BoxReady",
    "BoxSaid",
    "COMMAND_PORT",
    "CONSOLE_ENV",
    "Console",
    "ConsoleAcquisitionError",
    "ConsoleCommandError",
    "ConsoleConfig",
    "ConsoleInfo",
    "ConsoleProcess",
    "ConsoleProcessError",
    "ConsoleProtocolError",
    "ConsoleStateError",
    "ConsoleSupervisor",
    "ConsoleTimeout",
    "DATA_PORT",
    "DEFAULT_NOTIFY_ON_SCANS_COUNT",
    "DEFAULT_PERIOD_SAMPLES",
    "DEFAULT_POST_TRIGGER_SAMPLES",
    "DEFAULT_REARM_SAMPLES",
    "DEFAULT_SCAN_PERIOD",
    "DEFAULT_TIMEOUT_S",
    "DataStream",
    "EMPTY_SETTLE_S",
    "ERROR_PREFIX",
    "EmptyFrameError",
    "EnableGateError",
    "Event",
    "FINISHED",
    "FINISHED_ACQUIRE",
    "FRAME_POLL_S",
    "FRAME_TIMEOUT_FLOOR_S",
    "FRAME_TIMEOUT_SLACK",
    "FRAME_TYPES",
    "FakeConsole",
    "FakeConsoleProcess",
    "FoldRecord",
    "Folded",
    "FrameBegun",
    "FrameEnded",
    "FrameRecord",
    "FrameRequest",
    "GATE_GRANULARITY_SAMPLES",
    "GATE_PUBLISH_ALLOWANCE_S",
    "GateChecked",
    "Geometry",
    "KEYS",
    "PROVENANCE_KEYS",
    "PhaseSent",
    "Prepared",
    "QUEUE_MESSAGES",
    "RAW_SUFFIX",
    "ROW_SETTLE_S",
    "Recording",
    "Run",
    "RunBegun",
    "SA220P_DETECTOR_BITS",
    "SCAN_NUM_SMALLINT_MAX",
    "SECONDS_PER_SAMPLE_2GSPS",
    "SILENCE_S",
    "SILENT_COMMANDS",
    "STARTUP_TIMEOUT_S",
    "STOP_ACQUIRE_TIMEOUT_S",
    "SUMMED_SUFFIX",
    "Snapshot",
    "ReadingBack",
    "StateRead",
    "Status",
    "StreamTimeout",
    "TOPIC_DATA",
    "TOPIC_STATUS",
    "TofWidth",
    "WHEN_AFTER",
    "WHEN_ARMED",
    "WHEN_BEFORE",
    "Warned",
    "cautions",
    "compress",
    "declared_differences",
    "decode_batch",
    "decode_tof_width",
    "decompress",
    "enable_witness",
    "encode_batch",
    "encode_tof_width",
    "find_console",
    "fold_scans",
    "left_as_found",
    "prepare_console",
    "raw_path",
    "read_startup_block",
    "record_size_samples",
    "refusals",
    "run_acquisition",
    "run_frame",
    "samples_at",
    "send_phases",
    "stamp_globals",
    "start_chain",
    "summed_path",
]
