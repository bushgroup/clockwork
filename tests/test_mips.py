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
    decode,
    differences,
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


def test_set_style_ack_is_one_token() -> None:
    assert [t.kind for t in ResponseReader().feed(b"\x06\n\r")] == [Kind.ACK]


def test_get_style_reply_is_an_ack_then_a_line() -> None:
    tokens = ResponseReader().feed(b"\x061.263, June 20, 2026\r\n")
    assert [t.kind for t in tokens] == [Kind.ACK, Kind.LINE]
    assert tokens[1].text == "1.263, June 20, 2026"


def test_nak_swallows_its_question_mark() -> None:
    assert [t.kind for t in ResponseReader().feed(b"\x15?\n\r")] == [Kind.NAK]


def test_nak_swallows_its_question_mark_across_a_split_read() -> None:
    reader = ResponseReader()
    assert [t.kind for t in reader.feed(b"\x15")] == [Kind.NAK]
    assert reader.feed(b"?\n\r") == []


def test_status_lines_survive_their_doubled_newline() -> None:
    tokens = ResponseReader().feed(b"TBLRDY\n\r\nTBLTRIG\n\r\n")
    assert [t.text for t in tokens] == ["TBLRDY", "TBLTRIG"]


def test_a_line_split_across_reads_is_one_token() -> None:
    reader = ResponseReader()
    assert reader.feed(b"TBLCM") == []
    assert reader.partial() == "TBLCM"
    assert [t.text for t in reader.feed(b"PLT\n\r\n")] == ["TBLCMPLT"]


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


def test_arb_channels_store_float_bits() -> None:
    entry = compile_table("STBLDAT;0:[A:1,10:101:12.5,100:];").tables[0].points[0].entries[0]
    assert entry.chan == 101
    assert entry.kind is ValueKind.FLOAT
    assert entry.render_value() == "12.5"


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


def test_a_rejection_carries_the_reason_the_box_gave() -> None:
    box = Box(transport=FakeBox())
    with pytest.raises(BoxRejected) as caught:
        box.command("NOSUCHCMD")
    assert caught.value.code == 1
    assert "invalid command" in str(caught.value)


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


def test_a_software_triggered_box_does_not_re_arm_itself() -> None:
    box = Box(transport=FakeBox(trigger="SW"))
    box.send_table(EXAMPLE)
    box.arm()
    box.trigger()
    assert box.drain(0.02) == [TableEvent.TRIGGERED, TableEvent.COMPLETE]


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
