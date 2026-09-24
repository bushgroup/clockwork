"""The pre-filled bug report: what goes in, what stays out, and how long it may be."""

from __future__ import annotations

import os
import time
from urllib.parse import parse_qs, urlsplit

import pytest

import clockwork
from clockwork import report
from clockwork.app import main
from clockwork.report import BUDGET, Report, outbound, transcript_beside

TABLE = "STBLDAT;0:[A:100,0:b:1:W:2:0:1:1:0:2]"
"""A table string standing in for a method's contents: nothing of it may reach a URL."""


def fields(address: str) -> dict[str, str]:
    parsed = parse_qs(urlsplit(address).query, strict_parsing=True)
    assert all(len(values) == 1 for values in parsed.values())
    return {key: values[0] for key, values in parsed.items()}


def write_transcript(path, scans: int) -> str:
    lines = ["clockwork 1.1.0 wire transcript, 2026-09-24T10:00:00-07:00",
             "method       secret-method.toml  sha256 abc",
             "conditions", "  ZZ sample, 1 uM"]
    for n in range(scans):
        lines += [
            f"10:00:{n % 60:02d}.000 mips.wire        auklet > {TABLE!r}",
            f"10:00:{n % 60:02d}.001 mips.wire        auklet >   string: {TABLE}",
            f"10:00:{n % 60:02d}.002 mips.wire        auklet < b'\\x06'",
            f"10:00:{n % 60:02d}.003 acq.wire         > acquire frame {n}",
            f"10:00:{n % 60:02d}.004 acq.wire         < ok (0.010 s)",
            f"10:00:{n % 60:02d}.005 acq.loop         FrameDone: frame {n} finished",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def made(tmp_path, *, scans: int = 3, errors: int = 0, **extra) -> Report:
    errors_log = tmp_path / "errors.log"
    if errors:
        errors_log.write_text("".join(f"  File \"x.py\", line {n}, in f\n"
                                      for n in range(errors)), encoding="utf-8")
    return Report(version="9.9.9", commit="abc1234", pc="MASSTRO", system="Windows-11",
                  method=str(tmp_path / "secret-method.toml"), errors_log=str(errors_log),
                  transcript=write_transcript(tmp_path / "x-2026-09-24.transcript.log",
                                              scans),
                  issues="https://example.org/issues/new", **extra)


def test_the_url_round_trips_and_fills_the_form_by_its_ids(tmp_path):
    address = made(tmp_path, errors=3).url()
    assert address.startswith("https://example.org/issues/new?")
    got = fields(address)
    assert set(got) == {"template", "version", "pc", "attachments"}
    assert got["template"] == "bug.yml" and got["pc"] == "MASSTRO"
    assert got["version"].startswith("9.9.9 (abc1234, ")
    text = got["attachments"]
    assert "Windows-11" in text and "secret-method.toml" in text
    assert 'line 2, in f' in text and "FrameDone: frame 2 finished" in text
    assert "ok (0.010 s)" in text


def test_no_method_contents_ever_reach_the_url(tmp_path):
    text = fields(made(tmp_path, scans=5).url())["attachments"]
    assert "STBLDAT" not in text and "acquire frame" not in text
    assert "ZZ sample" not in text and "sha256" not in text


@pytest.mark.parametrize(("line", "sent"), [
    ("10:00:00.000 mips.wire        auklet > b'GVER\\n'", True),
    ("10:00:00.000 mips.wire        auklet >   chunk 1/3 at +0.001 s", True),
    ("10:00:00.000 acq.wire         > init", True),
    ("10:00:00.000 mips.wire        auklet < b'\\x06'", False),
    ("10:00:00.000 mips.wire        auklet ! TBLCMPLT", False),
    ("10:00:00.000 acq.stream       status 'armed'", False),
    ("instrument   slimphony.toml", True),
])
def test_outbound_is_every_string_this_host_sent(line, sent):
    assert outbound(line) is sent


def test_a_long_transcript_is_trimmed_to_the_budget(tmp_path):
    whole = made(tmp_path, scans=5000, errors=500)
    address = whole.url()
    assert len(address) <= BUDGET
    text = fields(address)["attachments"]
    assert "lines of the run transcript" in text
    assert "frame 4999 finished" in text  # the newest lines are the ones kept


def test_a_transcript_that_cannot_fit_is_named_for_dragging_in(tmp_path):
    address = made(tmp_path, scans=100, errors=40).url(budget=1000)
    assert len(address) <= 1000
    text = fields(address)["attachments"]
    assert "x-2026-09-24.transcript.log" in text
    assert "too long to include here; please drag it in" in text
    assert "FrameDone" not in text


def test_absent_files_are_neither_named_nor_said_to_be_too_long(tmp_path):
    text = fields(Report(errors_log=str(tmp_path / "none.log"),
                         transcript=str(tmp_path / "none.transcript.log")).url())[
        "attachments"]
    assert "none.log" not in text and "too long" not in text


def test_the_newest_transcript_of_a_stem_is_found_whatever_its_date(tmp_path):
    old = tmp_path / "ZZ-001-2026-09-22.transcript.log"
    new = tmp_path / "ZZ-001-2026-09-23.transcript.log"
    for path in (old, new, tmp_path / "ZZ-0012-2026-09-24.transcript.log"):
        path.write_text("x\n", encoding="utf-8")
    past = time.time() - 3600
    os.utime(old, (past, past))
    assert transcript_beside(str(tmp_path), "ZZ-001") == str(new)
    assert transcript_beside(str(tmp_path), "ZZ-999") == ""
    assert transcript_beside("", "ZZ-001") == ""


def test_the_tracker_can_be_pointed_elsewhere(monkeypatch):
    monkeypatch.setenv("CLOCKWORK_ISSUES", "https://example.net/new")
    assert report.url().startswith("https://example.net/new?template=bug.yml&")


def test_clockwork_report_prints_and_version_says_which_build(capsys, monkeypatch,
                                                              tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda address: opened.append(address) or True)
    assert main(["report", "--print-only", "--method", "m.toml"]) == 0
    printed = capsys.readouterr().out.strip()
    assert fields(printed)["attachments"].count("m.toml") == 1 and opened == []
    assert main(["report"]) == 0
    assert opened == [capsys.readouterr().out.strip()]
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.startswith(f"clockwork {clockwork.__version__} (")
