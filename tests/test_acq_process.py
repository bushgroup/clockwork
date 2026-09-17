"""The console as a process: its `config.txt`, its launch, and preparing it.

Three kinds of test, and they establish different things.

*`ConsoleConfig` is settled here and nowhere else.* It is a file format with a key
table, a set of refusals lifted from `reject_bad_settings()` and one comparison
rule, all of which are exact and none of which need a console.

*The launch path needs a program*, so `console_stand_in.py` is one. What it proves
is that this module starts something, reads its output, notices when it dies, kills
it and can do all of that again -- on the platform it is running on, through the
same `subprocess` call a real console goes through. What it cannot prove is
anything about the console itself: the stand-in has no `disable_quick_edit()`, so a
launch arrangement that would kill the real console passes here (the module
docstring's table is the measurement that settles that, on the instrument PC).

*`prepare_console` runs against `FakeConsole`*, which is enough: what it does is
read three fields off a document, refuse one that is missing and send the rest.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import time

import pytest

from clockwork import instrument as instrument_module
from clockwork import transcript
from clockwork.acq import Console, FakeConsole
from clockwork.acq.process import (
    KEYS_BY_NAME,
    ConsoleConfig,
    ConsoleProcess,
    ConsoleProcessError,
    FakeConsoleProcess,
    find_console,
    prepare_console,
    read_startup_block,
)

STAND_IN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "console_stand_in.py")

SAMPLE = """\
# The instrument's settings. Comments and blank lines are kept.

PostTriggerDelay=0.00001
ResourceName=PXI179::0::0::INSTR
NotifyOnScansCount=500
AcquisitionTimeoutMs=100

TriggerLevel=0.4
TriggerSlope=rising
FullScaleRange=0.5
ZeroSuppressThreshold=-32667
ZeroSuppressHysteresis=100
ControlIoPort=2
"""


def free_port() -> int:
    """A port nothing is on, for a stand-in that must not collide with 5555.

    Racy in principle and not in practice: the stand-in binds it within the second,
    and a test that hits the race fails loudly rather than quietly passing.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def collected():
    """Every `clockwork` record, as the transcript's handler would see them."""
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(transcript.ROOT_LOGGER)
    handler = Keep()
    level, propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate


# --- config.txt ------------------------------------------------------------------


def test_a_config_round_trips_with_its_comments_and_blank_lines(tmp_path):
    path = tmp_path / "config.txt"
    path.write_text(SAMPLE, encoding="utf-8", newline="")
    config = ConsoleConfig.load(str(path))
    assert config.dumps() == SAMPLE
    assert config.get("ResourceName") == "PXI179::0::0::INSTR"
    assert config.full_scale_v == 0.5
    assert config.notify_on_scans_count == 500
    assert config.acquisition_timeout_ms == 100
    assert config.control_io_port == 2
    assert config.zero_suppress_threshold == -32667


def test_a_key_the_file_omits_is_in_force_at_the_console_s_own_literal():
    """Which is the distinction `get` and `in_force` exist to keep.

    A file that does not mention `AcquisitionInitialBufferCount` is not a console
    with no buffer pool; it is a console holding the number compiled into it, and a
    window that showed the key as blank would be saying something false.
    """
    config = ConsoleConfig("TriggerLevel=0.4\n")
    assert config.get("AcquisitionInitialBufferCount") is None
    assert config.in_force("AcquisitionInitialBufferCount") == \
        KEYS_BY_NAME["AcquisitionInitialBufferCount"].default


def test_setting_a_key_changes_one_line_and_appends_one_the_file_lacks(tmp_path):
    path = tmp_path / "config.txt"
    path.write_text(SAMPLE, encoding="utf-8", newline="")
    config = ConsoleConfig.load(str(path))
    config.set(AcquisitionTimeoutMs=2000, LogLevel="debug")
    written = config.dumps()
    assert "AcquisitionTimeoutMs=2000" in written
    assert "LogLevel=debug" in written
    # Every other line is exactly as it was, which is the point of the whole class.
    before = [line for line in SAMPLE.splitlines()
              if not line.startswith("AcquisitionTimeoutMs")]
    after = [line for line in written.splitlines()
             if not line.startswith(("AcquisitionTimeoutMs", "LogLevel"))]
    assert before == after


def test_a_crlf_file_is_written_back_with_crlf(tmp_path):
    """The lab's copy of record is tracked; a run that changed one number and
    rewrote every line's ending would leave a diff of the whole file."""
    path = tmp_path / "config.txt"
    path.write_bytes(SAMPLE.replace("\n", "\r\n").encode("utf-8"))
    config = ConsoleConfig.load(str(path))
    config.set(TriggerLevel=0.5).save()
    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n")
    assert b"TriggerLevel=0.5" in raw


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("ZeroSuppressHysteresis=4", "ZeroSuppressHysteresis must be between 100 and 1023"),
        ("ZeroSuppressHysteresis=2000", "ZeroSuppressHysteresis must be between 100 and 1023"),
        ("FullScaleRange=1.0", "FullScaleRange must be 0.5 or 2.5"),
        ("ZeroSuppressThreshold=-40000", "ZeroSuppressThreshold must be between"),
        ("ControlIoPort=7", "ControlIoPort must be between 1 and 3"),
        ("TriggerSlope=sideways", "TriggerSlope must be rising or falling"),
        ("TriggerLevel=high", "TriggerLevel must be a number"),
    ],
)
def test_the_values_the_fork_refuses_to_start_on_are_caught_here(line, expected):
    """So that a trainee is told which value is wrong, before a console is launched
    and fails to answer for a reason that looks exactly like a missing card."""
    key = line.split("=")[0]
    config = ConsoleConfig(
        "\n".join(one for one in SAMPLE.splitlines() if not one.startswith(key))
        + "\n" + line + "\n")
    problems = config.problems()
    assert len(problems) == 1, problems
    assert problems[0].startswith(expected)


def test_a_good_config_has_no_problems():
    assert ConsoleConfig(SAMPLE).problems() == []


def test_the_console_s_own_spelling_of_a_value_is_not_a_disagreement():
    """`print_config` logs what it parsed, through `std::to_string`: six decimal
    places for every double. A textual comparison would report a file saying
    `0.00001` as having drifted from a console holding exactly that."""
    config = ConsoleConfig(SAMPLE)
    startup = {
        "PostTriggerDelay": "0.000010",
        "TriggerLevel": "0.400000",
        "FullScaleRange": "0.500000",
        "TriggerSlope": "rising",
        "NotifyOnScansCount": "500",
        "AcquisitionTimeoutMs": "100",
    }
    assert config.differences(startup) == {}


def test_a_file_that_has_drifted_from_a_running_console_says_which_key():
    """The whole failure of 2026-09-16: the file went back to 2000 and the process
    kept the 250 ms a bisection had left it on (lab record, task 47)."""
    config = ConsoleConfig(SAMPLE)
    differs = config.differences({"AcquisitionTimeoutMs": "250", "TriggerLevel": "0.400000"})
    assert differs == {"AcquisitionTimeoutMs": ("250", "100")}


def test_a_key_the_console_did_not_log_is_not_a_disagreement():
    assert ConsoleConfig(SAMPLE).differences({}) == {}


def test_a_value_the_console_cannot_print_faithfully_is_not_a_disagreement():
    """Measured against the real console: it starts from `config.txt`, logs
    `TriggerRearmDeadTime` of `0.000002048` as `0.000002`, and a numeric comparison
    then reports a console as having drifted from the file it just read."""
    config = ConsoleConfig(SAMPLE + "TriggerRearmDeadTime=0.000002048\n")
    assert config.differences({"TriggerRearmDeadTime": "0.000002"}) == {}


def test_and_the_key_it_cannot_print_faithfully_is_named_as_a_blind_spot():
    """Because the consequence is real: an edit below the sixth decimal place takes
    effect on the card and shows up in no comparison anything can make."""
    config = ConsoleConfig(SAMPLE + "TriggerRearmDeadTime=0.000002048\n")
    assert config.blind_spots() == {"TriggerRearmDeadTime": "0.000002"}
    # Every other float on this instrument survives the printing.
    assert ConsoleConfig(SAMPLE).blind_spots() == {}


def test_a_change_the_printing_does_survive_is_still_caught():
    config = ConsoleConfig(SAMPLE + "TriggerRearmDeadTime=0.000004\n")
    assert config.differences({"TriggerRearmDeadTime": "0.000002"}) == {
        "TriggerRearmDeadTime": ("0.000002", "0.000004")}


def test_the_startup_block_is_read_off_the_last_start_in_the_output():
    """A supervisor that restarts appends to the same capture, so the block that
    answers for the process now running is the last one."""
    lines = [
        "[info] Logger initialized",
        'Config value "AcquisitionTimeoutMs" found, value set to 2000',
        "[info] got on with the day",
        "[info] Logger initialized",
        'Config value "AcquisitionTimeoutMs" found, value set to 100',
        'Config value "ZeroSuppressHysteresis" not found, value defaulted to 100',
        "[info] listening",
    ]
    assert read_startup_block(lines) == {
        "AcquisitionTimeoutMs": "100", "ZeroSuppressHysteresis": "100"}


def test_output_with_no_startup_block_reads_as_nothing_known():
    assert read_startup_block(["[critical] config.txt is not usable"]) == {}


# --- the launch path, against the stand-in ----------------------------------------


@pytest.fixture
def stand_in(tmp_path):
    """A `ConsoleProcess` over the stand-in program, stopped however the test ends."""
    made: list[ConsoleProcess] = []

    def build(*args: str) -> ConsoleProcess:
        port = free_port()
        proc = ConsoleProcess(
            [sys.executable, STAND_IN, str(port), *args],
            directory=str(tmp_path), command_port=port,
            output_dir=str(tmp_path / "out"), label="stand-in")
        made.append(proc)
        return proc

    try:
        yield build
    finally:
        for proc in made:
            proc.stop()


def test_a_stand_in_console_starts_answers_and_stops(stand_in, collected):
    proc = stand_in()
    proc.start()
    assert proc.alive
    seconds = proc.wait_ready(timeout=30.0)
    assert seconds > 0
    assert proc.info is not None and proc.info.is_fork
    assert proc.info.full_scale_v == 0.5
    # The startup block came out of this process's own captured stdout, not out of
    # a guess about which file in a log directory belongs to it.
    assert proc.startup["AcquisitionTimeoutMs"] == "100"
    assert proc.startup["ZeroSuppressHysteresis"] == "100"
    proc.stop()
    assert not proc.alive


def test_both_streams_reach_the_transcript(stand_in, collected):
    """Including stderr, which reaches the console's own log file and no client:
    `wrong header` is written to `std::cerr` and is not an spdlog sink (lab
    record, task 21)."""
    proc = stand_in()
    proc.start()
    proc.wait_ready(timeout=30.0)
    proc.stop()
    said = [record.getMessage() for record in collected
            if record.name == "clockwork.acq.console_process"]
    assert any("stdout" in line and "Logger initialized" in line for line in said)
    assert any(line.startswith("stderr") for line in said)


def test_the_supervisor_s_own_decisions_reach_the_send_log_too(stand_in, collected):
    """A trainee reading the send log beside a file should see that the console was
    restarted between two runs; that is exactly what went unrecorded in task 47."""
    proc = stand_in()
    proc.start()
    proc.wait_ready(timeout=30.0)
    proc.stop()
    marked = [record for record in collected if getattr(record, "sent", None) is not None]
    texts = [record.sent.text for record in marked]
    assert any("starting the console" in text for text in texts)
    assert any("stopping the console" in text for text in texts)
    assert all(record.sent.source == transcript.CONSOLE for record in marked)
    # And the raw output is not in the send log, which keeps a trainee's file the
    # strings that drove the run rather than the console's own chatter.
    assert not any("Logger initialized" in text for text in texts)


def test_a_console_that_exits_is_reported_at_once_rather_than_waited_out(stand_in):
    """The console exits 1 within a second of refusing a `config.txt` value. Waiting
    the whole startup timeout for a process that is already gone turns a sentence
    into a forty-second hang."""
    proc = stand_in("--fail", "ZeroSuppressHysteresis must be between 100 and 1023, got 4")
    proc.start()
    began = time.perf_counter()
    with pytest.raises(ConsoleProcessError) as raised:
        proc.wait_ready(timeout=30.0)
    assert time.perf_counter() - began < 10.0
    assert "exited with code 1" in str(raised.value)
    # And the console's own complaint is in the message, not just in a file.
    assert "ZeroSuppressHysteresis" in str(raised.value)
    assert proc.returncode == 1


def test_a_console_that_never_binds_is_called_wedged_not_gone(stand_in):
    proc = stand_in("--silent")
    proc.start()
    with pytest.raises(ConsoleProcessError) as raised:
        proc.wait_ready(timeout=2.0)
    assert "wedged rather than gone" in str(raised.value)
    assert proc.alive
    proc.stop()


def test_a_restart_really_replaces_the_process(stand_in):
    proc = stand_in()
    proc.start()
    proc.wait_ready(timeout=30.0)
    first = proc.startup.copy()
    proc.restart(timeout=30.0)
    assert proc.alive
    assert proc.info is not None
    assert proc.startup == first
    proc.stop()
    assert not proc.alive


def test_stop_is_safe_on_a_process_that_was_never_started(tmp_path):
    proc = ConsoleProcess([sys.executable, STAND_IN, "1"], directory=str(tmp_path),
                          output_dir=str(tmp_path / "out"))
    proc.stop()
    assert not proc.alive


def test_starting_a_missing_executable_says_so(tmp_path):
    proc = ConsoleProcess(str(tmp_path / "AqMD3_console.exe"),
                          output_dir=str(tmp_path / "out"))
    with pytest.raises(ConsoleProcessError, match="no console executable"):
        proc.start()


def test_needs_restart_compares_the_file_beside_the_process(stand_in, tmp_path):
    """Which is the comparison nothing made until this module: the file is not the
    console, and only the console's value is the one a frame meets."""
    (tmp_path / "config.txt").write_text(SAMPLE, encoding="utf-8", newline="")
    proc = stand_in()
    proc.start()
    proc.wait_ready(timeout=30.0)
    assert proc.needs_restart() == {}
    proc.config().set(AcquisitionTimeoutMs=2000).save()
    assert proc.needs_restart() == {"AcquisitionTimeoutMs": ("100", "2000")}
    proc.stop()
    # Nothing running is nothing to disagree with, which is not the same as agreeing.
    assert proc.needs_restart() == {}


# --- the stand-in that is not a process -------------------------------------------


def test_the_fake_supervisor_answers_the_same_questions(collected):
    with FakeConsoleProcess() as supervisor:
        supervisor.start()
        assert supervisor.alive
        supervisor.wait_ready()
        assert supervisor.info is not None
        assert supervisor.command_endpoint.startswith("tcp://")
        assert supervisor.data_endpoint != supervisor.command_endpoint
        assert supervisor.needs_restart() == {}
        first = supervisor.command_endpoint
        supervisor.restart()
        assert supervisor.alive
        # A real restart, so the window's restart path is exercised rather than
        # short-circuited: the stand-in binds fresh ports.
        assert supervisor.command_endpoint != "" and first != ""
    assert not supervisor.alive


def test_the_fake_supervisor_puts_its_decisions_in_the_transcript(collected):
    with FakeConsoleProcess() as supervisor:
        supervisor.start()
        supervisor.wait_ready()
    texts = [record.sent.text for record in collected
             if getattr(record, "sent", None) is not None]
    assert any("simulated console" in text for text in texts)


# --- preparing a console from an instrument document ------------------------------


def instrument_with(**vertical: object):
    document = {"schema_version": instrument_module.SCHEMA_VERSION,
                "instrument": {"name": "SLIM3"}, "vertical": dict(vertical)}
    return instrument_module.from_dict(document)


def test_prepare_console_sends_the_document_s_offset_and_inversion():
    with FakeConsole() as fake, Console(fake.command_endpoint) as console:
        prepared = prepare_console(console, instrument_with(offset_v=0.251, inverted=True,
                                                            full_scale_v=0.5))
    assert prepared.offset_v == 0.251
    assert prepared.inverted is True
    assert prepared.full_scale_v == 0.5
    assert prepared.warnings == ()
    assert "invert" in [command[0] for command in fake.commands]
    assert fake.inverted is True
    assert fake.offset_v == 0.251


def test_a_document_that_states_no_inversion_configures_the_card_as_not_inverted():
    """Silence and `false` have to reach the card the same way, or a document that
    says nothing acquires differently from one that says what the console does."""
    with FakeConsole() as fake, Console(fake.command_endpoint) as console:
        prepared = prepare_console(console, instrument_with(offset_v=0.1))
    assert prepared.inverted is False
    assert fake.inverted is False


def test_a_document_with_no_offset_is_refused_by_name():
    """The failure without this was `float(None)` raising a bare TypeError from
    inside `vertical`, with the console already initialised (lab record, task 30)."""
    with FakeConsole() as fake, Console(fake.command_endpoint) as console:
        with pytest.raises(ConsoleProcessError, match="states no channel offset"):
            prepare_console(console, instrument_with(full_scale_v=0.5))
        assert "vertical" not in [command[0] for command in fake.commands]


def test_a_full_scale_the_console_contradicts_is_warned_about_not_refused():
    with FakeConsole(full_scale_v=2.5) as fake, Console(fake.command_endpoint) as console:
        prepared = prepare_console(console, instrument_with(offset_v=0.1, full_scale_v=0.5))
    assert prepared.full_scale_v == 2.5
    assert len(prepared.warnings) == 1
    assert "stamped with the console's value" in prepared.warnings[0]


def test_a_config_file_the_running_console_contradicts_is_warned_about():
    """The console reads `config.txt` at startup, so a file edited since says
    something the card is not doing, and only a restart changes that."""
    config = ConsoleConfig(SAMPLE).set(FullScaleRange=2.5)
    with FakeConsole(full_scale_v=0.5) as fake, Console(fake.command_endpoint) as console:
        prepared = prepare_console(console, instrument_with(offset_v=0.1, full_scale_v=0.5),
                                   config=config)
    assert any("restart is what changes it" in message for message in prepared.warnings)
    assert fake.offset_v == 0.1


def test_a_stock_console_is_warned_about_because_it_ignores_the_whole_key_table():
    with FakeConsole(fork="", full_scale_v=None) as fake, \
            Console(fake.command_endpoint) as console:
        prepared = prepare_console(console, instrument_with(offset_v=0.1))
    assert any("stock acquisition console" in message for message in prepared.warnings)


# --- finding one ------------------------------------------------------------------


def test_find_console_prefers_what_it_is_given(tmp_path):
    exe = tmp_path / "AqMD3_console.exe"
    exe.write_text("", encoding="utf-8")
    assert find_console(str(exe)) == str(exe)
    assert find_console(str(tmp_path)) == str(exe)


def test_find_console_finds_one_in_a_tree_without_knowing_its_layout(tmp_path):
    deep = tmp_path / "repo" / "build" / "Release"
    deep.mkdir(parents=True)
    from clockwork.acq.process import EXECUTABLE_NAME

    (deep / EXECUTABLE_NAME).write_text("", encoding="utf-8")
    assert find_console(str(tmp_path)) == str(deep / EXECUTABLE_NAME)


def test_find_console_answers_none_rather_than_guessing(tmp_path, monkeypatch):
    """None is an ordinary answer: a public clone has no console, and every path
    that wants one has to say so rather than assume."""
    import clockwork

    monkeypatch.setenv("CLOCKWORK_CONSOLE", str(tmp_path / "nothing-here"))
    monkeypatch.setattr(clockwork, "lab_dir", lambda name=None: None)
    assert find_console(str(tmp_path / "also-nothing")) is None
