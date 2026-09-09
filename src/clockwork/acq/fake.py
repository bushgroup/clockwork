"""An acquisition console simulated in this process, for development and tests.

`Console` and `DataStream` talk to a ROUTER and a PUB socket and nothing else,
so the same client drives PNNL's console on an instrument PC and the stand-in
below. It exists so that the command sequence, the two-frame `acquire` reply,
the protobuf and Snappy round trips and the two topics can be exercised on a
machine with no digitizer, no card driver and no console build, which is every
machine except the one in the lab.

**What it is.** Real ZeroMQ sockets on a real loopback port, running the
console's own request framing: the identity frame, the delimiter, the payload
frames, `ack`, the two-frame reply to `acquire` and `tof width`, silence for
the seven commands the console ignores, and the record-size arithmetic from the
period it is told to pretend to measure. A client that gets any of that wrong
fails here.

**What it is not.** It has no clock: a frame's batches and its `finished` are
published from inside the handler for `acquire frame`, so everything a caller
can observe about ordering is right and nothing about timing is. The two
numbers task 03 exists to measure, the gap between one frame's end and the next
frame's first record and whether trigger timestamps continue across a frame
boundary, are therefore the two things this cannot tell anyone. It continues
its timestamp counter across frames because it has to do something, not
because a real card does.

It also binds its data socket at startup, where the console binds at its first
`acquire` and unbinds at `stop acquire`. A client that subscribes early works
against both; a client that depends on the socket being absent works against
neither.

What it does model, on purpose, is the whole of the ordering rule, because
that is a set of hazards rather than a set of details. A frame asked for
before any `acquire` reads through a null pointer; a frame or an `acquire`
that starts while the console holds a thread it has not joined destroys that
thread, which calls `std::terminate`; a frame asked for while one is running
is acknowledged and dropped. The first two stop this answering, which is what
a client sees when a console process dies, and `died` says it happened. A
client that keeps to the sequence in `docs/console-protocol.md` never meets
any of them, which is the point of modelling them here rather than finding
out on the instrument.

The open-ended acquisition `acquire` starts is the one thing it cannot
reproduce faithfully, because that one streams until it is stopped and this
has no clock: it publishes `open_batches` batches and then goes quiet, staying
open until stopped. Ordering is right, volume is not.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import zmq

from .wire import (
    ACK,
    FINISHED,
    FINISHED_ACQUIRE,
    SILENT_COMMANDS,
    TOPIC_DATA,
    TOPIC_STATUS,
    Batch,
    FrameRequest,
    TofWidth,
    encode_batch,
    encode_tof_width,
    record_size_samples,
)

DEFAULT_PERIOD_SAMPLES = 8192
"""A cheap stand-in for the instrument's ~258000 samples at 2 GS/s.

Deliberately small: the record it implies is the length of every `mz` array
published, and a realistic one makes every test move a megabyte a batch for
nothing. Pass the real figures when the size is the point.
"""

DEFAULT_POST_TRIGGER_SAMPLES = 1024
DEFAULT_REARM_SAMPLES = 256
DEFAULT_NOTIFY_ON_SCANS_COUNT = 100
"""The console's own default is 500; this keeps a short frame to a few batches."""


class FakeConsole:
    """A console stand-in on a loopback port.

    Start it, point a `Console` at `command_endpoint` and a `DataStream` at
    `data_endpoint`, and stop it when done, or use it as a context manager.
    Everything a test wants to assert about what the client sent is on the
    instance afterwards: `commands`, `frames`, and the settings the commands
    left behind.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        model: str = "SA220P",
        serial: str = "AQ00070766",
        firmware: str = "2.7.1811",
        app: str = "AqMD3_console",
        version: str = "0.1.0-8c5ed07",
        fork: str = "bushgroup/AqMD3-Acquisition-Console",
        branch: str = "clockwork",
        instruments: int = 1,
        pusher_period_samples: int = DEFAULT_PERIOD_SAMPLES,
        post_trigger_samples: int = DEFAULT_POST_TRIGGER_SAMPLES,
        rearm_samples: int = DEFAULT_REARM_SAMPLES,
        notify_on_scans_count: int = DEFAULT_NOTIFY_ON_SCANS_COUNT,
        open_batches: int = 2,
        subscriber_wait_s: float = 1.0,
        context: zmq.Context | None = None,
    ) -> None:
        self.host = host
        self.model = model
        self.serial_number = serial
        self.firmware_revision = firmware
        self.app = app
        self.version = version
        self.fork = fork
        """Empty for a stand-in that should look like a stock console, which is
        the build that ignores everything `config.txt` says."""

        self.branch = branch
        self.instruments = instruments
        self.notify_on_scans_count = notify_on_scans_count
        self.open_batches = open_batches
        """Batches to publish for the open-ended acquisition before going quiet."""

        self.subscriber_wait_s = subscriber_wait_s
        """How long to wait for a subscription to arrive before publishing.

        The real console has no such courtesy: PUB drops what has no
        subscriber, so a client that subscribes a moment late loses the first
        batches. Waiting here is what makes a test deterministic rather than
        usually green, and it is waited for once; after that this gives up and
        publishes into the void as the console would.
        """

        self.pusher_period_samples = pusher_period_samples
        self.post_trigger_samples = post_trigger_samples
        self.rearm_samples = rearm_samples
        self.record_samples = record_size_samples(
            pusher_period_samples, post_trigger_samples, rearm_samples
        )
        self.num_samples = self.record_samples + post_trigger_samples

        # What the client did, for a test to read afterwards.
        self.commands: list[tuple[str, ...]] = []
        self.frames: list[FrameRequest] = []
        self.initialised = False
        self.seconds_per_sample: float | None = None
        self.offset_v: float | None = None
        self.inverted: bool | None = None
        self.io_ports_enabled: list[int] = []
        self.chain = False
        """Whether `acquire` has built an acquisition chain."""

        self.running = False
        """Whether an acquisition is still streaming."""

        self.unjoined = False
        """Whether a started acquisition has yet to be stopped.

        The console's own thread handle, in effect. True from the moment an
        acquisition starts until a `stop` joins it, including after the
        acquisition has ended by itself, which is what makes a second start
        without a stop fatal.
        """

        self.ignored_frames = 0
        """Frames acknowledged and dropped because one was already running."""

        self.died = False
        """Set when the client did something the real console does not survive."""

        self.subscriptions: set[str] = set()

        self._context = context if context is not None else zmq.Context.instance()
        self._router: zmq.Socket | None = None
        self._pub: zmq.Socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._gave_up: set[str] = set()
        self._timestamp = 0
        self.command_endpoint = ""
        self.data_endpoint = ""

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> FakeConsole:
        """Bind both sockets and serve on a worker thread.

        The sockets are made here and used only by that thread afterwards,
        which is the one way a ZeroMQ socket may cross threads: starting the
        thread is the memory barrier, and nothing on this side touches them
        again.
        """
        router = self._context.socket(zmq.ROUTER)
        router.setsockopt(zmq.LINGER, 0)
        command_port = router.bind_to_random_port(f"tcp://{self.host}")
        # XPUB rather than PUB, purely so that a subscription can be waited
        # for; on the sending side the two behave alike.
        pub = self._context.socket(zmq.XPUB)
        pub.setsockopt(zmq.LINGER, 0)
        pub.setsockopt(zmq.XPUB_VERBOSE, 1)
        data_port = pub.bind_to_random_port(f"tcp://{self.host}")
        self._router, self._pub = router, pub
        self.command_endpoint = f"tcp://{self.host}:{command_port}"
        self.data_endpoint = f"tcp://{self.host}:{data_port}"
        self._thread = threading.Thread(target=self._serve, name="FakeConsole", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def __enter__(self) -> FakeConsole:
        return self.start() if self._thread is None else self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- the server --------------------------------------------------------

    def _serve(self) -> None:
        router, pub = self._router, self._pub
        assert router is not None and pub is not None
        poller = zmq.Poller()
        poller.register(router, zmq.POLLIN)
        poller.register(pub, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                ready = dict(poller.poll(20))
                if ready.get(pub) == zmq.POLLIN:
                    self._note_subscription(pub.recv())
                if ready.get(router) != zmq.POLLIN:
                    continue
                frames = router.recv_multipart()
                if self.died:
                    continue
                self._handle(frames)
        finally:
            router.close(linger=0)
            pub.close(linger=0)

    def _note_subscription(self, message: bytes) -> None:
        if not message:
            return
        kind, topic = message[0], message[1:].decode("utf-8", "replace")
        if kind == 1:
            self.subscriptions.add(topic)
        else:
            self.subscriptions.discard(topic)

    def _handle(self, frames: list[bytes]) -> None:
        """One request: identity, a delimiter, then the payload frames."""
        identity, payload = frames[0], list(frames[1:])
        while payload and payload[0] == b"":
            payload.pop(0)
        if not payload:
            return
        command = payload[0].decode("utf-8", "replace")
        arguments = payload[1:]
        # A frame request is compressed protobuf and would be noise in a log of
        # what was sent; the decoded requests are in `frames` instead.
        shown = ["<request>"] * len(arguments) if command == "acquire frame" else [
            part.decode("utf-8", "replace") for part in arguments
        ]
        self.commands.append(tuple([command] + shown))
        if command in SILENT_COMMANDS:
            return
        handler = getattr(self, "_do_" + command.replace(" ", "_"), None)
        if handler is None:
            # The console's dispatch is a chain of string comparisons with no
            # final else, so an unknown command produces nothing at all.
            return
        handler(identity, arguments)

    # -- replies -----------------------------------------------------------

    def _respond(self, identity: bytes, *parts: bytes) -> None:
        assert self._router is not None
        self._router.send_multipart([identity, b""] + list(parts))

    def _ack(self, identity: bytes) -> None:
        self._respond(identity, ACK.encode("ascii"))

    def _publish(self, topic: str, payload: bytes) -> None:
        """Publish, having first given a subscription for this topic time to land.

        Per topic, and by prefix, because what a client subscribes to is its
        own business: one socket on the empty prefix covers both topics, and a
        client that named only one is entitled to miss the other. Waited for
        once per topic, then given up on and published into the void as the
        console would.
        """
        assert self._pub is not None
        deadline = time.monotonic() + self.subscriber_wait_s
        while not self._subscribed(topic) and topic not in self._gave_up:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._gave_up.add(topic)
                break
            if self._pub.poll(int(remaining * 1000), zmq.POLLIN):
                self._note_subscription(self._pub.recv())
        self._pub.send_multipart([topic.encode("utf-8"), payload])

    def _subscribed(self, topic: str) -> bool:
        """Whether any subscription covers `topic`, matching by prefix as ZeroMQ does.

        A client that subscribes to the empty prefix, which is how a client
        takes both topics with one socket, is subscribed to everything.
        """
        return any(topic.startswith(prefix) for prefix in self.subscriptions)

    # -- the command set ---------------------------------------------------

    def _do_num_instruments(self, identity: bytes, _: list[bytes]) -> None:
        self._respond(identity, str(self.instruments).encode("ascii"))

    def _do_info(self, identity: bytes, _: list[bytes]) -> None:
        text = (
            f"Digitizer Model: {self.model}"
            f" / Digitizer Serial No.: {self.serial_number}"
            f" / Digitizer Firmware Version: {self.firmware_revision}"
            f" / App: {self.app}"
            f" / App Version: {self.version}"
        )
        if self.fork:
            text += f" / Fork: {self.fork}@{self.branch}"
        self._respond(identity, text.encode("utf-8"))

    def _do_firmware(self, identity: bytes, _: list[bytes]) -> None:
        self._respond(identity, self.firmware_revision.encode("ascii"))

    def _do_serial(self, identity: bytes, _: list[bytes]) -> None:
        self._respond(identity, self.serial_number.encode("ascii"))

    def _do_init(self, identity: bytes, _: list[bytes]) -> None:
        self.initialised = True
        self._ack(identity)

    def _do_horizontal(self, identity: bytes, arguments: list[bytes]) -> None:
        if arguments:
            self.seconds_per_sample = float(arguments[0])
        self._ack(identity)

    def _do_vertical(self, identity: bytes, arguments: list[bytes]) -> None:
        if arguments:
            self.offset_v = float(arguments[0])
        self._ack(identity)

    def _do_invert(self, identity: bytes, arguments: list[bytes]) -> None:
        if arguments:
            self.inverted = arguments[0] == b"true"
        self._ack(identity)

    def _do_setup_array(self, identity: bytes, _: list[bytes]) -> None:
        self._ack(identity)

    def _do_enable_io_port(self, identity: bytes, arguments: list[bytes]) -> None:
        if arguments:
            # The console parses the number and then ignores it, using the port
            # `config.txt` names. Parsing it here keeps a client honest about
            # sending one at all.
            self.io_ports_enabled.append(int(arguments[0]))
        self._ack(identity)

    def _do_disable_io_port(self, identity: bytes, arguments: list[bytes]) -> None:
        if arguments and int(arguments[0]) in self.io_ports_enabled:
            self.io_ports_enabled.remove(int(arguments[0]))
        self._ack(identity)

    def _do_tof_width(self, identity: bytes, _: list[bytes]) -> None:
        payload, digest = encode_tof_width(self.tof_width())
        self._respond(identity, payload, digest.encode("ascii"))

    def _do_acquire(self, identity: bytes, _: list[bytes]) -> None:
        if self.unjoined:
            # Replacing the controller destroys a thread nobody joined.
            self.died = True
            return
        self.chain = True
        self.running = True
        self.unjoined = True
        payload, digest = encode_tof_width(self.tof_width())
        self._respond(identity, payload, digest.encode("ascii"))
        # Open-ended: a few batches, then quiet until stopped. No `finished`.
        for _batch in range(self.open_batches):
            self._publish(TOPIC_DATA, encode_batch(self._batch(self.notify_on_scans_count)))

    def _do_acquire_frame(self, identity: bytes, arguments: list[bytes]) -> None:
        if not self.chain:
            # The null-pointer dereference. A dead process answers nothing.
            self.died = True
            return
        if self.running:
            # Acknowledged, logged, and started anyway by nobody.
            self.ignored_frames += 1
            self._ack(identity)
            return
        if self.unjoined:
            self.died = True
            return
        if not arguments:
            self._ack(identity)
            return
        request = FrameRequest.decode(arguments[0])
        self.frames.append(request)
        self.running = True
        self.unjoined = True
        self._ack(identity)
        self._run_frame(request)

    def _do_stop(self, identity: bytes, arguments: list[bytes]) -> None:
        if len(arguments) != 1:
            # `stop` needs exactly two frames or the console says nothing.
            return
        was_running, self.running = self.running, False
        self.unjoined = False
        if arguments[0] == b"acquire":
            self.chain = False
            self._ack(identity)
            if was_running:
                self._publish(TOPIC_STATUS, FINISHED.encode("ascii"))
            self._publish(TOPIC_STATUS, FINISHED_ACQUIRE.encode("ascii"))
            return
        self._ack(identity)
        if was_running:
            # An acquisition cut short publishes its own end.
            self._publish(TOPIC_STATUS, FINISHED.encode("ascii"))

    # -- a frame -----------------------------------------------------------

    def tof_width(self) -> TofWidth:
        return TofWidth(
            pusher_pulse_width=self.pusher_period_samples,
            num_samples=self.num_samples,
        )

    def _run_frame(self, request: FrameRequest) -> None:
        """Publish the frame's batches and its end, with no time passing."""
        remaining = int(request.frame_length)
        while remaining > 0:
            scans = min(remaining, self.notify_on_scans_count)
            self._publish(TOPIC_DATA, encode_batch(self._batch(scans)))
            remaining -= scans
        # The frame's scans are all in, so its thread ends and says so; the
        # handle stays unjoined until a `stop` arrives.
        self.running = False
        self._publish(TOPIC_STATUS, FINISHED.encode("ascii"))

    def _batch(self, scans: int) -> Batch:
        """A summary shaped like the console's, with invented contents.

        Sparse, because a zero-suppressed spectrum is: a handful of non-zero
        bins per scan, summed over the batch into the dense array the console
        publishes. The peak positions are fixed so that a caller comparing two
        batches is comparing counts and not noise.
        """
        mz = np.zeros(self.num_samples, dtype=np.int32)
        peaks = np.linspace(
            self.post_trigger_samples, self.num_samples - 1, num=7, dtype=np.int64
        )
        mz[peaks] = np.array([120, 400, 2600, 900, 300, 80, 40], dtype=np.int32) * scans
        per_scan = int(mz.sum() // max(scans, 1))
        stamps = self._timestamp + self.pusher_period_samples * np.arange(
            1, scans + 1, dtype=np.uint64
        )
        self._timestamp = int(stamps[-1])
        return Batch(
            mz=mz,
            tic=np.full(scans, per_scan, dtype=np.uint32),
            time_stamps=stamps,
        )


__all__ = [
    "DEFAULT_NOTIFY_ON_SCANS_COUNT",
    "DEFAULT_PERIOD_SAMPLES",
    "DEFAULT_POST_TRIGGER_SAMPLES",
    "DEFAULT_REARM_SAMPLES",
    "FakeConsole",
]
