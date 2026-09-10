"""The console client, its messages, and a whole acquisition without a console.

What these can and cannot establish is worth being clear about. The framing,
the two topics, the protobuf and Snappy round trips and the command sequence
are checked against `docs/console-protocol.md` and hold whatever the real
console does, because `FakeConsole` reproduces the document rather than the
software: it is written from the same reading of the source that the document
is. What no test here can settle is timing. The stand-in publishes a frame's
batches and its end from inside the handler that started the frame, so the
per-repetition gap and whether trigger timestamps continue across a frame
boundary are exactly the two things a green run says nothing about (lab
record, task 03).
"""

import time

import pytest

from clockwork.acq import (
    ERROR_PREFIX,
    FINISHED,
    FINISHED_ACQUIRE,
    GATE_GRANULARITY_SAMPLES,
    SCAN_NUM_SMALLINT_MAX,
    SECONDS_PER_SAMPLE_2GSPS,
    Batch,
    Console,
    ConsoleAcquisitionError,
    ConsoleInfo,
    ConsoleProtocolError,
    ConsoleStateError,
    ConsoleTimeout,
    DataStream,
    EmptyFrameError,
    FakeConsole,
    FrameRequest,
    Status,
    StreamTimeout,
    TofWidth,
    decode_batch,
    decode_tof_width,
    encode_batch,
    encode_tof_width,
    record_size_samples,
    run_frame,
    start_chain,
)

# The instrument's own figures at 2 GS/s: a 129.0036 us pusher period, the
# 10 us post-trigger delay and the 2.048 us rearm dead time `config.txt` sets.
INSTRUMENT_PERIOD_SAMPLES = 258007
INSTRUMENT_POST_TRIGGER_SAMPLES = 20000
INSTRUMENT_REARM_SAMPLES = 4096


def died(fake, timeout: float = 2.0) -> bool:
    """Whether the stand-in stopped answering, waited for rather than guessed.

    A console that dies does so on its own thread and the client learns of it
    by a timeout, so the two are not ordered against each other.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fake.died:
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def fake():
    with FakeConsole(subscriber_wait_s=2.0) as console:
        yield console


@pytest.fixture
def client(fake):
    with Console(fake.command_endpoint, timeout=5.0) as console:
        yield console


@pytest.fixture
def stream(fake):
    with DataStream(fake.data_endpoint) as subscriber:
        yield subscriber


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------


def test_a_frame_request_round_trips_through_snappy_and_protobuf() -> None:
    request = FrameRequest(
        frame_length=5000,
        file_name=r"D:\data\260909_BK_001.uimf",
        frame_number=7,
        nbr_accumulations=100,
        start_trigger=3,
        offset_bins=20000,
        nbr_samples=253888,
        frame_type="Prescan",
    )
    assert FrameRequest.decode(request.encode()) == request


def test_a_default_frame_request_carries_only_a_length() -> None:
    # proto3 without `optional` has implicit presence, so a zero field is not
    # on the wire at all; what comes back is the zero, not a missing value.
    request = FrameRequest(frame_length=1)
    assert FrameRequest.decode(request.encode()) == request
    assert FrameRequest.decode(request.encode()).file_name == ""


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frame_length": 0},
        {"frame_length": -1},
        {"frame_length": 10, "frame_type": "SIM"},
        {"frame_length": 10, "start_trigger": -1},
    ],
)
def test_an_impossible_frame_request_is_refused(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        FrameRequest(**kwargs)


def test_a_frame_longer_than_the_scan_num_column_is_warned_about() -> None:
    warnings = FrameRequest(frame_length=500_000, file_name="f.uimf",
                            offset_bins=20000).warnings()
    assert len(warnings) == 1
    assert str(SCAN_NUM_SMALLINT_MAX) in warnings[0]
    # It is a warning and not an error: SQLite stores it and the console
    # writes it, so the acquisition would run.
    assert FrameRequest(frame_length=500_000).frame_length == 500_000


def test_a_file_written_with_no_offset_bins_is_warned_about() -> None:
    assert FrameRequest(frame_length=100, file_name="f.uimf").warnings()
    assert not FrameRequest(frame_length=100, file_name="f.uimf",
                            offset_bins=20000).warnings()
    # Without a file there is nothing for a leading zero run to be wrong in.
    assert not FrameRequest(frame_length=100).warnings()


def test_the_tof_width_reply_is_checked_against_its_own_hash() -> None:
    payload, digest = encode_tof_width(TofWidth(pusher_pulse_width=258007, num_samples=253888))
    assert decode_tof_width(payload, digest) == TofWidth(258007, 253888)
    with pytest.raises(ConsoleProtocolError, match="SHA-256"):
        decode_tof_width(payload, "0" * 64)


def test_a_tof_width_with_no_record_size_is_refused() -> None:
    payload, digest = encode_tof_width(TofWidth(pusher_pulse_width=258007, num_samples=0))
    with pytest.raises(ConsoleProtocolError, match="no record size"):
        decode_tof_width(payload, digest)


def test_a_batch_round_trips(fake: FakeConsole) -> None:
    batch = fake._batch(50)
    back = decode_batch(encode_batch(batch))
    assert back.scans == 50
    assert back.mz.size == fake.num_samples
    assert int(back.mz.sum()) == int(batch.mz.sum())
    assert int(back.tic.sum()) == int(batch.tic.sum())
    assert list(back.time_stamps) == list(batch.time_stamps)


def test_a_data_frame_that_is_not_snappy_says_so() -> None:
    with pytest.raises(ConsoleProtocolError, match="Snappy"):
        decode_batch(b"not compressed anything")


def test_the_info_reply_names_the_fork_when_there_is_one() -> None:
    forked = ConsoleInfo.parse(
        "Digitizer Model: SA220P / Digitizer Serial No.: AQ00070766"
        " / Digitizer Firmware Version: 2.7.1811 / App: AqMD3_console"
        " / App Version: 0.1.0-8c5ed07"
        " / Fork: bushgroup/AqMD3-Acquisition-Console@clockwork"
    )
    assert forked.is_fork
    assert forked.model == "SA220P"
    assert forked.serial == "AQ00070766"
    assert forked.branch == "clockwork"

    stock = ConsoleInfo.parse(
        "Digitizer Model: SA220P / Digitizer Serial No.: AQ00070766"
        " / Digitizer Firmware Version: 2.7.1811 / App: AqMD3_console"
        " / App Version: 0.1.0-1b8c964"
    )
    assert not stock.is_fork
    assert stock.model == "SA220P"
    # Which matters: a stock console ignores every setting config.txt moves
    # out of source, without saying that it has.
    assert stock.fork == "" and stock.branch == ""


def test_an_unrecognised_info_reply_is_kept_whole() -> None:
    info = ConsoleInfo.parse("something a later console says")
    assert info.text == "something a later console says"
    assert info.model == ""


# --------------------------------------------------------------------------
# Record sizing
# --------------------------------------------------------------------------


def test_the_record_is_the_period_less_the_delays_rounded_down_to_32() -> None:
    record = record_size_samples(
        INSTRUMENT_PERIOD_SAMPLES, INSTRUMENT_POST_TRIGGER_SAMPLES, INSTRUMENT_REARM_SAMPLES
    )
    assert record == 233888
    assert record % GATE_GRANULARITY_SAMPLES == 0
    usable = INSTRUMENT_PERIOD_SAMPLES - INSTRUMENT_POST_TRIGGER_SAMPLES - INSTRUMENT_REARM_SAMPLES
    assert 0 <= usable - record < GATE_GRANULARITY_SAMPLES


def test_a_period_shorter_than_its_own_delays_is_refused() -> None:
    with pytest.raises(ValueError, match="no record"):
        record_size_samples(1000, 20000, 4096)


# --------------------------------------------------------------------------
# The command socket
# --------------------------------------------------------------------------


def test_the_stand_in_answers_the_questions_that_need_no_card(
    client: Console, fake: FakeConsole
) -> None:
    assert client.num_instruments() == 1
    assert client.info().model == fake.model
    assert client.firmware() == fake.firmware_revision
    assert client.serial() == fake.serial_number


def test_configure_sends_the_sequence_the_console_expects(
    client: Console, fake: FakeConsole
) -> None:
    client.configure(offset_v=0.251, inverted=True)
    assert fake.commands == [
        ("init",),
        ("horizontal", "0.0000000005"),
        ("vertical", "0.251"),
        ("invert", "true"),
        ("enable io port", "2"),
    ]
    assert fake.seconds_per_sample == pytest.approx(SECONDS_PER_SAMPLE_2GSPS)
    assert client.sample_rate_hz == pytest.approx(2e9)
    assert fake.offset_v == 0.251
    assert fake.inverted is True
    assert fake.io_ports_enabled == [2]


def test_configure_can_leave_the_control_io_port_alone(
    client: Console, fake: FakeConsole
) -> None:
    client.configure(offset_v=0.1, io_port=None)
    assert fake.io_ports_enabled == []


def test_a_measured_period_becomes_seconds_through_the_rate_that_was_set() -> None:
    with FakeConsole(pusher_period_samples=INSTRUMENT_PERIOD_SAMPLES,
                     post_trigger_samples=INSTRUMENT_POST_TRIGGER_SAMPLES,
                     rearm_samples=INSTRUMENT_REARM_SAMPLES) as instrument:
        with Console(instrument.command_endpoint, timeout=5.0) as client:
            client.horizontal()
            width = client.tof_width(timeout=5.0)
    assert width.num_samples == 253888
    # 129.0035 us, which is the pusher period the instrument's own files
    # record as AverageTOFLength.
    assert width.period_seconds(2e9) == pytest.approx(129.0035e-6, rel=1e-6)


def test_a_silent_command_is_refused_rather_than_waited_on(client: Console) -> None:
    with pytest.raises(ConsoleStateError, match="never answers"):
        client.request("reset timestamps")
    # Sending one anyway is allowed, and answers nothing, which is why the
    # console's own sequence never needs it.
    client.send_only("reset timestamps")


def test_a_console_that_does_not_answer_times_out_and_leaves_a_usable_client(
    fake: FakeConsole
) -> None:
    endpoint = fake.command_endpoint
    fake.stop()
    with Console(endpoint, timeout=0.25) as client:
        with pytest.raises(ConsoleTimeout, match="did not answer"):
            client.info()
        # The socket was thrown away and remade, so the next request is a
        # fresh timeout and not a wedged socket or a stale reply.
        with pytest.raises(ConsoleTimeout):
            client.num_instruments()


def test_acquire_frame_before_acquire_is_refused_by_the_client(
    client: Console, fake: FakeConsole
) -> None:
    with pytest.raises(ConsoleStateError, match="acquire"):
        client.acquire_frame(FrameRequest(frame_length=10))
    assert fake.commands == []
    assert not fake.died


def test_and_a_client_that_bypasses_the_guard_kills_the_console(
    client: Console, fake: FakeConsole
) -> None:
    """The hazard the guard exists for, demonstrated once.

    The console dereferences an acquisition chain that only `acquire`
    creates. There is no error reply for this; the process dies, and every
    later request times out.
    """
    client.acquiring = True  # what no caller should ever do
    client.timeout = 0.25
    with pytest.raises(ConsoleTimeout):
        client.acquire_frame(FrameRequest(frame_length=10))
    assert died(fake)
    with pytest.raises(ConsoleTimeout):
        client.info()


def test_a_second_acquire_without_a_stop_is_refused(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    assert stream.endpoint  # present so the stand-in has a subscriber to publish to
    client.configure(offset_v=0.251)
    client.acquire(timeout=5.0)
    with pytest.raises(ConsoleStateError, match="already running"):
        client.acquire(timeout=5.0)
    assert not fake.died
    # Stopping the open-ended acquisition joins the thread, and then it is fine.
    client.stop_frame()
    client.acquire(timeout=5.0)
    assert not fake.died


def test_and_a_second_acquire_that_bypasses_the_guard_kills_the_console(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    """The hazard that guard exists for.

    `acquire` replaces the console's acquisition chain, which destroys the
    thread the previous one is running on without joining it. In C++ that
    calls `std::terminate`.
    """
    client.configure(offset_v=0.251)
    client.acquire(timeout=5.0)
    client.running = False  # what no caller should ever do
    client.timeout = 0.25
    with pytest.raises(ConsoleTimeout):
        client.acquire(timeout=0.25)
    assert died(fake)


def test_a_frame_asked_for_while_one_is_running_is_refused(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    client.configure(offset_v=0.251)
    start_chain(client, stream, timeout=5.0, settle=2.0)
    client.acquire_frame(FrameRequest(frame_length=100))
    with pytest.raises(ConsoleStateError, match="not been stopped"):
        client.acquire_frame(FrameRequest(frame_length=100))
    assert not fake.died


def test_and_a_frame_that_bypasses_that_guard_is_acknowledged_and_dropped(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    """The gentler half of the ordering rule, and the one that hides.

    A frame asked for while the console is still acquiring is acknowledged,
    logged, and never started. The client that sent it waits for a `finished`
    that no acquisition will publish.
    """
    client.configure(offset_v=0.251)
    width = client.acquire(timeout=5.0)
    assert width.num_samples
    client.running = False  # what no caller should ever do
    client.acquire_frame(FrameRequest(frame_length=100))
    assert fake.ignored_frames == 1
    assert fake.frames == []
    assert not fake.died
    with pytest.raises(StreamTimeout, match="did not arrive"):
        stream.wait_for_status(FINISHED, timeout=0.5)


def test_and_a_frame_after_an_unstopped_frame_kills_the_console(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    """The third way to break the rule, and the one a naive loop would take.

    A frame that has published its `finished` has ended, so the guard that
    drops a frame while one is running does not fire; but its thread has not
    been joined, and starting another destroys it.
    """
    client.configure(offset_v=0.251)
    start_chain(client, stream, timeout=5.0, settle=2.0)
    client.acquire_frame(FrameRequest(frame_length=100))
    stream.wait_for_status(FINISHED, timeout=10.0)
    client.running = False  # what run_frame's stop_frame() is for
    client.timeout = 0.25
    with pytest.raises(ConsoleTimeout):
        client.acquire_frame(FrameRequest(frame_length=100))
    assert died(fake)


def test_start_chain_leaves_the_stream_holding_nothing(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    """What `start_chain` is for: the open-ended acquisition cleared away.

    `acquire` starts an acquisition of its own, which publishes batches and,
    when stopped, a `finished`. All of it is consumed, so the next frame's
    `finished` is the next status a caller sees.
    """
    client.configure(offset_v=0.251)
    start_chain(client, stream, timeout=5.0, settle=2.0)
    assert [status.text for status in stream.statuses] == [FINISHED]
    assert stream.batches == fake.open_batches
    assert stream.poll(0.2) is None


# --------------------------------------------------------------------------
# A whole acquisition
# --------------------------------------------------------------------------


def test_one_frame_from_configure_to_finished(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    client.configure(offset_v=0.251)
    width = start_chain(client, stream, timeout=5.0, settle=2.0)
    assert client.acquiring and not client.running
    assert width.num_samples == fake.num_samples
    assert fake.chain

    request = FrameRequest(frame_length=250, file_name="frame.uimf", frame_number=4,
                           nbr_accumulations=100, offset_bins=20000)
    batches: list[Batch] = []
    end = run_frame(client, stream, request, timeout=10.0, on_batch=batches.append)
    assert fake.frames == [request]
    assert end.is_finished
    assert end.topic == "status"
    # Every scan of the frame appeared, in batches of NotifyOnScansCount.
    assert sum(batch.scans for batch in batches) == 250
    assert [batch.scans for batch in batches] == [100, 100, 50]
    assert all(batch.mz.size == fake.num_samples for batch in batches)
    assert fake.ignored_frames == 0

    client.stop_acquire()
    assert not client.acquiring and not client.running
    assert stream.wait_for_status(FINISHED_ACQUIRE, timeout=10.0).is_finished_acquire


def test_a_frame_that_writes_no_file_still_publishes(
    client: Console, stream: DataStream
) -> None:
    """An empty `file_name` means publish and write nothing.

    Which is how the period, the record size and the occupancy of a
    zero-suppressed spectrum get measured before any UIMF file exists.
    """
    client.configure(offset_v=0.251)
    start_chain(client, stream, timeout=5.0, settle=2.0)
    batches: list[Batch] = []
    run_frame(client, stream, FrameRequest(frame_length=100), timeout=10.0,
              on_batch=batches.append)
    assert sum(batch.scans for batch in batches) == 100


def test_repeated_frames_are_numbered_and_all_arrive(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    """The shape of a `per_repetition` method frame: one console frame each.

    Three repetitions of one ion mobility experiment, each its own console
    frame with its own `finished` (lab record, task 02).
    """
    client.configure(offset_v=0.251)
    start_chain(client, stream, timeout=5.0, settle=2.0)
    scans_before = stream.scans
    for number in (1, 2, 3):
        run_frame(client, stream,
                  FrameRequest(frame_length=100, frame_number=number,
                               file_name="rep.uimf", offset_bins=20000),
                  timeout=10.0)
    assert [frame.frame_number for frame in fake.frames] == [1, 2, 3]
    assert stream.scans - scans_before == 300
    assert [status.text for status in stream.statuses[-3:]] == [FINISHED] * 3
    assert fake.ignored_frames == 0


def test_the_status_topic_is_not_the_data_topic(
    client: Console, stream: DataStream
) -> None:
    """The correction that made this client work at all.

    The console publishes batches on `data` and frame ends on `status`, and
    its own test client subscribes only to the first. A client that copies it
    receives every batch and never learns that a frame ended.
    """
    client.configure(offset_v=0.251)
    start_chain(client, stream, timeout=5.0, settle=2.0)
    client.acquire_frame(FrameRequest(frame_length=100))
    seen = []
    while True:
        event = stream.poll(2.0)
        if event is None:
            break
        seen.append(event)
    assert any(isinstance(event, Batch) for event in seen)
    assert any(isinstance(event, Status) for event in seen)
    assert {event.topic for event in seen if isinstance(event, Status)} == {"status"}


def test_messages_reach_a_caller_in_the_order_they_were_published(
    client: Console, stream: DataStream
) -> None:
    """Why both topics come off one socket.

    Two sockets would be two connections with no ordering between them, so a
    frame end could be read before batches that were published before it. One
    subscription on the empty prefix cannot do that, which is what lets
    `wait_for_status` hand batches to a caller as they arrive.

    What this does not show, because the stand-in publishes a frame's batches
    from inside the handler that started it, is that a frame's batches are
    published before its end. On the real console they frequently are not: the
    batches go out from a subscriber thread and `finished` from the
    acquisition thread, and a fully occupied frame delivered none of its
    batches before its own end (lab record, task 20). Ordering on the wire is
    what holds; ordering in time is not.
    """
    client.configure(offset_v=0.251)
    start_chain(client, stream, timeout=5.0, settle=2.0)
    seen: list[object] = []
    run_frame(client, stream, FrameRequest(frame_length=250), timeout=10.0,
              on_batch=seen.append)
    assert [type(event).__name__ for event in seen] == ["Batch"] * 3
    assert sum(event.scans for event in seen if isinstance(event, Batch)) == 250


def test_stopping_a_frame_keeps_the_chain(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    client.configure(offset_v=0.251)
    client.acquire(timeout=5.0)
    client.stop_frame()
    assert stream.wait_for_status(FINISHED, timeout=5.0).is_finished
    assert fake.chain and not fake.running and not fake.unjoined
    assert client.acquiring and not client.running
    assert ("stop", "frame") in fake.commands


# --------------------------------------------------------------------------
# A frame that failed
# --------------------------------------------------------------------------
#
# The console ends a frame the same way whether it acquired every scan or
# died on its first fetch, so none of the below is visible in the messages
# themselves. Each is a way the client tells them apart (lab record, task 20).


def opened(client: Console, stream: DataStream) -> None:
    client.configure(offset_v=0.251)
    start_chain(client, stream, timeout=5.0, settle=2.0)


def test_an_error_status_is_told_apart_from_an_ordinary_one() -> None:
    error = Status(text=f"{ERROR_PREFIX} Invalid value (1000) for parameter")
    assert error.is_error
    assert error.error_text == "Invalid value (1000) for parameter"
    for text in (FINISHED, FINISHED_ACQUIRE, "errors were had"):
        assert not Status(text=text).is_error
        assert Status(text=text).error_text == ""


def test_a_frame_that_published_nothing_is_not_a_success(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    """The whole point of the exercise.

    A stock console logs an acquisition that failed and then publishes the
    same `finished` a good frame ends with, so this is the only sign there is.
    """
    opened(client, stream)
    fake.frame_batches = 0
    with pytest.raises(EmptyFrameError) as raised:
        run_frame(client, stream, FrameRequest(frame_length=250), timeout=5.0, settle=0.2)
    assert "250" in str(raised.value)
    # And it was still stopped, so the next frame may start.
    assert not client.running and not fake.unjoined and not fake.died


def test_and_a_caller_that_wants_an_empty_frame_may_have_one(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    opened(client, stream)
    fake.frame_batches = 0
    end = run_frame(client, stream, FrameRequest(frame_length=250), timeout=5.0,
                    allow_empty=True)
    assert end.is_finished


def test_a_frame_shorter_than_it_asked_for_is_not_treated_as_a_failure(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    """Deliberately not an error, because the count is not trustworthy.

    The data socket drops messages when a client falls behind, so a scan
    count short of `frame_length` says as much about the client as about the
    acquisition. Nothing at all is the signal; too little is not.
    """
    opened(client, stream)
    fake.frame_batches = 1
    batches: list[Batch] = []
    end = run_frame(client, stream, FrameRequest(frame_length=250), timeout=5.0,
                    on_batch=batches.append)
    assert end.is_finished
    assert sum(batch.scans for batch in batches) == 100


def test_an_error_the_console_publishes_reaches_the_caller(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    opened(client, stream)
    fake.frame_error = ("Error Code: -1074135024 Error Message: Invalid value (1000) for "
                        "parameter nbrElementsToFetch: Must be strict positive multiple of 16.")
    with pytest.raises(ConsoleAcquisitionError) as raised:
        run_frame(client, stream, FrameRequest(frame_length=250), timeout=5.0)
    assert "nbrElementsToFetch" in str(raised.value)
    assert [status.error_text for status in stream.errors] == [fake.frame_error]


def test_the_consoles_own_words_beat_an_inference_from_no_scans(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    """A frame that failed and said so raises what it said, not what we guessed."""
    opened(client, stream)
    fake.frame_batches = 0
    fake.frame_error = "timeout in acquisition"
    with pytest.raises(ConsoleAcquisitionError):
        run_frame(client, stream, FrameRequest(frame_length=250), timeout=5.0, settle=0.2)


def test_a_frame_that_failed_does_not_leave_its_finished_for_the_next_one(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    """Why an error is collected and raised after the wait rather than during it.

    The console publishes its error and then the frame's ordinary `finished`.
    Raising the moment the error arrives would leave that `finished` on the
    socket for the next frame's wait to read as its own, and the next frame
    would appear to end before it had begun.
    """
    opened(client, stream)
    fake.frame_error = "timeout in acquisition"
    with pytest.raises(ConsoleAcquisitionError):
        run_frame(client, stream, FrameRequest(frame_length=100, frame_number=1), timeout=5.0)

    fake.frame_error = None
    batches: list[Batch] = []
    end = run_frame(client, stream, FrameRequest(frame_length=100, frame_number=2),
                    timeout=5.0, on_batch=batches.append)
    assert end.is_finished
    # Its own batches arrived before its own end, so the end it saw was not
    # the one left over from the frame before.
    assert sum(batch.scans for batch in batches) == 100
    assert fake.frames[-1].frame_number == 2


def test_an_error_with_no_finished_after_it_raises_the_error_not_the_timeout(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    """`start()` itself failing publishes an error and never a `finished`.

    A timeout would be true and useless; the console has already said why.
    """
    opened(client, stream)
    fake.frame_error = "Error processing UIMF request: no such file"
    client.acquire_frame(FrameRequest(frame_length=100))
    with pytest.raises(ConsoleAcquisitionError) as raised:
        stream.wait_for_status("nothing publishes this", timeout=2.0)
    assert "never arrived" in str(raised.value)
    assert "no such file" in str(raised.value)
    client.stop_frame()


def test_an_error_may_be_collected_without_raising(
    client: Console, stream: DataStream, fake: FakeConsole
) -> None:
    opened(client, stream)
    fake.frame_error = "timeout in acquisition"
    client.acquire_frame(FrameRequest(frame_length=100))
    end = stream.wait_for_status(FINISHED, timeout=5.0, raise_on_error=False)
    client.stop_frame()
    assert end.is_finished
    assert [status.error_text for status in stream.errors] == ["timeout in acquisition"]
