"""The acquisition console's wire format: its command strings, topics and messages.

Every fact here is `docs/console-protocol.md`; this module implements that
document and decides nothing about the protocol itself. It holds no sockets, so
the UIMF side can build and read messages without pyzmq entering the picture.

The protobuf schema is built at run time rather than compiled. `message.proto`
has five messages and one enum, all of them scalar fields, and the console's
authors ship no generated Python; a `FileDescriptorProto` assembled here gives
the same classes out of the same runtime with no `protoc` in the loop, no
generated file to keep in step with the runtime version, and no build step in
front of a fresh clone. The transcription below is field for field the console's
`message.proto`, which is the thing to check it against.

What crosses the boundary of this module is dataclasses and numpy arrays, not
protobuf objects: `FrameRequest` in, `TofWidth` and `Batch` out. The generated
classes stay private, so nothing above has to care how they were made.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

import numpy as np
import snappy
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

# --------------------------------------------------------------------------
# Addresses, topics and command strings
# --------------------------------------------------------------------------

COMMAND_PORT = 5555
"""The ROUTER socket. Fixed in the console's source, not configurable."""

DATA_PORT = 5554
"""The PUB socket, which the first `acquire` binds and `stop acquire` drops."""

TOPIC_DATA = "data"
"""Snappy-compressed `Message`, one per batch of `NotifyOnScansCount` scans."""

TOPIC_STATUS = "status"
"""Plain, uncompressed `finished` and `finished acquire`.

Two topics, not one, and the console's own test client subscribes only to the
first: a client that copies it never sees a frame end.
"""

ACK = "ack"

FINISHED = "finished"
"""One frame's scans are off the digitizer. Not that they are in the file."""

FINISHED_ACQUIRE = "finished acquire"
"""The acquisition chain is torn down, every subscriber having drained."""

ERROR_PREFIX = "error"
"""What a status message that reports a failed acquisition begins with.

The rest of the line is whatever the console caught, its newlines flattened
to spaces. It is followed by the frame's own `finished`, so a failed frame
ends the same way a successful one does and the error is the only thing that
says otherwise.

A stock console publishes this never: it catches an acquisition error, logs
it, and ends the frame with the ordinary `finished`, which is why a frame
that carried nothing has to be treated as a failure in its own right (lab
record, task 20).
"""

SILENT_COMMANDS = frozenset(
    {
        "trig class",
        "trig source",
        "mode",
        "config digitizer",
        "post samples",
        "pre samples",
        "reset timestamps",
    }
)
"""Commands the console accepts, ignores, and does not answer.

Each is a TODO in the console's source that falls through the dispatch without
sending anything. Waiting for a reply to one of them waits forever, so
`Console` refuses to send them through the path that waits.
"""

SECONDS_PER_SAMPLE_2GSPS = 0.5e-9
"""The `horizontal` argument for 2 GS/s.

Not a free knob: the card runs at 2 GS/s whenever data reduction is on, which
for a SLIM separation is always (lab record, task 03). A method file should not
offer it as a choice.
"""

SCAN_NUM_SMALLINT_MAX = 32767
"""Where a frame outgrows the `ScanNum` column the UIMF schema declares.

SQLite stores the integer whatever the column says, so the console writes a
larger scan number without complaint and a reader that trusts the declared type
is the one that breaks. A `single_frame` acquisition of any real length crosses
this, which is why `FrameRequest.warnings()` says so rather than staying quiet.
"""

GATE_GRANULARITY_SAMPLES = 32
"""The record size is rounded down to a multiple of this."""


class AcqError(Exception):
    """Anything that went wrong acquiring through the console."""


class ConsoleProtocolError(AcqError):
    """The console said something the protocol document does not allow."""


class ConsoleCommandError(AcqError):
    """The console answered a command with an error instead of its reply.

    One frame, `error <what>`, in place of whatever the command normally
    answers with. It means the command threw inside the console and the
    console caught it: the card is in whatever state the failed command left,
    and no acquisition was started or stopped by it.

    A stock console sends this never. It has no error boundary around its
    command handlers at all, so a command that throws unwinds out of the
    server loop and out of `main`, and the client learns of it as a request
    that timed out because the process is gone (lab record, task 21).
    """


class ConsoleAcquisitionError(AcqError):
    """The console published an error while acquiring.

    Its own words, off the status topic. The frame it belongs to ended,
    early or with nothing in it, and whatever the console had written to the
    file before it failed is still there.
    """


class EmptyFrameError(AcqError):
    """A frame ended having published no scans at all.

    Not the same as a frame with no ions in it: the console publishes a batch
    per `NotifyOnScansCount` scans whether or not anything crossed the
    zero-suppress threshold, so a frame that produces no batches produced no
    scans. On a stock console that is the only visible sign of an acquisition
    that failed, and until the fetch fix it was the usual one (lab record,
    task 20).
    """


# --------------------------------------------------------------------------
# message.proto, transcribed
# --------------------------------------------------------------------------

_F = descriptor_pb2.FieldDescriptorProto

_PACKAGE = "aqmd3"

FRAME_TYPES = ("MS", "MSMS", "Calibration", "Prescan")
"""`UimfRequestMessage.FrameType`, in the order the enum numbers them.

The console carries the value onto the frame parameters and never acts on it.
"""


def _scalar(message: descriptor_pb2.DescriptorProto, name: str, number: int, kind: int) -> None:
    """One singular field. proto3 without `optional`, so presence is implicit."""
    entry = message.field.add()
    entry.name, entry.number, entry.type = name, number, kind
    entry.label = _F.LABEL_OPTIONAL


def _repeated(message: descriptor_pb2.DescriptorProto, name: str, number: int, kind: int) -> None:
    entry = message.field.add()
    entry.name, entry.number, entry.type = name, number, kind
    entry.label = _F.LABEL_REPEATED


def _build_schema() -> tuple[type, type, type]:
    """`message.proto`, field for field, as descriptors this runtime can use."""
    proto = descriptor_pb2.FileDescriptorProto()
    proto.name = "clockwork/acq/message.proto"
    proto.package = _PACKAGE
    proto.syntax = "proto3"

    # message Message: the per-batch summary published on topic `data`.
    batch = proto.message_type.add()
    batch.name = "Message"
    _repeated(batch, "mz", 1, _F.TYPE_INT32)
    _repeated(batch, "tic", 2, _F.TYPE_UINT32)
    _repeated(batch, "time_stamps", 3, _F.TYPE_UINT64)

    # message TofWidthMessage: the reply to `tof width` and to `acquire`.
    tof = proto.message_type.add()
    tof.name = "TofWidthMessage"
    _scalar(tof, "pusher_pulse_width", 1, _F.TYPE_UINT64)
    _scalar(tof, "num_samples", 2, _F.TYPE_UINT64)

    # message UimfRequestMessage: the argument of `acquire frame`.
    request = proto.message_type.add()
    request.name = "UimfRequestMessage"
    frame_type = request.enum_type.add()
    frame_type.name = "FrameType"
    for number, name in enumerate(FRAME_TYPES):
        value = frame_type.value.add()
        value.name, value.number = name, number
    _scalar(request, "start_trigger", 1, _F.TYPE_UINT64)
    _scalar(request, "nbr_samples", 2, _F.TYPE_UINT64)
    _scalar(request, "frame_length", 3, _F.TYPE_UINT64)
    _scalar(request, "nbr_accumulations", 4, _F.TYPE_UINT64)
    _scalar(request, "frame_number", 5, _F.TYPE_UINT32)
    _scalar(request, "offset_bins", 6, _F.TYPE_UINT32)
    _scalar(request, "file_name", 7, _F.TYPE_STRING)
    enum_field = request.field.add()
    enum_field.name, enum_field.number = "frame_type", 8
    enum_field.type, enum_field.label = _F.TYPE_ENUM, _F.LABEL_OPTIONAL
    enum_field.type_name = f".{_PACKAGE}.UimfRequestMessage.FrameType"

    # SetupMessage, TrigClassMessage, TrigSourceMessage and
    # DigitizerSetupMessage are in `message.proto` too and nothing sends or
    # receives them: every setting command takes plain strings. They are left
    # out until something needs them.

    pool = descriptor_pool.DescriptorPool()
    handle = pool.Add(proto)
    return (
        message_factory.GetMessageClass(handle.message_types_by_name["Message"]),
        message_factory.GetMessageClass(handle.message_types_by_name["TofWidthMessage"]),
        message_factory.GetMessageClass(handle.message_types_by_name["UimfRequestMessage"]),
    )


_Message, _TofWidthMessage, _UimfRequestMessage = _build_schema()


# --------------------------------------------------------------------------
# Snappy
# --------------------------------------------------------------------------


def compress(payload: bytes) -> bytes:
    """Snappy, raw format, as `snappy::Compress` writes it.

    The console uses two compressors for two destinations: Snappy on the data
    socket, LZF in the UIMF file. Nothing here touches the file's encoding.
    """
    return snappy.compress(payload)


def decompress(payload: bytes) -> bytes:
    try:
        return snappy.decompress(payload)
    except Exception as exc:  # python-snappy raises its own and cramjam's
        raise ConsoleProtocolError(f"data frame is not Snappy: {exc}") from exc


# --------------------------------------------------------------------------
# Record sizing
# --------------------------------------------------------------------------


def record_size_samples(
    pusher_period_samples: int,
    post_trigger_samples: int,
    rearm_samples: int,
) -> int:
    """What the console will set the record to, given the period it measured.

    The period, less the post-trigger delay and the trigger rearm dead time,
    rounded down to a multiple of 32 samples. Worth being able to predict
    without asking: it is the bin count of every scan the console writes, so
    the UIMF parameters a client creates before the first `acquire frame`
    depend on it.
    """
    usable = pusher_period_samples - post_trigger_samples - rearm_samples
    if usable <= 0:
        raise ValueError(
            f"a pusher period of {pusher_period_samples} samples leaves no record after "
            f"{post_trigger_samples} post-trigger and {rearm_samples} rearm samples"
        )
    return (usable // GATE_GRANULARITY_SAMPLES) * GATE_GRANULARITY_SAMPLES


def samples_at(seconds: float, sample_rate_hz: float) -> int:
    """Seconds to samples, the way the console's own arithmetic rounds."""
    return int(math.floor(seconds * sample_rate_hz))


# --------------------------------------------------------------------------
# What crosses the boundary
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TofWidth:
    """The console's own measurement of the pusher period, and the record it sized.

    `pusher_pulse_width` is samples between triggers, averaged over 20 of them;
    `num_samples` is the record plus the post-trigger samples, which is the
    length of every `Batch.mz` that follows.
    """

    pusher_pulse_width: int
    num_samples: int

    def period_seconds(self, sample_rate_hz: float) -> float:
        """The measured period in seconds, for comparing against the pusher."""
        return self.pusher_pulse_width / sample_rate_hz


@dataclass(frozen=True, slots=True)
class Batch:
    """One `Message`: the console's summary of `NotifyOnScansCount` scans.

    `mz` is a dense spectrum summed over the whole batch, one entry per sample
    in the record, so it is a display product and not the stored data. `tic`
    and `time_stamps` are per scan, the timestamps in samples of the digitizer
    clock. Nothing summed here reaches the UIMF file, and nothing in the file
    passes through here.
    """

    mz: np.ndarray
    tic: np.ndarray
    time_stamps: np.ndarray
    received_at: float = 0.0
    """`time.perf_counter()` when the client took the message off the socket.

    The console stamps nothing, so the host clock is the only clock a client
    has for how long a batch took to arrive.
    """

    @property
    def scans(self) -> int:
        return int(self.tic.size)


@dataclass(frozen=True, slots=True)
class Status:
    """A plain string on topic `status`.

    `topic` is carried because it is the one thing that distinguishes a message
    a later console adds from the two this one sends, and a client that meets
    an unrecognised one should be able to say which it met.
    """

    text: str
    received_at: float = 0.0
    topic: str = TOPIC_STATUS

    @property
    def is_finished(self) -> bool:
        return self.text == FINISHED

    @property
    def is_finished_acquire(self) -> bool:
        return self.text == FINISHED_ACQUIRE

    @property
    def is_error(self) -> bool:
        """Whether this reports a failed acquisition rather than an ordinary end."""
        return self.text == ERROR_PREFIX or self.text.startswith(ERROR_PREFIX + " ")

    @property
    def error_text(self) -> str:
        """What the console said, without the prefix. Empty if this is not an error."""
        return self.text[len(ERROR_PREFIX):].strip() if self.is_error else ""


@dataclass(slots=True)
class FrameRequest:
    """The argument of `acquire frame`, before it is a protobuf message.

    Only `frame_length` has to be chosen. `file_name` names a UIMF file that
    **already exists**, with its schema and parameters written, because the
    console opens it read-write and never creates it; an empty name means
    publish on the data socket and write nothing, which is how a client
    measures without producing a file.
    """

    frame_length: int
    file_name: str = ""
    frame_number: int = 1
    nbr_accumulations: int = 1
    """Stored on the frame parameters and never applied. The console writes one
    scan row per trigger whatever this says (lab record, task 02)."""

    start_trigger: int = 0
    """Scans before this index are dropped and `ScanNum` is renumbered from it."""

    offset_bins: int = 0
    """Added to the leading zero run of the first gate in every scan.

    The value that means what the console means by it is the post-trigger delay
    **in samples**, which is 20000 at 10 us and 2 GS/s; the console's own
    open-ended `acquire` uses exactly that. A comment in its source records
    that FALKOR sends nanoseconds here instead, so a file whose leading zero
    run is off by a factor of two thousand has been seen before.
    """

    nbr_samples: int = 0
    """Carried onto the frame parameters and otherwise unused."""

    frame_type: str = "MS"

    def __post_init__(self) -> None:
        if self.frame_type not in FRAME_TYPES:
            raise ValueError(f"frame_type {self.frame_type!r} is not one of {FRAME_TYPES}")
        if self.frame_length <= 0:
            raise ValueError(f"frame_length must be positive, not {self.frame_length}")
        for name in ("frame_number", "nbr_accumulations", "start_trigger", "offset_bins",
                     "nbr_samples"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")

    def warnings(self) -> list[str]:
        """Things worth saying about this request that are not errors.

        Kept separate from `__post_init__` because none of them stops an
        acquisition: they describe a file a reader might mishandle, not a
        request the console will refuse.
        """
        out: list[str] = []
        last_scan = self.start_trigger + self.frame_length - 1
        if last_scan > SCAN_NUM_SMALLINT_MAX:
            out.append(
                f"ScanNum reaches {last_scan}, past the {SCAN_NUM_SMALLINT_MAX} its column "
                "declares; SQLite stores it and a reader that trusts the declared type may not"
            )
        if self.file_name and self.offset_bins == 0:
            out.append(
                "offset_bins is 0, so the leading zero run of each scan carries no post-trigger "
                "delay; the post-trigger delay in samples is what the console means by it"
            )
        return out

    def encode(self) -> bytes:
        """Serialise and Snappy-compress, ready to be the second frame."""
        message = _UimfRequestMessage(
            start_trigger=self.start_trigger,
            nbr_samples=self.nbr_samples,
            frame_length=self.frame_length,
            nbr_accumulations=self.nbr_accumulations,
            frame_number=self.frame_number,
            offset_bins=self.offset_bins,
            file_name=self.file_name,
            frame_type=FRAME_TYPES.index(self.frame_type),
        )
        return compress(message.SerializeToString())

    @classmethod
    def decode(cls, payload: bytes) -> FrameRequest:
        """The inverse, so that a stand-in console can read what it was sent."""
        message = _UimfRequestMessage()
        try:
            message.MergeFromString(decompress(payload))
        except ConsoleProtocolError:
            raise
        except Exception as exc:
            raise ConsoleProtocolError(f"not a UimfRequestMessage: {exc}") from exc
        return cls(
            frame_length=message.frame_length,
            file_name=message.file_name,
            frame_number=message.frame_number,
            nbr_accumulations=message.nbr_accumulations,
            start_trigger=message.start_trigger,
            offset_bins=message.offset_bins,
            nbr_samples=message.nbr_samples,
            frame_type=FRAME_TYPES[message.frame_type],
        )


@dataclass(frozen=True, slots=True)
class ConsoleInfo:
    """The `info` reply, taken apart.

    The whole string is kept in `text`, because that is what belongs in a UIMF
    provenance stamp: it names the card, its firmware, the console's version
    and commit, and on our build the fork and branch as well. `is_fork` is how
    a client tells the two builds apart, which matters because a stock console
    silently ignores every setting the lab's `config.txt` moves out of source.
    """

    text: str
    model: str = ""
    serial: str = ""
    firmware: str = ""
    app: str = ""
    version: str = ""
    fork: str = ""
    branch: str = ""

    _LABELS = (
        ("Digitizer Model:", "model"),
        ("Digitizer Serial No.:", "serial"),
        ("Digitizer Firmware Version:", "firmware"),
        ("App:", "app"),
        ("App Version:", "version"),
        ("Fork:", "fork"),
    )

    @property
    def is_fork(self) -> bool:
        """Whether this console reads the six settings from `config.txt`."""
        return bool(self.fork)

    @classmethod
    def parse(cls, text: str) -> ConsoleInfo:
        """Split the reply on its own separators, keeping whatever is missing empty.

        The console formats one string of labelled fields joined by ` / `. A
        stock build stops after the version; ours appends `Fork: repo@branch`.
        Anything unrecognised is left out of the fields and stays in `text`,
        so a console built from a later source still parses to something.
        """
        fields: dict[str, str] = {}
        for part in text.split(" / "):
            part = part.strip()
            for label, name in cls._LABELS:
                if part.startswith(label):
                    fields[name] = part[len(label):].strip()
                    break
        fork, _, branch = fields.pop("fork", "").partition("@")
        return cls(text=text.strip(), fork=fork, branch=branch, **fields)


def decode_tof_width(payload: bytes, digest: str | None = None) -> TofWidth:
    """The two-frame reply to `acquire` and `tof width`.

    The console hashes the serialised message with SHA-256 and sends the hex
    beside it. Checking it costs nothing and is the only integrity check either
    socket offers, so it is checked whenever the second frame arrived.
    """
    if digest is not None:
        actual = hashlib.sha256(payload).hexdigest()
        if actual != digest.strip().lower():
            raise ConsoleProtocolError(
                f"TofWidthMessage failed its own SHA-256 (said {digest.strip()}, is {actual})"
            )
    message = _TofWidthMessage()
    try:
        message.MergeFromString(payload)
    except Exception as exc:
        raise ConsoleProtocolError(f"not a TofWidthMessage: {exc}") from exc
    if message.num_samples == 0:
        raise ConsoleProtocolError("TofWidthMessage carries no record size")
    return TofWidth(
        pusher_pulse_width=message.pusher_pulse_width,
        num_samples=message.num_samples,
    )


def encode_tof_width(width: TofWidth) -> tuple[bytes, str]:
    """The same reply, made rather than read, for a console stand-in."""
    message = _TofWidthMessage(
        pusher_pulse_width=width.pusher_pulse_width,
        num_samples=width.num_samples,
    )
    payload = message.SerializeToString()
    return payload, hashlib.sha256(payload).hexdigest()


def decode_batch(payload: bytes, *, received_at: float = 0.0) -> Batch:
    """One Snappy-compressed `Message` off topic `data`."""
    message = _Message()
    try:
        message.MergeFromString(decompress(payload))
    except ConsoleProtocolError:
        raise
    except Exception as exc:
        raise ConsoleProtocolError(f"not a Message: {exc}") from exc
    return Batch(
        mz=np.fromiter(message.mz, dtype=np.int32, count=len(message.mz)),
        tic=np.fromiter(message.tic, dtype=np.uint32, count=len(message.tic)),
        time_stamps=np.fromiter(message.time_stamps, dtype=np.uint64,
                                count=len(message.time_stamps)),
        received_at=received_at,
    )


def encode_batch(batch: Batch) -> bytes:
    """The inverse, for a console stand-in."""
    message = _Message(
        mz=batch.mz.tolist(),
        tic=batch.tic.tolist(),
        time_stamps=batch.time_stamps.tolist(),
    )
    return compress(message.SerializeToString())


__all__ = [
    "ACK",
    "COMMAND_PORT",
    "DATA_PORT",
    "ERROR_PREFIX",
    "FINISHED",
    "FINISHED_ACQUIRE",
    "FRAME_TYPES",
    "GATE_GRANULARITY_SAMPLES",
    "SCAN_NUM_SMALLINT_MAX",
    "SECONDS_PER_SAMPLE_2GSPS",
    "SILENT_COMMANDS",
    "TOPIC_DATA",
    "TOPIC_STATUS",
    "AcqError",
    "Batch",
    "ConsoleAcquisitionError",
    "ConsoleCommandError",
    "ConsoleInfo",
    "ConsoleProtocolError",
    "EmptyFrameError",
    "FrameRequest",
    "Status",
    "TofWidth",
    "compress",
    "decode_batch",
    "decode_tof_width",
    "decompress",
    "encode_batch",
    "encode_tof_width",
    "record_size_samples",
    "samples_at",
]
