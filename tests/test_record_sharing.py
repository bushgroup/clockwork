"""A run record written on one thread while another reads it.

On Windows `os.replace` onto a record is refused while a reader holds it open, and a
reader's `open` is refused while the rename is under way. The MCP server's recording
thread adds a run's file entry a moment after `progress` says the job is done, so the
session's next `verdict` and `manifest` read the record just then; a refusal dropped the
entry (the thread swallows it) or left the manifest's row with no record and no verdict.
The first two tests force each refusal on any platform; the third holds the record open
for real, which refuses the rename only on Windows.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from clockwork import manifest, record
from clockwork.acq.uimf import Series
from clockwork.record import RunRecord, find, find_for_stem
from test_manifest import by_stem, write

STEM = "261114_SC_001"


@pytest.fixture
def kept(tmp_path) -> RunRecord:
    return RunRecord.open(str(tmp_path), STEM, request_id="req-1", text="one run", source="mcp")


def refused(times: int, real):
    """`real`, refused with a sharing violation the first `times` calls."""
    calls = {"n": 0}

    def stand_in(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= times:
            raise PermissionError(13, "The process cannot access the file because it is "
                                      "being used by another process")
        return real(*args, **kwargs)
    return stand_in, calls


def test_a_rename_refused_for_a_moment_still_lands(kept, monkeypatch):
    stand_in, calls = refused(3, os.replace)
    monkeypatch.setattr(record.os, "replace", stand_in)
    kept.add_file({"stem": STEM, "file": STEM + ".summed.uimf"})
    assert calls["n"] == 4
    assert [entry["stem"] for entry in kept.read()["files"]] == [STEM]
    assert not os.path.exists(kept.path + ".tmp")


def test_a_read_refused_for_a_moment_still_finds_the_record(kept, tmp_path, monkeypatch):
    import builtins
    write(tmp_path / f"{STEM}.summed.uimf", series=Series("req-1", index=1, position=1))
    for reader in (lambda: find(str(tmp_path), "req-1") == kept.path,
                   lambda: find_for_stem(str(tmp_path), STEM) == kept.path,
                   lambda: kept.read()["request"]["id"] == "req-1",
                   lambda: by_stem(manifest.manifest([tmp_path]))[STEM]["record"]
                   == os.path.basename(kept.path)):
        stand_in, calls = refused(2, builtins.open)
        monkeypatch.setattr(record, "open", stand_in, raising=False)
        assert reader()
        assert calls["n"] >= 3


def test_a_refusal_that_outlasts_the_wait_is_raised(kept, monkeypatch):
    monkeypatch.setattr(record, "SHARING_WAIT_S", 0.05)
    stand_in, _ = refused(10**6, os.replace)
    monkeypatch.setattr(record.os, "replace", stand_in)
    with pytest.raises(PermissionError):
        kept.add_file({"stem": STEM})


def test_a_reader_holding_the_record_does_not_cost_the_writer_its_entry(kept):
    # The real collision: on Windows the rename is refused for as long as this handle
    # is open. Before the retry, `add_file` raised at once.
    errors: list[BaseException] = []

    def add() -> None:
        try:
            kept.add_file({"stem": STEM, "file": "x"})
        except BaseException as exc:  # noqa: BLE001 -- the test reports it
            errors.append(exc)

    with open(kept.path, encoding="utf-8"):
        writer = threading.Thread(target=add)
        writer.start()
        time.sleep(0.2)
    writer.join(5)
    assert errors == []
    assert [entry["stem"] for entry in kept.read()["files"]] == [STEM]


@pytest.mark.skipif(sys.platform != "win32", reason="the sharing refusal is Windows'")
def test_the_manifest_finds_the_record_while_it_is_being_rewritten(tmp_path):
    # The check's interleaving, forced to recur: the recording thread rewrites the
    # record while the session reads the manifest. Before the fix, rows came back with
    # no record a few times in a hundred.
    write(tmp_path / f"{STEM}.summed.uimf", series=Series("req-1", index=1, position=1))
    kept = RunRecord.open(str(tmp_path), STEM, request_id="req-1", text="one run", source="mcp")
    kept.add_verdict(STEM, "worked", "sc")
    stop = threading.Event()

    def rewrite() -> None:
        while not stop.is_set():
            kept.add_file({"stem": STEM, "file": STEM + ".summed.uimf"})

    writer = threading.Thread(target=rewrite)
    writer.start()
    try:
        missed = 0
        for _ in range(60):
            row = by_stem(manifest.manifest([tmp_path]))[STEM]
            missed += row["record"] is None or row["verdict"] != "worked"
    finally:
        stop.set()
        writer.join(10)
    assert missed == 0
