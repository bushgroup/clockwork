"""The window opens a series plan, shows its check and its custody, starts it as one series
job, and follows it in a table (lab record, task 166).

A `--fake` window, its owner in process (A6); the owner-level parity with the `series`
tool is `test_plan_runner.py`'s.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("pytestqt")

from clockwork import planning  # noqa: E402
from clockwork.app.planpanel import DONE  # noqa: E402
from clockwork.app.runlog import _JOB  # noqa: E402
from clockwork.mcp import AuditLog, Toolbox  # noqa: E402
from clockwork.mcp.server import SIMULATED  # noqa: E402
from clockwork.method import template as template_module  # noqa: E402
from test_app import idle, load_into, scratch_settings, until, window  # noqa: E402, F401
from test_plan_runner import PLAN  # noqa: E402
from test_series import LABELS, REQUEST, TWO_KNOBS, record_of  # noqa: E402


def test_a_file_counts_against_its_own_point_whatever_order_the_events_arrive_in():
    """A file comes as a signal and a point's start through the mailbox: the next point's
    start drawn first must not take the last point's file (the full suite's catch)."""
    from clockwork.app.planpanel import RUNNING, SKIPPED, STOPPED, FollowedPoint, FollowedSeries

    series = FollowedSeries(label="a series", points=[
        FollowedPoint(index=2, knobs={"b_ticks": 300}, replicates=2, minutes=1.0),
        FollowedPoint(index=1, knobs={}, replicates=1, minutes=0.5),
        FollowedPoint(index=3, knobs={"b_ticks": 450}, replicates=1, minutes=0.5)])
    series.point_started(1)
    series.run_done()
    assert series.minutes_left == 1.5, "half the first point and the two after it"
    series.point_started(2)
    series.run_done()  # the first point's second file, arriving after the next start
    series.run_done()
    assert [point.files for point in series.points] == [2, 1, 0]
    assert series.points[1].state == RUNNING
    series.point_started(3)
    series.finished(points_done=2, stopped="stopped by the operator")
    assert [point.state for point in series.points] == [DONE, DONE, STOPPED]
    series = FollowedSeries(label="b", points=[
        FollowedPoint(index=1, knobs={}), FollowedPoint(index=2, knobs={})])
    series.point_started(1)
    series.failed()
    assert [point.state for point in series.points] == ["failed", SKIPPED]


def test_a_plan_opened_at_the_window_shows_its_check_runs_and_is_followed(
        window, qtbot, tmp_path):  # noqa: F811
    library = tmp_path / "library"
    library.mkdir()
    (library / "two-knob.toml").write_text(TWO_KNOBS, encoding="utf-8")
    window.settings.library_dir = str(library)
    # The stand-in rack is built from the method open, so open the template's defaults.
    rendered = template_module.render(template_module.loads_template(TWO_KNOBS), {}, LABELS)
    load_into(window, tmp_path, rendered.method)
    until(qtbot, lambda: window.worker.boxes and idle(window))
    window.start_console()
    until(qtbot, lambda: window.worker.console_alive and idle(window))

    plan = tmp_path / "plan.toml"
    plan.write_text(PLAN, encoding="utf-8")
    # An agent's check of the same text, through the same owner, into the day folder.
    checker = Toolbox(window.worker.owner, library=str(library), output=str(tmp_path),
                      instrument=SIMULATED, log=AuditLog.beside(str(tmp_path), via="mcp"))
    checker.call("series", {"request": REQUEST, "initials": "zz", "plan": str(plan),
                            "check": True})

    window.run_panel.purpose.setText("")
    checked = window.open_plan(str(plan))
    assert checked is not None and checked.sha256 == planning.sha256(PLAN)
    panel = window.plan_panel
    assert panel.tree.topLevelItemCount() == 4
    assert panel.custody.text().startswith("checked through mcp at ")
    assert panel.custody.text().endswith(", unchanged since")
    assert window.run_panel.purpose.text() == "line B held for three lengths", "A1"
    assert panel.start_button.isEnabled()

    window.run_panel.at_window.setText("")
    assert window.start_plan() is None, "Start needs the person at the window"
    window.run_panel.at_window.setText("MB")
    job = window.start_plan()
    assert job is not None and job.source == "window" and job.plan == PLAN
    until(qtbot, lambda: window.followed is not None and window.followed.ended
          and idle(window), timeout=240_000)

    followed = window.followed
    assert [point.state for point in followed.points] == [DONE] * 4
    assert [point.files for point in followed.points] == [1] * 4
    assert followed.request_id and followed.minutes_left == 0.0
    assert followed.started_by == "this window, MB at the window"
    assert window.follow.tree.topLevelItemCount() == 4
    assert not window.follow.isHidden(), "shown once a series is followed"

    [name] = [name for name in os.listdir(tmp_path) if name.endswith(".request.json")]
    record = record_of(str(tmp_path / name))
    assert record["request"]["id"] == followed.request_id
    assert record["request"]["at_window"] == "MB"
    [entry] = record["plans"]
    assert entry["sha256"] == checked.sha256 and entry["checked"]["via"] == "mcp"
    assert len(record["files"]) == 4

    # D5: the run log narrowed to the series' own lines, and opened out again.
    log = window.run_panel.log
    window.follow.only.setChecked(True)
    shown = [log.topLevelItem(index) for index in range(log.topLevelItemCount())
             if not log.topLevelItem(index).isHidden()]
    assert shown and all(item.data(0, _JOB) == followed.tag for item in shown)
    assert len(shown) < log.topLevelItemCount()
    window.follow.only.setChecked(False)
    assert not any(log.topLevelItem(index).isHidden()
                   for index in range(log.topLevelItemCount()))
