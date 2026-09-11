"""One whole acquisition, driven end to end against the stand-ins.

Every test here runs the real loop against `FakeBox` and `FakeConsole`, so what is being
exercised is the order the boxes, the console and the two files happen in rather than any
one of them. Three things the stand-ins cannot model are worth knowing before reading an
assertion about them:

* **The console has no clock.** A frame's batches are published from inside the handler
  for `acquire frame`, so without `frame_hold_s` they race the start list the loop walks
  to release that frame. Every test that is not about the gate guard sets a hold, and the
  one that is about it sets none and makes the start list slow instead.
* **Its writer never lags its publisher.** Rows go into the file before each batch goes
  on the wire, so `rows_at_finished` and `rows_after_silence` are always equal here. What
  can still be asserted is that the loop waited for the silence before it wrote the
  completion marker, which is the ordering the lag makes necessary.
* **A box with no ARB modules NAKs the whole ARB command set**, with the firmware's own
  code for it, which is what the bench box did. The golden methods are written for boxes
  that have modules, so their fixtures are given some; what the stand-in then does with a
  section 6 command is store it and acknowledge it, which says the string was well formed
  and went out and nothing about what a module would have done with it.

The scans and repetitions below are tiny. The fake encodes every batch through protobuf
and Snappy in this process, and none of the properties under test depend on the size of a
frame; the one number that matters is that `SCANS` is a multiple of the fake's 16-scan
spectrum period, so a fold can be asserted as an equality.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import time

import pytest
from mainspring.uimf import UimfFile

import clockwork
from clockwork import acq
from clockwork import method as method_module
from clockwork.acq import (
    AcquisitionRefused,
    BatchSeen,
    BoxReady,
    Console,
    DataStream,
    FakeConsole,
    Folded,
    FrameBegun,
    FrameEnded,
    PhaseSent,
    RunBegun,
    Warned,
    refusals,
    run_acquisition,
    send_phases,
)
from clockwork.mips import Box, BoxRejected, FakeBox

SCANS = 32
"""Two of the fake's 16-scan spectrum periods, so a fold has something to add."""

ACCUMULATIONS = 3
SILENCE = 0.3
"""Long enough to be a silence against the fake, short enough to run a test suite in.

The instrument's number is `acq.SILENCE_S`, six seconds, and it is a measurement of the
console's publisher rather than a preference.
"""

HOLD = 0.05
"""What `FakeConsole.frame_hold_s` stands in for: the pushes a real frame spends waiting
for its enable to go high while the loop walks the start list."""

BOX = "box1"
"""The one box most of these tests need. A box name is a free-form string a method
supplies, so the suite invents its own rather than naming an instrument's."""

ARB_MODULES = 4
"""Enough modules for the golden methods' `SWFREQ,4,...` and `SARBCCLK,4,...`."""


# --- fixtures --------------------------------------------------------------------------


def make_method(
    *,
    frames: int = 1,
    scans: int = SCANS,
    accumulations: int = ACCUMULATIONS,
    repetition_mode: str = "per_repetition",
    keep_raw: bool = True,
    stem: str = "260911_TEST_001",
    reset: list[list[str]] | None = None,
) -> method_module.Method:
    return method_module.from_dict({
        "schema_version": 2,
        "metadata": {"name": "loop test", "created": dt.date(2026, 9, 11)},
        "acquisition": {
            "frames": frames,
            "scans": scans,
            "accumulations": accumulations,
            "file_stem": stem,
            "repetition_mode": repetition_mode,
            "keep_raw": keep_raw,
        },
        "boxes": [{
            "name": BOX,
            "port": "COM3",
            "setup": ["STBLCLK,EXT", "STBLTRG,POS"],
            "load": [f"STBLDAT;0:[A:1,0:B:1,500:B:0,{scans + 1}:A:0,{scans + 2}:];"],
            "arm": ["SMOD,TBL"],
        }],
        "start": [[BOX, "TBLSTRT"]],
        "reset": reset if reset is not None else [[BOX, "SMOD,LOC"],
                                                  [BOX, "SMOD,TBL"]],
    })


def golden(name: str, *, scans: int = SCANS, accumulations: int = ACCUMULATIONS):
    """One of the two golden methods, shrunk to a size a test can acquire.

    Every string, box name and sequence is the trainee's; only the frame arithmetic is
    reduced, because a real one is 20000 scans in one case and half a million in the
    other. Skips where the lab record is not beside this clone, which is every public one.
    """
    directory = clockwork.lab_dir("golden")
    if directory is None:
        pytest.skip("the golden experiments are lab material and this is a public clone")
    path = os.path.join(directory, name, "method.toml")
    if not os.path.isfile(path):
        pytest.skip(f"no golden method at {name}")
    loaded = method_module.load(path)
    return dataclasses.replace(
        loaded,
        acquisition=dataclasses.replace(
            loaded.acquisition, scans=scans, accumulations=accumulations,
        ),
    )


def make_boxes(*names: str, arb_modules: int = 0, transport=None) -> dict[str, Box]:
    return {
        name: Box(transport=transport() if transport
                  else FakeBox(arb_modules=arb_modules), name=name)
        for name in names
    }


def boxes_for(loaded, **kwargs) -> dict[str, Box]:
    """A stand-in box per box the method names.

    The roster comes off the method rather than out of this file, so a golden method
    that renames its boxes needs no edit here."""
    return make_boxes(*[box.name for box in loaded.boxes], **kwargs)


class SlowBox(FakeBox):
    """A box whose every write costs what a USB round trip costs.

    The start list is three serial round trips on the instrument, ~16 ms each, and the
    gate guard exists because a digitizer that was already recording publishes a batch
    inside that window. Against a stand-in that answers instantly there is no window at
    all, so this puts one back; nothing else in the suite needs it.
    """

    delay = 0.25

    def write(self, data: bytes) -> None:
        time.sleep(self.delay)
        super().write(data)


class Rig:
    """A console, a stream and a folder, set up the way a caller has to set them up."""

    def __init__(self, directory, **console_kwargs):
        self.directory = str(directory)
        self.fake = FakeConsole(**console_kwargs).start()
        self.fake.frame_hold_s = HOLD
        self.stream = DataStream(self.fake.data_endpoint)
        self.console = Console(self.fake.command_endpoint, timeout=10.0)
        self.console.configure(offset_v=0.251)

    def acquire(self, method, boxes, **kwargs):
        kwargs.setdefault("silence", SILENCE)
        kwargs.setdefault("empty_settle", 0.3)
        kwargs.setdefault("post_trigger_samples", self.fake.post_trigger_samples)
        kwargs.setdefault("directory", self.directory)
        return run_acquisition(method, boxes=boxes, console=self.console,
                               stream=self.stream, **kwargs)

    def close(self):
        self.console.close()
        self.stream.close()
        self.fake.stop()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def rig(tmp_path):
    with Rig(tmp_path) as running:
        yield running


# --- what a method has to say for itself ----------------------------------------------


def test_single_frame_with_more_than_one_frame_is_refused_before_anything_is_sent(rig):
    """The table that loops on the box raises the digitizer's enable once and never
    lowers it, so a second method frame would begin recording before its start list
    ran and be offset by the serial latency (lab record, task 05)."""
    method = make_method(repetition_mode="single_frame", frames=2)
    assert len(refusals(method)) == 1
    boxes = make_boxes(BOX)
    with pytest.raises(AcquisitionRefused, match="never lowers"):
        rig.acquire(method, boxes)
    assert not os.listdir(rig.directory)
    assert rig.fake.frames == []


def test_one_single_frame_and_every_per_repetition_method_are_acquirable():
    assert refusals(make_method(repetition_mode="single_frame", frames=1)) == []
    assert refusals(make_method(repetition_mode="per_repetition", frames=5)) == []


# --- the phases ------------------------------------------------------------------------


def test_send_phases_sends_setup_load_and_arm_in_the_order_written():
    method = make_method()
    boxes = make_boxes(BOX)
    seen: list[acq.Event] = []
    send_phases(method, boxes, progress=seen.append)
    assert isinstance(seen[0], BoxReady)
    sent = [(event.phase, event.command) for event in seen if isinstance(event, PhaseSent)]
    assert [phase for phase, _ in sent] == ["setup", "setup", "load", "arm"]
    assert sent[-1] == ("arm", "SMOD,TBL")
    assert "TBLRDY" in [event.detail for event in seen if isinstance(event, PhaseSent)][-1]


def test_setup_can_be_left_out_for_a_box_that_has_had_it_since_power_up():
    boxes = make_boxes(BOX)
    seen: list[acq.Event] = []
    send_phases(make_method(), boxes, setup=False, progress=seen.append)
    phases = [event.phase for event in seen if isinstance(event, PhaseSent)]
    assert phases == ["load", "arm"]


def test_a_table_is_streamed_in_paced_chunks_rather_than_written_in_one_go():
    """The box has no flow control and drops what overruns its 4 KB input buffer without
    saying so, so a long table written in one call loses its tail."""
    transport = FakeBox()
    box = Box(transport=transport, name=BOX)
    events = ",".join(f"{tick}:A:1" for tick in range(100, 4000, 2))
    method = method_module.from_dict({
        "schema_version": 2,
        "metadata": {"name": "long table", "created": dt.date(2026, 9, 11)},
        "acquisition": {"frames": 1, "scans": SCANS, "accumulations": 1,
                        "file_stem": "long"},
        "boxes": [{"name": BOX, "port": "COM3",
                   "load": [f"STBLDAT;0:[A:1,{events},8000:];"], "arm": ["SMOD,TBL"]}],
        "start": [[BOX, "TBLSTRT"]],
    })
    send_phases(method, {BOX: box})
    assert transport.dropped_bytes == 0
    assert len(transport.written) > 1


def test_a_box_that_refuses_a_string_stops_the_send_and_says_which():
    boxes = make_boxes(BOX)
    method = make_method()
    method = dataclasses.replace(method, boxes=(
        dataclasses.replace(method.boxes[0], setup=("NOSUCHCMD",)),
    ))
    seen: list[acq.Event] = []
    with pytest.raises(BoxRejected):
        send_phases(method, boxes, progress=seen.append)
    refused = [event for event in seen if isinstance(event, PhaseSent) and event.error]
    assert len(refused) == 1 and refused[0].command == "NOSUCHCMD"


def test_a_method_naming_a_box_with_no_open_port_is_an_error_not_a_skip():
    with pytest.raises(KeyError, match="no open port"):
        send_phases(make_method(), {})


def test_a_methods_own_warnings_are_passed_straight_through():
    method = method_module.loads(
        method_module.dumps(make_method()).replace('"SMOD,TBL"', '"SMOD,TBL\\t"')
    )
    assert method.warnings
    seen: list[acq.Event] = []
    send_phases(method, make_boxes(BOX), progress=seen.append)
    assert [event.message for event in seen if isinstance(event, Warned)] \
        == list(method.warnings)


# --- one acquisition -------------------------------------------------------------------


def test_a_whole_per_repetition_acquisition_writes_both_files(rig):
    method = make_method()
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    run = rig.acquire(method, boxes)

    assert run.complete
    assert len(run.frames) == ACCUMULATIONS and not run.failures
    assert [record.repetition for record in run.frames] == [1, 2, 3]
    assert run.scans_published == SCANS * ACCUMULATIONS
    assert len(run.folds) == 1 and run.folds[0].rows > 0

    raw, summed = UimfFile(run.raw_path), UimfFile(run.summed_path)
    assert raw.frame_numbers() == [1, 2, 3]
    assert summed.frame_numbers() == [1]
    assert all(raw.frame_params(n).marked_complete for n in (1, 2, 3))
    one, total = raw.read_frame(1), summed.read_frame(1)
    assert len(one) > 0
    assert all(
        list(total.scan(n)[1]) == [v * ACCUMULATIONS for v in one.scan(n)[1]]
        for n in range(SCANS)
    )


def test_a_single_frame_acquisition_folds_the_blocks_inside_its_one_frame(rig):
    method = make_method(repetition_mode="single_frame")
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    run = rig.acquire(method, boxes)

    assert run.complete
    assert len(run.frames) == 1
    assert run.frames[0].scans_published == SCANS * ACCUMULATIONS
    summed = UimfFile(run.summed_path)
    assert summed.frame_params(1).scans == SCANS
    assert summed.frame_params(1).accumulations == ACCUMULATIONS


def test_the_console_is_asked_for_a_frame_before_the_start_list_is_walked(rig, tmp_path):
    """The enable is a level, so a frame released before it was asked for begins on
    whatever push follows the request (lab record, task 05). This is the one ordering
    that cannot be recovered from afterwards, so it is asserted directly."""
    log: list[tuple[str, str]] = []

    class LoggingBox(FakeBox):
        def write(self, data: bytes) -> None:
            text = data.decode("ascii", "replace").strip()
            if text:
                log.append(("box", text))
            super().write(data)

    class LoggingConsole(Console):
        def acquire_frame(self, request):
            log.append(("console", "acquire frame"))
            super().acquire_frame(request)

    method = make_method()
    boxes = {BOX: Box(transport=LoggingBox(), name=BOX)}
    send_phases(method, boxes)
    console = LoggingConsole(rig.fake.command_endpoint, timeout=10.0)
    console.configure(offset_v=0.251)
    log.clear()
    try:
        run = run_acquisition(method, boxes=boxes, console=console, stream=rig.stream,
                              directory=str(tmp_path), silence=SILENCE,
                              post_trigger_samples=rig.fake.post_trigger_samples)
    finally:
        console.close()
    assert run.complete
    pairs = [entry for entry in log
             if entry == ("console", "acquire frame") or entry[1] == "TBLSTRT"]
    assert pairs == [("console", "acquire frame"), ("box", "TBLSTRT")] * ACCUMULATIONS


def test_a_frame_is_over_when_the_stream_falls_silent_not_when_it_says_finished(rig):
    """The completion marker is the only thing in the file that tells a frame that
    finished from one that was cut off, and the console is still inserting rows for a
    frame whose `finished` has gone out (lab record, task 20)."""
    method = make_method(accumulations=1)
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    run = rig.acquire(method, boxes)
    record = run.frames[0]
    assert record.silence_seconds >= SILENCE
    assert record.rows_at_finished is not None
    assert record.rows_after_silence is not None
    # The stand-in writes each batch's rows before publishing it, so it has no lag to
    # find; what is asserted is that both counts were taken and the second came later.
    assert record.writer_lag_rows == 0


def test_the_progress_stream_reports_every_stage_in_order(rig):
    method = make_method()
    boxes = make_boxes(BOX)
    seen: list[acq.Event] = []
    send_phases(method, boxes)
    rig.acquire(method, boxes, progress=seen.append)
    kinds = [type(event) for event in seen]
    assert kinds[0] is RunBegun
    assert kinds.count(FrameBegun) == ACCUMULATIONS
    assert kinds.count(FrameEnded) == ACCUMULATIONS
    assert kinds.count(Folded) == 1
    assert BatchSeen in kinds
    assert all(isinstance(event.text, str) and event.text for event in seen)


# --- a replicate -----------------------------------------------------------------------


def test_a_replicate_walks_the_reset_list_and_re_sends_no_table(rig):
    method = make_method()
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    first = rig.acquire(method, boxes)

    seen: list[acq.Event] = []
    second = rig.acquire(method, boxes, stem="260911_TEST_002", replicate=True,
                         progress=seen.append)
    assert second.complete and second.replicate
    assert os.path.basename(second.raw_path) == "260911_TEST_002.uimf"
    assert os.path.isfile(first.raw_path)

    phases = [(event.phase, event.command) for event in seen
              if isinstance(event, PhaseSent)]
    assert phases[:2] == [("reset", "SMOD,LOC"), ("reset", "SMOD,TBL")]
    assert not any(command.startswith("STBLDAT") for _, command in phases)
    assert not any(phase == "setup" for phase, _ in phases)


def test_a_replicate_that_reuses_a_stem_collides_rather_than_overwriting(rig):
    method = make_method()
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    rig.acquire(method, boxes)
    with pytest.raises(FileExistsError):
        rig.acquire(method, boxes, replicate=True)


# --- the re-arm fallback -----------------------------------------------------------------


def software_triggered(**kwargs):
    """A method whose box is configured the way the instrument's sequencer is.

    `STBLTRG,SW` rather than the suite's usual `POS`, which is the whole of the
    difference: `FakeBox` re-arms a table after an *external* trigger, as section 3 of the
    wire format documents, and drops to local mode after a software one, which section 3
    does not document either way. The stand-in's choice is the pessimistic reading of the
    open question, and it is the reading `rearm_with_reset` exists for.
    """
    document = method_module.to_dict(make_method(**kwargs))
    document["boxes"][0]["setup"] = ["STBLCLK,EXT", "STBLTRG,SW"]
    return method_module.from_dict(document)


def test_a_box_whose_table_does_not_re_arm_fails_its_frames_rather_than_hanging(rig):
    """The shape of the "no" the bench is looking for (lab record, tasks 05 and 28).

    A refused start step is the frame's failure and not the run's, so the run goes on and
    says per frame what happened, rather than stopping on a traceback that names one
    command.
    """
    method = software_triggered()
    boxes = boxes_for(method)
    send_phases(method, boxes)
    run = rig.acquire(method, boxes, abort_after=None)
    assert run.frames[0].acquired
    assert [record.outcome for record in run.frames[1:]] == ["BoxRejected"] * (
        ACCUMULATIONS - 1)
    assert "TBLSTRT" in run.frames[1].detail


def test_rearm_with_reset_re_arms_before_every_console_frame_but_the_run_s_first(rig):
    """Including the first repetition of the second method frame, which is the one a
    repetition counter misses: `repetition` starts again at 1 for each method frame while
    the table it has to re-arm was spent by the previous frame's last repetition.
    """
    method = software_triggered(frames=2)
    boxes = boxes_for(method)
    send_phases(method, boxes)
    seen: list[acq.Event] = []
    run = rig.acquire(method, boxes, rearm_with_reset=True, progress=seen.append)
    assert run.complete

    resets = [event for event in seen
              if isinstance(event, PhaseSent) and event.phase == "reset"]
    starts = [event for event in seen
              if isinstance(event, PhaseSent) and event.phase == "start"]
    # Two commands per reset, one before every console frame except the run's first.
    assert len(starts) == 2 * ACCUMULATIONS
    assert len(resets) == 2 * (2 * ACCUMULATIONS - 1)


def test_rearm_with_reset_is_off_unless_asked_for(rig):
    """It is a fallback unlocked by a bench answer, not the loop's own opinion, so a box
    that does re-arm is never made to pay for one that does not."""
    method = make_method(frames=2)
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    seen: list[acq.Event] = []
    run = rig.acquire(method, boxes, progress=seen.append)
    assert run.complete
    assert not [event for event in seen
                if isinstance(event, PhaseSent) and event.phase == "reset"]


# --- failures --------------------------------------------------------------------------


def test_a_frame_that_publishes_nothing_is_recorded_and_the_run_goes_on(rig):
    """A console that failed on its first fetch ends its frame with the same `finished`
    a good frame ends with, so an empty frame is the one signal there is."""
    method = make_method()
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    seen: list[acq.Event] = []

    def fail_the_second(event: acq.Event) -> None:
        seen.append(event)
        if isinstance(event, FrameEnded) and event.record.repetition == 1:
            rig.fake.frame_batches = 0
        if isinstance(event, FrameEnded) and event.record.repetition == 2:
            rig.fake.frame_batches = None

    run = rig.acquire(method, boxes, progress=fail_the_second)
    assert not run.complete
    assert [record.outcome for record in run.frames] == \
        ["acquired", "EmptyFrameError", "acquired"]
    raw = UimfFile(run.raw_path)
    assert raw.frame_params(1).marked_complete
    assert not raw.frame_params(2).marked_complete
    assert raw.frame_params(3).marked_complete
    assert len(run.folds) == 1 and run.folds[0].error is None


def test_an_error_the_console_publishes_is_a_frames_outcome_not_the_runs(rig):
    method = make_method(accumulations=2)
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    rig.fake.frame_error = "Invalid value (1000) for parameter nbrElementsToFetch"
    run = rig.acquire(method, boxes)
    assert [record.outcome for record in run.frames] == \
        ["ConsoleAcquisitionError"] * 2
    assert "nbrElementsToFetch" in run.frames[0].detail
    assert not UimfFile(run.raw_path).frame_params(1).marked_complete


def test_a_run_gives_up_after_enough_frames_fail_in_a_row(rig):
    method = make_method(accumulations=6)
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    rig.fake.frame_batches = 0
    run = rig.acquire(method, boxes, abort_after=2)
    assert run.stopped_early is not None and "2 frames in a row" in run.stopped_early
    assert len(run.frames) == 2
    assert not run.complete


# --- the enable-gate guard --------------------------------------------------------------


def test_a_batch_before_the_start_list_has_finished_fails_the_frame(rig):
    """The only automatic detector for the two faults that look identical afterwards: a
    table that left the enable high, and the enable lead off a pulled-up input."""
    rig.fake.frame_hold_s = 0.0
    method = make_method(accumulations=1)
    boxes = make_boxes(BOX, transport=SlowBox)
    send_phases(method, boxes)
    run = rig.acquire(method, boxes)
    assert [record.outcome for record in run.frames] == ["EnableGateError"]
    assert "already recording" in run.frames[0].detail
    assert "enable lead" in run.frames[0].detail
    assert not UimfFile(run.raw_path).frame_params(1).marked_complete


def test_the_guard_can_be_turned_off(rig):
    rig.fake.frame_hold_s = 0.0
    method = make_method(accumulations=1)
    boxes = make_boxes(BOX, transport=SlowBox)
    send_phases(method, boxes)
    run = rig.acquire(method, boxes, guard_gate=False)
    assert run.complete


def test_the_guard_leaves_a_status_message_for_whoever_waits_on_it(rig):
    """It reads what is on the data topic, so anything that is not a batch has to go
    back: a `finished` or an `error` belongs to the wait for the frame's end."""
    stream = rig.stream
    status = acq.Status(text="error something", received_at=0.0, topic=acq.TOPIC_STATUS)
    stream.unread(status)
    assert stream.poll(0.0) is status
    assert stream.poll(0.0) is None


# --- keep_raw ---------------------------------------------------------------------------


def test_keep_raw_false_leaves_only_the_companion(rig):
    method = make_method(keep_raw=False)
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    run = rig.acquire(method, boxes)
    assert run.complete
    assert not run.raw_kept
    assert not os.path.isfile(run.raw_path)
    assert os.path.isfile(run.summed_path)


def test_a_run_that_folded_nothing_keeps_its_raw_file_whatever_keep_raw_says(rig):
    """Discarding is irreversible and there is nothing to have been replaced by."""
    method = make_method(keep_raw=False, accumulations=2)
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    rig.fake.frame_batches = 0
    run = rig.acquire(method, boxes, abort_after=1)
    assert run.raw_kept and os.path.isfile(run.raw_path)
    assert not os.path.isfile(run.summed_path)


# --- the golden methods -------------------------------------------------------------------


def test_the_detection_response_golden_method_acquires(rig):
    method = golden("detection-response", accumulations=1)
    assert refusals(method) == []
    assert method.acquisition.repetition_mode == "per_repetition"
    boxes = boxes_for(method, arb_modules=ARB_MODULES)
    send_phases(method, boxes)
    run = rig.acquire(method, boxes)
    assert run.complete
    assert UimfFile(run.summed_path).frame_params(1).scans == SCANS


def test_the_bradykinin_clock_golden_method_acquires_with_its_cross_box_start_order(rig):
    """Both compression tables must be sitting at their hold before the box that issues
    the release edge is triggered, which is why the start list is ordered at all."""
    method = golden("bradykinin-clock")
    assert refusals(method) == []
    assert method.acquisition.repetition_mode == "single_frame"
    ordered = [step.box for step in method.start]
    assert len(set(ordered)) == 3, "three boxes, each started once"
    assert method.start[-1].command == "TBLSTRT", "the box that releases the edge goes last"
    boxes = boxes_for(method, arb_modules=ARB_MODULES)
    seen: list[acq.Event] = []
    send_phases(method, boxes)
    run = rig.acquire(method, boxes, progress=seen.append)
    assert run.complete
    walked = [(event.box, event.command) for event in seen
              if isinstance(event, PhaseSent) and event.phase == "start"]
    assert walked == [(step.box, step.command) for step in method.start]
    summed = UimfFile(run.summed_path)
    assert summed.frame_params(1).scans == SCANS
    assert summed.frame_params(1).accumulations == ACCUMULATIONS


# --- the instrument document against the machine ----------------------------------------


def test_an_offset_that_disagrees_with_the_instrument_document_is_warned_about(rig):
    """The stamp records the vertical settings so that two files acquired through
    different windows can be told apart (lab record, task 25), and a document that has
    drifted from the machine makes that record wrong rather than absent. The rig's
    console was configured to 0.251 V."""
    from clockwork.instrument import Instrument, Vertical

    method = make_method()
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    seen: list[acq.Event] = []
    machine = Instrument(name="SLIM3", vertical=Vertical(full_scale_v=0.5, offset_v=0.2))
    run = rig.acquire(method, boxes, instrument=machine, progress=seen.append)
    assert run.complete, "a half-checkable window is not a thing to stop a run on"
    warnings = [event.message for event in seen if isinstance(event, Warned)]
    assert len(warnings) == 1
    assert "0.251" in warnings[0] and "0.2 V" in warnings[0]


def test_an_offset_that_agrees_is_not_warned_about(rig):
    from clockwork.instrument import Instrument, Vertical

    method = make_method()
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    seen: list[acq.Event] = []
    machine = Instrument(vertical=Vertical(full_scale_v=0.5, offset_v=0.251))
    rig.acquire(method, boxes, instrument=machine, progress=seen.append)
    assert [event for event in seen if isinstance(event, Warned)] == []


def test_the_offset_is_checked_against_what_the_client_sent() -> None:
    """The console only ever knows the offset this client gave it, so the document is
    the only other source and a disagreement is the document's to fix."""
    from clockwork.acq.loop import _vertical_warnings
    from clockwork.instrument import Instrument, Vertical

    machine = Instrument(vertical=Vertical(full_scale_v=0.5, offset_v=0.251))

    class Stub:
        offset_v = None

    assert _vertical_warnings(Stub(), machine) == [], "nothing sent, nothing to check"

    Stub.offset_v = 0.251
    assert _vertical_warnings(Stub(), machine) == []

    Stub.offset_v = 0.200
    warnings = _vertical_warnings(Stub(), machine)
    assert len(warnings) == 1
    assert "0.2 V" in warnings[0] and "0.251 V" in warnings[0]
    assert "document's value" in warnings[0], "the document is what the file carries"


def test_the_full_scale_is_checked_against_what_the_console_reports() -> None:
    """The half of the window that was unverifiable until the fork's `info` reported it
    (lab record, task 24). Here the console is the authority, not the document."""
    from clockwork.acq.loop import _vertical_warnings
    from clockwork.acq.wire import ConsoleInfo
    from clockwork.instrument import Instrument, Vertical

    machine = Instrument(vertical=Vertical(full_scale_v=0.5, offset_v=0.251))

    class Stub:
        offset_v = 0.251

    agrees = ConsoleInfo.parse("Full Scale: 0.5")
    assert _vertical_warnings(Stub(), machine, agrees) == []

    disagrees = ConsoleInfo.parse("Full Scale: 2.5")
    warnings = _vertical_warnings(Stub(), machine, disagrees)
    assert len(warnings) == 1
    assert "full scale" in warnings[0]
    assert "2.5 V" in warnings[0] and "0.5 V" in warnings[0]
    assert "console's value" in warnings[0], "the console is what the file carries"

    silent = ConsoleInfo.parse("App Version: 0.1.0-8c5ed07")
    assert _vertical_warnings(Stub(), machine, silent) == [], (
        "a console that reports no full scale leaves that half unchecked, as before"
    )


def test_both_halves_of_the_window_can_disagree_at_once() -> None:
    from clockwork.acq.loop import _vertical_warnings
    from clockwork.acq.wire import ConsoleInfo
    from clockwork.instrument import Instrument, Vertical

    machine = Instrument(vertical=Vertical(full_scale_v=0.5, offset_v=0.251))

    class Stub:
        offset_v = 0.200

    warnings = _vertical_warnings(Stub(), machine, ConsoleInfo.parse("Full Scale: 2.5"))
    assert len(warnings) == 2


def test_a_document_with_no_offset_checks_nothing() -> None:
    from clockwork.acq.loop import _vertical_warnings
    from clockwork.instrument import Instrument, Vertical

    class Stub:
        offset_v = 0.251

    assert _vertical_warnings(Stub(), Instrument(vertical=Vertical(full_scale_v=0.5))) == []
