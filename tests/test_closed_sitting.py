"""A closed sitting's folder, and the two verbs that start a serve on a saved one.

The fixture is a real sitting's day record, cut down: the rc17 candidate's folder, stood
down at 13:33:44 and sealed, then four serves started on it by windows opened only to
read Setup, one of them the same evening, none of which sent anything (lab record, task
175). Qt-free.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path

import pytest

from clockwork.app import serving
from clockwork.dayrecord import STAND_DOWN, DayRecord, closed_sitting
from clockwork.owner.remote import DaemonError

SERVE = "clockwork serve"


def _line(time: str, kind: str, **fields: object) -> str:
    return json.dumps({"time": time, "kind": kind, **fields}, separators=(",", ":")) + "\n"


def _serve(started: str, console: str, stopped: str, session: str, reason: str) -> list[str]:
    return [
        _line(started, "daemon", event="started", session=session, by=SERVE, reason=""),
        _line(console, "console", event="started", job=2, by=SERVE, detail="ready in 5.5 s"),
        _line(stopped, "console", event="stopped", job=None, by=SERVE,
              detail=f"the owner closed: {reason}"),
        _line(stopped, "daemon", event="stopped", session=session, by=SERVE, reason=reason),
    ]


def _stamp(path: Path, when: str) -> None:
    moment = _dt.datetime.fromisoformat(when).timestamp()
    os.utime(path, (moment, moment))


def rc17(folder: Path, *, idle_serves: bool = True) -> Path:
    """The rc17 sitting's folder at triage: its runs' and the stand-down's send logs, the
    sealed segment the stand-down ended, and the live one four idle serves wrote."""
    folder.mkdir(parents=True, exist_ok=True)
    sitting = _line("2026-11-19T11:16:18", "sitting", version="1.2.0rc17",
                    session="80bedcf9361d", output=str(folder))
    sealed = folder / "day-record.20261119-133345.jsonl"
    sealed.write_text(sitting + "".join(_serve(
        "2026-11-19T11:16:18", "2026-11-19T11:16:24", "2026-11-19T13:33:44",
        "80bedcf9361d", STAND_DOWN)), encoding="utf-8")
    for name, when in (("warm-up-20261119-111948.sent.txt", "2026-11-19T11:20:30"),
                       ("261119_BF_030.sent.txt", "2026-11-19T13:29:48"),
                       ("stand-down-20261119-133326.sent.txt", "2026-11-19T13:33:43")):
        (folder / name).write_text("", encoding="utf-8")
        _stamp(folder / name, when)
    if idle_serves:
        closed = "the window that started it closed"
        lines = [_line("2026-11-19T20:08:39", "sitting", version="1.2.0rc17",
                       session="f1786da5ba1b", output=str(folder))]
        for started, console, stopped, session in (
                ("2026-11-19T20:08:39", "2026-11-19T20:08:51", "2026-11-19T20:09:13",
                 "f1786da5ba1b"),
                ("2026-11-20T11:38:44", "2026-11-20T11:38:57", "2026-11-20T11:49:06",
                 "da23f0b92bd9"),
                ("2026-11-20T14:17:08", "2026-11-20T14:17:14", "2026-11-20T14:18:05",
                 "4646ce388ef9"),
                ("2026-11-20T15:42:11", "2026-11-20T15:42:18", "2026-11-20T15:42:37",
                 "654c21c02cc1")):
            lines += _serve(started, console, stopped, session, closed)
        (folder / "day-record.jsonl").write_text("".join(lines), encoding="utf-8")
    return folder


# -- what makes a folder closed (D3) --------------------------------------------------


def test_the_rc17_folder_is_closed_through_every_idle_serve_after_it(tmp_path):
    folder = rc17(tmp_path / "261119-rc17-sitting")
    why = closed_sitting(str(folder))
    assert why == ("it was stood down at 2026-11-19 13:33:44, and no send has been "
                   "logged in it since")
    # The stand-down alone, before any window opened on it, reads the same.
    assert closed_sitting(str(rc17(tmp_path / "bare", idle_serves=False))) == why


def test_a_send_after_the_stand_down_reopens_the_folder(tmp_path):
    folder = rc17(tmp_path / "rc17")
    later = folder / "warm-up-20261120-081500.sent.txt"
    later.write_text("", encoding="utf-8")
    _stamp(later, "2026-11-20T08:15:00")
    assert closed_sitting(str(folder)) == ""


def test_a_send_in_the_stand_downs_own_second_does_not_reopen_it(tmp_path):
    folder = rc17(tmp_path / "rc17")
    _stamp(folder / "stand-down-20261119-133326.sent.txt", "2026-11-19T13:33:44")
    assert closed_sitting(str(folder))


def test_a_seal_without_a_stand_down_and_a_day_without_one_are_open(tmp_path):
    """A mid-day `seal_calls` seals the day record as a stand-down does; it closes
    nothing. Nor does a day that ended with the window closed and no stand-down."""
    folder = tmp_path / "sealed-mid-day"
    folder.mkdir()
    record = DayRecord(str(folder), sitting=lambda: {"version": "test"})
    record.write("daemon", event="started", session="a", by=SERVE, reason="")
    record.write("daemon", event="stopped", session="a", by=SERVE,
                 reason="the window that started it closed")
    assert record.seal() is not None
    assert closed_sitting(str(folder)) == ""


def test_no_folder_an_empty_one_and_one_with_no_day_record_are_open(tmp_path):
    assert closed_sitting("") == ""
    assert closed_sitting(str(tmp_path / "nowhere")) == ""
    assert closed_sitting(str(tmp_path)) == ""
    (tmp_path / "261013_BF_001.sent.txt").write_text("", encoding="utf-8")
    assert closed_sitting(str(tmp_path)) == ""


# -- the two verbs that start a serve on the saved folder (D4) -------------------------


class _Verb:
    def __init__(self, tool: str) -> None:
        self.tool = type("Tool", (), {"name": tool})()


def test_warm_up_refuses_a_closed_saved_folder_and_snapshot_says_so(tmp_path, capsys):
    import sys

    from clockwork.mcp.cli import _closed_folder

    folder = str(rc17(tmp_path / "rc17"))
    with pytest.raises(DaemonError) as refused:
        _closed_folder(_Verb("warm_up"), folder, given=False, err=sys.stderr,
                       program="clockwork warm-up")
    said = str(refused.value)
    assert folder in said and "13:33:44" in said and "--output" in said
    _closed_folder(_Verb("snapshot_boxes"), folder, given=False, err=sys.stderr,
                   program="clockwork snapshot-boxes")
    err = capsys.readouterr().err
    assert folder in err and "starts there all the same" in err
    # Named with --output, the closed folder is the person's choice: no word, no refusal.
    _closed_folder(_Verb("warm_up"), folder, given=True, err=sys.stderr,
                   program="clockwork warm-up")
    assert capsys.readouterr().err == ""


def test_a_refused_start_launches_nothing(tmp_path):
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    launched: list[list[str]] = []

    def refuse() -> None:
        raise DaemonError("a closed sitting's folder")

    with pytest.raises(DaemonError, match="closed sitting"):
        serving.connect_or_start(f"tcp://127.0.0.1:{port}", ["serve"],
                                 launch=launched.append, before=refuse)
    assert launched == []
