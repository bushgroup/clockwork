"""A series a point's own early stop ends short reads short to every client (lab #26; lab
record, task 171): the end's line, `progress`, the run record and the command line's exit
status all say which point stopped it, in which file and why, and which planned points
were never acquired. Ending the series is the rule (F3); reading it as done was the fault.

Over a `--fake` owner in process. A point's run stops itself as run 4 of the sitting the
issue came from did, on three frames in a row failing: the stand-in publishes nothing for
any frame from that point on, and the template has four repetitions a file so that three
failures in a row fit inside one.
"""

from __future__ import annotations

import io
import os

import pytest

from clockwork.acq import BeamGuard
from clockwork.acq.uimf import RAW_SUFFIX, SUMMED_SUFFIX
from clockwork.mcp import cli
from clockwork.owner import JobFinished, PointStarted, SeriesResult
from clockwork.owner.keeper import stem_of
from test_daemon import ended
from test_hold import WINDOW, at_rest, begin, plan_job, watched
from test_plan_runner import agent, one_record
from test_series import TWO_KNOBS, plan_text

__all__ = ["watched"]  # a fixture, imported for its use here

FOUR_REPETITIONS = TWO_KNOBS.replace("accumulations = 2", "accumulations = 4")

ENDS = plan_text(references='["start", "end"]',
                 extra='description = "a point between two references"\nshuffle = false',
                 middle="[[points]]\nb_ticks = 300")
"""The start reference, one point, the end reference: planned 1, 2 and 3, in that order."""

THREE = plan_text(references="[]", extra='description = "three"\nshuffle = false',
                  middle="[[points]]\nb_ticks = 300\n[[points]]\nb_ticks = 400\n"
                         "[[points]]\nb_ticks = 450")

FAILS = "3 frames in a row failed"


@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "library"
    folder.mkdir()
    (folder / "two-knob.toml").write_text(FOUR_REPETITIONS, encoding="utf-8")
    return str(folder)


def fail_from_point(watched, number: int) -> None:
    """Every frame from point `number` on publishes nothing, so its run stops itself."""
    owner = watched.owner
    watched.when(lambda event: isinstance(event, PointStarted) and event.number == number,
                 lambda: setattr(owner.console.fake, "frame_batches", 0))


def stem_of_summary(summary: dict) -> str:
    """A `progress` run's stem, as `stem_of` reads a `Run`'s."""
    name = os.path.basename(summary["raw_path"] or summary["summed_path"])
    for suffix in (SUMMED_SUFFIX, RAW_SUFFIX):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def test_a_point_that_stops_itself_ends_the_series_short_and_every_client_reads_it(
        watched, library, tmp_path):
    owner = watched.owner
    output = tmp_path / "runs"
    toolbox = agent(owner, library, output)
    toolbox.call("discover_boxes", {"template": "two-knob.toml",
                                    "labels": {"sample": "test mixture"}})
    fail_from_point(watched, 2)
    started = toolbox.call("series", {"request": "between two references, please",
                                      "initials": "zz", "plan": ENDS})
    answer, last, events = {"done": False}, 0, []
    while not answer["done"]:
        answer = toolbox.call("progress", {"job": started["job"], "after": last,
                                           "wait_s": 5})
        events += answer["events"]
        last = answer["last"]

    # Still a finish, its files valid, but not complete.
    result = answer["result"]
    assert result["points_done"] == 1 and result["points"] == 3
    assert result["complete"] is False and result["stopped_at"] == 2
    stopped_file = result["stopped_file"]
    assert stopped_file == stem_of_summary(answer["runs"][-1])

    # `progress`, at the top beside `done`.
    assert answer["complete"] is False
    assert answer["not_acquired"] == [{"index": 3, "reference": "end"}]
    assert answer["stopped"].startswith(f"point 2 of 3 (planned 2, {stopped_file}): {FAILS}")

    # The end's line: what the CLI's standard error and an agent's events end on.
    [end] = [event for event in events if event["kind"] == "JobFinished"]
    assert ": done" not in end["text"]
    assert "stopped short, 1 of 3 points complete, point 2 of 3" in end["text"]
    assert end["text"].endswith("Never acquired: planned 3 (the end reference).")

    # The record: points and files counted apart.
    [note] = [note["text"] for note in one_record(output)["notes"]
              if note["text"].startswith(f"series job {started['job']} ")]
    assert "stopped short, 1 of 3 points complete" in note
    assert note.endswith("Never acquired: planned 3 (the end reference). 2 files written.")

    # The command line: neither 0 nor 1, whether it waited on the job or followed it.
    err = io.StringIO()
    assert cli._wait(toolbox, started["job"], err, "clockwork") == cli.SHORT == 3
    assert "stopped short" in err.getvalue()
    assert cli._follow(toolbox, {"job": started["job"]}, io.StringIO(), io.StringIO(),
                       "clockwork") == cli.SHORT


def test_a_retake_then_a_stop_counts_points_and_files_apart(watched, library, tmp_path):
    """Run 4's shape: a point the beam guard ended is acquired again after a person's
    Resume, and a later point stops itself. The retake's failed file is a file, not a
    point, and the stopped point's file is neither a complete point nor a missing one."""
    owner = watched.owner
    job = plan_job(library, tmp_path, THREE)
    owner.guard(BeamGuard(floor=1000.0, repetitions=1, source="a test's floor"),
                by="MB", client=WINDOW)
    owner.console.fake.beam = [0.0]  # point 1's first repetition has no beam
    fail_from_point(watched, 2)
    handle = begin(owner, job)
    at_rest(owner)
    owner.guard(None, by="MB", client=WINDOW)  # nothing for it to trip on, from here
    owner.resume(by="MB", client=WINDOW)
    finished = ended(owner, handle, timeout=300)
    assert isinstance(finished, JobFinished)
    result = finished.result
    assert not result.complete and result.points_done == 1
    [lost] = result.replaced
    assert result.numbers == (1, 1, 2), "the lost file, its retake, the stopped point's"
    assert result.stopped_at == 2 and result.stopped_file == stem_of(result.runs[-1])
    assert result.stopped_file != lost
    assert result.not_acquired == ((3, ""),)
    assert f"point 2 of 3 (planned 2, {result.stopped_file}): {FAILS}" in result.stopped
    assert "stopped short, 1 of 3 points complete" in finished.text

    [note] = [note["text"] for note in one_record(tmp_path / "runs")["notes"]
              if note["text"].startswith(f"series job {handle.id} ")]
    assert note.endswith(f"Never acquired: planned 3. 3 files written, 1 of them failed "
                         f"and replaced by a retake ({lost}).")


def test_a_stop_between_two_points_names_the_next_as_not_acquired():
    """A series a stop ended before a point began: no stopped point and no file, and
    that point leads what was never acquired."""
    result = SeriesResult(points=3, points_done=1,
                          stopped="before point 2 of 3 (planned 2): stopped at the window",
                          not_acquired=((2, ""), (3, "end")))
    assert not result.complete and result.stopped_at == 0 and not result.stopped_file
    assert result.shortfall == (
        "stopped short, 1 of 3 points complete, before point 2 of 3 (planned 2): stopped "
        "at the window. Never acquired: planned 2, 3 (the end reference).")
    whole = SeriesResult(points=2, points_done=2)
    assert whole.complete and whole.shortfall == ""
