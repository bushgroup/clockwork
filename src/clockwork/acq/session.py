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

One judgement does live here, because it has nowhere better to go: what
counts as a frame that worked. The console ends a frame the same way whether
it acquired every scan or died on its first fetch, so `run_frame` refuses to
call a frame that published nothing a success (lab record, task 20).
"""

from __future__ import annotations

from collections.abc import Callable

from .console import ACQUIRE_TIMEOUT_S, Console
from .stream import DataStream
from .wire import FINISHED, Batch, EmptyFrameError, FrameRequest, Status, TofWidth

EMPTY_SETTLE_S = 5.0
"""How long to keep listening for batches after a frame that looked empty.

`finished` overtakes the batches of its own frame, and by much more than the
phrase suggests. The console hands each batch to a subscriber that publishes
it from its own thread on a 10 ms poll, and publishes `finished` itself,
directly. Measured on the bench with 5000 scan frames whose records were kept
entire: half or all of a frame's scans arrived after its own `finished`, the
first of them 0.4 to 0.9 s after it and the last as much as 9.5 s after it
(lab record, task 20). A frame that produced nothing is therefore
indistinguishable at `finished` from the busiest frame there is, and only
waiting tells them apart.

Five seconds is about six times the longest *first* arrival measured, and it
is the first arrival that matters: one batch is enough to prove a frame was
not empty, and the rest may take as long as they like. It is paid only by a
frame that looked empty, which is a failure or a rarity, and never by one
that worked.
"""


def start_chain(
    console: Console,
    stream: DataStream,
    *,
    timeout: float = ACQUIRE_TIMEOUT_S,
    settle: float = 5.0,
    quiet: float = 1.0,
) -> TofWidth:
    """`acquire`, then stop the open-ended acquisition it starts, and clear up after it.

    `acquire` is what measures the pusher period, builds the acquisition chain
    and binds the data socket, and it also starts an acquisition with no length
    and no file. That one has to be stopped before a frame can be asked for,
    and stopping it publishes a `finished` of its own plus however many batches
    it managed in the meantime. Those are consumed here, so that what the
    stream holds afterwards belongs to the frames.

    `settle` is how long to wait for that `finished`. Missing it is not an
    error: it is one message on a socket that drops what it cannot deliver, and
    nothing downstream depends on having seen it.

    `quiet` is how long a silence has to be before the leftovers are taken to
    have stopped arriving, and it is why the clearing up is not simply
    everything already queued. The open-ended acquisition's batches keep
    arriving after its own `finished`, the same way a frame's do, so a console
    that was streaming a fully occupied record has a backlog that outlives the
    stop. Anything still in flight after `quiet` is attributed to the first
    frame, which makes that frame look longer than it was rather than shorter,
    so a generous value costs a second and a mean one costs accuracy (lab
    record, task 20).
    """
    width = console.acquire(timeout=timeout)
    console.stop_frame()
    for event in stream.events(timeout=settle):
        if isinstance(event, Status) and event.text == FINISHED:
            break
    stream.drain(quiet)
    return width


def run_frame(
    console: Console,
    stream: DataStream,
    request: FrameRequest,
    *,
    timeout: float,
    on_batch: Callable[[Batch], None] | None = None,
    allow_empty: bool = False,
    settle: float = EMPTY_SETTLE_S,
) -> Status:
    """Acquire one frame, wait for its end, and stop it so the next one can start.

    The `stop_frame` at the end is not tidying up: the console's acquisition
    thread has ended by the time `finished` arrives but has not been joined,
    and the next start would destroy it unjoined, which kills the process.
    It is sent even when the wait fails, for the same reason.

    Returns the `finished` that ended the frame, and raises rather than
    returning it in three cases:

    `StreamTimeout` if `finished` never came, which says the console died,
    the subscription never reached it, or the client fell far enough behind to
    lose the message.

    `ConsoleAcquisitionError` if the console published an error while the
    frame ran. That is the honest report and it only reaches a client running
    against a console that publishes its errors at all.

    `EmptyFrameError` if the frame ended having published no scans, unless
    `allow_empty` says a caller wants that. This is the one signal a stock
    console gives that an acquisition failed, since it logs the failure and
    then ends the frame with the same `finished` a good frame ends with. It is
    a test on nothing rather than on too little on purpose: the data socket
    drops messages when a client falls behind, so a count short of
    `frame_length` says nothing reliable, while no batches at all means either
    the frame produced nothing or the client missed every message of it, and
    both are failures. What it does not mean is a frame with no ions in it:
    the console publishes a batch per `NotifyOnScansCount` scans whether or
    not anything crossed the threshold (lab record, task 20).
    """
    console.acquire_frame(request)
    scans_before = stream.scans
    try:
        status = stream.wait_for_status(FINISHED, timeout=timeout, on_batch=on_batch)
    finally:
        console.stop_frame()
    if stream.scans == scans_before and not allow_empty:
        # Give the batches that `finished` may have overtaken their chance to
        # arrive before calling the frame empty.
        for event in stream.drain(settle):
            if isinstance(event, Batch) and on_batch is not None:
                on_batch(event)
        if stream.scans == scans_before:
            raise EmptyFrameError(
                f"frame {request.frame_number} asked for {request.frame_length} scans and "
                f"published none in {timeout:g} s plus {settle:g} s of settling; the console "
                "logs an acquisition that failed and ends the frame as though it had not"
            )
    return status


__all__ = ["EMPTY_SETTLE_S", "run_frame", "start_chain"]
