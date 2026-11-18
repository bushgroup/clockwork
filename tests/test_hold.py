"""A person's pause, resume, stop and note on a running series (lab record, task 167).

The owner in process over `--fake`, then the same through a daemon: both grains of a
pause, what a held file says of itself, the run record's `interventions` with who made
each, a hold that outlives the client that asked for it, and what an agent's tools answer
while a person holds its series. The window's controls are driven in `test_app_plan.py`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import threading
import time

import pytest

from clockwork.acq import HOLD_RESUMED, FrameBegun, HoldBegun, HoldEnded, PhaseSent
from clockwork.acq.uimf import HELD_BEFORE_KEY, RAW_SUFFIX, SUMMED_SUFFIX
from clockwork.dayrecord import DayRecord
from clockwork.mcp import TOOLS, ToolFailure
from clockwork.mcp.server import SIMULATED
from clockwork.method import template as template_module
from clockwork.owner import (
    POINT,
    REPETITION,
    Acquire,
    DaemonRefused,
    Discover,
    Held,
    HoldRefused,
    JobFinished,
    JobStarted,
    LocalOwner,
    Noted,
    PauseAsked,
    PointStarted,
    ReadState,
    RemoteOwner,
    Resumed,
    RunDone,
    SeriesJob,
    StartConsole,
    StopAsked,
    wire,
)
from clockwork.owner import hold as hold_module
from clockwork.summary import summarize
from test_daemon import Daemon, ended, wait_for
from test_mcp import followed
from test_plan_runner import agent, one_record, window_check, window_job
from test_series import TWO_KNOBS, plan_text

WINDOW = "the clockwork window (pid 1)"

TWO = plan_text(references="[]", extra='description = "two holds"\nshuffle = false',
                middle="[[points]]\nb_ticks = 300\n[[points]]\nb_ticks = 450")
"""Two points of one file each, in planned order; each file is two repetitions."""

PAIRS = plan_text(references="[]", replicates=2, extra="shuffle = false",
                  middle="[[points]]\nb_ticks = 300\n[[points]]\nb_ticks = 450")


@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "library"
    folder.mkdir()
    (folder / "two-knob.toml").write_text(TWO_KNOBS, encoding="utf-8")
    return str(folder)


class Watched:
    """A `--fake` owner whose events can act: `when(predicate, act)` runs `act` once, on
    the owner's own thread, as the first event matching `predicate` is reported. That is
    what makes "asked while repetition 1 was in flight" a fact rather than a race."""

    def __init__(self) -> None:
        self._rules: list[tuple] = []
        self.owner = LocalOwner(fake=True, program="clockwork hold test",
                                on_event=self._seen).start()
        self.owner.submit(StartConsole())

    def when(self, predicate, act) -> None:
        self._rules.append((predicate, act))

    def _seen(self, _handle, event) -> None:
        for rule in list(self._rules):
            if rule[0](event):
                self._rules.remove(rule)
                rule[1]()

    def close(self) -> None:
        self.owner.shutdown()
        self.owner.join(30)


@pytest.fixture
def watched():
    made = Watched()
    yield made
    made.close()


def plan_job(library: str, tmp_path, text: str = TWO, name: str = "plan.toml") -> SeriesJob:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return window_job(window_check(str(path), library), tmp_path / "runs")


def begin(owner, job: SeriesJob):
    """Build the simulated rack from the series' first method, then submit the series."""
    found = owner.submit(Discover(method=job.points[0].method))
    assert isinstance(ended(owner, found), JobFinished)
    return owner.submit(job)


def at_rest(owner, timeout: float = 60.0):
    assert wait_for(lambda: owner.status().hold is not None and owner.status().hold.reached,
                    timeout), "the series never came to rest"
    return owner.status().hold


def events_of(owner, handle) -> list:
    return [entry.event for entry in owner.events(handle)]


def files(directory, suffix: str = SUMMED_SUFFIX) -> list[str]:
    return sorted(os.path.join(directory, name) for name in os.listdir(directory)
                  if name.endswith(suffix) and (suffix != RAW_SUFFIX
                                                or not name.endswith(SUMMED_SUFFIX)))


# -- when a pause may be asked -------------------------------------------------------------


def test_only_a_series_can_be_paused_and_only_where_it_can_rest(library, tmp_path):
    job = plan_job(library, tmp_path)
    per_repetition = job.points[0].method
    single = template_module.loads_template(
        TWO_KNOBS.replace('"per_repetition"', '"single_frame"'))
    single_frame = template_module.render(single, {}, {"sample": "x"}).method
    refusal = hold_module.refusal
    assert "nothing is running" in refusal(None, POINT)
    assert "only a series" in refusal(Acquire(), POINT)
    assert refusal(job, POINT, number=1, method=per_repetition) == ""
    assert refusal(job, REPETITION, number=2, method=per_repetition) == ""
    assert "last point" in refusal(job, POINT, number=2, method=per_repetition)
    assert "single_frame" in refusal(job, REPETITION, number=1, method=single_frame)
    assert "a stop has been asked" in refusal(job, POINT, number=1, stopping=True)
    assert "after this point or after this repetition" in refusal(job, "frame")
    resting = hold_module.Hold(job=3, grain=POINT, by="MB", since="2026-11-18T14:02:11",
                               where="after point 1 of 2 (planned 1)")
    assert "already paused by MB" in refusal(job, REPETITION, number=1, hold=resting)
    assert "job 3 is paused by MB at the window since 2026-11-18T14:02:11" in (
        hold_module.held(resting))


def test_nothing_running_and_a_lone_acquire_are_refused(watched, library, tmp_path):
    owner = watched.owner
    with pytest.raises(HoldRefused, match="nothing is running"):
        owner.pause(POINT, by="MB")
    with pytest.raises(HoldRefused, match="initials"):
        owner.pause(POINT, by=" ")
    with pytest.raises(HoldRefused, match="nothing is paused"):
        owner.resume(by="MB")
    job = plan_job(library, tmp_path)
    method = job.points[0].method
    assert isinstance(ended(owner, owner.submit(Discover(method=method))), JobFinished)
    refused: list[str] = []

    def ask() -> None:
        try:
            owner.pause(REPETITION, by="MB")
        except HoldRefused as exc:
            refused.append(str(exc))

    # A single acquisition has Stop alone (B2), whatever its grain could be.
    from clockwork.owner import Send
    runs = tmp_path / "runs"
    assert isinstance(ended(owner, owner.submit(
        Send(method=method, directory=str(runs), stem="261118_ZZ_001"))), JobFinished)
    watched.when(lambda event: isinstance(event, FrameBegun), ask)
    lone = owner.submit(Acquire(method=method, directory=str(runs), stem="261118_ZZ_001",
                                initials="ZZ", instrument=SIMULATED))
    assert isinstance(ended(owner, lone, timeout=120), JobFinished)
    assert refused and "only a series" in refused[0]
    assert owner.status().hold is None


# -- after this point ----------------------------------------------------------------------


def test_a_pause_after_the_point_rests_between_points_until_a_person_resumes(
        watched, library, tmp_path):
    owner = watched.owner
    job = plan_job(library, tmp_path)
    watched.when(lambda event: isinstance(event, PointStarted) and event.number == 1,
                 lambda: owner.pause(POINT, by="MB", client=WINDOW))
    handle = begin(owner, job)
    hold = at_rest(owner)
    assert (hold.job, hold.grain, hold.by, hold.client) == (handle.id, POINT, "MB", WINDOW)
    assert hold.where == "after point 1 of 2 (planned 1)" and hold.asked <= hold.since
    runs = tmp_path / "runs"
    assert len(files(runs)) == 1, "the first point's file is whole and the second not begun"

    # It holds the instrument: a job submitted meanwhile waits, for as long as it rests.
    behind = owner.submit(ReadState())
    time.sleep(0.6)
    status = owner.status()
    assert status.running == handle and behind in status.queued and status.hold == hold
    assert ended(owner, handle, timeout=0.2) is None

    owner.resume(by="MB", client=WINDOW)
    finished = ended(owner, handle, timeout=120)
    assert isinstance(finished, JobFinished) and finished.result.points_done == 2
    assert not finished.result.stopped and owner.status().hold is None
    assert isinstance(ended(owner, behind), JobFinished)

    kinds = [type(event).__name__ for event in events_of(owner, handle)
             if isinstance(event, (PauseAsked, Held, Resumed, StopAsked))]
    assert kinds == ["PauseAsked", "Held", "Resumed"]
    record = one_record(runs)
    asked, lifted = record["interventions"]
    assert (asked["kind"], asked["grain"], asked["by"], asked["client"], asked["job"]) == (
        "pause", POINT, "MB", WINDOW, handle.id)
    assert (lifted["kind"], lifted["by"], lifted["where"]) == ("resume", "MB", hold.where)
    assert lifted["held_since"] == hold.since and lifted["held_s"] >= 0.5
    # Between two points no file is open, so no file says it was held.
    assert all("ClockworkHolds" not in summarize(path)["clockwork"] for path in files(runs))
    assert all("holds" not in entry for entry in record["files"])


# -- after this repetition -----------------------------------------------------------------


def test_a_pause_after_the_repetition_rests_inside_the_file_and_the_file_says_so(
        watched, library, tmp_path):
    owner = watched.owner
    job = plan_job(library, tmp_path)
    watched.when(lambda event: isinstance(event, FrameBegun) and event.repetition == 1,
                 lambda: owner.pause(REPETITION, by="MB", client=WINDOW))
    handle = begin(owner, job)
    hold = at_rest(owner)
    runs = tmp_path / "runs"
    [raw] = files(runs, RAW_SUFFIX)
    stem = os.path.basename(raw).removesuffix(RAW_SUFFIX)
    assert hold.where == f"after method frame 1 repetition 1 of {stem}"
    time.sleep(0.5)
    assert owner.status().hold == hold and len(files(runs, RAW_SUFFIX)) == 1
    owner.resume(by="MB", client=WINDOW)
    finished = ended(owner, handle, timeout=120)
    assert isinstance(finished, JobFinished) and finished.result.points_done == 2

    held, unheld = finished.result.runs
    [record] = held.holds
    assert (record.method_frame, record.repetition, record.ended_by) == (1, 1, HOLD_RESUMED)
    assert record.seconds >= 0.5 and not unheld.holds and held.complete
    first, second = held.frames
    assert first.held_before_s is None and second.held_before_s == record.seconds
    assert all(frame.held_before_s is None for frame in unheld.frames)

    # B3: the first repetition after the hold is readied as a run's first is. The reset
    # list is walked before frame 1.1 and again before 1.2, and the gate is proved twice.
    events = events_of(owner, handle)
    begun = next(n for n, event in enumerate(events) if isinstance(event, HoldBegun))
    over = next(n for n, event in enumerate(events) if isinstance(event, HoldEnded))
    done = next(n for n, event in enumerate(events) if isinstance(event, RunDone))
    resets = [n for n, event in enumerate(events)
              if isinstance(event, PhaseSent) and event.phase == "reset" and n < done]
    assert len([n for n in resets if n < begun]) == 2
    assert len([n for n in resets if n > over]) == 2
    checks = [n for n, event in enumerate(events)
              if type(event).__name__ == "GateChecked" and n < done]
    assert len(checks) == 2 and checks[0] < begun < over < checks[1]

    # B4: both files say it, in clockwork's own parameters, and the raw frame is marked.
    for path in (held.raw_path, held.summed_path):
        stamps = summarize(path, texts=True)["clockwork"]
        assert stamps["ClockworkHolds"] == 1
        assert stamps["ClockworkHeldSeconds"] == pytest.approx(record.seconds, abs=0.01)
        assert stamps["ClockworkHoldText"].startswith(
            "after method frame 1 repetition 1, from ")
        assert "resumed: paused by MB at the window" in stamps["ClockworkHoldText"]
    assert "ClockworkHolds" not in summarize(unheld.summed_path)["clockwork"]
    with sqlite3.connect(held.raw_path) as conn:
        marked = conn.execute(
            "SELECT FrameNum, ParamValue FROM Frame_Params WHERE ParamID = ?",
            (HELD_BEFORE_KEY.param_id,)).fetchall()
    conn.close()
    assert [frame for frame, _ in marked] == [second.frame_number]
    assert float(marked[0][1]) == pytest.approx(record.seconds, abs=0.01)

    data = one_record(runs)
    entry = next(kept for kept in data["files"] if kept["stem"] == stem)
    assert entry["holds"] == [{"after": {"method_frame": 1, "repetition": 1},
                               "began": record.began, "seconds": round(record.seconds, 1),
                               "ended_by": "resumed"}]
    assert [kept["kind"] for kept in data["interventions"]] == ["pause", "resume"]
    assert data["interventions"][0]["grain"] == REPETITION

    from clockwork import manifest
    rows = {row["stem"]: row for row in manifest.manifest([str(runs)])["rows"]}
    assert rows[stem]["held_s"] == pytest.approx(record.seconds, abs=0.01)
    assert [row["held_s"] for name, row in rows.items() if name != stem] == [None]


def test_a_pause_asked_in_a_files_last_repetition_rests_before_the_next_file(
        watched, library, tmp_path):
    owner = watched.owner
    job = plan_job(library, tmp_path, PAIRS)
    watched.when(lambda event: isinstance(event, FrameBegun) and event.repetition == 2,
                 lambda: owner.pause(REPETITION, by="MB"))
    handle = begin(owner, job)
    hold = at_rest(owner)
    assert hold.where.startswith("after file 1 of 2 (") and "before the next file" in hold.where
    assert len(files(tmp_path / "runs")) == 1
    owner.resume(by="MB")
    finished = ended(owner, handle, timeout=180)
    assert isinstance(finished, JobFinished) and len(finished.result.runs) == 4
    assert not any(run.holds for run in finished.result.runs)


def test_a_pause_the_series_never_reaches_is_let_go_and_said(watched, library, tmp_path):
    owner = watched.owner
    job = plan_job(library, tmp_path)
    seen = {"files": 0}

    def last_repetition(event) -> bool:
        seen["files"] += isinstance(event, RunDone)
        return (seen["files"] == 1 and isinstance(event, FrameBegun)
                and event.repetition == 2)

    watched.when(last_repetition, lambda: owner.pause(REPETITION, by="MB"))
    handle = begin(owner, job)
    finished = ended(owner, handle, timeout=120)
    assert isinstance(finished, JobFinished) and owner.status().hold is None
    record = one_record(tmp_path / "runs")
    assert [kept["kind"] for kept in record["interventions"]] == ["pause"]
    assert any("was never reached: the job ended first" in note["text"]
               and note["by"] == "server" for note in record["notes"])


# -- stop ----------------------------------------------------------------------------------


def test_a_stop_while_paused_ends_the_series_as_stopped_while_paused(
        watched, library, tmp_path):
    owner = watched.owner
    job = plan_job(library, tmp_path)
    watched.when(lambda event: isinstance(event, FrameBegun) and event.repetition == 1,
                 lambda: owner.pause(REPETITION, by="MB", client=WINDOW))
    handle = begin(owner, job)
    hold = at_rest(owner)
    owner.stop("stopped at the window", by="KT", client=WINDOW)
    finished = ended(owner, handle, timeout=120)
    assert isinstance(finished, JobFinished)
    result = finished.result
    assert result.points_done == 0 and "while paused by MB at the window" in result.stopped
    [run] = result.runs
    assert run.stop_asked and len(run.frames) == 1 and run.holds[0].ended_by == "stopped"
    assert summarize(run.summed_path)["clockwork"]["ClockworkHolds"] == 1
    assert owner.status().hold is None

    record = one_record(tmp_path / "runs")
    asked, stopped = record["interventions"]
    assert (stopped["kind"], stopped["by"], stopped["client"]) == ("stop", "KT", WINDOW)
    assert stopped["while_paused"] is True and hold.since in stopped["reason"]
    assert any("stopped after 0 of 2 points" in note["text"] for note in record["notes"])


def test_every_stop_names_its_client_and_shutdown_ends_a_paused_series(
        watched, library, tmp_path):
    owner = watched.owner
    job = plan_job(library, tmp_path)
    watched.when(lambda event: isinstance(event, PointStarted) and event.number == 1,
                 lambda: owner.stop("the person asked for it to end",
                                    client="Claude, through clockwork mcp"))
    handle = begin(owner, job)
    assert isinstance(ended(owner, handle, timeout=120), JobFinished)
    [stopped] = one_record(tmp_path / "runs")["interventions"]
    assert stopped == {**stopped, "kind": "stop", "grain": "", "job": handle.id,
                       "by": "Claude, through clockwork mcp",
                       "client": "Claude, through clockwork mcp",
                       "reason": "the person asked for it to end"}
    assert "while_paused" not in stopped

    again = dataclasses.replace(plan_job(library, tmp_path, name="again.toml"),
                                directory=str(tmp_path / "more"))
    watched.when(lambda event: isinstance(event, PointStarted) and event.number == 1,
                 lambda: owner.pause(POINT, by="MB"))
    second = begin(owner, again)
    at_rest(owner)
    owner.shutdown("the daemon is closing")
    assert owner.join(60)
    last = [event for event in events_of(owner, second) if isinstance(event, JobFinished)]
    assert last and "while paused by MB" in last[0].result.stopped


# -- the note ------------------------------------------------------------------------------


def test_a_note_goes_to_the_running_jobs_record_and_to_its_followers(
        watched, library, tmp_path):
    owner = watched.owner
    with pytest.raises(HoldRefused, match="does not keep"):
        owner.note("the source was cleaned at noon", by="MB")
    owner.day = DayRecord(str(tmp_path / "runs"))
    assert owner.note(" the source was cleaned  at noon ", by="MB",
                      client=WINDOW) == "the day record"
    [line] = [line for line in owner.day.lines() if line["kind"] == "note"]
    assert (line["text"], line["by"], line["client"], line["job"]) == (
        "the source was cleaned at noon", "MB", WINDOW, None)
    with pytest.raises(HoldRefused, match="says something"):
        owner.note("  ", by="MB")
    with pytest.raises(HoldRefused, match="initials"):
        owner.note("x", by="")

    job = plan_job(library, tmp_path)
    kept: list[str] = []
    watched.when(lambda event: isinstance(event, PointStarted) and event.number == 1,
                 lambda: (kept.append(owner.note("flow dropped, topping up", by="MB",
                                                 client=WINDOW)),
                          owner.pause(POINT, by="MB")))
    handle = begin(owner, job)
    at_rest(owner)
    kept.append(owner.note("resumed after a new tip", by="KT", client=WINDOW))
    owner.resume(by="KT")
    assert isinstance(ended(owner, handle, timeout=120), JobFinished)
    record = one_record(tmp_path / "runs")
    assert all(where == f"the run record of job {handle.id}, request "
               f"{record['request']['id']}" for where in kept)
    people = [note for note in record["notes"] if "client" in note]
    assert [(note["text"], note["by"], note["client"]) for note in people] == [
        ("flow dropped, topping up", "MB", WINDOW), ("resumed after a new tip", "KT", WINDOW)]
    noted = [event for event in events_of(owner, handle) if isinstance(event, Noted)]
    assert [event.note for event in noted] == [note["text"] for note in people]
    assert "kept with the data" in noted[0].text
    assert len([line for line in owner.day.lines() if line["kind"] == "note"]) == 1


# -- through the daemon, and an agent's tools ----------------------------------------------


def test_the_new_events_and_the_status_cross_the_wire():
    for cls in (PauseAsked, Held, Resumed, StopAsked, Noted, HoldBegun, HoldEnded,
                hold_module.Hold):
        made = wire.example(cls)
        assert wire.loads(wire.dumps(made)) == made, cls.__name__


def test_a_hold_outlives_the_client_that_asked_and_the_tools_read_and_note_only(
        library, tmp_path):
    """A person's hold of an MCP-started series: every tool that would submit, `stop` and
    `stand_down` are refused at once with the hold's sentence; `status` and `progress`
    answer at once with it; `note` writes; and no tool pauses or resumes."""
    output = tmp_path / "runs"
    made = Daemon(output)
    session = RemoteOwner(made.endpoint, timeout=10, origin="Claude, through clockwork mcp")
    window = RemoteOwner(made.endpoint, timeout=10, origin=WINDOW)
    try:
        toolbox = agent(session, library, output)
        toolbox.call("discover_boxes", {"template": "two-knob.toml",
                                        "labels": {"sample": "test mixture"}})
        started = toolbox.call("series", {
            "request": "two holds, please", "initials": "zz", "plan": TWO})
        job = started["job"]

        def pause_at_first_point() -> bool:
            try:
                window.pause(POINT, by="MB")
            except DaemonRefused:
                return False
            return True

        assert wait_for(pause_at_first_point, 60), "the series never took a pause"
        hold = at_rest(window)
        assert hold.client == WINDOW and hold.job == job
        window.close()  # the window that asked has gone; the hold is the daemon's

        status = toolbox.call("status", {})
        assert status["paused"] == {
            "job": job, "grain": POINT, "by": "MB", "client": WINDOW, "asked": hold.asked,
            "since": hold.since, "where": hold.where, "text": hold.text}
        began = time.monotonic()
        answer = toolbox.call("progress", {"job": job, "after": 10**9, "wait_s": 30})
        assert time.monotonic() - began < 5, "progress answers at once while paused"
        assert answer["paused"] == status["paused"] and not answer["done"]

        sentence = hold_module.held(hold)
        for name, arguments in (
                ("stop", {}), ("stand_down", {}), ("stand_down", {"stop": True}),
                ("warm_up", {}), ("discover_boxes", {}), ("read_box_state", {}),
                ("snapshot_boxes", {}),
                ("arm", {"request": "another", "initials": "zz", "method": "x.toml"}),
                ("acquire", {"request": "another", "initials": "zz", "method": "x.toml"}),
                ("series", {"request": "another", "initials": "zz", "plan": TWO}),
                ("run_routine", {"name": "any", "initials": "zz"})):
            began = time.monotonic()
            with pytest.raises(ToolFailure) as refused:
                toolbox.call(name, arguments)
            assert str(refused.value) == sentence, name
            assert time.monotonic() - began < 2, f"{name} is refused at once"
        # A check submits nothing, and reading and noting go on.
        assert toolbox.call("series", {"request": "another", "initials": "zz",
                                       "plan": TWO, "check": True})["points"]
        noted = toolbox.call("note", {"request_id": started["request_id"],
                                      "text": "paused by MB; waiting to be told"})
        assert noted["notes"] >= 1
        assert not {"pause", "resume"} & {tool.name for tool in TOOLS}
        assert not made.server.owner.status().stopping  # type: ignore[union-attr]

        # Another window, opened later, finds the hold and lifts it.
        later = RemoteOwner(made.endpoint, timeout=10, origin="the clockwork window (pid 2)")
        try:
            assert later.status().hold == hold
            assert later.note("resuming: the tip was changed", by="KT").startswith(
                "the run record of job")
            later.resume(by="KT")
            with pytest.raises(DaemonRefused, match="nothing is paused"):
                later.resume(by="KT")
            answer = followed(toolbox, job, timeout=180)
        finally:
            later.close()
        assert answer["done"] and "paused" not in answer
        assert "paused" not in toolbox.call("status", {}) or toolbox.call(
            "status", {})["paused"] is None
        record = one_record(output)
        assert record["request"]["source"] == "mcp"
        assert [(kept["kind"], kept["by"], kept["client"])
                for kept in record["interventions"]] == [
            ("pause", "MB", WINDOW), ("resume", "KT", "the clockwork window (pid 2)")]
        texts = [(note["text"], note["by"]) for note in record["notes"]]
        assert ("paused by MB; waiting to be told", "session") in texts
        assert ("resuming: the tip was changed", "KT") in texts
        with open(output / "serve.log", encoding="utf-8") as handle:
            log = handle.read()
        assert "pause asked for from the clockwork window (pid 1) by MB" in log
    finally:
        session.close()
        made.stop()


def test_a_note_through_the_daemon_with_nothing_running_is_in_the_day_record(tmp_path):
    made = Daemon(tmp_path)
    window = RemoteOwner(made.endpoint, timeout=10, origin=WINDOW)
    try:
        assert wait_for(lambda: window.status().running is None
                        and not window.status().queued)
        assert window.note("MCP raised to 2.1 kV", by="MB") == "the day record"
        with pytest.raises(DaemonRefused, match="nothing is running"):
            window.pause(POINT, by="MB")
        lines = [line for line in DayRecord.beside(str(tmp_path)).lines()
                 if line["kind"] == "note"]
        assert [(line["text"], line["by"], line["client"]) for line in lines] == [
            ("MCP raised to 2.1 kV", "MB", WINDOW)]
    finally:
        window.close()
        made.stop()


def test_an_older_daemon_is_one_sentence(monkeypatch, tmp_path):
    from clockwork.owner import remote

    made = Daemon(tmp_path)
    window = RemoteOwner(made.endpoint, timeout=10, origin=WINDOW)
    try:
        window.hello()
        for name in ("pause", "resume", "note"):
            monkeypatch.delitem(remote._COMMANDS, name)
        with pytest.raises(DaemonRefused, match="older than this window") as refused:
            window.pause(POINT, by="MB")
        assert "there is no command" not in str(refused.value)
        with pytest.raises(DaemonRefused, match="cannot keep a note"):
            window.note("x", by="MB")
    finally:
        window.close()
        made.stop()


def test_the_tool_count_and_the_read_only_flags_have_not_moved():
    """B6: no verb pauses or resumes, so the count the paper's table is checked against
    stands: 28 tools, 13 read-only."""
    assert len(TOOLS) == 28
    assert sum(tool.read_only for tool in TOOLS) == 13
    assert json.dumps(sorted(tool.name for tool in TOOLS if tool.destructive)) == (
        '["stand_down", "stop"]')


def test_threads_asking_at_once_leave_one_hold(watched, library, tmp_path):
    owner = watched.owner
    job = plan_job(library, tmp_path)
    asked: list[object] = []

    def ask_twice() -> None:
        def one(grain: str) -> None:
            try:
                asked.append(owner.pause(grain, by="MB"))
            except HoldRefused as exc:
                asked.append(exc)
        threads = [threading.Thread(target=one, args=(grain,))
                   for grain in (POINT, REPETITION)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    watched.when(lambda event: isinstance(event, JobStarted)
                 and isinstance(event.job, SeriesJob), lambda: None)
    watched.when(lambda event: isinstance(event, PointStarted) and event.number == 1,
                 ask_twice)
    handle = begin(owner, job)
    at_rest(owner)
    assert len(asked) == 2
    owner.resume(by="MB")
    assert isinstance(ended(owner, handle, timeout=120), JobFinished)
