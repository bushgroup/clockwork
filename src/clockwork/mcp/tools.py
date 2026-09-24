"""The tools: one registry, each a line or two over a function the package already has.

`Toolbox` holds an owner (`LocalOwner` under `--fake`, `RemoteOwner` to `clockwork
serve` otherwise), the method library, the output directory and the audit log, and each
public method marked `@tool` is one tool. `TOOLS` is the registry in the order the
methods are written -- name, group, the function, its docstring as the description, its
signature as the arguments -- and it is what `clockwork.mcp.server` registers with the
MCP SDK and what a command-line twin builds its verbs from (lab record, task 73). This
module does not import the SDK, so neither needs the other.

**Every tool answers a JSON-ready dict or raises `ToolFailure`**, whose message is one
sentence -- the loop's own, through `clockwork.owner.sentence` -- and never a traceback.
A tool that judges a method (`validate_method`, `render_template`) answers its problems
as data rather than failing, because "this method has three problems" is the answer to
the question asked. `Toolbox.call` is the one way in: it finds the tool, runs it, turns
anything raised into a sentence and writes the audit line.

**What reaches the boxes goes through the interlock.** `arm` and `acquire` call
`guard_acquisition` before they submit anything, and `acquire` also refuses a method
the boxes are not holding: arming is its own tool (Matt, 2026-09-23), so one request
arms once and acquires as often as it needs to, as the window's Replicate does, and a
failed send is reported before any acquisition starts.

**A request is the unit of work.** Every `arm` and `acquire` carries the words of the
person it is for; the first use of a set of words mints a request id, which every run
of the request stamps as its series (`ClockworkSeriesId`, with its place in the request
as `ClockworkSeriesIndex`), and the words go into each run's log header and the audit
log. Passing `request_id` back continues a request across calls and sessions.

Qt-free, under the seam with the three lower layers.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import glob
import hashlib
import inspect
import os
import threading
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .. import method as method_module
from .. import summary
from ..acq import BatchSeen, Event, Run, Snapshot, cautions, refusals
from ..acq.uimf import RAW_SUFFIX, SUMMED_SUFFIX
from ..app import methodlib
from ..instrument import UNCALIBRATED, Instrument
from ..method import Method, MethodError
from ..method import template as template_module
from ..method.template import Rendered, TemplateError
from ..mips import BoxState, Discovery
from ..naming import clean_initials, next_stem
from ..owner import (
    Acquire,
    Discover,
    Discovered,
    Handle,
    JobFailed,
    JobFinished,
    JobStarted,
    Progress,
    ReadState,
    RunDone,
    Send,
    SendResult,
    StaleHandle,
    matches_wire,
    sentence,
)
from ..owner.remote import DaemonError
from ..owner.wire import TYPE_KEY, to_wire
from ..transcript import send_log_name
from .audit import AuditLog
from .guard import guard_acquisition

__all__ = ["TOOLS", "Tool", "ToolFailure", "Toolbox", "tool"]

JOB_WAIT_S = 120.0
"""How long `discover_boxes`, `read_box_state` and `arm` wait for their job before
answering that it is still running. A send of three boxes' tables takes seconds; a job
queued behind an acquisition waits for the acquisition."""

PROGRESS_WAIT_S = 10.0
PROGRESS_WAIT_MAX_S = 30.0
"""`progress` waits up to its `wait_s` for something new, never past this (Matt,
2026-09-23): a session following a run makes one call every several seconds rather
than spinning, and still hears about a finished run within one call."""

HASH_DIGITS = methodlib.HASH_DIGITS


class ToolFailure(ValueError):
    """A tool's answer when it could not do what it was asked: one sentence."""


# -- the registry --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tool:
    """One tool: its name, its group, the `Toolbox` method behind it, what it says."""

    name: str
    group: str
    """`method`, `hardware`, `acquisition` or `data`."""
    function: Callable[..., dict]
    read_only: bool
    """True for a tool that changes nothing: no job, no file, no stop."""

    @property
    def description(self) -> str:
        return inspect.cleandoc(self.function.__doc__ or "")

    @property
    def signature(self) -> inspect.Signature:
        """The arguments, as the function declares them, without `self`."""
        full = inspect.signature(self.function, eval_str=True)
        return full.replace(parameters=list(full.parameters.values())[1:])


TOOLS: list[Tool] = []


def tool(group: str, *, read_only: bool = True) -> Callable[[Callable], Callable]:
    """Register a `Toolbox` method as a tool, in the order the methods are written."""
    def register(function: Callable) -> Callable:
        TOOLS.append(Tool(name=function.__name__, group=group, function=function,
                          read_only=read_only))
        return function
    return register


# -- the toolbox ---------------------------------------------------------------------


class Toolbox:
    """The tools, over one owner, one library and one output directory.

    Any thread may call any tool: the MCP SDK runs each call on a worker thread of its
    own, and two can overlap. What this object remembers -- the handles it issued, the
    requests it has minted and how many runs each has had, what it last armed -- is
    kept under one lock.
    """

    def __init__(
        self,
        owner: object,
        *,
        library: str = "",
        output: str = "",
        instrument: Instrument = UNCALIBRATED,
        instrument_path: str = "",
        log: AuditLog | None = None,
    ) -> None:
        self.owner = owner
        self.library = os.path.abspath(library) if library else ""
        self.output = os.path.abspath(output or os.getcwd())
        self.instrument = instrument
        self.instrument_path = instrument_path
        self.log = log if log is not None else AuditLog.beside(self.output)
        self._guard = threading.Lock()
        self._handles: dict[int, Handle] = {}
        self._words: dict[str, str] = {}
        """Request id to the words it was minted for."""
        self._ids: dict[str, str] = {}
        """The words to the id they minted, so a second call with the same words
        continues the same request."""
        self._places: dict[str, int] = {}
        """Request id to the place its next run takes, counted from 1."""
        self._armed: tuple = ()
        self._armed_stem = ""
        self._armed_name = ""
        self._armed_conditions = ""
        self._planned: dict[int, int] = {}
        """Job number to the replicates it was asked for, for `progress`'s position."""

    # -- the one way in ----------------------------------------------------------

    def call(self, name: str, arguments: Mapping[str, object] | None = None) -> dict:
        """Run the tool called `name` with `arguments`, log it, and answer its result.

        `ToolFailure` with one sentence for anything the tool raised, including an
        argument it does not take."""
        found = next((entry for entry in TOOLS if entry.name == name), None)
        if found is None:
            raise ToolFailure(f"there is no tool called {name!r}; the tools are "
                              + ", ".join(entry.name for entry in TOOLS))
        arguments = dict(arguments or {})
        started = time.monotonic()
        result: dict | None = None
        error: str | None = None
        try:
            try:
                found.signature.bind(**arguments)
            except TypeError as exc:
                raise ToolFailure(f"{name} was given the wrong arguments: {exc}") from exc
            result = found.function(self, **arguments)
            return result
        except ToolFailure as exc:
            error = str(exc)
            raise
        except DaemonError as exc:
            error = str(exc)
            raise ToolFailure(error) from exc
        except Exception as exc:  # noqa: BLE001 -- one sentence back, never a traceback
            error = sentence(exc)
            raise ToolFailure(error) from exc
        finally:
            self.log.write(tool=name, arguments=arguments, result=result, error=error,
                           seconds=time.monotonic() - started,
                           request=self._request_for_log(arguments, result))

    def _request_for_log(self, arguments: Mapping[str, object],
                         result: Mapping[str, object] | None) -> dict[str, str] | None:
        identity = str((result or {}).get("request_id") or arguments.get("request_id") or "")
        if not identity:
            return None
        with self._guard:
            words = self._words.get(identity, "")
        return {"id": identity, "text": words or str(arguments.get("request", ""))}

    # -- method ------------------------------------------------------------------

    @tool("method")
    def list_templates(self) -> dict:
        """Every method template in the library, and what a user can vary in each.

        A template is an experiment with named knobs: for each, its unit, default,
        allowed range and a description of what it does physically. Labels name the
        data (such as the sample) and render nothing; marks are the moments a run
        records, such as when the final mobility separation starts. Start here to
        turn a person's request into knob values, then `render_template`.
        """
        templates = []
        for path in self._library_files():
            relative = os.path.relpath(path, self.library).replace(os.sep, "/")
            try:
                with open(path, "rb") as handle:
                    if not template_module.is_template(tomllib.load(handle)):
                        continue
                loaded = template_module.load_template(path)
            except (OSError, tomllib.TOMLDecodeError, TemplateError) as exc:
                templates.append({"path": relative, "problem": str(exc)})
                continue
            constants = dict(loaded.constants)
            templates.append({
                "path": relative,
                "name": loaded.name,
                "description": str(loaded.body.get("metadata", {}).get("description", "")),
                "hash": loaded.hash[:HASH_DIGITS],
                "knobs": [{"name": knob.name, "unit": knob.unit, "default": knob.default,
                           "min": knob.min, "max": knob.max, "integer": knob.integer,
                           "description": knob.description} for knob in loaded.knobs],
                "labels": [{"name": label.name, "required": label.required,
                            "description": label.description} for label in loaded.labels],
                "marks": [{"name": mark.name, "description": mark.description}
                          for mark in loaded.marks],
                "tick_us": constants.get(template_module.TICK_NAME),
            })
        return {"library": self.library, "templates": templates}

    @tool("method")
    def list_methods(self) -> dict:
        """Every hand-written method in the library: name, hash, date and description.

        Templates are listed by `list_templates` instead. A document that does not load
        is listed with the problem that stopped it.
        """
        methods = []
        for entry in methodlib.scan_library(self.library):
            if self._is_template(entry.path):
                continue
            methods.append({
                "path": os.path.relpath(entry.path, self.library).replace(os.sep, "/"),
                "name": entry.name,
                "hash": entry.hash,
                "created": entry.created.isoformat() if entry.created else None,
                "description": entry.description,
                **({"problem": entry.problem} if entry.problem else {}),
            })
        return {"library": self.library, "methods": methods}

    @tool("method")
    def load_method(self, method: str) -> dict:
        """One method document: its canonical text and the same as structured fields.

        `method` is a path in the library, or an absolute path.
        """
        path = self._in_library(method)
        loaded = method_module.load(path)
        return {
            "path": path,
            "name": loaded.metadata.name,
            "hash": method_module.stamp(loaded)["method_hash"][:HASH_DIGITS],
            "text": method_module.dumps(loaded),
            "method": _plain(method_module.to_dict(loaded)),
            "warnings": list(loaded.warnings),
        }

    @tool("method")
    def diff_methods(self, a: str, b: str) -> dict:
        """Two methods compared field by field and line by line, as the window's diff.

        `a` and `b` are paths in the library. `identical` is true when nothing a box is
        sent differs; each box's phases are compared line by line.
        """
        first = method_module.load(self._in_library(a))
        second = method_module.load(self._in_library(b))
        diff = methodlib.method_diff(first, second)
        return {"identical": diff.identical, **_plain(dataclasses.asdict(diff))}

    @tool("method")
    def validate_method(self, method: str = "", template: str = "",
                        knobs: dict[str, int | float] | None = None,
                        labels: dict[str, str] | None = None) -> dict:
        """Whether a method, or a template at some knob values, can be acquired.

        Name either `method` (a path in the library) or `template` with `knobs` and
        `labels`. `problems` are why the document does not load or render; `refusals`
        are why clockwork would refuse to acquire it; `cautions` are strings it could
        not check and will not stop for. `ok` is true when the first two are empty.
        `warnings` are repairs clockwork made while loading, such as whitespace stripped
        from a string; they never stop a run.
        """
        try:
            loaded, rendered, _ = self._method(method, template, knobs, labels)
        except (MethodError, TemplateError) as exc:
            return {"ok": False, "problems": list(exc.problems), "refusals": [],
                    "cautions": [], "warnings": []}
        found = refusals(loaded)
        return {
            "ok": not found,
            "name": loaded.metadata.name,
            "hash": method_module.stamp(loaded)["method_hash"][:HASH_DIGITS],
            "rendered": rendered is not None,
            "problems": [],
            "refusals": found,
            "cautions": cautions(loaded),
            "warnings": list(loaded.warnings),
        }

    @tool("method")
    def render_template(self, template: str, knobs: dict[str, int | float] | None = None,
                        labels: dict[str, str] | None = None) -> dict:
        """A template rendered at some knob values: the method that would be sent.

        Knobs not given take their defaults. Answers every knob's value, the derived
        values, the marks (each in ms and as the expected scan), the rendered method's
        text and hash, and its refusals and cautions. A knob outside its range, an
        unknown knob or a missing required label comes back in `problems`, with `ok`
        false. `warnings` are repairs made while loading; they never stop a run.
        """
        try:
            loaded, rendered, _ = self._method("", template, knobs, labels)
        except TemplateError as exc:
            return {"ok": False, "problems": list(exc.problems)}
        assert rendered is not None
        found = refusals(loaded)
        return {
            "ok": not found,
            "problems": [],
            "template": template,
            "template_hash": rendered.template_hash[:HASH_DIGITS],
            "name": loaded.metadata.name,
            "knobs": rendered.knobs,
            "labels": rendered.labels,
            "derived": rendered.derived,
            "marks": [{"name": mark.name, "ms": mark.ms, "scan": mark.scan,
                       "description": mark.description} for mark in rendered.marks],
            "tick_us": rendered.tick_us,
            "hash": method_module.stamp(loaded)["method_hash"][:HASH_DIGITS],
            "text": method_module.dumps(loaded),
            "refusals": found,
            "cautions": cautions(loaded),
            "warnings": list(loaded.warnings),
        }

    # -- hardware ----------------------------------------------------------------

    @tool("hardware", read_only=False)
    def discover_boxes(self, method: str = "", template: str = "",
                       knobs: dict[str, int | float] | None = None,
                       labels: dict[str, str] | None = None) -> dict:
        """Find the MIPS boxes: which answered, on which port, with which firmware.

        Against the instrument the ports say what is there and a method is ignored.
        Under --fake the stand-in boxes are built from a method, so name one (or a
        template); nothing a --fake scan reports is evidence about a real box.
        """
        loaded = None
        if method or template:
            loaded, _, _ = self._method(method, template, knobs, labels)
        event, handle = self._run(Discover(method=loaded), JOB_WAIT_S)
        discovery = event.result if isinstance(event, JobFinished) else None
        if not isinstance(discovery, Discovery):
            return self._unfinished(handle, event)
        return {"text": discovery.text, **_plain(_bare(to_wire(discovery))),
                "boxes": list(self.owner.status().boxes)}  # type: ignore[attr-defined]

    @tool("hardware", read_only=False)
    def read_box_state(self, boxes: list[str] | None = None) -> dict:
        """Read every box's persistent settings back: DC bias, RF, ARB, clocks.

        `boxes` names some by name; all found boxes otherwise. Reads only; sends no
        setter. Under --fake the values are the stand-ins' own.
        """
        event, handle = self._run(ReadState(names=tuple(boxes or ())), JOB_WAIT_S)
        states = event.result if isinstance(event, JobFinished) else None
        if not isinstance(states, Mapping):
            return self._unfinished(handle, event)
        return {
            "boxes": {name: _plain(_bare(to_wire(state))) for name, state in states.items()},
            "text": "\n".join(state.render() for state in states.values()
                              if isinstance(state, BoxState)),
        }

    @tool("hardware", read_only=False)
    def arm(self, request: str, initials: str, method: str = "", template: str = "",
            knobs: dict[str, int | float] | None = None, labels: dict[str, str] | None = None,
            setup: bool = True, conditions: str = "", request_id: str = "") -> dict:
        """Send a method to every box and leave them armed, waiting for `acquire`.

        Run `discover_boxes` once first, so the server knows which boxes answer.
        `request` is what the experiment is for: the person's own words, quoted, without
        greetings or sign-offs. `initials` are theirs, and name the files. Name either
        `method` or `template` with `knobs` and `labels`. Leave `setup` true unless this
        same method was armed earlier in this session; false sends only the table and
        the mode change. `conditions` is free text about the sample and source (for
        example concentration, solvent, spray voltage), stamped into every file acquired
        from this arming; empty is allowed. `request_id` continues an earlier request
        instead of starting one. Refused outside --fake until the standing-limits check
        exists. Answers the request id, the stem the first file will take, and what the
        boxes read back.
        """
        loaded, rendered, path = self._method(method, template, knobs, labels)
        self._refuse_unless_allowed(rendered, request)
        who = self._initials(initials)
        identity, _ = self._request(request, request_id)
        stem = next_stem(self.output, who)
        event, handle = self._run(Send(
            label=f"arming for {identity}", method=loaded, setup=setup,
            conditions=conditions, directory=self.output, stem=stem,
            method_path=path, instrument=self.instrument,
            instrument_path=self.instrument_path), JOB_WAIT_S)
        result = event.result if isinstance(event, JobFinished) else None
        if not isinstance(result, SendResult):
            return {"request_id": identity, **self._unfinished(handle, event)}
        with self._guard:
            self._armed = result.armed
            self._armed_stem = stem
            self._armed_name = loaded.metadata.name
            self._armed_conditions = conditions
        return {
            "request_id": identity,
            "stem": stem,
            "method": loaded.metadata.name,
            "hash": method_module.stamp(loaded)["method_hash"][:HASH_DIGITS],
            "setup": result.setup,
            "seconds": round(result.seconds, 2),
            "send_log": result.send_log,
            "transcript": result.transcript_path,
            "read_back": _snapshot_text(result.snapshot),
        }

    # -- acquisition -------------------------------------------------------------

    @tool("acquisition", read_only=False)
    def acquire(self, request: str, initials: str, method: str = "", template: str = "",
                knobs: dict[str, int | float] | None = None,
                labels: dict[str, str] | None = None, replicates: int = 1,
                request_id: str = "") -> dict:
        """Start an acquisition of the armed method, and answer at once with a job number.

        Same `request`, `initials`, method or template, knobs and labels as `arm`, which
        must have sent this exact method first; pass `arm`'s `request_id` so the files
        join that request. `replicates` is how many files to acquire: "three replicates"
        is 3, three files, of which the first is the run and the rest its technical
        replicates. The conditions given to `arm` are stamped into every file. Every
        file also stamps the request id and its place in the request. Follow the job
        with `progress` until `done`; stop it with `stop`. Refused outside --fake until
        the standing-limits check exists.
        """
        if replicates < 1:
            raise ToolFailure("replicates counts files, from 1")
        loaded, rendered, path = self._method(method, template, knobs, labels)
        self._refuse_unless_allowed(rendered, request)
        who = self._initials(initials)
        with self._guard:
            armed, armed_stem = self._armed, self._armed_stem
            conditions = self._armed_conditions
        if not (matches_wire(armed, loaded)
                and self.owner.status().snapshot):  # type: ignore[attr-defined]
            raise ToolFailure(
                "the boxes are not holding this method: arm it first, with the same "
                "method or template and knob values")
        identity, words = self._request(request, request_id)
        with self._guard:
            place = self._places.get(identity) or self._next_place_on_disk(identity)
            self._places[identity] = place + replicates
        stem = armed_stem if armed_stem and not self._stem_taken(armed_stem) else ""
        job = Acquire(
            label=f"acquiring for {identity}", method=loaded, instrument=self.instrument,
            instrument_path=self.instrument_path, method_path=path,
            directory=self.output, stem=stem, initials=who, replicates=replicates,
            conditions=conditions, request=words, series=identity, series_index=place,
            template=rendered.template_text if rendered is not None else "",
            knobs=dict(rendered.knobs) if rendered is not None else {},
            labels=dict(rendered.labels) if rendered is not None else {})
        handle = self._submit(job)
        with self._guard:
            self._planned[handle.id] = replicates
        return {"job": handle.id, "request_id": identity, "first_place": place,
                "replicates": replicates, "label": handle.label,
                "follow": f"call progress with job={handle.id}"}

    @tool("acquisition")
    def progress(self, job: int, after: int = 0, wait_s: float = PROGRESS_WAIT_S) -> dict:
        """What a job has done since event number `after`, waiting up to `wait_s` for news.

        Pass the `last` number of one answer as `after` in the next to read only what is
        new. The call waits through the scan counter's ticks and answers when something
        else happens or `wait_s` runs out (capped at 30 s; 0 answers at once), with the
        counter's latest value only. `position` says how many files are done of how many
        were asked for and how many scans the frame in progress has published. `done` is
        true once the job has finished or failed; a finished acquisition lists its files
        under `runs`, and a failed job says why under `failed`.
        """
        with self._guard:
            handle = self._handles.get(job)
        if handle is None:
            raise ToolFailure(f"job {job} was not started through this server")
        wait = max(0.0, min(float(wait_s), PROGRESS_WAIT_MAX_S))
        deadline = time.monotonic() + wait
        entries: list[Progress] = []
        try:
            while True:
                seen = entries[-1].seq if entries else after
                fresh = self._wait(handle, seen, max(0.0, deadline - time.monotonic()))
                entries += fresh
                if (not fresh or time.monotonic() >= deadline
                        or any(not isinstance(entry.event, BatchSeen) for entry in fresh)):
                    break
            history = list(self.owner.events(handle, 0))  # type: ignore[attr-defined]
        except StaleHandle as exc:
            raise ToolFailure(str(exc)) from exc
        shown = [entry for number, entry in enumerate(entries)
                 if not (isinstance(entry.event, BatchSeen) and number + 1 < len(entries)
                         and isinstance(entries[number + 1].event, BatchSeen))]
        counter = next((entry.event for entry in reversed(history)
                        if isinstance(entry.event, BatchSeen)), None)
        with self._guard:
            planned = self._planned.get(job)
        answer: dict[str, Any] = {
            "job": job, "label": handle.label,
            "events": [_entry(entry) for entry in shown],
            "last": entries[-1].seq if entries else after,
            "position": {
                "files_done": sum(isinstance(entry.event, RunDone) for entry in history),
                "files_asked": planned,
                "scans_this_frame": counter.scans_so_far if counter is not None else 0,
            },
            "done": False,
        }
        for entry in entries:
            if isinstance(entry.event, JobFinished):
                answer["done"] = True
                answer["result"] = _result(entry.event.result)
            elif isinstance(entry.event, JobFailed):
                answer["done"] = True
                answer["failed"] = entry.event.message
        if isinstance(answer.get("result"), list):
            answer["runs"] = answer.pop("result")
        return answer

    @tool("acquisition", read_only=False)
    def stop(self, reason: str = "stopped at the request of the person it was for") -> dict:
        """Stop the acquisition in flight after its current repetition and its fold.

        What it leaves on disk is a short experiment, not a broken one; replicates not
        yet started are not acquired. `reason` goes in the file's log.
        """
        running = self.owner.status().running  # type: ignore[attr-defined]
        self.owner.stop(reason)  # type: ignore[attr-defined]
        return {"stopping": running is not None,
                "job": running.id if running is not None else None,
                "reason": reason}

    @tool("acquisition")
    def status(self) -> dict:
        """The instrument as the server sees it: simulated or real, the console, the
        boxes, the job running and those queued, the method this server last armed and
        the stem it named then, and whether sends through this server are refused and
        why."""
        state = self.owner.status()  # type: ignore[attr-defined]
        with self._guard:
            armed = ({"method": self._armed_name, "stem": self._armed_stem}
                     if self._armed else None)
        refused = [] if state.fake else guard_acquisition(self.owner, None, "status")
        return {
            "fake": state.fake,
            "program": state.program,
            "console": state.console.text,
            "boxes": list(state.boxes),
            "running": _handle(state.running),
            "queued": [_handle(handle) for handle in state.queued],
            "stopping": state.stopping,
            "last_armed": armed,
            "holder": state.holder.text if state.holder is not None else None,
            "lock_refused": state.refused or None,
            "sends_refused": refused[0] if refused else None,
            "library": self.library,
            "output": self.output,
        }

    # -- data --------------------------------------------------------------------

    @tool("data")
    def list_files(self, directory: str = "") -> dict:
        """The runs in the output directory (or `directory`), newest first.

        For each stem: the raw per-repetition file and the summed file with sizes and
        times, the send log and the wire transcript, the request the run served (read
        from the file's own stamp, with the request's words from the audit log), and
        `folded`: whether the summed file exists, which a run cut short never writes.
        """
        where = self._in_output(directory) if directory else self.output
        stems: dict[str, dict[str, Any]] = {}
        for path in glob.glob(os.path.join(where, "*.uimf")):
            name = os.path.basename(path)
            if name.endswith(SUMMED_SUFFIX):
                stem, kind = name[: -len(SUMMED_SUFFIX)], "summed"
            else:
                stem, kind = name[: -len(RAW_SUFFIX)], "raw"
            stems.setdefault(stem, {"stem": stem})[kind] = _file(path)
        words = self.log.requests()
        runs = []
        for stem, run in stems.items():
            sent = send_log_name(stem)
            run["send_log"] = sent if os.path.isfile(os.path.join(where, sent)) else None
            run["transcripts"] = sorted(os.path.basename(path) for path in glob.glob(
                os.path.join(where, glob.escape(stem) + "-*.transcript.log")))
            stamped_file = (run.get("summed") or run.get("raw") or {}).get("name", "")
            stamped = _series(os.path.join(where, stamped_file)) if stamped_file else None
            if stamped is not None:
                stamped["text"] = words.get(stamped["id"], "")
            run["request"] = stamped
            run["folded"] = "summed" in run
            runs.append(run)
        runs.sort(key=lambda run: max(part.get("modified", "") for part in
                                      (run.get("raw", {}), run.get("summed", {}))),
                  reverse=True)
        return {"directory": where, "runs": runs}

    @tool("data")
    def summarize_file(self, path: str, frames: list[int] | None = None, points: int = 200,
                       texts: bool = False) -> dict:
        """What one UIMF file holds, as numbers: the first thing to ask of a run.

        Which file of the pair it is, frames, scans, accumulations, total counts, the
        total ion current per frame and against scan, the base peak, the calibration,
        the pusher period, saturation, and every clockwork stamp including a rendered
        run's knobs, labels and marks, and under `request` the request the run served
        with its words. `path` is in the output directory or absolute. Profiles are
        summed down to at most `points` values. `frames.provisional` counts frames still
        being written, or left unfinished by a run that was cut short.
        """
        found = summary.summarize(self._in_output(path), frames=frames, points=points,
                                  texts=texts)
        stamps = found.get("clockwork") or {}
        identity = stamps.get("ClockworkSeriesId")
        found["request"] = None if not identity else {
            "id": identity, "index": stamps.get("ClockworkSeriesIndex"),
            "position": stamps.get("ClockworkSeriesPosition"),
            "text": self.log.requests().get(str(identity), "")}
        return found

    @tool("data")
    def windowed_intensities(self, path: str, windows: str | dict[str, list],
                             reference: str | None = None, scans: list[int] | None = None,
                             frames: list[int] | None = None) -> dict:
        """Summed intensity in named m/z windows, and each window's ratio to a reference.

        `windows` is a preset's name or a mapping of name to `[lo, hi]` in m/z (or a
        list of such intervals, summed). `scans` is a half-open `[start, stop)` scan
        range, a mobility window; scans count from 0. A ratio means something only
        against a reference measured the same way on the same day.
        """
        return summary.windowed(self._in_output(path), windows, reference=reference,
                                scans=scans, frames=frames)

    @tool("data")
    def arrival_time_distribution(self, path: str, mz: str | list,
                                  preset: str | None = None,
                                  frames: list[int] | None = None,
                                  points: int = 200) -> dict:
        """Intensity against scan in one m/z window, with the peak in scans and in ms.

        `mz` is `[lo, hi]`, a list of intervals, or with `preset` one of the preset's
        window names. The peak is the argmax and the centroid over its half-maximum
        run, taken on the undecimated profile; the profile itself is summed down to at
        most `points` values.
        """
        return summary.atd(self._in_output(path), mz, preset=preset, frames=frames,
                           points=points)

    # -- helpers -----------------------------------------------------------------

    def _library_files(self) -> list[str]:
        if not self.library or not os.path.isdir(self.library):
            return []
        return sorted(glob.glob(os.path.join(self.library, "**", "*.toml"), recursive=True))

    @staticmethod
    def _is_template(path: str) -> bool:
        try:
            with open(path, "rb") as handle:
                return template_module.is_template(tomllib.load(handle))
        except (OSError, tomllib.TOMLDecodeError):
            return False

    def _in_library(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        if not self.library:
            raise ToolFailure(f"{path!r} is not an absolute path and no method library is "
                              "set; start the server with --library")
        return os.path.join(self.library, path)

    def _in_output(self, path: str) -> str:
        return path if os.path.isabs(path) else os.path.join(self.output, path)

    def _method(self, method: str, template: str, knobs: Mapping[str, object] | None,
                labels: Mapping[str, object] | None) -> tuple[Method, Rendered | None, str]:
        """The method a tool was pointed at: a document, or a template rendered.

        Answers the method, its render (None for a document) and its path."""
        if bool(method) == bool(template):
            raise ToolFailure("name a method or a template, one of the two")
        if method:
            if knobs or labels:
                raise ToolFailure("knobs and labels belong to a template, not a method")
            path = self._in_library(method)
            return method_module.load(path), None, path
        path = self._in_library(template)
        rendered = template_module.render(template_module.load_template(path),
                                          dict(knobs or {}), dict(labels or {}))
        return rendered.method, rendered, path

    def _refuse_unless_allowed(self, rendered: Rendered | None, request: str) -> None:
        problems = guard_acquisition(self.owner, rendered, request)
        if problems:
            raise ToolFailure("; ".join(problems))

    @staticmethod
    def _initials(initials: str) -> str:
        cleaned = clean_initials(initials)
        if not cleaned:
            raise ToolFailure("say whose run this is: give the initials of the person it "
                              "is for, which name the files")
        return cleaned

    def _request(self, words: str, identity: str) -> tuple[str, str]:
        """The request id for these words, minted on first use, and the words."""
        words = words.strip()
        with self._guard:
            if identity:
                self._words.setdefault(identity, words)
                self._ids.setdefault(words, identity)
                return identity, words
            known = self._ids.get(words)
            if known is not None:
                return known, words
            digest = hashlib.sha256(f"{time.time_ns()}{words}".encode()).hexdigest()[:6]
            identity = f"{_dt.datetime.now():%y%m%d-%H%M%S}-{digest}"
            self._ids[words] = identity
            self._words[identity] = words
            return identity, words

    def _next_place_on_disk(self, identity: str) -> int:
        """One past the highest place this request's files already hold, for a request
        continued by id from an earlier session. Under `_guard`."""
        highest = 0
        for path in glob.glob(os.path.join(self.output, "*" + SUMMED_SUFFIX)):
            stamped = _series(path)
            if stamped is not None and stamped["id"] == identity:
                highest = max(highest, int(stamped.get("index") or 0))
        return highest + 1

    def _stem_taken(self, stem: str) -> bool:
        return any(os.path.exists(os.path.join(self.output, stem + suffix))
                   for suffix in (RAW_SUFFIX, SUMMED_SUFFIX))

    def _submit(self, job: object) -> Handle:
        handle = self.owner.submit(job)  # type: ignore[attr-defined]
        with self._guard:
            self._handles[handle.id] = handle
        return handle

    def _run(self, job: object, timeout: float) -> tuple[Event | None, Handle]:
        """Submit `job` and wait up to `timeout` for it to finish or fail."""
        handle = self._submit(job)
        deadline = time.monotonic() + timeout
        seen = 0
        while True:
            entries = self._wait(handle, seen, max(0.0, deadline - time.monotonic()))
            for entry in entries:
                seen = entry.seq
                if isinstance(entry.event, (JobFinished, JobFailed)):
                    if isinstance(entry.event, JobFailed):
                        raise ToolFailure(entry.event.message)
                    return entry.event, handle
            if time.monotonic() >= deadline:
                return None, handle

    def _wait(self, handle: Handle, after: int, timeout: float) -> list[Progress]:
        """The owner's progress after `after`, waiting up to `timeout` for some.

        `RemoteOwner.wait` sleeps on its event stream; an owner in this process has no
        such call and is asked again every fifty milliseconds."""
        waiting = getattr(self.owner, "wait", None)
        if waiting is not None:
            return list(waiting(handle, after, timeout))
        deadline = time.monotonic() + timeout
        while True:
            found = list(self.owner.events(handle, after))  # type: ignore[attr-defined]
            if found or time.monotonic() >= deadline:
                return found
            time.sleep(0.05)

    @staticmethod
    def _unfinished(handle: Handle, event: Event | None) -> dict:
        return {"job": handle.id, "done": event is not None,
                "still_running": event is None,
                "follow": f"call progress with job={handle.id}"}


# -- what goes back ----------------------------------------------------------------


def _plain(value: object) -> Any:
    """`value` as JSON can carry it: dates as ISO text, tuples as lists."""
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _bare(value: object) -> Any:
    """A wire form without its `@type` tags, for a result a person reads."""
    if isinstance(value, Mapping):
        return {key: _bare(item) for key, item in value.items() if key != TYPE_KEY}
    if isinstance(value, list):
        return [_bare(item) for item in value]
    return value


def _handle(handle: Handle | None) -> dict | None:
    if handle is None:
        return None
    return {"job": handle.id, "kind": handle.kind, "label": handle.label}


def _run_summary(run: Run) -> dict:
    return {
        "text": run.text,
        "raw_path": run.raw_path,
        "summed_path": run.summed_path,
        "raw_kept": run.raw_kept,
        "complete": run.complete,
        "stopped_early": run.stopped_early,
        "replicate": run.replicate,
        "seconds": round(run.seconds, 2),
        "frames": len(run.frames),
        "failed_frames": [
            f"frame {record.method_frame}.{record.repetition}: {record.outcome}"
            + (f": {record.detail}" if record.detail else "")
            for record in run.failures],
        "failed_folds": [fold.text for fold in run.folds if fold.error is not None],
        "scans_published": run.scans_published,
        "warnings": list(run.warnings),
    }


def _snapshot_text(snapshot: Snapshot | None) -> str:
    """The read-back after `setup`, as the state panel renders it, or the earlier one."""
    if snapshot is None:
        return ""
    states = snapshot.after or snapshot.before
    return "\n".join(state.render() for state in states)


def _result(result: object) -> Any:
    """A finished job's result, cut to what a caller reads: never a method's text."""
    if isinstance(result, (list, tuple)) and all(isinstance(run, Run) for run in result):
        return [_run_summary(run) for run in result]
    if isinstance(result, SendResult):
        return {"setup": result.setup, "seconds": round(result.seconds, 2),
                "send_log": result.send_log, "transcript": result.transcript_path}
    if isinstance(result, Discovery):
        return {"text": result.text}
    if isinstance(result, Mapping):
        return {"boxes": sorted(result)}
    try:
        return _bare(to_wire(result))
    except TypeError:
        return repr(result)


def _entry(progress: Progress) -> dict:
    """One numbered event: its kind, its line of text, and the fields worth reading."""
    event = progress.event
    out: dict[str, Any] = {"seq": progress.seq, "kind": type(event).__name__,
                           "text": event.text}
    if isinstance(event, (JobStarted, JobFinished, JobFailed, Discovered)):
        return out
    if isinstance(event, RunDone):
        out["run"] = _run_summary(event.run)
    elif isinstance(event, BatchSeen):
        out.update(method_frame=event.method_frame, repetition=event.repetition,
                   scans_so_far=event.scans_so_far)
    else:
        try:
            fields = _bare(to_wire(event))
        except TypeError:
            fields = {}
        out.update({key: value for key, value in fields.items() if key not in out})
    return out


def _file(path: str) -> dict:
    status = os.stat(path)
    return {"name": os.path.basename(path), "bytes": status.st_size,
            "modified": _dt.datetime.fromtimestamp(status.st_mtime).isoformat(
                timespec="seconds")}


def _series(path: str) -> dict | None:
    """The request a clockwork file served, off its own stamp, or None."""
    if not path or not os.path.isfile(path):
        return None
    try:
        from mainspring.uimf import UimfFile

        extra = UimfFile(path).global_params().extra
    except Exception:  # noqa: BLE001 -- a file that will not open served no request we know
        return None
    identity = extra.get("ClockworkSeriesId")
    if not identity:
        return None

    def number(key: str) -> int | None:
        try:
            return int(extra[key])
        except (KeyError, ValueError):
            return None

    return {"id": identity, "index": number("ClockworkSeriesIndex"),
            "position": number("ClockworkSeriesPosition")}

