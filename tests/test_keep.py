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


# --- task 82: a row's several stems, and the two manifest gaps lab #2's folder showed ---


def test_a_rows_report_keeps_every_stem_it_names_and_not_the_next_rows(tmp_path):
    directory = run_dir(tmp_path)
    for name in ("260925_ZZ_041.summed.uimf", "260925_ZZ_041-2026-09-25.transcript.log",
                 "260925_ZZ_054.uimf"):
        (directory / name).write_bytes(b"x" * 10)
    folder = keep.for_report(str(tmp_path / "kept"), directory=str(directory),
                             stems=[STEM, "260925_ZZ_041"],
                             method_path=str(directory / "Sample ID.txt"))
    manifest = keep.read_manifest(folder)
    assert manifest.stems() == [STEM, "260925_ZZ_041"]
    kept = {row.kept for row in manifest.rows if row.status == "copied"}
    assert {f"{STEM}.uimf", "260925_ZZ_041.uimf", "260925_ZZ_041.summed.uimf",
            "Sample ID.txt"} <= kept
    assert not any(name.startswith("260925_ZZ_054") for name in kept)


def test_a_rows_report_after_one_replicate_failed_adds_the_others_to_that_folder(
        tmp_path):
    directory = run_dir(tmp_path)
    root = str(tmp_path / "kept")
    failed = keep.keep(root, keep.run_files(str(directory), "260925_ZZ_041"),
                       reason="it failed", stems=["260925_ZZ_041"])
    folder = keep.for_report(root, directory=str(directory),
                             stems=[STEM, "260925_ZZ_041"])
    assert folder == failed
    manifest = keep.read_manifest(folder)
    assert manifest.stems() == ["260925_ZZ_041", STEM]
    assert f"{STEM}.uimf" in {row.kept for row in manifest.rows}


def test_a_second_report_press_adds_no_second_missing_row(tmp_path):
    root = str(tmp_path / "kept")
    errors_log = str(tmp_path / "errors.log")
    folder = keep.keep(root, [errors_log], reason="failed", stems=[STEM])
    keep.add(folder, [errors_log], note="reported")
    keep.add(folder, [errors_log], note="reported again")
    rows = keep.read_manifest(folder).rows
    assert [row.status for row in rows if row.original == errors_log] == ["missing"]
    # Once it exists it is copied, beside the row that said it was missing.
    open(errors_log, "w", encoding="utf-8").write("a traceback\n")
    keep.add(folder, [errors_log], note="reported a third time")
    rows = keep.read_manifest(folder).rows
    assert [row.status for row in rows if row.original == errors_log] == [
        "missing", "copied"]


def test_a_file_dropped_into_a_kept_folder_is_hashed_as_added_by_hand(tmp_path):
    """Lab #2's folder: the trainee copied the right run's files in by hand and left a
    note, and nothing hashed them. The next `add` records each one as it is then, and
    leaves the rows of what `keep` copied exactly as they were."""
    directory = run_dir(tmp_path)
    folder = keep.keep(str(tmp_path / "kept"), keep.run_files(str(directory), STEM),
                       reason="reported", stems=[STEM])
    before = keep.read_manifest(folder).rows
    assert keep.unrecorded(folder) == []
    note = os.path.join(folder, "ReadMe-forMatt.txt")
    open(note, "w", encoding="utf-8").write("the wrong run was kept\n")
    # A recorded copy changed afterwards stays detectable against its kept hash.
    with open(os.path.join(folder, f"{STEM}.sent.txt"), "ab") as stream:
        stream.write(b"edited")
    assert keep.unrecorded(folder) == ["ReadMe-forMatt.txt"]

    keep.add(folder, [], note="reported")
    manifest = keep.read_manifest(folder)
    assert manifest.rows[:len(before)] == before
    [hand] = [row for row in manifest.rows if row.status == keep.HAND_ADDED]
    assert (hand.kept, hand.original, hand.sha256) == ("ReadMe-forMatt.txt", "-",
                                                       sha256(note))
    assert hand.size == str(os.path.getsize(note))
    assert keep.unrecorded(folder) == []
    keep.add(folder, [], note="reported again")
    assert len([row for row in keep.read_manifest(folder).rows
                if row.status == keep.HAND_ADDED]) == 1
