"""Where `--self-check`'s report goes with no console to attach to (task 60).

`clockwork.app` imports no Qt at module scope, so this needs no `pytestqt` and runs on
a machine with no display -- exactly the case `_open_report_file` exists for: a frozen
build launched with no parent console (double-clicked, or a parent that is not one),
where `_attach_parent_console` (Windows-only, proven against the real build by
`tools/build_exe.ps1`'s launch check) has already returned False.
"""

from __future__ import annotations

import sys
from pathlib import Path

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
