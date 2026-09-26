"""What every test in this suite needs whether or not it asks for it.

Three things so far: the run pointer, the instrument lock and the kept-files root go
somewhere of this session's own.
"""

from __future__ import annotations

import os

import pytest
from mainspring.interface import LIVE_POINTER_ENV, LIVE_POINTER_NAME

from clockwork import keep
from clockwork.owner.lock import LOCK_ENV, LOCK_NAME


@pytest.fixture(autouse=True)
def isolated_run_pointer(tmp_path_factory, monkeypatch):
    """Point `mainspring.interface` at a pointer file of this test's own.

    Any test that acquires publishes the file being written as the run in progress
    (lab record, task 58), and the default place to publish it is per user rather than
    per run: on an instrument PC it is the pointer a real mainspring is reading. A suite
    that wrote there would move an operator's window onto a stand-in's invented spectrum
    mid-run and then withdraw the pointer the real acquisition had published, and a test
    that *read* there would answer differently depending on whether anyone happened to
    be at the rig.

    Autouse rather than asked for, because the tests that have to be isolated are not
    only the ones that are about the pointer: every acquisition through
    `run_acquisition` writes one. Per test rather than per session so that one test's
    leftover pointer is never another's starting state.
    """
    monkeypatch.setenv(
        LIVE_POINTER_ENV,
        os.path.join(str(tmp_path_factory.mktemp("live-pointer")), LIVE_POINTER_NAME),
    )


@pytest.fixture(autouse=True)
def isolated_instrument_lock(tmp_path_factory, monkeypatch):
    """Point the instrument lock at a file of this test's own (lab record, task 67).

    The same reasoning as the run pointer's, with a sharper failure: every owner that is
    not `--fake` takes the per-user lock as it is built, so a suite run on an instrument
    PC with the window open would have every such test refused by the trainee's window
    -- and a suite that took the real lock while a trainee launched the window would
    refuse the trainee.
    """
    monkeypatch.setenv(
        LOCK_ENV, os.path.join(str(tmp_path_factory.mktemp("instrument-lock")), LOCK_NAME))


@pytest.fixture(autouse=True)
def isolated_kept_root(tmp_path_factory, monkeypatch):
    """Point `clockwork.keep` at a root of this test's own (lab record, task 81).

    Every owner that is not `--fake` copies a failed run's files to the configured root,
    and the default is per user: a suite run on an instrument PC would fill the lab's
    kept folders with stand-in failures that read as real ones.
    """
    monkeypatch.setenv(keep.ENV, str(tmp_path_factory.mktemp("kept")))


@pytest.fixture(autouse=True)
def no_real_daemon(monkeypatch):
    """Refuse to start `clockwork serve` from any test (lab record, task 77).

    A window that is not `--fake` is a client of the daemon and starts one when none
    answers. Started from a test, that daemon is a real one: on an instrument PC it would
    scan the trainees' boxes and start the real console, and it outlives the test. It
    happened once, during the first full run after the window became a client: a test's
    window started a daemon that came up after the test had let go of its lock, found
    the rack and started the console. A test that means to start a daemon hands the
    window a launch of its own.
    """
    def refused(command: list[str]) -> None:
        raise OSError("a test tried to start a real clockwork serve; hand the window a "
                      "launch of its own")

    monkeypatch.setattr("clockwork.app.serving.start_serve", refused)
    monkeypatch.setattr("clockwork.app.window.start_serve", refused, raising=False)
