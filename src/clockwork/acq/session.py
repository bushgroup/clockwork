"""The command socket and the data socket, driven together.

`Console` sends commands and `DataStream` reads what the console publishes,
and neither knows about the other. Two things a client does need both for, and
both are places where getting the order wrong kills the console rather than
earning an error from it, so they live here once rather than in every caller:
opening the acquisition chain, and running one frame.

The whole sequence, which is `docs/console-protocol.md` "The order commands
have to come in":

    with DataStream(data) as stream, Console(commands) as console:
        console.configure(offset_v=0.251)
        width = start_chain(console, stream)
        for number in range(1, repetitions + 1):
            run_frame(console, stream, FrameRequest(...), timeout=30.0)
        console.stop_acquire()

Nothing here decides anything about an experiment: how many frames, how long,
and into which file are the method's business (`clockwork.method`), and the
fold that sums repetitions is not this layer's either. This is the two sockets
kept in step, and no more than that.
"""

from __future__ import annotations

from collections.abc import Callable

from .console import ACQUIRE_TIMEOUT_S, Console
from .stream import DataStream
from .wire import FINISHED, Batch, FrameRequest, Status, TofWidth


def start_chain(
    console: Console,
    stream: DataStream,
    *,
    timeout: float = ACQUIRE_TIMEOUT_S,
    settle: float = 5.0,
) -> TofWidth:
    """`acquire`, then stop the open-ended acquisition it starts, and clear up after it.

    `acquire` is what measures the pusher period, builds the acquisition chain
    and binds the data socket, and it also starts an acquisition with no length
    and no file. That one has to be stopped before a frame can be asked for,
    and stopping it publishes a `finished` of its own plus however many batches
    it managed in the meantime. All of that is consumed here, so that what the
    stream holds afterwards belongs to the frames.

    `settle` is how long to wait for that `finished`. Missing it is not an
    error: it is one message on a socket that drops what it cannot deliver, and
    nothing downstream depends on having seen it.
    """
    width = console.acquire(timeout=timeout)
    console.stop_frame()
    for event in stream.events(timeout=settle):
        if isinstance(event, Status) and event.text == FINISHED:
            break
    stream.drain(0.0)
    return width


def run_frame(
    console: Console,
    stream: DataStream,
    request: FrameRequest,
    *,
    timeout: float,
    on_batch: Callable[[Batch], None] | None = None,
) -> Status:
    """Acquire one frame, wait for its end, and stop it so the next one can start.

    The `stop_frame` at the end is not tidying up: the console's acquisition
    thread has ended by the time `finished` arrives but has not been joined,
    and the next start would destroy it unjoined, which kills the process.
    It is sent even when the wait fails, for the same reason.

    Returns the `finished` that ended the frame. Raises `StreamTimeout` if it
    never came, which says the console died, the subscription never reached it,
    or the client fell far enough behind to lose the message.
    """
    console.acquire_frame(request)
    try:
        return stream.wait_for_status(FINISHED, timeout=timeout, on_batch=on_batch)
    finally:
        console.stop_frame()


__all__ = ["run_frame", "start_chain"]
