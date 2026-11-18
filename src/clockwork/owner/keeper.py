"""The owner's hand in the run record: each file a request's job writes, entered by the
process that wrote it.

A request's record (`clockwork.record`) is opened by whoever issued the request, an MCP
server or a command line, as it submits the job. Its files used to be entered by a
thread in that same process, following the owner's events; the process could end before
the job did (a session restarted, a command line's `--no-wait`), and every file written
after it ended was in no record. The owner is the one party that sees every `RunDone`,
so it enters them: `RecordKeeper` finds the record by the request id the job carries and
the files stamp, adds each file's entry, and adds a note when the job fails or a series
stops (lab record, task 164).

On a thread of its own, because an entry carries the file's summary, which reads every
frame: the owner's loop goes on to the next replicate while the last one is summarized.
`drain` is called before the job's end is reported, so a client that waits for the end
and then reads the record finds it whole. A job with no request id, or whose request has
no record in its directory (a window run, a bench script), is left alone; an entry that
cannot be written is dropped, never raised, since the record is the run's and never a
reason to disturb it. Entries replace by stem, so a client that also enters a file
(a `progress` follower of an older release) enters it once.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections.abc import Callable
from typing import Any

from .. import summary
from ..acq.loop import Run
from ..acq.uimf import RAW_SUFFIX, SUMMED_SUFFIX
from ..record import RunRecord, brief, find

__all__ = ["RecordKeeper", "file_entry", "stem_of"]

_log = logging.getLogger(__name__)


def stem_of(run: Run) -> str:
    """A run's stem, off its raw file's name, or its summed file's."""
    stem = os.path.basename(run.raw_path or run.summed_path or "")
    for suffix in (SUMMED_SUFFIX, RAW_SUFFIX):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def file_entry(run: Run) -> dict[str, Any]:
    """One finished run as its request's record keeps it, with its file's summary: the
    summed file's, or the raw file's for a run that never folded and so wrote none."""
    path = next((candidate for candidate in (run.summed_path, run.raw_path)
                 if candidate and os.path.isfile(candidate)), "")
    try:
        numbers: dict[str, Any] = brief(summary.summarize(path)) if path else {}
    except Exception as exc:  # noqa: BLE001 -- the record says why, never raises
        from .local import sentence
        numbers = {"problem": sentence(exc)}
    return {"stem": stem_of(run), "raw": run.raw_path, "summed": run.summed_path,
            "complete": run.complete, "stopped_early": run.stopped_early,
            "replicate": run.replicate, "seconds": round(run.seconds, 2),
            "summary": numbers}


class RecordKeeper:
    """Writes into run records on a thread of its own, in the order asked."""

    def __init__(self) -> None:
        self._work: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()

    def enter(self, directory: str, request_id: str, run: Run) -> None:
        """Add `run`'s file to the record of `request_id` in `directory`, if there is one."""
        if request_id:
            self._put(lambda: self._with(directory, request_id,
                                         lambda record: record.add_file(file_entry(run))))

    def note(self, directory: str, request_id: str, text: str) -> None:
        """Add a note in the owner's words to the record of `request_id`, if there is one."""
        if request_id and text:
            self._put(lambda: self._with(
                directory, request_id,
                lambda record: record.append("notes", {"text": text, "by": "server"})))

    def drain(self, timeout: float | None = None) -> bool:
        """Wait until everything asked so far is written. True if it was, within
        `timeout` seconds."""
        done = threading.Event()
        self._put(done.set)
        return done.wait(timeout)

    def close(self, timeout: float | None = None) -> None:
        """Finish what is asked, then end the thread."""
        with self._guard:
            thread, self._thread = self._thread, None
        if thread is not None:
            self._work.put(None)
            thread.join(timeout)

    def _put(self, task: Callable[[], None]) -> None:
        with self._guard:
            if self._thread is None:
                self._thread = threading.Thread(target=self._serve, daemon=True,
                                                name="clockwork run records")
                self._thread.start()
            self._work.put(task)

    def _serve(self) -> None:
        while True:
            task = self._work.get()
            if task is None:
                return
            try:
                task()
            except Exception:  # noqa: BLE001 -- the record is the run's, never its end
                _log.exception("a run record entry was not written")

    @staticmethod
    def _with(directory: str, request_id: str,
              write: Callable[[RunRecord], None]) -> None:
        found = find(directory, request_id)
        if found is not None:
            write(RunRecord(found))
