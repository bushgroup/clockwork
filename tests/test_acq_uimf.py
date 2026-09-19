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
from mainspring.interface import read_live_pointer, write_live_pointer
from mainspring.uimf import UimfFile

import clockwork
from clockwork.acq import (
    SUMMED_SUFFIX,
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
        "boxes": [{"name": "box1", "port": "COM3", "load": ["STBLDAT;..."],
                   "arm": ["SMOD,TBL"]}],
        "start": [["box1", "TBLSTRT"]],
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


def test_summed_suffix_is_mainspring_s_own():
    from mainspring.uimf import SUMMED_SUFFIX as mainspring_summed_suffix

    assert SUMMED_SUFFIX is mainspring_summed_suffix


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
    assert recording.discard_error is None, (
        "nothing was attempted, so there is no failure to report: the file was kept on "
        "purpose"
    )


def test_a_successful_discard_reports_no_error(tmp_path):
    method = make_method(accumulations=2, keep_raw=False)
    with FakeConsole() as fake, DataStream(fake.data_endpoint) as stream, \
            Console(fake.command_endpoint) as console:
        geometry = make_geometry(fake)
        console.configure(offset_v=0.25)
        start_chain(console, stream, settle=1.0, quiet=0.05)
        with Recording.create(tmp_path, method, geometry) as recording:
            raw = recording.raw_path
            acquire(recording, console, stream, method)
        console.stop_acquire()
    assert not os.path.exists(raw)
    assert recording.discard_error is None


def test_a_discard_the_platform_refuses_is_recorded_and_does_not_raise(
        tmp_path, monkeypatch):
    """The failure this task exists for, forced rather than provoked.

    On Windows a viewer holding the raw file open makes `os.remove` raise
    `PermissionError`, which is an `OSError` and used to be suppressed whole: the file
    stayed, nothing was raised and nothing was said. Forced here with a stand-in
    `os.remove` so that the recording's half is the same assertion on every platform;
    the real thing, a file genuinely held open, is the Windows-only test below and the
    self-check, both of which can only run where the platform refuses.
    """
    method = make_method(accumulations=2, keep_raw=False)
    attempts = []
    real_remove = os.remove

    def refuse(path, *args, **kwargs):
        if str(path).endswith(".uimf"):
            attempts.append(str(path))
            raise PermissionError(13, "the process cannot access the file")
        return real_remove(path, *args, **kwargs)

    with FakeConsole() as fake, DataStream(fake.data_endpoint) as stream, \
            Console(fake.command_endpoint) as console:
        geometry = make_geometry(fake)
        console.configure(offset_v=0.25)
        start_chain(console, stream, settle=1.0, quiet=0.05)
        with Recording.create(tmp_path, method, geometry) as recording:
            raw = recording.raw_path
            acquire(recording, console, stream, method)
            monkeypatch.setattr("clockwork.acq.uimf.os.remove", refuse)
        console.stop_acquire()

    assert os.path.isfile(raw), "the removal was refused, so the file is still here"
    assert len(attempts) > 1, "a removal that failed is retried, not attempted once"
    assert recording.discard_error is not None
    assert "PermissionError" in recording.discard_error


def test_a_discard_retried_into_success_reports_nothing(tmp_path, monkeypatch):
    """The reader lets go, which is the normal case and why the retry is there at all
    (lab record, task 57: measured at one and two attempts against a real viewer)."""
    method = make_method(accumulations=2, keep_raw=False)
    real_remove = os.remove
    still_to_refuse = [1]

    def relent(path, *args, **kwargs):
        if str(path).endswith(".uimf") and still_to_refuse:
            still_to_refuse.pop()
            raise PermissionError(13, "the process cannot access the file")
        return real_remove(path, *args, **kwargs)

    with FakeConsole() as fake, DataStream(fake.data_endpoint) as stream, \
            Console(fake.command_endpoint) as console:
        geometry = make_geometry(fake)
        console.configure(offset_v=0.25)
        start_chain(console, stream, settle=1.0, quiet=0.05)
        with Recording.create(tmp_path, method, geometry) as recording:
            raw = recording.raw_path
            acquire(recording, console, stream, method)
            monkeypatch.setattr("clockwork.acq.uimf.os.remove", relent)
        console.stop_acquire()

    assert not os.path.exists(raw)
    assert recording.discard_error is None


@pytest.mark.skipif(os.name != "nt",
                    reason="only Windows refuses to delete a file that is open")
def test_a_reader_holding_the_raw_file_open_is_the_real_failure(tmp_path):
    """The same thing again without a stand-in, which is the scene the task describes:
    a viewer reading the run holds the file, and the close cannot take it away."""
    method = make_method(accumulations=2, keep_raw=False)
    with FakeConsole() as fake, DataStream(fake.data_endpoint) as stream, \
            Console(fake.command_endpoint) as console:
        geometry = make_geometry(fake)
        console.configure(offset_v=0.25)
        start_chain(console, stream, settle=1.0, quiet=0.05)
        recording = Recording.create(tmp_path, method, geometry)
        raw = recording.raw_path
        acquire(recording, console, stream, method)
        with open(raw, "rb"):
            recording.close()
        console.stop_acquire()

    assert os.path.isfile(raw)
    assert recording.discard_error is not None
    assert os.path.isfile(recording.summed_path), "the companion is complete regardless"


# --- the run in progress (lab record, task 58) -----------------------------------------


def test_a_recording_is_not_published_unless_it_is_asked_to_be(tmp_path):
    """The pointer is a claim that somebody is running the instrument now, and most of
    the ways a `Recording` gets made are not that: a bench script re-folding an old file,
    a test, `clockwork --self-check`. Off unless a caller says otherwise."""
    with FakeConsole() as fake:
        geometry = make_geometry(fake)
    with Recording.create(tmp_path, make_method(), geometry) as recording:
        assert recording.live_pointer is None
        assert read_live_pointer() is None


def test_a_published_recording_names_its_raw_file_and_gives_it_up_at_the_close(tmp_path):
    """The raw file and not the companion: it is the one that exists first and grows
    during the run, so it is the one a viewer can follow. The companion does not exist
    yet at this point, which is the other half of the same fact."""
    with FakeConsole() as fake:
        geometry = make_geometry(fake)
    recording = Recording.create(tmp_path, make_method(), geometry, publish=True)
    named = read_live_pointer()
    assert named is not None
    assert named.path == recording.raw_path == os.path.abspath(recording.raw_path)
    assert named.writer == "clockwork"
    assert not os.path.exists(recording.summed_path)
    recording.close()
    assert read_live_pointer() is None
    assert recording.live_pointer is None


def test_closing_twice_is_still_idempotent_with_a_pointer_to_withdraw(tmp_path):
    with FakeConsole() as fake:
        geometry = make_geometry(fake)
    recording = Recording.create(tmp_path, make_method(), geometry, publish=True)
    recording.close()
    write_live_pointer(str(tmp_path / "somebody-elses-run.uimf"), writer="another")
    recording.close()
    named = read_live_pointer()
    assert named is not None and named.writer == "another", (
        "a second close must not take away a pointer this recording did not write"
    )


def test_the_pointer_is_withdrawn_before_keep_raw_removes_the_file(tmp_path, monkeypatch):
    """The ordering is the whole of this. A pointer still standing while the file it
    names is deleted is an instruction to follow a file this process is in the act of
    taking away, and on Windows the reader it sent there is what makes the delete fail.

    Every removal is recorded, not only the raw file's: `clockwork.acq.uimf` and
    `mainspring.interface` hold the same `os` module, so patching one patches both, and
    the withdrawal of the pointer is itself one of the calls seen here. That makes the
    sequence the assertion -- the pointer goes, and only then does the file.
    """
    method = make_method(accumulations=2, keep_raw=False)
    removals: list[tuple[str, object]] = []
    real_remove = os.remove

    def remove(path, *args, **kwargs):
        removals.append((str(path), read_live_pointer()))
        return real_remove(path, *args, **kwargs)

    with FakeConsole() as fake, DataStream(fake.data_endpoint) as stream, \
            Console(fake.command_endpoint) as console:
        geometry = make_geometry(fake)
        console.configure(offset_v=0.25)
        start_chain(console, stream, settle=1.0, quiet=0.05)
        with Recording.create(tmp_path, method, geometry, publish=True) as recording:
            raw = recording.raw_path
            acquire(recording, console, stream, method)
            assert read_live_pointer() is not None
            monkeypatch.setattr("clockwork.acq.uimf.os.remove", remove)
        console.stop_acquire()

    assert not os.path.exists(raw), "keep_raw = false leaves only the companion"
    assert [path for path, _ in removals][-1] == raw
    assert dict(removals)[raw] is None, (
        "the pointer was still standing when the file it named was deleted"
    )


def test_a_close_does_not_withdraw_a_pointer_that_names_a_later_run(tmp_path):
    """Every writer publishes one pointer, so `self._pointer is not None` is not enough
    to say the pointer standing there is this recording's (lab record, task 57).

    Unreachable while acquisitions are sequential on one worker thread, which they are;
    checked because the guard is two lines and the failure it prevents -- a finished run
    taking the live run's pointer away -- is invisible from inside clockwork.
    """
    with FakeConsole() as fake:
        geometry = make_geometry(fake)
    first = Recording.create(tmp_path, make_method(), geometry, stem="first",
                             publish=True)
    second = Recording.create(tmp_path, make_method(), geometry, stem="second",
                              publish=True)
    first.close()
    named = read_live_pointer()
    assert named is not None and named.path == second.raw_path, (
        "the first run's close took away the second run's pointer"
    )
    second.close()
    assert read_live_pointer() is None
