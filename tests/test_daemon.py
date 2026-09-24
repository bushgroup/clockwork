"""`clockwork serve` and its client: the protocol, a whole series over the stand-ins, stop
and shutdown mid-run, the lock, a restart, and the console left behind.

Every daemon here but one is `run` on a thread of the test's own with ports the OS picks,
so a suite run on an instrument PC with a real daemon up never meets it. The one that is
a real process (`clockwork serve --fake` in a subprocess) is skipped when the default
port is taken. No daemon here that is not `--fake` scans a port, clears the console's
port or starts a console: on the instrument PC those would be the trainee's boxes and
the trainee's console (lab record, task 68).
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest
import zmq

from clockwork import instrument as instrument_module
from clockwork import method as method_module
from clockwork.acq import FakeConsoleProcess, FrameEnded
from clockwork.acq.process import Listener, listening_on, listening_pid
from clockwork.mips import Discovery
from clockwork.owner import (
    Acquire,
    Discover,
    Handle,
    InstrumentLock,
    JobFailed,
    JobFinished,
    JobStarted,
    LocalOwner,
    Progress,
    RunDone,
    Send,
    StaleHandle,
    daemon,
    wire,
)
from clockwork.owner.lock import LOCK_ENV, read_holder
from clockwork.owner.remote import (
    PROTOCOL,
    DaemonError,
    DaemonServer,
    DaemonUnavailable,
    RemoteOwner,
)

BOX = "box1"
SCANS = 32


def make_method(*, frames: int = 1, accumulations: int = 2) -> method_module.Method:
    table = (f"STBLDAT;0:[A:1,0:A:1:B:1,500:B:0,"
             f"{method_module.enable_fall_tick(SCANS)}:A:0,"
             f"{method_module.table_period(SCANS)}:];")
    return method_module.from_dict({
        "schema_version": 2,
        "metadata": {"name": "daemon test", "created": dt.date(2026, 9, 23)},
        "acquisition": {
            "frames": frames, "scans": SCANS, "accumulations": accumulations,
            "file_stem": "260923_ZZ_001", "repetition_mode": "per_repetition",
            "keep_raw": True, "enable": {"box": BOX, "channel": "A"},
        },
        "boxes": [{"name": BOX, "port": "COM3", "setup": ["STBLCLK,EXT", "STBLTRG,POS"],
                   "load": [table], "arm": ["SMOD,TBL"]}],
        "start": [[BOX, "TBLSTRT"]],
        "reset": [[BOX, "SMOD,LOC"], [BOX, "SMOD,TBL"]],
    })


def make_instrument() -> instrument_module.Instrument:
    return instrument_module.from_dict({
        "schema_version": 1, "instrument": {"name": "daemon test"},
        "vertical": {"full_scale_v": 0.5, "offset_v": 0.251, "inverted": False},
    })


class Daemon:
    """`daemon.run` on a thread, with its server once it is bound and its exit code."""

    def __init__(self, tmp_path, *, command: str = "tcp://127.0.0.1:*",
                 events: str = "tcp://127.0.0.1:*", **options: object) -> None:
        self.log = io.StringIO()
        self.server: DaemonServer | None = None
        self.code: int | None = None
        bound = threading.Event()

        def ready(server: DaemonServer) -> None:
            self.server = server
            bound.set()

        def body() -> None:
            self.code = daemon.run(
                command=command, events=events, output=str(tmp_path),
                log_file=str(tmp_path / "serve.log"), stream=self.log, on_ready=ready,
                handle_signals=False, **{"fake": True, **options})
            bound.set()

        self.thread = threading.Thread(target=body, name="test daemon")
        self.thread.start()
        assert bound.wait(20), "the daemon never bound its sockets"

    @property
    def endpoint(self) -> str:
        assert self.server is not None, self.log.getvalue()
        return self.server.command_endpoint

    def stop(self, client: RemoteOwner | None = None) -> int | None:
        if self.thread.is_alive():
            if client is not None:
                client.shutdown("the test is over")
            elif self.server is not None:
                self.server.request_shutdown("the test is over")
            self.thread.join(60)
        assert not self.thread.is_alive(), "the daemon did not stop"
        return self.code


@pytest.fixture
def served(tmp_path):
    made = Daemon(tmp_path)
    client = RemoteOwner(made.endpoint, timeout=10)
    yield made, client
    made.stop()
    client.close()


def ended(owner, handle: Handle, timeout: float = 60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for entry in owner.events(handle):
            if isinstance(entry.event, (JobFinished, JobFailed)):
                return entry.event
        time.sleep(0.02)
    return None


def wait_for(condition, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def same(these, those) -> bool:
    """Two lists of progress compared by their wire forms: a batch's arrays have no `==`
    that answers with one boolean."""
    return wire.to_wire(list(these)) == wire.to_wire(list(those))


def raw(endpoint: str, payload: bytes) -> dict:
    """One request on a bare DEALER, the way a client in another language would send it."""
    socket_ = zmq.Context.instance().socket(zmq.DEALER)
    socket_.setsockopt(zmq.LINGER, 0)
    socket_.connect(endpoint)
    try:
        socket_.send_multipart([b"", payload])
        assert socket_.poll(5000), "no reply"
        return json.loads(socket_.recv_multipart()[-1])
    finally:
        socket_.close()


# --- the protocol -----------------------------------------------------------------


def test_hello_names_the_daemon_and_the_protocol_refuses_what_it_does_not_know(served):
    made, client = served
    hello = client.hello()
    assert hello.protocol == PROTOCOL and hello.fake and hello.program == "clockwork serve"
    assert hello.pid == os.getpid() and hello.events == made.server.events_endpoint
    assert hello.holder is None  # --fake takes no lock

    answer = raw(made.endpoint, b'{"id": 1, "cmd": "hello"}')
    assert answer["ok"] and answer["id"] == 1 and answer["session"] == hello.session
    for payload, kind in ((b"not json", "bad-request"),
                          (b'{"id": 2}', "bad-request"),
                          (b'{"id": 3, "cmd": "launch"}', "unknown-command"),
                          (b'{"id": 4, "cmd": "submit", "args": {"job": 7}}', "refused"),
                          (b'{"id": 5, "cmd": "status", "args": {"x": 1}}', "bad-request"),
                          (b'{"id": 6, "cmd": "submit", "args": {"job": '
                           b'{"@type": "NoSuchJob"}}}', "bad-request")):
        answer = raw(made.endpoint, payload)
        assert not answer["ok"] and answer["error"]["kind"] == kind, (payload, answer)
        assert answer["error"]["message"]


def test_a_client_with_no_daemon_is_told_so_in_one_sentence():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with RemoteOwner(f"tcp://127.0.0.1:{port}", timeout=0.3) as client:
        with pytest.raises(DaemonUnavailable, match="no clockwork serve answered"):
            client.status()
        with pytest.raises(DaemonUnavailable):  # and the client is still usable
            client.status()


# --- a whole series ---------------------------------------------------------------


def test_a_remote_owner_discovers_sends_and_acquires_two_replicates(served, tmp_path):
    made, client = served
    method = make_method()
    found = ended(client, client.submit(Discover(method=method)))
    assert isinstance(found, JobFinished) and client.status().boxes == (BOX,)
    assert wait_for(lambda: client.status().console.state == "ready")

    sent = ended(client, client.submit(Send(method=method, stem="260923_ZZ_001")))
    assert isinstance(sent, JobFinished), sent
    assert sent.result.send_log.startswith(str(tmp_path))  # --output filled the directory
    assert client.snapshot() is not None and client.status().snapshot

    handle = client.submit(Acquire(method=method, instrument=make_instrument(),
                                   stem="260923_ZZ_001", initials="ZZ", replicates=2))
    finished = ended(client, handle, timeout=180)
    assert isinstance(finished, JobFinished), finished
    runs = finished.result
    assert len(runs) == 2 and all(run.complete for run in runs)
    assert [run.replicate for run in runs] == [False, True]
    for run in runs:
        assert os.path.isfile(run.summed_path) and os.path.isfile(run.raw_path)
        stem = os.path.splitext(os.path.basename(run.raw_path))[0]
        assert os.path.isfile(tmp_path / f"{stem}.sent.txt")
        assert any(name.startswith(stem) and name.endswith(".transcript.log")
                   for name in os.listdir(tmp_path))

    progress = client.events(handle)
    kinds = [type(entry.event) for entry in progress]
    assert kinds[0] is JobStarted and kinds[-1] is JobFinished
    assert kinds.count(RunDone) == 2
    seqs = [entry.seq for entry in progress]
    assert seqs == sorted(set(seqs))
    # What the stream gave the client is what the daemon kept.
    assert same(progress, made.server.owner.events(handle))
    assert same(client.events(handle, after=seqs[-2]), progress[-1:])

    log = (tmp_path / "serve.log").read_text(encoding="utf-8")
    assert f"submitted job {handle.id}: acquiring (Acquire)" in log
    assert f"job {handle.id}: acquiring: done" in log


def test_the_stream_keeps_the_client_s_copy_and_a_gap_sends_it_back_to_the_record(
        served):
    made, client = served
    handle = client.submit(Discover(method=make_method()))
    assert isinstance(ended(client, handle), JobFinished)
    assert wait_for(lambda: client.streaming)
    client.events(handle)  # fills the copy
    assert client._copies[handle.id].trusted  # noqa: SLF001

    # A lost message: the run breaks and no copy is trusted until it is filled again.
    with client._state:  # noqa: SLF001
        last = client._last_seq  # noqa: SLF001
    client._heard(b"handle/99/", json.dumps(  # noqa: SLF001
        {"@type": "Progress", "seq": last + 5,
         "event": {"@type": "Said", "line": "after a gap"}}).encode())
    assert not client._copies[handle.id].trusted  # noqa: SLF001
    assert same(client.events(handle), made.server.owner.events(handle))


def test_a_late_client_catches_up_on_a_job_that_finished_before_it_connected(served):
    made, first = served
    handle = first.submit(Discover(method=make_method()))
    assert isinstance(ended(first, handle), JobFinished)
    with RemoteOwner(made.endpoint, timeout=10) as late:
        assert same(late.events(handle), made.server.owner.events(handle))
        assert wait_for(lambda: late.streaming)
        assert same(late.events(handle), made.server.owner.events(handle))


def test_stop_mid_run_ends_after_the_repetition_folds_and_closes(served):
    made, client = served
    method = make_method(frames=4, accumulations=3)
    assert isinstance(ended(client, client.submit(Discover(method=method))), JobFinished)
    assert isinstance(ended(client, client.submit(Send(method=method))), JobFinished)
    handle = client.submit(Acquire(method=method, instrument=make_instrument(),
                                   initials="ZZ", replicates=2))
    assert wait_for(lambda: any(isinstance(entry.event, FrameEnded)
                                for entry in client.events(handle)), 60)
    client.stop("the test pressed Stop")
    finished = ended(client, handle, timeout=120)
    assert isinstance(finished, JobFinished), finished
    (run,) = finished.result  # the second replicate never began
    assert run.stopped_early and os.path.isfile(run.summed_path)
    assert len(run.frames) < 4 * 3 and run.folds and not run.failures


def test_shutdown_mid_run_folds_closes_fails_the_queue_and_exits(tmp_path):
    made = Daemon(tmp_path)
    client = RemoteOwner(made.endpoint, timeout=10)
    try:
        method = make_method(frames=4, accumulations=3)
        assert isinstance(ended(client, client.submit(Discover(method=method))),
                          JobFinished)
        assert isinstance(ended(client, client.submit(Send(method=method))), JobFinished)
        acquiring = client.submit(Acquire(method=method, instrument=make_instrument(),
                                          initials="ZZ"))
        queued = client.submit(Discover(method=method))
        assert wait_for(lambda: any(isinstance(entry.event, FrameEnded)
                                    for entry in client.events(acquiring)), 60)
        client.shutdown("the test is over")
        with pytest.raises(DaemonError, match="shutting down"):
            client.submit(Discover(method=method))
        made.thread.join(120)
        assert made.code == 0
        owner = made.server.owner
        finished = ended(owner, acquiring, 1)
        assert isinstance(finished, JobFinished)
        (run,) = finished.result
        assert run.stopped_early and os.path.isfile(run.summed_path)
        refused = ended(owner, queued, 1)
        assert isinstance(refused, JobFailed) and "never started" in refused.message
        assert owner.console is None and owner.boxes == {}
    finally:
        made.stop()
        client.close()
    assert "shutdown asked for: the test is over" in made.log.getvalue()
    assert "clockwork serve stopped" in made.log.getvalue()


# --- the lock -----------------------------------------------------------------------


def no_scan(**_: object) -> Discovery:
    return Discovery()


def test_a_real_daemon_holds_the_lock_and_a_second_serve_is_refused_with_its_sentence(
        tmp_path, capsys, monkeypatch):
    lock_path = os.environ[LOCK_ENV]
    first = Daemon(tmp_path, fake=False, discover=no_scan, clear_port=False,
                   start_console=False)
    client = RemoteOwner(first.endpoint, timeout=10)
    try:
        holder = read_holder(lock_path)
        assert holder is not None and holder.program == "clockwork serve"
        assert client.hello().holder == holder.text
        assert client.status().holder == holder

        # The command line's own path: `clockwork serve`, refused before any port.
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
        from clockwork.app import main

        assert main(["serve"]) == 1
        out = capsys.readouterr().out
        assert (f"the instrument is already owned by clockwork serve (pid {os.getpid()}"
                in out)
        # And an owner of any kind, the window's included.
        window = LocalOwner(program="the clockwork window", discover=no_scan)
        assert "clockwork serve" in window.refused
    finally:
        first.stop(client)
        client.close()
    assert read_holder(lock_path) is None


def test_a_daemon_started_while_another_owner_holds_the_lock_exits_saying_who(tmp_path):
    holder = InstrumentLock("the clockwork window")
    holder.acquire()
    try:
        log = io.StringIO()
        code = daemon.run(fake=False, command="tcp://127.0.0.1:*",
                          events="tcp://127.0.0.1:*", log_file=str(tmp_path / "s.log"),
                          stream=log, handle_signals=False, discover=no_scan,
                          clear_port=False, start_console=False)
    finally:
        holder.release()
    assert code == 1
    assert "the instrument is already owned by the clockwork window" in log.getvalue()


# --- a restart ---------------------------------------------------------------------


def test_a_client_carries_on_across_a_daemon_restart_and_refuses_the_old_handles(
        tmp_path):
    first = Daemon(tmp_path)
    endpoint, events = first.endpoint, first.server.events_endpoint
    client = RemoteOwner(endpoint, timeout=10)
    try:
        old = client.submit(Discover(method=make_method()))
        assert isinstance(ended(client, old), JobFinished)
        session = client.hello().session
        first.stop()

        with pytest.raises(DaemonUnavailable):
            RemoteOwner(endpoint, timeout=0.3).status()
        second = Daemon(tmp_path, command=endpoint, events=events)
        try:
            assert client.status().fake
            assert client.hello().session != session
            with pytest.raises(StaleHandle, match="different clockwork owner"):
                client.events(old)
            new = client.submit(Discover(method=make_method()))
            assert isinstance(ended(client, new), JobFinished)
            assert new.owner == client.hello().session
        finally:
            second.stop()
    finally:
        client.close()


# --- the console ---------------------------------------------------------------------


def test_the_watch_restarts_a_console_that_stopped_on_its_own_and_only_then(tmp_path):
    import logging

    owner = LocalOwner(fake=True).start()
    watch = daemon.ConsoleWatch(owner, logging.getLogger("clockwork.serve.test"))
    try:
        assert not watch.check()  # no console at all
        from clockwork.owner import StartConsole

        assert isinstance(ended(owner, owner.submit(StartConsole())), JobFinished)
        assert not watch.check()  # alive
        assert isinstance(owner.console, FakeConsoleProcess)
        owner.console.stop()
        assert watch.check()
        assert wait_for(lambda: owner.console.alive
                        and owner.console_status.state == "ready")
        owner.console.stop()
        owner.console_status = owner.console_status.__class__(state="starting")
        assert not watch.check()  # a restart that did not come up is not retried
    finally:
        owner.shutdown()
        owner.join(20)


def test_a_listening_port_is_named_by_the_process_that_holds_it():
    assert listening_pid(NETSTAT, 5555) == 7420
    assert listening_pid(NETSTAT, 5554) == 7420
    assert listening_pid(NETSTAT, 5570) == 0  # only an established connection
    assert Listener(port=5555, pid=7420, image="AqMD3_console.exe").is_console
    assert not Listener(port=5555, pid=1, image="python.exe").is_console

    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        found = listening_on(held.getsockname()[1])
        assert found is not None and not found.is_console
        if os.name == "nt":
            assert found.pid == os.getpid() and found.image.lower().startswith("python")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert listening_on(port) is None


NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1140
  TCP    0.0.0.0:5554           0.0.0.0:0              LISTENING       7420
  TCP    0.0.0.0:5555           0.0.0.0:0              LISTENING       7420
  TCP    127.0.0.1:5570         127.0.0.1:50122        ESTABLISHED     9012
  TCP    [::]:135               [::]:0                 LISTENING       1140
"""


# --- the owner beneath -------------------------------------------------------------


def test_a_job_submitted_after_shutdown_fails_at_once_and_a_foreign_handle_is_refused():
    owner = LocalOwner(fake=True)
    owner.shutdown()
    owner.serve()
    handle = owner.submit(Discover())
    (entry,) = owner.events(handle)
    assert isinstance(entry.event, JobFailed) and "never started" in entry.event.message
    assert handle.owner == owner.session
    with pytest.raises(StaleHandle):
        owner.events(Handle(id=handle.id, kind="Discover", label="x", owner="elsewhere"))
    assert owner.events(Handle(id=handle.id, kind="", label="")) == [entry]


def test_listeners_see_every_entry_in_numbered_order():
    owner = LocalOwner(fake=True)
    seen: list[Progress] = []
    owner.listen(lambda _handle, progress: seen.append(progress))
    owner.start()
    try:
        handle = owner.submit(Discover(method=make_method()))
        assert isinstance(ended(owner, handle), JobFinished)
        assert [entry.seq for entry in seen] == list(range(1, len(seen) + 1))
        assert same(seen, owner.events(handle))
    finally:
        owner.shutdown()
        owner.join(20)


# --- a real process ------------------------------------------------------------------


def test_clockwork_serve_fake_is_a_process_a_client_can_drive_and_shut_down(tmp_path):
    from clockwork.owner.remote import COMMAND_PORT, DEFAULT_COMMAND

    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", COMMAND_PORT)) == 0:
            pytest.skip(f"port {COMMAND_PORT} is in use; a daemon may be running here")
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path / "appdata"))
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; from clockwork.app import main; "
         "sys.exit(main(['serve', '--fake', '--output', sys.argv[1]]))", str(tmp_path)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        with RemoteOwner(DEFAULT_COMMAND, timeout=1.0) as client:
            assert wait_for(lambda: _answers(client), 60), "the daemon never answered"
            hello = client.hello()
            assert hello.fake and hello.pid != os.getpid()
            assert hello.output == str(tmp_path)
            handle = client.submit(Discover(method=make_method()))
            assert isinstance(ended(client, handle), JobFinished)
            client.shutdown("the test is over")
        assert process.wait(60) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)
    out = process.stdout.read()
    assert "clockwork serve stopped" in out, out
    assert (tmp_path / "appdata" / "clockwork" / "serve.log").is_file()


def _answers(client: RemoteOwner) -> bool:
    try:
        client.status()
        return True
    except DaemonUnavailable:
        return False
