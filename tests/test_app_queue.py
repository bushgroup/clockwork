"""The row queue as the owner's series job, and the beam guard as the owner's (lab record,
task 168).

The rules first, with no window in sight: what Start offers, how the job's progress is
written back onto the rows, a retake, the queue file without its two retired keys. Then a
`--fake` window, its owner in process (A6): a started queue is a snapshot with a run record
and series stamps, a document that will not open keeps it from starting, a stopped queue's
unrun rows start as a new queue naming the first, and the guard holds, resumes and
retakes. The same through a daemon, and a queue that outlives its window, are in
`test_app_remote.py`.
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import replace

import pytest

pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt  # noqa: E402

from clockwork import method as method_module  # noqa: E402
from clockwork import planning  # noqa: E402
from clockwork.acq import BeamGuard  # noqa: E402
from clockwork.app import runqueue  # noqa: E402
from clockwork.app.queuepanel import GUARD_OFF, GUARD_ON, QueuePanel  # noqa: E402
from clockwork.series import SEED_LIMIT  # noqa: E402
from clockwork.summary import summarize  # noqa: E402
from test_app import (  # noqa: E402, F401
    SCANS,
    a_run,
    idle,
    make_method,
    ready_to_acquire,
    scratch_settings,
    until,
    window,
)


def lettered(*names: str, **row) -> runqueue.RunQueue:
    return runqueue.RunQueue([runqueue.QueueRow(method_path=f"{n}.toml", **row)
                              for n in names])


def queued(window, tmp_path, name: str, method, **row) -> str:  # noqa: F811
    """Save a method under its own name and put a row for it in the window's queue."""
    path = str(tmp_path / f"{name}.toml")
    method_module.save(method, path)
    window.queue.add(runqueue.QueueRow(method_path=path, **row))
    window.queue_panel.refresh()
    return path


def records(directory) -> list[dict]:
    """Every run record in `directory`, oldest first."""
    found = []
    for path in glob.glob(os.path.join(str(directory), "*.request.json")):
        with open(path, encoding="utf-8") as handle:
            found.append(json.load(handle))
    return sorted(found, key=lambda record: record["request"]["opened"] + record[
        "request"]["id"])


def stamps(directory, stems) -> list[dict]:
    return [summarize(str(directory / f"{stem}.summed.uimf"))["clockwork"]
            for stem in stems]


def finished(window, qtbot, timeout: int = 300_000) -> None:  # noqa: F811
    until(qtbot, lambda: not window.queue.running and idle(window), timeout=timeout)


# --- the rules ------------------------------------------------------------------------------


def test_start_offers_the_waiting_and_skipped_rows_and_not_the_finished_ones():
    queue = lettered("a", "b", "c")
    queue.rows[0].state = runqueue.DONE
    queue.rows[1].state = runqueue.SKIPPED
    queue.rows[1].problem = "the queue stopped before this row"
    assert [row.name for row in queue.begin()] == ["b", "c"]
    assert queue.rows[1].state == runqueue.WAITING and not queue.rows[1].problem


def test_a_running_queue_is_its_snapshot_and_is_not_edited():
    queue = lettered("a", "b", "c")
    queue.start(queue.begin())
    assert queue.remove(0) is False
    assert queue.move(2, -1) == 2
    with pytest.raises(ValueError, match="running"):
        queue.randomize(2)
    assert [row.name for row in queue.rows] == ["a", "b", "c"]


def test_the_rows_follow_the_jobs_points_and_end_from_its_files():
    queue = lettered("a", "b", "c")
    queue.start(queue.begin())
    queue.request_id = "261118-140211-abcdef"
    queue.point_started(1)
    first = a_run("261118_ZZ_001")
    queue.run_done(1, first)
    queue.point_started(2)
    assert [row.state for row in queue.rows] == [runqueue.DONE, runqueue.RUNNING,
                                                 runqueue.WAITING]
    stopped = a_run("261118_ZZ_002", stopped="stopped at the window")
    queue.run_done(2, stopped)
    queue.ended([first, stopped], [1, 2], stopped="stopped at the window")
    a, b, c = queue.rows
    assert (a.state, b.state, c.state) == (runqueue.DONE, runqueue.STOPPED,
                                           runqueue.SKIPPED)
    assert a.stems == ("261118_ZZ_001",) and b.stems == ("261118_ZZ_002",)
    assert "stopped before this row" in c.problem
    assert not queue.running
    # F1: the rows not yet run start as a new queue naming this one.
    assert queue.follows == "261118-140211-abcdef"
    assert [row.name for row in queue.begin()] == ["c"]


def test_a_failed_queue_fails_the_row_running_and_skips_the_rest():
    """F3: no go-on. A failed row ends the queue as a failed point ends a series."""
    queue = lettered("a", "b", "c")
    queue.start(queue.begin())
    queue.point_started(1)
    queue.failed("box rejected 'TBLSTRT'")
    assert [row.state for row in queue.rows] == [runqueue.FAILED, runqueue.SKIPPED,
                                                 runqueue.SKIPPED]
    assert queue.rows[0].problem == "box rejected 'TBLSTRT'"
    assert queue.rows[1].problem == "a failed, and a failed row ends the queue"
    assert queue.follows == "", "no record was begun, so there is nothing to follow"


def test_a_retake_sets_the_guards_file_aside_and_the_row_is_judged_on_its_own():
    queue = lettered("a")
    queue.start(queue.begin())
    queue.point_started(1)
    reason = "the beam was lost: 5 repetitions in a row read under 1e+03 ions"
    lost = replace(a_run("261118_ZZ_001"), stopped_early=reason, stop_asked=False,
                   beam_lost=reason)
    queue.run_done(1, lost)
    assert queue.rows[0].beam_lost == reason
    queue.point_started(1, retake=1)
    row = queue.rows[0]
    assert row.replaced == ("261118_ZZ_001",) and row.stems == () and not row.beam_lost
    assert row.step == "acquiring again"
    again = a_run("261118_ZZ_002")
    queue.run_done(1, again)
    queue.ended([lost, again], [1, 1], replaced=["261118_ZZ_001"])
    assert row.state == runqueue.DONE and row.stems == ("261118_ZZ_002",)
    assert "acquired again after the beam guard ended 261118_ZZ_001" in row.outcome


def test_randomizing_draws_its_seed_as_a_plan_does_and_the_copies_carry_the_rows():
    """The seed is stamped into every file now, as an Int32: drawn under `SEED_LIMIT`."""
    queue = runqueue.RunQueue([runqueue.QueueRow(
        method_path="a.toml", conditions="10 uM", replicates=3, stays_first=True)])
    done = queue.randomize(2)
    assert 0 <= done.seed < SEED_LIMIT and queue.stamped_seed == done.seed
    one, two = queue.rows
    assert one is not two
    assert all((row.conditions, row.replicates, row.stays_first) == ("10 uM", 3, True)
               for row in (one, two))
    assert lettered("a").stamped_seed is None, "nobody shuffled it"


def test_finished_rows_stay_above_new_passes_and_skipped_ones_are_taken():
    queue = lettered("done", "skipped", "b")
    queue.rows[0].state = runqueue.DONE
    queue.rows[1].state = runqueue.SKIPPED
    queue.randomize(2, seed=4)
    assert queue.rows[0].name == "done" and queue.rows[0].pass_number == 0
    assert len(queue.rows) == 5
    assert all(row.state == runqueue.WAITING for row in queue.rows[1:])
    with pytest.raises(ValueError, match="no waiting rows"):
        runqueue.RunQueue().randomize(2)


def test_a_queue_file_no_longer_writes_setup_or_go_on_and_still_opens_one_that_does(
        tmp_path):
    for name in ("a", "b"):
        (tmp_path / f"{name}.toml").write_text("", encoding="utf-8")
    queue = runqueue.RunQueue([
        runqueue.QueueRow(method_path=str(tmp_path / "a.toml"), conditions="1 uM",
                          replicates=2),
        runqueue.QueueRow(method_path=str(tmp_path / "b.toml"), stays_first=True),
    ])
    queue.randomize(2, seed=99)
    path = str(tmp_path / "series.queue.toml")
    runqueue.save(queue, path)
    text = open(path, encoding="utf-8").read()
    assert "schema_version = 1" in text
    assert "setup" not in text and "go_on" not in text
    opened = runqueue.load(path)
    assert opened.seed == 99 and opened.ignored == 0 and opened.path == path
    fields = ("method_path", "conditions", "replicates", "stays_first", "pass_number",
              "pass_count")
    assert [[getattr(row, f) for f in fields] for row in opened.rows] == \
        [[getattr(row, f) for f in fields] for row in queue.rows]

    older = (f"schema_version = 1\n[[rows]]\nmethod = '{tmp_path.as_posix()}/a.toml'\n"
             "setup = false\ngo_on = true\n[[rows]]\n"
             f"method = '{tmp_path.as_posix()}/b.toml'\nsetup = true\ngo_on = false\n")
    old = runqueue.loads(older)
    assert len(old.rows) == 2 and old.ignored == 2
    with pytest.raises(runqueue.QueueFileError, match="`go_on` is not true or false"):
        runqueue.loads("schema_version = 1\n[[rows]]\nmethod = 'a.toml'\ngo_on = 3\n")


def test_the_panel_has_no_setup_or_go_on_column_and_locks_a_running_queue(qtbot):
    queue = lettered("a", "b")
    panel = QueuePanel(queue)
    qtbot.addWidget(panel)
    headings = [panel.tree.headerItem().text(column)
                for column in range(panel.tree.columnCount())]
    assert "setup" not in headings and "go on if it fails" not in headings
    assert panel.tree.topLevelItem(1).flags() & Qt.ItemFlag.ItemIsEditable
    queue.start(queue.begin())
    panel.refresh()
    assert not panel.tree.topLevelItem(1).flags() & Qt.ItemFlag.ItemIsEditable
    assert not panel.add_button.isEnabled() and not panel.start_button.isEnabled()
    assert panel.stop_button.isEnabled()
    queue.ended([], [], stopped="stopped at the window")
    panel.refresh()
    assert panel.start_button.isEnabled() and "2 to run" in panel.caption.text()


# --- the window over the stand-ins -------------------------------------------------------


def test_a_started_queue_is_one_series_job_with_a_record_and_stamps(
        window, tmp_path, qtbot):  # noqa: F811
    """F1, F2: Start reads and hashes every row; the files stamp the queue's series id,
    each row's index and the positions; the record's `plans` entry carries the queue's
    text and every row's document hash, and the hash its files stamp."""
    ready_to_acquire(window, qtbot, tmp_path)
    first = queued(window, tmp_path, "first", make_method(), conditions="10 uM")
    queued(window, tmp_path, "second", make_method(scans=SCANS * 2), replicates=2)
    window.run_panel.purpose.setText("two rows")
    job = window.start_queue()
    assert job is not None and window.queue.running
    # The snapshot is taken: an edit to the document now changes nothing that runs.
    method_module.save(make_method(scans=SCANS * 4), first)
    finished(window, qtbot)

    a, b = window.queue.rows
    assert [row.state for row in window.queue.rows] == [runqueue.DONE, runqueue.DONE]
    assert len(a.stems) == 1 and len(b.stems) == 2
    [record] = records(tmp_path)
    request = record["request"]
    assert (request["source"], request["text"], request["at_window"]) == (
        "window", "two rows", "ZZ")
    assert window.queue.request_id == request["id"]
    [plans] = record["plans"]
    assert plans["queue"] == job.queue and plans["sha256"] == planning.sha256(job.queue)
    assert plans["template"] is None and plans["checked"] is None
    assert plans["order"] == [1, 2] and plans["seed"] is None and plans["follows"] is None
    one, two = plans["rows"]
    assert (one["index"], one["name"], one["conditions"], one["replicates"]) == (
        1, "first", "10 uM", 1)
    assert two["replicates"] == 2 and one["sha256"] == job.points[0].sha256
    [acquisitions] = record["acquisitions"]
    assert acquisitions["queue"] is True and acquisitions["points"] == 2

    stamped = stamps(tmp_path, a.stems + b.stems)
    assert {entry["ClockworkSeriesId"] for entry in stamped} == {request["id"]}
    assert [(entry["ClockworkSeriesIndex"], entry["ClockworkSeriesPosition"])
            for entry in stamped] == [(1, 1), (2, 2), (2, 3)]
    assert stamped[0]["ClockworkMethodHash"] == one["method_hash"]
    # The method that ran is the one read at Start, not the one saved over it after.
    entry = next(entry for entry in record["files"] if entry["stem"] == b.stems[0])
    assert entry["summary"]["scans_per_frame"] == [SCANS * 2]
    assert sorted(entry["stem"] for entry in record["files"]) == sorted(a.stems + b.stems)


def test_a_row_runs_the_method_a_button_would_have_run(window, tmp_path, qtbot):  # noqa: F811
    """The same document acquired by Acquire through the panes and as a queue row stamps
    the same method hash: the snapshot is the document as the window would send it."""
    path = queued(window, tmp_path, "same", make_method())
    window.queue.rows.clear()
    window._load_method(path)
    until(qtbot, lambda: window.worker.boxes and idle(window))
    window.send(setup=True)
    until(qtbot, lambda: idle(window) and window._armed)
    runs: list[object] = []
    window.worker.run_done.connect(runs.append)
    window.replicates.setValue(1)
    window.acquire()
    until(qtbot, lambda: runs and idle(window), timeout=180_000)
    by_button = summarize(runs[0].summed_path)["clockwork"]["ClockworkMethodHash"]

    window.queue.add(runqueue.QueueRow(method_path=path))
    window.queue_panel.refresh()
    window.start_queue()
    finished(window, qtbot)
    [row] = window.queue.rows
    assert stamps(tmp_path, row.stems)[0]["ClockworkMethodHash"] == by_button


def test_a_row_whose_document_will_not_open_keeps_the_queue_from_starting(
        window, tmp_path, qtbot):  # noqa: F811
    """Start reads every row first, and says why on the row; no dialog, no job."""
    ready_to_acquire(window, qtbot, tmp_path)
    broken = tmp_path / "broken.toml"
    broken.write_text("this is not a method\n", encoding="utf-8")
    queued(window, tmp_path, "good", make_method())
    window.queue.add(runqueue.QueueRow(method_path=str(broken)))
    complaints: list[str] = []
    window._complain = lambda title, detail: complaints.append(title)
    assert window.start_queue() is None
    assert not window.queue.running and idle(window) and complaints == []
    assert window.queue.rows[1].problem.startswith("not started:")
    assert [row.state for row in window.queue.rows] == [runqueue.WAITING] * 2


def test_a_randomized_queue_stamps_its_seed_and_the_record_keeps_the_passes(
        window, tmp_path, qtbot):  # noqa: F811
    ready_to_acquire(window, qtbot, tmp_path)
    for name in ("a", "b"):
        queued(window, tmp_path, name, make_method())
    window._ask_randomize = lambda rows, passes: (2, True)
    window.randomize_queue()
    seed = window.queue.seed
    assert 0 <= seed < SEED_LIMIT
    job = window.start_queue()
    assert job.seed == seed
    finished(window, qtbot)
    stems = [stem for row in window.queue.rows for stem in row.stems]
    assert {entry["ClockworkSeriesSeed"] for entry in stamps(tmp_path, stems)} == {seed}
    [record] = records(tmp_path)
    plans = record["plans"][0]
    assert plans["seed"] == seed and plans["shuffled"] is True
    assert [row["name"].split(" (pass ")[1] for row in plans["rows"]] == [
        "1/2)", "1/2)", "2/2)", "2/2)"]


def test_stop_leaves_the_unrun_rows_and_start_runs_them_as_a_queue_naming_the_first(
        window, tmp_path, qtbot):  # noqa: F811
    ready_to_acquire(window, qtbot, tmp_path)
    queued(window, tmp_path, "long", make_method(accumulations=6))
    queued(window, tmp_path, "next", make_method())
    window.start_queue()
    qtbot.waitUntil(lambda: window.run_panel.progress.repetition >= 1, timeout=120_000)
    panel = window.queue_panel
    assert not panel.tree.topLevelItem(1).flags() & Qt.ItemFlag.ItemIsEditable
    assert not window.acquire_button.isEnabled()
    window.stop()
    finished(window, qtbot, 180_000)
    first_id = window.queue.request_id
    assert [row.state for row in window.queue.rows] == [runqueue.STOPPED,
                                                        runqueue.SKIPPED]
    assert window.queue.follows == first_id and panel.start_button.isEnabled()

    second = window.start_queue()
    assert second.follows == first_id and len(second.points) == 1
    finished(window, qtbot)
    assert window.queue.rows[1].state == runqueue.DONE
    first, then = records(tmp_path)
    assert first["request"]["id"] == first_id
    assert then["plans"][0]["follows"] == first_id
    stop = [entry for entry in first["interventions"] if entry["kind"] == "stop"]
    assert stop and stop[0]["by"] == "ZZ"


def test_an_older_queue_file_opens_with_one_line_saying_two_values_are_ignored(
        window, tmp_path):  # noqa: F811
    method_module.save(make_method(), str(tmp_path / "a.toml"))
    path = tmp_path / "old.queue.toml"
    path.write_text(f"schema_version = 1\n[[rows]]\nmethod = '{tmp_path.as_posix()}/a.toml'"
                    "\nsetup = false\ngo_on = true\n", encoding="utf-8")
    lines: list[str] = []
    window.run_panel.say = lambda text, **_: lines.append(text)
    window._queue_file_path = lambda save: str(path)
    window.open_queue()
    assert len(window.queue.rows) == 1 and window.queue.path == str(path)
    [said] = [line for line in lines if "ignored" in line]
    assert "1 row(s) of old.queue.toml" in said


def test_the_guard_is_armed_at_the_window_holds_the_queue_and_resume_retakes_the_row(
        window, tmp_path, qtbot):  # noqa: F811
    """B8 at the window: the guard is the owner's; a row whose beam is lost ends its file
    failed and the queue waits, held by the guard; the window's Resume acquires the row
    again from its start, and the row ends done with the replaced file named."""
    ready_to_acquire(window, qtbot, tmp_path)
    queued(window, tmp_path, "first", make_method(accumulations=6))
    queued(window, tmp_path, "second", make_method())
    guard = BeamGuard(floor=1000.0, repetitions=3, source="a test's floor")
    window._ask_guard = lambda current: (guard,)
    window.queue_guard()
    until(qtbot, lambda: window.worker.guarding is not None)
    window._show_guard()
    assert window.worker.guarding.guard == guard and window.worker.guarding.by == "ZZ"
    assert window.run_panel.guard_button.text() == GUARD_ON
    # Dark for exactly the three repetitions that trip it; the retake has its beam back.
    window.worker.console.fake.beam = [1.0, 0.0, 0.0, 0.0]

    window.start_queue()
    until(qtbot, lambda: window.worker.hold is not None and window.worker.hold.reached,
          timeout=180_000)
    assert window.worker.hold.guard
    until(qtbot, lambda: window.queue.rows[0].step == "held by the beam guard")
    first = window.queue.rows[0]
    assert first.beam_lost.startswith("the beam was lost: 3 repetitions in a row")
    lost = first.stems
    window.resume()
    finished(window, qtbot)

    first, second = window.queue.rows
    assert (first.state, second.state) == (runqueue.DONE, runqueue.DONE)
    assert first.replaced == lost and first.stems and first.stems != lost
    assert "acquired again after the beam guard ended" in first.outcome
    [record] = records(tmp_path)
    assert record["retakes"][0]["replaces"] == list(lost)
    kinds = [(entry["kind"], entry["by"]) for entry in record["interventions"]]
    assert ("pause", "the beam guard") in kinds and ("resume", "ZZ") in kinds
    stamped = stamps(tmp_path, lost + first.stems)
    assert {entry["ClockworkSeriesIndex"] for entry in stamped} == {1}

    window._ask_guard = lambda current: (None,)
    window.queue_guard()
    until(qtbot, lambda: window.worker.guarding is None)
    window._show_guard()
    assert window.run_panel.guard_button.text() == GUARD_OFF
