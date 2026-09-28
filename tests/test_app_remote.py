"""The window as a client of `clockwork serve` (lab record, task 77).

`RemoteWorker` against a `--fake` daemon run on a thread of the test's own with ports
the OS picks, as `test_daemon.py` runs them, so that a suite run on an instrument PC
with a real daemon up never meets it. A daemon the window "starts" here is that same
thread, standing in for the process `start_serve` would have made; the command line
itself is checked as a list, never run.
"""

from __future__ import annotations

import os
import socket
import sys

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QSettings  # noqa: E402

from clockwork import instrument as instrument_module  # noqa: E402
from clockwork import method as method_module  # noqa: E402
from clockwork.app import runqueue, serving  # noqa: E402
from clockwork.app.settings import Settings  # noqa: E402
from clockwork.app.window import MainWindow  # noqa: E402
from clockwork.app.worker import SERVE, Discover, RemoteWorker  # noqa: E402
from clockwork.owner import Acquire, Send  # noqa: E402
from clockwork.owner.remote import RemoteOwner  # noqa: E402
from test_daemon import Daemon, make_instrument, make_method  # noqa: E402

CLAUDE = "Claude, through clockwork mcp"


@pytest.fixture
def scratch_settings(tmp_path, monkeypatch):
    backing = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr("clockwork.app.window.Settings", lambda: Settings(backing))
    return backing


@pytest.fixture
def daemon(tmp_path):
    made = Daemon(tmp_path)
    yield made
    made.stop()


@pytest.fixture
def claude(daemon):
    """Another client of the same daemon, naming itself as the MCP server does."""
    client = RemoteOwner(daemon.endpoint, timeout=10, origin=CLAUDE)
    yield client
    client.close()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Started:
    """Stands in for the `Popen` of a daemon the window started."""

    def __init__(self, code: int | None = None) -> None:
        self.code = code

    def poll(self) -> int | None:
        return self.code


def client_for(endpoint: str, **options: object):
    return lambda mailbox: RemoteWorker(mailbox=mailbox, endpoint=endpoint,
                                        poll_s=0.05, **options)


def ended(worker: RemoteWorker) -> None:
    worker.shutdown()
    assert worker.wait(10_000)


# -- starting the daemon ------------------------------------------------------------


def test_the_command_line_carries_the_window_settings(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    command = serving.serve_command(output="D:/data", library="", console="C:/c.exe",
                                    kept="D:/kept", errors_log="C:/errors.log")
    assert command == [sys.executable, "serve", "--detached", "--output", "D:/data",
                       "--console", "C:/c.exe", "--kept", "D:/kept", "--errors-log",
                       "C:/errors.log"]
    monkeypatch.delattr(sys, "frozen")
    checkout = serving.serve_command()
    assert checkout[:2] == [sys.executable, "-c"] and checkout[-2:] == ["serve", "--detached"]


def test_a_started_daemon_never_attaches_to_its_starters_console(monkeypatch):
    """A daemon `clockwork-cli.exe` started from a shortcut's `cmd` window attached to
    that console and printed `console: ready` after the verb's prompt (lab record, task
    96). A detached `serve` must not attach; one typed at a terminal still does."""
    import clockwork.app as app_module
    from clockwork.owner import daemon

    attached: list[bool] = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_module, "_attach_parent_console",
                        lambda: attached.append(True) or True)
    monkeypatch.setattr(daemon, "run", lambda **options: 0)
    assert app_module.main(["serve", "--fake", "--detached"]) == 0
    assert attached == []
    assert app_module.main(["serve", "--fake"]) == 0
    assert attached == [True]


def test_a_daemon_that_would_not_start_says_why_from_its_log(tmp_path):
    log = tmp_path / "serve.log"
    log.write_text("2026-09-26 14:02:11 clockwork serve 1.1.0 (pid 1)\n"
                   "2026-09-26 14:02:11 the instrument is already owned by the clockwork "
                   "window (pid 4812); close that one first\n", encoding="utf-8")
    said = serving.why_serve_stopped(1, str(log))
    assert said == ("clockwork serve did not start (exit code 1): the instrument is "
                    "already owned by the clockwork window (pid 4812); close that one first")
    assert str(tmp_path) in serving.why_serve_stopped(None, str(tmp_path / "none.log"))


def test_no_daemon_and_one_that_exits_names_the_holder(qtbot, tmp_path):
    log = tmp_path / "serve.log"
    log.write_text("2026-09-26 14:02:11 the instrument is already owned by a bench "
                   "script (pid 7)\n", encoding="utf-8")
    worker = RemoteWorker(endpoint=f"tcp://127.0.0.1:{free_port()}", connect_timeout=0.3,
                          launch=lambda: Started(code=1), log=str(log))
    worker.start()
    try:
        qtbot.waitUntil(lambda: "bench script" in worker.refused, timeout=10_000)
        assert worker.spawned is None and worker.hello is None
        failed = []
        worker.failed_job.connect(lambda job, message: failed.append(message))
        worker.submit(Discover())
        qtbot.waitUntil(lambda: bool(failed), timeout=10_000)
        assert "bench script" in failed[0]
    finally:
        ended(worker)


def test_the_window_starts_a_daemon_and_follows_its_own_start(qtbot, tmp_path):
    port = free_port()
    endpoint = f"tcp://127.0.0.1:{port}"
    daemons: list[Daemon] = []

    def launch() -> Started:
        daemons.append(Daemon(tmp_path, command=endpoint,
                              events=f"tcp://127.0.0.1:{free_port()}"))
        return Started()

    worker = RemoteWorker(endpoint=endpoint, connect_timeout=0.5, launch=launch,
                          poll_s=0.05)
    connected, started = [], []
    worker.connected.connect(lambda hello, spawned: connected.append(spawned))
    worker.started_job.connect(started.append)
    worker.start()
    try:
        qtbot.waitUntil(lambda: connected == [True], timeout=30_000)
        assert isinstance(worker.spawned, Started) and len(daemons) == 1
        # The daemon's own start-up console, submitted before this client said hello,
        # is followed all the same, and named as the daemon's.
        qtbot.waitUntil(lambda: bool(started), timeout=20_000)
        assert worker.origin(started[0]) == SERVE
        qtbot.waitUntil(lambda: worker.console_alive, timeout=20_000)
    finally:
        ended(worker)
        for made in daemons:
            made.stop()


# -- following every client's jobs -------------------------------------------------


def test_own_jobs_come_back_as_the_objects_submitted(qtbot, daemon):
    worker = RemoteWorker(endpoint=daemon.endpoint, poll_s=0.05)
    worker.start()
    try:
        qtbot.waitUntil(lambda: worker.hello is not None, timeout=10_000)
        job = Discover(method=make_method())
        with qtbot.waitSignal(worker.finished_job, timeout=20_000) as blocker:
            worker.submit(job)
        assert blocker.args[0] is job and worker.origin(job) == ""
        qtbot.waitUntil(lambda: worker.boxes == ("box1",), timeout=10_000)
    finally:
        ended(worker)


def test_another_clients_job_is_shown_with_its_origin(qtbot, daemon, claude):
    worker = RemoteWorker(endpoint=daemon.endpoint, poll_s=0.05)
    started, finished = [], []
    worker.started_job.connect(started.append)
    worker.finished_job.connect(lambda job, result: finished.append(job))
    worker.start()
    try:
        qtbot.waitUntil(lambda: worker.hello is not None, timeout=10_000)
        method = make_method()
        claude.submit(Discover(method=method))
        claude.submit(Send(method=method))
        qtbot.waitUntil(lambda: len(finished) >= 2, timeout=30_000)
        foreign = [job for job in started if isinstance(job, (Discover, Send))]
        assert foreign and all(worker.origin(job) == CLAUDE for job in foreign)
        assert finished[-1] is started[-1]  # one object per job, start to end
        assert not worker.busy_mine() and worker.has_snapshot
    finally:
        ended(worker)


def test_a_lost_daemon_fails_nothing_it_did_not_have_and_says_so(qtbot, tmp_path):
    made = Daemon(tmp_path)
    worker = RemoteWorker(endpoint=made.endpoint, poll_s=0.05, connect_timeout=0.5)
    worker.start()
    try:
        qtbot.waitUntil(lambda: worker.hello is not None, timeout=10_000)
        made.stop()
        qtbot.waitUntil(lambda: "stopped answering" in worker.refused, timeout=20_000)
        assert worker.status is None
    finally:
        ended(worker)
        made.stop()


# -- the window over the daemon ------------------------------------------------------


def remote_window(qtbot, endpoint: str, **options: object) -> MainWindow:
    made = MainWindow(client=client_for(endpoint, **options))
    qtbot.addWidget(made)
    made.show()
    qtbot.waitUntil(lambda: made.worker.hello is not None, timeout=20_000)
    return made


def idle_daemon(qtbot, client: RemoteOwner, issued: int = 1) -> None:
    def idle() -> bool:
        status = client.status()
        return status.running is None and not status.queued and status.issued >= issued
    qtbot.waitUntil(idle, timeout=20_000)


def test_a_window_that_found_a_daemon_scans_and_leaves_it_running(
        qtbot, scratch_settings, daemon, claude):
    idle_daemon(qtbot, claude)  # its start-up console has been started
    made = remote_window(qtbot, daemon.endpoint)
    # An idle daemon scanned long ago, so the window asks it again, as its own job.
    qtbot.waitUntil(lambda: made._job is None and made.worker.status is not None
                    and made.worker.status.issued == 2
                    and made.worker.status.running is None, timeout=30_000)
    assert made.worker.mine(made.worker.status.running) is False
    asked = []
    made.worker.shutdown_daemon = lambda reason="": asked.append(reason)
    made.close()
    assert not made.isVisible() and asked == []


def test_closing_the_window_that_started_an_idle_daemon_stops_it(
        qtbot, scratch_settings, daemon):
    made = remote_window(qtbot, daemon.endpoint)
    qtbot.waitUntil(lambda: made._job is None and made.worker.status is not None
                    and made.worker.status.running is None, timeout=30_000)
    made.worker.spawned = Started()
    asked = []
    made.worker.shutdown_daemon = lambda reason="": asked.append(reason)
    made.close()
    assert not made.isVisible() and len(asked) == 1


def test_claudes_acquisition_is_shown_and_survives_the_close(
        qtbot, scratch_settings, daemon, claude, tmp_path):
    idle_daemon(qtbot, claude)
    made = remote_window(qtbot, daemon.endpoint)
    idle_daemon(qtbot, claude, issued=2)  # the window's own scan has run
    method = make_method(frames=4, accumulations=4)
    claude.submit(Discover(method=method))
    claude.submit(Send(method=method))
    handle = claude.submit(Acquire(method=method, instrument=make_instrument(),
                                   initials="ZZ", replicates=3))
    qtbot.waitUntil(lambda: made.worker.status is not None
                    and made.worker.status.running == handle, timeout=60_000)
    qtbot.waitUntil(lambda: CLAUDE in made.queue_panel.others.text(), timeout=10_000)
    lines = [made.run_panel.log.topLevelItem(index).text(1)
             for index in range(made.run_panel.log.topLevelItemCount())]
    assert any(CLAUDE in line for line in lines), lines
    told, asked = [], []
    made._tell = lambda title, detail: told.append(detail)
    made._ask_close = lambda: asked.append("asked") or "cancel"
    made.worker.spawned = Started()  # even a daemon this window started is left
    stopped = []
    made.worker.shutdown_daemon = lambda reason="": stopped.append(reason)
    made.close()
    assert not made.isVisible() and asked == [] and stopped == []
    assert told and CLAUDE in told[0]
    assert claude.status().running == handle  # carried on
    claude.stop("the test is over")


def test_a_window_joining_a_busy_daemon_can_send_setup_once_it_is_idle(
        qtbot, scratch_settings, daemon, claude, tmp_path):
    """A join that finds another client's job running names the panes without a scan,
    and used to leave the ports to a Find boxes the trainee had to know to press before
    Send setup (on the rack, 2026-09-28; lab record, task 96). The scan it put off is
    made as soon as the daemon is idle."""
    idle_daemon(qtbot, claude)
    method = make_method(frames=4, accumulations=4)
    path = str(tmp_path / "joined.toml")
    method_module.save(method, path)
    scratch_settings.setValue("method_path", path)
    claude.submit(Discover(method=method))
    claude.submit(Send(method=method))
    # Long enough that the join certainly meets it running, however loaded the PC.
    handle = claude.submit(Acquire(method=method, instrument=make_instrument(),
                                   initials="ZZ", replicates=20))
    qtbot.waitUntil(lambda: claude.status().running == handle, timeout=60_000)
    made = remote_window(qtbot, daemon.endpoint)
    # A failure below must not leave teardown closing over a run behind a real dialog.
    made._ask_close = lambda: "cancel"
    made._tell = lambda title, detail: None
    made.worker.shutdown_daemon = lambda reason="": None
    try:
        # The status can arrive before the `connected` that sets the flag: wait for both.
        qtbot.waitUntil(lambda: made._scan_when_idle, timeout=20_000)
        assert made.worker.status.running == handle
        assert not made.setup_button.isEnabled()
    finally:
        claude.stop("the test is over")
    qtbot.waitUntil(lambda: made._job is None and made.worker.status is not None
                    and made.worker.status.running is None
                    and made.worker.status.issued > handle.id, timeout=120_000)
    qtbot.waitUntil(lambda: made.setup_button.isEnabled(), timeout=20_000)
    assert not made._scan_when_idle
    assert made.ports == {entry.name: entry.port for entry in method.boxes}


def test_closing_over_the_windows_own_run_asks(qtbot, scratch_settings, daemon, tmp_path):
    made = remote_window(qtbot, daemon.endpoint)
    method = make_method(frames=4, accumulations=4)
    made.worker.submit(Discover(method=method))
    made.worker.submit(Send(method=method, directory=str(tmp_path)))
    job = Acquire(method=method, instrument=make_instrument(), initials="ZZ",
                  replicates=3, directory=str(tmp_path))
    made.worker.submit(job)
    qtbot.waitUntil(lambda: made._job is job, timeout=60_000)
    made.worker.spawned = Started()
    stopped = []
    made.worker.shutdown_daemon = lambda reason="": stopped.append(reason)

    made._ask_close = lambda: "cancel"
    made.close()
    assert made.isVisible() and made._job is job

    made._ask_close = lambda: "stop"
    made.close()
    assert made.isVisible() and made.worker.stopping  # open until the frame ends
    qtbot.waitUntil(lambda: not made.isVisible(), timeout=120_000)
    assert len(stopped) == 1


def test_a_queue_row_run_through_the_daemon_is_done(
        qtbot, scratch_settings, daemon, claude, tmp_path):
    """An Acquire's runs come back over the wire as a tuple, not the list they are in
    process, and the queue took only a list: every row a window ran through a daemon was
    marked failed after a complete run (on the rack, 2026-09-28; lab record, task 86)."""
    idle_daemon(qtbot, claude)
    made = remote_window(qtbot, daemon.endpoint)
    made.output_dir.setText(str(tmp_path))
    made.initials.setText("ZZ")
    made._initials_changed()
    instrument = str(tmp_path / "instrument.toml")
    instrument_module.save(make_instrument(), instrument)
    made._load_instrument(instrument)
    path = str(tmp_path / "queued.toml")
    method_module.save(make_method(), path)
    # A `--fake` daemon builds its rack from the method a Discover carries.
    qtbot.waitUntil(lambda: made._job is None and made.worker.status is not None
                    and made.worker.status.issued == 2
                    and made.worker.status.running is None, timeout=30_000)
    made.worker.submit(Discover(method=make_method()))
    qtbot.waitUntil(lambda: made._job is None and bool(made.worker.boxes)
                    and made.worker.console_alive, timeout=60_000)
    made.queue.add(runqueue.QueueRow(method_path=path))
    made.queue_panel.refresh()

    made.start_queue()
    qtbot.waitUntil(lambda: not made.queue.running and made._job is None, timeout=120_000)
    (row,) = made.queue.rows
    assert row.state == runqueue.DONE, row.outcome
    assert len(row.stems) == 1 and (tmp_path / f"{row.stems[0]}.uimf").is_file()


@pytest.mark.skipif(os.name != "nt", reason="the job object is Windows's")
def test_contain_children_ends_the_console_with_the_daemon(tmp_path):
    """A process in `contain_children`'s job takes what it started down with it, however
    it ends: here, killed outright, as closing a daemon's console window kills it."""
    import subprocess
    import time

    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "from clockwork.owner.daemon import contain_children\n"
        "assert contain_children()\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(120)\n", encoding="utf-8")
    parent = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                              text=True)
    try:
        child = int(parent.stdout.readline())
        assert _alive(child)
        parent.kill()
        parent.wait(10)
        deadline = time.monotonic() + 10
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(child), "the child outlived the process that contained it"
    finally:
        parent.kill()


def _alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32")
    handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)  # query, synchronize
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)
