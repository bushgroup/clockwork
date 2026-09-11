"""The two files an acquisition produces: their parameters, the two phases, the fold.

Everything here runs against `FakeConsole`, which appends `Frame_Scans` the way the real
console does, so the whole chain -- clockwork creates the file, a console fills it,
mainspring reads it back, clockwork folds it -- is exercised with no hardware.

The fold's own acceptance test is the one the bench will run with a pulse generator: a
method frame of A repetitions must sum to exactly A times one repetition, bin for bin
(lab record, task 18). The fake's invented spectrum repeats every `scan_period` scans
precisely so that this can be asserted as an equality rather than a tolerance.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3

import numpy as np
import pytest
from mainspring.uimf import UimfFile

import clockwork
from clockwork.acq import (
    Console,
    DataStream,
    FakeConsole,
    Geometry,
    Recording,
    fold_scans,
    raw_path,
    run_frame,
    start_chain,
    summed_path,
)
from clockwork.acq.uimf import PROVENANCE_KEYS, stamp_globals
from clockwork.method import Method, from_dict

SCANS = 32
"""Two of the fake's 16-scan spectrum periods, so a fold has something to add."""


def make_method(
    *,
    scans: int = SCANS,
    accumulations: int = 3,
    frames: int = 1,
    repetition_mode: str = "per_repetition",
    keep_raw: bool = True,
    stem: str = "260910_TEST_001",
) -> Method:
    return from_dict({
        "schema_version": 2,
        "metadata": {"name": "test method", "created": dt.date(2026, 9, 10)},
        "acquisition": {
            "frames": frames,
            "scans": scans,
            "accumulations": accumulations,
            "file_stem": stem,
            "repetition_mode": repetition_mode,
            "keep_raw": keep_raw,
        },
        "boxes": [{"name": "mips-a", "port": "COM3", "load": ["STBLDAT;..."],
                   "arm": ["SMOD,TBL"]}],
        "start": [["mips-a", "TBLSTRT"]],
    })


def make_geometry(fake: FakeConsole) -> Geometry:
    return Geometry.from_tof_width(
        fake.tof_width(),
        sample_rate_hz=2e9,
        post_trigger_samples=fake.post_trigger_samples,
    )


def acquire(recording: Recording, console: Console, stream: DataStream, method: Method):
    """Run every frame of a method through the two-phase calls and fold each one."""
    for method_frame in range(1, method.acquisition.frames + 1):
        for repetition in range(1, method.acquisition.console_frames + 1):
            with recording.frame(method_frame, repetition) as request:
                run_frame(console, stream, request, timeout=5.0)
        recording.fold(method_frame)


# --- names ---------------------------------------------------------------------------


def test_the_raw_file_keeps_the_plain_name_and_the_fold_writes_beside_it(tmp_path):
    raw = raw_path(tmp_path, "260910_BK_001")
    assert os.path.basename(raw) == "260910_BK_001.uimf"
    assert os.path.basename(summed_path(raw)) == "260910_BK_001.summed.uimf"


def test_summed_path_refuses_something_that_is_not_a_uimf_path():
    with pytest.raises(ValueError):
        summed_path("260910_BK_001.sqlite")


# --- the axis ------------------------------------------------------------------------


def test_the_axis_comes_from_what_the_console_measured():
    """`Bins` is the console's own `num_samples`, so a stored bin can never run past
    the axis the parameters declare."""
    with FakeConsole() as fake:
        geometry = make_geometry(fake)
    assert geometry.bins == fake.num_samples
    assert geometry.bin_width_ns == pytest.approx(0.5)
    assert geometry.offset_bins == fake.post_trigger_samples
    assert geometry.time_offset_ns == pytest.approx(fake.post_trigger_samples * 0.5)
    assert geometry.average_tof_length_ns == pytest.approx(
        fake.pusher_period_samples * 0.5
    )


def test_a_post_trigger_delay_longer_than_the_record_is_refused():
    with pytest.raises(ValueError, match="past the"):
        Geometry(bins=100, bin_width_ns=0.5, time_offset_ns=0.0,
                 average_tof_length_ns=1.0, offset_bins=200)


# --- the file the console is handed ----------------------------------------------------


def test_the_raw_file_exists_with_its_parameters_before_any_frame(tmp_path):
    """The console opens a file read-write and never creates one, so everything below
    has to be true before the first `acquire frame`."""
    with FakeConsole() as fake:
        geometry = make_geometry(fake)
    method = make_method()
    with Recording.create(tmp_path, method, geometry) as recording:
        path = recording.raw_path
        assert os.path.isfile(path)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
            assert {"Frame_Scans", "Global_Params", "Frame_Param_Keys", "Frame_Params",
                    "V_Frame_Params", "Global_Parameters", "Frame_Parameters"} <= tables
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            conn.close()

    globals_ = UimfFile(path).global_params()
    assert globals_.bins == geometry.bins
    assert globals_.detector_bits == 14, "the SA220P is 14-bit and the file says so"
    assert globals_.written_by, "the writer stamps itself; is_provisional depends on it"
    assert globals_.extra["PrescanTOFPulses"] == str(SCANS)


def test_the_method_is_stamped_into_the_file(tmp_path):
    """A `.uimf` leaves the instrument on its own, so what made it travels inside it."""
    with FakeConsole() as fake:
        geometry = make_geometry(fake)
    method = make_method()
    version = "AqMD3 console 1.2.3 (bushgroup fork)"
    with Recording.create(tmp_path, method, geometry, console_version=version) as recording:
        path = recording.raw_path
    extra = UimfFile(path).global_params().extra
    assert extra["AcquisitionMethod"] == "test method", "PNNL's own key names the method"
    stamped = stamp_globals(method, console_version=version)
    for key, _field in PROVENANCE_KEYS:
        if key in stamped:
            assert extra[key.name] == str(stamped[key]), key.name
    assert "[acquisition]" in extra["ClockworkMethodText"], "the method travels in full"
    assert extra["ClockworkVersion"] == clockwork.__version__


# --- the two phases ----------------------------------------------------------------


def test_a_frame_is_provisional_until_it_is_finalised(tmp_path):
    with FakeConsole() as fake:
        geometry = make_geometry(fake)
    method = make_method(accumulations=1)
    with Recording.create(tmp_path, method, geometry) as recording:
        path = recording.raw_path
        recording.begin_frame(1, 1)
        assert UimfFile(path).is_provisional(1), "no marker yet: the frame may still grow"
        recording.end_frame()
        assert not UimfFile(path).is_provisional(1)

    params = UimfFile(path).frame_params(1)
    assert params.marked_complete
    assert params.method_frame == 1 and params.repetition == 1
    assert params.accumulations == 1, "one push per stored scan in the raw file"
    assert params.scans == SCANS


def test_the_request_names_the_frame_whose_parameters_were_just_written(tmp_path):
    """The pairing that makes the two phases mean anything: the rows the console
    appends land under the parameters `begin_frame` wrote."""
    with FakeConsole() as fake:
        geometry = make_geometry(fake)
    method = make_method(accumulations=2)
    with Recording.create(tmp_path, method, geometry) as recording:
        first = recording.begin_frame(1, 1)
        recording.end_frame()
        second = recording.begin_frame(1, 2)
        recording.end_frame()
    assert (first.frame_number, second.frame_number) == (1, 2)
    assert first.file_name == recording.raw_path
    assert first.offset_bins == geometry.offset_bins
    assert first.frame_length == SCANS
    assert first.nbr_accumulations == 1


def test_a_frame_left_open_blocks_the_next_one(tmp_path):
    with FakeConsole() as fake:
        geometry = make_geometry(fake)
    with Recording.create(tmp_path, make_method(), geometry) as recording:
        recording.begin_frame(1, 1)
        with pytest.raises(ValueError, match="never ended"):
            recording.begin_frame(1, 2)


def test_an_acquisition_that_throws_leaves_its_frame_provisional(tmp_path):
    """What a power cut or a dead console should look like in the file: that frame
    unfinished for ever, every earlier one finished."""
    with FakeConsole() as fake:
        geometry = make_geometry(fake)
    method = make_method(accumulations=2)
    with Recording.create(tmp_path, method, geometry) as recording:
        path = recording.raw_path
        with recording.frame(1, 1):
            pass
        with pytest.raises(RuntimeError):
            with recording.frame(1, 2):
                raise RuntimeError("the console died")

    opened = UimfFile(path)
    assert opened.frame_params(1).marked_complete
    assert not opened.frame_params(2).marked_complete
    assert opened.is_provisional(2)


# --- the fold ------------------------------------------------------------------------


def test_fold_scans_adds_the_blocks_of_one_frame(tmp_path):
    """The `single_frame` half of the fold, on a frame built by hand."""
    from mainspring.uimf import SparseFrame

    frame = SparseFrame.from_scans(
        frame=1, scans=4, bins=64,
        points={
            0: (np.array([5]), np.array([2])),
            1: (np.array([9]), np.array([3])),
            2: (np.array([5]), np.array([10])),
            3: (np.array([9]), np.array([1])),
        },
    )
    folded = fold_scans(frame, 2)
    assert folded.scans == 2 and folded.bins == 64
    bins0, values0 = folded.scan(0)
    bins1, values1 = folded.scan(1)
    assert list(bins0) == [5] and list(values0) == [12]
    assert list(bins1) == [9] and list(values1) == [4]


def test_fold_scans_refuses_a_frame_that_does_not_divide():
    from mainspring.uimf import SparseFrame

    frame = SparseFrame.from_scans(frame=1, scans=5, bins=8, points={})
    with pytest.raises(ValueError, match="does not divide"):
        fold_scans(frame, 2)


@pytest.mark.parametrize("mode", ["per_repetition", "single_frame"])
def test_the_summed_frame_is_exactly_a_times_one_repetition(tmp_path, mode):
    """The bench's acceptance test, run against a stand-in: a fixed spectrum acquired A
    times must sum to A times itself, bin for bin. Both repetition modes reach the same
    answer by different routes, which is the point of running this twice."""
    accumulations = 4
    method = make_method(accumulations=accumulations, repetition_mode=mode)
    with FakeConsole() as fake, DataStream(fake.data_endpoint) as stream, \
            Console(fake.command_endpoint) as console:
        geometry = make_geometry(fake)
        console.configure(offset_v=0.25)
        start_chain(console, stream, settle=1.0, quiet=0.05)
        with Recording.create(tmp_path, method, geometry) as recording:
            raw, summed = recording.raw_path, recording.summed_path
            acquire(recording, console, stream, method)
        console.stop_acquire()

    total = UimfFile(summed).read_frame(1)
    assert total.scans == SCANS

    # One repetition, whichever way the raw file holds them: its own frame in
    # `per_repetition`, and the first block of the only frame in `single_frame`.
    reference = UimfFile(raw).read_frame(1)
    per_repetition = (_first_block(reference, SCANS) if mode == "single_frame"
                      else reference)
    assert len(per_repetition), "the stand-in console wrote nothing to fold"

    for scan in range(SCANS):
        want_bins, want_values = per_repetition.scan(scan)
        got_bins, got_values = total.scan(scan)
        assert list(got_bins) == list(want_bins), f"scan {scan}"
        assert list(got_values) == [v * accumulations for v in want_values], f"scan {scan}"


def _first_block(frame, period: int):
    """The first `period` scans of a frame, as a frame that long."""
    from mainspring.uimf import SparseFrame

    return SparseFrame.from_scans(
        frame=frame.frame, scans=period, bins=frame.bins,
        points={scan: frame.scan(scan) for scan in range(period)},
    )


def test_a_method_frame_is_folded_once(tmp_path):
    """A second fold would put a second summed frame in the companion for the same
    method frame, which is a duplicate rather than a correction."""
    method = make_method(accumulations=1)
    with FakeConsole() as fake, DataStream(fake.data_endpoint) as stream, \
            Console(fake.command_endpoint) as console:
        geometry = make_geometry(fake)
        console.configure(offset_v=0.25)
        start_chain(console, stream, settle=1.0, quiet=0.05)
        with Recording.create(tmp_path, method, geometry) as recording:
            acquire(recording, console, stream, method)
            with pytest.raises(ValueError, match="already been folded"):
                recording.fold(1)
        console.stop_acquire()


def test_the_summed_file_is_todays_shape(tmp_path):
    """One frame per method frame, `Accumulations` = A, `Scans` = the method's scans:
    the shape FALKOR writes and PNNL's tools open."""
    accumulations = 3
    method = make_method(accumulations=accumulations, frames=2)
    with FakeConsole() as fake, DataStream(fake.data_endpoint) as stream, \
            Console(fake.command_endpoint) as console:
        geometry = make_geometry(fake)
        console.configure(offset_v=0.25)
        start_chain(console, stream, settle=1.0, quiet=0.05)
        with Recording.create(tmp_path, method, geometry) as recording:
            summed = recording.summed_path
            acquire(recording, console, stream, method)
            assert recording.frames_of(2) == [4, 5, 6]
        console.stop_acquire()

    opened = UimfFile(summed)
    assert opened.frame_numbers() == [1, 2]
    for number in (1, 2):
        params = opened.frame_params(number)
        assert params.accumulations == accumulations
        assert params.scans == SCANS
        assert params.method_frame == number
        assert params.marked_complete
    assert opened.global_params().extra["AcquisitionMethod"] == "test method"


def test_the_three_grouping_cases_a_viewer_has_to_tell_apart(tmp_path):
    """`repetitions` is what the method asked for and goes on every frame; `repetition`
    is which one this frame is, and only a frame that is one of them carries it. So a
    frame reads as one repetition of a method frame, or as the whole of one, and a
    viewer that has never seen the method can tell which (lab record, task 16)."""
    accumulations = 3
    per_repetition = make_method(accumulations=accumulations, stem="a")
    single = make_method(accumulations=accumulations, repetition_mode="single_frame",
                         stem="b")
    with FakeConsole() as fake, DataStream(fake.data_endpoint) as stream, \
            Console(fake.command_endpoint) as console:
        geometry = make_geometry(fake)
        console.configure(offset_v=0.25)
        start_chain(console, stream, settle=1.0, quiet=0.05)
        paths = {}
        for method in (per_repetition, single):
            with Recording.create(tmp_path, method, geometry) as recording:
                paths[method.acquisition.repetition_mode] = (
                    recording.raw_path, recording.summed_path
                )
                acquire(recording, console, stream, method)
        console.stop_acquire()

    # One repetition of a method frame: both parameters, and the count to check it
    # against, so a method frame missing a repetition is visible without the method.
    raw, summed = paths["per_repetition"]
    frames = [UimfFile(raw).frame_params(n) for n in (1, 2, 3)]
    assert [f.repetition for f in frames] == [1, 2, 3]
    assert {f.method_frame for f in frames} == {1}
    assert {f.repetitions for f in frames} == {accumulations}

    # The whole of a method frame, twice over: the one console frame that held every
    # repetition, and the summed frame the fold made of them.
    whole_raw, whole_summed = paths["single_frame"]
    for path in (whole_raw, whole_summed, summed):
        params = UimfFile(path).frame_params(1)
        assert params.method_frame == 1, path
        assert params.repetition is None, path
        assert params.repetitions == accumulations, path


# --- keep_raw --------------------------------------------------------------------------


def test_keep_raw_false_removes_the_raw_file_once_the_companion_exists(tmp_path):
    method = make_method(accumulations=2, keep_raw=False)
    with FakeConsole() as fake, DataStream(fake.data_endpoint) as stream, \
            Console(fake.command_endpoint) as console:
        geometry = make_geometry(fake)
        console.configure(offset_v=0.25)
        start_chain(console, stream, settle=1.0, quiet=0.05)
        with Recording.create(tmp_path, method, geometry) as recording:
            raw, summed = recording.raw_path, recording.summed_path
            acquire(recording, console, stream, method)
        console.stop_acquire()

    assert not os.path.exists(raw), "keep_raw = false leaves only the companion"
    assert os.path.isfile(summed)


def test_a_run_that_folded_nothing_keeps_its_raw_file_anyway(tmp_path):
    """Discarding is irreversible, so it happens only when there is something to have
    been discarded in favour of (lab record, task 02)."""
    with FakeConsole() as fake:
        geometry = make_geometry(fake)
    method = make_method(accumulations=2, keep_raw=False)
    with Recording.create(tmp_path, method, geometry) as recording:
        raw = recording.raw_path
        recording.begin_frame(1, 1)
        recording.end_frame(complete=False)
    assert os.path.isfile(raw)
