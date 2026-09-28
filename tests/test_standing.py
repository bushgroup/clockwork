"""Warm-up and stand-down: the ramp on stand-in boxes, the `[standing]` table, and the two
verbs over a `--fake` daemon -- that warm-up sends the setup phase only and leaves the
daemon up, that stand-down leaves the boxes zeroed and local and the daemon gone, that
both refuse to overlap a run, and that warm-up starts a daemon when none answers.
"""

from __future__ import annotations

import dataclasses
import json
import socket
import threading

import pytest

from clockwork import instrument
from clockwork import method as method_module
from clockwork.acq import PhaseSent
from clockwork.acq.standing import RampStepped, ramp_values, stand_down, warm_up
from clockwork.app import main
from clockwork.mcp import Toolbox, ToolFailure
from clockwork.owner import Handle, LocalOwner, RemoteOwner, fake_rack
from test_daemon import Daemon

STANDING = """\
schema_version = 2
start = [["seq", "TBLSTRT"]]
reset = []

[metadata]
name = "standing-stack"
created = 2026-09-27
description = "The stack: sixteen DC bias channels, two RF heads, and one ARB box's range."

[acquisition]
frames = 1
scans = 16
accumulations = 1
repetition_mode = "per_repetition"
keep_raw = true
file_stem = "standing"

[[boxes]]
name = "seq"
port = "COM3"
setup = ["STBLCLK,EXT", "STBLTRG,SW"]
load = ["STBLDAT;0:A:1[A:1,16:];"]
arm = ["SMOD,TBL"]

[boxes.dc_bias]
1 = 99.0
2 = -70.0
16 = 5.0

[boxes.rf.1]
frequency_hz = 943000
drive_pct = 50.0
mode = "MANUAL"

[boxes.rf.2]
drive_pct = 30.0

[[boxes]]
name = "arb"
port = "COM4"
setup = ["SWFREQ,1,15000", "SWFVRNG,1,15", "SWFVRNG,2,12.5", "SWFDIR,1,FWD"]
load = []
arm = []
"""


def standing_method() -> method_module.Method:
    return method_module.loads(STANDING)


# --- the ramp --------------------------------------------------------------------


def test_a_ramp_is_equal_steps_ending_exactly_on_the_target():
    assert ramp_values(0.0, 99.0, 3) == [33.0, 66.0, 99.0]
    assert ramp_values(50.0, 0.0, 2) == [25.0, 0.0]
    assert ramp_values(-70.0, -70.0, 4)[-1] == -70.0
    assert ramp_values(1.0, 2.0, 0) == [2.0]  # never fewer than one step


def test_warm_up_sends_the_setup_phase_only_and_ramps_the_stack_from_the_read_back():
    method = standing_method()
    boxes = fake_rack(method)
    events: list = []
    naps: list[float] = []
    snapshot, steps = warm_up(method, boxes, steps=3, dwell_s=1.5, progress=events.append,
                              sleep=naps.append)
    sent = [event for event in events if isinstance(event, PhaseSent)]
    commands = [event.command for event in sent]
    assert steps == 3 and naps == [1.5, 1.5, 1.5]
    # Nothing past the setup phase: no table, no arming.
    assert not any(command.startswith(("STBLDAT", "SMOD,TBL")) for command in commands)
    assert "STBLCLK,EXT" in commands and "SWFREQ,1,15000" in commands
    # The declared DC bias moves as one bank per step, never as single channels.
    banks = [command for command in commands if command.startswith("SDCBALL,")]
    assert len(banks) == 3 and not any(command.startswith("SDCB,") for command in commands)
    assert banks[0].split(",")[1:3] == ["33.00", "-23.33"]
    assert banks[-1].split(",")[1] == "99.00" and banks[-1].split(",")[16] == "5.00"
    assert [command for command in commands if command.startswith("SRFDRV,1,")] == [
        "SRFDRV,1,16.67", "SRFDRV,1,33.33", "SRFDRV,1,50.00"]
    assert "SWFVRNG,2,12.50" in commands
    stepped = [event for event in events if isinstance(event, RampStepped)]
    assert [event.step for event in stepped] == [1, 2, 3]
    after = {state.name: state for state in snapshot.after}
    assert after["seq"].dc_bias(1) == 99.0 and after["seq"].dc_bias(2) == -70.0
    assert [reading.drive_pct for reading in after["seq"].rf] == [50.0, 30.0]
    assert after["arb"].module(1)["GWFVRNG"].startswith("15")


def test_a_box_already_at_its_target_is_sent_nothing_and_a_cut_ramp_resumes():
    method = standing_method()
    boxes = fake_rack(method)
    stops = iter([None, "operator"])

    with pytest.raises(Exception, match="stopped after ramp step 2 of 4"):
        warm_up(method, boxes, steps=4, dwell_s=0, stop=lambda: next(stops))
    events: list = []
    snapshot, _ = warm_up(method, boxes, steps=4, dwell_s=0, progress=events.append)
    first = next(event.command for event in events if isinstance(event, PhaseSent)
                 and event.command.startswith("SRFDRV,1,"))
    # From the half-way reading, 25 %, a quarter of the remaining way at a time.
    assert first == "SRFDRV,1,31.25"
    events.clear()
    warm_up(method, boxes, steps=4, dwell_s=0, progress=events.append)
    assert not any(isinstance(event, RampStepped) for event in events)
    assert not any(isinstance(event, PhaseSent) and event.command.startswith(
        ("SDCBALL", "SRFDRV", "SWFVRNG")) for event in events)


def test_stand_down_leaves_every_box_local_zeroed_and_its_outputs_low():
    method = standing_method()
    boxes = fake_rack(method)
    warm_up(method, boxes, steps=1, dwell_s=0)
    boxes["seq"].local()
    boxes["seq"].send_table("STBLDAT;0:A:1[A:1,16:];")
    boxes["seq"].arm()
    events: list = []
    snapshot, steps, lowered = stand_down(boxes, steps=2, dwell_s=0, progress=events.append)
    commands = [(event.box, event.command) for event in events
                if isinstance(event, PhaseSent)]
    assert commands[:2] == [("seq", "SMOD,LOC"), ("arb", "SMOD,LOC")]
    assert steps == 2
    assert ("seq", "SDIO,A,0") in commands and ("seq", "SDIO,P,0") in commands
    assert lowered == ("seq A-P", "arb A-P")
    for state in snapshot.after:
        assert all(value == 0.0 for value in state.dc_bias_setpoints)
        assert all(reading.drive_pct == 0.0 for reading in state.rf)
        assert all(float(state.module(module)["GWFVRNG"]) == 0.0
                   for module in state.modules)
        assert state.table_status == "IDLE"
    frequencies = {state.name: [reading.frequency_hz for reading in state.rf]
                   for state in snapshot.after}
    assert frequencies["seq"][0] == 943000  # frequency is left as it was


# --- the instrument document ------------------------------------------------------


def test_the_standing_table_round_trips_and_is_checked():
    text = ('schema_version = 1\n[standing]\nmethod = "detection-response/method.toml"\n'
            "steps = 4\ndwell_s = 2.5\n")
    loaded = instrument.loads(text)
    assert loaded.standing == instrument.Standing(
        method="detection-response/method.toml", steps=4, dwell_s=2.5)
    assert instrument.loads(instrument.dumps(loaded)) == loaded
    assert instrument.loads("schema_version = 1\n").standing == instrument.Standing()
    assert "[standing]" not in instrument.dumps(instrument.loads("schema_version = 1\n"))
    with pytest.raises(instrument.InstrumentError) as caught:
        instrument.loads("schema_version = 1\n[standing]\nsteps = 0\ndwell_s = -1\n"
                         "ramp = true\n")
    assert len(caught.value.problems) == 3


# --- the verbs over a daemon --------------------------------------------------------


@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "library"
    folder.mkdir()
    (folder / "standing.toml").write_text(STANDING, encoding="utf-8")
    return str(folder)


@pytest.fixture
def instrument_path(tmp_path):
    path = tmp_path / "instrument.toml"
    path.write_text('schema_version = 1\n[standing]\nmethod = "standing.toml"\n'
                    "steps = 2\ndwell_s = 0.05\n", encoding="utf-8")
    return str(path)


def verb(capsys, *words: str) -> tuple[int, dict | None, str]:
    capsys.readouterr()
    code = main(list(words))
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


def test_warm_up_then_stand_down_through_the_verbs(capsys, tmp_path, library,
                                                   instrument_path):
    (tmp_path / "runs").mkdir()
    made = Daemon(tmp_path / "runs", library=library)
    common = ("--endpoint", made.endpoint, "--instrument", instrument_path)
    try:
        code, answer, err = verb(capsys, "warm-up", *common)
        assert code == 0, err
        assert answer["ok"] and answer["differences"] == [] and answer["steps"] == 2
        assert "ramp step 2 of 2" in err
        with open(answer["send_log"], encoding="utf-8") as handle:
            log = handle.read()
        assert "SDCBALL" in log and "STBLDAT" not in log
        client = RemoteOwner(made.endpoint, timeout=10)
        try:
            state = client.status()
            assert state.armed is None and "seq" in state.boxes  # up, and nothing armed
        finally:
            client.close()

        code, answer, err = verb(capsys, "stand-down", *common)
        assert code == 0, err
        assert answer["ok"] and answer["not_zero"] == [] and answer["daemon"]["stopped"]
        assert answer["outputs_lowered"] == ["seq A-P", "arb A-P"]
        made.thread.join(30)
        assert not made.thread.is_alive()
    finally:
        made.stop()


class _Busy:
    """An owner whose status says an acquisition is running."""

    def __init__(self, owner: LocalOwner) -> None:
        self._owner = owner

    def __getattr__(self, name: str) -> object:
        return getattr(self._owner, name)

    def status(self):
        return dataclasses.replace(self._owner.status(), running=Handle(
            id=7, kind="Acquire", label="acquiring for r-1"))


def test_both_are_refused_while_a_run_is_in_flight(tmp_path, library, instrument_path):
    owner = LocalOwner(fake=True, program="standing test").start()
    try:
        toolbox = Toolbox(_Busy(owner), library=library, output=str(tmp_path),
                          instrument=instrument.load(instrument_path))
        with pytest.raises(ToolFailure, match="job 7 .* is running"):
            toolbox.call("warm_up", {})
        with pytest.raises(ToolFailure, match="stand-down ends no acquisition by surprise"):
            toolbox.call("stand_down", {})
    finally:
        owner.shutdown()
        owner.join(30)


def test_warm_up_starts_a_daemon_when_none_answers(capsys, monkeypatch, tmp_path, library,
                                                   instrument_path):
    from clockwork.app import serving

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    endpoint = f"tcp://127.0.0.1:{port}"
    (tmp_path / "runs").mkdir()
    started: dict[str, object] = {}

    class Launched:
        def poll(self):
            return None

    def launch(command: list[str]) -> Launched:
        # The command line a real start would run, and an in-process `--fake` daemon in
        # its place: `serve` has no endpoint flag, and a test must not bind 5570.
        started["command"] = command
        started["daemon"] = threading.Thread(target=lambda: started.setdefault(
            "made", Daemon(tmp_path / "runs", command=endpoint, library=library)))
        started["daemon"].start()
        return Launched()

    monkeypatch.setattr(serving, "start_serve", launch)
    try:
        code, answer, err = verb(capsys, "warm-up", "--endpoint", endpoint,
                                 "--instrument", instrument_path, "--library", library)
        assert code == 0, err
        assert "starting one" in err and answer["ok"]
        assert "serve" in started["command"] and library in started["command"]
    finally:
        started["daemon"].join(30)
        made = started.get("made")
        if made is not None:
            made.stop()


def test_a_saved_instrument_document_that_will_not_load_says_where_it_came_from(
        capsys, monkeypatch, tmp_path, library):
    """The shortcut's warm-up is given no flags; a deleted document named in the
    window's saved settings read as a flag nobody gave (lab record, task 96)."""
    missing = str(tmp_path / "deleted" / "instrument.toml")
    monkeypatch.setattr("clockwork.app.saved.saved_settings",
                        lambda keys=None: {"instrument_path": missing})
    (tmp_path / "runs").mkdir()
    made = Daemon(tmp_path / "runs", library=library)
    try:
        code, answer, err = verb(capsys, "warm-up", "--endpoint", made.endpoint)
        assert code == 1 and answer is None
        assert missing in err and "window's saved instrument document" in err
        code, answer, err = verb(capsys, "warm-up", "--endpoint", made.endpoint,
                                 "--instrument", missing)
        assert code == 1 and "window's saved" not in err, "a typed path is not saved"
    finally:
        made.stop()


def test_a_routine_given_no_instrument_takes_the_saved_one_as_warm_up_does(
        capsys, monkeypatch, tmp_path, library, instrument_path):
    """With no document a routine had no limits and refused its first send, burning a
    file number, where warm-up had found the document saved (lab record, task 96). The
    saved library is not taken: the routine runs in the daemon's."""
    missing = str(tmp_path / "deleted" / "instrument.toml")
    monkeypatch.setattr("clockwork.app.saved.saved_settings", lambda keys=None: {
        "instrument_path": missing, "library_dir": str(tmp_path / "not-the-daemons")})
    (tmp_path / "runs").mkdir()
    made = Daemon(tmp_path / "runs", library=library)
    try:
        code, answer, err = verb(capsys, "routine", "no-such", "--initials", "zz",
                                 "--endpoint", made.endpoint)
        assert code == 1 and missing in err and "window's saved instrument" in err
        code, answer, err = verb(capsys, "routine", "no-such", "--initials", "zz",
                                 "--endpoint", made.endpoint, "--instrument",
                                 instrument_path)
        assert code == 1 and "no-such" in err and "window's saved" not in err
    finally:
        made.stop()


def test_a_daemon_started_from_the_console_twin_runs_as_the_windowed_exe(monkeypatch,
                                                                         tmp_path):
    from clockwork.app import serving

    (tmp_path / "clockwork.exe").write_bytes(b"")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", str(tmp_path / "clockwork-cli.exe"))
    assert serving.serve_command()[:2] == [str(tmp_path / "clockwork.exe"), "serve"]
