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
no record in its directory (a bench script), is left alone; an entry that cannot be
written is dropped, never raised, since the record is the run's and never a reason to
disturb it. Entries replace by stem, so a client that also enters a file (a `progress`
follower of an older release) enters it once.

**A window run's record is begun here too** (`begin`, lab record, task 165): the window
is a client that begins no record of its own, so a job that names a `source` and no
request id has the owner mint the id, stamp it as the files' series id, and open the
record before the job's first file exists. The decision for the owner rather than the
window is A3 of the window revision: the daemon writes, so the record is whole however
the window ends, and a `--fake` window's in-process owner writes the same document
(A6). Its request block is the person's Start: the purpose line as the request's words,
the file initials, and who was at the window; its acquisition entry adds the method's
hash, and for a rendered method the rendered method's hash and every knob outside the
limits the sitting carries (R10). An `mcp` or `cli` record is begun as before, by its
client, and is byte for byte what it was.

**A series plan started at the window** (lab record, task 166) carries the plan's text,
and its record's `plans` and `acquisitions` entries are the `series` tool's for the same
plan and seed (`clockwork.planning`), checked again here under the sitting's limits, with
the plan's hash and the check the audit log holds of it.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from .. import envelope, summary
from .. import method as method_module
from ..acq.loop import Run
from ..acq.uimf import RAW_SUFFIX, SUMMED_SUFFIX
from ..envelope import Limits
from ..method.template import loads_template, render
from ..naming import next_stem
from ..record import RunRecord, brief, find, mint_id
from .jobs import Acquire, SeriesJob

__all__ = ["RecordKeeper", "begin", "file_entry", "stem_of"]

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
    entry: dict[str, Any] = {
        "stem": stem_of(run), "raw": run.raw_path, "summed": run.summed_path,
        "complete": run.complete, "stopped_early": run.stopped_early,
        "replicate": run.replicate, "seconds": round(run.seconds, 2),
        "summary": numbers}
    if run.holds:
        # Only for a file that was held, so every other file's entry is what it was.
        entry["holds"] = [
            {"after": {"method_frame": hold.method_frame, "repetition": hold.repetition},
             "began": hold.began, "seconds": round(hold.seconds, 1),
             "ended_by": hold.ended_by} for hold in run.holds]
    return entry


def begin(job: Acquire | SeriesJob, job_id: int, *,
          limits: Limits | None) -> tuple[Acquire | SeriesJob, str]:
    """Open the run record of a job that names a `source` and no request id, and answer
    the job as the owner runs it: its request id minted and stamped as `series`, and an
    `Acquire`'s first stem fixed, so the record is named for the file that follows it;
    and the record's path.

    `limits` are the sitting's (`LocalOwner.limits`): a rendered run records, under
    `outside_limits`, each knob an agent's refusal would name, and null where the sitting
    carries no limits at all. `OSError` where the record cannot be written; the caller
    goes on without it."""
    directory = job.directory or os.getcwd()
    identity = mint_id(job.request)
    if isinstance(job, SeriesJob):
        record = RunRecord.open(directory, next_stem(directory, job.initials),
                                request_id=identity, text=job.request, source=job.source,
                                initials=job.initials, at_window=job.at_window)
        if job.plan:
            _plan_entries(record, job, job_id, directory, limits)
        else:
            record.append("acquisitions", {
                "job": job_id, "series": True, "first_place": max(1, job.first_position),
                "points": len(job.points),
                "replicates": sum(point.replicates for point in job.points)})
        return replace(job, series=identity), record.path
    stem = job.stem or next_stem(directory, job.initials)
    record = RunRecord.open(directory, stem, request_id=identity, text=job.request,
                            source=job.source, initials=job.initials,
                            at_window=job.at_window)
    record.append("acquisitions", _acquisition_entry(job, job_id, limits))
    return replace(job, series=identity, series_index=1, stem=stem), record.path


def _plan_entries(record: RunRecord, job: SeriesJob, job_id: int, directory: str,
                  limits: Limits | None) -> None:
    """A window series' `plans` and `acquisitions` entries, as the `series` tool writes
    them for the same plan and seed: the plan checked again from its text and the
    template's (`clockwork.planning.check`), under the sitting's limits and the job's
    pusher period, with the hash and the audit log's matching `check` (the window
    revision's A5). A plan that does not check again leaves a note saying why, and the
    run goes on: the job is the client's, already judged."""
    from .. import planning
    from ..mcp.audit import AuditLog  # the MCP tools import the owner
    from ..series import loads

    first = max(1, job.first_position)
    try:
        judged = planning.check(
            loads(job.plan), loads_template(job.template), template_path=job.method_path,
            path=job.plan_path, limits=limits,
            pusher_period_us=job.instrument.pusher_period_us)
        planned = sorted(point.index - (first - 1) for point in job.points)
        if planned != [checked.point.index for checked in judged.points]:
            raise ValueError(f"its {len(judged.points)} points are not the job's "
                             f"{len(job.points)}")
        found = AuditLog.beside(directory).check_for(judged.sha256)
    except Exception as exc:  # noqa: BLE001 -- the record says why, never raises
        from .local import sentence
        record.append("notes", {"text": f"the series' plan did not check again, so the "
                                        f"record holds its text alone: {sentence(exc)}",
                                "by": "server"})
        record.append("plans", {"text": "", "job": job_id, "plan": job.plan,
                                "path": job.plan_path, "sha256": planning.sha256(job.plan)})
        record.append("acquisitions", {
            "job": job_id, "series": True, "first_place": first,
            "points": len(job.points),
            "replicates": sum(point.replicates for point in job.points)})
        return
    entry = judged.plans_entry(job=job_id, seed=job.seed, first_place=first, checked=found)
    # The order the job carries, which is the one acquired, over the one the seed draws
    # here: the two agree for every job `Checked.job` built.
    entry["order"] = [point.index for point in job.points]
    record.append("plans", entry)
    record.append("acquisitions", judged.acquisitions_entry(job=job_id, first_place=first))


def _acquisition_entry(job: Acquire, job_id: int, limits: Limits | None) -> dict[str, Any]:
    """A window `Acquire` as its record's `acquisitions` keeps it."""
    method = job.method
    entry: dict[str, Any] = {
        "job": job_id, "replicates": max(1, job.replicates), "first_place": 1,
        "method": method.metadata.name if method is not None else "",
        "method_hash": _hash(method), "template": None}
    if not job.template:
        return entry
    template: dict[str, Any] = {"path": job.method_path, "hash": "",
                                "knobs": dict(job.knobs), "labels": dict(job.labels),
                                "method_hash": ""}
    try:
        rendered = render(loads_template(job.template), dict(job.knobs), dict(job.labels))
    except Exception as exc:  # noqa: BLE001 -- the run refuses it; the record says why
        from .local import sentence
        template["problem"] = sentence(exc)
        entry["template"] = template
        return entry
    template.update(hash=rendered.template_hash, knobs=dict(rendered.knobs),
                    labels=dict(rendered.labels), method_hash=_hash(rendered.method))
    entry["template"] = template
    entry["outside_limits"] = (envelope.outside_limits(rendered, limits)
                               if limits is not None else None)
    return entry


def _hash(method: object) -> str:
    """The method's SHA-256 as its files stamp it, or empty."""
    if method is None:
        return ""
    try:
        return str(method_module.stamp(method)["method_hash"])  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 -- a hash the record cannot take is left empty
        return ""


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

    def intervention(self, directory: str, request_id: str,
                     entry: dict[str, Any]) -> None:
        """Add a person's act on a running job, or any client's stop of one, to the
        `interventions` of the record of `request_id`, if there is one (the window
        revision's A3 and A4; lab record, task 167)."""
        if request_id:
            self._put(lambda: self._with(
                directory, request_id,
                lambda record: record.append("interventions", entry)))

    def person_note(self, directory: str, request_id: str, text: str, by: str,
                    client: str) -> None:
        """Add a note a person typed at the window, under their initials."""
        if request_id and text:
            self._put(lambda: self._with(
                directory, request_id,
                lambda record: record.append(
                    "notes", {"text": text, "by": by, "client": client})))

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
