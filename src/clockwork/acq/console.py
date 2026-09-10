"""The acquisition console's command socket, as the rest of clockwork uses it.

One `Console` is one conversation with one console process: configure the
digitizer, start the acquisition chain, ask for a frame, stop. Every wire fact
is `docs/console-protocol.md`; this module implements that document and decides
nothing about the protocol itself. The live data the console publishes while a
frame runs comes back on the other socket, which is `stream.py`.

Three properties are the point of the design.

*A request that goes unanswered does not wedge the client.* The console has
commands it accepts and never answers, and commands that do not answer until
the digitizer has seen twenty trigger pulses, which on a bench with the pulse
generator switched off is never. A REQ socket in either situation is stuck for
good, because REQ enforces send-then-receive and cannot be sent on again. This
uses a DEALER with its own delimiter frame, waits on a deadline, and on a
timeout throws the socket away and reconnects, so the late reply cannot be
mistaken for the answer to the next question.

*The commands that answer nothing are refused rather than waited on.* Seven of
them are TODOs in the console's source. `send_only` exists for anyone who
wants to send one anyway; the path that waits will not.

*The order acquisitions come in is enforced here, because the console does not
enforce it and does not survive it being wrong.* One acquisition runs at a
time and every one of them is ended with a `stop` before the next begins; a
start that breaks that rule destroys a thread the console has not joined,
which kills the process, and one that comes before any `acquire` reads through
a null pointer, which also kills it. There is no reply that says so. The two
flags below track what the console is doing, and every method that starts
something checks them: `acquiring` for the chain, `running` for an acquisition
inside it. `session.py` puts the whole sequence in two functions so that a
caller does not have to hold it in mind.

Blocking, and deliberately so: every request waits on a deadline. Nothing here
may be called from the UI thread. A ZeroMQ socket belongs to the thread that
made it, so one `Console` belongs to one worker thread and is not shared.
"""

from __future__ import annotations

import time

import zmq

from .wire import (
    ACK,
    COMMAND_PORT,
    ERROR_PREFIX,
    SECONDS_PER_SAMPLE_2GSPS,
    SILENT_COMMANDS,
    AcqError,
    ConsoleCommandError,
    ConsoleInfo,
    ConsoleProtocolError,
    FrameRequest,
    TofWidth,
    decode_tof_width,
)

DEFAULT_TIMEOUT_S = 10.0
"""Long enough for any command that does not wait on the digitizer.

Generous on purpose. The console opens the card before it answers anything at
all, which took 5.2 s on the instrument PC, so a client that starts talking to
a console that has just been launched is talking to a process that is not
listening yet (lab record, task 17).
"""

ACQUIRE_TIMEOUT_S = 60.0
"""For `acquire` and `tof width`, which measure the pusher period first.

Twenty trigger pulses at 129 us is under 3 ms, so this is not about the
measurement taking long: it is about what happens when no pulse arrives. The
console waits, and this timeout is the client's only way out of that.
"""

STOP_ACQUIRE_TIMEOUT_S = 60.0
"""For `stop acquire`, which does not answer until every subscriber has drained.

The one command whose reply waits on however much data the last frame
produced. Measured at 11.2 s on the bench for a 5000 scan frame at 2 GS/s
whose records were entirely unsuppressed, which is the largest a frame of that
length can be, and the ordinary timeout is 10 s: this is a command that failed
on the first fully occupied frame anyone ran through it (lab record, task 20).
Sixty seconds is not a measurement, it is room, and it is bounded because the
console's own publish of `finished acquire` gives up after thirty.
"""


class ConsoleTimeout(AcqError):
    """The console did not answer in time.

    The socket has been reconnected by the time this is raised, so the client
    is usable again. The console is not necessarily: a timeout on `acquire`
    leaves it part way through measuring a period it will never measure, and
    a timeout on anything else means it is busy with something or gone.
    """


class ConsoleStateError(AcqError):
    """The command would have been sent in an order the console cannot take."""


class Console:
    """A client of one console's command socket.

    Nothing is sent by the constructor, so making one costs a socket and no
    round trip; a console that is not running looks exactly like one that is
    until the first request times out. `info()` is the cheapest way to find out
    which, and the only way to tell a forked console from a stock one.
    """

    def __init__(
        self,
        endpoint: str = f"tcp://127.0.0.1:{COMMAND_PORT}",
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        context: zmq.Context | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.acquiring = False
        """Whether `acquire` has built the console's acquisition chain.

        Set by `acquire`, cleared by `stop_acquire`. Set it by hand, along
        with `running`, when attaching to a console another client has already
        started, which is the one case this object cannot work out for itself.
        """

        self.running = False
        """Whether an acquisition has been started and not yet stopped.

        True from `acquire` or `acquire_frame` until the next `stop_frame` or
        `stop_acquire`, including after a frame has published its `finished`:
        the console's thread has ended by then but has not been joined, and
        joining it is what `stop` is for.
        """

        self.sample_rate_hz: float | None = None
        """What the last `horizontal` asked for, so a measured pusher period
        can be turned into seconds without the caller carrying the rate."""

        self.last_reply_seconds = 0.0
        """How long the last answered request waited.

        The console times nothing on its behalf, so this is the only figure a
        client has for how long a command actually cost: the 5.2 s card open,
        the period measurement, the setup of a frame.
        """

        self._context = context if context is not None else zmq.Context.instance()
        self._socket: zmq.Socket | None = None
        self._open_socket()

    # -- the socket --------------------------------------------------------

    def _open_socket(self) -> None:
        socket = self._context.socket(zmq.DEALER)
        socket.setsockopt(zmq.LINGER, 0)
        socket.connect(self.endpoint)
        self._socket = socket

    def _reset_socket(self) -> None:
        """Throw the socket away and make another.

        The only reliable way to be sure a reply to a request that timed out
        cannot arrive later and be read as the answer to the next one. Cheap:
        no handshake, and the console neither notices nor cares.
        """
        if self._socket is not None:
            self._socket.close(linger=0)
            self._socket = None
        self._open_socket()

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close(linger=0)
            self._socket = None

    def __enter__(self) -> Console:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- requests ----------------------------------------------------------

    def send_only(self, *frames: str | bytes) -> None:
        """Send a command and do not wait, which is all the silent ones allow.

        Also the honest way to send anything at all when the reply is of no
        interest, though every command that does reply leaves that reply in the
        socket for the next request to trip over, so prefer `request`.
        """
        if self._socket is None:
            raise AcqError("this Console is closed")
        self._socket.send_multipart([b""] + [_frame(part) for part in frames])

    def request(
        self,
        *frames: str | bytes,
        replies: int = 1,
        timeout: float | None = None,
    ) -> list[bytes]:
        """Send a command, wait for its reply frames, and hand them back raw.

        `replies` is 1 for everything except `acquire` and `tof width`, which
        answer with a message and its hash in one multipart reply. A command
        the console never answers is refused here rather than waited on.
        """
        if self._socket is None:
            raise AcqError("this Console is closed")
        command = frames[0] if isinstance(frames[0], str) else frames[0].decode("ascii", "replace")
        if command in SILENT_COMMANDS:
            raise ConsoleStateError(
                f"{command!r} is one of the commands the console accepts and never answers; "
                "send it with send_only if it is wanted at all"
            )
        deadline = self.timeout if timeout is None else timeout
        self._socket.send_multipart([b""] + [_frame(part) for part in frames])
        started = time.monotonic()
        if not self._socket.poll(int(deadline * 1000), zmq.POLLIN):
            self._reset_socket()
            raise ConsoleTimeout(
                f"the console at {self.endpoint} did not answer {command!r} "
                f"within {deadline:g} s"
            )
        reply = self._socket.recv_multipart()
        if reply and reply[0] == b"":
            reply = reply[1:]
        if len(reply) == 1 and _is_error_reply(reply[0]):
            raise ConsoleCommandError(
                f"the console refused {command!r}: "
                f"{reply[0].decode('utf-8', 'replace')[len(ERROR_PREFIX):].strip()}"
            )
        if len(reply) != replies:
            raise ConsoleProtocolError(
                f"{command!r} answered with {len(reply)} frames, not {replies}: "
                f"{[part[:64] for part in reply]!r}"
            )
        self.last_reply_seconds = time.monotonic() - started
        return reply

    def _ack(self, *frames: str, timeout: float | None = None) -> None:
        """A setting command, whose whole reply is the string `ack`."""
        reply = self.request(*frames, timeout=timeout)[0].decode("ascii", "replace")
        if reply != ACK:
            raise ConsoleProtocolError(f"{frames[0]!r} answered {reply!r}, not {ACK!r}")

    def _text(self, *frames: str, timeout: float | None = None) -> str:
        return self.request(*frames, timeout=timeout)[0].decode("utf-8", "replace").strip()

    # -- what the console can be asked ------------------------------------

    def num_instruments(self) -> int:
        """VISA resources matching `PXI?*::INSTR`, which is 0 with no card."""
        reply = self._text("num instruments")
        try:
            return int(reply)
        except ValueError as exc:
            raise ConsoleProtocolError(f"num instruments answered {reply!r}") from exc

    def info(self) -> ConsoleInfo:
        """Card, firmware, console version, and on our build the fork.

        The whole string is what a UIMF provenance stamp should carry, and
        `ConsoleInfo.is_fork` is what says whether the six settings in
        `config.txt` are being read at all: a stock console ignores every one
        of them without a word.
        """
        return ConsoleInfo.parse(self._text("info"))

    def firmware(self) -> str:
        return self._text("firmware")

    def serial(self) -> str:
        return self._text("serial")

    def init(self, *, timeout: float | None = None) -> None:
        """External trigger, with the level, slope and delay `config.txt` sets."""
        self._ack("init", timeout=timeout)

    def horizontal(self, seconds_per_sample: float = SECONDS_PER_SAMPLE_2GSPS) -> None:
        """Set the sample rate as its reciprocal, which is what the console takes.

        The default is 2 GS/s, and it is the only rate a zero-suppressed
        separation runs at (lab record, task 03), so passing anything else is
        a deliberate act.
        """
        if seconds_per_sample <= 0:
            raise ValueError("seconds per sample must be positive")
        # Written out in full rather than as an exponent, which is the shape
        # the console's own test client sends and std::stod certainly reads.
        text = f"{seconds_per_sample:.18f}".rstrip("0")
        self._ack("horizontal", text + "0" if text.endswith(".") else text)
        self.sample_rate_hz = 1.0 / seconds_per_sample

    def vertical(self, offset_v: float) -> None:
        """Channel 1's offset, at the full scale `config.txt` sets.

        The offset is the knob that controls how much data a zero-suppressed
        acquisition produces, and the range it moves is the card's window, not
        the signal, so a value that worked on another digitizer does not
        carry across (lab record, task 01).
        """
        self._ack("vertical", repr(float(offset_v)))

    def invert(self, inverted: bool) -> None:
        self._ack("invert", "true" if inverted else "false")

    def enable_io_port(self, port: int = 2) -> None:
        """Make the Control I/O port an acquisition enable input.

        The port number in the command is ignored: the console uses whichever
        port `config.txt` names, defaulting to 2. It is passed anyway, because
        the console reads the frame with `std::stoi` and a request without one
        is a request it cannot parse.
        """
        self._ack("enable io port", str(int(port)))

    def disable_io_port(self, port: int = 2) -> None:
        self._ack("disable io port", str(int(port)))

    def tof_width(self, *, timeout: float = ACQUIRE_TIMEOUT_S) -> TofWidth:
        """Measure the pusher period and size the record to it, acquiring nothing."""
        return self._tof_reply("tof width", timeout=timeout)

    def acquire(self, *, timeout: float = ACQUIRE_TIMEOUT_S) -> TofWidth:
        """Build the acquisition chain and start an open-ended acquisition.

        Measures the period as `tof width` does, configures zero-suppressed
        streaming, and starts an acquisition with no length and no file, which
        streams and publishes until it is stopped. This is also what binds the
        data socket, so a subscriber should already be connected.

        What it leaves behind is an acquisition, not just a chain: a frame
        asked for before this one is stopped is acknowledged and ignored. Send
        `stop_frame` next, which ends it and leaves the chain, and expect the
        `finished` that ending it publishes.

        The period is measured once, here. A pusher that drifts afterwards is
        not re-measured and the record is not resized.
        """
        if self.running:
            raise ConsoleStateError(
                "an acquisition is already running: stop_frame() or stop_acquire() first. "
                "A second acquire replaces the console's acquisition thread without joining "
                "it, which kills the console process rather than earning an error"
            )
        width = self._tof_reply("acquire", timeout=timeout)
        self.acquiring = True
        self.running = True
        return width

    def _tof_reply(self, command: str, *, timeout: float) -> TofWidth:
        payload, digest = self.request(command, replies=2, timeout=timeout)
        return decode_tof_width(payload, digest.decode("ascii", "replace"))

    def acquire_frame(self, request: FrameRequest) -> None:
        """Start one frame, into the UIMF file the request names, and return.

        Returns as soon as the console has taken the request: the frame itself
        ends with `finished` on the data socket's `status` topic, which is what
        `stream.DataStream` is for. The file has to exist already, with its
        schema and parameters written, because the console opens it read-write
        and never creates one.
        """
        if not self.acquiring:
            raise ConsoleStateError(
                "acquire() has to come before acquire_frame(): the console dereferences an "
                "acquisition chain that only acquire() creates, so this order kills the "
                "console process rather than earning an error from it"
            )
        if self.running:
            raise ConsoleStateError(
                "the previous acquisition has not been stopped: stop_frame() first. While one "
                "is still running this frame would be acknowledged and never started, and "
                "after one has ended it would destroy a thread the console has not joined, "
                "which kills the console process"
            )
        self._ack("acquire frame", request.encode())
        self.running = True

    def stop_frame(self, *, timeout: float | None = None) -> None:
        """End the running acquisition and keep the chain for the next one.

        Sent between frames whether or not one is still running: after a frame
        has published its `finished` the console's thread has ended but has not
        been joined, and this is what joins it. An acquisition still running
        when this arrives ends here and publishes its own `finished`; one that
        had already ended publishes nothing further. It does not wait for the
        subscribers, so it is as quick as any other command.
        """
        try:
            self._ack("stop", "frame", timeout=timeout)
        finally:
            self.running = False

    def stop_acquire(self, *, timeout: float = STOP_ACQUIRE_TIMEOUT_S) -> None:
        """Stop and tear the acquisition chain down.

        Followed by `finished acquire` on the status topic once every
        subscriber has drained, which unlike `finished` does mean the writing
        is done. The next `acquire` builds a fresh chain and rebinds the data
        socket.

        The draining happens before the reply, not after it, so this is the one
        command that can take tens of seconds and it has its own timeout to
        match.

        Both stops clear their flags even when the reply never comes. The
        command went out, and the console takes its requests one at a time off
        a poll loop, so a stop that was sent is a stop that will be acted on:
        the flags describe the console and not the reply. Leaving them set
        would refuse every later acquisition on this object for a stop that had
        in fact happened.
        """
        try:
            self._ack("stop", "acquire", timeout=timeout)
        finally:
            self.acquiring = False
            self.running = False

    # -- the sequence, in the order the console expects it ------------------

    def configure(
        self,
        *,
        offset_v: float,
        inverted: bool = False,
        seconds_per_sample: float = SECONDS_PER_SAMPLE_2GSPS,
        io_port: int | None = 2,
        timeout: float | None = None,
    ) -> None:
        """`init`, `horizontal`, `vertical`, `invert`, and the enable input.

        The first four in the order the console's own test client uses them.
        The fifth is not in that client and is what a real experiment needs:
        the Control I/O port has to be an enable input before the start edge
        can release the digitizer. Pass `io_port=None` to leave it alone.
        """
        self.init(timeout=timeout)
        self.horizontal(seconds_per_sample)
        self.vertical(offset_v)
        self.invert(inverted)
        if io_port is not None:
            self.enable_io_port(io_port)


def _frame(part: str | bytes) -> bytes:
    """Command frames are plain strings; only `acquire frame`'s is bytes."""
    return part if isinstance(part, bytes) else part.encode("utf-8")


def _is_error_reply(frame: bytes) -> bool:
    """Whether a one-frame reply is the console saying the command failed.

    The same `error <what>` shape the status topic uses, on the command socket
    and in place of the reply. Only a console that has an error boundary around
    its command handlers ever sends it, and only in place of a reply, so
    checking a single-frame reply is enough: nothing the protocol document
    lists answers with one frame beginning `error`.
    """
    text = frame.decode("utf-8", "replace")
    return text == ERROR_PREFIX or text.startswith(ERROR_PREFIX + " ")


__all__ = [
    "ACQUIRE_TIMEOUT_S",
    "DEFAULT_TIMEOUT_S",
    "STOP_ACQUIRE_TIMEOUT_S",
    "Console",
    "ConsoleStateError",
    "ConsoleTimeout",
]
