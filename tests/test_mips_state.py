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
    Box,
    BoxState,
    FakeBox,
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
        box.command(f"SWFREQ,{module},{14914 if module == 2 else 10019}")
    state = read_state(box)
    assert state.modules == (1, 2, 3, 4)
    assert set(state.module(2)) == set(ARB_MODULE_GETTERS)
    assert state.module(2)["GWFREQ"] == "14914"
    assert state.module(1)["GWFREQ"] == "10019"


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
