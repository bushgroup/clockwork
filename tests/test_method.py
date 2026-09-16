"""Load/save/validate for the flat TOML method document, and its provenance stamp."""

import datetime

import pytest

from clockwork import method

SAMPLE = """\
schema_version = 2

start = [["box2", "TARBTRG"], ["box1", "TBLSTRT"]]
reset = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]

[metadata]
name = "smoke-test"
created = 2026-09-06
description = "A minimal two-box method for tests."

[acquisition]
frames = 1
scans = 100
accumulations = 10
repetition_mode = "per_repetition"
keep_raw = true
file_stem = "smoke-test"

[[boxes]]
name = "box1"
port = "COM3"
setup = ["STBLCLK,EXT"]
load = ["STBLDAT;25:[A:10,10:A:1,25:A:0,100:];"]
arm = ["SMOD,TBL"]

[[boxes]]
name = "box2"
port = "COM4"
setup = ["SWFREQ,1,15000"]
load = ["STWCTBL,J22[HRsm2CD5m2ND10r]10"]
arm = []
"""


def test_loads_sample_method() -> None:
    m = method.loads(SAMPLE)
    assert m.schema_version == method.SCHEMA_VERSION == 2
    assert m.metadata.name == "smoke-test"
    assert m.metadata.created == datetime.date(2026, 9, 6)
    assert m.acquisition == method.Acquisition(
        frames=1,
        scans=100,
        accumulations=10,
        file_stem="smoke-test",
        repetition_mode="per_repetition",
        keep_raw=True,
    )
    assert [box.name for box in m.boxes] == ["box1", "box2"]
    assert m.boxes[0].port == "COM3"
    assert m.boxes[0].setup == ("STBLCLK,EXT",)
    assert m.boxes[0].arm == ("SMOD,TBL",)
    assert m.boxes[1].arm == ()
    assert m.warnings == ()


def test_phases_default_to_empty() -> None:
    text = SAMPLE.replace('setup = ["SWFREQ,1,15000"]\n', "").replace("arm = []\n", "")
    m = method.loads(text)
    assert m.boxes[1].setup == ()
    assert m.boxes[1].arm == ()
    assert m.boxes[1].load == ("STWCTBL,J22[HRsm2CD5m2ND10r]10",)


def test_a_box_with_only_setup_is_accepted() -> None:
    """A box that only ever needs its persistent block loads nothing per acquisition."""
    text = SAMPLE.replace('load = ["STWCTBL,J22[HRsm2CD5m2ND10r]10"]', "load = []")
    m = method.loads(text)
    assert m.boxes[1].setup == ("SWFREQ,1,15000",)
    assert m.boxes[1].load == ()


def test_start_and_reset_are_ordered_steps() -> None:
    m = method.loads(SAMPLE)
    assert m.start == (
        method.Step(box="box2", command="TARBTRG"),
        method.Step(box="box1", command="TBLSTRT"),
    )
    assert m.reset == (
        method.Step(box="box1", command="SMOD,LOC"),
        method.Step(box="box1", command="SMOD,TBL"),
    )


def test_reset_may_be_absent() -> None:
    m = method.loads(SAMPLE.replace('reset = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]\n', ""))
    assert m.reset == ()


def test_box_lookup_by_name() -> None:
    m = method.loads(SAMPLE)
    assert m.box("box2").port == "COM4"
    with pytest.raises(KeyError):
        m.box("box3")


def test_per_repetition_frame_length_is_scans() -> None:
    m = method.loads(SAMPLE)
    assert m.acquisition.frame_length == 100
    assert m.acquisition.console_frames == 10


def test_single_frame_frame_length_is_the_whole_method_frame() -> None:
    m = method.loads(SAMPLE.replace('"per_repetition"', '"single_frame"'))
    assert m.acquisition.repetition_mode == "single_frame"
    assert m.acquisition.frame_length == 100 * 10
    assert m.acquisition.console_frames == 1


def test_the_enable_window_is_a_whole_batch_past_the_last_counted_scan() -> None:
    """One constant, in one place, and it is `NotifyOnScansCount` and not one push.

    The console publishes nothing until it has seen the trigger *after* the batch it is
    filling, and takes markers from the card only in whole batches' worth; a record with
    everything suppressed carries one marker hunk, which is the worst case and makes the
    margin the whole batch. Measured: at `scans + 1` and `scans + 100` every frame of
    every run stopped exactly `NotifyOnScansCount` short, and `scans + 250` upwards
    completed (lab record, task 33).
    """
    assert method.NOTIFY_ON_SCANS_COUNT == 500
    assert method.enable_fall_tick(5000) == 5500
    assert method.table_period(5000) == 5501


def test_the_enable_window_follows_a_console_configured_differently() -> None:
    assert method.enable_fall_tick(5000, 250) == 5250
    assert method.table_period(5000, 250) == 5251


def test_the_enable_window_is_taken_from_the_frame_not_the_scan_count() -> None:
    """`single_frame` counts a whole method frame off the digitizer in one go, so it is
    `frame_length` and not `scans` that the enable has to outlast."""
    m = method.loads(SAMPLE.replace('"per_repetition"', '"single_frame"'))
    assert method.enable_fall_tick(m.acquisition.frame_length) == 1000 + 500


def test_the_gate_line_is_absent_unless_a_document_declares_it() -> None:
    """Which digital output gates the digitizer is a fact about the cabling, so a
    document that does not say leaves it unsaid rather than guessing."""
    assert method.loads(SAMPLE).acquisition.enable is None


def test_the_gate_line_loads_and_round_trips() -> None:
    text = SAMPLE.replace(
        'file_stem = "smoke-test"',
        'file_stem = "smoke-test"\nenable = { box = "box1", channel = "A" }',
    )
    m = method.loads(text)
    assert m.acquisition.enable == method.Enable(box="box1", channel="A")
    assert method.loads(method.dumps(m)) == m


def test_a_document_with_no_gate_line_round_trips_to_one_that_still_has_none() -> None:
    """The key is written only where there is one, so an old document's stamp hashes
    the same bytes it always did."""
    m = method.loads(SAMPLE)
    assert "enable" not in method.to_dict(m)["acquisition"]
    assert method.loads(method.dumps(m)).acquisition.enable is None


def test_repetition_mode_and_keep_raw_default() -> None:
    text = SAMPLE.replace('repetition_mode = "per_repetition"\n', "").replace(
        "keep_raw = true\n", ""
    )
    m = method.loads(text)
    assert m.acquisition.repetition_mode == method.DEFAULT_REPETITION_MODE == "per_repetition"
    assert m.acquisition.keep_raw is method.DEFAULT_KEEP_RAW is True


def test_surrounding_whitespace_is_stripped_and_warned_about() -> None:
    text = SAMPLE.replace(
        '"STWCTBL,J22[HRsm2CD5m2ND10r]10"', '"STWCTBL,J22[HRsm2CD5m2ND10r]10\\t"'
    )
    m = method.loads(text)
    assert m.boxes[1].load == ("STWCTBL,J22[HRsm2CD5m2ND10r]10",)
    assert len(m.warnings) == 1
    assert "boxes[1].load[0]" in m.warnings[0]
    assert "whitespace" in m.warnings[0]


def test_warnings_do_not_affect_equality_or_the_dumped_text() -> None:
    text = SAMPLE.replace(
        '"STWCTBL,J22[HRsm2CD5m2ND10r]10"', '"STWCTBL,J22[HRsm2CD5m2ND10r]10 "'
    )
    stripped = method.loads(text)
    clean = method.loads(SAMPLE)
    assert stripped == clean
    assert method.dumps(stripped) == method.dumps(clean)
    assert method.loads(method.dumps(stripped)).warnings == ()


def test_save_then_load_round_trips(tmp_path) -> None:
    m = method.loads(SAMPLE)
    path = tmp_path / "method.toml"
    method.save(m, str(path))
    assert method.load(str(path)) == m


def test_dumps_is_deterministic() -> None:
    m = method.loads(SAMPLE)
    assert method.dumps(m) == method.dumps(m)


def test_stamp_fields() -> None:
    m = method.loads(SAMPLE)
    s = method.stamp(m, console_version="1.2.3")
    assert s["method_name"] == "smoke-test"
    assert s["method_text"] == method.dumps(m)
    assert s["method_hash"] == method.stamp(m, console_version="1.2.3")["method_hash"]
    assert len(s["method_hash"]) == 64
    assert s["console_version"] == "1.2.3"

    import clockwork

    assert s["clockwork_version"] == clockwork.__version__


def test_stamp_hash_changes_with_content() -> None:
    m = method.loads(SAMPLE)
    other = SAMPLE.replace('name = "smoke-test"', 'name = "different"', 1)
    m2 = method.loads(other)
    assert method.stamp(m)["method_hash"] != method.stamp(m2)["method_hash"]


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("repetition_mode", 'repetition_mode = "single_frame"'),
        ("keep_raw", "keep_raw = false"),
        ("start", 'start = [["box1", "TBLSTRT"], ["box2", "TARBTRG"]]'),
        ("reset", "reset = []"),
        ("arm", 'arm = ["SMOD,ONCE"]'),
        ("file_stem", 'file_stem = "smoke-test"\n'
                      'enable = { box = "box1", channel = "A" }'),
    ],
)
def test_stamp_hash_covers_every_new_field(field: str, replacement: str) -> None:
    """A field the hash does not cover is a field a stamp silently forgets."""
    original = next(line for line in SAMPLE.splitlines() if line.startswith(field))
    changed = SAMPLE.replace(original, replacement, 1)
    assert changed != SAMPLE
    m, m2 = method.loads(SAMPLE), method.loads(changed)
    assert method.stamp(m)["method_hash"] != method.stamp(m2)["method_hash"]


def test_stamp_console_version_defaults_to_none() -> None:
    m = method.loads(SAMPLE)
    assert method.stamp(m)["console_version"] is None


@pytest.mark.parametrize(
    "broken,expected_fragment",
    [
        (SAMPLE.replace("schema_version = 2", "schema_version = 3"), "schema_version"),
        (SAMPLE.replace('name = "smoke-test"\n', "", 1), "metadata.name"),
        (SAMPLE.replace("frames = 1", "frames = 0"), "acquisition.frames"),
        (SAMPLE.replace('file_stem = "smoke-test"', 'file_stem = "a/b"'), "file_stem"),
        (SAMPLE.replace('name = "box2"', 'name = "box1"'), "duplicate box name"),
        (SAMPLE.replace('name = "box2"', 'name = "box2 "'), "whitespace"),
        (SAMPLE.replace('"per_repetition"', '"per-repetition"'), "repetition_mode"),
        (SAMPLE.replace("keep_raw = true", 'keep_raw = "yes"'), "keep_raw"),
        (SAMPLE.replace("scans = 100", "scan = 100"), "acquisition.scan"),
        (SAMPLE.replace('port = "COM4"', 'prt = "COM4"'), "boxes[1].prt"),
        (
            SAMPLE.replace('load = ["STWCTBL,J22[HRsm2CD5m2ND10r]10"]', "load = []").replace(
                'load = ["STBLDAT;25:[A:10,10:A:1,25:A:0,100:];"]', "load = []"
            ),
            "no box has anything to load",
        ),
        (SAMPLE.replace('["box2", "TARBTRG"]', '["box9", "TARBTRG"]'), "no box named 'box9'"),
        (SAMPLE.replace('["box1", "SMOD,LOC"]', '["box9", "SMOD,LOC"]'), "no box named 'box9'"),
        (SAMPLE.replace('start = [["box2", "TARBTRG"], ["box1", "TBLSTRT"]]', "start = []"),
         "start"),
        (SAMPLE.replace('["box1", "TBLSTRT"]', '["box1", "TBLSTRT", "now"]'), "start[1]"),
        (SAMPLE.replace('file_stem = "smoke-test"',
                        'file_stem = "smoke-test"\n'
                        'enable = { box = "box9", channel = "A" }'),
         "acquisition.enable.box"),
        (SAMPLE.replace('file_stem = "smoke-test"',
                        'file_stem = "smoke-test"\n'
                        'enable = { box = "box1", chanel = "A" }'),
         "acquisition.enable.chanel"),
        (SAMPLE.replace('file_stem = "smoke-test"',
                        'file_stem = "smoke-test"\nenable = "box1:A"'),
         "acquisition.enable"),
    ],
)
def test_validation_rejects(broken: str, expected_fragment: str) -> None:
    with pytest.raises(method.MethodError) as exc_info:
        method.loads(broken)
    assert expected_fragment in str(exc_info.value)


def test_schema_1_is_rejected_with_a_reason() -> None:
    """Schema 1 had one flat `strings` list per box and no start sequence."""
    text = """\
schema_version = 1

[metadata]
name = "old"
created = 2026-09-06

[acquisition]
frames = 1
scans = 100
accumulations = 10
file_stem = "old"

[[boxes]]
name = "box1"
port = "COM3"
strings = ["STBLCLK,EXT", "SMOD,TBL", "TBLSTRT"]
"""
    with pytest.raises(method.MethodError) as exc_info:
        method.loads(text)
    problems = " ".join(exc_info.value.problems)
    assert "expected 2, got 1" in problems
    assert "version 1 is not supported" in problems


def test_start_written_after_the_boxes_is_reported_as_such() -> None:
    """TOML gives a bare key to the table above it, so the message has to say so."""
    text = SAMPLE.replace('start = [["box2", "TARBTRG"], ["box1", "TBLSTRT"]]\n', "")
    text += '\nstart = [["box1", "TBLSTRT"]]\n'
    with pytest.raises(method.MethodError) as exc_info:
        method.loads(text)
    problems = " ".join(exc_info.value.problems)
    assert "boxes[1].start" in problems
    assert "before the first [[boxes]] table" in problems


def test_no_boxes_is_rejected() -> None:
    text = SAMPLE.split("[[boxes]]")[0]
    with pytest.raises(method.MethodError) as exc_info:
        method.loads(text)
    assert "boxes" in str(exc_info.value)


def test_every_problem_is_reported_at_once() -> None:
    broken = (
        SAMPLE.replace("frames = 1", "frames = 0")
        .replace('file_stem = "smoke-test"', 'file_stem = "a/b"')
        .replace('"per_repetition"', '"whenever"')
    )
    with pytest.raises(method.MethodError) as exc_info:
        method.loads(broken)
    assert len(exc_info.value.problems) == 3


def test_invalid_toml_reports_as_method_error() -> None:
    with pytest.raises(method.MethodError):
        method.loads("this is not [ valid toml")


# --- declared DC bias and RF (task 40) -------------------------------------------------

DECLARED = """
[boxes.dc_bias]
16 = 5.0
15 = 0.0

[boxes.rf.1]
frequency_hz = 943000
drive_pct = 50.0
mode = "manual"

[boxes.rf.2]
drive_pct = 30
"""


def declaring() -> method.Method:
    """The sample method with AUKLET's analog state declared on its first box."""
    first, _, rest = SAMPLE.partition("\n[[boxes]]\nname = \"box2\"")
    return method.loads(first + DECLARED + "\n[[boxes]]\nname = \"box2\"" + rest)


def test_a_box_may_declare_dc_bias_and_rf() -> None:
    box = declaring().box("box1")
    assert box.dc_bias == ((15, 0.0), (16, 5.0))
    assert box.rf[0] == method.RfChannel(channel=1, frequency_hz=943000,
                                         drive_pct=50.0, mode="MANUAL")
    assert box.rf[1] == method.RfChannel(channel=2, drive_pct=30.0)


def test_a_declaration_comes_out_as_setter_strings_in_channel_order() -> None:
    """Two decimals, which is what the box reports every one of these back at, so a
    send log shows the declared value and the readback in the same shape (§8.2)."""
    assert method.declared_commands(declaring().box("box1")) == (
        "SDCB,15,0.00", "SDCB,16,5.00",
        "SRFFRQ,1,943000", "SRFDRV,1,50.00", "SRFMODE,1,MANUAL",
        "SRFDRV,2,30.00",
    )


def test_a_box_declaring_nothing_produces_no_strings() -> None:
    assert method.declared_commands(method.loads(SAMPLE).box("box1")) == ()


def test_a_declaration_round_trips() -> None:
    declared = declaring()
    assert method.loads(method.dumps(declared)) == declared


def test_a_method_that_declares_nothing_writes_neither_key() -> None:
    """The rule `acquisition.enable` already follows: a document that says nothing
    about the analog state hashes exactly as it did before the keys existed."""
    plain = method.loads(SAMPLE)
    assert "dc_bias" not in method.to_dict(plain)["boxes"][0]
    assert "rf" not in method.to_dict(plain)["boxes"][0]
    assert method.stamp(plain)["method_hash"] == method.stamp(method.loads(SAMPLE))["method_hash"]


def test_declaring_changes_the_hash() -> None:
    assert method.stamp(declaring())["method_hash"] \
        != method.stamp(method.loads(SAMPLE))["method_hash"]


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ("[boxes.dc_bias]\nguard = 5.0\n", "not a channel number"),
        ("[boxes.dc_bias]\n0 = 5.0\n", "numbered from 1"),
        ('[boxes.dc_bias]\n16 = "five"\n', "expected a voltage"),
        ("[boxes.dc_bias]\n16 = true\n", "expected a voltage"),
        ("[boxes.rf.1]\nmode = \"SOMETHING\"\n", "MANUAL"),
        ("[boxes.rf.1]\n", "declares no setting"),
        ("[boxes.rf.1]\nfrequency_hz = 943000.5\n", "whole number of hertz"),
        ("[boxes.rf.1]\ndrive_pct = 50.0\nvolts = 5\n", "not a key of schema"),
        ("[boxes.rf.0]\ndrive_pct = 50.0\n", "numbered from 1"),
    ],
)
def test_a_malformed_declaration_is_refused(block: str, expected: str) -> None:
    first, _, rest = SAMPLE.partition("\n[[boxes]]\nname = \"box2\"")
    with pytest.raises(method.MethodError) as exc_info:
        method.loads(first + "\n" + block + "\n[[boxes]]\nname = \"box2\"" + rest)
    assert expected in " ".join(exc_info.value.problems)


def test_a_declaration_is_not_range_checked_here() -> None:
    """The board range-checks every value itself and NAKs one it cannot reach
    (§8.2). A host that guessed the limits would refuse a method the instrument
    would have accepted."""
    first, _, rest = SAMPLE.partition("\n[[boxes]]\nname = \"box2\"")
    loaded = method.loads(first + "\n[boxes.dc_bias]\n16 = -9999.0\n"
                          + "\n[[boxes]]\nname = \"box2\"" + rest)
    assert loaded.box("box1").dc_bias == ((16, -9999.0),)

