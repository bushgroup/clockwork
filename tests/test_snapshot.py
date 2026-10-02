"""The box snapshot: `read_health` against the stand-in, and `snapshot_boxes` over a
`--fake` owner -- that it writes its three files, sends getters only and leaves an
armed box armed, that it is refused while a run is in flight, and that an older
firmware without the health getters is recorded as such rather than failed.
"""

from __future__ import annotations

import json
import os

import pytest

from clockwork import instrument
from clockwork.mcp import Toolbox, ToolFailure
from clockwork.mips import Box, FakeBox, read_health
from clockwork.owner import LocalOwner, SnapshotResult
from clockwork.owner.wire import from_wire
from test_standing import STANDING, _Busy

HEALTH_COMMANDS = {"UPTIME", "STATUS", "THREADS"}


# --- the reader ------------------------------------------------------------------


def test_read_health_parses_the_firmware_shapes():
    stand_in = FakeBox(name="auklet", rf_channels=2)
    stand_in.twi_fails = 3
    stand_in.reset_cause = "Watchdog fault occurred"
    stand_in.threads.append(("ARB", 4, 100, False, 0))
    health = read_health(Box(stand_in, name="auklet"))
    assert health.uptime_min is not None and 0 <= health.uptime_min < 1
    assert health.reset_cause == "Watchdog fault occurred"
    assert health.status_millis is not None
    assert health.twi_fails == 3
    assert health.dc_power == "ON" and health.last_error == 0
    assert [row.name for row in health.threads] == ["DCbias", "RFdriver", "Serial", "ARB"]
    assert [row.name for row in health.threads if not row.enabled] == ["ARB"]
    assert not health.skipped and not health.refused
    text = health.render()
    assert "TWI fails   3" in text and "DISABLED" in text


def test_a_firmware_without_the_health_getters_is_skipped_not_failed():
    stand_in = FakeBox(name="old")
    stand_in.withheld = set(HEALTH_COMMANDS)
    health = read_health(Box(stand_in, name="old"))
    assert health.skipped == ("UPTIME", "STATUS", "THREADS")
    assert health.uptime_min is None and health.twi_fails is None
    assert health.dc_power == "ON"
    sent = b"".join(stand_in.written).decode("ascii")
    assert not any(command in sent for command in HEALTH_COMMANDS)


def test_a_status_without_the_twi_line_is_absent_not_zero():
    stand_in = FakeBox(name="old")
    stand_in._do_status = lambda _: stand_in._emit(b"\x06First power-up Reset, 5\r\n")
    health = read_health(Box(stand_in, name="old"))
    assert health.reset_cause == "First power-up Reset"
    assert health.twi_fails is None
    assert "(not reported)" in health.render()


# --- the verb ----------------------------------------------------------------------


@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "library"
    folder.mkdir()
    (folder / "standing.toml").write_text(STANDING, encoding="utf-8")
    return str(folder)


@pytest.fixture
def standing_instrument(tmp_path):
    path = tmp_path / "instrument.toml"
    path.write_text('schema_version = 1\n[standing]\nmethod = "standing.toml"\n'
                    "steps = 2\ndwell_s = 0.05\n", encoding="utf-8")
    return instrument.load(str(path))


@pytest.fixture
def owner():
    made = LocalOwner(fake=True, program="snapshot test").start()
    yield made
    made.shutdown()
    made.join(30)


def test_a_snapshot_writes_its_files_sends_only_getters_and_leaves_a_box_armed(
        tmp_path, library, standing_instrument, owner):
    out = tmp_path / "runs"
    toolbox = Toolbox(owner, library=library, output=str(out),
                      instrument=standing_instrument)
    toolbox.call("warm_up", {})
    seq = owner.boxes["seq"]
    seq.local()
    seq.send_table("STBLDAT;0:A:1[A:1,16:];")
    seq.arm()
    stand_ins = {name: box.transport for name, box in owner.boxes.items()}
    before = {name: len(stand_in.written) for name, stand_in in stand_ins.items()}

    answer = toolbox.call("snapshot_boxes", {"note": "low intensity, run 095, BF"})

    assert answer["ok"] and set(answer["boxes"]) == {"seq", "arb"}
    assert answer["stem"].startswith("snapshot-")
    for key in ("send_log", "transcript", "json"):
        assert os.path.isfile(answer[key]), key
        assert os.path.dirname(answer[key]) == str(out)
    assert answer["boxes"]["seq"]["dc_power"] == "ON"
    assert answer["boxes"]["seq"]["twi_fails"] == 0

    for name, stand_in in stand_ins.items():
        sent = b"".join(stand_in.written[before[name]:]).decode("ascii").split("\n")
        commands = {line.partition(",")[0].strip() for line in sent if line.strip()}
        assert commands, name
        assert all(command.startswith("G") or command in HEALTH_COMMANDS
                   for command in commands), (name, sorted(commands))
    assert stand_ins["seq"].mode == "TBL" and stand_ins["seq"].status == "READY"

    with open(answer["send_log"], encoding="utf-8") as handle:
        log = handle.read()
    assert "low intensity, run 095, BF" in log
    assert "seq: health" in log and "state at snapshot" in log
    with open(answer["json"], encoding="utf-8") as handle:
        written = from_wire(json.load(handle))
    assert isinstance(written, SnapshotResult)
    assert written.note == "low intensity, run 095, BF"
    assert written.health["arb"].reset_cause == "First power-up Reset"
    assert written.states["seq"].table_status == "READY"


def test_a_snapshot_goes_to_the_directory_it_is_given(tmp_path, library,
                                                      standing_instrument, owner):
    toolbox = Toolbox(owner, library=library, output=str(tmp_path / "runs"),
                      instrument=standing_instrument)
    elsewhere = tmp_path / "data" / "260930"
    answer = toolbox.call("snapshot_boxes", {"directory": str(elsewhere)})
    assert answer["ok"]
    assert os.path.dirname(answer["json"]) == str(elsewhere)


def test_a_snapshot_is_refused_while_a_run_is_in_flight(tmp_path, library,
                                                        standing_instrument, owner):
    toolbox = Toolbox(_Busy(owner), library=library, output=str(tmp_path),
                      instrument=standing_instrument)
    with pytest.raises(ToolFailure, match="job 7 .* is running: a snapshot reads"):
        toolbox.call("snapshot_boxes", {})
