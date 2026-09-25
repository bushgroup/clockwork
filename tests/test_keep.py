"""Kept files: the folder, its manifest, the report id, and what `prune` may remove."""

from __future__ import annotations

import datetime as _dt
import hashlib
import os

from clockwork import keep

STEM = "260925_ZZ_040"


def run_dir(tmp_path, stem: str = STEM):
    """A run's four files, a neighbour's, and one the trainee wrote themselves."""
    directory = tmp_path / "data"
    directory.mkdir()
    for name in (f"{stem}.uimf", f"{stem}.summed.uimf", f"{stem}.sent.txt",
                 f"{stem}-2026-09-25.transcript.log", f"{stem}0.uimf",
                 "260925_ZZ_041.uimf", "Sample ID.txt"):
        (directory / name).write_bytes(name.encode() * 50)
    return directory


def sha256(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_the_root_is_the_environment_then_the_setting_then_the_default(monkeypatch,
                                                                         tmp_path):
    monkeypatch.setenv(keep.ENV, str(tmp_path / "env"))
    assert keep.root("E:/somewhere") == str(tmp_path / "env")
    monkeypatch.delenv(keep.ENV)
    assert keep.root("E:/somewhere") == "E:/somewhere"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert keep.root("") == os.path.join(str(tmp_path), "clockwork", "reports")


def test_a_runs_files_are_its_stem_followed_by_a_dot_or_a_hyphen(tmp_path):
    directory = run_dir(tmp_path)
    names = [os.path.basename(path) for path in keep.run_files(str(directory), STEM)]
    assert names == [f"{STEM}-2026-09-25.transcript.log", f"{STEM}.sent.txt",
                     f"{STEM}.summed.uimf", f"{STEM}.uimf"]
    assert keep.run_files(str(tmp_path / "gone"), STEM) == []
    assert keep.stem_of(f"D:/data/{STEM}-2026-09-25.transcript.log") == STEM
    assert keep.stem_of("D:/data/notes.txt") == ""


def test_the_copies_match_their_manifest_and_outlive_the_originals(tmp_path):
    directory = run_dir(tmp_path)
    root = tmp_path / "kept"
    when = _dt.datetime(2026, 9, 25, 15, 41, 2)
    paths = [*keep.run_files(str(directory), STEM), str(tmp_path / "no-such.toml")]
    folder = keep.keep(str(root), paths, reason="auklet stopped answering on its port.",
                       stems=[STEM], pc="MASS TRO_1", when=when)
    assert os.path.basename(folder) == "R-20260925-154102-MASSTRO1"
    assert keep.is_report_id(os.path.basename(folder))
    for path in keep.run_files(str(directory), STEM):
        os.remove(path)
    manifest = keep.read_manifest(folder)
    assert manifest.fields["reason"] == "auklet stopped answering on its port."
    assert manifest.fields["pc"] == "MASS TRO_1" and manifest.stems() == [STEM]
    assert manifest.time() == when
    copied = [row for row in manifest.rows if row.status == "copied"]
    assert len(copied) == 4
    for row in copied:
        kept = os.path.join(folder, row.kept)
        assert sha256(kept) == row.sha256 and os.path.getsize(kept) == int(row.size)
    [missing] = [row for row in manifest.rows if row.status == "missing"]
    assert missing.original.endswith("no-such.toml")
    with open(os.path.join(folder, keep.MANIFEST), "rb") as stream:
        assert b"\r\n" not in stream.read()


def test_a_second_folder_in_the_same_second_gets_a_suffix(tmp_path):
    when = _dt.datetime(2026, 9, 25, 15, 41, 2)
    first = keep.keep(str(tmp_path), [], reason="x", pc="PC", when=when)
    second = keep.keep(str(tmp_path), [], reason="x", pc="PC", when=when)
    assert os.path.basename(second) == os.path.basename(first) + "-2"
    assert keep.is_report_id(os.path.basename(second))


def test_a_report_after_a_failure_reuses_its_folder_and_adds_a_fresh_error_log(tmp_path):
    directory = run_dir(tmp_path)
    errors = tmp_path / "errors.log"
    errors.write_text("the failure's traceback\n", encoding="utf-8")
    root = str(tmp_path / "kept")
    failed = keep.keep(root, [*keep.run_files(str(directory), STEM), str(errors)],
                       reason="failed", stems=[STEM])
    errors.write_text("the failure's traceback\nand what came after\n", encoding="utf-8")
    assert keep.kept_for(root, STEM) == failed
    assert keep.kept_for(root, "260925_ZZ_041") is None
    reported = keep.for_report(root, directory=str(directory), stem=STEM,
                               errors_log=str(errors))
    assert reported == failed
    manifest = keep.read_manifest(failed)
    assert "reported" in manifest.fields
    assert {row.kept for row in manifest.rows if row.original == str(errors)} == {
        "errors.log", "errors-2.log"}
    assert open(os.path.join(failed, "errors-2.log")).read().endswith("came after\n")


def test_a_report_with_nothing_kept_before_keeps_the_run_afresh(tmp_path):
    directory = run_dir(tmp_path)
    folder = keep.for_report(str(tmp_path / "kept"), directory=str(directory), stem=STEM)
    manifest = keep.read_manifest(folder)
    assert manifest.fields["reason"] == "reported"
    assert len([row for row in manifest.rows if row.status == "copied"]) == 4


def test_prune_removes_only_old_unclaimed_folders(tmp_path):
    root = str(tmp_path)
    now = _dt.datetime(2026, 12, 31, 12, 0, 0)
    old = keep.keep(root, [], reason="x", pc="PC", when=now - _dt.timedelta(days=91))
    young = keep.keep(root, [], reason="x", pc="PC", when=now - _dt.timedelta(days=89))
    claimed = keep.keep(root, [], reason="x", pc="PC2", when=now - _dt.timedelta(days=200))
    renamed = os.path.join(root, "lab-0001_" + os.path.basename(claimed))
    os.rename(claimed, renamed)
    stray = os.path.join(root, "R-20200101-000000-PC")
    os.makedirs(stray)  # no manifest: not ours to judge
    assert keep.prune(root, now=now) == [old]
    assert sorted(os.listdir(root)) == sorted(
        os.path.basename(path) for path in (young, renamed, stray))
    assert keep.prune(str(tmp_path / "absent"), now=now) == []
