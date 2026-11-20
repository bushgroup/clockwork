"""A client following a job must not change the acquisition's timing (lab #25, lab #27).

A follower that started a new process every few seconds asked the daemon for its job's
whole history each time, and the daemon encoded it on its one loop, in the process that
runs the acquisition: AUKLET's `TBLSTRT` acknowledgements waited, the gate guard dropped
repetitions, and the window's `status` timed out and offered Find boxes (lab record,
task 170). These tests hold a daemon to three things: a job's `summary` is cheap and
right however long the job; an `events` reply of a full-cap history holds no other
thread of the daemon's process for long, and delays no other request; and the window
tells a daemon that is busy from one that has gone.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
import zmq

from clockwork.acq import BatchSeen, FrameBegun, FrameEnded, PhaseSent
from clockwork.acq.wire import Batch
from clockwork.owner import (
    Discover,
    Handle,
    JobFinished,
    JobStarted,
    JobSummary,
    LocalOwner,
    PointStarted,
    Progress,
    RunDone,
    Said,
    StaleHandle,
    wire,
)
from clockwork.owner.interface import summarize
from clockwork.owner.remote import (
    _COMMANDS,
    EVENTS_PAGE,
    DaemonServer,
    RemoteOwner,
    _encoded,
    _Pieces,
)

LIMIT = 20_000
"""`LocalOwner`'s default `limit`: the most one job's history keeps."""
SCANS = 500
"""`NotifyOnScansCount` on the instrument."""
STALL_MS = 50
"""The most the serve may hold its other threads while answering a follower: under one
batch, 500 pushes or 64.5 ms of recording, since the gate guard reads a batch published
before the start list has been acknowledged as a stale gate.

On MASSTRO (lab record, task 170) one `events` reply of this history held them for about
600 ms before, and a `status` waited 3 s behind it; built beside the loop, a piece at a
time with rests, it holds them for about 15 ms, one tick of the Windows timer, and a
`status` waits under 10 ms. The bound leaves room for a loaded test machine."""


def _run() -> object:
    return wire.example(wire.wire_types()["Run"])


def _history(owner: LocalOwner, handle: Handle, entries: int) -> None:
    """`entries` events against `handle` shaped like a long series' repetitions: a
    frame begun, a start list sent, a batch kept with its arrays, a frame ended; and a
    run done every hundred repetitions."""
    rng = np.random.default_rng(170)
    ended = wire.example(FrameEnded)
    job = Discover()
    owner._report(JobStarted(handle=handle, job=job), handle)
    repetition = 0
    while len(owner._history[handle.id]) < entries:
        repetition += 1
        owner._report(FrameBegun(method_frame=1, repetition=repetition,
                                 frame_number=repetition, of=100), handle)
        owner._report(PhaseSent(box="auklet", phase="start", command="TBLSTRT",
                                seconds=0.002, detail="ACK"), handle)
        batch = Batch(mz=np.zeros(0),
                      tic=rng.integers(0, 60_000, SCANS).astype(np.uint32),
                      time_stamps=(np.arange(SCANS, dtype=np.uint64) * 258_007
                                   + 10**12 + repetition),
                      received_at=time.perf_counter())
        owner._report(BatchSeen(method_frame=1, repetition=repetition, batch=batch,
                                scans_so_far=7421), handle)
        owner._report(ended, handle)
        if repetition % 100 == 0:
            owner._report(RunDone(run=_run()), handle)


class Served:
    """A `DaemonServer` over an owner that is never started, served on a thread, and a
    thread beside it that wakes every millisecond and keeps its worst lateness."""

    def __init__(self, owner: LocalOwner) -> None:
        self.owner = owner
        self.server = DaemonServer(owner, command="tcp://127.0.0.1:*",
                                   events="tcp://127.0.0.1:*")
        self.thread = threading.Thread(target=self.server.serve, name="test serve")
        self.thread.start()
        self.worst = 0.0
        self._stop = threading.Event()
        self._waker = threading.Thread(target=self._wake, name="test waker")
        self._waker.start()

    def _wake(self) -> None:
        while not self._stop.is_set():
            began = time.perf_counter()
            time.sleep(0.001)
            self.worst = max(self.worst, time.perf_counter() - began)

    def close(self) -> None:
        self._stop.set()
        self._waker.join()
        self.owner.shutdown("the test is over")
        self.server.request_shutdown("the test is over")
        self.owner.serve()  # nothing queued that runs: closes up so `join` answers
        self.thread.join(30)
        assert not self.thread.is_alive()


@pytest.fixture
def long_job():
    owner = LocalOwner(fake=True, limit=LIMIT)
    handle = owner.submit(Discover())  # never runs: the owner is not started
    _history(owner, handle, LIMIT)
    served = Served(owner)
    yield served, handle
    served.close()


# -- the summary ---------------------------------------------------------------------


def test_a_kept_summary_counts_every_run_after_the_history_lets_old_ones_go():
    owner = LocalOwner(fake=True, limit=10)
    handle = owner.submit(Discover())
    job = Discover()
    owner._report(JobStarted(handle=handle, job=job), handle)
    for _ in range(30):
        owner._report(RunDone(run=_run()), handle)
    point = wire.example(PointStarted)
    owner._report(point, handle)
    owner._report(JobFinished(handle=handle, job=job, result=None), handle)

    kept = owner.summary(handle)
    assert len(owner.events(handle)) == 10
    assert kept.runs_done == 30 and kept.job is job and kept.point is point
    assert isinstance(kept.ended, JobFinished)
    assert kept.last == owner.events(handle)[-1].seq
    # Worked out from the history it undercounts, which is why the owner keeps one.
    assert summarize(handle, owner.events(handle)).runs_done < 30

    assert owner.summary(Handle(id=99, kind="", label="")) == JobSummary(
        handle=Handle(id=99, kind="", label=""))
    with pytest.raises(StaleHandle):
        owner.summary(Handle(id=handle.id, kind="", label="", owner="another"))


def test_summary_crosses_and_matches_the_history_it_sums_up(long_job):
    served, handle = long_job
    with RemoteOwner(served.server.command_endpoint, timeout=10) as client:
        remote = client.summary(handle)
        here = served.owner.summary(handle)
    assert wire.to_wire(remote) == wire.to_wire(here)
    assert remote.scans_so_far == 7421 and remote.runs_done == here.runs_done > 0
    assert remote.last == served.owner.events(handle)[-1].seq


def test_an_older_daemon_without_summary_is_summed_up_from_its_history(long_job):
    served, handle = long_job
    del _COMMANDS["summary"]
    try:
        with RemoteOwner(served.server.command_endpoint, timeout=30) as client:
            remote = client.summary(handle)
    finally:
        _COMMANDS["summary"] = DaemonServer._summary
    assert remote.runs_done == summarize(handle, served.owner.events(handle)).runs_done


# -- the reply -----------------------------------------------------------------------


def test_an_events_reply_is_the_text_one_dumps_would_give():
    import json

    entries = [Progress(seq=1, event=Said(line='é ,"session":')),
               Progress(seq=2, event=Said(line="two"))]
    reply = {"id": 'a ,"session": b', "ok": True, "result": _Pieces(entries),
             "session": "s1"}
    assert _encoded(reply) == json.dumps(
        {**reply, "result": wire.to_wire(entries)}, ensure_ascii=False,
        separators=(",", ":")).encode()


def test_a_full_cap_history_holds_the_serve_briefly_and_status_is_answered_meanwhile(
        long_job):
    served, handle = long_job
    endpoint = served.server.command_endpoint
    status_waits: list[float] = []
    asking = threading.Event()

    def ask_status() -> None:
        with RemoteOwner(endpoint, timeout=10) as other:
            other.hello()
            asking.set()
            for _ in range(40):
                began = time.perf_counter()
                other.status()
                status_waits.append(time.perf_counter() - began)
                time.sleep(0.02)

    poller = threading.Thread(target=ask_status)
    poller.start()
    assert asking.wait(10)
    served.worst = 0.0
    began = time.perf_counter()
    # A raw request, as a client that keeps no copy asks: the whole history from 0.
    socket_ = zmq.Context.instance().socket(zmq.DEALER)
    socket_.setsockopt(zmq.LINGER, 0)
    socket_.connect(endpoint)
    try:
        request = wire.dumps({"id": 1, "cmd": "events",
                              "args": {"handle": handle, "after": 0}})
        socket_.send_multipart([b"", request.encode()])
        assert socket_.poll(60_000), "no reply"
        # Read before this test's own client copies tens of megabytes off the socket,
        # which a follower in its own process does in that process.
        worst_ms = served.worst * 1e3
        took = time.perf_counter() - began
        reply = socket_.recv_multipart()[-1]
    finally:
        socket_.close()
    poller.join()

    import json

    entries = json.loads(reply)["result"]
    assert len(entries) == LIMIT
    assert entries == [wire.to_wire(entry) for entry in served.owner.events(handle)]
    assert worst_ms < STALL_MS, (
        f"answering events from 0 held the serve's other threads for {worst_ms:.0f} ms "
        f"(reply took {took:.2f} s)")
    assert max(status_waits) < 2 * STALL_MS / 1e3, (
        f"status waited {max(status_waits):.2f} s behind an events reply")


def test_a_follower_in_a_new_process_asks_for_what_is_new_not_the_history(long_job):
    """What `clockwork progress` does each time: a new client, `wait` for the events
    after its last number, then `summary`. Neither is the whole history."""
    served, handle = long_job
    last = served.owner.events(handle)[-1].seq
    served.owner._report(RunDone(run=_run()), handle)
    asked: list[tuple[str, int]] = []
    original = DaemonServer._answer

    def counting(self, payload: bytes) -> dict:
        reply = original(self, payload)
        result = reply.get("result")
        asked.append((str(payload)[:60], len(result) if isinstance(result, list) else -1))
        return reply

    DaemonServer._answer = counting  # type: ignore[method-assign]
    try:
        with RemoteOwner(served.server.command_endpoint, timeout=10) as client:
            for _ in range(3):  # through the subscriber's first heartbeat
                fresh = client.wait(handle, last, timeout=1.5)
                assert [type(entry.event) for entry in fresh] == [RunDone]
            client.summary(handle)
    finally:
        DaemonServer._answer = original  # type: ignore[method-assign]
    sizes = [size for _, size in asked if size >= 0]
    assert sizes and max(sizes) <= 1, asked


def test_a_long_history_is_read_a_page_at_a_time_and_comes_back_whole(long_job):
    served, handle = long_job
    pages: list[int] = []
    original = _COMMANDS["events"]

    def counting(self, handle=None, after=0, limit=0):
        result = original(self, handle, after, limit)
        pages.append(len(result))
        return result

    _COMMANDS["events"] = counting
    try:
        with RemoteOwner(served.server.command_endpoint, timeout=10) as client:
            read = client.events(handle, 0)
    finally:
        _COMMANDS["events"] = original
    assert len(pages) >= LIMIT // EVENTS_PAGE and max(pages) <= EVENTS_PAGE
    assert wire.to_wire(list(read)) == wire.to_wire(served.owner.events(handle))


def test_pages_that_meet_on_a_batch_keep_the_newer_one_as_the_daemon_does():
    owner = LocalOwner(fake=True)
    handle = owner.submit(Discover())
    batch = wire.example(BatchSeen)
    for _ in range(EVENTS_PAGE - 1):
        owner._report(RunDone(run=_run()), handle)
    owner._report(batch, handle)  # the last entry of the first page
    served = Served(owner)
    original = _COMMANDS["events"]
    calls = []

    def replaced_between_pages(self, handle=None, after=0, limit=0):
        calls.append(after)
        if len(calls) == 2:
            owner._report(batch, handle)  # replaces the first, under a new number
        return original(self, handle, after, limit)

    _COMMANDS["events"] = replaced_between_pages
    try:
        with RemoteOwner(served.server.command_endpoint, timeout=10) as client:
            read = client.events(handle, 0)
        kept = owner.events(handle)
    finally:
        _COMMANDS["events"] = original
        served.close()
    assert len(calls) == 2
    assert [type(entry.event) for entry in read[-2:]] == [RunDone, BatchSeen]
    assert read[-1].seq == kept[-1].seq and len(read) == len(kept) == EVENTS_PAGE


def test_a_daemon_from_before_limit_is_asked_for_the_whole_history_once(long_job):
    served, handle = long_job
    def without_limit(self, handle=None, after=0):
        return DaemonServer._events(self, handle, after)

    _COMMANDS["events"] = without_limit
    try:
        with RemoteOwner(served.server.command_endpoint, timeout=30) as client:
            read = client.events(handle, 0)
            again = client.events(handle, read[-10].seq)
    finally:
        _COMMANDS["events"] = DaemonServer._events
    assert len(read) == LIMIT and len(again) == 9
