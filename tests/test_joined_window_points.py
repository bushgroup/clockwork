"""A window that joins an agent's series mid-point follows the job's own points (lab #30).

An agent's series submitted through the `series` tool over a `RemoteOwner` to a `--fake`
serve, with thirty repetitions a file so the join replays hundreds of entries a point and
the stream runs, and a window opened mid-point. The window's followed points are the job's
in the order acquired, every caption it shows on the way names a point as the record's
`order` does, and its run log gives that order (lab record, task 174). Two sittings read a
wrong "(planned N)" in the first caption after a join, copied by hand; this route never
showed one, and a saved window's start-up (a restored queue or plan) is not covered.
"""

from __future__ import annotations

import re
import time

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from clockwork.app.planpanel import FollowedSeries  # noqa: E402
from clockwork.app.worker import Discover  # noqa: E402
from clockwork.method import template as template_module  # noqa: E402
from clockwork.owner import SeriesJob  # noqa: E402
from test_app_remote import (  # noqa: E402,F401 -- fixtures, imported
    claude, daemon, idle_daemon, log_lines, remote_window, scratch_settings)
from test_mcp import TEMPLATE  # noqa: E402
from test_plan_runner import agent  # noqa: E402

LONG = TEMPLATE.replace("accumulations = 2", "accumulations = 30")
"""The MCP tests' one-box template with thirty repetitions a file, so a join meets a
history of hundreds of entries a point."""

PLAN = """\
plan_schema = 1
description = "line B across seventeen lengths, a reference either side"
template = "line-b.toml"
labels = { sample = "test mixture" }
replicates = 1
references = ["start", "end"]

[grid]
b_ticks = [100, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300, 320, 340, 360, 380, 400, 420]
"""


def test_a_window_joining_an_agents_series_mid_point_follows_the_jobs_points(
        qtbot, scratch_settings, daemon, claude, tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    (library / "line-b.toml").write_text(LONG, encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    tools = agent(claude, str(library), output)
    method = template_module.render(template_module.loads_template(LONG), {},
                                    {"sample": "test mixture"}).method
    claude.submit(Discover(method=method))
    answer = tools.call("series", {"request": "line B across seventeen lengths, please",
                                   "initials": "BF", "plan": PLAN})
    order = answer["order"]
    assert len(order) == 19
    handle = next(h for h in [claude.status().running, *claude.status().queued]
                  if h is not None and h.id == answer["job"])

    def at(number: int) -> bool:
        summary = claude.summary(handle)
        assert summary.ended is None, summary.ended
        return summary.point is not None and summary.point.number >= number
    qtbot.waitUntil(lambda: at(3), timeout=240_000)

    began = time.monotonic()
    made = remote_window(qtbot, daemon.endpoint)
    seen: list[tuple[float, str]] = []

    def settled() -> bool:
        text = made.follow.caption.text()
        if text and (not seen or seen[-1][1] != text):
            seen.append((time.monotonic() - began, text))
        followed = made.followed
        return (followed is not None and followed.current is not None
                and followed.current + 1 == claude.summary(handle).point.number)
    qtbot.waitUntil(settled, timeout=60_000)
    for when, text in seen:
        print(f"{when:6.2f} s  {text}")
    followed: FollowedSeries = made.followed
    job = made._job
    assert isinstance(job, SeriesJob)
    assert [point.index for point in followed.points] == order
    assert [point.index for point in job.points] == order
    # Every caption the join showed, the replay's included, names a point as the record
    # orders it.
    named = [re.search(r"point (\d+) of 19 \(planned (\d+)\)", text) for _, text in seen]
    named = [(int(found[1]), int(found[2])) for found in named if found]
    assert named and all(order[number - 1] == index for number, index in named), named
    # The order the window rebuilt, in its run log, for a sitting to set beside the record.
    line = (f"following {job.label}: 19 points in the order acquired, as planned "
            f"indices: {', '.join(map(str, order))}")
    assert any(line in text for text in log_lines(made)), log_lines(made)[-20:]
    tools.call("stop", {})
    qtbot.waitUntil(lambda: claude.summary(handle).ended is not None, timeout=120_000)
    qtbot.waitUntil(lambda: made._job is None, timeout=60_000)
    idle_daemon(qtbot, claude)
