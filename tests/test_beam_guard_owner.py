"""The beam guard as the owner's (lab record, task 168): armed by a person at the window,
watched by every acquisition, and a trip inside a series held for a person, whose Resume
acquires the point again from its start as new files under the same planned index.

The owner in process over `--fake`, then the guard command through a daemon; the window's
side is in `test_app_queue.py`.
"""

from __future__ import annotations

import os

import pytest

from clockwork.acq import BeamGuard
from clockwork.acq.uimf import SUMMED_SUFFIX
from clockwork.dayrecord import DayRecord
from clockwork.mcp import TOOLS
from clockwork.owner import (
    BEAM_GUARD,
    POINT,
    Acquire,
    Discover,
    GuardChanged,
    Held,
    HoldRefused,
    JobFinished,
    PointStarted,
    RemoteOwner,
    Resumed,
    Send,
    wire,
)
from clockwork.owner import hold as hold_module
from clockwork.summary import summarize
from test_daemon import Daemon, ended, wait_for
from test_hold import WINDOW, at_rest, begin, events_of, files, library, plan_job, watched
from test_plan_runner import one_record

__all__ = ["library", "watched"]  # fixtures, imported for their use here

GUARD = BeamGuard(floor=1000.0, repetitions=1, source="a test's floor")


def stamped(directory) -> list[tuple[str, int, int, str]]:
    """Each summed file's stem, planned index, position and outcome, by position."""
    rows = []
    for path in files(directory, SUMMED_SUFFIX):
        stamps = summarize(path)["clockwork"]
        rows.append((os.path.basename(path)[: -len(SUMMED_SUFFIX)],
                     stamps["ClockworkSeriesIndex"], stamps["ClockworkSeriesPosition"],
                     summarize(path).get("run_outcome", "")))
    return sorted(rows, key=lambda row: row[2])


def test_the_guard_is_a_persons_and_the_status_says_who(watched):
    owner = watched.owner
    with pytest.raises(HoldRefused, match="initials"):
        owner.guard(GUARD, by=" ")
    assert owner.status().guard is None
    owner.guard(GUARD, by="MB", client=WINDOW)
    guarding = owner.status().guard
    assert (guarding.guard, guarding.by, guarding.client) == (GUARD, "MB", WINDOW)
    assert guarding.since and "armed by MB" in guarding.text
    owner.guard(None, by="MB", client=WINDOW)
    assert owner.status().guard is None


def test_with_nothing_running_a_change_is_a_line_in_the_day_record(watched, tmp_path):
    owner = watched.owner
    owner.day = DayRecord(str(tmp_path))
    assert wait_for(lambda: owner.status().running is None, 30), "the console's start"
    assert owner.guard(GUARD, by="MB", client=WINDOW) == "the day record"
    assert owner.guard(None, by="JD", client=WINDOW) == "the day record"
    lines = [line for line in owner.day.lines() if line["kind"] == "guard"]
    assert [(line["by"], line["armed"]) for line in lines] == [("MB", True), ("JD", False)]
    assert lines[0]["floor"] == 1000.0 and lines[0]["source"] == "a test's floor"
    assert lines[1]["was"]["floor"] == 1000.0 and lines[1]["job"] is None


def test_a_trip_holds_the_series_and_a_resume_retakes_the_point(watched, library,
                                                                 tmp_path):
    """Point 1 acquires on a good beam; point 2's first repetition has none. The guard
    ends that file failed, the series rests paused by the guard, a person's Resume
    acquires point 2 again from its start at the next position, and the series ends
    whole, its record naming the file the retake replaced."""
    owner = watched.owner
    job = plan_job(library, tmp_path)
    runs = tmp_path / "runs"
    owner.guard(GUARD, by="MB", client=WINDOW)
    # Two repetitions a file: point 1's two are good, point 2's first is not.
    owner.console.fake.beam = [1.0, 1.0, 0.0]
    handle = begin(owner, job)
    hold = at_rest(owner)
    assert hold.guard and hold.by == BEAM_GUARD and hold.grain == POINT
    assert hold.beam_lost.startswith("the beam was lost")
    assert "paused by the beam guard" in hold.text and "acquires that point again" in hold.text
    assert "the beam guard" in hold_module.held(hold)
    # A person's pause cannot be asked over it; only resume or stop.
    with pytest.raises(HoldRefused, match="already paused by the beam guard"):
        owner.pause(POINT, by="MB", client=WINDOW)
    assert len(files(runs)) == 2, "point 1's file and point 2's failed one"

    owner.resume(by="JD", client=WINDOW)
    finished = ended(owner, handle, timeout=180)
    assert isinstance(finished, JobFinished)
    result = finished.result
    assert result.points_done == 2 and not result.stopped
    [failed_stem] = result.replaced
    assert len(result.runs) == 3

    (first, one, _, _), (lost, two, two_at, _), (retake, again, again_at, _) = stamped(runs)
    assert lost == failed_stem
    assert (one, two, again) == (1, 2, 2), "the retake keeps its planned index"
    assert again_at == two_at + 1, "and takes the next position"

    started = [event for event in events_of(owner, handle) if isinstance(event, PointStarted)]
    assert [(event.number, event.retake) for event in started] == [(1, 0), (2, 0), (2, 1)]
    assert "acquired again (retake 1)" in started[-1].text
    assert any(isinstance(event, Held) and event.hold.guard
               for event in events_of(owner, handle))
    assert any(isinstance(event, Resumed) and event.by == "JD"
               for event in events_of(owner, handle))

    record = one_record(runs)
    paused, resumed = record["interventions"][-2:]
    assert (paused["kind"], paused["by"], paused["reason"]) == (
        "pause", BEAM_GUARD, hold.beam_lost)
    assert (resumed["kind"], resumed["by"]) == ("resume", "JD")
    [retaken] = record["retakes"]
    assert (retaken["index"], retaken["retake"], retaken["replaces"]) == (2, 1, [lost])
    assert {entry["stem"] for entry in record["files"]} == {first, lost, retake}


def test_a_stop_while_the_guard_holds_ends_the_series(watched, library, tmp_path):
    owner = watched.owner
    job = plan_job(library, tmp_path)
    owner.guard(GUARD, by="MB", client=WINDOW)
    owner.console.fake.beam = [0.0]
    handle = begin(owner, job)
    at_rest(owner)
    owner.stop("stopped at the window", by="MB", client=WINDOW)
    finished = ended(owner, handle, timeout=120)
    assert isinstance(finished, JobFinished)
    assert finished.result.points_done == 0 and finished.result.stopped
    assert finished.result.replaced == ()
    assert owner.status().hold is None


def test_a_guard_armed_while_a_series_runs_is_an_intervention_and_an_event(
        watched, library, tmp_path):
    owner = watched.owner
    job = plan_job(library, tmp_path)
    watched.when(lambda event: isinstance(event, PointStarted) and event.number == 1,
                 lambda: owner.guard(GUARD, by="MB", client=WINDOW))
    handle = begin(owner, job)
    assert isinstance(ended(owner, handle, timeout=180), JobFinished)
    changed = [event for event in events_of(owner, handle)
               if isinstance(event, GuardChanged)]
    assert [event.guarding.guard for event in changed] == [GUARD]
    [entry] = [entry for entry in one_record(tmp_path / "runs")["interventions"]
               if entry["kind"] == "guard"]
    assert (entry["by"], entry["armed"], entry["floor"], entry["repetitions"]) == (
        "MB", True, 1000.0, 1)


def test_a_lone_acquisition_the_guard_ends_fails_its_file_and_is_not_held(
        watched, library, tmp_path):
    """B2: a single acquisition has nothing to hold, so a trip ends it as before."""
    owner = watched.owner
    job = plan_job(library, tmp_path)
    owner.guard(GUARD, by="MB", client=WINDOW)
    owner.console.fake.beam = [0.0]
    method = job.points[0].method
    assert isinstance(ended(owner, owner.submit(Discover(method=method))), JobFinished)
    sent = owner.submit(Send(method=method, directory=str(tmp_path / "lone"), stem="zz_send",
                             instrument=job.instrument))
    assert isinstance(ended(owner, sent, timeout=60), JobFinished)
    handle = owner.submit(Acquire(method=method, directory=str(tmp_path / "lone"),
                                  initials="zz", replicates=2, instrument=job.instrument))
    finished = ended(owner, handle, timeout=120)
    assert isinstance(finished, JobFinished)
    [run] = finished.result
    assert run.beam_lost and owner.status().hold is None


def test_the_guard_and_its_event_cross_the_wire():
    for cls in (hold_module.Guarding, GuardChanged):
        value = wire.example(cls)
        assert wire.loads(wire.dumps(value)) == value


def test_the_guard_command_through_a_daemon_and_no_tool_holds_it(tmp_path):
    made = Daemon(tmp_path)
    client = RemoteOwner(made.endpoint, timeout=10, origin=WINDOW)
    try:
        assert client.guard(GUARD, by="MB") == "the day record"
        status = client.status()
        assert status.guard.guard == GUARD and status.guard.client == WINDOW
        client.guard(None, by="MB")
        assert wait_for(lambda: client.status().guard is None, 5)
        lines = [line for line in DayRecord(str(tmp_path)).lines()
                 if line["kind"] == "guard"]
        assert [line["armed"] for line in lines] == [True, False]
    finally:
        made.stop()
        client.close()
    assert len(TOOLS) == 28 and not any("guard" in tool.name for tool in TOOLS)
