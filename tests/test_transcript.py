"""The wire transcript: what it records, what it refuses to record, and what it costs.

Everything here runs against `FakeBox` and `FakeConsole`, so what is being checked is
that the package offers its traffic to the loggers in the right order and with the right
content, not anything about a real link. Two properties are worth naming before the
assertions, because both are the reason the module exists in the shape it does.

**Off means the level gate.** `clockwork` carries a `NullHandler` and no level, so the
effective level of every logger under it is the root's `WARNING` and a call site never
builds a `LogRecord`. `test_a_transcript_that_is_not_open_costs_no_records` attaches a
handler with the level left alone and asserts the count is zero, which is the same thing
the package's call sites rely on.

**The `STBLDAT` send writes nothing between its chunks.** A record flushed in the gap
would add itself to the interval that paces the load, which is a bench measurement
(`docs/mips-wire-format.md` section 1). The test for it is not a timing test: the
handler below stamps every record with how many chunks the box had received when it was
emitted, so "no record was emitted mid-send" is a fact about the sequence rather than
about a clock. The timing measurement that goes with it is in the lab record, task 29.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

import pytest

from clockwork import acq, transcript
from clockwork.acq import Console, DataStream, FakeConsole, FrameRequest, run_frame
from clockwork.mips import DEFAULT_CHUNK_BYTES, Box, FakeBox, MipsError

LONG_TABLE = "STBLDAT;0:[A:1," + ",".join(
    f"{tick}:A:1" for tick in range(100, 1200, 2)
) + ",4000:];"
"""Comfortably more than one chunk, and more than the 4096-byte ring buffer, so the
chunk boundaries under test are the ones a real table would actually need."""


class Collected(logging.Handler):
    """Every record, as `(logger name, rendered message)`, with an optional stamp.

    `stamp` is called at emit time and its result kept beside the message. The send-path
    test passes one that counts what the box has received, which is how "nothing was
    written between two writes" becomes an assertion about a sequence.
    """

    def __init__(self, stamp=None):
        super().__init__()
        self.records: list[tuple[str, str, object]] = []
        self._stamp = stamp

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.name, record.getMessage(),
                             self._stamp() if self._stamp else None))

    def messages(self, prefix: str = "") -> list[str]:
        return [text for name, text, _ in self.records if name.startswith(prefix)]

    def text(self, prefix: str = "") -> str:
        return "\n".join(self.messages(prefix))


@pytest.fixture
def collected():
    """A handler on `clockwork`, with the transcript's level, removed afterwards.

    `to_file` is what a caller uses; this is the same arrangement without a file, so a
    test can read the records instead of a log.
    """
    logger = logging.getLogger(transcript.ROOT_LOGGER)
    handler = Collected()
    level, propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate


# --- the box ---------------------------------------------------------------------------


def test_a_box_session_is_transcribed_byte_for_byte_in_order(collected):
    """Command out, reply in, and the async status line classified where it landed.

    Rendered as `repr` because the framing is the question: section 1's LF-CR after a
    set-style ACK, CR-LF after a value and doubled newline after a status line are three
    conventions in one firmware, and a transcript that normalised them would be useless
    for the one job it has on a box day.
    """
    box = Box(transport=FakeBox(name="stand-in"), name="dunlin")
    box.version()
    box.send_table("STBLDAT;0:[A:1,100:];")
    box.arm()

    lines = collected.messages("clockwork.mips.wire")
    assert r"dunlin > b'GVER\n'" in lines
    assert any(line.startswith("dunlin < b'\\x06") and "1.263" in line for line in lines)
    assert "dunlin > b'SMOD,TBL\\n'" in lines
    assert "dunlin ! TBLRDY" in lines
    # The order is the point: the ACK's bytes are read before the line they carried is
    # classified, and both come after the command that provoked them.
    assert lines.index("dunlin > b'SMOD,TBL\\n'") < lines.index("dunlin ! TBLRDY")


def test_a_table_load_records_its_string_its_chunks_and_its_cost(collected):
    box = Box(transport=FakeBox(), name="auklet")
    load = box.send_table(LONG_TABLE)
    chunks = -(-load.bytes_sent // DEFAULT_CHUNK_BYTES)

    text = collected.text("clockwork.mips.wire")
    assert f"STBLDAT {load.bytes_sent} bytes in {chunks} chunks of " \
           f"{DEFAULT_CHUNK_BYTES}" in text
    # The string itself, whole. It is the evidence any correction to the wire format's
    # section 2 would be argued from, so it is the one thing never elided.
    assert f"string: {LONG_TABLE}" in text
    for number in (1, chunks // 2, chunks):
        assert f"chunk {number}/{chunks} at +" in text
    assert "bytes 0-255" in text
    assert "of stall margin" in text


def test_the_send_path_writes_nothing_between_two_chunks():
    """The one constraint the whole design is shaped by.

    A record formatted and flushed in the gap would add itself to `chunk_gap`, which is
    a bench measurement against a box with no flow control. So `send_table` buffers a
    tuple per chunk and emits the lines once the string is on the wire, and that is
    checked here by counting the writes the box had taken at the moment each record was
    emitted: nothing may be emitted while that count is strictly inside the send.
    """
    fake = FakeBox()
    box = Box(transport=fake, name="auklet")
    handler = Collected(stamp=lambda: len(fake.written))
    logger = logging.getLogger(transcript.ROOT_LOGGER)
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        load = box.send_table(LONG_TABLE)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)

    chunks = -(-load.bytes_sent // DEFAULT_CHUNK_BYTES)
    assert chunks > 4, "the table under test has to need several chunks"
    mid_send = [text for _, text, written in handler.records if 0 < written < chunks]
    assert mid_send == [], f"records written between chunks: {mid_send}"
    # And the chunk lines are all there, emitted once the last write had gone.
    after = [text for _, text, written in handler.records if written >= chunks]
    assert sum(">   chunk " in text for text in after) == chunks


def test_a_rejection_records_the_nak_and_the_error_query(collected):
    """A NAK is four bytes and says nothing; what it meant is the `GERR` that follows.

    Both are on the wire and both are in the file, so a rejection on a bench day can be
    read back without the script having chosen in advance to keep it.
    """
    box = Box(transport=FakeBox(), name="dunlin")
    with pytest.raises(Exception):
        box.command("NOSUCHCMD")
    text = collected.text("clockwork.mips.wire")
    assert r"dunlin > b'NOSUCHCMD\n'" in text
    assert r"dunlin < b'\x15?\n\r'" in text
    assert r"dunlin > b'GERR\n'" in text


def test_a_long_read_is_elided_rather_than_dumped(collected):
    """A `TBLRPT` dump is one line per byte and a real table runs to thousands.

    Untruncated it would be tens of thousands of characters saying nothing the parsed
    report does not, so a read is rendered to `MAX_BYTES` and then counted.
    """
    box = Box(transport=FakeBox(), name="dunlin")
    load = box.send_table(LONG_TABLE)
    box.report(load.predicted.byte_size)
    reads = [line for line in collected.messages("clockwork.mips.wire")
             if " < " in line and "more)" in line]
    assert reads, "a TBLRPT dump should have been elided"
    assert all(len(line) < transcript.MAX_BYTES * 2 for line in reads)


# --- the console -------------------------------------------------------------------------


def test_a_console_frame_is_transcribed_on_both_sockets(collected, tmp_path):
    """The command socket, the data socket and the frame request in words.

    `acquire frame`'s own frame is Snappy-compressed protobuf, so the wire line for it
    says only how big it was; the numbers a reader wants are logged where they are
    known. The batch lines carry a count and never a payload.
    """
    with FakeConsole() as fake:
        with DataStream(fake.data_endpoint) as stream, \
                Console(fake.command_endpoint, timeout=10.0) as console:
            console.configure(offset_v=0.251)
            acq.start_chain(console, stream, timeout=10.0, settle=2.0, quiet=0.1)
            run_frame(console, stream,
                      FrameRequest(frame_length=250, offset_bins=20000, frame_number=2),
                      timeout=10.0)
            console.stop_acquire()

    commands = collected.text("clockwork.acq.wire")
    assert "> init" in commands
    assert "> vertical | 0.251" in commands
    assert "< ack" in commands
    assert "acquire frame 2: 250 scans" in commands and "offset_bins 20000" in commands
    assert "> acquire frame | <" in commands
    assert "> stop | frame" in commands

    stream_lines = collected.messages("clockwork.acq.stream")
    assert any(line == "status 'finished'" for line in stream_lines)
    batches = [line for line in stream_lines if line.startswith("batch ")]
    assert batches, "the frame published batches and none was transcribed"
    assert all("scans," in line and "bytes on the wire" in line for line in batches)


def test_a_batch_line_carries_a_count_and_never_the_payload(collected):
    """Hundreds of kilobytes of display product per message, and not the data path.

    The acquired data goes from the console into the UIMF file without passing through
    this client, so the transcript says how much arrived and leaves it at that.
    """
    with FakeConsole() as fake:
        with DataStream(fake.data_endpoint) as stream, \
                Console(fake.command_endpoint, timeout=10.0) as console:
            console.configure(offset_v=0.251)
            acq.start_chain(console, stream, timeout=10.0, settle=2.0, quiet=0.1)
            run_frame(console, stream, FrameRequest(frame_length=100), timeout=10.0)
            console.stop_acquire()
    for line in collected.messages("clockwork.acq.stream"):
        if line.startswith("batch "):
            assert len(line) < 200, line
            assert "array(" not in line and "[" not in line


def test_a_console_timeout_records_the_socket_it_threw_away(collected):
    """A request that goes unanswered is the failure a bench day most wants back.

    The socket is reconnected so the late reply cannot be read as the next question's
    answer, and the transcript says that happened; without it the file would show a
    command with no reply and no explanation.
    """
    with FakeConsole() as fake:
        fake.stop()
        with Console(fake.command_endpoint, timeout=0.2) as console:
            with pytest.raises(acq.ConsoleTimeout):
                console.firmware()
    assert any("unanswered after" in line and "reconnected" in line
               for line in collected.messages("clockwork.acq.wire"))


# --- the loop ----------------------------------------------------------------------------


def test_the_loops_events_go_to_the_transcript_as_well_as_to_progress(collected):
    """The loop's narrative is its decisions, beside the wire that provoked them.

    Reported whether or not the caller passed a `progress`, so a window that draws a
    status line and a script that prints one do not each have to remember to write the
    same events to the file.
    """
    from clockwork import method as method_module

    recipe = method_module.from_dict({
        "schema_version": 2,
        "metadata": {"name": "transcript test", "created": dt.date(2026, 9, 11)},
        "acquisition": {"frames": 1, "scans": 16, "accumulations": 1,
                        "file_stem": "transcript-test",
                        "enable": {"box": "box1", "channel": "A"}},
        "boxes": [{"name": "box1", "port": "COM3", "setup": ["STBLCLK,EXT"],
                   # The two numbers are `clockwork.method`'s rule and not this file's:
                   # a table that drops the digitizer's enable anywhere else is a method
                   # that contradicts itself, and `send_phases` refuses one.
                   "load": [f"STBLDAT;0:[A:1,0:A:1:B:1,"
                            f"{method_module.enable_fall_tick(16)}:A:0,"
                            f"{method_module.table_period(16)}:];"],
                   "arm": ["SMOD,TBL"]}],
        "start": [["box1", "TBLSTRT"]],
    })
    boxes = {"box1": Box(transport=FakeBox(), name="box1")}
    acq.send_phases(recipe, boxes)

    text = collected.text("clockwork.acq.loop")
    assert "BoxReady: box1 on COM3" in text
    assert "PhaseSent: box1 setup: STBLCLK,EXT" in text
    assert "PhaseSent: box1 arm: SMOD,TBL armed, TBLRDY" in text


# --- the helper --------------------------------------------------------------------------


def test_a_transcript_that_is_not_open_costs_no_records():
    """The level gate, which is what "switched off" means.

    A handler with the level left alone: the effective level of `clockwork.*` is the
    root's `WARNING`, so no call site in the package builds a record at all.
    """
    logger = logging.getLogger(transcript.ROOT_LOGGER)
    handler = Collected()
    logger.addHandler(handler)
    try:
        box = Box(transport=FakeBox(), name="dunlin")
        box.version()
        box.send_table(LONG_TABLE)
        box.arm()
        transcript.note("this costs nothing either")
    finally:
        logger.removeHandler(handler)
    assert handler.records == []


def test_to_file_writes_a_header_flushes_as_it_goes_and_puts_the_logger_back(tmp_path):
    logger = logging.getLogger(transcript.ROOT_LOGGER)
    level, propagate, handlers = logger.level, logger.propagate, list(logger.handlers)
    path = tmp_path / "sub" / transcript.default_name("bench")

    with transcript.to_file(path, header="a header a caller asked for") as open_file:
        assert os.path.isfile(path), "the directory and the file are made on the way in"
        Box(transport=FakeBox(), name="dunlin").version()
        # Flushed per record, so a run that dies leaves the file it had written.
        assert "GVER" in path.read_text(encoding="utf-8")
        assert open_file.path == str(path)
        assert logger.propagate is False, "the package's records do not go to the root"

    written = path.read_text(encoding="utf-8")
    assert "clockwork" in written.splitlines()[0]
    assert "a header a caller asked for" in written
    assert "batch payloads and digitizer samples are not here" in written
    assert "closed after" in written
    assert logger.level == level and logger.propagate == propagate
    assert list(logger.handlers) == handlers


def test_a_note_puts_a_callers_own_line_in_the_same_file(tmp_path):
    """For traffic the package cannot see: a reply read straight off a transport, or a
    knob turned between steps. In the same file, in the same order."""
    path = tmp_path / "notes.transcript.log"
    with transcript.to_file(path):
        transcript.note("GCMDS read raw off the transport, %d bytes", 4096)
        Box(transport=FakeBox(), name="dunlin").version()
    written = path.read_text(encoding="utf-8")
    assert "GCMDS read raw off the transport, 4096 bytes" in written
    assert written.index("GCMDS read raw") < written.index("GVER")


def test_bytes_render_as_repr_and_elide_past_the_limit():
    assert transcript.render(b"\x06\n\r") == r"b'\x06\n\r'"
    long = b"x" * (transcript.MAX_BYTES + 40)
    rendered = transcript.render(long)
    assert rendered.endswith("(+40 more)")
    assert len(rendered) < len(repr(long))


def test_default_name_is_the_shape_every_caller_should_use():
    assert transcript.default_name("rig", when=dt.date(2026, 9, 11)) \
        == "rig-2026-09-11.transcript.log"


def test_the_five_logger_names_are_the_ones_the_package_emits_to(collected):
    """The names are a public contract: a caller may attach to one of them alone."""
    assert set(transcript.LOGGERS) == {
        "clockwork.mips.wire", "clockwork.acq.wire",
        "clockwork.acq.stream", "clockwork.acq.loop",
        "clockwork.acq.console_process",
    }
    for name in transcript.LOGGERS:
        assert name.startswith(transcript.ROOT_LOGGER + "."), \
            "one handler on `clockwork` has to catch every link"


# --- the send log ------------------------------------------------------------------------


SEND_LOG_METHOD = {
    "schema_version": 2,
    "metadata": {"name": "send log test", "created": dt.date(2026, 9, 15)},
    "acquisition": {"frames": 1, "scans": 16, "accumulations": 1,
                    "file_stem": "send-log-test",
                    "enable": {"box": "box1", "channel": "A"}},
    "boxes": [{"name": "box1", "port": "COM3",
               "setup": ["STBLCLK,EXT", "STBLTRG,POS"],
               "load": ["STBLDAT;0:[A:1,0:A:1:B:1,516:A:0,517:];"],
               "arm": ["SMOD,TBL"]}],
    "start": [["box1", "TBLSTRT"]],
}
"""One box, one string per phase, and a table whose enable ticks agree with the 16 scans
`[acquisition]` asks for -- the loop refuses a method where they do not, and a send log
test that could not send its own strings would be testing the refusal."""


def a_method(**changes):
    from clockwork import method as method_module

    document = dict(SEND_LOG_METHOD)
    document.update(changes)
    return method_module.from_dict(document)


def send_log_of(path, method=None, header="", acquire=False, tmp_path=None):
    """One `send_phases`, optionally a whole acquisition, and the two files it left.

    Both are opened, not just the send log: the pair is what a bench run writes, and
    several of the assertions below are about what the two do differently.
    """
    method = method or a_method()
    boxes = {"box1": Box(transport=FakeBox(), name="box1")}
    wire = str(path) + ".wire"
    with transcript.send_log(path, header=header), transcript.to_file(wire):
        if not acquire:
            acq.send_phases(method, boxes)
        else:
            with FakeConsole() as fake:
                fake.frame_hold_s = 0.05
                with (DataStream(fake.data_endpoint) as stream,
                      Console(fake.command_endpoint) as console):
                    console.configure(offset_v=0.251)
                    acq.send_phases(method, boxes)
                    acq.run_acquisition(
                        method, boxes=boxes, console=console, stream=stream,
                        directory=str(tmp_path), stem="send-log-test-20260915-120000",
                        post_trigger_samples=fake.post_trigger_samples,
                        silence=0.3, gate_dwell=0.01,
                    )
                    console.stop_acquire()
    return path.read_text(encoding="utf-8"), open(wire, encoding="utf-8").read()


def test_a_send_log_holds_every_string_of_a_method_once_and_in_order(tmp_path):
    """What Addison asked for: the strings that drove this file, as he would type them.

    Once each, because a reader who finds the same table twice has to work out whether
    the box was loaded twice; in the order they went, because the order is half of what
    a method is; and whole, because a table elided at 48 characters is not a string
    anyone can compare against the one they know works.
    """
    written, _ = send_log_of(tmp_path / "run.sent.txt")
    strings = (["STBLCLK,EXT", "STBLTRG,POS"]
               + list(a_method().box("box1").load) + ["SMOD,TBL"])
    lines = written.splitlines()
    at = -1
    for string in strings:
        sent = [line for line in lines if line.endswith(f"{transcript.TO_BOX} {string}")]
        assert len(sent) == 1, f"{string!r} appears {len(sent)} times"
        position = lines.index(sent[0])
        assert position > at, f"{string!r} is out of order"
        at = position
    # And each of them was answered, on its own line, in words.
    assert written.count(f"{transcript.FROM_BOX} ACK") >= len(strings)


def test_a_send_log_names_the_box_the_port_and_the_firmware_it_drove(tmp_path):
    """A log that named three COM numbers would name nothing a reader can check.

    Windows renumbers a port when a box is unplugged, so the identity that survives is
    the box's own `GNAME` and the firmware `GVER` reports (lab record, task 39).
    """
    written, _ = send_log_of(tmp_path / "run.sent.txt")
    assert "> GVER" in written and "> GNAME" in written
    assert "box1 on COM3: SLIMbox, firmware" in written


def test_a_refusal_carries_the_string_and_the_firmwares_text_on_one_line(tmp_path):
    """The one line the day's ledger asked for by name.

    `GERR`'s text points the wrong way often enough that reading it apart from the
    string it refused is how a bench hour goes: 6 says "not in table mode" for a command
    that needs *local* mode. So the string and the firmware's account of the rejection
    are on one line, and the `GERR` round trip that produced the account is not in this
    file at all.
    """
    boxes = {"box1": Box(transport=FakeBox(), name="box1")}
    refused = a_method(boxes=[dict(SEND_LOG_METHOD["boxes"][0], setup=["NOSUCHCMD"])])
    path = tmp_path / "refused.sent.txt"
    with transcript.send_log(path):
        with pytest.raises(MipsError):
            acq.send_phases(refused, boxes)
    written = path.read_text(encoding="utf-8")
    assert "NAK NOSUCHCMD: error 1, invalid command" in written
    refusal = [line for line in written.splitlines() if " refused: " in line]
    assert refusal and "NOSUCHCMD" in refusal[0] and "error 1, invalid command" in refusal[0]
    assert "GERR" not in written, "the query behind the answer is not the answer"


def test_a_send_log_drops_the_chunks_the_batches_and_the_bytes(tmp_path):
    """It is a view of the wire transcript, and the view is what makes it readable.

    A `STBLDAT` send is four lines of chunk bookkeeping and a frame publishes a batch
    summary every sixty-odd milliseconds; both belong in the forensic record and neither
    belongs in a file a trainee reads to find a string.
    """
    written, wire = send_log_of(tmp_path / "run.sent.txt", acquire=True, tmp_path=tmp_path)
    assert "chunk 1/" in wire and "chunk 1/" not in written
    assert "of stall margin" in wire
    assert "batch 1:" in wire and "batch 1:" not in written
    assert "BatchSeen" not in written
    assert r"b'SMOD,TBL\n'" in wire, "the bytes stay in the transcript"
    assert "b'" not in written, "and never reach the send log"


def test_the_two_files_of_one_run_do_not_disagree(tmp_path):
    """Everything in the send log is a record the wire transcript holds too, bar one kind.

    The classified replies -- `ACK`, a value, the NAK with its number -- are written for
    the send log alone, because the bytes behind each are in the transcript already and a
    derived line there would be length without evidence.
    """
    written, wire = send_log_of(tmp_path / "run.sent.txt", acquire=True, tmp_path=tmp_path)
    for string in ("STBLCLK,EXT", "SMOD,TBL", "TBLSTRT", "TBLRDY", "acquire frame 1"):
        assert string in written and string in wire
    assert "< ACK" in written and "< ACK" not in wire


def test_a_header_says_what_the_run_was(tmp_path):
    """A file of strings says nothing on its own about the window they were acquired in.

    The same method through a different full scale, offset or inversion is a different
    experiment, and the method's hash is the link back to the document the strings came
    from.
    """
    from clockwork.instrument import Instrument, Vertical

    header = transcript.run_header(
        method=a_method(), method_path="/somewhere/clock.toml",
        instrument=Instrument(name="SLIM3", vertical=Vertical(0.5, 0.2512, False)),
        instrument_path="instrument.toml",
        console="Digitizer Model: SA220P / App Version: 0.1.0",
        boxes=[("auklet", "COM6", "MIPS-A", "Version 1.211t")],
    )
    written, _ = send_log_of(tmp_path / "run.sent.txt", header=header)
    assert "clock.toml" in written and "'send log test'" in written
    assert "sha256 " in written
    assert "SLIM3" in written and "full scale 0.5 V" in written
    assert "offset 0.2512 V" in written and "not inverted" in written
    assert "Digitizer Model: SA220P" in written
    assert "auklet      COM6  MIPS-A  Version 1.211t" in written


def test_a_header_leaves_out_what_it_was_not_given():
    """"offset: unknown" reads as a measurement that failed, and this is a caller that
    passed none."""
    from clockwork.instrument import Instrument, Vertical

    assert transcript.run_header() == ""
    assert transcript.run_header(instrument=Instrument(vertical=Vertical(offset_v=0.2))) \
        == "instrument   offset 0.2 V"


def test_both_files_end_their_lines_with_lf_on_every_platform(tmp_path):
    """`logging.FileHandler` writes CRLF on Windows into a repo that pins `eol=lf`.

    Every transcript then reads as modified the moment it is touched, and a log committed
    beside its run shows up as a diff of itself (lab record, tasks 39 and 41).
    """
    send = tmp_path / "run.sent.txt"
    send_log_of(send)
    for path in (send, tmp_path / "run.sent.txt.wire"):
        raw = path.read_bytes()
        assert b"\r\n" not in raw, f"{path.name} was written with CRLF"
        assert raw.endswith(b"\n")


def test_send_log_name_is_the_shape_every_caller_should_use():
    """No date in it, unlike a transcript's: the stem already carries the run's, and the
    point of the name is that a file and its log sort together."""
    assert (transcript.send_log_name("bradykinin_clock-20260915-145701")
            == "bradykinin_clock-20260915-145701.sent.txt")


def test_a_send_log_that_is_not_open_costs_no_records():
    """The same level gate the wire transcript relies on, through the new call sites.

    A `Sent` is built at the call site, so a call site that never runs builds none.
    """
    logger = logging.getLogger(transcript.ROOT_LOGGER)
    handler = Collected()
    logger.addHandler(handler)
    try:
        box = Box(transport=FakeBox(), name="dunlin")
        box.version()
        box.box_name()
        box.local()
        box.send_table("STBLDAT;0:[A:1,100:];")
    finally:
        logger.removeHandler(handler)
    assert handler.records == []
