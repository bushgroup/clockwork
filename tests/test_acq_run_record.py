"""What a file records about the run that is not the method (lab record, task 25).

Three things the method cannot supply and a file has to state: the m/z calibration, so
`CalibrationDone` is 1 and a mass axis exists; a `StartTime` per frame off a run clock,
so a viewer can lay out a session; and the vertical settings in force, so two files
acquired through different ranges are distinguishable. All three come from
`clockwork.instrument`, and this file is where each is asserted to reach the disk.

The clock is substituted throughout. `Recording` times frames and start times off one
clock precisely so that a test can control both, which is what makes "the second frame
starts later than the first" an equality rather than a race.
"""

from __future__ import annotations

import datetime as dt

import pytest
from mainspring.uimf import UimfFile

from clockwork import instrument as instrument_module
from clockwork.acq import Console, DataStream, FakeConsole, Geometry, Recording, run_frame
from clockwork.acq.uimf import PROVENANCE_KEYS, stamp_globals
from clockwork.method import Method, from_dict

SCANS = 32

CALIBRATION = instrument_module.Calibration(
    slope=0.738123, intercept=0.07690495, measured=dt.date(2026, 9, 9)
)
"""The pair the golden files carry, which is the one a real acquisition would write."""

SLIMPHONY = instrument_module.Instrument(
    name="SLIM3",
    calibration=CALIBRATION,
    vertical=instrument_module.Vertical(full_scale_v=0.5, offset_v=0.251),
)


class FakeClock:
    """A clock that only moves when a test moves it."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def make_method(*, accumulations: int = 2, frames: int = 1, stem: str = "260911_TEST_001",
                repetition_mode: str = "per_repetition") -> Method:
    return from_dict({
        "schema_version": 2,
        "metadata": {"name": "test method", "created": dt.date(2026, 9, 11)},
        "acquisition": {
            "frames": frames,
            "scans": SCANS,
            "accumulations": accumulations,
            "file_stem": stem,
            "repetition_mode": repetition_mode,
        },
        "boxes": [{"name": "box1", "port": "COM1", "load": ["STBLDAT;..."]}],
        "start": [["box1", "TBLSTRT"]],
    })


def start_minutes(path: str, frame: int) -> float:
    """`StartTimeMinutes` off a frame, which the reader hands back in `extra`."""
    return float(UimfFile(path).frame_params(frame).extra["StartTimeMinutes"])


def duration_seconds(path: str, frame: int) -> float:
    return float(UimfFile(path).frame_params(frame).extra["DurationSeconds"])


def make_geometry(fake: FakeConsole) -> Geometry:
    return Geometry.from_tof_width(
        fake.tof_width(), sample_rate_hz=2e9,
        post_trigger_samples=fake.post_trigger_samples,
    )


@pytest.fixture
def geometry():
    with FakeConsole() as fake:
        return make_geometry(fake)


# --- the calibration -----------------------------------------------------------------


def test_an_instrument_with_a_calibration_writes_a_mass_axis(tmp_path, geometry):
    """`CalibrationDone = 1`, which no file this code wrote had before task 25."""
    method = make_method(accumulations=1)
    with Recording.create(tmp_path, method, geometry, instrument=SLIMPHONY) as recording:
        path = recording.raw_path
        recording.begin_frame(1, 1)
        recording.end_frame()
    params = UimfFile(path).frame_params(1)
    assert params.calibration_slope == pytest.approx(CALIBRATION.slope)
    assert params.calibration_intercept == pytest.approx(CALIBRATION.intercept)
    assert params.calibration_done


def test_no_instrument_still_writes_an_honest_uncalibrated_file(tmp_path, geometry):
    """The default is the file this code wrote before an instrument document existed: a
    bin axis, no mass axis, and nothing claiming otherwise."""
    method = make_method(accumulations=1)
    with Recording.create(tmp_path, method, geometry) as recording:
        path = recording.raw_path
        recording.begin_frame(1, 1)
        recording.end_frame()
    params = UimfFile(path).frame_params(1)
    assert params.calibration_slope == 0.0
    assert not params.calibration_done


def test_the_instrument_names_itself_under_pnnls_own_key(tmp_path, geometry):
    """`InstrumentName`, not a clockwork key: a fact under two keys is two to keep in
    step, and every UIMF tool reads this one."""
    with Recording.create(tmp_path, make_method(), geometry,
                          instrument=SLIMPHONY) as recording:
        path = recording.raw_path
    assert UimfFile(path).global_params().instrument_name == "SLIM3"


def test_the_companion_inherits_the_calibration(tmp_path, geometry):
    """The fold's file is the one a trainee keeps, so it carries the mass axis too."""
    method = make_method(accumulations=2)
    with FakeConsole() as fake, \
            DataStream(fake.data_endpoint) as stream, \
            Console(fake.command_endpoint) as console:
        from clockwork.acq import start_chain
        start_chain(console, stream, timeout=10.0, settle=2.0, quiet=0.1)
        with Recording.create(tmp_path, method, geometry,
                              instrument=SLIMPHONY) as recording:
            summed = recording.summed_path
            for repetition in (1, 2):
                with recording.frame(1, repetition) as request:
                    run_frame(console, stream, request, timeout=10.0)
            recording.fold(1)
        console.stop_acquire()
    params = UimfFile(summed).frame_params(1)
    assert params.calibration_slope == pytest.approx(CALIBRATION.slope)
    assert params.calibration_done


# --- StartTime -----------------------------------------------------------------------


def test_start_time_is_minutes_since_the_run_clocks_origin(tmp_path, geometry):
    clock = FakeClock()
    method = make_method(accumulations=3)
    with Recording.create(tmp_path, method, geometry, clock=clock,
                          started=clock()) as recording:
        path = recording.raw_path
        for repetition in (1, 2, 3):
            recording.begin_frame(1, repetition)
            recording.end_frame()
            clock.advance(30.0)
    assert start_minutes(path, 1) == pytest.approx(0.0)
    assert start_minutes(path, 2) == pytest.approx(0.5)
    assert start_minutes(path, 3) == pytest.approx(1.0)


def test_a_recording_given_no_epoch_times_from_its_own_creation(tmp_path, geometry):
    """The default has to be right rather than zero: rig scripts, the tests and the
    self-check all drive a `Recording` directly (lab record, task 25)."""
    clock = FakeClock()
    method = make_method(accumulations=2)
    clock.advance(500.0)
    with Recording.create(tmp_path, method, geometry, clock=clock) as recording:
        path = recording.raw_path
        recording.begin_frame(1, 1)
        recording.end_frame()
        clock.advance(60.0)
        recording.begin_frame(1, 2)
        recording.end_frame()
    assert start_minutes(path, 1) == pytest.approx(0.0)
    assert start_minutes(path, 2) == pytest.approx(1.0)


def test_the_summed_frame_starts_when_its_first_repetition_did(tmp_path, geometry):
    """Not when the fold got round to it, which can be a whole method frame later."""
    clock = FakeClock()
    method = make_method(accumulations=2, frames=2)
    with FakeConsole() as fake, \
            DataStream(fake.data_endpoint) as stream, \
            Console(fake.command_endpoint) as console:
        from clockwork.acq import start_chain
        start_chain(console, stream, timeout=10.0, settle=2.0, quiet=0.1)
        with Recording.create(tmp_path, method, geometry, instrument=SLIMPHONY,
                              clock=clock, started=clock()) as recording:
            summed = recording.summed_path
            for method_frame in (1, 2):
                clock.advance(120.0)
                for repetition in (1, 2):
                    with recording.frame(method_frame, repetition) as request:
                        run_frame(console, stream, request, timeout=10.0)
                    clock.advance(10.0)
                # The fold happens well after the rows it is adding up.
                clock.advance(300.0)
                recording.fold(method_frame)
        console.stop_acquire()
    # Method frame 1's first repetition began 120 s in. Method frame 2's began
    # 120 + 10 + 10 + 300 + 120 = 560 s in, which is 300 s after the fold of frame 1 and
    # is the number a summed frame would carry if it were timed by its fold instead.
    assert start_minutes(summed, 1) == pytest.approx(2.0)
    assert start_minutes(summed, 2) == pytest.approx(560 / 60)


def test_one_clock_times_the_frames_and_their_start_times(tmp_path, geometry):
    """A substituted clock controls both, which is only true because there is one."""
    clock = FakeClock()
    method = make_method(accumulations=1)
    with Recording.create(tmp_path, method, geometry, clock=clock,
                          started=clock()) as recording:
        summed = recording.summed_path
        recording.begin_frame(1, 1)
        clock.advance(45.0)
        recording.end_frame()
        recording.fold(1)
    assert duration_seconds(summed, 1) == pytest.approx(45.0)
    assert start_minutes(summed, 1) == pytest.approx(0.0)


# --- the vertical settings in the stamp ----------------------------------------------


def test_the_vertical_settings_reach_the_stamp(tmp_path, geometry):
    """Two files acquired through different ranges are otherwise indistinguishable."""
    with Recording.create(tmp_path, make_method(), geometry,
                          instrument=SLIMPHONY) as recording:
        path = recording.raw_path
    extra = UimfFile(path).global_params().extra
    assert extra["ClockworkChannelOffset"] == "0.251"
    assert extra["ClockworkFullScale"] == "0.5"


def test_the_two_new_keys_sit_in_clockworks_own_block(tmp_path, geometry):
    """Above mainspring's, below nobody's, and named so an unrecognised name cannot be
    parsed into a future PNNL enum member."""
    from mainspring.uimf.writer import CLIENT_PARAM_ID_BASE

    added = {key.name: key for key, _field in PROVENANCE_KEYS}
    for name in ("ClockworkChannelOffset", "ClockworkFullScale"):
        assert added[name].param_id > CLIENT_PARAM_ID_BASE
        assert added[name].data_type == "System.Double"
    ids = [key.param_id for key, _field in PROVENANCE_KEYS]
    assert len(ids) == len(set(ids)), "two parameters would collide on one ID"


def test_an_instrument_with_no_window_stamps_no_window() -> None:
    """Absent is a better statement than a made-up number, and a rig in front of a
    function generator has no window to state."""
    stamped = stamp_globals(make_method())
    names = {getattr(key, "name", key) for key in stamped}
    assert "ClockworkChannelOffset" not in names
    assert "ClockworkFullScale" not in names


def test_a_zero_offset_is_stamped_rather_than_dropped() -> None:
    """Zero volts is a real setting and a falsy one."""
    machine = instrument_module.Instrument(
        vertical=instrument_module.Vertical(offset_v=0.0)
    )
    stamped = stamp_globals(make_method(), instrument=machine)
    offsets = [value for key, value in stamped.items()
               if getattr(key, "name", "") == "ClockworkChannelOffset"]
    assert offsets == [0.0]


def test_the_companion_carries_the_same_stamp(tmp_path, geometry):
    method = make_method(accumulations=1)
    with Recording.create(tmp_path, method, geometry, instrument=SLIMPHONY) as recording:
        summed = recording.summed_path
        recording.begin_frame(1, 1)
        recording.end_frame()
        recording.fold(1)
    extra = UimfFile(summed).global_params().extra
    assert extra["ClockworkFullScale"] == "0.5"
    assert extra["ClockworkChannelOffset"] == "0.251"
