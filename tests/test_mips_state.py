"""The state readback: what a box is holding, read with getters and nothing else.

Task 40's `clockwork.mips.state`, against `FakeBox`. What is checked here is what
the instrument day showed matters: that the readback sends only what the box says
it has, that a rejection does not shift every answer after it, that the setpoint
and the monitor stay two different numbers, and that `GRFALL` is parsed with the
four fields per channel the firmware prints rather than the three its help text
promises (wire format §8.3).
"""

from __future__ import annotations

import logging

import pytest

from clockwork import transcript
from clockwork.mips import (
    ARB_MODULE_GETTERS,
    ARB_POINTS_PER_PERIOD,
    Box,
    BoxState,
    FakeBox,
    arb_frequency,
    arb_points_per_period,
    read_state,
)

# AUKLET's bank and heads, as read on 2026-09-15 (lab record, task 40).
AUKLET_BIASES = [99.0, 89.0, 60.0, 54.0, 31.0, 28.0, 25.0, 5.0,
                 26.0, 5.0, -5.0, -70.0, -72.0, -40.0, 0.0, 5.0]


def auklet() -> Box:
    """A stand-in set to what the instrument's DC bias and RF box was holding."""
    fake = FakeBox(name="MIPS-A", version="1.211t, Nov 4, 2021",
                   dcb_channels=16, rf_channels=2)
    fake.dc_bias = list(AUKLET_BIASES)
    fake.dc_bias_error = -0.03
    fake.rf[1].update({"SRFFRQ": "943000", "SRFDRV": "50.00", "GRFPPVP": "239.97",
                       "GRFPPVN": "233.85", "GRFPWR": "16.92"})
    fake.rf[2].update({"SRFFRQ": "804000", "SRFDRV": "30.00", "GRFPPVP": "98.77",
                       "GRFPPVN": "128.43", "GRFPWR": "4.15"})
    return Box(transport=fake, name="auklet")


# --- the listing -----------------------------------------------------------------------


def test_gcmds_comes_back_as_the_set_of_command_names() -> None:
    listing = Box(transport=FakeBox()).command_listing(settle=0.05, limit=2.0)
    assert "GDCBALL" in listing and "GVER" in listing
    assert all(" " not in name for name in listing)


def test_a_getter_the_listing_does_not_name_is_never_sent() -> None:
    """The trap this module exists for (§8.4).

    A getter this firmware lacks is rejected, and a rejection shifts every answer
    after it while leaving each one plausible. Filtering on the box's own listing
    is what stops a readback inventing a record of an instrument.
    """
    box = auklet()
    state = read_state(box, listing=frozenset({"GVER", "GNAME"}))
    assert state.version and state.identity
    assert not state.refused
    assert "GDCBALL" in state.skipped and "GRFALL" in state.skipped
    written = b"".join(box.transport.written)
    assert b"GDCBALL" not in written and b"GRFALL" not in written


def test_a_box_that_will_not_list_its_commands_says_so() -> None:
    """An empty listing is not "the box has everything": the record says which."""
    state = read_state(auklet(), listing=frozenset())
    assert not state.listed
    assert "GCMDS would not answer" in state.render()


# --- DC bias ---------------------------------------------------------------------------


def test_the_bank_comes_back_channel_by_channel_in_order() -> None:
    state = read_state(auklet())
    assert list(state.dc_bias_setpoints) == AUKLET_BIASES
    assert state.dc_bias(16) == 5.0
    assert state.dc_bias(15) == 0.0
    assert state.count("DCB") == 16


def test_the_setpoint_and_the_monitor_stay_two_different_numbers() -> None:
    """`GDCBALL` is what the box was told and `GDCBALLV` is what it measures.

    They never agree exactly on a real board, and a readback that collapsed them
    would lose the only evidence a file carries that a channel is following its
    setpoint at all (§8.2).
    """
    state = read_state(auklet())
    assert state.dc_bias(1) == 99.0
    assert state.dc_bias_readback(1) == pytest.approx(98.97)
    assert "99.00 V" in state.render() and "98.97 V" in state.render()


def test_a_channel_the_box_does_not_have_reads_as_nothing() -> None:
    state = read_state(auklet())
    assert state.dc_bias(17) is None
    assert state.dc_bias(0) is None


# --- RF --------------------------------------------------------------------------------


def test_grfall_is_four_fields_a_channel_not_three() -> None:
    """The help text and the firmware's own dispatch comment are both wrong (§8.3).

    A parser written from either is off by one field per channel from the first
    channel on, and every number it reports is a real number in the wrong place.
    """
    state = read_state(auklet())
    first, second = state.rf
    assert (first.channel, first.frequency_hz, first.drive_pct) == (1, 943000.0, 50.0)
    assert (first.peak_positive_v, first.peak_negative_v) == (239.97, 233.85)
    assert (second.channel, second.frequency_hz, second.drive_pct) == (2, 804000.0, 30.0)


def test_the_mode_and_the_power_come_from_their_own_getters() -> None:
    """`GRFALL` reports neither, and `MANUAL` is what a hand-set head looks like."""
    state = read_state(auklet())
    assert [reading.mode for reading in state.rf] == ["MANUAL", "MANUAL"]
    assert state.rf[0].power_w == 16.92


def test_a_frequency_is_rendered_in_whole_hertz() -> None:
    """Not `1e+06`: these are read beside a front panel that shows integers."""
    assert "943000 Hz" in read_state(auklet()).render()


def test_a_box_with_no_rf_board_reports_no_channels() -> None:
    state = read_state(Box(transport=FakeBox(dcb_channels=16), name="plain"))
    assert state.rf == ()
    assert state.count("RF") == 0


# --- ARB modules -----------------------------------------------------------------------


def test_every_module_answers_the_per_module_getters() -> None:
    fake = FakeBox(name="MIPS-B", arb_modules=4, dcb_channels=0)
    box = Box(transport=fake, name="bufflehead")
    for module in (1, 2, 3, 4):
        # Requested, not read back: the module quantises 15000 to the 14914 its divider
        # can make, which is what both ARB boxes answer on the instrument (wire format
        # 6.2, lab record, task 56).
        box.command(f"SWFREQ,{module},{15000 if module == 2 else 10000}")
    state = read_state(box)
    assert state.modules == (1, 2, 3, 4)
    assert set(state.module(2)) == set(ARB_MODULE_GETTERS)
    assert state.module(2)["GWFREQ"] == "14914"
    assert state.module(1)["GWFREQ"] == "9943"


def test_a_box_with_no_arb_modules_costs_no_per_module_round_trips() -> None:
    box = auklet()
    read_state(box)
    written = b"".join(box.transport.written)
    assert b"GWFREQ" not in written


def test_an_empty_compression_table_reads_as_empty_rather_than_timing_out() -> None:
    """`GARBCTBL` on a box with nothing loaded answers ACK and an empty line (§1).

    A framer that threw empty lines away reported that as silence, which is a
    timeout where the box replied promptly and correctly.
    """
    state = read_state(Box(transport=FakeBox(arb_modules=4), name="arb"))
    assert state.values["GARBCTBL"] == ""
    assert "(empty)" in state.render()


# --- refusals --------------------------------------------------------------------------


def test_a_refusal_is_recorded_and_does_not_shift_what_follows() -> None:
    """The measured failure (§8.4): after a rejection, the next getter read this
    one's leftover and every answer after it was wrong and plausible."""
    box = auklet()
    # A listing naming a command the stand-in does not implement, so the readback
    # sends it, is refused, and has to recover before the next one.
    listing = box.command_listing(settle=0.05, limit=2.0) | {"GBOGUS"}
    state = read_state(box, listing=listing)
    assert list(state.dc_bias_setpoints) == AUKLET_BIASES
    assert state.rf[0].frequency_hz == 943000.0


def test_a_state_with_nothing_in_it_still_renders_its_name() -> None:
    assert BoxState(name="auklet").render().startswith("auklet")


# --- the send log ----------------------------------------------------------------------


class Collected(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_a_readback_leaves_the_wire_transcript_full_and_the_send_log_empty() -> None:
    """Forty-odd getters are evidence, not something a trainee reads (task 40).

    The invariant the send log rests on is that everything in it is in the
    transcript too. This is the one direction that is allowed: traffic in the
    transcript and not in the send log, with the block the caller writes standing
    for it.
    """
    logger = logging.getLogger(transcript.ROOT_LOGGER)
    handler, level = Collected(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        read_state(auklet())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    assert any("GDCBALL" in record.getMessage() for record in handler.records)
    assert not [record for record in handler.records
                if getattr(record, "sent", None) is not None]


def test_summarised_nests_and_puts_logging_back() -> None:
    box = auklet()
    with box.summarised():
        with box.summarised():
            assert box._marking({"sent": 1}) is None
        assert box._marking({"sent": 1}) is None
    assert box._marking({"sent": 1}) == {"sent": 1}


# --- the ARB waveform divider ---------------------------------------------------------


def test_the_frequency_a_module_can_make_is_the_one_the_instrument_reads_back():
    """The number the 2026-09-17 sitting saw on all eight modules of both ARB boxes.

    `SWFREQ,n,15000` in TWAVE mode at 32 points per period: the firmware takes
    `42000000 / (2 * 32 * 15000) + 1 = 44` as its divider and reports
    `42000000 / (2 * 32 * 44) = 14914`. Not a failed send (wire format 6.2).
    """
    assert arb_frequency(15000) == 14914
    assert arb_frequency(15000, ARB_POINTS_PER_PERIOD, mode="TWAVE") == 14914


def test_the_divider_is_the_firmware_s_own_integer_arithmetic():
    """Restated here from `SetFrequency` rather than taken on trust, because integer
    division is the whole of it and a float would give 15000 back."""
    for requested, period in ((15000, 32), (10000, 32), (15000, 96), (4000, 8)):
        divider = 42_000_000 // (2 * period * requested) + 1
        assert arb_frequency(requested, period) == 42_000_000 // (2 * period * divider)


def test_arb_mode_divides_without_the_points_per_period():
    """`SetFrequency` has two branches and only the TWAVE one multiplies by `ppp`."""
    assert arb_frequency(15000, mode="ARB") != arb_frequency(15000, mode="TWAVE")
    assert arb_frequency(15000, mode="ARB") == 42_000_000 // (
        2 * (42_000_000 // (2 * 15000) + 1))


def test_a_request_no_divider_can_be_computed_for_is_refused_rather_than_guessed():
    assert arb_frequency(0) is None
    assert arb_frequency(-15000) is None


def test_the_points_per_period_behind_a_reading_is_recovered_or_refused():
    """`SARBPPP` is not read back, so a module at some other points per period is
    recognised by back-solving rather than reported as a disagreement -- and a frequency
    no points per period explains is the disagreement that is real."""
    assert arb_points_per_period(15000, arb_frequency(15000, 96)) is not None
    assert arb_points_per_period(15000, 5000) is None


def test_the_stand_in_quantises_a_frequency_the_way_a_module_does():
    """A stand-in that answered whatever it was told is a stand-in no desk run can meet
    the eight standing disagreements on, which is how they reached the instrument."""
    box = Box(transport=FakeBox(name="MIPS-A", arb_modules=1), name="t")
    box.command("SWFREQ,1,15000")
    assert read_state(box).module(1)["GWFREQ"] == "14914"
