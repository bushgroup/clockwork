"""The method-level electrode check: the nets a limits document names, the potential a
method's strings put on each at every moment, and the one refusal when two are too far
apart (lab record, task 159). Synthetic boxes and strings throughout; the instrument's
own methods are checked lab-side."""

from __future__ import annotations

import pytest

from clockwork import electrodes, envelope
from clockwork import method as method_module
from clockwork.electrodes import Unreadable, refusals, worst
from clockwork.envelope import Ledger, LimitsError, check
from clockwork.mips import compile_table, compression_voltages, run_order

# A CLOCK-shaped method: box1 holds the guard on channel 16 and the TW bias on 15 and
# pulses both in its table; box2's four modules ride on the bias at 15 V p-p.
CLOCK_TABLE = ("STBLDAT;0:A:1[A:100,0:B:1,500:B:0,783:15:10,806:16:37,2357:16:5,"
               "2364:15:0,4966:16:-20,4976:16:5,5000:];")


def method_text(table: str = CLOCK_TABLE, *, box2_setup: list[str] | None = None,
                box2_load: list[str] | None = None, bank: str = "15 = 0.0\n16 = 5.0",
                rf: str = "") -> str:
    setup = box2_setup if box2_setup is not None else [
        f"SWFVRNG,{module},15" for module in (1, 2, 3, 4)]
    return f"""\
schema_version = 2
start = [["box1", "TBLSTRT"]]
reset = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]

[metadata]
name = "electrode-test"
created = 2026-11-10

[acquisition]
frames = 1
scans = 5000
accumulations = 100
repetition_mode = "single_frame"
keep_raw = true
file_stem = "electrode-test"
enable = {{ box = "box1", channel = "A" }}

[[boxes]]
name = "box1"
port = "COM3"
setup = ["STBLCLK,EXT"]
load = [{table!r}]
arm = ["SMOD,TBL"]

[boxes.dc_bias]
{bank}
{rf}

[[boxes]]
name = "box2"
port = "COM4"
setup = {setup!r}
load = {box2_load or []!r}
arm = []
"""


def load(text: str) -> method_module.Method:
    return method_module.loads(text)


NETS = [
    {"name": "GUARD", "dc": {"box": "box1", "channel": 16}},
    *({"name": f"TW {module}", "dc": {"box": "box1", "channel": 15},
       "arb": {"box": "box2", "module": module}} for module in (1, 2, 3, 4)),
]


def nets(max_v: float = 60.0, extra: list[dict] | None = None) -> electrodes.Electrodes:
    problems: list[str] = []
    found = electrodes.from_dict({"max_v": max_v, "nets": NETS + (extra or [])}, problems)
    assert problems == [] and found is not None
    return found


# --- the walk and the readers beneath it -------------------------------------------


def test_a_table_walks_in_run_order_with_each_loop_run_twice_at_most():
    compiled = compile_table("STBLDAT;0:A:1[A:100,0:B:1,10:B:0,20:];")
    ticks = [point.count for _, point in run_order(compiled)]
    assert ticks == [0, 0, 10, 20, 0, 10, 20]
    # An inner `]` is followed by a delimiter before the next tick (`Table.cpp`).
    nested = compile_table("STBLDAT;0:[A:3,0:B:1[B:5,0:C:1,5:],10:B:0,20:];")
    order = [(index, point.count) for index, point in run_order(nested)]
    # The outer loop's two passes each run the inner loop's two.
    assert order == [(0, 0), (1, 0), (1, 5), (1, 0), (1, 5), (2, 10), (2, 20)] * 2


def test_a_dc_bias_entry_keeps_the_volts_its_string_wrote():
    compiled = compile_table("STBLDAT;0:16:37.5:33:12.5,10:;")
    entries = compiled.tables[0].points[0].entries
    assert [entry.written for entry in entries] == [37.5, 12.5]
    assert entries[0].value is None


def test_a_compression_table_says_which_ranges_it_sets_and_whether_it_swaps():
    assert compression_voltages("SARBCTBL,J10[HRsm1CD12m1ND4.0272r]100") == ({}, False)
    assert compression_voltages("SARBCTBL,V20.5v40L30[HRM1C]1") == (
        {1: (20.5,), 2: (40.0,), 3: (30.0,)}, True)
    # `m1N` is a module's mode, not a range, and `H` swallows the `V` after it.
    assert compression_voltages("SARBCTBL,HVm1Nl12") == ({4: (12.0,)}, False)


# --- the document ------------------------------------------------------------------


def test_a_bad_electrodes_table_reports_every_problem_at_once():
    problems: list[str] = []
    found = electrodes.from_dict({"max_v": -1, "colour": "red", "nets": [
        {"name": "a", "dc": {"box": "box1", "channel": 40}, "volts": 0},
        {"name": "b", "volts": 0, "rf": {"box": "box1", "channel": 1}},
        {"name": "c", "volts": 0, "arb": {"box": "box2", "module": 1},
         "rf": {"box": "box1", "channel": 1}, "rf_peak_v_per_pct": 2},
        {"name": "a", "volts": 0},
    ]}, problems)
    assert found is None
    joined = " | ".join(problems)
    for expected in ("max_v must be a positive", "electrodes.colour",
                     "'a' needs one base", "channel must be a whole number from 1 to 32",
                     "'b' gives rf and rf_peak_v_per_pct together or neither",
                     "'c' carries both arb and rf", "names the net 'a' more than once"):
        assert expected in joined


def test_one_net_is_not_a_difference():
    problems: list[str] = []
    assert electrodes.from_dict({"max_v": 10, "nets": [{"name": "a", "volts": 0}]},
                                problems) is None
    assert "at least two nets" in problems[0]


# --- the check ---------------------------------------------------------------------


def test_the_widest_moment_is_the_guard_against_the_lowest_pad_at_its_activation():
    spread = worst(load(method_text()), nets())
    assert spread is not None
    assert (spread.high, spread.high_v, spread.low_v, spread.volts) == (
        "GUARD", 37.0, 2.5, 34.5)
    assert spread.when == "at tick 806 of box1's table"
    assert refusals(load(method_text()), nets(max_v=34.5)) == []


def test_a_method_past_the_maximum_is_refused_in_one_sentence_naming_both_nets():
    unsafe = CLOCK_TABLE.replace("806:16:37", "806:16:120")
    found = refusals(load(method_text(unsafe)), nets())
    assert found == ["GUARD reaches 120 V and TW 1 2.5 V at tick 806 of box1's table, "
                     "117.5 V apart, more than the 60 V the standing limits allow "
                     "between any two electrodes"]


def test_a_one_tick_pulse_counts_as_much_as_a_hold():
    clearing = CLOCK_TABLE.replace("4966:16:-20", "4966:16:-80")
    spread = worst(load(method_text(clearing)), nets())
    assert spread is not None and spread.low == "GUARD" and spread.low_v == -80.0
    assert spread.volts == 87.5 and spread.when == "at tick 4966 of box1's table"


def test_a_state_only_the_second_pass_of_a_loop_reaches_is_counted():
    # The bias falls at tick 10 and the guard rises at tick 0, so only a second pass
    # starts with both.
    table = "STBLDAT;0:[A:3,0:16:40,10:16:0:15:-40,20:];"
    spread = worst(load(method_text(table, bank="15 = 0.0\n16 = 0.0")), nets())
    assert spread is not None and spread.volts == 87.5
    once = table.replace("[A:3", "[A:1")
    assert worst(load(method_text(once, bank="15 = 0.0\n16 = 0.0")), nets()).volts == 47.5


def test_the_alternate_range_and_the_compression_table_widen_a_module():
    setup = ["SWFVRNG,1,15", "SWFVRNG,2,15", "SALTRNG,1,60", "SALTRENA,1,TRUE"]
    # Module 1's own pads, 60 V p-p, are the widest pair at rest.
    spread = worst(load(method_text(box2_setup=setup)), nets())
    assert spread is not None and (spread.high, spread.low) == ("TW 1", "TW 1")
    assert spread.volts == 60.0
    # An alternate range that is never enabled does not count.
    quiet = worst(load(method_text(box2_setup=setup[:3])), nets())
    assert quiet is not None and quiet.low_v == 2.5
    # The compressor's `v` sets module 2, and `M1` lets module 1 run at module 2's level.
    swapping = worst(load(method_text(box2_setup=setup[:2],
                                      box2_load=["SARBCTBL,v50[HRM1C]1"])), nets())
    assert swapping is not None and swapping.low_v == 10 - 25


def test_offsets_count_at_every_value_the_method_gives_them():
    setup = ["SWFVRNG,1,15", "SWFVOFF,1,-30", "SARBOFFB,1,5"]
    spread = worst(load(method_text(box2_setup=setup)), nets())
    assert spread is not None and spread.low == "TW 1"
    assert spread.low_v == 10 - 30 + 0 - 7.5


def test_an_rf_head_rides_on_its_base_at_its_drive():
    rf = "\n[boxes.rf.1]\ndrive_pct = 50.0\nmode = \"MANUAL\""
    head = {"name": "RF1", "volts": 0, "rf": {"box": "box1", "channel": 1},
            "rf_peak_v_per_pct": 2.0}
    spread = worst(load(method_text(rf=rf)), nets(extra=[head]))
    assert spread is not None and (spread.high, spread.low) == ("RF1", "RF1")
    assert spread.volts == 200.0
    assert refusals(load(method_text(rf=rf)), nets(max_v=150, extra=[head]))[0].startswith(
        "RF1 spans -100 to 100 V")


def test_a_net_the_method_never_sets_is_left_to_the_cold_start_check():
    # No bank and no table event on 16: the guard is whatever the box holds.
    table = "STBLDAT;0:[A:1,0:15:10,10:];"
    spread = worst(load(method_text(table, bank="15 = 0.0")), nets())
    assert spread is not None and "GUARD" not in (spread.high, spread.low)
    nothing = worst(load(method_text(table, bank="15 = 0.0", box2_setup=[])), nets())
    assert nothing is None


def test_a_table_the_check_cannot_read_or_model_is_refused():
    ramp = "STBLDAT;0:[A:1,0:144:2.5,10:];"
    with pytest.raises(Unreadable, match="ramps DC bias channel 16"):
        worst(load(method_text(ramp)), nets())
    found = refusals(load(method_text(ramp)), nets())
    assert len(found) == 1 and found[0].endswith(
        "cannot be checked against the standing limits")


# --- through the standing limits ---------------------------------------------------

LIMITS = """\
schema_version = 1

[allow]
boxes = ["box1", "box2"]
hand_written = true

[budget]
max_runs = 3
max_replicates_per_run = 2
max_hours = 8

[electrodes]
max_v = 60.0
nets = [
  { name = "GUARD", dc = { box = "box1", channel = 16 } },
  { name = "TW 1", dc = { box = "box1", channel = 15 }, arb = { box = "box2", module = 1 } },
]
"""


def test_the_standing_limits_refuse_an_unsafe_hand_written_method():
    limits = envelope.loads(LIMITS)
    assert limits.electrodes is not None and limits.electrodes.max_v == 60.0
    safe = load(method_text())
    assert check(safe, None, limits, Ledger(), real=True) == []
    unsafe = load(method_text(CLOCK_TABLE.replace("806:16:37", "806:16:120")))
    found = check(unsafe, None, limits, Ledger(), real=True)
    assert len(found) == 1 and found[0].startswith("GUARD reaches 120 V and TW 1 2.5 V")


def test_a_bad_electrodes_table_is_a_problem_in_the_limits():
    with pytest.raises(LimitsError, match="electrodes.max_v"):
        envelope.loads(LIMITS.replace("max_v = 60.0", "max_v = 0"))


# --- the RF drive caps -------------------------------------------------------------

# Array-of-tables, so the caps may follow any other table in the document.
RF_CAPS = """
[[rf_drive]]
box = "box1"
channel = 1
max_pct = 50.0

[[rf_drive]]
box = "box1"
channel = 2
max_pct = 30.0
"""


def rf_bank(*heads: tuple[int, float, str]) -> str:
    return "".join(f'\n[boxes.rf.{channel}]\ndrive_pct = {drive}\nmode = "{mode}"\n'
                   for channel, drive, mode in heads)


def test_rf_heads_at_the_drives_the_limits_name_are_sent():
    limits = envelope.loads(LIMITS + RF_CAPS)
    assert [(cap.channel, cap.max_pct) for cap in limits.rf_drive] == [(1, 50.0), (2, 30.0)]
    tested = load(method_text(rf=rf_bank((1, 50.0, "MANUAL"), (2, 30.0, "MANUAL"))))
    assert check(tested, None, limits, Ledger(), real=True) == []
    # Below the cap is within it, and a method that sets no drive is the cold-start
    # check's to judge.
    lower = load(method_text(rf=rf_bank((1, 20.0, "MANUAL"))))
    assert check(lower, None, limits, Ledger(), real=True) == []
    assert check(load(method_text()), None, limits, Ledger(), real=True) == []


def test_an_rf_drive_past_its_cap_is_refused_wherever_the_method_sets_it():
    limits = envelope.loads(LIMITS + RF_CAPS)
    declared = load(method_text(rf=rf_bank((1, 60.0, "MANUAL"))))
    assert check(declared, None, limits, Ledger(), real=True) == [
        "the method drives box1 RF 1 at 60%, more than the 50% the standing limits allow"]
    # A setter string and a table event (channel 34 is RF 2) count the same.
    by_string = method_text().replace('setup = ["STBLCLK,EXT"]',
                                      'setup = ["STBLCLK,EXT", "SRFDRV,2,45"]')
    assert "box1 RF 2 at 45%" in check(load(by_string), None, limits, Ledger(), real=True)[0]
    by_table = CLOCK_TABLE.replace("806:16:37", "806:16:37:34:31")
    assert "box1 RF 2 at 31%" in check(load(method_text(by_table)), None, limits, Ledger(),
                                       real=True)[0]


def test_auto_mode_and_a_head_the_caps_do_not_name_are_refused():
    limits = envelope.loads(LIMITS + RF_CAPS)
    auto = load(method_text(rf=rf_bank((1, 50.0, "AUTO"))))
    assert check(auto, None, limits, Ledger(), real=True) == [
        "the method puts box1 RF 1 in AUTO, where the box chooses the drive, and the "
        "standing limits hold it at 50% or less"]
    unnamed = load(method_text(rf=rf_bank((3, 10.0, "MANUAL"))))
    assert "box1 RF 3, which the standing limits' rf_drive does not name" in check(
        unnamed, None, limits, Ledger(), real=True)[0]


def test_a_bad_rf_drive_list_reports_every_problem_at_once():
    with pytest.raises(LimitsError) as caught:
        envelope.loads(LIMITS.replace("[allow]", """rf_drive = [
  { box = "box1", channel = 5, max_pct = 50.0 },
  { box = "box1", channel = 1, max_pct = 120.0, colour = "red" },
  { box = "box1", channel = 2, max_pct = 30.0 },
  { box = "box1", channel = 2, max_pct = 20.0 },
]

[allow]"""))
    joined = " | ".join(caught.value.problems)
    for expected in ("rf_drive[0].channel must be a whole number from 1 to 4",
                     "rf_drive[1].colour", "rf_drive[1].max_pct must be a percent",
                     "names box1 RF 2 more than once"):
        assert expected in joined
