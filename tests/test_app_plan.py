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


# -- a person's pause, resume, stop and note (lab record, task 167) --------------------------


def held_plan(window, qtbot, tmp_path) -> None:  # noqa: F811
    """A window with the four-point plan open and ready to start, as the test above."""
    library = tmp_path / "library"
    library.mkdir()
    (library / "two-knob.toml").write_text(TWO_KNOBS, encoding="utf-8")
    window.settings.library_dir = str(library)
    rendered = template_module.render(template_module.loads_template(TWO_KNOBS), {}, LABELS)
    load_into(window, tmp_path, rendered.method)
    until(qtbot, lambda: window.worker.boxes and idle(window))
    window.start_console()
    until(qtbot, lambda: window.worker.console_alive and idle(window))
    plan = tmp_path / "plan.toml"
    plan.write_text(PLAN, encoding="utf-8")
    assert window.open_plan(str(plan)) is not None


def log_lines(window) -> list[str]:  # noqa: F811
    log = window.run_panel.log
    return [log.topLevelItem(index).text(1) for index in range(log.topLevelItemCount())]


def test_the_controls_are_a_series_own_and_say_why_one_is_greyed(qtbot):
    """B2: the two pauses are present only while a series runs; one that cannot be asked
    is greyed with the owner's sentence, not hidden; Resume appears with a pause."""
    from clockwork.app.runlog import NOTE_SAYS, RunPanel
    from clockwork.owner import POINT, Hold

    panel = RunPanel()
    qtbot.addWidget(panel)
    assert panel.pause_point.isHidden() and panel.pause_repetition.isHidden()
    assert panel.resume_button.isHidden() and not panel.stop_run.isEnabled()
    assert panel.note_says.text() == NOTE_SAYS
    assert "commands nothing; the buttons are the commands" in NOTE_SAYS
    assert "may be published" in panel.note.placeholderText()

    why = "this point's method is single_frame: pause after this point instead"
    panel.show_hold(None, series=True, running=True, repetition=why)
    assert not panel.pause_point.isHidden() and panel.pause_point.isEnabled()
    assert not panel.pause_repetition.isHidden() and not panel.pause_repetition.isEnabled()
    assert panel.pause_repetition.toolTip().startswith("This point's method is single_frame")
    assert panel.stop_run.isEnabled() and panel.held.isHidden()

    asked = Hold(job=3, grain=POINT, by="MB", asked="2026-11-18T14:02:00")
    panel.show_hold(asked, series=True, running=True)
    assert panel.resume_button.text() == "Withdraw the pause" and not panel.held.isHidden()
    assert "still acquiring" in panel.held.text()
    resting = Hold(job=3, grain=POINT, by="MB", asked="2026-11-18T14:02:00",
                   since="2026-11-18T14:02:11", where="after point 1 of 4 (planned 1)")
    panel.show_hold(resting, series=True, running=True)
    assert panel.pause_point.isHidden() and panel.pause_repetition.isHidden()
    assert panel.resume_button.text() == "Resume" and not panel.resume_button.isHidden()
    assert panel.held.text().startswith("Paused by MB at the window since 2026-11-18T14:02:11")
    assert "until a person at a window resumes or stops it" in panel.held.text()

    sent: list[str] = []
    panel.note_requested.connect(sent.append)
    panel.note.setText("   ")
    panel.note_button.click()
    panel.note.setText(" the tip was changed ")
    panel.note_button.click()
    assert sent == ["the tip was changed"]


def test_a_person_pauses_notes_and_resumes_a_plan_from_the_run_panel(
        window, qtbot, tmp_path):  # noqa: F811
    from clockwork.app.planpanel import HELD

    held_plan(window, qtbot, tmp_path)
    panel = window.run_panel
    assert panel.pause_point.isHidden(), "no series, no pause"

    # A note with nothing running goes to the day record, which a --fake window's own
    # owner does not keep: it says so rather than losing the note quietly.
    panel.at_window.setText("MB")
    window.keep_note("warming up")
    until(qtbot, lambda: any("was not kept" in line for line in log_lines(window)))

    assert window.start_plan() is not None
    until(qtbot, lambda: window.followed is not None and window.followed.current == 0,
          timeout=120_000)
    until(qtbot, lambda: not panel.pause_point.isHidden() and panel.pause_point.isEnabled())
    assert panel.pause_repetition.isEnabled(), "a per_repetition point has an inner pause"

    panel.at_window.setText("")
    panel.pause_point.click()
    assert window.worker.hold is None, "a pause needs the person at the window"
    assert any("a pause is recorded with who made it" in line for line in log_lines(window))
    panel.at_window.setText("mb")
    panel.pause_point.click()
    until(qtbot, lambda: window.worker.hold is not None and window.worker.hold.reached,
          timeout=120_000)
    hold = window.worker.hold
    assert hold.by == "MB" and hold.where == "after point 1 of 4 (planned 1)"
    until(qtbot, lambda: not panel.held.isHidden() and panel.pause_point.isHidden())
    assert not panel.resume_button.isHidden() and panel.stop_run.isEnabled()
    assert panel.caption.text() == hold.text
    until(qtbot, lambda: window.followed.shown(1) == HELD)
    followed = window.followed
    assert [followed.shown(number) for number in range(4)] == [
        DONE, HELD, "waiting", "waiting"]
    assert followed.caption.endswith(hold.text)
    assert window.follow.tree.topLevelItem(1).text(2) == HELD
    assert hold.text in window.statusBar().currentMessage()
    assert not window.acquire_button.isEnabled(), "a paused series holds the instrument"

    panel.note.setText("flow dropped; changing the tip")
    panel.note_button.click()
    assert panel.note.text() == ""
    until(qtbot, lambda: any("note from MB at the window" in line
                             for line in log_lines(window)))

    panel.at_window.setText("KT")
    panel.resume_button.click()
    until(qtbot, lambda: window.followed.ended and idle(window), timeout=240_000)
    assert [point.state for point in window.followed.points] == [DONE] * 4
    assert window.worker.hold is None and panel.held.isHidden()
    assert panel.pause_point.isHidden() and panel.resume_button.isHidden()

    [name] = [name for name in os.listdir(tmp_path) if name.endswith(".request.json")]
    record = record_of(str(tmp_path / name))
    assert [(entry["kind"], entry["by"]) for entry in record["interventions"]] == [
        ("pause", "MB"), ("resume", "KT")]
    assert all(entry["client"] == "the clockwork window" for entry in record["interventions"])
    assert [(note["text"], note["by"]) for note in record["notes"] if "client" in note] == [
        ("flow dropped; changing the tip", "MB")]
    lines = log_lines(window)
    assert any(line.startswith("paused by MB at the window since") for line in lines)
    assert any(line.startswith("resumed by KT at the window after") for line in lines)


def test_stop_at_the_window_is_signed_and_ends_a_paused_plan_where_it_rests(
        window, qtbot, tmp_path):  # noqa: F811
    held_plan(window, qtbot, tmp_path)
    panel = window.run_panel
    panel.at_window.setText("MB")
    assert window.start_plan() is not None
    until(qtbot, lambda: not panel.pause_repetition.isHidden()
          and panel.pause_repetition.isEnabled(), timeout=120_000)
    panel.pause_repetition.click()
    until(qtbot, lambda: window.worker.hold is not None and window.worker.hold.reached,
          timeout=120_000)
    panel.at_window.setText("")
    panel.stop_run.click()
    assert window.worker.hold is not None, "a Stop button needs the person at the window"
    panel.at_window.setText("KT")
    panel.stop_run.click()
    until(qtbot, lambda: window.followed.ended and idle(window), timeout=120_000)
    assert window.followed.ended == "stopped" and window.worker.hold is None
    [name] = [name for name in os.listdir(tmp_path) if name.endswith(".request.json")]
    record = record_of(str(tmp_path / name))
    asked, stopped = record["interventions"]
    assert (asked["kind"], asked["grain"], asked["by"]) == ("pause", "repetition", "MB")
    assert (stopped["kind"], stopped["by"], stopped["while_paused"]) == ("stop", "KT", True)
    assert "stopped at the window (while paused by MB at the window since" in stopped["reason"]
