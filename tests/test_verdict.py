"""The `verdict` tool: a person's judgement of one run, into the run record beside its files.

The files are test_manifest's synthetic ones, written with mainspring's writer and
clockwork's stamp; the records are written through `clockwork.record`, so every verdict the
manifest and `summarize_file` read back is one the test gave.
"""

from __future__ import annotations

import json
import os

import pytest

from clockwork import manifest
from clockwork.acq.uimf import Series
from clockwork.mcp import Toolbox, ToolFailure
from clockwork.mcp.tools import UNREQUESTED
from clockwork.record import SECTIONS, VERDICTS, RunRecord, find_for_stem, newest_verdict
from test_manifest import by_stem, write


@pytest.fixture
def day(tmp_path):
    """One run of a request with its record, one of a request whose record is elsewhere,
    and one the window took, which no record holds."""
    folder = tmp_path / "260927"
    folder.mkdir()
    write(folder / "260927_AB_001.uimf", series=Series("req-1", index=1, position=1))
    write(folder / "260927_AB_001.summed.uimf", series=Series("req-1", index=1, position=1))
    write(folder / "260927_AB_002.uimf", series=Series("req-2", index=1, position=1))
    write(folder / "260927_CD_003.uimf", rendered=False)
    record = RunRecord.open(str(folder), "260927_AB_001", request_id="req-1",
                            text="pulse at 3 ms")
    record.add_file({"stem": "260927_AB_001"})
    return folder


def toolbox(day) -> Toolbox:
    return Toolbox(object(), output=str(day))


def test_the_vocabulary_has_a_sentence_a_word_and_verdicts_are_a_section():
    assert list(VERDICTS) == ["worked", "no_signal", "saturated", "wrong_sample", "other"]
    assert all(sentence for sentence in VERDICTS.values())
    assert "verdicts" in SECTIONS


def test_a_verdict_on_a_run_its_record_holds_is_written_there(day):
    given = toolbox(day).call("verdict", {"path": "260927_AB_001.summed.uimf",
                                          "verdict": "worked", "initials": "ab"})
    assert given["record"] == str(day / "260927_AB_001.request.json")
    assert not given["begun"] and given["stem"] == "260927_AB_001"
    assert given["verdicts_on_stem"] == 1
    entry = given["verdict"]
    assert (entry["verdict"], entry["by"], entry["words"]) == ("worked", "AB", "")
    written = json.loads((day / "260927_AB_001.request.json").read_text(encoding="utf-8"))
    assert [kept["stem"] for kept in written["verdicts"]] == ["260927_AB_001"]
    assert written["notes"] == [], "a verdict is not a note"


def test_a_second_verdict_supersedes_the_first_and_both_are_kept(day):
    tools = toolbox(day)
    tools.call("verdict", {"path": "260927_AB_001", "verdict": "worked", "initials": "AB"})
    later = tools.call("verdict", {"path": str(day / "260927_AB_001.uimf"),
                                   "verdict": "saturated", "initials": "MB",
                                   "words": "  base peak railed in frames 2-4 "})
    assert later["verdicts_on_stem"] == 2
    assert later["verdict"]["words"] == "base peak railed in frames 2-4"
    data = RunRecord(later["record"]).read()
    assert [entry["verdict"] for entry in data["verdicts"]] == ["worked", "saturated"]
    assert newest_verdict(data, "260927_AB_001")["by"] == "MB"

    # Read back by both: the manifest's column is the word, the summary's the entry.
    assert by_stem(manifest.manifest([str(day)]))["260927_AB_001"]["verdict"] == "saturated"
    for path in ("260927_AB_001.uimf", "260927_AB_001.summed.uimf"):
        summed = tools.call("summarize_file", {"path": path})
        assert summed["verdict"]["verdict"] == "saturated", path
    assert tools.call("summarize_file", {"path": "260927_CD_003.uimf"})["verdict"] is None


def test_a_word_outside_the_vocabulary_and_other_without_words_are_refused(day):
    tools = toolbox(day)
    asked = {"path": "260927_AB_001", "initials": "AB"}
    with pytest.raises(ToolFailure, match="'great' is not a verdict; say one of worked"):
        tools.call("verdict", {**asked, "verdict": "great"})
    with pytest.raises(ToolFailure, match="a verdict of other says what it is"):
        tools.call("verdict", {**asked, "verdict": "other", "words": "   "})
    with pytest.raises(ToolFailure, match="say whose verdict this is"):
        tools.call("verdict", {**asked, "verdict": "worked", "initials": "-"})
    with pytest.raises(ToolFailure, match="no UIMF file of 260927_AB_009"):
        tools.call("verdict", {**asked, "path": "260927_AB_009", "verdict": "worked"})
    given = tools.call("verdict", {**asked, "verdict": "other", "words": "sprayer spat"})
    assert given["verdicts_on_stem"] == 1, "the refusals wrote nothing"


def test_a_run_its_requests_record_does_not_hold_yet_is_refused(day):
    # The request's record, begun at another stem, before this run's file entry is in it.
    RunRecord.open(str(day), "260927_AB_000", request_id="req-2", text="pulse at 5 ms")
    with pytest.raises(ToolFailure, match="holds no file of 260927_AB_002; a verdict waits"):
        toolbox(day).call("verdict", {"path": "260927_AB_002", "verdict": "worked",
                                      "initials": "AB"})
    with pytest.raises(ValueError, match="holds no file"):
        RunRecord(str(day / "260927_AB_000.request.json")).add_verdict(
            "260927_AB_002", "worked", "AB")


def test_a_run_no_record_holds_gets_one_begun_for_it(day):
    tools = toolbox(day)
    given = tools.call("verdict", {"path": "260927_CD_003.uimf", "verdict": "no_signal",
                                   "initials": "CD"})
    assert given["begun"] and given["record"] == str(day / "260927_CD_003.request.json")
    data = RunRecord(given["record"]).read()
    assert data["request"]["source"] == "window" and data["request"]["text"] == UNREQUESTED
    assert data["request"]["initials"] == "CD"
    [kept] = data["files"]
    assert kept["stem"] == "260927_CD_003" and kept["summed"] is None
    assert kept["raw"] == str(day / "260927_CD_003.uimf")
    assert kept["summary"]["frames"] == 1, kept["summary"]

    again = tools.call("verdict", {"path": "260927_CD_003", "verdict": "wrong_sample",
                                   "initials": "CD", "words": "vial 4, not vial 3"})
    assert not again["begun"] and again["record"] == given["record"]
    assert again["verdicts_on_stem"] == 2
    row = by_stem(manifest.manifest([str(day)]))["260927_CD_003"]
    assert (row["verdict"], row["record"]) == ("wrong_sample", "260927_CD_003.request.json")


def test_a_stamped_run_whose_record_is_missing_gets_one_under_its_request(day):
    given = toolbox(day).call("verdict", {"path": "260927_AB_002", "verdict": "worked",
                                          "initials": "AB"})
    assert given["begun"]
    data = RunRecord(given["record"]).read()
    assert data["request"]["id"] == "req-2" and data["request"]["source"] == "agent"
    assert find_for_stem(str(day), "260927_AB_002", "req-2") == given["record"]
    assert os.path.basename(find_for_stem(str(day), "260927_AB_002")) == (
        "260927_AB_002.request.json")
