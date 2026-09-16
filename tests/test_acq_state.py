"""Task 40: the snapshot a run takes, and what it says about the boxes.

The half of the golden record the strings do not carry. `send_phases` reads every
box back before it sends anything and again after the `setup` phase, reports the
ARB settings the method leaves as it found them and the declared DC bias and RF
the box disagrees with, and hands back a `Snapshot` the file's stamp holds.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import tempfile

from mainspring.uimf import UimfFile

from clockwork import method as method_module
from clockwork import transcript
from clockwork.acq import (
    Geometry,
    Recording,
    Snapshot,
    StateRead,
    Warned,
    declared_differences,
    left_as_found,
    send_phases,
)
from clockwork.mips import Box, FakeBox, read_state

SCANS = 16


def document(**box_extra: object) -> dict:
    return {
        "schema_version": method_module.SCHEMA_VERSION,
        "metadata": {"name": "state", "created": dt.date(2026, 9, 15)},
        "acquisition": {"frames": 1, "scans": SCANS, "accumulations": 1,
                        "file_stem": "state", "enable": {"box": "auklet",
                                                         "channel": "A"}},
        "boxes": [{"name": "auklet", "port": "COM6",
                   "setup": ["STBLCLK,EXTS", "STBLTRG,SW"],
                   "load": [f"STBLDAT;0:A:1[A:1,{SCANS}:];"],
                   "arm": ["SMOD,TBL"], **box_extra}],
        "start": [["auklet", "TBLSTRT"]],
    }


def geometry() -> Geometry:
    """A bin axis with numbers in it. Nothing here is about the axis."""
    return Geometry(bins=64, bin_width_ns=0.5, time_offset_ns=0.0,
                    average_tof_length_ns=32.0, offset_bins=0)


def auklet_box() -> Box:
    fake = FakeBox(name="MIPS-A", dcb_channels=16, rf_channels=2)
    fake.rf[1].update({"SRFFRQ": "943000", "SRFDRV": "50.00"})
    return Box(transport=fake, name="auklet")


# --- the two readings ------------------------------------------------------------------


def test_send_phases_reads_each_box_before_and_after_its_setup() -> None:
    method = method_module.from_dict(document())
    seen: list[object] = []
    snapshot = send_phases(method, {"auklet": auklet_box()}, progress=seen.append)
    assert [event.when for event in seen if isinstance(event, StateRead)] \
        == ["before", "after"]
    assert len(snapshot.before) == 1 and len(snapshot.after) == 1
    assert snapshot.before[0].name == "auklet"


def test_a_run_that_sends_no_setup_takes_one_reading_and_says_so() -> None:
    """`setup=False` is a box that has had its setup since power-up. There is
    nothing for an "after" reading to be after."""
    method = method_module.from_dict(document())
    snapshot = send_phases(method, {"auklet": auklet_box()}, setup=False)
    assert len(snapshot.before) == 1 and snapshot.after == ()


def test_the_snapshot_can_be_turned_off_entirely() -> None:
    method = method_module.from_dict(document())
    box = auklet_box()
    snapshot = send_phases(method, {"auklet": box}, snapshot=False)
    assert not snapshot
    assert b"GDCBALL" not in b"".join(box.transport.written)


def test_the_reading_is_getters_only_and_writes_nothing() -> None:
    box = auklet_box()
    before = list(box.transport.dc_bias)
    send_phases(method_module.from_dict(document()), {"auklet": box})
    assert box.transport.dc_bias == before


def test_a_box_that_cannot_be_read_warns_and_the_run_goes_on() -> None:
    """A readback is a record. A run with no record of one box beats no run.

    `OSError` is the case that matters: a port unplugged between two acquisitions
    raises `serial.SerialException`, which is one. A box that cannot answer the
    identity `send_phases` itself asks for is a different failure and still stops
    the send.
    """

    class Mute(FakeBox):
        def _do_gdcball(self, _: str) -> None:
            raise OSError("the port went away")

    method = method_module.from_dict(document())
    seen: list[object] = []
    snapshot = send_phases(method, {"auklet": Box(transport=Mute(), name="auklet")},
                           progress=seen.append)
    assert snapshot.before == ()
    assert any(isinstance(event, Warned) and "state readback" in event.message
               for event in seen)


# --- declared DC bias and RF -----------------------------------------------------------


def test_a_declaration_reaches_the_box_at_the_end_of_the_setup_phase() -> None:
    method = method_module.from_dict(document(
        dc_bias={"15": 0.0, "16": 5.0},
        rf={"1": {"frequency_hz": 943000, "drive_pct": 50.0, "mode": "MANUAL"}},
    ))
    box = auklet_box()
    seen: list[object] = []
    send_phases(method, {"auklet": box}, progress=seen.append)
    setup = [event.command for event in seen
             if getattr(event, "phase", None) == "setup"]
    assert setup[-5:] == ["SDCB,15,0.00", "SDCB,16,5.00", "SRFFRQ,1,943000",
                          "SRFDRV,1,50.00", "SRFMODE,1,MANUAL"]
    assert box.transport.dc_bias[15] == 5.0
    assert box.transport.rf[1]["SRFDRV"] == "50.00"


def test_a_box_that_agrees_with_its_method_is_warned_about_nothing() -> None:
    method = method_module.from_dict(document(dc_bias={"16": 5.0}))
    seen: list[object] = []
    send_phases(method, {"auklet": auklet_box()}, progress=seen.append)
    assert not [event for event in seen
                if isinstance(event, Warned) and "declared" in event.message]


def test_a_bias_the_box_does_not_hold_is_warned_about_and_refuses_nothing() -> None:
    """Matt's decision of 2026-09-15: a mismatch warns and the acquisition goes on.

    A monitor past tolerance or a channel the box quantised should not stop an
    instrument session; a trainee who sees the line decides.
    """
    method = method_module.from_dict(document(dc_bias={"16": 5.0}))
    box = auklet_box()
    snapshot = send_phases(method, {"auklet": box})
    assert declared_differences(method.boxes[0], snapshot.after[0]) == []
    box.transport.dc_bias[15] = 37.0  # the CLOCK activation, left behind
    moved = declared_differences(method.boxes[0], read_state(box))
    assert len(moved) == 1
    assert "5.00 V" in moved[0] and "37.00 V" in moved[0]


def test_a_monitor_that_is_not_following_its_setpoint_is_its_own_line() -> None:
    """Two comparisons against two different numbers (§8.2): the setpoint against
    what was declared, and the monitor against the setpoint."""
    method = method_module.from_dict(document(dc_bias={"16": 5.0}))
    box = auklet_box()
    box.transport.dc_bias[15] = 5.0
    box.transport.dc_bias_error = 4.0
    lines = declared_differences(method.boxes[0], read_state(box))
    assert len(lines) == 1 and "monitors 9.00 V" in lines[0]


def test_a_quantised_rf_frequency_is_inside_the_tolerance() -> None:
    """What these boxes do to a frequency is quantise it: `SWFREQ` came back 0.57%
    below the 15000 asked for. A head is not compared to the hertz."""
    method = method_module.from_dict(document(
        rf={"1": {"frequency_hz": 943000, "drive_pct": 50.0}}))
    box = auklet_box()
    box.transport.rf[1]["SRFFRQ"] = "942500"
    assert declared_differences(method.boxes[0], read_state(box)) == []
    box.transport.rf[1]["SRFFRQ"] = "804000"
    assert len(declared_differences(method.boxes[0], read_state(box))) == 1


def test_a_declared_channel_the_box_does_not_have_is_named() -> None:
    method = method_module.from_dict(document(dc_bias={"30": 1.0}, rf={"4": {"drive_pct": 1.0}}))
    lines = declared_differences(method.boxes[0], read_state(auklet_box()))
    assert len(lines) == 2
    assert all("no such channel" in line for line in lines)


# --- left as found ---------------------------------------------------------------------


def test_every_module_setting_the_setup_does_not_name_is_reported() -> None:
    """The list the 2026-09-15 conditions document wrote by hand: `SWFDIR` REV left
    behind on two modules by the CLOCK method, and nothing knowing about it."""
    method = method_module.from_dict(document())
    arb = read_state(Box(transport=FakeBox(arb_modules=4), name="bufflehead"))
    loose = left_as_found(method.boxes[0], arb)
    assert any("WFDIR is left as found on modules 1: FWD, 2: FWD" in line
               for line in loose)


def test_a_setting_the_setup_does_name_is_not_reported() -> None:
    method = method_module.from_dict(document(
        setup=[f"SWFDIR,{module},FWD" for module in (1, 2, 3, 4)]))
    arb = read_state(Box(transport=FakeBox(arb_modules=4), name="bufflehead"))
    loose = left_as_found(method.boxes[0], arb)
    assert not any("WFDIR" in line for line in loose)
    assert any("WFREQ" in line for line in loose)


def test_a_box_with_no_modules_has_nothing_left_as_found() -> None:
    method = method_module.from_dict(document())
    assert left_as_found(method.boxes[0], read_state(auklet_box())) == []


# --- what lands in the file and in the log ---------------------------------------------


def test_the_snapshot_is_stamped_into_the_file_as_the_log_renders_it() -> None:
    method = method_module.from_dict(document())
    box = auklet_box()
    snapshot = send_phases(method, {"auklet": box})
    snapshot = Snapshot(before=snapshot.before, after=snapshot.after,
                        conditions="bradykinin 1 uM; MCP 1750 V")
    with tempfile.TemporaryDirectory() as directory:
        recording = Recording.create(directory, method, geometry(),
                                     box_state=snapshot.render(),
                                     conditions=snapshot.conditions)
        path = recording.raw_path
        recording.close()
        stamped = UimfFile(path).global_params().extra
    assert stamped["ClockworkConditions"] == "bradykinin 1 uM; MCP 1750 V"
    assert "as found" in stamped["ClockworkBoxState"]
    assert "after setup" in stamped["ClockworkBoxState"]
    assert "DCB 16" in stamped["ClockworkBoxState"]


def test_a_run_that_takes_no_snapshot_stamps_nothing_rather_than_an_empty_string() -> None:
    method = method_module.from_dict(document())
    with tempfile.TemporaryDirectory() as directory:
        recording = Recording.create(directory, method, geometry())
        path = recording.raw_path
        recording.close()
        stamped = UimfFile(path).global_params().extra
    assert "ClockworkBoxState" not in stamped
    assert "ClockworkConditions" not in stamped


def test_the_send_log_carries_the_block_under_each_box_own_name() -> None:
    method = method_module.from_dict(document())
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, transcript.send_log_name("state"))
        header = transcript.run_header(method=method,
                                       conditions="bradykinin 1 uM\nMCP 1750 V")
        with transcript.send_log(path, header=header):
            send_phases(method, {"auklet": auklet_box()})
        written = open(path, encoding="utf-8").read()
    assert f"auklet      {transcript.ASIDE} state before setup" in written
    assert f"auklet      {transcript.ASIDE} state after setup" in written
    assert f"auklet      {transcript.ASIDE}   DCB 16" in written
    # The getters themselves are not here: the block stands for them (task 40).
    assert "GDCBALL" not in written
    # And the operator's own lines are in the header, where a replicate's log
    # picks them up too.
    assert "conditions\n  bradykinin 1 uM\n  MCP 1750 V" in written


def test_the_conditions_note_is_left_out_where_there_is_none() -> None:
    """A header line saying "conditions:" with nothing under it reads as a field
    that failed rather than one nobody filled in."""
    assert "conditions" not in transcript.run_header(console="stand-in")
    assert "conditions" not in transcript.run_header(console="stand-in", conditions="  \n ")


def test_note_block_gives_every_line_its_own_record() -> None:
    """One record a line, so a block keeps its columns down the page."""

    class Collected(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.DEBUG)
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    logger = logging.getLogger(transcript.ROOT_LOGGER)
    handler, level = Collected(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        transcript.note_block("one\n\ntwo\n", source="auklet")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    assert handler.messages == ["one", "two"]
