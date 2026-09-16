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
  on the wire, so `rows_at_finished` and `rows_at_end` are always equal here and the row
  count has stopped moving before the last scan arrives. What can still be asserted is
  which rule ended each frame and that the completion marker came after it, which is the
  ordering the lag makes necessary. It also writes no row for one scan in every sixteen,
  which is the console's own rule and the reason a frame cannot be called written by
  counting its rows up to `frame_length`.
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
import re
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
    cautions,
    refusals,
    run_acquisition,
    send_phases,
)
from clockwork.mips import Box, BoxRejected, FakeBox, compile_table, digital_events
from clockwork.mips import compressor as mips_compressor

SCANS = 32
"""Two of the fake's 16-scan spectrum periods, so a fold has something to add."""

ACCUMULATIONS = 3
SILENCE = 0.3
"""Long enough to be a silence against the fake, short enough to run a test suite in.

The instrument's number is `acq.SILENCE_S`, three seconds, and it is the longest gap
ever measured between two messages of a frame that was still delivering, with margin,
rather than a preference. It is the fallback: a frame that counts out never reaches it.
"""

ROW_SETTLE = 0.05
"""How long the file's row count has to hold still, against the stand-in.

The instrument's number is `acq.ROW_SETTLE_S`, 200 ms. The fake writes each batch's rows
before publishing it, so its file is never behind and any value settles at once; what
this buys the suite is that the ordinary end of a frame costs one poll rather than four.
"""

HOLD = 0.05
"""What `FakeConsole.frame_hold_s` stands in for: the pushes a real frame spends waiting
for its enable to go high while the loop walks the start list."""

DWELL = 0.01
"""How long the run's first frame is held open before anything is released.

The instrument's number is derived from the period the console measured -- one batch of
pushes plus `acq.GATE_PUBLISH_ALLOWANCE_S`, about 165 ms -- and the stand-in has no
period to derive it from, only `HOLD`. What the suite needs is a dwell comfortably
shorter than the hold, so that a frame the fake holds looks like a gate that is shut and
a frame it does not looks like one that is open.
"""

BOX = "box1"
"""The one box most of these tests need. A box name is a free-form string a method
supplies, so the suite invents its own rather than naming an instrument's."""

ARB_MODULES = 4
"""Enough modules for the golden methods' `SWFREQ,4,...` and `SARBCCLK,4,...`."""


def per_repetition_table(scans: int) -> str:
    """The sequencer's table for a `per_repetition` frame of `scans` scans.

    The shape and the two load-bearing numbers are `clockwork.method`'s, not this
    suite's: DIOA and DIOB raised together at the loop's tick 0, DIOB dropped at 500,
    and DIOA dropped a whole console batch past the last counted scan. Written out here
    because the digitizer's enable being DIOA is an instrument fact and not a property
    of a method document, so nothing in the package generates this string.
    """
    return (f"STBLDAT;0:[A:1,0:A:1:B:1,500:B:0,"
            f"{method_module.enable_fall_tick(scans)}:A:0,"
            f"{method_module.table_period(scans)}:];")


def single_frame_table(scans: int, accumulations: int) -> str:
    """The sequencer's table for a `single_frame` method, the trainee's own shape.

    DIOA raised once before the loop and never lowered, the loop running once per
    repetition for `accumulations` passes of `scans` ticks. This is what the golden
    CLOCK method carries; the mode exists so that it can be sent verbatim.
    """
    return f"STBLDAT;0:A:1[A:{accumulations},0:B:1,500:B:0,{scans}:];"


DECLARED_ENABLE = {"box": BOX, "channel": "A"}
"""What `acquisition.enable` says on this suite's methods, and on the instrument's.

Declared by default rather than left out, because the checks the lab record's task 31
added read the enable's edges out of the sequencer's table and need to be told which
line that is. A test that wants a method which does not say passes `enable=None`.
"""


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
    enable: dict[str, str] | None = DECLARED_ENABLE,
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
            **({"enable": enable} if enable is not None else {}),
        },
        "boxes": [{
            "name": BOX,
            "port": "COM3",
            "setup": ["STBLCLK,EXT", "STBLTRG,POS"],
            # The table has to be the shape the mode says, or the method contradicts
            # itself and `refusals` says so before a box is opened (lab record, task 31).
            "load": [single_frame_table(scans, accumulations)
                     if repetition_mode == "single_frame" else per_repetition_table(scans)],
            "arm": ["SMOD,TBL"],
        }],
        "start": [[BOX, "TBLSTRT"]],
        "reset": reset if reset is not None else [[BOX, "SMOD,LOC"],
                                                  [BOX, "SMOD,TBL"]],
    })


def golden(name: str, *, scans: int = SCANS, accumulations: int = ACCUMULATIONS):
    """One of the two golden methods, shrunk to a size a test can acquire.

    Every box name, sequence and op is the trainee's; only the frame arithmetic is
    reduced, because a real one is 20000 scans in one case and half a million in the
    other. Since the lab record's task 31 the counts inside the strings are reduced with
    it, because the two are now compared: a document shrunk on its own would be a method
    that contradicts itself, which is the check working rather than a fixture owed an
    exemption. Skips where the lab record is not beside this clone, which is every public
    one.
    """
    directory = clockwork.lab_dir("golden")
    if directory is None:
        pytest.skip("the golden experiments are lab material and this is a public clone")
    path = os.path.join(directory, name, "method.toml")
    if not os.path.isfile(path):
        pytest.skip(f"no golden method at {name}")
    loaded = method_module.load(path)
    passes = (accumulations
              if loaded.acquisition.repetition_mode == "single_frame" else 1)
    return dataclasses.replace(
        loaded,
        acquisition=dataclasses.replace(
            loaded.acquisition, scans=scans, accumulations=accumulations,
        ),
        boxes=tuple(
            dataclasses.replace(
                box,
                load=tuple(shrunk(command, scans=scans, passes=passes)
                           for command in box.load),
            )
            for box in loaded.boxes
        ),
    )


def shrunk(command: str, *, scans: int, passes: int) -> str:
    """A golden string with its own two counts moved to where the document's are.

    Three numbers and no others: an `STBLDAT` loop's repeat count and its period, and a
    compression table's `]N`. Everything else in the string, including every DC bias
    event and its tick, is left exactly as the trainee wrote it -- which does leave a
    shrunk CLOCK table with events past its own period, since the point of the fixture
    is the order the loop does things in and not an experiment anyone could run.
    """
    if command.startswith(mips_compressor.COMPRESSION_COMMAND):
        return re.sub(r"\](\d*)(\s*)$", rf"]{passes}\2", command)
    if command.startswith("STBLDAT"):
        command = re.sub(r"\[([A-P]):(\d+),", rf"[\1:{passes},", command, count=1)
        return re.sub(r"(\d+):\];$", f"{scans}:];", command)
    return command


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
        kwargs.setdefault("row_settle", ROW_SETTLE)
        kwargs.setdefault("empty_settle", 0.3)
        kwargs.setdefault("gate_dwell", DWELL)
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


BATCH_SCANS = 8
"""Scans per batch in the `batched` rig below, so that `SCANS` is four of them.

The stand-in's default batch is 100 scans and a test frame is 32, so every other test
here runs a frame that is one whole batch and can say nothing about the order batches
and `finished` arrive in. That order is the whole subject of the wait for a frame's end.
"""


@pytest.fixture
def batched(tmp_path):
    """A rig whose frames are four batches, with the last two trailing `finished`.

    Which is the console's ordering and not this stand-in's default: batches go out from
    a subscriber thread and `finished` from the acquisition thread, so a frame's last
    batches arrive after the frame has ended (lab record, task 20).
    """
    with Rig(tmp_path, notify_on_scans_count=BATCH_SCANS) as running:
        running.fake.trailing_batches = 2
        yield running


# --- what a method has to say for itself ----------------------------------------------


def test_single_frame_with_more_than_one_frame_is_refused_before_anything_is_sent(rig):
    """The table that loops on the box raises the digitizer's enable once and never
    lowers it, so a second method frame would begin recording before its start list
    ran and be offset by the serial latency (lab record, task 05)."""
    method = make_method(repetition_mode="single_frame", frames=2, enable=None)
    assert len(refusals(method)) == 1
    boxes = make_boxes(BOX)
    with pytest.raises(AcquisitionRefused, match="never lowers"):
        rig.acquire(method, boxes)
    assert not os.listdir(rig.directory)
    assert rig.fake.frames == []


def test_one_single_frame_and_every_per_repetition_method_are_acquirable():
    assert refusals(make_method(repetition_mode="single_frame", frames=1)) == []
    assert refusals(make_method(repetition_mode="per_repetition", frames=5)) == []


# --- a method against its own strings (lab record, task 31) ---------------------------


def golden_text(name: str) -> str:
    """One golden method as it is written on disk, for mutating a single number of."""
    directory = clockwork.lab_dir("golden")
    if directory is None:
        pytest.skip("the golden experiments are lab material and this is a public clone")
    path = os.path.join(directory, name, "method.toml")
    if not os.path.isfile(path):
        pytest.skip(f"no golden method at {name}")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def mutated(text: str, old: str, new: str) -> method_module.Method:
    assert old in text, f"{old!r} is no longer in the golden method"
    return method_module.loads(text.replace(old, new, 1))


@pytest.mark.parametrize("name", ["bradykinin-clock", "detection-response"])
def test_both_golden_methods_agree_with_their_own_strings(name):
    """Full size and unedited, which is the only way this says anything: the counts
    compared are the trainee's 100 accumulations and 5000 scans, not a fixture's."""
    loaded = method_module.loads(golden_text(name))
    assert refusals(loaded) == []
    assert cautions(loaded) == []


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("[A:100,", "[A:50,", "loops 50 time(s)"),
        ("r]100", "r]50", "runs 50 pass(es)"),
        ("accumulations = 100", "accumulations = 50", "acquisition.accumulations is 50"),
        ("scans = 5000", "scans = 4000", "acquisition.scans is 4000"),
        ("STBLDAT;0:A:1[A:100,", "STBLDAT;0:[A:100,", "never raises A"),
    ],
)
def test_one_changed_number_in_the_clock_method_is_refused_and_named(old, new, expected):
    """The way this actually happens: a trainee shortens a run by editing one of the
    five numbers and leaves the other four. Each mutation is one character's worth of
    edit and each used to be acquired silently -- as a wrong fold, a frame that never
    completes, or a frame that acquires nothing (lab record, tasks 31 and 33)."""
    problems = refusals(mutated(golden_text("bradykinin-clock"), old, new))
    assert problems, f"{old!r} -> {new!r} was not caught"
    assert any(expected in problem for problem in problems), problems


def test_a_per_repetition_table_that_drops_the_enable_at_the_wrong_tick_is_refused():
    """A whole `NotifyOnScansCount` past the last counted scan is the rule, measured:
    at `scans + 1` and at `scans + 100` every frame of every run stopped one batch short
    and never finished (lab record, task 33)."""
    early = per_repetition_table(SCANS).replace(
        f"{method_module.enable_fall_tick(SCANS)}:A:0", f"{SCANS + 1}:A:0")
    method = dataclasses.replace(
        make_method(),
        boxes=(dataclasses.replace(make_method().boxes[0], load=(early,)),),
    )
    problems = refusals(method)
    assert len(problems) == 1
    assert f"lowers A at tick {SCANS + 1}" in problems[0]
    assert str(method_module.enable_fall_tick(SCANS)) in problems[0]


def test_a_per_repetition_table_that_never_drops_the_enable_is_refused_past_one_frame():
    """`_lower_enable` is `single_frame`'s alone, so under `per_repetition` a table that
    leaves the gate high leaves it high into the next repetition's `acquire frame`."""
    never = single_frame_table(SCANS, 1)
    boxes = (dataclasses.replace(make_method().boxes[0], load=(never,)),)
    one = dataclasses.replace(make_method(accumulations=1, frames=1), boxes=boxes)
    assert refusals(one) == [], "one console frame has no later frame to offset"
    many = dataclasses.replace(make_method(accumulations=1, frames=2), boxes=boxes)
    assert any("never lowers A" in problem for problem in refusals(many))


def test_a_string_that_could_not_be_read_is_a_caution_and_not_a_refusal():
    """A trainee's verbatim string is not clockwork's to reject for being unusual, and a
    string this package could not read is not evidence that anything is wrong with the
    method (Matt, 2026-09-14)."""
    boxes = (dataclasses.replace(make_method().boxes[0],
                                 load=(per_repetition_table(SCANS),
                                       "SARBCTBL,J10[HRsm1CD12r")),)
    method = dataclasses.replace(make_method(), boxes=boxes)
    assert refusals(method) == []
    assert any("unclosed" in line for line in cautions(method))


def test_a_method_that_does_not_name_its_gate_line_keeps_the_count_checks():
    """The enable's edges need `acquisition.enable` and the counts do not, so a method
    that leaves it out is told what was skipped and still has its loop count read."""
    quiet = make_method(enable=None)
    assert refusals(quiet) == []
    assert any("acquisition.enable is not declared" in line for line in cautions(quiet))
    wrong = dataclasses.replace(
        make_method(enable=None, repetition_mode="single_frame"),
        boxes=(dataclasses.replace(
            make_method().boxes[0],
            load=(single_frame_table(SCANS, ACCUMULATIONS + 1),)),),
    )
    assert [problem for problem in refusals(wrong)
            if f"loops {ACCUMULATIONS + 1} time(s)" in problem]


def test_send_phases_refuses_a_method_that_contradicts_itself_before_it_sends(rig):
    """Before anything is sent is the whole point: a refused method costs nothing and
    leaves no half-loaded box behind."""
    method = mutated(golden_text("bradykinin-clock"), "[A:100,", "[A:50,")
    boxes = boxes_for(method, arb_modules=ARB_MODULES)
    with pytest.raises(AcquisitionRefused, match="loops 50"):
        send_phases(method, boxes)
    assert boxes["auklet"].transport.written == []


def test_the_cautions_reach_a_caller_as_warned_events():
    method = make_method(enable=None)
    seen: list[acq.Event] = []
    send_phases(method, make_boxes(BOX), progress=seen.append)
    assert [event.message for event in seen if isinstance(event, Warned)] \
        == cautions(method)


# --- the phases ------------------------------------------------------------------------


def test_send_phases_sends_setup_load_and_arm_in_the_order_written():
    method = make_method()
    boxes = make_boxes(BOX)
    seen: list[acq.Event] = []
    send_phases(method, boxes, progress=seen.append)
    assert isinstance(seen[0], BoxReady)
    sent = [(event.phase, event.command) for event in seen if isinstance(event, PhaseSent)]
    # The leading `SMOD,LOC` is the setup phase's own: this method's `STBLCLK` and
    # `STBLTRG` are LOC-mode only, and the box may still be armed from the acquisition
    # before this one.
    assert [phase for phase, _ in sent] == ["setup", "setup", "setup", "load", "arm"]
    assert sent[0] == ("setup", "SMOD,LOC")
    assert sent[-1] == ("arm", "SMOD,TBL")
    assert "TBLRDY" in [event.detail for event in seen if isinstance(event, PhaseSent)][-1]


def test_a_setup_phase_with_loc_only_commands_drops_the_box_out_of_table_mode_first():
    """`STBLCLK` and `STBLTRG` are LOC-mode only (wire format, section 4), so a box left
    armed by the acquisition before refuses the first string of the next one. A bench
    session met exactly that, twice over, and only the flag that had happened to leave
    the box local hid it the first time (lab record, task 30)."""
    boxes = make_boxes(BOX)
    send_phases(make_method(), boxes, progress=None)
    written = boxes[BOX].transport.written
    assert written.index(b"SMOD,LOC\n") < written.index(b"STBLCLK,EXT\n")


def test_a_setup_phase_with_no_loc_only_command_is_sent_exactly_as_written():
    """Both golden methods' ARB boxes take only a frequency block, and their sequencer's
    setup is empty; none of them may gain a mode change it never asked for."""
    document = method_module.to_dict(make_method())
    document["boxes"][0]["setup"] = ["SWFREQ,1,15000", "SWFVRNG,1,15"]
    boxes = make_boxes(BOX, arb_modules=ARB_MODULES)
    seen: list[acq.Event] = []
    send_phases(method_module.from_dict(document), boxes, progress=seen.append)
    setup_sent = [event.command for event in seen
                  if isinstance(event, PhaseSent) and event.phase == "setup"]
    assert setup_sent == ["SWFREQ,1,15000", "SWFVRNG,1,15"]


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
        # 8000 scans against a table of 8000 ticks: nothing here acquires, and a method
        # whose two counts disagree is refused before the send this test is about.
        "acquisition": {"frames": 1, "scans": 8000, "accumulations": 1,
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
                              gate_dwell=DWELL,
                              post_trigger_samples=rig.fake.post_trigger_samples)
    finally:
        console.close()
    assert run.complete
    pairs = [entry for entry in log
             if entry == ("console", "acquire frame") or entry[1] == "TBLSTRT"]
    assert pairs == [("console", "acquire frame"), ("box", "TBLSTRT")] * ACCUMULATIONS


def test_a_frame_is_over_when_it_has_counted_out_not_when_it_says_finished(rig):
    """The completion marker is the only thing in the file that tells a frame that
    finished from one that was cut off, and the console is still inserting rows for a
    frame whose `finished` has gone out (lab record, tasks 20 and 34)."""
    method = make_method(accumulations=1)
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    run = rig.acquire(method, boxes)
    record = run.frames[0]
    assert record.ended_by == "counted"
    assert record.scans_published == SCANS
    # The whole point of the change: a frame that counted out never waits the fallback
    # out. Against the stand-in the wait is one poll and a settle.
    assert record.wait_seconds < SILENCE
    assert record.settle_seconds == 0.0
    assert record.rows_at_finished is not None
    assert record.rows_at_end is not None
    # The stand-in writes each batch's rows before publishing it, so it has no lag to
    # find; what is asserted is that both counts were taken and the second came later.
    assert record.writer_lag_rows == 0
    assert UimfFile(run.raw_path).frame_params(1).marked_complete


def test_a_frame_is_not_over_while_its_batches_are_still_arriving(batched):
    """The ordering that makes the wait necessary at all: two of the frame's four
    batches arrive after its own `finished`, and the frame is whole only because the
    loop kept listening."""
    method = make_method(accumulations=1)
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    run = batched.acquire(method, boxes)
    record = run.frames[0]
    assert record.trailing_batches == 2
    assert record.scans_after_finished == 2 * BATCH_SCANS
    assert record.ended_by == "counted"
    assert record.scans_published == SCANS
    assert record.wait_seconds < SILENCE


def test_a_frame_that_stops_short_falls_back_to_the_silence(batched):
    """A frame that never reaches its own `frame_length` cannot count out, and the
    silence is what ends it. The scans it did publish are still its own."""
    batched.fake.frame_batches = 3
    method = make_method(accumulations=1)
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    run = batched.acquire(method, boxes)
    record = run.frames[0]
    assert record.ended_by == "silence"
    assert record.settle_seconds is None
    assert 0 < record.scans_published < SCANS
    assert record.wait_seconds >= SILENCE
    # Short is not the same as failed: the console ended the frame the way it ends a
    # whole one, and nothing the loop can see says otherwise (`session.run_frame`).
    assert record.acquired
    assert "ended on the silence" in record.text


def test_a_last_batch_later_than_a_batch_is_waited_for_and_still_counts_out(batched):
    """The count survives a console that is slow, which is the case the six-second
    silence covered by waiting six seconds on every frame. The gap here is four times a
    poll and well inside the fallback."""
    batched.fake.final_batch_delay_s = 0.2
    method = make_method(accumulations=1)
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    run = batched.acquire(method, boxes)
    record = run.frames[0]
    assert record.ended_by == "counted"
    assert record.scans_published == SCANS
    assert record.wait_seconds >= 0.2


def test_a_last_batch_later_than_the_silence_is_the_gap_the_fallback_is_set_against(
    batched,
):
    """The one thing shortening the fallback costs, stated as a test rather than left to
    be met on an instrument. A frame quiet for longer than `silence` before its last
    batch ends on the fallback, and the batch that would have completed its count
    arrives too late to be counted, so the fallback has to clear the largest gap a
    delivering frame has ever shown: 1.194 s against 3 s (lab record, task 34)."""
    batched.fake.final_batch_delay_s = SILENCE * 2
    method = make_method(accumulations=1)
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    run = batched.acquire(method, boxes)
    record = run.frames[0]
    assert record.ended_by == "silence"
    assert record.scans_published < SCANS


def assert_companion_sums_the_raw_file(run, method_frame=1):
    """The fold's contract: the companion holds the sum of the raw frames it folded.

    Stated against the raw file rather than against `ACCUMULATIONS` times one
    repetition, because a run whose repetitions were cut short at different places has
    no single repetition to multiply, and it is exactly that run the fold has to keep
    honest. This is what `uimf-info --verify` checks on the bench.
    """
    raw, summed = UimfFile(run.raw_path), UimfFile(run.summed_path)
    numbers = [record.frame_number for record in run.frames
               if record.method_frame == method_frame and record.acquired]
    folded = [raw.read_frame(number) for number in numbers]
    total = summed.read_frame(method_frame)
    assert len(total) > 0
    for scan in range(summed.frame_params(method_frame).scans):
        values: dict[int, int] = {}
        for frame in folded:
            for at, value in zip(*frame.scan(scan), strict=True):
                values[at] = values.get(at, 0) + value
        assert dict(zip(*total.scan(scan), strict=True)) == {
            at: v for at, v in values.items() if v
        }, (
            f"scan {scan} of method frame {method_frame}"
        )


def test_the_fold_is_exact_however_the_frames_ended(batched):
    """The equality the fold owes, on a run whose frames counted out and on one whose
    frames were cut short by the fallback. A fold that ran before a frame was written
    would be short here rather than wrong, which is why it is asserted per scan."""
    method = make_method()
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    counted = batched.acquire(method, boxes, stem="260911_TEST_COUNTED")
    assert {record.ended_by for record in counted.frames} == {"counted"}
    assert_companion_sums_the_raw_file(counted)

    # A run in which the console's last batch is later than the fallback. The frames
    # that end on it are short, and the backlog bleeds into the repetition after, which
    # is the console's own behaviour and what the next frame's gate guard exists for.
    batched.fake.final_batch_delay_s = SILENCE * 2
    fell_back = batched.acquire(method, boxes, stem="260911_TEST_SILENCE")
    assert "silence" in {record.ended_by for record in fell_back.frames}
    assert_companion_sums_the_raw_file(fell_back)


def test_the_boxes_are_read_while_the_frame_runs_and_not_after_it(batched):
    """A box raises `TBLTRIG` at its table's first tick and `TBLCMPLT` at its last, and
    an event's only timestamp is when the host read the port. Until task 34 the loop
    drained the ports after each frame's wait, so every box event on the BUFFLEHEAD day
    was dated seconds late and the day's first reading of the transcripts was wrong.
    """
    marks: list[str] = []

    class WatchedBox(FakeBox):
        def write(self, data: bytes) -> None:
            marks.append("wrote " + data.decode("ascii", "replace").strip())
            super().write(data)

        def read_some(self, timeout: float) -> bytes:
            marks.append("read")
            return super().read_some(timeout)

    boxes = make_boxes(BOX, transport=WatchedBox)
    method = make_method(accumulations=1)
    send_phases(method, boxes)
    marks.clear()
    batched.acquire(method, boxes, progress=lambda e: marks.append(type(e).__name__))

    released = marks.index("wrote TBLSTRT")
    ended = marks.index("FrameEnded")
    said = [at for at, mark in enumerate(marks) if mark == "BoxSaid"]
    assert said and max(said) < ended, "a box event was reported after its frame ended"
    assert released < min(said)
    # And the ports were polled through the wait rather than read once at the end of
    # it. The frame is four batches, two of them after its own `finished`, so a wait
    # that reads the ports on its own cadence reads them several times over.
    assert marks[released:ended].count("read") >= 3


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
    table that left the enable high, and the enable lead off a pulled-up input.

    `gate_dwell=0` leaves the start list as the only window, which is the half of the
    guard this is about; the dwell has its own tests below.
    """
    rig.fake.frame_hold_s = 0.0
    method = make_method(accumulations=1)
    boxes = make_boxes(BOX, transport=SlowBox)
    send_phases(method, boxes)
    run = rig.acquire(method, boxes, gate_dwell=0.0)
    assert [record.outcome for record in run.frames] == ["EnableGateError"]
    assert "already recording" in run.frames[0].detail
    assert "enable lead" in run.frames[0].detail
    assert not UimfFile(run.raw_path).frame_params(1).marked_complete


def test_the_run_proves_its_gate_is_shut_before_it_releases_anything(rig):
    """The dwell's passing case, which is the only positive evidence a run has.

    Reported once and on the first frame: every frame after it is released against a
    gate this one proved shut, and a dwell per repetition would be spent a hundred times
    over on the per-repetition dead time.
    """
    seen: list[acq.GateChecked] = []
    method = make_method(accumulations=3)
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    run = rig.acquire(method, boxes,
                      progress=lambda event: seen.append(event)
                      if isinstance(event, acq.GateChecked) else None)
    assert run.complete and len(run.frames) == 3
    assert len(seen) == 1
    assert (seen[0].method_frame, seen[0].repetition) == (1, 1)
    assert seen[0].seconds >= DWELL


def test_a_batch_during_the_dwell_fails_every_frame_until_the_run_gives_up(rig):
    """A digitizer recording before anything was released, which is what a chain opened
    with the enable input disabled looks like when enabling it again did not take.

    The start list is instantaneous against the stand-in, so nothing but the dwell could
    catch this: the fake publishes the moment the frame is asked for, which is the shape
    of an ungated frame rather than of a slow acknowledgement. The dwell is the whole of
    a real one here rather than the suite's `DWELL`, because what is being waited on is
    the fake writing rows and encoding a batch and not a number of pushes. It is not
    marked checked on a failure, so it fails again on the next frame and `abort_after`
    ends the run.
    """
    rig.fake.frame_hold_s = 0.0
    method = make_method(accumulations=4)
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    run = rig.acquire(method, boxes, abort_after=2, gate_dwell=0.3)
    assert [record.outcome for record in run.frames] == ["EnableGateError"] * 2
    assert "before anything had been released" in run.frames[0].detail
    assert "enable input disabled" in run.frames[0].detail
    assert run.stopped_early is not None


def test_the_dwell_is_one_batch_of_pushes_plus_the_path_a_batch_takes(rig):
    """Derived from the period the console measured, because the pushes are the larger
    half of it and a bench generator is not the pusher."""
    geometry = acq.Geometry(bins=100, bin_width_ns=0.5, time_offset_ns=0.0,
                            average_tof_length_ns=129_003.6, offset_bins=20)
    dwell = acq.loop._gate_dwell(geometry)
    assert dwell == pytest.approx(
        method_module.NOTIFY_ON_SCANS_COUNT * 129_003.6e-9 + acq.GATE_PUBLISH_ALLOWANCE_S
    )
    assert 0.16 < dwell < 0.17


def test_single_frame_with_more_than_one_frame_acquires_once_the_gate_line_is_named(rig):
    """The refusal above is lifted by the one thing the loop was missing: which output
    carries the enable. With that declared it lowers the line itself between method
    frames and the trainee's looping table is acquirable as written (lab record,
    task 26)."""
    method = make_method(frames=2, repetition_mode="single_frame",
                         enable={"box": BOX, "channel": "A"})
    assert refusals(method) == []
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    run = rig.acquire(method, boxes)
    assert run.complete and len(run.frames) == 2 and len(run.folds) == 2


def test_the_gate_line_is_lowered_between_method_frames_and_not_before_the_first(rig):
    """Three commands, in local mode, before the frame is asked for -- and none of it
    ahead of the run's own first frame, whose gate is the dwell's business.

    The order is the whole invariant and the only part of it that cannot be recovered
    from the file afterwards: the gate has to be down when the console is told to
    acquire, not a moment after.
    """
    log: list[str] = []

    def watch(event):
        if isinstance(event, PhaseSent) and event.phase == "enable":
            log.append(event.command)
        if isinstance(event, FrameBegun):
            log.append(f"frame {event.method_frame}")

    method = make_method(frames=2, repetition_mode="single_frame",
                         enable={"box": BOX, "channel": "A"})
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    # The state the previous method frame leaves behind: a table that looped on the box
    # and raised the enable at its tick 0, with nothing in it to lower the line again.
    boxes[BOX].transport.dio_image["A"] = True
    boxes[BOX].transport.dio_pins["A"] = True
    run = rig.acquire(method, boxes, progress=watch)

    assert run.complete
    assert log == ["frame 1", "SMOD,LOC", "SDIO,A,0", "SMOD,TBL", "frame 2"]
    assert boxes[BOX].transport.dio_pins["A"] is False


def test_per_repetition_lowers_nothing_by_command(rig):
    """Its table drops the enable a batch past its last counted scan, and a round trip
    through local mode per repetition would be a hundred of them per method frame."""
    sent: list[str] = []
    method = make_method(frames=2, accumulations=2,
                         enable={"box": BOX, "channel": "A"})
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    run = rig.acquire(method, boxes,
                      progress=lambda event: sent.append(event.command)
                      if isinstance(event, PhaseSent) and event.phase == "enable"
                      else None)
    assert run.complete and sent == []


def test_a_gate_line_on_a_digital_input_is_refused_before_anything_is_sent():
    """MIPS acknowledges `SDIO,Q,0` and drives output `I` with it, so a method naming an
    input has to be caught by the host or not at all."""
    method = make_method(enable={"box": BOX, "channel": "Q"})
    problems = refusals(method)
    assert len(problems) == 1
    assert "digital input" in problems[0]
    assert "acquisition.enable.channel" in problems[0]


def test_the_table_a_per_repetition_method_carries_actually_raises_the_enable(rig):
    """The check that would have caught the defect the first hardware run met.

    The table that failed differed from this one by four characters: it opened
    `0:[A:1,0:B:1` rather than `0:[A:1,0:A:1:B:1`, which is a loop named `'A'` and no
    event on DIOA anywhere, and so a frame with the digitizer's enable never raised
    (lab record, task 33).
    """
    method = make_method()
    compiled = compile_table(method.box(BOX).load[0])
    rise, fall = digital_events(compiled, "A")
    assert rise[1:] == (0, "1")
    assert fall[1:] == (method_module.enable_fall_tick(method.acquisition.frame_length),
                        "0")
    assert compiled.tables[-1].max_count == method_module.table_period(
        method.acquisition.frame_length)


def test_the_start_list_is_spaced_and_not_only_ordered(rig):
    """Order on the wire is not an interval: two consecutive serial writes left the
    host in the same millisecond on 13 of 54 measured repetitions, and the ARB box the
    order exists for is not at its first hold until its own trigger delay has run
    (lab record, task 33)."""
    method = make_method()
    method = dataclasses.replace(method, start=(
        method_module.Step(BOX, "TARBTRG"), method_module.Step(BOX, "TBLSTRT"),
    ))
    boxes = make_boxes(BOX, arb_modules=ARB_MODULES)
    send_phases(method, boxes)
    sent: list[tuple[float, str]] = []

    def watch(event: acq.Event) -> None:
        if isinstance(event, PhaseSent) and event.phase == "start":
            sent.append((time.perf_counter(), event.command))

    rig.acquire(method, boxes, start_step_gap=0.05, progress=watch)
    assert [command for _, command in sent[:2]] == ["TARBTRG", "TBLSTRT"]
    assert sent[1][0] - sent[0][0] >= 0.05


def test_the_fold_starts_after_the_next_frame_has_been_released(rig, monkeypatch):
    """A fold running across a start list stretches the serial round trips that are the
    only thing telling the loop the experiment has begun: measured at 226 ms on a
    `TBLSTRT` against 0-7 ms everywhere else in the same run, which failed a good frame
    through the gate guard (lab record, task 33)."""
    began: list[float] = []
    original = acq.Recording.fold

    def timed(self, method_frame):
        began.append(time.perf_counter())
        return original(self, method_frame)

    monkeypatch.setattr(acq.Recording, "fold", timed)

    released: list[float] = []

    def watch(event: acq.Event) -> None:
        if isinstance(event, PhaseSent) and event.phase == "start":
            released.append(time.perf_counter())

    method = make_method(frames=2, accumulations=1)
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    run = rig.acquire(method, boxes, progress=watch)

    assert run.complete and len(run.folds) == 2
    assert len(began) == 2 and len(released) == 2
    # Method frame 1's fold is owed the moment its last repetition ends; it must not
    # begin until method frame 2's start list has gone out.
    assert began[0] > released[1]


def test_the_guard_can_be_turned_off(rig):
    """`guard_gate=False` turns off both halves, the dwell included."""
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


def test_an_inversion_that_disagrees_with_the_instrument_document_is_warned_about(rig):
    """The rig's console was configured with `inverted` at its default, false."""
    from clockwork.instrument import Instrument, Vertical

    method = make_method()
    boxes = make_boxes(BOX)
    send_phases(method, boxes)
    seen: list[acq.Event] = []
    machine = Instrument(vertical=Vertical(full_scale_v=0.5, offset_v=0.251, inverted=True))
    run = rig.acquire(method, boxes, instrument=machine, progress=seen.append)
    assert run.complete, "a half-checkable window is not a thing to stop a run on"
    warnings = [event.message for event in seen if isinstance(event, Warned)]
    assert len(warnings) == 1
    assert "inversion" in warnings[0]


def test_the_offset_is_checked_against_what_the_client_sent() -> None:
    """The console only ever knows the offset this client gave it, so the document is
    the only other source and a disagreement is the document's to fix."""
    from clockwork.acq.loop import _vertical_warnings
    from clockwork.instrument import Instrument, Vertical

    machine = Instrument(vertical=Vertical(full_scale_v=0.5, offset_v=0.251))

    class Stub:
        offset_v = None
        inverted = None

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
        inverted = None

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
        inverted = None

    warnings = _vertical_warnings(Stub(), machine, ConsoleInfo.parse("Full Scale: 2.5"))
    assert len(warnings) == 2


def test_a_document_with_no_offset_checks_nothing() -> None:
    from clockwork.acq.loop import _vertical_warnings
    from clockwork.instrument import Instrument, Vertical

    class Stub:
        offset_v = 0.251
        inverted = None

    assert _vertical_warnings(Stub(), Instrument(vertical=Vertical(full_scale_v=0.5))) == []


def test_the_inversion_is_checked_against_what_the_client_sent() -> None:
    """The console never reports it back, so the document is the only other source,
    exactly as for the offset (lab record, task 38)."""
    from clockwork.acq.loop import _vertical_warnings
    from clockwork.instrument import Instrument, Vertical

    machine = Instrument(vertical=Vertical(full_scale_v=0.5, offset_v=0.251, inverted=True))

    class Stub:
        offset_v = 0.251
        inverted = None

    assert _vertical_warnings(Stub(), machine) == [], "nothing sent, nothing to check"

    Stub.inverted = True
    assert _vertical_warnings(Stub(), machine) == []

    Stub.inverted = False
    warnings = _vertical_warnings(Stub(), machine)
    assert len(warnings) == 1
    assert "inversion" in warnings[0]
    assert "document's value" in warnings[0]
