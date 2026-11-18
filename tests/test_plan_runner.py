"""A series plan run from the window: the path from a plan's text to the owner's job is one
function the `series` tool and the window both call, every `series` audit line carries the
plan's hash, and a series a person starts at the window leaves the record, the stamps and the
order an agent's leaves, tied to the agent's check by that hash (lab record, task 166).

Driven over a `--fake` owner in process, as `test_series.py` drives the tool; the window's
job is built here as the window builds it (`clockwork.planning`), and the window itself is
driven in `test_app_plan.py`.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from clockwork import planning
from clockwork import series as series_module
from clockwork.mcp import AuditLog, Toolbox, ToolFailure
from clockwork.mcp.server import SIMULATED
from clockwork.method import template as template_module
from clockwork.owner import JobFinished
from test_daemon import ended
from test_mcp import audit_lines, fake_owner, followed  # noqa: F401 -- a fixture, imported
from test_series import (  # noqa: F401 -- a fixture, imported
    LABELS,
    REQUEST,
    library,
    limits,
    plan_text,
    record_of,
    summed_stamps,
)

PLAN = plan_text(references='["start"]', middle="[grid]\nb_ticks = [300, 400, 450]",
                 extra='description = "line B held for three lengths"\nseed = 4242')
"""Four points of one file: the start reference and three holds, shuffled under a seed."""

SHORT = plan_text(references="[]", middle="[[points]]\nb_ticks = 300\n[[points]]\nb_ticks = 450",
                  extra='description = "two holds"\nshuffle = false')


def agent(owner, folder: str, output) -> Toolbox:
    """A toolbox writing its audit log as the MCP server's does, `via` mcp."""
    return Toolbox(owner, library=folder, output=str(output), instrument=SIMULATED,
                   log=AuditLog.beside(str(output), via="mcp"))


def window_check(path: str, folder: str, limits=None) -> planning.Checked:
    """A plan file checked as the window checks it (`MainWindow._check_plan`)."""
    text, path = series_module.text_of(path)
    planned = series_module.loads(text)
    template_path = planning.locate(planned.template, [folder])
    return planning.check(planned, template_module.load_template(template_path),
                          template_path=template_path, path=path, limits=limits,
                          real=False, pusher_period_us=SIMULATED.pusher_period_us)


def window_job(checked: planning.Checked, directory) -> object:
    """The job the window's Start submits (`MainWindow.start_plan`)."""
    planned = checked.plan
    return checked.job(
        seed=planned.seed, label="acquiring the series plan plan.toml",
        directory=str(directory), initials="zz", request=planned.description,
        instrument=SIMULATED, cold_start="caution", source="window", at_window="MB",
        with_plan=True)


def one_record(directory) -> dict:
    [name] = [name for name in os.listdir(directory) if name.endswith(".request.json")]
    return record_of(os.path.join(directory, name))


def stamps(directory) -> list[tuple]:
    return [(entry["ClockworkSeriesIndex"], entry["ClockworkSeriesPosition"],
             entry.get("ClockworkSeriesSeed"), entry["ClockworkKnobBTicks"])
            for entry in summed_stamps(str(directory))]


# -- the hash on every line --------------------------------------------------------------


def test_every_series_line_carries_the_hash_of_the_plan_however_it_was_passed(
        fake_owner, library, tmp_path):  # noqa: F811
    """A5: inline, as a file with Windows line endings, refused, and unreadable; the
    other tools' lines are as they were."""
    output = tmp_path / "runs"
    toolbox = agent(fake_owner, library, output)
    saved = tmp_path / "plan.toml"
    saved.write_bytes(PLAN.replace("\n", "\r\n").encode("utf-8"))
    refused = PLAN.replace("[300, 400, 450]", "[300, 600]")
    inline = toolbox.call("series", {"request": REQUEST, "initials": "zz", "plan": PLAN,
                                     "check": True})
    from_file = toolbox.call("series", {"request": REQUEST, "initials": "zz",
                                        "plan": str(saved), "check": True})
    assert inline == from_file, "the same plan, however passed, checks the same"
    with pytest.raises(ToolFailure, match="refused whole"):
        toolbox.call("series", {"request": REQUEST, "initials": "zz", "plan": refused})
    with pytest.raises(ToolFailure, match="neither a plan's text nor a file"):
        toolbox.call("series", {"request": REQUEST, "initials": "zz",
                                "plan": str(tmp_path / "missing.toml"), "check": True})
    toolbox.call("status", {})

    lines = audit_lines(str(output))
    series_lines = [line for line in lines if line["tool"] == "series"]
    sha = planning.sha256(PLAN)
    assert [line["plan_sha256"] for line in series_lines] == [
        sha, sha, planning.sha256(refused), None]
    assert series_lines[0]["arguments"]["plan"]["sha256"] == sha, "inline text hashes alike"
    assert series_lines[1]["arguments"]["plan"] == str(saved), "a path is logged as given"
    assert all("plan_sha256" not in line for line in lines if line["tool"] != "series")

    found = toolbox.log.check_for(sha)
    assert found == {"time": series_lines[1]["time"], "via": "mcp",
                     "session": series_lines[1]["session"]}
    assert toolbox.log.check_for(planning.sha256(refused)) is None, "a refusal is no check"


def test_custody_says_the_check_or_its_absence():
    today = dt.date(2026, 11, 18)
    found = {"time": "2026-11-18T14:02:11", "via": "mcp", "session": "s"}
    assert planning.custody(found, today=today) == (
        "checked through mcp at 14:02, unchanged since")
    assert planning.custody({**found, "via": "cli"}, today=dt.date(2026, 11, 19)) == (
        "checked through cli at 2026-11-18 14:02, unchanged since")
    assert planning.custody(None) == "no check of this text on record"


# -- one function, two callers -----------------------------------------------------------


def test_the_limits_refuse_an_agent_and_are_shown_to_a_person(library, tmp_path):  # noqa: F811
    """R10: a point outside the limits is the tool's refusal and the window's mark; a
    point outside the template's range refuses both."""
    path = tmp_path / "plan.toml"
    path.write_text(SHORT, encoding="utf-8")
    held = limits(library, high=420)
    checked = window_check(str(path), library, held)
    assert [entry["index"] for entry in checked.outside()] == [2]
    assert checked.blocked() == []
    [refusal] = checked.agent_refusals()
    assert refusal.startswith("point 2 (b_ticks = 450): b_ticks = 450")
    assert window_check(str(path), library).outside() is None, "no limits, nothing shown"

    path.write_text(SHORT.replace("450", "600"), encoding="utf-8")
    beyond = window_check(str(path), library, held)
    assert len(beyond.blocked()) == 1 and beyond.blocked()[0].startswith("point 2")


def test_a_plan_started_at_the_window_leaves_what_an_mcp_series_leaves(
        fake_owner, library, tmp_path):  # noqa: F811
    """The record, the stamps and the order under one seed, and the window's record tied
    to the agent's check by the plan's hash; `progress` follows the window's series from
    a toolbox that did not start it (D6)."""
    by_agent, at_window = tmp_path / "agent", tmp_path / "window"
    started_by_agent = agent(fake_owner, library, by_agent)
    started_by_agent.call("discover_boxes", {"template": "two-knob.toml", "labels": LABELS})
    # The agent's check, in the day folder the window's series goes to.
    checker = agent(fake_owner, library, at_window)
    checker.call("series", {"request": REQUEST, "initials": "zz", "plan": PLAN,
                            "check": True})
    started = started_by_agent.call("series", {"request": REQUEST, "initials": "zz",
                                               "plan": PLAN})
    assert followed(started_by_agent, started["job"], timeout=240)["done"]

    path = tmp_path / "plan.toml"
    path.write_text(PLAN, encoding="utf-8")
    job = window_job(window_check(str(path), library), at_window)
    planned = record_of(started["record"])["plans"][0]["points"]
    assert {point.index: point.minutes for point in job.points} == {
        entry["index"]: entry["minutes"] for entry in planned}, "the points carry minutes"
    handle = fake_owner.submit(job)
    assert isinstance(ended(fake_owner, handle, timeout=240), JobFinished)

    progress = checker.call("progress", {"job": handle.id, "wait_s": 0})
    assert progress["done"] and len(progress["runs"]) == 4
    assert progress["series"]["order"] == started["order"]
    assert progress["series"]["seed"] == 4242

    assert stamps(at_window) == stamps(by_agent)
    agents, windows = record_of(started["record"]), one_record(at_window)
    assert windows["request"]["source"] == "window"
    assert windows["request"]["at_window"] == "MB"
    assert windows["request"]["text"] == "line B held for three lengths"
    [agent_plan], [window_plan] = agents["plans"], windows["plans"]
    assert list(window_plan) == list(agent_plan)
    differ = ("time", "job", "path", "checked")
    assert ({key: value for key, value in window_plan.items() if key not in differ}
            == {key: value for key, value in agent_plan.items() if key not in differ})
    assert window_plan["sha256"] == planning.sha256(PLAN)
    assert window_plan["path"] == str(path) and agent_plan["path"] == ""
    assert window_plan["checked"]["via"] == "mcp"
    assert agent_plan["checked"] is None, "nothing was checked in the agent's own folder"
    assert window_plan["outside_limits"] is None, "the fake owner carries no limits"
    [agent_acquisition], [window_acquisition] = agents["acquisitions"], windows["acquisitions"]
    assert ({key: value for key, value in window_acquisition.items() if key not in differ}
            == {key: value for key, value in agent_acquisition.items()
                if key not in (*differ, "cold_start")})
    assert len(windows["files"]) == len(agents["files"]) == 4


def test_a_plan_edited_after_its_check_shows_unchecked_and_still_runs(
        fake_owner, library, tmp_path):  # noqa: F811
    """A5: the hash is of the text, so one line added after the check is a plan nobody
    checked; it runs, and the record says so. Its point outside the sitting's limits
    runs too, and the record lists it (R10)."""
    checker = agent(fake_owner, library, tmp_path)
    checker.call("discover_boxes", {"template": "two-knob.toml", "labels": LABELS})
    checker.call("series", {"request": REQUEST, "initials": "zz", "plan": SHORT,
                            "check": True})
    path = tmp_path / "plan.toml"
    path.write_text(SHORT + "# the second hold lengthened after the check\n",
                    encoding="utf-8")
    held = limits(os.fspath(library), high=420)
    checked = window_check(str(path), library, held)
    assert planning.custody(checker.log.check_for(checked.sha256)) == (
        "no check of this text on record")
    assert planning.custody(checker.log.check_for(planning.sha256(SHORT))).startswith(
        "checked through mcp at ")

    fake_owner.limits = held
    try:
        handle = fake_owner.submit(window_job(checked, tmp_path))
        assert isinstance(ended(fake_owner, handle, timeout=240), JobFinished)
    finally:
        fake_owner.limits = None
    [plan] = one_record(tmp_path)["plans"]
    assert plan["checked"] is None and plan["sha256"] == checked.sha256
    [outside] = plan["outside_limits"]
    assert outside["index"] == 2 and "b_ticks = 450" in outside["problems"][0]
    assert [entry[3] for entry in stamps(tmp_path)] == [300, 450]
