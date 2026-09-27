"""`clockwork.manifest`: one row per run across directories, read off the files themselves.

Synthetic files written with mainspring's own writer and clockwork's own stamp
(`stamp_globals`), the fixture template rendered for the knob columns, and a run record
beside them written through `clockwork.record`, so every cell the manifest reports is one
the test chose.
"""

from __future__ import annotations

import csv
import io
import json
import os

import numpy as np
import pytest
from mainspring.uimf import (
    OUTCOME_STOPPED,
    OUTCOME_UNKNOWN,
    FrameSpec,
    GlobalSpec,
    SparseFrame,
    UimfWriter,
)

from clockwork import manifest
from clockwork.acq.uimf import Provenance, Series, stamp_globals
from clockwork.instrument import Instrument
from clockwork.mcp import Toolbox, ToolFailure
from clockwork.record import RunRecord
from test_acq_loop import make_method
from test_acq_uimf import rendered_fixture

BINS = 1000
PERIOD_NS = 129_000.0


def write(path, *, knobs=None, series=None, rendered=True, outcome=None, period_ns=PERIOD_NS,
          instrument=None):
    """One frame of four scans, stamped as clockwork stamps a run.

    `rendered` False stamps a hand-written method; `outcome`, `(word, planned, acquired)`,
    records how the run ended as clockwork's recording does.
    """
    if rendered:
        _loaded, render = rendered_fixture(knobs)
        extra = stamp_globals(render.method, provenance=Provenance(rendered=render,
                                                                   series=series),
                              **({"instrument": instrument} if instrument else {}))
    else:
        extra = stamp_globals(make_method(), provenance=Provenance(series=series),
                              **({"instrument": instrument} if instrument else {}))
    spec = GlobalSpec(bins=BINS, bin_width_ns=0.5, extra=extra,
                      records_outcome=outcome is not None,
                      repetitions_planned=outcome[1] if outcome else None)
    with UimfWriter(str(path), spec) as writer:
        number = writer.add_frame(FrameSpec(scans=4, average_tof_length_ns=period_ns))
        writer.write_sparse_frame(number, SparseFrame.from_scans(
            frame=number, scans=4, bins=BINS,
            points={1: (np.asarray([10], dtype=np.int32), np.asarray([5], dtype=np.int32))}))
        writer.finalise_frame(number)
        if outcome is not None:
            writer.set_outcome(outcome[0], repetitions_acquired=outcome[2])
    return str(path)


def by_stem(found: dict) -> dict[str, dict]:
    return {row["stem"]: row for row in found["rows"]}


@pytest.fixture
def day(tmp_path):
    """Two rendered runs, one of them in a series, and one hand-written, on one day."""
    folder = tmp_path / "260927"
    folder.mkdir()
    write(folder / "260927_AB_001.uimf", knobs={"pulse_ms": 3.0})
    write(folder / "260927_AB_002.uimf", knobs={"pulse_ms": 5.0},
          series=Series("req-1", index=2, position=1, seed=7))
    write(folder / "260927_CD_003.uimf", rendered=False)
    return folder


def test_two_rendered_files_and_a_hand_written_one_are_three_rows(day):
    found = manifest.manifest([str(day)])
    assert found["columns"][:len(manifest.FIXED)] == list(manifest.FIXED)
    assert found["columns"][len(manifest.FIXED):] == [
        "pulse_ms", "cycles", "wait_ms", "off_ms", "off_scan"]
    rows = by_stem(found)
    assert sorted(rows) == ["260927_AB_001", "260927_AB_002", "260927_CD_003"]

    first, second, hand = (rows[stem] for stem in sorted(rows))
    assert (first["day"], first["initials"], first["kind"]) == ("2026-09-27", "AB", "raw")
    assert first["file"] == os.path.abspath(day / "260927_AB_001.uimf")
    assert first["sample"] == "polyalanine" and first["method"]
    assert (first["pulse_ms"], first["cycles"], first["wait_ms"]) == (3.0, 1.0, 10.0)
    assert (first["off_ms"], first["off_scan"]) == (4.0, 40)
    assert second["pulse_ms"] == 5.0 and second["off_scan"] == 60
    assert first["template_hash"] == second["template_hash"] and first["template_hash"]
    assert first["method_hash"] != second["method_hash"]
    assert (second["series_id"], second["series_index"], second["series_position"],
            second["series_seed"]) == ("req-1", 2, 1, 7)
    assert first["series_id"] is None and first["series_seed"] is None

    # A hand-written run has no knobs, and says so with empty cells rather than zeros.
    assert hand["template_hash"] is None and hand["method_hash"]
    assert all(hand[name] is None for name in ("pulse_ms", "cycles", "off_scan"))
    assert hand["initials"] == "CD" and hand["problem"] is None

    # Written before the outcome record existed: unknown, as mainspring reads it.
    assert first["outcome"] == OUTCOME_UNKNOWN and first["repetitions_planned"] is None
    # The fixture's tick is 100 us against this pusher's 129 us.
    assert (first["declared_us"], first["declared_by"]) == (100.0, "template")
    assert first["measured_us"] == 129.0 and first["ratio"] == pytest.approx(1.29)
    assert hand["declared_us"] is None and hand["ratio"] is None


def test_the_csv_has_the_columns_in_order_and_empty_cells_for_what_a_file_lacks(day):
    found = manifest.manifest([str(day)])
    text = manifest.to_csv(found["columns"], found["rows"])
    assert "\r" not in text
    read = list(csv.DictReader(io.StringIO(text)))
    assert list(read[0]) == found["columns"]
    hand = next(row for row in read if row["stem"] == "260927_CD_003")
    assert hand["pulse_ms"] == "" and hand["template_hash"] == ""
    first = next(row for row in read if row["stem"] == "260927_AB_001")
    assert (first["pulse_ms"], first["off_scan"], first["cycles"]) == ("3", "40", "1")


def test_a_file_that_will_not_open_is_a_row_with_its_problem(day):
    (day / "260927_AB_004.uimf").write_bytes(b"not a database at all" * 100)
    missing = str(day / "no-such-directory")
    rows = by_stem(manifest.manifest([str(day), missing]))
    broken = rows["260927_AB_004"]
    assert broken["problem"] and broken["kind"] is None
    assert (broken["day"], broken["initials"]) == ("2026-09-27", "AB")
    gone = next(row for row in rows.values() if row["file"] == os.path.abspath(missing))
    assert "no such file or directory" in gone["problem"]
    assert len(rows) == 5


def test_a_summed_file_is_chosen_over_its_raw_and_a_raw_one_stands_alone(tmp_path):
    write(tmp_path / "260927_AB_001.uimf", outcome=(OUTCOME_STOPPED, 4, 2))
    write(tmp_path / "260927_AB_001.summed.uimf", outcome=(OUTCOME_STOPPED, 4, 2))
    write(tmp_path / "260927_AB_002.uimf")
    for asked in ([str(tmp_path)], [str(tmp_path / "260927_AB_001.uimf")]):
        rows = by_stem(manifest.manifest(asked))
        assert rows["260927_AB_001"]["kind"] == "summed", asked
        assert rows["260927_AB_001"]["file"].endswith("260927_AB_001.summed.uimf")
    rows = by_stem(manifest.manifest([str(tmp_path)]))
    assert len(rows) == 2 and rows["260927_AB_002"]["kind"] == "raw"
    stopped = rows["260927_AB_001"]
    assert (stopped["outcome"], stopped["repetitions_planned"],
            stopped["repetitions_acquired"]) == (OUTCOME_STOPPED, 4, 2)


def test_a_directory_is_walked_recursively(tmp_path, day):
    deeper = tmp_path / "later" / "260928"
    deeper.mkdir(parents=True)
    write(deeper / "260928_AB_001.uimf")
    rows = by_stem(manifest.manifest([str(tmp_path)]))
    assert "260928_AB_001" in rows and len(rows) == 4
    assert rows["260928_AB_001"]["day"] == "2026-09-28"


def test_a_run_records_notes_are_counted_and_its_newest_verdict_read(day):
    record = RunRecord.open(str(day), "260927_AB_002", request_id="req-1",
                            text="pulse at 5 ms")
    record.append("notes", {"text": "spray steady", "by": "session"})
    record.append("notes", {"text": "second look", "by": "session"})
    rows = by_stem(manifest.manifest([str(day)]))
    served = rows["260927_AB_002"]
    assert served["notes"] == 2 and served["record"] == "260927_AB_002.request.json"
    assert served["verdict"] is None, "no verdicts section yet: an empty cell"
    assert rows["260927_AB_001"]["notes"] is None, "no record, not zero notes"

    # The section a verdict tool writes, read if it is there and never required.
    data = record.read()
    data["verdicts"] = [{"stem": "260927_AB_002", "verdict": "no_signal"},
                        {"stem": "260927_AB_009", "verdict": "worked"},
                        {"stem": "260927_AB_002", "verdict": "worked"}]
    record._write(data)
    assert by_stem(manifest.manifest([str(day)]))["260927_AB_002"]["verdict"] == "worked"


def test_a_record_is_found_by_the_stems_it_holds_when_the_file_names_no_series(day):
    record = RunRecord.open(str(day), "260927_AB_001", request_id="req-9", text="one run")
    record.add_file({"stem": "260927_AB_001"})
    record.append("notes", {"text": "fine", "by": "session"})
    assert by_stem(manifest.manifest([str(day)]))["260927_AB_001"]["notes"] == 1


def test_a_hand_written_file_is_checked_against_the_instruments_period(tmp_path):
    write(tmp_path / "260927_AB_001.uimf", rendered=False,
          instrument=Instrument(pusher_period_us=129.0))
    [row] = manifest.manifest([str(tmp_path)])["rows"]
    assert (row["declared_us"], row["declared_by"], row["ratio"]) == (
        129.0, "instrument", pytest.approx(1.0))


def test_the_tool_writes_the_csv_where_it_is_told_and_answers_rows_where_not(day,
                                                                              tmp_path):
    toolbox = Toolbox(object(), output=str(day))
    asked = toolbox.call("manifest", {})
    assert len(asked["rows"]) == 3 and asked["columns"][0] == "file"
    written = toolbox.call("manifest", {"paths": [str(day)], "out": "manifest.csv"})
    assert written["out"] == os.path.join(str(day), "manifest.csv")
    assert "rows" not in written and written["files"] == 3 and written["problems"] == 0
    with open(written["out"], encoding="utf-8", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 3
    # The manifest's own output is not a .uimf, so a second walk still finds three runs.
    assert len(toolbox.call("manifest", {})["rows"]) == 3
    json.dumps(asked)
    with pytest.raises(ToolFailure, match="is not a .csv path"):
        toolbox.call("manifest", {"out": "manifest.txt"})
