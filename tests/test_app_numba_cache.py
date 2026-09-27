"""`clockwork.app`'s frozen-build numba cache: seeded once per executable, warmed by the .exe.

The seed is only ever read because mainspring 1.10.0's frozen locator puts a frozen
program's cache in one folder under `NUMBA_CACHE_DIR`; what is tested here is clockwork's
half, the copying and the warm mode the build runs (mainspring's lab record, task 34).
"""

from __future__ import annotations

from clockwork import app


def frozen_as(monkeypatch, tmp_path, exe_bytes=b"exe"):
    exe = tmp_path / "bundle" / "clockwork.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(exe_bytes)
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app.sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    monkeypatch.setattr(app.sys, "executable", str(exe))
    seed = tmp_path / "bundle" / "numba_cache_seed" / "clockwork_uimf"
    seed.mkdir(parents=True)
    (seed / "warm.nbi").write_text("from the build")
    return exe


def test_nothing_is_seeded_outside_a_frozen_build(tmp_path, monkeypatch):
    monkeypatch.delattr(app.sys, "frozen", raising=False)
    cache = tmp_path / "cache"
    app._seed_numba_cache(str(cache))
    assert not cache.exists()


def test_an_upgrade_is_seeded_over_a_folder_the_last_version_filled(tmp_path, monkeypatch):
    frozen_as(monkeypatch, tmp_path)
    cache = tmp_path / "cache"
    (cache / "clockwork_uimf").mkdir(parents=True)
    (cache / "clockwork_uimf" / "older.nbi").write_text("from a real run")

    app._seed_numba_cache(str(cache))

    assert (cache / "clockwork_uimf" / "warm.nbi").read_text() == "from the build"
    assert (cache / "clockwork_uimf" / "older.nbi").read_text() == "from a real run"


def test_the_seed_is_copied_once_per_executable(tmp_path, monkeypatch):
    exe = frozen_as(monkeypatch, tmp_path)
    cache = tmp_path / "cache"
    app._seed_numba_cache(str(cache))
    (cache / "clockwork_uimf" / "warm.nbi").write_text("numba wrote this since")

    app._seed_numba_cache(str(cache))
    assert (cache / "clockwork_uimf" / "warm.nbi").read_text() == "numba wrote this since"

    exe.write_bytes(b"a different, larger executable")
    app._seed_numba_cache(str(cache))
    assert (cache / "clockwork_uimf" / "warm.nbi").read_text() == "from the build"


def test_the_warm_mode_compiles_and_exits_before_the_parser(tmp_path, monkeypatch):
    """Not an option argparse knows, so it must be answered before argparse sees it."""
    monkeypatch.setenv("NUMBA_CACHE_DIR", "")
    assert app.main([app.WARM_OPTION, str(tmp_path / "seed")]) == 0
