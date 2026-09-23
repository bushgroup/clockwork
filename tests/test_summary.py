"""`clockwork.summary`: a UIMF file read back as numbers.

Three kinds of file. Synthetic ones written here with mainspring's own writer, where every
stored value is chosen and so every number the summary reports can be asserted exactly;
the stand-in acquisition's pair, whose invented spectrum repeats and so sums to a number
known in advance; and the golden CLOCK files, where they are beside this clone, against the
figures the lab record's task 09 comparison printed.
"""

from __future__ import annotations

import json
import os

import numpy as np
import pytest
from mainspring.uimf import Calibration as MzCalibration
from mainspring.uimf import FrameSpec, GlobalSpec, SparseFrame, UimfWriter

import clockwork
from clockwork import summary
from clockwork.acq.uimf import Provenance, Series, stamp_globals
from clockwork.instrument import Calibration, Instrument
from test_acq_loop import BOX, SCANS, Rig, make_boxes, make_method, send_phases
from test_acq_uimf import rendered_fixture

SLOPE = 0.7381
INTERCEPT_US = 0.0769
BIN_WIDTH_NS = 0.5
BINS = 150_000
PERIOD_NS = 129_000.0
AXIS = MzCalibration(slope=SLOPE, intercept=INTERCEPT_US, bin_width_ns=BIN_WIDTH_NS)


def bin_at(mz: float) -> int:
    """The bin whose m/z is nearest `mz` on this file's axis."""
    return int(round(float(AXIS.bin_of(mz))))


def write(path, frames, *, stamped=True, calibrated=True, provenance=None):
    """A file of `frames`, each `(FrameSpec keywords, {scan: [(bin, value), ...]})`.

    `stamped` gives it a hand-written method's clockwork stamp, which is what makes it one
    of clockwork's files rather than a foreign one.
    """
    extra = {}
    if stamped:
        _loaded, rendered = rendered_fixture({"pulse_ms": 3.0})
        extra = stamp_globals(rendered.method, provenance=provenance)
    with UimfWriter(path, GlobalSpec(bins=BINS, bin_width_ns=BIN_WIDTH_NS,
                                     detector_bits=14, extra=extra)) as writer:
        for keywords, points in frames:
            spec = FrameSpec(
                calibration_slope=SLOPE if calibrated else 0.0,
                calibration_intercept=INTERCEPT_US if calibrated else 0.0,
                average_tof_length_ns=PERIOD_NS, **keywords)
            number = writer.add_frame(spec)
            writer.write_sparse_frame(number, SparseFrame.from_scans(
                frame=number, scans=spec.scans, bins=BINS,
                points={scan: (np.asarray([b for b, _ in sorted(row)], dtype=np.int32),
                               np.asarray([v for _, v in sorted(row)], dtype=np.int32))
                        for scan, row in points.items()}))
            writer.finalise_frame(number)
    return str(path)


# The task 09 windows, one point in each interval and one outside them all.
PRECURSOR, WATER = bin_at(531.0), bin_at(522.0)
Y1, Y2, Y3, OUTSIDE = bin_at(808.0), bin_at(887.0), bin_at(905.0), bin_at(600.0)


def clock_spectrum(scale: int = 1) -> dict[int, list[tuple[int, int]]]:
    return {
        10: [(PRECURSOR, 400 * scale), (WATER, 100 * scale)],
        11: [(PRECURSOR, 600 * scale), (Y1, 50 * scale), (OUTSIDE, 999)],
        30: [(Y2, 70 * scale), (Y3, 80 * scale)],
    }


# --- windows ----------------------------------------------------------------------------


def test_the_preset_measures_task_09s_windows_and_quotes_them_back(tmp_path):
    path = write(tmp_path / "run.uimf", [(dict(scans=40), clock_spectrum())])
    result = summary.windowed(path, "bradykinin-clock")
    windows = result["windows"]
    assert windows["precursor"]["intensity"] == 1000
    assert windows["water_loss"]["intensity"] == 100
    assert windows["fragments"]["intensity"] == 200
    assert windows["fragments"]["per_interval"] == [50, 70, 80]
    assert windows["fragments"]["mz"] == [[806.9, 809.1], [885.9, 888.1], [903.9, 906.1]]
    assert result["reference"] == "precursor" and result["preset"] == "bradykinin-clock"
    assert result["ratios"] == {"precursor": 1.0, "water_loss": 0.1, "fragments": 0.2}
    assert result["total"] == 1000 + 100 + 200 + 999
    json.dumps(result)


def test_explicit_windows_are_closed_intervals_and_a_scan_range_is_half_open(tmp_path):
    path = write(tmp_path / "run.uimf", [(dict(scans=40), clock_spectrum())])
    exact = float(AXIS.mz(float(PRECURSOR)))
    result = summary.windowed(path, {"edge": [exact, exact + 1.0], "just_above":
                                     [exact + 1e-9, exact + 1.0]}, reference="edge")
    assert result["windows"]["edge"]["intensity"] == 1000
    assert result["windows"]["just_above"]["intensity"] == 0
    assert result["ratios"]["just_above"] == 0.0

    ranged = summary.windowed(path, "bradykinin-clock", scans=[10, 11])
    assert ranged["windows"]["precursor"]["intensity"] == 400
    assert ranged["scans"] == [10, 11]
    assert summary.windowed(path, {"p": [530.3, 532.4]})["ratios"] is None


def test_an_uncalibrated_file_is_refused_for_windows_and_still_summarised(tmp_path):
    path = write(tmp_path / "run.uimf", [(dict(scans=40), clock_spectrum())],
                 calibrated=False)
    with pytest.raises(summary.SummaryError, match="CalibrationDone 0"):
        summary.windowed(path, "bradykinin-clock")
    with pytest.raises(summary.SummaryError, match="CalibrationDone 0"):
        summary.atd(path, [530.3, 532.4])
    described = summary.summarize(path)
    assert described["calibration"]["done"] is False
    assert described["base_peak"] == {"bin": PRECURSOR, "mz": None, "intensity": 1000}


@pytest.mark.parametrize("call, message", [
    (lambda p: summary.windowed(p, "no-such-preset"), "no preset"),
    (lambda p: summary.windowed(p, {"a": [1.0, 2.0]}, reference="b"), "not one of"),
    (lambda p: summary.windowed(p, {"a": [2.0, 1.0]}), "not an m/z interval"),
    (lambda p: summary.windowed(p, {"a": "530-532"}), "expected"),
    (lambda p: summary.windowed(p, "bradykinin-clock", scans=[5, 5]), "not a range"),
    (lambda p: summary.windowed(p, "bradykinin-clock", frames=[2]), "not in the file"),
    (lambda p: summary.atd(p, "nope", preset="bradykinin-clock"), "has windows"),
    (lambda p: summary.summarize(p, points=0), "at least 1"),
])
def test_a_request_the_file_cannot_answer_is_a_sentence(tmp_path, call, message):
    path = write(tmp_path / "run.uimf", [(dict(scans=40), clock_spectrum())])
    with pytest.raises(summary.SummaryError, match=message):
        call(path)


# --- the arrival-time distribution ----------------------------------------------------------


def test_the_atd_peak_is_the_argmax_and_the_centroid_of_its_half_maximum_run(tmp_path):
    heights = {20: 10, 21: 60, 22: 100, 23: 50, 24: 49, 25: 60, 40: 30}
    path = write(tmp_path / "run.uimf", [(dict(scans=50), {
        scan: [(PRECURSOR, value), (OUTSIDE, 7)] for scan, value in heights.items()})])
    result = summary.atd(path, "precursor", preset="bradykinin-clock", points=5)
    peak = result["peak"]
    assert peak["argmax_scan"] == 22 and peak["height"] == 100
    # 21, 22 and 23 are at or above 50; 24 is not, so the run stops there.
    assert peak["half_max_scans"] == [21, 23]
    assert peak["centroid_scan"] == pytest.approx((21 * 60 + 22 * 100 + 23 * 50) / 210)
    assert peak["argmax_ms"] == pytest.approx(22 * PERIOD_NS * 1e-6)
    assert result["mz"] == [[530.3, 532.4]] and result["window"] == "precursor"
    assert result["total"] == sum(heights.values())
    assert result["profile"]["scans_per_point"] == 10
    assert result["profile"]["values"] == [0, 0, 329, 0, 30]
    json.dumps(result)


def test_an_empty_window_has_no_peak(tmp_path):
    path = write(tmp_path / "run.uimf", [(dict(scans=40), clock_spectrum())])
    result = summary.atd(path, [700.0, 701.0])
    assert result["peak"] is None and result["total"] == 0 and result["mean_ms"] is None


# --- which file, and what saturation means in it --------------------------------------------


def test_saturation_is_exact_in_a_raw_file_and_a_bound_in_its_companion(tmp_path):
    clipped = {3: [(100, summary.STORED_CEILING), (104, 20)],
               7: [(100, summary.STORED_CEILING), (200, summary.STORED_CEILING)],
               8: [(300, 5)]}
    raw = write(tmp_path / "run.uimf", [(dict(scans=10), clipped)])
    summed = write(tmp_path / "run.summed.uimf", [(dict(scans=10, accumulations=4), {
        1: [(100, summary.STORED_CEILING + 10)], 2: [(100, 30000)]})])

    raw_result = summary.summarize(raw)
    assert raw_result["file"] == "raw" and raw_result["companion"] == os.path.abspath(summed)
    assert raw_result["saturation"] | {"definition": None} == {
        "bound": "exact", "ceiling": 65535, "points_at_ceiling": 3, "fraction": 3 / 5,
        "pushes": 10, "pushes_at_ceiling": 2, "definition": None}

    summed_result = summary.summarize(summed)
    assert summed_result["file"] == "summed"
    assert summed_result["companion"] == os.path.abspath(raw)
    assert summed_result["saturation"]["bound"] == "upper"
    assert summed_result["saturation"]["fraction"] == 0.5

    foreign = summary.summarize(write(tmp_path / "falkor.uimf",
                                      [(dict(scans=10), clipped)], stamped=False))
    assert foreign["file"] == "foreign" and foreign["saturation"]["bound"] is None
    assert foreign["clockwork"] == {} and foreign["template"] is None


def test_a_single_frame_raw_file_is_folded_onto_the_methods_scans(tmp_path):
    """Three repetitions of a 40-scan table end to end in one frame, as the console writes a
    `single_frame` method frame, against the companion the fold would write."""
    one = clock_spectrum()
    blocks = {block * 40 + scan: row for block in range(3) for scan, row in one.items()}
    raw = write(tmp_path / "run.uimf", [(dict(scans=120, method_frame=1, repetitions=3),
                                         blocks)])
    summed = write(tmp_path / "run.summed.uimf", [(
        dict(scans=40, accumulations=3, method_frame=1, repetitions=3),
        {scan: [(b, 3 * v) for b, v in row] for scan, row in one.items()})])
    for question in (
        lambda p: summary.atd(p, "precursor", preset="bradykinin-clock")["profile"],
        lambda p: summary.windowed(p, "bradykinin-clock", scans=[11, 40])["windows"],
        lambda p: summary.summarize(p)["tic_profile"],
    ):
        assert question(raw) == question(summed)
    assert summary.summarize(raw)["fold_period"] == 40
    assert summary.summarize(summed)["fold_period"] is None


def test_frame_totals_and_profiles_are_summed_in_blocks_and_lose_nothing(tmp_path):
    frames = [(dict(scans=40, method_frame=1, repetition=r, repetitions=7),
               {scan: [(PRECURSOR, r * 10 + scan)] for scan in range(40)})
              for r in range(1, 8)]
    result = summary.summarize(write(tmp_path / "run.uimf", frames), points=3)
    assert result["frames"] | {"provisional": 0} == {
        "count": 7, "first": 1, "last": 7, "provisional": 0, "method_frames": 1}
    assert result["frame_tic"]["frames_per_point"] == 3
    per_frame = [sum(r * 10 + scan for scan in range(40)) for r in range(1, 8)]
    assert result["frame_tic"]["values"] == [sum(per_frame[0:3]), sum(per_frame[3:6]),
                                             per_frame[6]]
    assert sum(result["tic_profile"]["values"]) == result["total_counts"] == sum(per_frame)
    assert result["base_peak"]["bin"] == PRECURSOR
    assert result["base_peak"]["mz"] == pytest.approx(531.0, abs=0.01)
    assert result["pusher_period_ns"] == PERIOD_NS
    assert summary.summarize(write(tmp_path / "two.uimf", frames),
                             frames=[2, 5])["frames"]["count"] == 2


# --- provenance -----------------------------------------------------------------------------


def test_every_clockwork_parameter_is_read_back_typed_with_the_template_grouped(tmp_path):
    loaded, rendered = rendered_fixture({"pulse_ms": 3.0})
    provenance = Provenance(rendered=rendered,
                            series=Series("req-0923-a", index=4, position=2, seed=12345))
    path = write(tmp_path / "run.uimf", [(dict(scans=40), clock_spectrum())],
                 provenance=provenance)
    result = summary.summarize(path)
    stamped = result["clockwork"]
    assert stamped["ClockworkKnobPulseMs"] == 3.0
    assert stamped["ClockworkMarkOffScan"] == 40 and isinstance(
        stamped["ClockworkMarkOffScan"], int)
    assert stamped["ClockworkMarkOffMs"] == 4.0
    assert stamped["ClockworkLabelSample"] == "polyalanine"
    assert stamped["ClockworkSeriesIndex"] == 4 and stamped["ClockworkSeriesSeed"] == 12345
    assert stamped["ClockworkSeriesId"] == "req-0923-a"
    assert stamped["ClockworkTickUs"] == 100.0
    assert stamped["ClockworkTemplateHash"] == loaded.hash
    assert result["template"] == {
        "hash": loaded.hash, "tick_us": 100.0,
        "knobs": {"PulseMs": 3.0, "Cycles": 1.0, "WaitMs": 10.0},
        "labels": {"Sample": "polyalanine"}, "marks": {"Off": {"ms": 4.0, "scan": 40}}}
    assert result["method"] == rendered.method.metadata.name

    long_text = stamped["ClockworkTemplateText"]
    assert isinstance(long_text, dict) and long_text["chars"] > summary.TEXT_LIMIT
    whole = summary.summarize(path, texts=True)["clockwork"]["ClockworkTemplateText"]
    assert isinstance(whole, str) and len(whole) == long_text["chars"]
    assert len(json.dumps(result)) < 8000, "a summary fits in a tool result"


# --- the stand-in acquisition, end to end ---------------------------------------------------


CALIBRATED = Instrument(calibration=Calibration(slope=SLOPE, intercept=INTERCEPT_US))


@pytest.mark.parametrize("mode, frames", [("per_repetition", 2), ("single_frame", 1)])
def test_the_stand_ins_pair_sums_to_its_invented_spectrum(tmp_path, mode, frames):
    """The fake's per-push spectrum repeats every sixteen scans, so a run of `frames`
    method frames of A repetitions holds exactly frames x A copies of one repetition,
    in both files, and every number the summary gives is that."""
    method = make_method(repetition_mode=mode, frames=frames)
    accumulations = method.acquisition.accumulations
    boxes = make_boxes(BOX)
    with Rig(tmp_path) as rig:
        send_phases(method, boxes)
        run = rig.acquire(method, boxes, instrument=CALIBRATED)
        spectrum = [rig.fake._scan_spectrum(scan) for scan in range(SCANS)]
    assert run.complete
    copies = frames * accumulations
    per_scan = [sum(values) for _bins, values in spectrum]
    raw, summed = summary.summarize(run.raw_path), summary.summarize(run.summed_path)
    assert (raw["file"], summed["file"]) == ("raw", "summed")
    assert raw["companion"] == os.path.abspath(run.summed_path)
    for result in (raw, summed):
        assert result["total_counts"] == copies * sum(per_scan)
        assert result["tic_profile"]["values"] == [copies * v for v in per_scan]
        assert result["saturation"]["points_at_ceiling"] == 0
        assert result["calibration"]["done"] and result["calibration"]["same_on_every_frame"]
    assert raw["fold_period"] == (SCANS if mode == "single_frame" else None)
    assert summed["frames"]["count"] == frames
    assert raw["frames"]["count"] == (frames * accumulations if mode == "per_repetition"
                                      else frames)

    # One window around the bin a push's tallest point sits in at phase 3, which comes round
    # once in every sixteen scans; the ATD's argmax is the first of those.
    bins, values = spectrum[3]
    lo, hi = (float(AXIS.mz(bins[1] - 0.5)), float(AXIS.mz(bins[1] + 0.5)))
    for path in (run.raw_path, run.summed_path):
        window = summary.windowed(path, {"tall": [lo, hi]})["windows"]["tall"]
        assert window["intensity"] == copies * values[1] * (SCANS // 16)
        peak = summary.atd(path, [lo, hi])["peak"]
        assert peak["argmax_scan"] == 3 and peak["height"] == copies * values[1]


# --- the golden CLOCK files, where they are beside this clone -------------------------------


GOLDEN_RATIOS = {
    # file: (water loss / precursor, fragments / precursor), as task 09's comparison
    # printed them to three places (lab record, task 09).
    "260825_BK_025.uimf": (0.504, 1.182),
    "260825_BK_037.uimf": (0.557, 1.574),
    "260825_BK_057.uimf": (0.586, 1.773),
}


@pytest.mark.parametrize("name", sorted(GOLDEN_RATIOS))
def test_the_golden_clock_files_reproduce_task_09s_ratios(name):
    directory = clockwork.lab_dir("golden")
    if directory is None:
        pytest.skip("the golden experiments are lab material and this is a public clone")
    path = os.path.join(directory, "bradykinin-clock", name)
    if not os.path.isfile(path):
        pytest.skip(f"{name} is inventoried by checksum and restored by hand; not here")
    result = summary.windowed(path, "bradykinin-clock")
    ratios = result["ratios"]
    assert (round(ratios["water_loss"], 3), round(ratios["fragments"], 3)) == \
        GOLDEN_RATIOS[name]
    assert result["file"] == "foreign"
    assert summary.summarize(path)["saturation"]["bound"] is None
