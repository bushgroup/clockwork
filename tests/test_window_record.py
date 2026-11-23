"""A run started at the window leaves a run record, written by the owner; the daemon
carries the sitting and keeps a day record (lab record, task 165).

The record's format is `docs/run-record.md`. These pin what the owner writes for a
window `Acquire` with no client alive at its end, a rendered one's template and knobs
against the sitting's limits, a failed one's note, that an `mcp` or `cli` record is what
it was, that the in-process `--fake` owner and a daemon leave the same record (A6), and
that the day record opens every segment with the sitting and is sealed with the audit log.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re

import pytest

from clockwork import envelope
from clockwork.dayrecord import LIVE_NAME, DayRecord, segments
from clockwork.method import template as template_module
from clockwork.owner import (
    Acquire,
    Discover,
    JobFailed,
    JobFinished,
    JobStarted,
    LocalOwner,
    RecordBegun,
    RemoteOwner,
    Send,
    StartConsole,
)
from clockwork.record import LATER_SECTIONS, SECTIONS, RunRecord, find, load, mint_id
from clockwork.sitting import Sitting, from_daemon
from test_daemon import Daemon, ended, make_instrument, make_method
from test_mcp import LABELS, TEMPLATE, limits_text

PERSON = "MB"
PURPOSE = "a check of the standard before the afternoon's series"


def _owner(**options) -> LocalOwner:
    owner = LocalOwner(fake=True, program="clockwork window-record test", **options).start()
    owner.submit(StartConsole())
    return owner


def _discover(owner, method, sends=None) -> None:
    """Find the stand-in rack, and send `method` (its send log in `sends`), so that an
    Acquire finds the boxes armed."""
    assert isinstance(ended(owner, owner.submit(Discover(method=method))), JobFinished)
    if sends is not None:
        sent = owner.submit(Send(method=method, directory=str(sends), stem="261118_SS_001"))
        assert isinstance(ended(owner, sent, timeout=60), JobFinished)


def _records(directory) -> list[str]:
    return sorted(name for name in os.listdir(directory) if name.endswith(".request.json"))


def _window_acquire(directory, method, **fields) -> Acquire:
    return Acquire(method=method, instrument=make_instrument(), directory=str(directory),
                   initials="ZZ", request=PURPOSE, source="window", at_window=PERSON,
                   **fields)


def _started(owner, handle) -> Acquire:
    """The job as the owner ran it: its request id, off the `RecordBegun` that follows
    the `JobStarted` of the job as submitted."""
    events = [entry.event for entry in owner.events(handle)]
    submitted = next(event.job for event in events if isinstance(event, JobStarted))
    assert not submitted.series  # the window's own object, as it sent it
    begun = next(event for event in events if isinstance(event, RecordBegun))
    assert os.path.basename(begun.path).endswith(".request.json")
    return dataclasses.replace(submitted, series=begun.request_id)


def test_a_window_acquire_leaves_a_whole_record_with_no_client_following(tmp_path):
    """Nothing follows the job but `ended`, which reads the owner's history: the record
    is the owner's own writing, whole before the end is reported."""
    from clockwork.mcp.tools import _series

    owner = _owner()
    try:
        method = make_method()
        _discover(owner, method, tmp_path / "sends")
        handle = owner.submit(_window_acquire(tmp_path, method, replicates=2))
        finished = ended(owner, handle, timeout=120)
        assert isinstance(finished, JobFinished), finished
        started = _started(owner, handle)
        identity = started.series
        assert re.fullmatch(r"\d{6}-\d{6}-[0-9a-f]{6}", identity)

        [name] = _records(tmp_path)
        stems = [os.path.basename(run.raw_path).removesuffix(".uimf")
                 for run in finished.result]
        assert name == f"{stems[0]}.request.json"
        data = load(os.path.join(tmp_path, name))
        assert data["record_schema"] == 1
        assert data["request"] == {
            "id": identity, "text": PURPOSE, "source": "window", "initials": "ZZ",
            "opened": data["request"]["opened"], "stem": stems[0], "at_window": PERSON}
        [acquisition] = data["acquisitions"]
        assert acquisition["job"] == handle.id
        assert acquisition["replicates"] == 2 and acquisition["first_place"] == 1
        assert acquisition["method"] == method.metadata.name
        assert len(acquisition["method_hash"]) == 64
        assert acquisition["template"] is None and "outside_limits" not in acquisition
        assert [entry["stem"] for entry in data["files"]] == stems
        assert all(entry["complete"] and entry["summary"]["frames"]
                   for entry in data["files"])
        assert "interventions" not in data

        # The files stamp the request, which no window file did before.
        stamped = [_series(run.summed_path) for run in finished.result]
        assert [(entry["id"], entry["index"]) for entry in stamped] == [
            (identity, 1), (identity, 2)]
    finally:
        owner.shutdown()
        assert owner.join(30)


def test_one_start_is_one_request_however_many_presses_follow(tmp_path):
    owner = _owner()
    try:
        method = make_method()
        _discover(owner, method, tmp_path / "sends")
        first = owner.submit(_window_acquire(tmp_path, method))
        second = owner.submit(_window_acquire(tmp_path, method, replicate_only=True))
        assert isinstance(ended(owner, first, timeout=120), JobFinished)
        assert isinstance(ended(owner, second, timeout=120), JobFinished)
        assert _started(owner, first).series != _started(owner, second).series
        assert len(_records(tmp_path)) == 2
    finally:
        owner.shutdown()
        assert owner.join(30)


@pytest.mark.parametrize("b_ticks, outside", [(400, 0), (500, 1)])
def test_a_rendered_window_run_records_its_template_and_any_knob_outside_the_limits(
        tmp_path, b_ticks, outside):
    """A person is never held to the agent's limits (R10): the run goes, and its record
    says which knob an agent's refusal would have named."""
    library = tmp_path / "library"
    library.mkdir()
    (library / "line-b.toml").write_text(TEMPLATE, encoding="utf-8")
    output = tmp_path / "runs"
    template = template_module.loads_template(TEMPLATE)
    rendered = template_module.render(template, {"b_ticks": b_ticks}, LABELS)
    owner = _owner()
    owner.limits = envelope.loads(limits_text(str(library)))
    try:
        _discover(owner, rendered.method)
        sent = owner.submit(Send(method=rendered.method, directory=str(output),
                                 stem="261118_ZZ_001"))
        assert isinstance(ended(owner, sent), JobFinished)
        handle = owner.submit(_window_acquire(
            output, rendered.method, template=TEMPLATE, knobs=dict(rendered.knobs),
            labels=dict(rendered.labels), method_path=str(library / "line-b.toml")))
        assert isinstance(ended(owner, handle, timeout=120), JobFinished)
        [name] = _records(output)
        [acquisition] = load(os.path.join(output, name))["acquisitions"]
        entry = acquisition["template"]
        assert entry["hash"] == template.hash
        assert entry["knobs"]["b_ticks"] == b_ticks
        assert entry["labels"] == LABELS
        assert entry["method_hash"] == acquisition["method_hash"]
        assert len(acquisition["outside_limits"]) == outside
        if outside:
            assert "b_ticks = 500" in acquisition["outside_limits"][0]
    finally:
        owner.shutdown()
        assert owner.join(30)


def test_a_window_run_under_no_limits_records_none_to_be_outside(tmp_path):
    rendered = template_module.render(template_module.loads_template(TEMPLATE),
                                      {"b_ticks": 500}, LABELS)
    owner = _owner()
    try:
        _discover(owner, rendered.method)
        sent = owner.submit(Send(method=rendered.method, directory=str(tmp_path),
                                 stem="261118_ZZ_001"))
        assert isinstance(ended(owner, sent), JobFinished)
        handle = owner.submit(_window_acquire(
            tmp_path, rendered.method, template=TEMPLATE, knobs=dict(rendered.knobs),
            labels=dict(rendered.labels)))
        assert isinstance(ended(owner, handle, timeout=120), JobFinished)
        [name] = _records(tmp_path)
        [acquisition] = load(os.path.join(tmp_path, name))["acquisitions"]
        assert acquisition["outside_limits"] is None
    finally:
        owner.shutdown()
        assert owner.join(30)


def test_a_failed_window_run_leaves_its_record_with_the_note_that_says_why(tmp_path):
    """A stem a finished run used is refused before anything is sent; the second Start
    is still a request, recorded beside the first and never over it."""
    owner = _owner()
    try:
        method = make_method()
        _discover(owner, method, tmp_path / "sends")
        first = owner.submit(_window_acquire(tmp_path, method, stem="261118_ZZ_007"))
        assert isinstance(ended(owner, first, timeout=120), JobFinished)
        again = owner.submit(_window_acquire(tmp_path, method, stem="261118_ZZ_007"))
        failed = ended(owner, again, timeout=120)
        assert isinstance(failed, JobFailed), failed
        identity = _started(owner, again).series
        found = find(str(tmp_path), identity)
        assert os.path.basename(found) == f"261118_ZZ_007-{identity}.request.json"
        data = load(found)
        assert data["files"] == []
        [note] = data["notes"]
        assert note == {"time": note["time"], "by": "server",
                        "text": f"job {again.id} failed: {failed.message}"}
    finally:
        owner.shutdown()
        assert owner.join(30)


def test_an_mcp_or_cli_record_is_begun_as_it_was(tmp_path):
    """No key a window record adds: the request block has its six, and the sections
    are the seven, with `interventions` begun only by its first entry."""
    for source in ("mcp", "cli"):
        record = RunRecord.open(str(tmp_path), f"261118_ZZ_00{len(source)}",
                                request_id=mint_id(source), text="words", source=source,
                                initials="ZZ", at_window="ignored")
        data = record.read()
        assert list(data) == ["record_schema", "request", *SECTIONS]
        assert list(data["request"]) == ["id", "text", "source", "initials", "opened",
                                         "stem"]
    record.append("interventions", {"kind": "stop", "grain": "", "by": "MB",
                                    "client": "the clockwork window (pid 1)", "job": 4,
                                    "reason": ""})
    assert record.read()["interventions"][0]["kind"] == "stop"
    assert LATER_SECTIONS == ("interventions", "retakes")
    with pytest.raises(ValueError):
        record.append("pauses", {})


def test_a_fake_window_and_a_daemon_leave_the_same_record(tmp_path):
    """A6: the `--fake` window keeps its owner in process, and its run's record is the
    one a run through `clockwork serve` leaves, field for field."""
    method = make_method()

    def shape(directory) -> dict:
        [name] = _records(directory)
        data = load(os.path.join(directory, name))
        request = {key: value for key, value in data["request"].items()
                   if key not in ("id", "opened", "stem")}
        [acquisition] = data["acquisitions"]
        return {"keys": list(data), "request": request,
                "acquisition": sorted(key for key in acquisition if key != "time"),
                "files": [sorted(entry) for entry in data["files"]],
                "summaries": [sorted(entry["summary"]) for entry in data["files"]]}

    in_process = tmp_path / "window"
    owner = _owner()
    try:
        _discover(owner, method, tmp_path / "sends")
        handle = owner.submit(_window_acquire(in_process, method, replicates=2))
        assert isinstance(ended(owner, handle, timeout=120), JobFinished)
    finally:
        owner.shutdown()
        assert owner.join(30)

    through = tmp_path / "daemon"
    through.mkdir()
    made = Daemon(through)
    client = RemoteOwner(made.endpoint, timeout=10, origin="the clockwork window (test)")
    try:
        _discover(client, method, tmp_path / "daemon-sends")
        handle = client.submit(_window_acquire(through, method, replicates=2))
        assert isinstance(ended(client, handle, timeout=120), JobFinished)
    finally:
        made.stop(client)
        client.close()
    assert shape(in_process) == shape(through)


# -- the sitting and the day record ----------------------------------------------------


def test_the_daemon_carries_the_sitting_and_hello_reports_it(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    (library / "line-b.toml").write_text(TEMPLATE, encoding="utf-8")
    limits = tmp_path / "limits.toml"
    limits.write_text(limits_text(str(library)), encoding="utf-8")
    output = tmp_path / "runs"
    output.mkdir()
    made = Daemon(output, limits=str(limits))
    client = RemoteOwner(made.endpoint, timeout=10)
    try:
        hello = client.hello()
        assert hello.limits == str(limits) and hello.instrument == ""
        assert hello.project_limits == ""
        # A client that names none of its own takes the daemon's, and one that names
        # its own instrument takes the limits beside it instead.
        assert from_daemon(hello) == ("", str(limits), "", False)
        assert from_daemon(hello, instrument_path="x.toml") == ("x.toml", "", "", False)
    finally:
        made.stop(client)
        client.close()

    lines = list(DayRecord.beside(str(output)).lines())
    sitting = lines[0]
    assert sitting["kind"] == "sitting" and sitting["fake"] is True
    assert sitting["limits"]["path"] == str(limits)
    assert len(sitting["limits"]["sha256"]) == 64
    assert sitting["instrument"] is None and sitting["project_limits"] is None
    kinds = [(line["kind"], line.get("event")) for line in lines]
    assert kinds[1] == ("daemon", "started")
    assert ("console", "started") in kinds
    assert kinds[-1] == ("daemon", "stopped")
    console = next(line for line in lines if line["kind"] == "console")
    assert console["by"] == "clockwork serve"


def test_a_daemon_started_again_in_a_live_segment_writes_its_own_sitting(tmp_path):
    # rc17: serves started again in one folder, with no seal between them, wrote no
    # sitting line, so one started on other limits left the day record silent about them.
    library = tmp_path / "library"
    library.mkdir()
    (library / "line-b.toml").write_text(TEMPLATE, encoding="utf-8")
    limits = tmp_path / "limits.toml"
    limits.write_text(limits_text(str(library)), encoding="utf-8")
    output = tmp_path / "runs"
    output.mkdir()
    for given in ({}, {"limits": str(limits)}):
        made = Daemon(output, **given)
        client = RemoteOwner(made.endpoint, timeout=10)
        try:
            client.hello()
        finally:
            made.stop(client)
            client.close()

    assert [os.path.basename(path) for path in segments(str(output))] == [LIVE_NAME]
    lines = list(DayRecord.beside(str(output)).lines())
    starts = [index for index, line in enumerate(lines)
              if (line["kind"], line.get("event")) == ("daemon", "started")]
    sittings = [index for index, line in enumerate(lines) if line["kind"] == "sitting"]
    assert len(starts) == 2 and sittings == [start - 1 for start in starts]
    first, second = (lines[index] for index in sittings)
    assert first["limits"] is None
    assert second["limits"]["path"] == str(limits)
    assert second["session"] == lines[starts[1]]["session"] != first["session"]


def test_a_daemon_whose_limits_do_not_load_says_so_and_exits(tmp_path):
    bad = tmp_path / "limits.toml"
    bad.write_text("schema_version = 99\n", encoding="utf-8")
    made = Daemon(tmp_path, limits=str(bad))
    made.thread.join(30)
    assert made.code == 1
    assert "could not be read" in made.log.getvalue()


def test_the_day_record_opens_every_segment_with_the_sitting_and_seals(tmp_path):
    day = DayRecord(str(tmp_path), sitting=lambda: {"version": "test",
                                                    **Sitting().entry()})
    day.write("daemon", event="started", by="clockwork serve")
    day.write("console", event="restarted", by="the console watch")
    sealed = day.seal()
    assert re.fullmatch(r"day-record\.\d{8}-\d{6}\.jsonl", sealed["name"])
    assert not os.path.exists(tmp_path / LIVE_NAME)
    day.write("console", event="stopped", by="clockwork serve")
    assert [os.path.basename(path) for path in segments(str(tmp_path))] == [
        sealed["name"], LIVE_NAME]
    kinds = [line["kind"] for line in day.lines()]
    assert kinds == ["sitting", "daemon", "console", "sitting", "console"]
    with open(tmp_path / LIVE_NAME, encoding="utf-8") as handle:
        assert json.loads(handle.readline())["version"] == "test"
    # Neither name is a run's stem, so neither moves a file counter.
    from clockwork.naming import next_number, parse_stem
    assert parse_stem(LIVE_NAME) is None and parse_stem(sealed["name"]) is None
    assert next_number(str(tmp_path), "ZZ") == 1
