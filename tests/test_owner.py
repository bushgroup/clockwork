"""The owner of the hardware: its wire forms, the instrument lock, and `LocalOwner`.

Three kinds of test. Every class that crosses the owner interface is round-tripped
through JSON, one test per class and the class list found by walking the tree, so a new
event is tested the day it is written. The lock is taken and refused in this process and
across two, including the case the lock was designed around: a holder that dies without
letting go. And `LocalOwner` is driven through its own protocol, under `--fake` and under
a lock another owner holds -- the last of those once through the window, which is the
whole of what the 1.0 window learns about the lock (lab record, task 67).

Nothing here touches a port: `--fake` owners open none, and a real owner is only ever
built with a stand-in scan or under a lock that refuses it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

import numpy as np
import pytest

from clockwork import method as method_module
from clockwork.acq import BatchSeen, Event, FrameEnded, FrameRecord, Run, Snapshot
from clockwork.acq.wire import Batch
from clockwork.method import Method
from clockwork.mips import Box, BoxState, Discovery, FakeBox, Found
from clockwork.owner import (
    Acquire,
    Discover,
    Discovered,
    Handle,
    InstrumentLock,
    JobFailed,
    JobFinished,
    JobStarted,
    LocalOwner,
    LockHeld,
    Said,
    Send,
    StartConsole,
    wire,
)
from clockwork.owner.lock import LOCK_ENV, read_holder

WIRE_TYPES = sorted(wire.wire_types())


def ended(owner: LocalOwner, handle: Handle, timeout: float = 10.0) -> Event | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for entry in owner.events(handle):
            if isinstance(entry.event, (JobFinished, JobFailed)):
                return entry.event
        time.sleep(0.01)
    return None


# --- the wire forms ---------------------------------------------------------------


@pytest.mark.parametrize("name", WIRE_TYPES)
def test_every_wire_type_round_trips_through_json(name):
    cls = wire.wire_types()[name]
    original = wire.example(cls)
    data = wire.to_wire(original)
    back = wire.loads(json.dumps(data))
    assert type(back) is cls
    assert wire.to_wire(back) == data
    if name not in ("Batch", "BatchSeen"):  # numpy arrays have no `==` to a bool
        assert back == original


def test_the_event_list_is_the_whole_tree():
    names = {name for name, cls in wire.wire_types().items() if issubclass(cls, Event)}
    assert {"BatchSeen", "FrameEnded", "Folding", "PhaseSent", "StateRead",
            "JobStarted", "JobFinished", "JobFailed", "Said", "Discovered",
            "ConsoleChanged", "RunDone", "BoxStateRead"} <= names


def test_a_method_crosses_as_its_text_and_keeps_its_warnings():
    method = dataclasses.replace(wire.example(Method), warnings=("repaired a string",))
    data = wire.to_wire(Send(method=method, method_path="C:/methods/clock.toml"))
    assert data["method"]["text"] == method_module.dumps(method)
    back = wire.from_wire(data)
    assert back.method == method and back.method.warnings == ("repaired a string",)
    assert back.method_path == "C:/methods/clock.toml"


def test_a_batch_crosses_without_its_spectrum_and_keeps_its_scan_count():
    batch = Batch(mz=np.ones(258_000), tic=np.array([7, 8, 9]),
                  time_stamps=np.array([0, 258, 516]), received_at=4.0)
    data = wire.to_wire(BatchSeen(method_frame=1, repetition=2, batch=batch,
                                  scans_so_far=30))
    assert "mz" not in data["batch"]
    back = wire.from_wire(data)
    assert back.batch.scans == 3 and back.batch.mz.size == 0
    assert back.batch.tic.dtype == batch.tic.dtype
    assert back.scans_so_far == 30


def test_an_open_box_never_crosses():
    box = Box(transport=FakeBox(), name="box1")
    try:
        found = Discovery(found=(Found(name="box1", port="COM3", version="v1", box=box),),
                          _boxes={"box1": box})
        back = wire.from_wire(wire.to_wire(found))
        assert back.found == (Found(name="box1", port="COM3", version="v1"),)
        assert back.boxes == {}
    finally:
        box.close()


def test_the_results_of_a_series_and_a_reading_come_back_as_their_shapes():
    run = wire.example(Run)
    assert wire.from_wire(wire.to_wire([run, run])) == (run, run)
    reading = {"AUKLET": BoxState(name="AUKLET", values={"GVER": "1.2"})}
    assert wire.from_wire(wire.to_wire(reading)) == reading


def test_something_with_no_wire_form_is_refused_by_name():
    with pytest.raises(TypeError, match=r"^JobFinished.result: Box has no wire form"):
        wire.to_wire(JobFinished(handle=Handle(1, "Discover", "x"), job=Discover(),
                                 result=Box(transport=FakeBox(), name="b")))


# --- the lock ---------------------------------------------------------------------


def test_a_second_lock_is_refused_with_the_first_holders_name(tmp_path):
    path = str(tmp_path / "instrument.lock")
    first = InstrumentLock("the clockwork window", path)
    first.acquire()
    try:
        with pytest.raises(LockHeld) as refused:
            InstrumentLock("clockwork serve", path).acquire()
        assert refused.value.holder == first.holder
        assert "the clockwork window" in str(refused.value)
        assert f"pid {os.getpid()}" in str(refused.value)
    finally:
        first.release()
    assert read_holder(path) is None
    with InstrumentLock("clockwork serve", path) as second:
        assert second.held and read_holder(path) == second.holder


def test_the_default_path_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(LOCK_ENV, str(tmp_path / "elsewhere.lock"))
    assert InstrumentLock("x").path == str(tmp_path / "elsewhere.lock")
    monkeypatch.delenv(LOCK_ENV)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert InstrumentLock("x").path == os.path.join(str(tmp_path), "clockwork",
                                                    "instrument.lock")


HOLD = """
import os, sys, time
from clockwork.owner.lock import InstrumentLock
InstrumentLock("a process that dies", sys.argv[1]).acquire()
print(os.getpid(), flush=True)
time.sleep(60)
"""


def test_a_holder_that_dies_frees_the_lock_without_anyone_cleaning_up(tmp_path):
    """The case a lock that meant "the file exists" gets wrong: a window killed from
    Task Manager. The record it wrote stays in the file; the lock goes with it.

    The child says its own pid, because under a virtual environment's launcher the
    process `Popen` started is not the interpreter that took the lock."""
    path = str(tmp_path / "instrument.lock")
    child = subprocess.Popen([sys.executable, "-c", HOLD, path],
                             stdout=subprocess.PIPE, text=True)
    try:
        pid = int(child.stdout.readline())
        with pytest.raises(LockHeld, match=f"a process that dies \\(pid {pid},"):
            InstrumentLock("clockwork serve", path).acquire()
        os.kill(pid, signal.SIGTERM)
        child.wait(10)
    finally:
        child.kill()
        child.wait(10)
    assert read_holder(path) is not None  # its record is still written down
    with InstrumentLock("clockwork serve", path) as lock:
        assert lock.held


# --- the owner --------------------------------------------------------------------


def test_a_fake_owner_takes_no_lock_and_reports_a_job_as_numbered_progress(tmp_path):
    path = str(tmp_path / "instrument.lock")
    seen: list[tuple[Handle, Event]] = []
    owner = LocalOwner(fake=True, lock_path=path,
                       on_event=lambda handle, event: seen.append((handle, event))).start()
    try:
        handle = owner.submit(Discover(method=wire.example(Method)))
        assert isinstance(ended(owner, handle), JobFinished)
        kinds = [type(entry.event) for entry in owner.events(handle)]
        assert kinds == [JobStarted, Said, Discovered, JobFinished]
        assert [event for _, event in seen] == [e.event for e in owner.events(handle)]
        assert {h for h, _ in seen} == {handle}
        seqs = [entry.seq for entry in owner.events(handle)]
        assert owner.events(handle, after=seqs[1]) == owner.events(handle)[2:]
        status = owner.status()
        assert status.fake and status.boxes == ("box1",) and status.running is None
        assert status.holder is None and not status.refused
        assert read_holder(path) is None
    finally:
        owner.shutdown()
        assert owner.join(10)


def test_consecutive_batches_collapse_in_the_history_and_leave_their_spectrum(tmp_path):
    owner = LocalOwner(fake=True)
    handle = owner.submit(Discover())
    owner._current = handle  # noqa: SLF001 -- reporting as the job would, without a loop
    batch = Batch(mz=np.ones(1000), tic=np.array([1, 1]), time_stamps=np.array([0, 1]))
    for scans in (2, 4, 6):
        owner._report(BatchSeen(method_frame=1, repetition=1, batch=batch,  # noqa: SLF001
                                scans_so_far=scans))
    record = FrameRecord(method_frame=1, repetition=1, frame_number=1, outcome="acquired")
    owner._report(FrameEnded(record=record))  # noqa: SLF001
    owner._report(BatchSeen(method_frame=1, repetition=2, batch=batch))  # noqa: SLF001
    kept = owner.events(handle)
    assert [type(entry.event) for entry in kept] == [BatchSeen, FrameEnded, BatchSeen]
    assert kept[0].event.scans_so_far == 6
    assert kept[0].event.batch.mz.size == 0 and kept[0].event.batch.scans == 2
    assert [entry.seq for entry in kept] == sorted(entry.seq for entry in kept)
    owner.shutdown()
    owner.serve()


def test_an_owner_under_a_held_lock_refuses_every_job_before_touching_a_port(tmp_path):
    path = str(tmp_path / "instrument.lock")
    scans: list[dict] = []

    def scan(**kwargs):
        scans.append(kwargs)
        return Discovery()

    holder = InstrumentLock("the clockwork window", path)
    holder.acquire()
    owner = LocalOwner(program="clockwork serve", lock_path=path, discover=scan).start()
    try:
        assert "the clockwork window" in owner.refused
        assert owner.status().holder == holder.holder
        for job in (Discover(), Send(method=wire.example(Method)), Acquire()):
            failed = ended(owner, owner.submit(job))
            assert isinstance(failed, JobFailed) and failed.message == owner.refused
        assert scans == []

        holder.release()
        assert isinstance(ended(owner, owner.submit(Discover())), JobFinished)
        assert len(scans) == 1 and not owner.refused
        assert owner.status().holder.program == "clockwork serve"
        with pytest.raises(LockHeld, match="clockwork serve"):
            InstrumentLock("a second window", path).acquire()
    finally:
        owner.shutdown()
        assert owner.join(10)
    assert read_holder(path) is None


def test_the_snapshot_is_the_last_sends(tmp_path):
    owner = LocalOwner(fake=True)
    assert owner.snapshot() is None
    owner._snapshot = Snapshot(conditions="dry N2")  # noqa: SLF001
    assert owner.snapshot() == Snapshot(conditions="dry N2") and owner.status().snapshot
    owner.shutdown()
    owner.serve()


# --- a box that goes away (lab #1) ------------------------------------------------


def _acquiring_owner(tmp_path, on_event=None, **options) -> LocalOwner:
    """A `--fake` owner with the daemon suite's method discovered and sent."""
    from test_daemon import make_method

    owner = LocalOwner(fake=True, program="clockwork owner test", on_event=on_event,
                       **options).start()
    owner.submit(StartConsole())
    method = make_method()
    assert isinstance(ended(owner, owner.submit(Discover(method=method))), JobFinished)
    return owner


def _transcript_events(directory) -> list[str]:
    [name] = [name for name in os.listdir(directory) if name.endswith(".transcript.log")]
    with open(os.path.join(directory, name), encoding="utf-8") as handle:
        return [line.rstrip("\n") for line in handle if line[:2].isdigit()]


def test_a_held_box_unplugged_before_a_run_is_dropped_and_the_run_goes_on(tmp_path):
    """The 15:52 re-run on lab #1: a box the method does not use had been unplugged, was
    still held, and ended the next run as well. Now its header read notices the port has
    gone, the owner lets go of it, and the run never reads it."""
    from test_acq_loop import UnpluggedBox
    from test_daemon import make_instrument, make_method

    owner = _acquiring_owner(tmp_path)
    try:
        stray = UnpluggedBox()
        stray.pulled = True
        owner.boxes["stray"] = Box(transport=stray, name="stray")
        method = make_method()
        # The send's own header is the first read of it, so the send lets go of it.
        sent = owner.submit(Send(method=method, directory=str(tmp_path),
                                 stem="260925_ZZ_001"))
        assert isinstance(ended(owner, sent), JobFinished)
        said = [entry.event.line for entry in owner.events(sent)
                if isinstance(entry.event, Said)]
        assert any(line.startswith("stray is no longer held") for line in said)
        assert "stray" not in owner.boxes and "stray" not in owner.status().boxes
        handle = owner.submit(Acquire(method=method, instrument=make_instrument(),
                                      directory=str(tmp_path), stem="260925_ZZ_001"))
        finished = ended(owner, handle, timeout=120)
        assert isinstance(finished, JobFinished), finished
        assert all(run.complete for run in finished.result)
        assert stray.touched_after == 1  # the one header read that found it gone
    finally:
        owner.shutdown()
        assert owner.join(30)


def test_a_used_box_unplugged_mid_run_fails_the_job_in_a_sentence_and_is_dropped(
        tmp_path):
    from test_acq_loop import UnpluggedBox
    from test_daemon import BOX, make_instrument, make_method

    pulled = UnpluggedBox()

    def pull_after_the_first(handle: Handle, event: Event) -> None:
        if isinstance(event, FrameEnded) and event.record.repetition == 1:
            pulled.pulled = True

    owner = _acquiring_owner(tmp_path, on_event=pull_after_the_first)
    try:
        owner.boxes[BOX] = Box(transport=pulled, name=BOX)
        method = make_method(accumulations=3)
        sent = owner.submit(Send(method=method, directory=str(tmp_path),
                                 stem="260925_ZZ_001"))
        assert isinstance(ended(owner, sent), JobFinished)
        handle = owner.submit(Acquire(method=method, instrument=make_instrument(),
                                      directory=str(tmp_path), stem="260925_ZZ_001",
                                      replicates=2))
        failed = ended(owner, handle, timeout=120)
        assert isinstance(failed, JobFailed), failed
        assert failed.message.startswith(f"{BOX} stopped answering on its port")
        assert "SerialException" not in failed.message and "OSError" not in failed.message
        assert BOX not in owner.boxes
        assert _transcript_events(tmp_path)[-1].endswith(f"Stopped: {failed.message}")
        # The next run that names it is refused before anything is sent.
        refused = ended(owner, owner.submit(Acquire(
            method=method, instrument=make_instrument(), directory=str(tmp_path),
            stem="260925_ZZ_002")))
        assert isinstance(refused, JobFailed) and "Find boxes again" in refused.message
    finally:
        owner.shutdown()
        assert owner.join(30)


def test_a_failed_run_is_kept_with_its_method_and_error_log_and_a_finished_one_is_not(
        tmp_path):
    """Task 81: the files behind a failure survive the trainee tidying the directory,
    and the job's sentence says where they went."""
    from clockwork import keep
    from test_acq_loop import UnpluggedBox
    from test_daemon import BOX, make_instrument, make_method

    pulled = UnpluggedBox()
    armed = [False]

    def pull_after_the_first(handle: Handle, event: Event) -> None:
        if armed[0] and isinstance(event, FrameEnded) and event.record.repetition == 1:
            pulled.pulled = True

    data, root = tmp_path / "data", tmp_path / "kept"
    method_file = tmp_path / "bradykinin.toml"
    method_file.write_text("# the method\n", encoding="utf-8")
    errors_log = tmp_path / "errors.log"
    errors_log.write_text("an earlier traceback\n", encoding="utf-8")
    owner = _acquiring_owner(tmp_path, on_event=pull_after_the_first,
                             kept_root=str(root), errors_log=str(errors_log))
    try:
        owner.boxes[BOX] = Box(transport=pulled, name=BOX)
        method = make_method(accumulations=3)
        job = {"method": method, "instrument": make_instrument(), "directory": str(data),
               "method_path": str(method_file)}
        assert isinstance(ended(owner, owner.submit(
            Send(method=method, directory=str(data), stem="260925_ZZ_001"))), JobFinished)
        finished = ended(owner, owner.submit(Acquire(stem="260925_ZZ_001", **job)),
                         timeout=120)
        assert isinstance(finished, JobFinished), finished
        assert not root.exists() or os.listdir(root) == []

        armed[0] = True
        failed = ended(owner, owner.submit(Acquire(stem="260925_ZZ_002", **job)),
                       timeout=120)
        assert isinstance(failed, JobFailed), failed
        [name] = os.listdir(root)
        folder = str(root / name)
        assert failed.message.startswith(f"{BOX} stopped answering on its port")
        assert failed.message.endswith(f". Files kept in {folder}")
        manifest = keep.read_manifest(folder)
        assert manifest.stems() == ["260925_ZZ_002"]
        assert manifest.fields["reason"] in failed.message
        originals = {row.original for row in manifest.rows if row.status == "copied"}
        assert str(method_file) in originals and str(errors_log) in originals
        assert not any("260925_ZZ_001" in path for path in originals)
        for path in keep.run_files(str(data), "260925_ZZ_002"):
            os.remove(path)
        for row in manifest.rows:
            if row.status == "copied":
                with open(os.path.join(folder, row.kept), "rb") as stream:
                    assert hashlib.sha256(stream.read()).hexdigest() == row.sha256
        [transcript] = [row.kept for row in manifest.rows
                        if row.kept.endswith(".transcript.log")]
        with open(os.path.join(folder, transcript), encoding="utf-8") as stream:
            events = [line.rstrip("\n") for line in stream if line[:2].isdigit()]
        assert events[-1].endswith(f"Stopped: {manifest.fields['reason']}")
    finally:
        owner.shutdown()
        assert owner.join(30)


def test_a_fake_owner_keeps_nothing_unless_given_a_root(tmp_path, monkeypatch):
    from clockwork import keep

    monkeypatch.setenv(keep.ENV, str(tmp_path / "configured"))
    assert LocalOwner(fake=True).kept_root == ""
    assert LocalOwner(fake=True, kept_root=str(tmp_path)).kept_root == str(tmp_path)


# --- the window under the lock ----------------------------------------------------


def test_a_window_refused_the_lock_says_who_holds_it_and_greys_the_hardware(
        qtbot, tmp_path, monkeypatch):
    pytest.importorskip("pytestqt")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings

    from clockwork.app.settings import Settings
    from clockwork.app.window import MainWindow

    backing = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr("clockwork.app.window.Settings", lambda: Settings(backing))
    holder = InstrumentLock("clockwork serve", os.environ[LOCK_ENV])
    holder.acquire()
    window = MainWindow()
    qtbot.addWidget(window)
    try:
        # The launch's own Find boxes, job 1, is refused with the sentence before any
        # port is opened.
        owner = window.worker.owner
        qtbot.waitUntil(lambda: any(isinstance(entry.event, JobFailed)
                                    for entry in owner.events(Handle(1, "", ""))),
                        timeout=10_000)
        qtbot.waitUntil(lambda: window._job is None, timeout=10_000)
        assert window.lock_label.isVisibleTo(window)
        assert "clockwork serve" in window.lock_label.text()
        assert window.find_button.isEnabled()
        for button in (window.setup_button, window.arm_button, window.acquire_button,
                       window.replicate_button):
            assert not button.isEnabled()
        assert not window.action_console.isEnabled()
        assert window.worker.boxes == {}
    finally:
        window.worker.shutdown()
        window.worker.wait(10_000)
        holder.release()
