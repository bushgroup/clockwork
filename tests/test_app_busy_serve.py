"""The window tells a `clockwork serve` that is busy from one that has gone (lab #27).

A long series' history held the daemon for seconds when a follower asked for it; the
window's `status` timed out, and it read a daemon still running as one that had stopped,
failed its jobs and offered Find boxes (lab record, task 170). A daemon whose command
port still takes connections, whose process still runs and, unless it is `--fake`, that
still holds the instrument lock is busy: the window says so once and goes on following
it.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from clockwork.app.worker import RemoteWorker  # noqa: E402
from clockwork.owner import InstrumentLock, LocalOwner  # noqa: E402
from clockwork.owner.remote import Hello  # noqa: E402
from test_follower_load import Served  # noqa: E402


def test_a_busy_serve_is_followed_through_and_find_boxes_is_not_offered(qtbot):
    owner = LocalOwner(fake=True)
    served = Served(owner)
    slow = threading.Event()
    answering = owner.status

    def status():
        if slow.is_set():
            time.sleep(1.5)
        return answering()

    owner.status = status  # type: ignore[method-assign]
    worker = RemoteWorker(endpoint=served.server.command_endpoint, poll_s=0.05,
                          connect_timeout=0.5)
    warned: list[str] = []
    said: list[str] = []
    worker.warned.connect(warned.append)
    worker.said.connect(said.append)
    worker.start()
    try:
        qtbot.waitUntil(lambda: worker.hello is not None, timeout=10_000)
        slow.set()
        qtbot.waitUntil(lambda: any("still running" in line for line in warned),
                        timeout=10_000)
        assert worker.hello is not None and worker.refused == ""
        slow.clear()
        qtbot.waitUntil(lambda: any("answers again" in line for line in said),
                        timeout=15_000)
        assert not any("stopped answering" in line for line in warned), warned
        assert sum("still running" in line for line in warned) == 1
    finally:
        slow.clear()
        worker.shutdown()
        assert worker.wait(10_000)
        served.close()


def _hello(*, pid: int, fake: bool) -> Hello:
    return Hello(program="clockwork serve", version="x", protocol=1, session="s",
                 pid=pid, started="", fake=fake, events="")


def test_a_serve_is_busy_only_while_it_listens_runs_and_holds_the_lock(qtbot):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        worker = RemoteWorker(endpoint=f"tcp://127.0.0.1:{port}", connect_timeout=0.1)
        worker.hello = _hello(pid=os.getpid(), fake=False)
        assert not worker._still_running()  # nothing holds the (test's own) lock
        with InstrumentLock("clockwork serve"):
            assert worker._still_running()
            worker.hello = _hello(pid=os.getpid() + 1_000_000, fake=False)
            assert not worker._still_running()  # no such process
        worker.hello = _hello(pid=os.getpid(), fake=True)
        assert worker._still_running()  # a rehearsal takes no lock
    # Its port closed: a daemon on a thread of this process has stopped, though the
    # process it ran in has not.
    assert not worker._still_running()
