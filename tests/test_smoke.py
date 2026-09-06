"""The package imports, carries a version, and resolves lab material gracefully."""

import clockwork


def test_version_is_declared() -> None:
    assert clockwork.__version__


def test_lab_dir_never_raises_for_the_repo_root() -> None:
    # Either a lab checkout resolves or it does not; a public clone gets None.
    assert clockwork.lab_dir() is None or isinstance(clockwork.lab_dir(), str)
