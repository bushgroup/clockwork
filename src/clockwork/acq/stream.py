"""The acquisition console's data socket: batch summaries and frame status.

While a frame runs the console publishes two different things on one PUB
socket: a summary of every `NotifyOnScansCount` scans on topic `data`, and the
plain strings `finished` and `finished acquire` on topic `status`. The first is
what a live TIC is drawn from and the second is how a client knows a frame
ended, so a client needs both (`docs/console-protocol.md`).

Three decisions are worth stating, because all three are visible in what this
returns.

*One subscription, on the empty prefix, taking both topics.* Splitting them
across two sockets was tried and rejected. It would keep a frame end from ever
queueing behind a display backlog, but two sockets are two connections and
ZeroMQ orders neither against the other, so `finished` could arrive before the
last batch of the frame it ends. Ordering is an everyday property and the
crowding it would buy protection against needs a client that has fallen
hundreds of messages behind, which has failed at something more serious than
message loss.

*A bounded queue, and a dropped message is a reported failure.* The queue is
deep enough to ride out a stalled repaint and shallow enough that a client
which has stopped reading does not grow without limit. A client left far
enough behind loses messages, and if one of them is a frame end the wait for
it raises rather than hanging, which is the honest outcome: it could not keep
up.

*Every event carries the host's clock.* The console stamps its messages with
digitizer sample counts and nothing else, so the only measure of when
something arrived is when it was read. `received_at` is what makes the gap
between one frame's `finished` and the next frame's first batch measurable at
all (lab record, task 03).

The console binds this socket inside its first `acquire`, so connecting before
the console has ever acquired is normal and ZeroMQ reconnects on its own; it
keeps the socket afterwards, so one subscription lasts a session. What is not
recoverable is subscribing late: a subscription that has not reached the
console yet loses whatever was published in the meantime.

Blocking, like the command socket, and the same thread rule: one `DataStream`
belongs to one worker thread.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator

import zmq

from .wire import (
    DATA_PORT,
    TOPIC_DATA,
    AcqError,
    Batch,
    Status,
    decode_batch,
)

QUEUE_MESSAGES = 200
"""Messages to hold before the oldest are dropped.

A display buffer, not a data path: nothing on this socket is the acquired
data, which goes from the console straight into the UIMF file. At the
instrument's record size a batch is a few hundred kilobytes, so two hundred of
them is tens of megabytes and about twenty frames of slack.
"""


class StreamTimeout(AcqError):
    """Nothing expected arrived on the data socket in time.

    Which has three causes worth telling apart, and this cannot tell them
    apart: the console died, the subscription never reached it, or the client
    fell so far behind that the message it was waiting for was dropped.
    """


class DataStream:
    """A subscriber to one console's data socket.

    Subscribes on construction, so the usual shape is to make one, then
    configure and `acquire` on the command socket, which is the order that
    avoids losing the first batches.
    """

    def __init__(
        self,
        endpoint: str = f"tcp://127.0.0.1:{DATA_PORT}",
        *,
        queue: int = QUEUE_MESSAGES,
        context: zmq.Context | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.batches = 0
        self.scans = 0
        """Scans seen on the data topic, which is every scan the console
        acquired unless the queue dropped some. Not a row count."""

        self.statuses: list[Status] = []
        """Every status message, in the order it arrived, kept because there
        are few of them and the sequence is the record of what the console
        did."""

        self._context = context if context is not None else zmq.Context.instance()
        socket = self._context.socket(zmq.SUB)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.RCVHWM, queue)
        # The empty prefix. ZeroMQ matches a subscription by prefix, so this
        # is both topics and anything a later console adds.
        socket.setsockopt_string(zmq.SUBSCRIBE, "")
        socket.connect(endpoint)
        self._socket = socket

    def close(self) -> None:
        if not self._socket.closed:
            self._socket.close(linger=0)

    def __enter__(self) -> DataStream:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- reading -----------------------------------------------------------

    def poll(self, timeout: float = 0.1) -> Batch | Status | None:
        """The next event, or None if `timeout` seconds pass with none."""
        if not self._socket.poll(int(timeout * 1000), zmq.POLLIN):
            return None
        return self._read()

    def _read(self) -> Batch | Status:
        frames = self._socket.recv_multipart()
        received_at = time.perf_counter()
        if len(frames) != 2:
            raise AcqError(
                f"the data socket sent {len(frames)} frames, not a topic and a payload"
            )
        topic = frames[0].decode("utf-8", "replace")
        if topic == TOPIC_DATA:
            batch = decode_batch(frames[1], received_at=received_at)
            self.batches += 1
            self.scans += batch.scans
            return batch
        # Anything that is not data is a plain string, which today means the
        # status topic. A topic a later console adds arrives here rather than
        # being dropped, carrying its own name, so it can be seen and named.
        status = Status(
            text=frames[1].decode("utf-8", "replace").strip(),
            received_at=received_at,
            topic=topic,
        )
        self.statuses.append(status)
        return status

    def drain(self, timeout: float = 0.0) -> list[Batch | Status]:
        """Everything waiting now, plus whatever arrives within `timeout`."""
        out: list[Batch | Status] = []
        deadline = time.monotonic() + timeout
        while True:
            event = self.poll(max(0.0, deadline - time.monotonic()))
            if event is None:
                return out
            out.append(event)

    def events(self, *, timeout: float) -> Iterator[Batch | Status]:
        """Events until `timeout` seconds pass with nothing arriving."""
        while True:
            event = self.poll(timeout)
            if event is None:
                return
            yield event

    def wait_for_status(
        self,
        text: str,
        *,
        timeout: float,
        on_batch: Callable[[Batch], None] | None = None,
    ) -> Status:
        """Wait for one status message, handing batches to `on_batch` meanwhile.

        Because both topics come off one socket in the order they were
        published, every batch of the frame reaches `on_batch` before the
        `finished` that ends it. That is the whole reason for the single
        subscription.

        `timeout` is the whole wait, not the gap between messages: a frame
        either ends inside it or something is wrong. Raises `StreamTimeout`
        when it does not, which is the failure a caller has to be ready for on
        every frame.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StreamTimeout(
                    f"{text!r} did not arrive on {self.endpoint} within {timeout:g} s "
                    f"({self.batches} batches, {self.scans} scans seen)"
                )
            event = self.poll(min(remaining, 0.1))
            if isinstance(event, Status) and event.text == text:
                return event
            if isinstance(event, Batch) and on_batch is not None:
                on_batch(event)


__all__ = [
    "QUEUE_MESSAGES",
    "DataStream",
    "StreamTimeout",
]
