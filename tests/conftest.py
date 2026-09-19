"""What every test in this suite needs whether or not it asks for it.

One thing so far: the run pointer goes somewhere of this session's own.
"""

from __future__ import annotations

import os

import pytest
from mainspring.interface import LIVE_POINTER_ENV, LIVE_POINTER_NAME


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
