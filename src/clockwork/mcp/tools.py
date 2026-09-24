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
failed send is reported before any acquisition starts. Then the cold-start check
(`clockwork.envelope.cold_start`): `arm` reads the boxes back -- getters only, as
`read_box_state` does -- and refuses or cautions on what they hold that the method does
not declare before the `Send` job exists, and `acquire` repeats it against what that
`arm`'s send read back, declared values included (lab record, task 71).

**Every request leaves a run record** (`clockwork.record`) beside its files: `arm` opens
it with the request, the plan and the arming, `acquire` adds the acquisition, a thread
per acquisition adds each file with its summary as the owner reports it, and `note` adds
the session's notes.

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
import json
import os
import threading
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .. import envelope, summary
from .. import method as method_module
from .. import routine as routine_module
from ..acq import BatchSeen, Event, Run, Snapshot, cautions, refusals
from ..acq.uimf import RAW_SUFFIX, SUMMED_SUFFIX
from ..app import methodlib
from ..envelope import Ledger, Limits
from ..instrument import UNCALIBRATED, Instrument
from ..method import Method, MethodError
from ..method import template as template_module
from ..method.template import Rendered, Template, TemplateError
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
from ..record import RECORD_SUFFIX, RunRecord, brief
from ..record import find as find_record
from ..routine import Routine, RoutineError
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

ROUTINE_WAIT_S = 3600.0
"""How long `run_routine` waits for its acquisition before judging it could not judge.
A routine's run is seconds to minutes; an hour is a run that is stuck."""

HASH_DIGITS = methodlib.HASH_DIGITS


class ToolFailure(ValueError):
    """A tool's answer when it could not do what it was asked: one sentence."""


# -- the registry --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tool:
    """One tool: its name, its group, the `Toolbox` method behind it, what it says."""

    name: str
    group: str
    """`method`, `hardware`, `acquisition`, `data` or `routine`."""
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
    requests it has minted and how many runs each has had -- is kept under one lock.
    **What the boxes are holding is not among it**: that is the owner's
    (`OwnerStatus.armed`, the send's `Snapshot`), so an `acquire` in one process follows
    an `arm` in another, as the command line's verbs do (lab record, task 73).
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
        limits: Limits | None = None,
        routines: str = "",
    ) -> None:
        self.owner = owner
        self.library = os.path.abspath(library) if library else ""
        self.routines = (os.path.abspath(routines) if routines
                         else routine_module.default_directory(self.library))
        """Where the instrument's routines are: `routines` beside the library unless named."""
        self.narrate: Callable[[str], None] | None = None
        """Told each step of a `run_routine` as it happens, for a person watching a
        command line; nothing else reads it."""
        self.output = os.path.abspath(output or os.getcwd())
        self.instrument = instrument
        self.instrument_path = instrument_path
        self.log = log if log is not None else AuditLog.beside(self.output)
        self.limits = limits
        """The standing limits in force, or None: every send refused against the
        instrument, nothing refused in a rehearsal (`clockwork.envelope.check`)."""
        self._guard = threading.Lock()
        self._session: tuple[str, _dt.datetime] | None = None
        """The daemon session's id and start, asked for once: the budget's window."""
        self._handles: dict[int, Handle] = {}
        self._words: dict[str, str] = {}
        """Request id to the words it was minted for."""
        self._ids: dict[str, str] = {}
        """The words to the id they minted, so a second call with the same words
        continues the same request."""
        self._places: dict[str, int] = {}
        """Request id to the place its next run takes, counted from 1."""
        self._planned: dict[int, int] = {}
        """Job number to the replicates it was asked for, for `progress`'s position."""
        self._recorders: dict[int, threading.Thread] = {}
        """Job number to the thread adding its files to its request's run record."""

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
            self.log.session = self._session_of()[0]
        except DaemonError:
            pass  # the call below meets the same daemon, and says so in its own words
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
        turn a person's request into knob values, then `render_template`. When the
        instrument's standing limits are in force, `limits` says whether an agent may
        run the template at all and how far each knob may be turned: narrower than the
        template's own range, or fixed.
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
                "limits": self._limits_of(loaded),
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
        not check and will not stop for. `limits` are why the instrument's standing
        limits would stop this server sending it: a template they do not list, a knob
        outside their range. `ok` is true when problems, refusals and limits are all
        empty. `warnings` are repairs clockwork made while loading, such as whitespace
        stripped from a string; they never stop a run.
        """
        try:
            loaded, rendered, _ = self._method(method, template, knobs, labels)
        except (MethodError, TemplateError) as exc:
            return {"ok": False, "problems": list(exc.problems), "refusals": [],
                    "cautions": [], "limits": [], "warnings": []}
        found = refusals(loaded)
        outside = self._outside_limits(loaded, rendered)
        return {
            "ok": not found and not outside,
            "name": loaded.metadata.name,
            "hash": method_module.stamp(loaded)["method_hash"][:HASH_DIGITS],
            "rendered": rendered is not None,
            "problems": [],
            "refusals": found,
            "cautions": cautions(loaded),
            "limits": outside,
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
        false. A knob inside the template's range and outside the instrument's standing
        limits is rendered, not clamped, and comes back in `limits`, with `ok` false.
        `warnings` are repairs made while loading; they never stop a run.
        """
        try:
            loaded, rendered, _ = self._method("", template, knobs, labels)
        except TemplateError as exc:
            return {"ok": False, "problems": list(exc.problems)}
        assert rendered is not None
        found = refusals(loaded)
        outside = self._outside_limits(loaded, rendered)
        return {
            "ok": not found and not outside,
            "problems": [],
            "limits": outside,
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
            setup: bool = True, conditions: str = "", request_id: str = "",
            plan: str = "") -> dict:
        """Send a method to every box and leave them armed, waiting for `acquire`.

        Run `discover_boxes` once first, so the server knows which boxes answer.
        `request` is what the experiment is for: the person's own words, quoted, without
        greetings or sign-offs. `initials` are theirs, and name the files. Name either
        `method` or `template` with `knobs` and `labels`. Leave `setup` true unless this
        same method was armed earlier in this session; false sends only the table and
        the mode change. `conditions` is free text about the sample and source (for
        example concentration, solvent, spray voltage), stamped into every file acquired
        from this arming; empty is allowed. `request_id` continues an earlier request
        instead of starting one. `plan` is what you intend to do for the request, as you
        stated it to the person: which knob values, how many acquisitions, what you
        will report; it goes in the request's run record.

        Refused, before anything is sent, outside the instrument's standing limits:
        a template they do not list, a knob outside their range, a box they do not
        allow, a spent budget, or on the instrument a hand-written method. Then the
        boxes are read back and compared with what the method declares: what a box
        holds that the method leaves as found is refused or cautioned by the limits'
        cold-start rule, and the cautions come back under `cold_start` for you to
        report. Answers the request id, the stem the first file will take, what the
        boxes read back, and the run record's path.
        """
        loaded, rendered, path = self._method(method, template, knobs, labels)
        self._refuse_unless_allowed(loaded, rendered, request)
        who = self._initials(initials)
        found = self._read_boxes(loaded)
        refused, cautioned = envelope.judge(envelope.cold_start(loaded, found),
                                            self._cold_start_mode(rendered))
        if refused:
            raise ToolFailure(_cold_start_refusal(refused))
        identity, words = self._request(request, request_id)
        stem = next_stem(self.output, who)
        record = RunRecord.open(self.output, stem, request_id=identity, text=words,
                                initials=who)
        if plan.strip():
            record.append("plans", {"text": plan.strip()})
        event, handle = self._run(Send(
            label=f"arming for {identity}", method=loaded, setup=setup,
            conditions=conditions, directory=self.output, stem=stem,
            method_path=path, instrument=self.instrument,
            instrument_path=self.instrument_path), JOB_WAIT_S)
        result = event.result if isinstance(event, JobFinished) else None
        arming = {
            "stem": stem, "method": loaded.metadata.name,
            "hash": method_module.stamp(loaded)["method_hash"][:HASH_DIGITS],
            "template": _template_entry(rendered, path), "setup": setup,
            "conditions": conditions, "cold_start": cautioned,
        }
        if not isinstance(result, SendResult):
            record.append("arms", {**arming, "job": handle.id, "finished": False})
            return {"request_id": identity, "record": record.path,
                    **self._unfinished(handle, event)}
        record.append("arms", {**arming, "finished": True,
                               "seconds": round(result.seconds, 2),
                               "send_log": result.send_log})
        return {
            "request_id": identity,
            "stem": stem,
            "method": loaded.metadata.name,
            "hash": arming["hash"],
            "setup": result.setup,
            "seconds": round(result.seconds, 2),
            "send_log": result.send_log,
            "transcript": result.transcript_path,
            "read_back": _snapshot_text(result.snapshot),
            "cold_start": cautioned,
            "record": record.path,
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
        with `progress` until `done`; stop it with `stop`. Refused as `arm` is outside
        the standing limits, and for more replicates than they allow in one acquisition
        or once the budget is spent; the cold-start comparison is repeated against what
        the boxes read back when `arm` sent the method, including any declared value a
        box does not hold, and its cautions come back under `cold_start`.
        """
        if replicates < 1:
            raise ToolFailure("replicates counts files, from 1")
        loaded, rendered, path = self._method(method, template, knobs, labels)
        self._refuse_unless_allowed(loaded, rendered, request, replicates=replicates)
        who = self._initials(initials)
        armed = self.owner.status().armed  # type: ignore[attr-defined]
        snapshot = self.owner.snapshot()  # type: ignore[attr-defined]
        if armed is None or snapshot is None or not matches_wire(armed.fingerprint, loaded):
            raise ToolFailure(
                "the boxes are not holding this method: arm it first, with the same "
                "method or template and knob values")
        states = snapshot.after or snapshot.before
        conditions = snapshot.conditions
        armed_stem = armed.stem if _same_directory(armed.directory, self.output) else ""
        refused, cautioned = envelope.judge(
            envelope.cold_start(loaded, states, declared=True),
            self._cold_start_mode(rendered))
        if refused:
            raise ToolFailure(_cold_start_refusal(refused))
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
        record = RunRecord.open(self.output, stem or next_stem(self.output, who),
                                request_id=identity, text=words, initials=who)
        record.append("acquisitions", {
            "job": handle.id, "replicates": replicates, "first_place": place,
            "method": loaded.metadata.name,
            "template": _template_entry(rendered, path), "cold_start": cautioned})
        recorder = threading.Thread(target=self._record_runs, args=(handle, record),
                                    name=f"run record, job {handle.id}", daemon=True)
        with self._guard:
            self._recorders[handle.id] = recorder
        recorder.start()
        return {"job": handle.id, "request_id": identity, "first_place": place,
                "replicates": replicates, "label": handle.label,
                "cold_start": cautioned, "record": record.path,
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
        handle = self._known(job)
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
        started = next((entry.event.job for entry in history
                        if isinstance(entry.event, JobStarted)), None)
        with self._guard:
            planned = self._planned.get(job)
            recorded = job in self._recorders
        if planned is None and isinstance(started, Acquire):
            planned = started.replicates
        if not recorded and isinstance(started, Acquire):
            self._record_seen(started, [entry.event.run for entry in entries
                                        if isinstance(entry.event, RunDone)])
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
        boxes, the job running and those queued, the method the boxes were last armed
        with and the stem that arming named, whether sends through this server are refused and why,
        the standing limits in force, and how much of this daemon session's budget is
        left."""
        state = self.owner.status()  # type: ignore[attr-defined]
        armed = ({"method": state.armed.method, "stem": state.armed.stem}
                 if state.armed is not None else None)
        ledger = self._ledger()
        refused = guard_acquisition(self.owner, None, "status", limits=self.limits,
                                    ledger=ledger)
        limits = self.limits
        return {
            "limits": None if limits is None else {
                "path": limits.path, "description": limits.description,
                "cold_start": limits.cold_start, "boxes": list(limits.boxes),
                "templates": [entry.key for entry in limits.templates],
                "problems": limits.against(self._templates()),
            },
            "budget": None if limits is None else {
                "acquisitions_made": ledger.runs,
                "max_acquisitions": limits.budget.max_runs,
                "hours_since_start": round(ledger.hours, 2),
                "max_hours": limits.budget.max_hours,
                "max_replicates_per_acquisition": limits.budget.max_replicates_per_run,
                "daemon_session": self._session_of()[0],
            },
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
            "sends_refused": "; ".join(refused) if refused else None,
            "library": self.library,
            "output": self.output,
        }

    @tool("acquisition", read_only=False)
    def note(self, request_id: str, text: str) -> dict:
        """Add a note to a request's run record, beside its files.

        For what the files alone will not say: why the next acquisition is the one you
        chose, what you told the person, a judgement on a file, why you stopped. One
        note per call; `request_id` is the one `arm` answered. Answers the record's path
        and how many notes it holds.
        """
        if not text.strip():
            raise ToolFailure("a note says something: pass its text")
        found = find_record(self.output, request_id)
        if found is None:
            raise ToolFailure(f"request {request_id} has no run record in {self.output}; "
                              "a record is begun by the request's first arm")
        record = RunRecord(found)
        record.append("notes", {"text": text.strip(), "by": "session"})
        return {"record": record.path, "notes": len(record.read().get("notes", []))}

    # -- data --------------------------------------------------------------------

    @tool("data")
    def list_files(self, directory: str = "") -> dict:
        """The runs in the output directory (or `directory`), newest first.

        For each stem: the raw per-repetition file and the summed file with sizes and
        times, the send log and the wire transcript, the request the run served (read
        from the file's own stamp, with the request's words from the audit log), the
        request's run record, and `folded`: whether the summed file exists, which a
        run cut short never writes.
        """
        where = self._in_output(directory) if directory else self.output
        records = _records(where)
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
            run["record"] = records.get(stamped["id"]) if stamped is not None else None
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

    @tool("data")
    def ion_events(self, path: str, frames: list[int] | None = None) -> dict:
        """Ion arrivals push by push: how many per push, how tall, how wide.

        The detector's single-ion view, for a file whose rows are single pushes: the
        raw file of any run, or the summed file of a run with one accumulation, such as
        the detection-response experiment. An event is a run of consecutive stored bins
        in one push. Answers `events_per_push`, `occupancy` (the fraction of pushes
        holding any), the event heights at the 5th to 99th percentiles and their
        maximum in stored units and in millivolts, the widths in bins, the median
        area, how many events reached the card's top code (`railed_events`), and the
        total counts per push. A summed file of several pushes a row is refused.
        """
        return summary.ion_events(self._in_output(path), frames=frames)

    # -- routines ----------------------------------------------------------------

    @tool("routine")
    def list_routines(self) -> dict:
        """The instrument's routines: experiments run with nothing left to choose.

        Each routine is a template at fixed knob values, or a read-back of the boxes
        against documents, with the criteria that judge it. For each: its name, what it
        asks, whether it may run with nobody at the instrument (`unattended`), what it
        acquires or reads, each criterion in words with its source, and whether the
        standing limits allow its template. Run one with `run_routine`.
        """
        routines = []
        for path, loaded in routine_module.scan(self.routines):
            relative = os.path.relpath(path, self.routines).replace(os.sep, "/")
            if isinstance(loaded, RoutineError):
                routines.append({"path": relative, "problem": str(loaded)})
                continue
            entry: dict[str, Any] = {
                "name": loaded.name, "path": relative, "description": loaded.description,
                "unattended": loaded.unattended, "hash": loaded.hash[:HASH_DIGITS],
                "acquires": None, "audits": [entry.document for entry in loaded.audit],
                "measures": [{"name": item.name, "function": item.function,
                              "file": item.file} for item in loaded.measures],
                "criteria": [{"name": item.name, "value": item.value,
                              "comparison": item.describe(), "unmet": item.unmet,
                              "judged": item.judge, "source": item.source}
                             for item in loaded.criteria],
                "limits": None,
            }
            if loaded.acquire is not None:
                spec = loaded.acquire
                entry["acquires"] = {"template": spec.template, "knobs": dict(spec.knobs),
                                     "labels": dict(spec.labels),
                                     "replicates": spec.replicates}
                try:
                    entry["limits"] = self._limits_of(
                        template_module.load_template(self._in_library(spec.template)))
                except (OSError, TemplateError, ToolFailure) as exc:
                    entry["problem"] = f"its template does not load: {exc}"
            routines.append(entry)
        return {"directory": self.routines, "routines": routines}

    @tool("routine", read_only=False)
    def run_routine(self, name: str, initials: str, conditions: str = "") -> dict:
        """Run one of the instrument's routines to its verdict, and answer the report.

        `name` is a routine's name from `list_routines`. `initials` are those of the
        person it is run for, or who scheduled it, and name the files. `conditions` is
        free text about the sample and source, stamped into every file beside the
        routine's own; empty is allowed. A routine is a request with no free
        parameters: it is armed and acquired through `arm` and `acquire`, so the
        standing limits, the cold-start check and the budget apply to it exactly, and it
        leaves a run record and stamped files like any request. A routine that reads
        the boxes back acquires nothing.

        Answers when the routine is judged, which for one that acquires is after its
        files are written: `verdict` is `pass`, `fail` or `could not judge`, with
        `reason` (such as no beam, saturation, or a refused arm); `criteria` gives each
        number, what it was compared with and whether it was met; `text` is the report
        in a few lines to give the person; `record` is the run record it was added to.
        """
        loaded = self._routine(name)
        who = self._initials(initials)
        words = routine_module.request_words(loaded)
        self._say(f"routine {loaded.name}: {loaded.description or 'no description'}")
        if loaded.audit:
            return self._run_audit(loaded, who, words)
        return self._run_acquiring(loaded, who, words, conditions)

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

    def _refuse_unless_allowed(self, loaded: Method, rendered: Rendered | None, request: str,
                               *, replicates: int | None = None) -> None:
        problems = guard_acquisition(self.owner, rendered, request, method=loaded,
                                     limits=self.limits, ledger=self._ledger(),
                                     replicates=replicates)
        if problems:
            raise ToolFailure("; ".join(problems))

    # -- the standing envelope ---------------------------------------------------

    def _session_of(self) -> tuple[str, _dt.datetime]:
        """The daemon session this server works in, and when it began.

        A daemon's own, from `hello`, so every server and every restart of one over
        the same daemon spends one budget; an owner in this process is a session of
        its own, begun when this toolbox first asked."""
        with self._guard:
            if self._session is not None:
                return self._session
        hello = getattr(self.owner, "hello", None)
        session: tuple[str, _dt.datetime]
        if hello is not None:
            said = hello()
            try:
                began = _dt.datetime.fromisoformat(str(said.started))
            except ValueError:
                began = _dt.datetime.now()
            session = (str(said.session), began)
        else:
            session = (f"local-{os.getpid()}-{id(self.owner):x}", _dt.datetime.now())
        with self._guard:
            if self._session is None:
                self._session = session
            return self._session

    def _ledger(self) -> Ledger:
        """What this daemon session has spent, off the audit log."""
        if self.limits is None:
            return Ledger()
        session, began = self._session_of()
        return Ledger(runs=self.log.acquisitions(session), started=began)

    def _cold_start_mode(self, rendered: Rendered | None) -> str:
        """The limits' rule for this method, or `caution` with none: a real owner with
        no limits never gets this far, and a rehearsal with none refuses nothing."""
        return self.limits.mode_for(rendered) if self.limits is not None else "caution"

    def _outside_limits(self, loaded: Method, rendered: Rendered | None) -> list[str]:
        """What the limits say about this method itself, leaving the budget out."""
        if self.limits is None:
            return []
        real = not self.owner.status().fake  # type: ignore[attr-defined]
        return envelope.check(loaded, rendered, self.limits, Ledger(), real=real)

    def _limits_of(self, template: Template) -> dict | None:
        if self.limits is None:
            return None
        entry = self.limits.entry_for(template.hash)
        if entry is None:
            return {"allowed": False, "problems": []}
        return {
            "allowed": True,
            "knobs": {limit.name: {"min": limit.min, "max": limit.max}
                      for limit in entry.knobs},
            "fixed": dict(entry.fixed),
            "cold_start": entry.cold_start or self.limits.cold_start,
            "problems": entry.problems_against(template),
        }

    def _templates(self) -> list[Template]:
        found = []
        for path in self._library_files():
            if not self._is_template(path):
                continue
            try:
                found.append(template_module.load_template(path))
            except (OSError, TemplateError):
                continue
        return found

    def _read_boxes(self, loaded: Method) -> tuple[BoxState, ...]:
        """Every box the method names, read back with getters only, for the cold-start
        check. Nothing is sent to a box that does not answer."""
        event, handle = self._run(
            ReadState(label="reading the boxes before a send",
                      names=tuple(box.name for box in loaded.boxes)), JOB_WAIT_S)
        states = event.result if isinstance(event, JobFinished) else None
        if not isinstance(states, Mapping):
            raise ToolFailure(
                f"the boxes did not finish reading back within {JOB_WAIT_S:.0f} s (job "
                f"{handle.id}), so nothing was sent; call status to see what is running")
        return tuple(state for state in states.values() if isinstance(state, BoxState))

    def recorder(self, job: int) -> threading.Thread | None:
        """The thread adding job `job`'s files to its run record, if this toolbox
        started the job: what a process that must not exit before the record is whole
        -- a command line's `acquire` -- joins."""
        with self._guard:
            return self._recorders.get(job)

    def _record_seen(self, job: Acquire, runs: list[Run]) -> None:
        """Add files a `progress` call saw to their request's run record, for a job
        another process started and so no thread here is recording: an `acquire` verb
        run with `--no-wait`, followed later. Replaces by stem, so a file seen twice is
        entered once; never raises."""
        if not runs or not job.series:
            return
        try:
            found = find_record(job.directory or self.output, job.series)
            if found is None:
                return
            record = RunRecord(found)
            for run in runs:
                record.add_file(_file_entry(run))
        except Exception:  # noqa: BLE001 -- the record is the run's, never the answer's
            return

    def _record_runs(self, handle: Handle, record: RunRecord) -> None:
        """Add each file an acquisition writes to its request's record, as the owner
        reports it, until the job ends. On a thread of its own; a record it cannot
        write is not a reason to disturb the run."""
        seen = 0
        try:
            while True:
                entries = self._wait(handle, seen, 30.0)
                for entry in entries:
                    seen = entry.seq
                    event = entry.event
                    if isinstance(event, RunDone):
                        record.add_file(_file_entry(event.run))
                    elif isinstance(event, JobFailed):
                        record.append("notes", {"text": f"job {handle.id} failed: "
                                                        f"{event.message}", "by": "server"})
                        return
                    elif isinstance(event, JobFinished):
                        return
        except Exception:  # noqa: BLE001 -- the record is the run's, never its end
            return

    @staticmethod
    def _initials(initials: str) -> str:
        cleaned = clean_initials(initials)
        if not cleaned:
            raise ToolFailure("say whose run this is: give the initials of the person it "
                              "is for, which name the files")
        return cleaned

    def _request(self, words: str, identity: str) -> tuple[str, str]:
        """The request id for these words, and the words: `identity` when one is given,
        else the id these words already have -- in this toolbox, or in this daemon
        session's audit log from another process -- else a new one."""
        words = words.strip()
        with self._guard:
            if identity:
                self._words.setdefault(identity, words)
                self._ids.setdefault(words, identity)
                return identity, words
            known = self._ids.get(words)
        if known is None:
            known = self._logged_request(words)
        with self._guard:
            if known is None:
                known = self._ids.get(words)
            if known is None:
                digest = hashlib.sha256(f"{time.time_ns()}{words}".encode()).hexdigest()[:6]
                known = f"{_dt.datetime.now():%y%m%d-%H%M%S}-{digest}"
            self._ids.setdefault(words, known)
            self._words.setdefault(known, words)
            return known, words

    def _logged_request(self, words: str) -> str | None:
        """An id these words were given earlier in this daemon session by another
        toolbox -- another process's -- off the audit log."""
        try:
            session = self._session_of()[0]
        except DaemonError:
            return None
        return self.log.request_for(words, session)

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

    def _known(self, job: int) -> Handle:
        """The handle for job number `job`: one this toolbox issued, or else one the
        owner still knows, running, queued or with its progress kept -- a job another
        process started, such as an `acquire` verb's that a `progress` verb follows."""
        with self._guard:
            handle = self._handles.get(job)
        if handle is not None:
            return handle
        state = self.owner.status()  # type: ignore[attr-defined]
        found = next((candidate for candidate in (state.running, *state.queued)
                      if candidate is not None and candidate.id == job), None)
        if found is None:
            bare = Handle(id=job, kind="", label="")
            found = next((entry.event.handle for entry in self.owner.events(bare, 0)  # type: ignore[attr-defined]
                          if isinstance(entry.event, (JobStarted, JobFinished, JobFailed))),
                         None)
        if found is None:
            raise ToolFailure(f"job {job} is not one the owner knows: never submitted, or "
                              "finished long enough ago that its progress was let go")
        with self._guard:
            self._handles.setdefault(found.id, found)
        return found

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

    # -- running a routine -------------------------------------------------------

    def _say(self, line: str) -> None:
        if self.narrate is not None:
            try:
                self.narrate(line)
            except Exception:  # noqa: BLE001 -- a watcher that cannot hear is not a failure
                pass

    def _routine(self, name: str) -> Routine:
        """The routine called `name` in the routine directory, or one sentence why not."""
        found = routine_module.scan(self.routines)
        names = []
        for path, loaded in found:
            if isinstance(loaded, RoutineError):
                if os.path.splitext(os.path.basename(path))[0] == name:
                    raise ToolFailure(f"routine {name} does not load: {loaded}")
                continue
            if loaded.name == name:
                return loaded
            names.append(loaded.name)
        if not self.routines:
            raise ToolFailure("no routine directory is set: start the server with --library "
                              "or --routines")
        raise ToolFailure(f"there is no routine called {name!r} in {self.routines}; the "
                          f"routines are {', '.join(names) or 'none'}")

    def _discover_for(self, loaded: Method) -> None:
        """Find the boxes if the owner knows none yet: from the ports on the instrument,
        and under --fake as the stand-ins `loaded` names."""
        if self.owner.status().boxes:  # type: ignore[attr-defined]
            return
        self._say("finding the boxes")
        event, handle = self._run(Discover(label="finding the boxes for a routine",
                                           method=loaded), JOB_WAIT_S)
        if not isinstance(event, JobFinished):
            raise ToolFailure(f"the boxes were not found within {JOB_WAIT_S:.0f} s (job "
                              f"{handle.id}); call status to see what is running")

    def _audit_method(self, entry: routine_module.AuditEntry) -> tuple[Method, str]:
        """An audit's document as the method it declares, and its hash: a method as it
        stands, a template rendered at its defaults (or the entry's knobs), a required
        label it does not give filled with the routine's name, since a label renders
        nothing."""
        path = self._in_library(entry.document)
        try:
            with open(path, "rb") as handle:
                data = tomllib.load(handle)
            if template_module.is_template(data):
                template = template_module.load_template(path)
                labels = {label.name: "stack audit" for label in template.labels
                          if label.required} | dict(entry.labels)
                rendered = template_module.render(template, dict(entry.knobs), labels)
                return rendered.method, rendered.template_hash
            loaded = method_module.load(path)
        except (OSError, tomllib.TOMLDecodeError, MethodError, TemplateError) as exc:
            raise ToolFailure(f"the audit's document {entry.document} does not load: "
                              f"{exc}") from exc
        return loaded, method_module.stamp(loaded)["method_hash"]

    def _run_audit(self, loaded: Routine, who: str, words: str) -> dict:
        documents = [(entry, *self._audit_method(entry)) for entry in loaded.audit]
        self._discover_for(documents[0][1])
        names = tuple(dict.fromkeys(box.name for _, method, _ in documents
                                    for box in method.boxes))
        self._say(f"reading back {', '.join(names)}")
        event, handle = self._run(ReadState(label=f"reading the boxes for routine "
                                                  f"{loaded.name}", names=names), JOB_WAIT_S)
        states = event.result if isinstance(event, JobFinished) else None
        if not isinstance(states, Mapping):
            raise ToolFailure(f"the boxes did not finish reading back within "
                              f"{JOB_WAIT_S:.0f} s (job {handle.id})")
        readings = [state for state in states.values() if isinstance(state, BoxState)]
        audited = {entry.name: {"document": entry.document, "hash": digest[:HASH_DIGITS],
                                "settings": list(entry.settings),
                                **routine_module.audit(method, readings, entry.settings)}
                   for entry, method, digest in documents}
        judged = routine_module.judge(loaded, [audited])
        identity, words = self._request(words, "")
        record = RunRecord.open(self.output, next_stem(self.output, who),
                                request_id=identity, text=words, initials=who)
        return self._routine_report(loaded, judged, record, identity, files=[],
                                    extra={"audit": audited,
                                           "read_back": "\n".join(state.render()
                                                                  for state in readings)})

    def _run_acquiring(self, loaded: Routine, who: str, words: str, conditions: str) -> dict:
        spec = loaded.acquire
        assert spec is not None
        chosen = {"template": spec.template, "knobs": dict(spec.knobs),
                  "labels": dict(spec.labels)}
        try:
            method, _, _ = self._method("", spec.template, spec.knobs, spec.labels)
        except (MethodError, TemplateError) as exc:
            raise ToolFailure(f"routine {loaded.name}'s template does not render: "
                              f"{exc}") from exc
        self._discover_for(method)
        stamped = "; ".join(part for part in (spec.conditions, conditions.strip()) if part)
        plan = (f"routine {loaded.name}: {spec.template} at "
                f"{dict(spec.knobs) or 'its defaults'}, {spec.replicates} file"
                f"{'s' if spec.replicates != 1 else ''}; judged by "
                + "; ".join(f"{item.name} {item.describe()}" for item in loaded.criteria))
        self._say(f"arming {spec.template}")
        try:
            armed = self.call("arm", {"request": words, "initials": who, **chosen,
                                      "conditions": stamped, "plan": plan})
        except ToolFailure as exc:
            return self._unjudged(loaded, who, words, "", f"the arm was refused: {exc}")
        identity = str(armed["request_id"])
        if "stem" not in armed:
            return self._unjudged(loaded, who, words, identity,
                                  f"the send had not finished within {JOB_WAIT_S:.0f} s "
                                  f"(job {armed.get('job')})")
        for caution in armed.get("cold_start") or []:
            self._say(f"caution: {caution}")
        self._say(f"acquiring {spec.replicates} file{'s' if spec.replicates != 1 else ''}")
        try:
            started = self.call("acquire", {"request": words, "initials": who, **chosen,
                                            "replicates": spec.replicates,
                                            "request_id": identity})
        except ToolFailure as exc:
            return self._unjudged(loaded, who, words, identity,
                                  f"the acquisition was refused: {exc}")
        job = int(started["job"])
        ended = self._await(self._known(job), ROUTINE_WAIT_S)
        recorder = self.recorder(job)
        if recorder is not None:
            recorder.join(60.0)
        if ended is None:
            return self._unjudged(loaded, who, words, identity,
                                  f"the acquisition had not finished within "
                                  f"{ROUTINE_WAIT_S / 60:.0f} min; follow job {job} with "
                                  "progress", job=job)
        if isinstance(ended, JobFailed):
            return self._unjudged(loaded, who, words, identity,
                                  f"the acquisition failed: {ended.message}", job=job)
        runs = [run for run in (ended.result or []) if isinstance(run, Run)]
        files = [{"stem": _stem_of(run), "raw_path": run.raw_path,
                  "summed_path": run.summed_path, "complete": run.complete}
                 for run in runs]
        if len(runs) != spec.replicates or not all(run.complete for run in runs):
            return self._unjudged(loaded, who, words, identity,
                                  f"{sum(run.complete for run in runs)} of "
                                  f"{spec.replicates} files were acquired whole",
                                  job=job, files=files)
        self._say("judging the files")
        judged = routine_module.judge(
            loaded, routine_module.measure(loaded, files),
            last=routine_module.last_passing(self.output, loaded.name, before=identity))
        record = RunRecord.open(self.output, files[0]["stem"], request_id=identity,
                                text=words, initials=who)
        return self._routine_report(loaded, judged, record, identity, files=files, job=job)

    def _await(self, handle: Handle, timeout: float) -> JobFinished | JobFailed | None:
        """Wait for a job to end, telling `narrate` each event but the scan counter's."""
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            for entry in self._wait(handle, seen, min(30.0, deadline - time.monotonic())):
                seen = entry.seq
                if isinstance(entry.event, (JobFinished, JobFailed)):
                    return entry.event
                if not isinstance(entry.event, BatchSeen):
                    self._say(entry.event.text)
        return None

    def _unjudged(self, loaded: Routine, who: str, words: str, identity: str, reason: str,
                  *, job: int | None = None, files: list[dict] | None = None) -> dict:
        """The report of a routine that never reached its numbers, in a run record of
        the request's own (begun here if nothing was armed)."""
        if not identity:
            identity, words = self._request(words, "")
        record = RunRecord.open(self.output, next_stem(self.output, who),
                                request_id=identity, text=words, initials=who)
        judged = {"verdict": "could not judge", "reason": reason, "criteria": [],
                  "values": {}}
        return self._routine_report(loaded, judged, record, identity, files=files or [],
                                    job=job)

    def _routine_report(self, loaded: Routine, judged: Mapping[str, Any], record: RunRecord,
                        identity: str, *, files: list[dict], job: int | None = None,
                        extra: Mapping[str, Any] | None = None) -> dict:
        """The report `run_routine` answers, and the same added to the run record."""
        text = routine_module.report_text(loaded, judged)
        entry = {"routine": loaded.name, "hash": loaded.hash, "path": loaded.path,
                 "verdict": judged["verdict"], "reason": judged["reason"],
                 "criteria": judged["criteria"], "values": judged["values"],
                 "files": [item["stem"] for item in files], "job": job, "text": text,
                 **dict(extra or {})}
        record.append("routines", _plain(entry))
        for line in text.splitlines():
            self._say(line)
        return {"routine": loaded.name, "verdict": judged["verdict"],
                "reason": judged["reason"], "text": text, "request_id": identity,
                "record": record.path, "job": job, "files": files,
                "criteria": judged["criteria"], "values": judged["values"],
                "unattended": loaded.unattended, "hash": loaded.hash[:HASH_DIGITS],
                **_plain(dict(extra or {}))}

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


def _same_directory(first: str, second: str) -> bool:
    if not first or not second:
        return False
    return (os.path.normcase(os.path.abspath(first))
            == os.path.normcase(os.path.abspath(second)))


def _cold_start_refusal(refused: list[str]) -> str:
    return ("the boxes hold settings the method does not declare, and the standing "
            "limits refuse a cold start on them: " + "; ".join(refused)
            + ". Nothing was sent. Declare them in the template, or ask the person who "
            "keeps the instrument's limits")


def _template_entry(rendered: Rendered | None, path: str) -> dict | None:
    """A render as its run record keeps it: which template, and every value it took."""
    if rendered is None:
        return None
    return {"path": path, "hash": rendered.template_hash, "knobs": dict(rendered.knobs),
            "labels": dict(rendered.labels)}


def _stem_of(run: Run) -> str:
    """A run's stem, off its raw file's name, or its summed file's."""
    stem = os.path.basename(run.raw_path or run.summed_path or "")
    for suffix in (SUMMED_SUFFIX, RAW_SUFFIX):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _file_entry(run: Run) -> dict:
    """One finished run as its request's record keeps it, with its file's summary: the
    summed file's, or the raw file's for a run that never folded and so wrote none."""
    path = next((candidate for candidate in (run.summed_path, run.raw_path)
                 if candidate and os.path.isfile(candidate)), "")
    try:
        numbers: dict[str, Any] = brief(summary.summarize(path)) if path else {}
    except Exception as exc:  # noqa: BLE001 -- the record says why, never raises
        numbers = {"problem": sentence(exc)}
    return {"stem": _stem_of(run), "raw": run.raw_path, "summed": run.summed_path,
            "complete": run.complete, "stopped_early": run.stopped_early,
            "replicate": run.replicate, "seconds": round(run.seconds, 2),
            "summary": numbers}


def _records(directory: str) -> dict[str, str]:
    """Every run record in `directory`, by the request id it records."""
    found: dict[str, str] = {}
    for path in glob.glob(os.path.join(glob.escape(directory), "*" + RECORD_SUFFIX)):
        try:
            with open(path, encoding="utf-8") as handle:
                identity = json.load(handle).get("request", {}).get("id")
        except (OSError, ValueError, AttributeError):
            continue
        if identity:
            found[str(identity)] = os.path.basename(path)
    return found


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

