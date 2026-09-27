"""Where `--self-check`'s report goes with no console to attach to (task 60).

`clockwork.app` imports no Qt at module scope, so this needs no `pytestqt` and runs on
a machine with no display -- exactly the case `_open_report_file` exists for: a frozen
build launched with no parent console (double-clicked, or a parent that is not one),
where `_attach_parent_console` (Windows-only, proven against the real build by
`tools/build_exe.ps1`'s launch check) has already returned False.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from clockwork.app import _GuardedStream, _open_report_file, _report_log_path


def test_a_guarded_stream_over_none_swallows_every_write():
    stream = _GuardedStream(None)
    stream.write("nobody is listening")
    stream.flush()  # neither raises


def test_a_guarded_stream_swallows_a_write_that_still_fails():
    class Locked:
        def write(self, text):
            raise OSError("the file is locked")

        def flush(self):
            raise OSError("the file is locked")

    stream = _GuardedStream(Locked())
    stream.write("this would otherwise crash the self-check")
    stream.flush()


def test_the_report_log_sits_beside_the_numba_cache_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert _report_log_path() == str(tmp_path / "clockwork" / "self-check.log")


def test_opening_the_report_file_points_both_streams_at_it(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "stdout", sys.stdout)  # registers it for teardown
    monkeypatch.setattr(sys, "stderr", sys.stderr)

    path = _open_report_file()
    assert path == str(tmp_path / "clockwork" / "self-check.log")
    print("self-check OK", file=sys.stdout)
    sys.stdout.flush()
    assert "self-check OK" in (tmp_path / "clockwork" / "self-check.log").read_text()


def test_a_report_file_that_cannot_be_created_still_leaves_the_streams_silent(
        monkeypatch):
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    # A path under a file (not a directory) can never become one: `os.makedirs` raises
    # `NotADirectoryError`, a plain `OSError` subclass, exactly like a locked or
    # read-only profile would (`_open_report_file`'s own `except OSError`).
    monkeypatch.setattr(
        "clockwork.app._report_log_path",
        lambda: str(Path(sys.executable) / "clockwork" / "self-check.log"),
    )

    path = _open_report_file()
    assert path is None
    sys.stdout.write("nothing raises even with nowhere to go")
    sys.stdout.flush()


# The report file a caller names (task 84): `tools/build_exe.ps1` reads it back, since
# whether a windowed exe attaches to a console is not predictable from how it started.

ROWS = ("numba cache", "imports", "box: table sent and armed", "method",
        "simulated console: connect", "simulated console: start chain",
        "recording created",
        *(f"repetition {n}: {what}" for n in (1, 2)
          for what in ("frame opened", "acquired (60 s limit)", "frame written")),
        "fold (numba decode)",
        "recording closed", "simulated console: stop", "summed file read back")


def _run_main(monkeypatch, *argv):
    from clockwork.app import _numba_cache_dir, main

    monkeypatch.setattr(sys, "stdout", sys.stdout)  # main replaces both; restored after
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    # The value main would set itself, so the test leaves the environment as it was.
    monkeypatch.setenv("NUMBA_CACHE_DIR", _numba_cache_dir())
    monkeypatch.setattr("clockwork.app._attach_parent_console", lambda: False)
    return main(list(argv))


def test_a_named_report_file_carries_every_row_and_the_verdict(monkeypatch, tmp_path):
    report = tmp_path / "nested" / "self-check.log"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))

    code = _run_main(monkeypatch, "--self-check", "--self-check-report", str(report))

    text = report.read_bytes().decode("utf-8")
    assert code == 0
    assert "\r\n" not in text
    for row in ROWS:
        assert re.search(rf"^  {re.escape(row)} +ok +\d+\.\d\d s$", text, re.M), row
    assert text.rstrip().splitlines()[-1].startswith("self-check OK:")
    assert "FAILED" not in text
    # The named file replaces the per-user fallback rather than adding to it.
    assert not (tmp_path / "appdata" / "clockwork" / "self-check.log").exists()


def test_a_failing_stage_is_named_with_its_traceback_and_exit_1(monkeypatch, tmp_path):
    report = tmp_path / "self-check.log"

    def refuse(*args, **kwargs):
        raise TimeoutError("no stream within 10 s")

    monkeypatch.setattr("clockwork.acq.start_chain", refuse)
    code = _run_main(monkeypatch, "--self-check", "--self-check-report", str(report))

    text = report.read_text(encoding="utf-8")
    assert code == 1
    assert "  simulated console: connect" in text
    assert "  simulated console: start chain" in text and "FAILED after" in text
    assert "self-check FAILED at 'simulated console: start chain'" in text
    assert "TimeoutError('no stream within 10 s')" in text
    assert "Traceback (most recent call last)" in text
    assert "repetition 1" not in text and "self-check OK" not in text


def test_a_report_path_without_the_self_check_is_refused(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as raised:
        _run_main(monkeypatch, "--self-check-report", str(tmp_path / "x.log"))
    assert raised.value.code == 2
