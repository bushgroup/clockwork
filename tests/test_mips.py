"""Framing, the compiled-table oracle, and the sender, all without a box.

What these can and cannot establish is worth being clear about. The framing and
the plumbing are checked against `docs/mips-wire-format.md` and hold whatever a
real box does. The compiled-table prediction in `clockwork.mips.table` is a
model of the firmware's parser, and `FakeBox` answers `TBLRPT` from that same
model, so a green round trip here proves the machinery around the oracle and
not the oracle itself. Only a bench box settles that (lab record, task 04).
"""

import time

import pytest

from clockwork.mips import (
    DEFAULT_CHUNK_BYTES,
    DEFAULT_CHUNK_GAP_S,
    RING_BUFFER_BYTES,
    TOKEN_TIMEOUT_S,
    Box,
    BoxRejected,
    BoxTimeout,
    FakeBox,
    Kind,
    ResponseReader,
    TableEvent,
    TableSyntaxError,
    ValueKind,
    compile_table,
    compression_passes,
    decode,
    differences,
    digital_events,
    dio_command,
    encode,
    error_text,
    parse_report,
    table_event,
)

# The example in the firmware's own comment block, and the one in the wire
# format document: a 25-tick delay, then a ten-cycle loop of period 100.
EXAMPLE = "STBLDAT;25:[A:10,10:A:1,25:A:0:5:34.5,100:];"


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def test_set_style_ack_is_an_ack_and_a_blank() -> None:
    """The LF-CR terminator leaves an empty line, which is framed and skipped."""
    assert [t.kind for t in ResponseReader().feed(b"\x06\n\r")] \
        == [Kind.ACK, Kind.BLANK]


def test_get_style_reply_is_an_ack_then_a_line() -> None:
    tokens = ResponseReader().feed(b"\x061.263, June 20, 2026\r\n")
    assert [t.kind for t in tokens] == [Kind.ACK, Kind.LINE]
    assert tokens[1].text == "1.263, June 20, 2026"


def test_nak_swallows_its_question_mark() -> None:
    assert [t.kind for t in ResponseReader().feed(b"\x15?\n\r")] \
        == [Kind.NAK, Kind.BLANK]


def test_nak_swallows_its_question_mark_across_a_split_read() -> None:
    reader = ResponseReader()
    assert [t.kind for t in reader.feed(b"\x15")] == [Kind.NAK]
    assert [t.kind for t in reader.feed(b"?\n\r")] == [Kind.BLANK]


def test_status_lines_survive_their_doubled_newline() -> None:
    tokens = ResponseReader().feed(b"TBLRDY\n\r\nTBLTRIG\n\r\n")
    assert [t.text for t in tokens if t.kind is Kind.LINE] \
        == ["TBLRDY", "TBLTRIG"]


def test_a_line_split_across_reads_is_one_token() -> None:
    reader = ResponseReader()
    assert reader.feed(b"TBLCM") == []
    assert reader.partial() == "TBLCM"
    assert [t.text for t in reader.feed(b"PLT\n\r\n") if t.kind is Kind.LINE] \
        == ["TBLCMPLT"]


@pytest.mark.parametrize(
    ("line", "event"),
    [
        ("TBLRDY", TableEvent.READY),
        ("TBLTRIG", TableEvent.TRIGGERED),
        ("TBLCMPLT", TableEvent.COMPLETE),
        ("ABORTED", TableEvent.ABORTED),
        ("ABORTED by user", TableEvent.ABORTED),
        ("Table stoped by user", TableEvent.STOPPED),
    ],
)
def test_status_lines_are_recognised(line: str, event: TableEvent) -> None:
    assert table_event(line) is event


def test_a_value_is_not_a_status_line() -> None:
    assert table_event("1.263, June 20, 2026") is None


def test_error_codes_degrade_gracefully() -> None:
    assert error_text(8) == "timed out waiting for a token"
    assert error_text(9999) == "error 9999"


# --------------------------------------------------------------------------
# The compiled table
# --------------------------------------------------------------------------


def test_a_leading_offset_costs_a_whole_table() -> None:
    compiled = compile_table(EXAMPLE)
    assert len(compiled.tables) == 2
    delay, loop = compiled.tables
    assert (delay.name, delay.repeat, delay.max_count) == (0xFF, 1, 25)
    assert delay.points == (delay.points[0],) and delay.points[0].entries == ()
    assert (chr(loop.name), loop.repeat, loop.max_count) == ("A", 10, 100)
    assert [p.count for p in loop.points] == [10, 25, 100]
    # 13 + 5, then 13 + (5 + 5) + (5 + 10) + (5 + 5)
    assert compiled.byte_size == 66


def test_the_delay_table_is_not_counted_as_loaded() -> None:
    # `TablesLoaded` counts tables that ended, and the delay table never does.
    compiled = compile_table(EXAMPLE)
    assert len(compiled.tables) == 2
    assert compiled.tables_loaded == 1


def test_a_zero_offset_does_not_make_a_delay_table() -> None:
    compiled = compile_table("STBLDAT;0:[A:4,0:A:1,20000:];")
    assert len(compiled.tables) == 1
    assert compiled.tables[0].max_count == 20000


def test_dc_bias_channels_are_stored_zero_based_and_opaque() -> None:
    entry = compile_table("STBLDAT;0:[A:1,10:5:34.5,100:];").tables[0].points[0].entries[0]
    assert entry.chan == 4
    assert entry.kind is ValueKind.DAC
    assert entry.value is None


def test_digital_outputs_store_the_value_character() -> None:
    entry = compile_table("STBLDAT;0:[A:1,10:A:1,100:];").tables[0].points[0].entries[0]
    assert (entry.chan, entry.kind, entry.value) == (ord("A"), ValueKind.CHAR, ord("1"))


def test_sdio_moves_the_line_in_local_mode() -> None:
    fake = FakeBox()
    box = Box(transport=fake, name="dunlin")
    box.set_dio("A", True)
    assert fake.dio_image["A"] is True
    assert fake.dio_pins["A"] is True


def test_sdio_in_table_mode_is_acked_and_does_not_move_the_line() -> None:
    """Measured on a box, and the firmware says why: the latch that applies the
    digital-output image is the LDAC pin, which entering table mode hands to the
    table's timer, so a host pulse writes a pin the PIO no longer drives. The write
    is staged rather than lost (lab record, task 26)."""
    fake = FakeBox()
    box = Box(transport=fake, name="dunlin")
    box.send_table("STBLDAT;0:[A:1,0:A:1,100:];")
    box.set_dio("A", True)
    box.arm()
    box.set_dio("A", False)
    assert fake.dio_image["A"] is False
    assert fake.dio_pins["A"] is True


def test_reading_a_digital_output_back_cannot_confirm_it_moved() -> None:
    """`GDIO` answers an output from the image, which is exactly what a pending
    `SDIO` changed, so it reports the write as applied while the pin has not moved."""
    fake = FakeBox()
    box = Box(transport=fake, name="dunlin")
    box.send_table("STBLDAT;0:[A:1,0:A:1,100:];")
    box.set_dio("A", True)
    box.arm()
    box.set_dio("A", False)
    assert box.command("GDIO,A", value=True) == "0"
    assert fake.dio_pins["A"] is True


def test_a_digital_input_channel_is_refused_before_it_reaches_a_box() -> None:
    """The firmware takes `Q`-`X` and wraps them onto outputs `I`-`P`, so
    `SDIO,Q,1` drives output `I` and reports success. The host has to refuse it."""
    with pytest.raises(ValueError, match="digital input"):
        dio_command("Q", True)
    with pytest.raises(ValueError, match="A to P"):
        dio_command("Z", False)
    with pytest.raises(ValueError, match="A to P"):
        dio_command("AB", True)
    assert dio_command("A", True) == "SDIO,A,1"
    assert dio_command("P", False) == "SDIO,P,0"


def test_the_stand_in_reproduces_the_aliasing_rather_than_hiding_it() -> None:
    """A raw string can still carry one, and then the box does what a box does."""
    fake = FakeBox()
    box = Box(transport=fake, name="dunlin")
    box.command("SDIO,Q,1")
    assert fake.dio_image["I"] is True
    assert fake.dio_image["A"] is False


def test_a_loop_header_is_not_an_event_on_the_line_it_is_named_after() -> None:
    """The trap that cost a bench day: `[A:1,` opens a table *named* `'A'` that runs
    once, and reads exactly like an event raising DIOA. A `per_repetition` table written
    this way drives DIOA precisely once, downwards (lab record, task 33)."""
    compiled = compile_table("STBLDAT;0:[A:1,0:B:1,500:B:0,5001:A:0,5002:];")
    assert compiled.tables[0].label == "'A'"
    assert digital_events(compiled, "A") == ((0, 5001, "0"),)
    assert digital_events(compiled, "B") == ((0, 0, "1"), (0, 500, "0"))


def test_digital_events_reads_a_table_that_does_raise_the_line() -> None:
    compiled = compile_table("STBLDAT;0:[A:1,0:A:1:B:1,500:B:0,5500:A:0,5501:];")
    assert digital_events(compiled, "A") == ((0, 0, "1"), (0, 5500, "0"))


def test_digital_events_names_the_table_each_event_came_from() -> None:
    """A leading offset is a table of its own, so a pre-loop event is table 0 and the
    loop's own tick 0 is table 1; the ticks are each table's own."""
    compiled = compile_table("STBLDAT;0:A:1[A:1,0:B:1,5002:];")
    assert digital_events(compiled, "A") == ((0, 0, "1"),)
    assert digital_events(compiled, "B") == ((1, 0, "1"),)


def test_digital_events_refuses_a_channel_that_is_not_a_digital_output() -> None:
    with pytest.raises(ValueError, match="A to P"):
        digital_events(compile_table(EXAMPLE), "Q")


# --- the compression table, as far as its loop counts ------------------------------------


def test_the_golden_compression_tables_give_up_their_pass_counts() -> None:
    """The third place the accumulation count is written (lab record, task 31). Both of
    the CLOCK method's compression tables end `]100`, which is the `accumulations` its
    `[acquisition]` states and the count its sequencer table loops."""
    assert compression_passes("SARBCTBL,J10[HRsm1CD12m1ND4.0272r]100") == (100,)
    assert compression_passes(
        "SARBCTBL,J30[HRsD90m3CD10m3ND208rD16.7628sD10.0253r]100") == (100,)


def test_a_bare_loop_end_is_one_pass_and_no_loop_at_all_is_none() -> None:
    """`count` defaults to 1 where no digit follows an op, so `]` runs its body once
    (wire format, section 6.6). A table with no loop reports nothing rather than one,
    because "one pass" and "not a loop" are different answers to the caller."""
    assert compression_passes("SARBCTBL,J10[HRr]") == (1,)
    assert compression_passes("SARBCTBL,HRsD90r") == ()


def test_only_the_outermost_loops_are_counted() -> None:
    """An inner loop multiplies the body, not the table's passes."""
    assert compression_passes("SARBCTBL,J10[HR[sD1r]5]100") == (100,)
    assert compression_passes("SARBCTBL,[HRr]2[sr]3") == (2, 3)


def test_an_op_that_swallows_the_next_character_does_not_hide_a_bracket() -> None:
    """Five ops take one raw character after them, and a reader that counted brackets
    without knowing which would read that character as an op. None of the five ever
    takes a bracket, which is why the walk is safe -- and why it has to be a walk."""
    assert compression_passes("SARBCTBL,[HRm1CS]1g]2G]3r]4") == (4,)


def test_a_compression_table_that_cannot_be_read_says_so_rather_than_guessing() -> None:
    """The box syntax-checks nothing on load and skips what it does not know, so an
    unbalanced table is a string this host cannot check rather than one the box would
    reject; the caller warns rather than refusing (lab record, task 31)."""
    with pytest.raises(ValueError, match="unclosed"):
        compression_passes("SARBCTBL,J10[HRsm1CD12r")
    with pytest.raises(ValueError, match="no '\\['"):
        compression_passes("SARBCTBL,J10]100")
    with pytest.raises(ValueError, match="not a SARBCTBL"):
        compression_passes("STBLDAT;0:[A:1,100:];")


def test_arb_channels_store_float_bits() -> None:
    entry = compile_table("STBLDAT;0:[A:1,10:101:12.5,100:];").tables[0].points[0].entries[0]
    assert entry.chan == 101
    assert entry.kind is ValueKind.FLOAT
    assert entry.render_value() == "12.5"


def test_arb_channels_store_the_token_as_written_not_zero_based() -> None:
    # The encoding a 1.262-or-later box uses, pinned because 101-108 carry the
    # INITIAL flag bit and older firmware routes them into the DC bias path,
    # one byte lower and with a DAC frame for a value (docs §2).
    string = "STBLDAT;0:[A:1,10:" + ":".join(f"{c}:1.5" for c in range(101, 109)) + ",100:];"
    entries = compile_table(string).tables[0].points[0].entries
    assert [entry.chan for entry in entries] == list(range(101, 109))
    assert all(entry.kind is ValueKind.FLOAT for entry in entries)
    assert all(entry.render_value() == "1.5" for entry in entries)


def test_flagged_dc_bias_channels_still_take_the_dac_path() -> None:
    # The other side of the same test: INITIAL and RAMP on a real DC bias
    # channel are stored zero-based with an unpredictable DAC value.
    entries = compile_table("STBLDAT;0:[A:1,10:65:1.0:129:0.5,100:];").tables[0].points[0].entries
    assert [(entry.chan, entry.value) for entry in entries] == [(64, None), (128, None)]
    assert all(entry.kind is ValueKind.DAC for entry in entries)


def test_the_trigger_channel_stores_a_signed_int() -> None:
    entry = compile_table("STBLDAT;0:[A:1,10:t:-1,100:];").tables[0].points[0].entries[0]
    assert (entry.chan, entry.kind, entry.value) == (ord("t"), ValueKind.INT, -1)


def test_arb_letter_tokens_are_rejected_with_the_reason() -> None:
    # The firmware parses these into an uninitialised channel byte instead of
    # failing, which is why the wire encoding has to be the numerals.
    with pytest.raises(TableSyntaxError, match="101-108"):
        compile_table("STBLDAT;0:[A:1,10:e:12.5,100:];")


def test_a_closing_bracket_starts_a_fresh_unnamed_table() -> None:
    # `]` ends a table and the parser opens another one behind it, which is
    # how a loop and the events after it concatenate in series.
    compiled = compile_table("STBLDAT;0:[A:2,10:A:1,20:],30:B:1,40:C:0;")
    assert [t.label for t in compiled.tables] == ["'A'", "unnamed"]
    assert [p.count for p in compiled.tables[1].points] == [30, 40]
    assert compiled.nesting == 0


def test_nesting_five_deep_is_accepted() -> None:
    opens = "".join(f"{i * 10}:[{name}:1," for i, name in enumerate("ABCDE"))
    closes = ",".join(f"{60 + i * 10}:]" for i in range(5)) + ";"
    compiled = compile_table("STBLDAT;" + opens + "50:A:1," + closes)
    assert [t.label for t in compiled.tables[:5]] == ["'A'", "'B'", "'C'", "'D'", "'E'"]
    assert compiled.nesting == 0


def test_nesting_deeper_than_five_is_rejected() -> None:
    opens = "".join(f"{i * 10}:[{name}:1," for i, name in enumerate("ABCDEF"))
    with pytest.raises(TableSyntaxError) as caught:
        compile_table("STBLDAT;" + opens + "60:A:1,70:];")
    assert caught.value.error_code == 20


def test_a_closing_bracket_without_an_opening_one_is_rejected() -> None:
    with pytest.raises(TableSyntaxError) as caught:
        compile_table("STBLDAT;0:A:1,10:],20:];")
    assert caught.value.error_code == 21


def test_a_string_that_never_ends_is_a_token_timeout() -> None:
    with pytest.raises(TableSyntaxError) as caught:
        compile_table("STBLDAT;25:[A:10,10:A:1")
    assert caught.value.error_code == 8


def test_encode_and_decode_round_trip() -> None:
    compiled = compile_table(EXAMPLE)
    assert differences(compiled, decode(encode(compiled))) == []


def test_encoded_bytes_are_the_predicted_size_plus_the_marker() -> None:
    compiled = compile_table(EXAMPLE)
    assert len(encode(compiled)) == compiled.byte_size + 1


def test_decode_stops_at_the_marker_and_ignores_stale_bytes() -> None:
    compiled = compile_table(EXAMPLE)
    stale = encode(compiled) + b"\xde\xad\xbe\xef"
    assert decode(stale) == decode(encode(compiled))


def test_decode_needs_a_marker() -> None:
    with pytest.raises(ValueError, match="end-of-tables marker"):
        decode(encode(compile_table(EXAMPLE))[:-1])


def test_differences_notices_a_box_that_parsed_something_else() -> None:
    compiled = compile_table(EXAMPLE)
    other = decode(encode(compile_table("STBLDAT;25:[A:9,10:A:1,25:A:0:5:34.5,100:];")))
    assert differences(compiled, other) == [
        "table 1 ('A'): repeat predicted 10, box has 9"
    ]


def test_a_dac_value_is_compared_only_for_presence() -> None:
    # Two boards calibrated differently hold different bytes for one voltage,
    # and neither is wrong, so the comparison must not look at them.
    compiled = compile_table("STBLDAT;0:[A:1,10:5:34.5,100:];")
    tables = decode(encode(compiled))
    assert differences(compiled, tables) == []
    assert tables[0].points[0].entries[0].value is None


def test_parse_report_reads_the_preamble_and_the_bytes() -> None:
    report = parse_report(
        [
            "TestNesting = 0",
            "TablesLoaded = 1",
            "Size of TableHeader = 13",
            "Size of TableEntryHeader = 5",
            "Size of TableEntry = 5",
            "ff",
            "0",
            "7b",
        ]
    )
    assert (report.test_nesting, report.tables_loaded) == (0, 1)
    assert report.packing_matches
    assert report.data == b"\xff\x00\x7b"


def test_parse_report_rejects_a_reply_that_is_not_one() -> None:
    with pytest.raises(ValueError, match="TestNesting"):
        parse_report(["\x06", "1", "2", "3", "4", "5"])


# --------------------------------------------------------------------------
# The sender
# --------------------------------------------------------------------------


class ScriptedTransport:
    """Replays fixed bytes, so a test can stage what a box would say."""

    def __init__(self, *replies: bytes) -> None:
        self.replies = list(replies)
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written += data

    def read_some(self, timeout: float) -> bytes:
        return self.replies.pop(0) if self.replies else b""

    def close(self) -> None:
        pass


def test_a_status_line_arriving_mid_reply_is_not_read_as_the_reply() -> None:
    # The box re-arms itself under an external trigger, so a TBLRDY can land
    # between a command and its answer.
    box = Box(transport=ScriptedTransport(b"TBLRDY\n\r\n\x06READY\r\n"))
    assert box.table_status() == "READY"
    assert list(box.events) == [TableEvent.READY]


def test_a_silent_box_times_out_rather_than_hanging() -> None:
    box = Box(transport=ScriptedTransport(), timeout=0.05)
    started = time.monotonic()
    with pytest.raises(BoxTimeout):
        box.version()
    assert time.monotonic() - started < 2.0


def test_a_timeout_reports_the_partial_reply() -> None:
    box = Box(transport=ScriptedTransport(b"\x061.26"), timeout=0.05)
    with pytest.raises(BoxTimeout, match="1.26"):
        box.version()


def test_the_box_identifies_itself() -> None:
    box = Box(transport=FakeBox(name="SLIM3-box2", version="1.263, June 20, 2026"))
    assert box.version() == "1.263, June 20, 2026"
    assert box.box_name() == "SLIM3-box2"


def test_the_external_clock_frequency_round_trips() -> None:
    """§4: `SEXTFREQ` declares the rate of the clock on Q; `GEXTFREQ` reads it back.

    It exists so that `TBLCHK` and the idle-task mode have a number to reason
    with, and a box that never hears it still runs the same table at the same
    speed. A rig that clocks a box from a function generator sets it because
    the timing check is worth having, not because the sequence needs it.
    """
    fake = FakeBox()
    box = Box(transport=fake)
    box.command("SEXTFREQ,7752")
    assert fake.ext_freq == 7752
    assert box.command("GEXTFREQ", value=True) == "7752"
    with pytest.raises(BoxRejected) as caught:
        box.command("SEXTFREQ,fast")
    assert caught.value.code == 2


def test_a_rejection_carries_the_reason_the_box_gave() -> None:
    box = Box(transport=FakeBox())
    with pytest.raises(BoxRejected) as caught:
        box.command("NOSUCHCMD")
    assert caught.value.code == 1
    assert "invalid command" in str(caught.value)


def test_going_local_is_idempotent_though_the_command_is_not() -> None:
    """§4: `SMOD,LOC` NAKs with error 3 on a box that is already local.

    Measured on the bench box, which NAKed every `SMOD,LOC` sent to make sure
    of the mode before a load. `local()` exists to establish the mode, so that
    answer is a success.
    """
    fake = FakeBox()
    assert fake.mode == "LOC"
    box = Box(transport=fake)
    box.local()
    box.local()
    assert fake.mode == "LOC"


def test_going_local_still_reports_a_rejection_that_is_not_that_one() -> None:
    fake = FakeBox()
    box = Box(transport=fake)
    box.send_table(EXAMPLE)
    box.arm()
    fake.mode = "TBL"
    # The fake ACKs a real LOC transition, so force a different rejection to
    # be sure only error 3 is being swallowed.
    fake._do_smod = lambda argument: fake._nak(27)  # noqa: SLF001
    with pytest.raises(BoxRejected) as caught:
        box.local()
    assert caught.value.code == 27


def test_an_uncleared_late_nak_becomes_the_next_reply() -> None:
    """The failure `resync` exists to prevent, stated as a test."""
    box = Box(transport=ScriptedTransport(b"\x15?\n\r"), timeout=0.05)
    with pytest.raises(BoxRejected):
        box.version()


def test_resync_drops_a_late_nak_so_the_next_reply_is_its_own() -> None:
    """§4: a table the box cannot parse is NAKed only after a 3-second flush.

    A host that gave up waiting has that NAK still in flight. Left queued it
    becomes the answer to the next command, as the test above shows, and every
    reply after it is off by one -- which is what made the rows after a failure
    meaningless in the first bench sweep.
    """
    box = Box(transport=ScriptedTransport(b"\x15?\n\r"), timeout=0.05)
    box.resync(settle=0.02)
    with pytest.raises(BoxTimeout):
        box.version()


def test_resync_keeps_the_status_lines_it_finds() -> None:
    box = Box(transport=ScriptedTransport(b"ABORTED\n", b"\x15?\n\r"))
    assert box.resync(settle=0.05) == [TableEvent.ABORTED]


def test_arming_without_a_table_is_rejected() -> None:
    box = Box(transport=FakeBox())
    with pytest.raises(BoxRejected) as caught:
        box.arm()
    assert caught.value.code == 5


def test_a_table_string_must_end_with_its_semicolon() -> None:
    box = Box(transport=FakeBox())
    with pytest.raises(ValueError, match="';'"):
        box.send_table("STBLDAT;0:[A:1,10:A:1,100:]")


def test_a_load_reports_what_it_sent_and_what_it_expects() -> None:
    box = Box(transport=FakeBox())
    load = box.send_table(EXAMPLE)
    assert load.bytes_sent == len(EXAMPLE) + 1
    assert load.predicted is not None
    assert load.predicted.byte_size == 66
    assert load.prediction_error is None


def test_a_long_table_is_written_in_chunks_the_box_can_swallow() -> None:
    fake = FakeBox()
    box = Box(transport=fake)
    events = ",".join(f"{tick}:A:1" for tick in range(100, 2000, 2))
    load = box.send_table(f"STBLDAT;0:[A:1,{events},4000:];", chunk_bytes=512)
    assert load.bytes_sent > fake.ring_buffer_bytes
    assert max(len(chunk) for chunk in fake.written) <= 512
    assert fake.dropped_bytes == 0
    assert box.verify_table(load) == []


def test_a_load_paces_itself_because_the_box_has_no_flow_control() -> None:
    """§1: the pause between chunks is what carries a table past 4 KB.

    Asserted as a lower bound on elapsed time, because the point is that the
    writes are separated at all, not how accurately Python sleeps.
    """
    fake = FakeBox()
    box = Box(transport=fake)
    points = ",".join(f"{100 + 2 * i}:A:{i % 2}" for i in range(400))
    table = f"STBLDAT;0:[A:1,{points},900:];"
    started = time.monotonic()
    load = box.send_table(table, chunk_bytes=256, chunk_gap=0.005)
    elapsed = time.monotonic() - started
    assert len(fake.written) > 4
    assert elapsed >= (len(fake.written) - 1) * 0.005 * 0.5
    assert load.bytes_sent == len(table) + 1
    assert not box.verify_table(load)


def test_an_unpaced_load_is_still_available_for_the_bench() -> None:
    box = Box(transport=FakeBox())
    load = box.send_table(EXAMPLE, chunk_gap=0)
    assert not box.verify_table(load)


def test_the_default_chunk_is_far_below_the_boxs_input_buffer() -> None:
    assert DEFAULT_CHUNK_BYTES * 4 <= RING_BUFFER_BYTES
    assert 0 < DEFAULT_CHUNK_GAP_S < TOKEN_TIMEOUT_S / 100


def test_writing_a_long_table_in_one_go_loses_its_tail() -> None:
    # The failure this guards against is silent on a real box: no NAK, no
    # error, just a table that is not the one that was sent.
    fake = FakeBox()
    box = Box(transport=fake, timeout=0.05)
    events = ",".join(f"{tick}:A:1" for tick in range(100, 2000, 2))
    with pytest.raises((BoxTimeout, BoxRejected)):
        box.send_table(f"STBLDAT;0:[A:1,{events},4000:];", chunk_bytes=1_000_000)
    assert fake.dropped_bytes > 0


def test_a_table_round_trips_through_tblrpt() -> None:
    box = Box(transport=FakeBox())
    load = box.send_table(EXAMPLE)
    assert box.verify_table(load) == []


def test_verify_reports_a_box_holding_a_different_table() -> None:
    fake = FakeBox()
    box = Box(transport=fake)
    load = box.send_table(EXAMPLE)
    fake.loaded = compile_table("STBLDAT;25:[A:9,10:A:1,25:A:0:5:34.5,100:];")
    assert box.verify_table(load) == [
        "table 1 ('A'): repeat predicted 10, box has 9"
    ]


def test_verify_notices_a_box_packing_its_structs_differently() -> None:
    class RepackedBox(FakeBox):
        def _do_tblrpt(self, argument: str) -> None:
            super()._do_tblrpt(argument)
            self._out = bytearray(
                bytes(self._out).replace(b"Size of TableHeader = 13",
                                         b"Size of TableHeader = 16")
            )

    box = Box(transport=RepackedBox())
    load = box.send_table(EXAMPLE)
    assert "not the (13, 5, 5)" in box.verify_table(load)[0]


def test_arming_waits_for_the_ready_line() -> None:
    box = Box(transport=FakeBox())
    box.send_table(EXAMPLE)
    box.arm()
    assert box.table_status() == "READY"


def test_a_pass_reports_trigger_completion_and_the_automatic_re_arm() -> None:
    box = Box(transport=FakeBox(trigger="POS"))
    box.send_table(EXAMPLE)
    box.arm()
    box.trigger()
    assert box.wait_for(TableEvent.TRIGGERED, timeout=0.5) is TableEvent.TRIGGERED
    assert box.wait_for(TableEvent.COMPLETE, timeout=0.5) is TableEvent.COMPLETE
    assert box.wait_for(TableEvent.READY, timeout=0.5) is TableEvent.READY


def test_a_software_triggered_box_re_arms_itself_too() -> None:
    """Section 1: both of `ProcessTables()`'s loops re-arm, `SW` included.

    Measured as `TBLTRIG`, `TBLCMPLT`, `TBLRDY` on one `TBLSTRT` at 1.211t, and as
    `TRIGGERED`, `COMPLETE`, `READY` at 1.163t (lab record, tasks 28 and 44). This
    stand-in used to drop to local here, which was the pessimistic reading of a
    question the wire format left open, and it made every software-triggered
    rehearsal red for a reason no box supplies.
    """
    fake = FakeBox(trigger="SW")
    box = Box(transport=fake)
    box.send_table(EXAMPLE)
    box.arm()
    box.trigger()
    assert box.drain(0.02) == [
        TableEvent.TRIGGERED, TableEvent.COMPLETE, TableEvent.READY]
    assert (fake.mode, fake.status) == ("TBL", "READY")


def test_a_software_triggered_box_takes_repeat_starts_with_no_round_trip() -> None:
    """100 consecutive `TBLSTRT` on one load, no `SMOD` between them, is what AUKLET
    took on two separate days; ten is enough to pin the state machine."""
    fake = FakeBox(trigger="SW")
    box = Box(transport=fake)
    box.send_table(EXAMPLE)
    box.arm()
    for _ in range(10):
        box.trigger()
        assert box.drain(0.02) == [
            TableEvent.TRIGGERED, TableEvent.COMPLETE, TableEvent.READY]
    assert box.table_status() == "READY"


def test_once_runs_one_pass_and_leaves_table_mode() -> None:
    """`TableN`, which the outer loop decrements and only the `SW` path reaches."""
    fake = FakeBox(trigger="SW")
    box = Box(transport=fake)
    box.send_table(EXAMPLE)
    box.arm("ONCE")
    box.trigger()
    assert box.drain(0.02) == [TableEvent.TRIGGERED, TableEvent.COMPLETE]
    assert (fake.mode, fake.status) == ("LOC", "IDLE")
    with pytest.raises(BoxRejected) as refused:
        box.trigger()
    assert refused.value.code == 6


def test_a_bare_pass_count_runs_that_many_passes() -> None:
    fake = FakeBox(trigger="SW")
    box = Box(transport=fake)
    box.send_table(EXAMPLE)
    box.arm("3")
    for _ in range(2):
        box.trigger()
        assert box.drain(0.02)[-1] is TableEvent.READY
    box.trigger()
    assert box.drain(0.02) == [TableEvent.TRIGGERED, TableEvent.COMPLETE]
    assert fake.mode == "LOC"


def test_arming_stages_the_tables_first_time_point_into_the_digital_image() -> None:
    """Section 3 step 2: `SetupTimer()` calls `SetupNextEntry()`, which writes the
    first time point's digital outputs into the image and leaves the latch pending.

    So `GDIO` answers what the table is about to drive while the pin still holds the
    old value -- measured on AUKLET at 1.211t as `GDIO,A` and `GDIO,B` both 0 with the
    table loaded and the box local, both 1 after `SMOD,TBL` (lab record, task 42).
    """
    fake = FakeBox(trigger="SW")
    box = Box(transport=fake)
    box.send_table("STBLDAT;0:[A:1,0:A:1:B:1,500:B:0,4999:A:0,5000:];")
    assert (box.command("GDIO,A", value=True), box.command("GDIO,B", value=True)) == (
        "0", "0")
    box.arm()
    assert (box.command("GDIO,A", value=True), box.command("GDIO,B", value=True)) == (
        "1", "1")
    assert not any(fake.dio_pins.values()), "no latch has fired, so no pin has moved"


def test_aborting_leaves_table_mode() -> None:
    box = Box(transport=FakeBox())
    box.send_table(EXAMPLE)
    box.arm()
    box.abort()
    assert box.wait_for(TableEvent.ABORTED, timeout=0.5) is TableEvent.ABORTED
    assert box.table_status() == "ABORTED"


def test_silencing_the_status_lines_is_honoured() -> None:
    box = Box(transport=FakeBox())
    box.command("STBLREPLY,FALSE")
    box.send_table(EXAMPLE)
    box.command("SMOD,TBL")
    assert box.drain(0.02) == []
    with pytest.raises(BoxTimeout):
        box.wait_for(TableEvent.READY, timeout=0.05)


# --------------------------------------------------------------------------
# The ARB command surface
# --------------------------------------------------------------------------


def test_a_box_reports_what_it_is_fitted_with() -> None:
    box = Box(transport=FakeBox(arb_modules=2, do_channels=16, dcb_channels=16))
    assert box.command("GCHAN,ARB", value=True) == "2"
    assert box.command("GCHAN,DO", value=True) == "16"
    assert box.command("GCHAN,DCB", value=True) == "16"


def test_a_box_with_no_arb_modules_refuses_the_whole_command_set() -> None:
    box = Box(transport=FakeBox())
    assert box.command("GCHAN,ARB", value=True) == "0"
    for command in ("SWFREQ,1,15000", "ARBSYNC", "TARBTRG", "SARBCTBL,J10[HR]1"):
        with pytest.raises(BoxRejected) as raised:
            box.command(command)
        assert raised.value.code == 115  # no ARB module in system


def test_an_arb_setup_block_reads_back() -> None:
    box = Box(transport=FakeBox(arb_modules=4))
    for command in ("SWFREQ,3,15000", "SWFVRNG,3,15", "SWFDIR,1,REV", "SALTWFM,1,REV"):
        box.command(command)
    # 14914 and not 15000: the module's waveform clock is an integer divider and
    # `GWFREQ` answers what it could make of the request, which is what both ARB boxes
    # answer on the instrument (wire format 6.2, lab record, task 56).
    assert box.command("GWFREQ,3", value=True) == "14914"
    assert box.command("GWFVRNG,3", value=True) == "15"
    assert box.command("GWFDIR,1", value=True) == "REV"
    assert box.command("GALTWFM,1", value=True) == "REV"
    # Untouched modules keep their own values rather than the box's last write.
    assert box.command("GWFREQ,4", value=True) == "0"


def test_the_commands_with_no_documented_getter_have_none_here_either() -> None:
    """A probe that finds one on a real box has found something (task 10).

    `SARBCCLK` selects which module the common clock freezes, and nothing in
    the protocol document reads it back; the same holds for the two line-role
    commands. A stand-in that invented getters would hide exactly the question
    a box is being asked.
    """
    box = Box(transport=FakeBox(arb_modules=4))
    box.command("SARBCCLK,3,TRUE")
    box.command("SARBSYNLN,3,1")
    for absent in ("GARBCCLK,3", "GARBSYNLN,3", "GARBCMPLN,3"):
        with pytest.raises(BoxRejected) as raised:
            box.command(absent, value=True)
        assert raised.value.code == 1  # invalid command


def test_a_module_the_box_does_not_hold_is_rejected() -> None:
    box = Box(transport=FakeBox(arb_modules=2))
    assert box.command("GARBVER,2", value=True) == "2.21"
    with pytest.raises(BoxRejected) as raised:
        box.command("GARBVER,3", value=True)
    assert raised.value.code == 15  # board number too high, or board not present
    with pytest.raises(BoxRejected) as raised:
        box.command("GARBVER,0", value=True)
    assert raised.value.code == 14


def test_a_compression_table_reads_back_byte_for_byte() -> None:
    """The engine syntax-checks nothing on load (section 6.6), so the readback
    is the only check a host has that the string arrived intact."""
    fake = FakeBox(arb_modules=4)
    box = Box(transport=fake)
    table = "J30[HRsD90m3CD10m3ND208rD16.7628sD10.0253r]100"
    box.command(f"SARBCTBL,{table}")
    assert box.command("GARBCTBL", value=True) == table
    box.command("TARBTRG")
    assert fake.compressor_triggers == 1
