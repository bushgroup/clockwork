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
it with the request, the plan and the arming, `acquire` adds the acquisition, the owner
adds each file with its summary as it writes it (`clockwork.owner.keeper`, lab record,
task 164), and `note` adds the session's notes. Its `source` is this toolbox's route,
`mcp` or `cli` (`Toolbox.route`).

**A request is the unit of work.** Every `arm` and `acquire` carries the words of the
person it is for; the first use of a set of words mints a request id, which every run
of the request stamps as its series (`ClockworkSeriesId`, with its place in the request
as `ClockworkSeriesIndex`), and the words go into each run's log header and the audit
log. Passing `request_id` back continues a request across calls and sessions.

**A planned series is one job, not a loop of calls.** `series` checks a whole plan
(`clockwork.series`) against the ranges, the limits and the boxes before
anything is sent, shuffles it under a recorded seed, and submits a `SeriesJob`, which the
owner arms and acquires point by point: a session that ends mid-series does not end the
series, and every file stamps its planned index, its executed position and the seed
(lab record, task 91).

Qt-free, under the seam with the three lower layers.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import glob
import inspect
import os
import threading
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .. import (
    INSTRUMENT_ENV,
    INSTRUMENT_FILE,
    envelope,
    library_dirs,
    library_setting,
    planning,
    summary,
)
from .. import manifest as manifest_module
from .. import method as method_module
from .. import routine as routine_module
from .. import series as series_module
from ..acq import (
    BatchSeen,
    BoxSaid,
    Event,
    Folding,
    FrameBegun,
    FrameEnded,
    GateChecked,
    PhaseSent,
    PusherPeriodChecked,
    Run,
    Snapshot,
    Warned,
    cautions,
    refusals,
)
from ..acq.standing import RampStepped, StandDownDone
from ..acq.uimf import RAW_SUFFIX, SUMMED_SUFFIX
from ..app import methodlib
from ..dayrecord import DayRecord
from ..envelope import Limits
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
    LogsOpened,
    PointStarted,
    Progress,
    ReadState,
    RunDone,
    Send,
    SendResult,
    SeriesJob,
    SeriesResult,
    SnapshotBoxes,
    SnapshotResult,
    StaleHandle,
    StandDown,
    StandingResult,
    WarmUp,
    matches_wire,
    sentence,
    wire_fingerprint,
)
from ..owner.hold import Guarding, Hold, held
from ..owner.interface import JobSummary, summarize
from ..owner.keeper import stem_of
from ..owner.remote import DaemonError
from ..owner.wire import TYPE_KEY, to_wire
from ..record import (
    RECORD_SUFFIX,
    VERDICTS,
    RunRecord,
    brief,
    find_for_stem,
    mint_id,
    newest_verdict,
)
from ..record import find as find_record
from ..record import load as load_record
from ..routine import Routine, RoutineError
from ..transcript import send_log_name
from .audit import LOG_NAME, AuditLog, SealFailure
from .guard import guard_acquisition

__all__ = ["TOOLS", "Tool", "ToolFailure", "Toolbox", "tool"]

JOB_WAIT_S = 120.0

UNREQUESTED = "verdict on a file acquired without a request"
"""The request text of a record `verdict` begins for a file no record holds: one the
window acquired, which writes none (lab record, task 90)."""
"""How long `discover_boxes`, `read_box_state` and `arm` wait for their job before
answering that it is still running. A send of three boxes' tables takes seconds; a job
queued behind an acquisition waits for the acquisition."""

PROGRESS_WAIT_S = 10.0
PROGRESS_WAIT_MAX_S = 30.0
"""`progress` waits up to its `wait_s` for something new, never past this (Matt,
2026-09-23): a session following a run makes one call every several seconds rather
than spinning, and still hears about a finished run within one call."""
ROUTINE = (BatchSeen, BoxSaid, PhaseSent, FrameBegun, FrameEnded, GateChecked,
           LogsOpened, PusherPeriodChecked, Folding)
"""The events `progress` waits through rather than answer on: a repetition's own, and
the bookkeeping each file does at its start and end beside the file's own `RunBegun`
and `Folded`.

A `per_repetition` run makes several every repetition, and a follower answered on each
asked the serve several times a second and wrote a line in the audit log each time (lab
record, task 173). The list is what a progress call may hold back; anything not on it,
a new kind of event included, answers at once."""

ROUTINE_WAIT_S = 3600.0
"""How long `run_routine` waits for its acquisition before judging it could not judge.
A routine's run is seconds to minutes; an hour is a run that is stuck."""

HASH_DIGITS = methodlib.HASH_DIGITS

STANDING_SETTINGS = ("dc_bias", "rf", "arb")
"""What `warm_up` compares its read-back with: every family the standing method's
`setup` can declare, as `clockwork.routine.audit` compares them."""

STARTUP_KINDS = ("Discover", "StartConsole", "RestartConsole")
"""The jobs a daemon queues for itself, which `warm_up` and `stand_down` wait out."""

BUSY_KINDS = ("Send", "Acquire", "SeriesJob", "WarmUp", "StandDown")
"""The jobs `warm_up` and `stand_down` refuse to overlap."""

STOP_WAIT_S = 120.0
"""How long `stand_down` with `stop` waits for the run it stopped: a repetition and its
fold are seconds, and a series ends after its current repetition too."""

AT_REST = 0.005
"""Within this of zero a DC bias setpoint, RF drive or ARB range counts as zero: each is
read back to two decimals."""

REFUSED_WHILE_PAUSED = ("discover_boxes", "read_box_state", "arm", "acquire", "series",
                        "run_routine", "warm_up", "stand_down", "snapshot_boxes", "stop")
"""The tools refused, at once and with one sentence, while a person at the window has
paused the series running or asked for it to pause (`clockwork.owner.hold.held`): every
tool that submits a job, and the two that end a run. What is left reads and notes (the
window revision's B7; lab record, task 167). `series` with `check` true submits nothing
and is answered. **No tool pauses or resumes, and none is to be added**: the daemon's
`pause` and `resume` are sent by the window alone, so an agent's part in a pause is to
report it and wait to be told (B6). `Toolbox._submit` refuses the same way, so a tool
written later cannot queue a job behind a pause by being left off this list."""

SEALS = ("seal_calls", "stand_down")
"""The tools that end the audit log's live segment once their own line is in it, and
answer the segment under `sealed` (lab record, task 161); and the day record's, under
`sealed_day` (lab record, task 165)."""


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
    destructive: bool = False
    """True for a tool that ends something already under way: a run, or the daemon."""

    @property
    def description(self) -> str:
        return inspect.cleandoc(self.function.__doc__ or "")

    @property
    def signature(self) -> inspect.Signature:
        """The arguments, as the function declares them, without `self`."""
        full = inspect.signature(self.function, eval_str=True)
        return full.replace(parameters=list(full.parameters.values())[1:])


TOOLS: list[Tool] = []


def tool(group: str, *, read_only: bool = True,
         destructive: bool = False) -> Callable[[Callable], Callable]:
    """Register a `Toolbox` method as a tool, in the order the methods are written."""
    def register(function: Callable) -> Callable:
        TOOLS.append(Tool(name=function.__name__, group=group, function=function,
                          read_only=read_only, destructive=destructive))
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
        self.libraries = library_dirs(library)
        """The library's directories in the order searched: the instrument's first, then
        any a project adds (`clockwork.library_dirs`)."""
        self.library = library_setting(self.libraries)
        self.routines = (os.path.abspath(routines) if routines
                         else routine_module.default_directory(
                             self.libraries[0] if self.libraries else ""))
        """Where the instrument's routines are: `routines` beside the library's first
        directory unless named."""
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
        """The daemon session's id and start, asked for once."""
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
        # Every `series` line, a check's and a refusal's too, carries the hash of the
        # plan's text however it was passed: the argument alone is the text hashed as
        # given, or a path whose file can change (the window revision's A5).
        extra = ({"plan_sha256": planning.plan_sha256(arguments.get("plan"))}
                 if name == "series" else None)
        try:
            self.log.session = self._session_of()[0]
        except DaemonError:
            pass  # the call below meets the same daemon, and says so in its own words
        try:
            try:
                found.signature.bind(**arguments)
            except TypeError as exc:
                raise ToolFailure(f"{name} was given the wrong arguments: {exc}") from exc
            if name in REFUSED_WHILE_PAUSED and not (name == "series"
                                                     and arguments.get("check")):
                # Before the tool does anything, a record begun included: a person's
                # pause leaves this route reading and noting only.
                self._refuse_if_paused()
            result = found.function(self, **arguments)
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
                           request=self._request_for_log(arguments, result), extra=extra)
        if name in SEALS:
            # After the line: the segment ends with the call that ended it.
            try:
                result["sealed"] = self.log.seal()
            except SealFailure as exc:
                if name == "seal_calls":
                    raise ToolFailure(str(exc)) from exc
                result["sealed"] = {"error": str(exc)}
            # The day record the daemon keeps beside it is sealed with it, so a folder
            # sealed and then listed holds neither live file (lab record, task 165).
            try:
                result["sealed_day"] = DayRecord.beside(self.log_directory).seal()
            except OSError as exc:
                result["sealed_day"] = {"error": f"the day record could not be sealed: "
                                                 f"{exc.strerror or exc}"}
        return result

    @property
    def log_directory(self) -> str:
        """The folder the audit log is in, and the day record beside it."""
        return os.path.dirname(self.log.path) if self.log.path else ""

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

        `library` is the directory each template was found in: the library can be
        several, the instrument's and a project's own, and `path` is relative to it. A
        path found in two of them is listed from both with `duplicate` naming every
        copy, and every tool refuses it until one copy is renamed or removed.
        """
        templates = []
        duplicates = self._duplicates()
        for library, path in self._library_files():
            relative = os.path.relpath(path, library).replace(os.sep, "/")
            where = {"path": relative, "library": library,
                     **({"duplicate": duplicates[relative]} if relative in duplicates
                        else {})}
            try:
                with open(path, "rb") as handle:
                    if not template_module.is_template(tomllib.load(handle)):
                        continue
                loaded = template_module.load_template(path)
            except (OSError, tomllib.TOMLDecodeError, TemplateError) as exc:
                templates.append({**where, "problem": str(exc)})
                continue
            constants = dict(loaded.constants)
            templates.append({
                **where,
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
        """Every method in the library: name, hash, date and description.

        Templates are listed by `list_templates` instead. A method written by
        `render_template` with `to` says which template and knob values it came from, in
        `rendered`. A document that does not load is listed with the problem that stopped
        it. `library` is the directory each was found in, and `path` is relative to it.
        A path found in two library directories is listed from both with `duplicate`
        naming every copy, as `list_templates` lists one, and refused the same way.
        """
        methods = []
        duplicates = self._duplicates()
        for entry in methodlib.scan_library(self.library):
            if self._is_template(entry.path):
                continue
            relative = os.path.relpath(entry.path, entry.library).replace(os.sep, "/")
            methods.append({
                "path": relative,
                "library": entry.library,
                **({"duplicate": duplicates[relative]} if relative in duplicates else {}),
                "name": entry.name,
                "hash": entry.hash,
                "created": entry.created.isoformat() if entry.created else None,
                "description": entry.description,
                **({"rendered": entry.rendered} if entry.rendered else {}),
                **({"problem": entry.problem} if entry.problem else {}),
            })
        return {"library": self.library, "methods": methods}

    @tool("method")
    def load_method(self, method: str) -> dict:
        """One method document: its canonical text and the same as structured fields.

        `method` is a path in the library, or an absolute path.
        """
        path = self._document(method, "method")
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
        first = method_module.load(self._document(a, "method"))
        second = method_module.load(self._document(b, "method"))
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

    @tool("method", read_only=False)
    def render_template(self, template: str, knobs: dict[str, int | float] | None = None,
                        labels: dict[str, str] | None = None, to: str = "",
                        overwrite: bool = False) -> dict:
        """A template rendered at some knob values: the method that would be sent.

        Knobs not given take their defaults. Answers every knob's value, the derived
        values, the marks (each in ms and as the expected scan), the rendered method's
        text and hash, and its refusals and cautions. A knob outside its range, an
        unknown knob or a missing required label comes back in `problems`, with `ok`
        false. A knob inside the template's range and outside the instrument's standing
        limits is rendered, not clamped, and comes back in `limits`, with `ok` false.
        `warnings` are repairs made while loading; they never stop a run.

        `to` also writes the rendered method to a file, a path in the library or an
        absolute path, for a person to open in the window or put in its run queue. The
        file carries the template, knobs and labels, so its runs stamp as rendered while
        it is unedited. An existing file is not replaced unless `overwrite` is true.
        Answers where it wrote as `written`. Nothing is written where `problems` is not
        empty; a render with refusals or outside the limits is still written, and says so.
        """
        try:
            loaded, rendered, _ = self._method("", template, knobs, labels)
        except TemplateError as exc:
            return {"ok": False, "problems": list(exc.problems)}
        assert rendered is not None
        found = refusals(loaded)
        outside = self._outside_limits(loaded, rendered)
        written = self._write_rendered(rendered, to, overwrite) if to else ""
        return {
            **({"written": written} if written else {}),
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

        Refused, before anything is sent, outside the instrument's standing limits and
        the project's: a template they do not list, a knob outside their range or the
        standing caps, a box they do not allow, two electrodes further apart or an RF
        head driven harder than they allow, or on the instrument a hand-written method
        where they do not bound the electrodes and the RF. Then the
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
                                initials=who, source=self.route)
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
        the limits; the cold-start comparison is repeated against what
        the boxes read back when `arm` sent the method, including any declared value a
        box does not hold, and its cautions come back under `cold_start`.
        """
        if replicates < 1:
            raise ToolFailure("replicates counts files, from 1")
        loaded, rendered, path = self._method(method, template, knobs, labels)
        self._refuse_unless_allowed(loaded, rendered, request)
        who = self._initials(initials)
        armed = self.owner.status().armed  # type: ignore[attr-defined]
        snapshot = self.owner.snapshot()  # type: ignore[attr-defined]
        if armed is None or snapshot is None or not matches_wire(armed.fingerprint, loaded):
            raise ToolFailure(
                "the boxes are not holding this method: arm it first, with the same "
                "method or template and knob values")
        states = snapshot.after or snapshot.before
        conditions = snapshot.conditions
        # Not a stem a run has already begun under, even one that failed before its file
        # existed: the owner would refuse it (lab #3).
        armed_stem = (armed.stem if _same_directory(armed.directory, self.output)
                      and not armed.used else "")
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
        # Opened before the job is queued: the owner enters each file in it as the file
        # is written, which may be before `submit` has returned here.
        record = RunRecord.open(self.output, stem or next_stem(self.output, who),
                                request_id=identity, text=words, initials=who,
                                source=self.route)
        handle = self._submit_recorded(job, record)
        with self._guard:
            self._planned[handle.id] = replicates
        record.append("acquisitions", {
            "job": handle.id, "replicates": replicates, "first_place": place,
            "method": loaded.metadata.name,
            "template": _template_entry(rendered, path), "cold_start": cautioned})
        return {"job": handle.id, "request_id": identity, "first_place": place,
                "replicates": replicates, "label": handle.label,
                "cold_start": cautioned, "record": record.path,
                "follow": f"call progress with job={handle.id}"}

    @tool("acquisition", read_only=False)
    def series(self, request: str, initials: str, plan: str, conditions: str = "",
               setup: bool = True, request_id: str = "", check: bool = False) -> dict:
        """Acquire a whole planned series as one job, in shuffled order, and answer at once.

        `plan` is a series plan's TOML text, or the path of a file holding one: a
        template, its labels, then a `[grid]` of values per knob or a list of
        `[[points]]` whose knobs move together, `replicates` per point, `references` at
        the template's defaults (`["start", "end"]` puts one before and one after), an
        optional `seed`, and `shuffle`, true by default (docs/series-file-format.md).
        A template with a `repetitions` knob takes the repetitions per file like any
        knob, for the whole plan in `[grid]` or point by point. `request`, `initials`,
        `conditions` and `request_id` are as for `arm`; `setup` is the first point's,
        and each later point sends its setup only when it differs. No `arm` is needed
        first: the series sends every point itself, and a point the boxes already hold
        from the point before is acquired without a send.

        Refused whole, before anything is sent, for a point outside the template's
        ranges or the limits, naming the point; or for a cold-start refusal on any
        point, after one read-back of the boxes. The
        points between the references are shuffled under the seed, drawn when the plan
        gives none; every file stamps the request id, the point's planned index, its
        own position in the order acquired and the seed. Answers the job number, the
        seed, the order, the counts, about how many minutes the series will acquire for
        (every repetition's frame and the console's re-arm after it) and the run record;
        follow it with `progress` and end it with `stop`, which leaves the finished
        files and the record saying where it stopped. A point whose run stops early on
        its own (frames failing in a row, a gate that never closed) ends the series the
        same way, and the remaining points, an end reference among them, are not
        acquired: the series finishes with `complete: false` in `progress`, never as done.

        `check` true does everything up to the read-back and sends nothing, starts no
        job and writes no record: it answers each point's knobs, repetitions, files and
        minutes in planned order, and the totals, or the refusal the series would meet.
        Use it to show a person what a plan costs before asking to run it.
        """
        who = self._initials(initials)
        try:
            planned, plan_path = series_module.resolve(plan)
            template_path = self._in_library(planned.template)
            template = template_module.load_template(template_path)
            points = series_module.expand(planned, template)
        except (series_module.PlanError, TemplateError) as exc:
            raise ToolFailure(f"the plan does not load, so nothing was sent: {exc}") from exc
        except OSError as exc:
            raise ToolFailure(f"the plan's template {planned.template} does not open: "
                              f"{exc.strerror or exc}") from exc
        self._refuse_unless_allowed(None, None, request)
        # The plan's path to its job is `clockwork.planning`'s, which the window takes
        # too; the limits' verdict comes back as data, and here it refuses.
        judged = planning.check(
            planned, template, template_path=template_path, path=plan_path,
            limits=self.limits, real=not self.owner.status().fake,  # type: ignore[attr-defined]
            pusher_period_us=self.instrument.pusher_period_us)
        refused = judged.agent_refusals()
        if refused:
            raise ToolFailure("the series was refused whole and nothing was sent: "
                              + "; ".join(refused))
        spent = judged.acquisitions
        minutes = judged.minutes
        if check:
            return {"sent": False, "template": template_path,
                    "template_hash": template.hash[:HASH_DIGITS],
                    "points": judged.entries(), "acquisitions": spent,
                    "files": judged.files, "minutes": minutes}
        renders = [point.rendered for point in judged.points if point.rendered is not None]
        found = self._read_boxes(renders[0].method)
        mode = self._cold_start_mode(renders[0])
        cautioned: list[str] = []
        distinct: list[tuple] = []
        for point, rendered in zip(points, renders, strict=True):
            fingerprint = wire_fingerprint(rendered.method)
            if fingerprint in distinct:
                continue
            distinct.append(fingerprint)
            stopped, noted = envelope.judge(
                envelope.cold_start(rendered.method, found), mode)
            if stopped:
                raise ToolFailure(f"{series_module.describe(point)}: "
                                  + _cold_start_refusal(stopped))
            cautioned += [line for line in noted if line not in cautioned]
        identity, words = self._request(request, request_id)
        count = judged.files
        with self._guard:
            place = self._places.get(identity) or self._next_place_on_disk(identity)
            self._places[identity] = place + count
        seed = (planned.seed if planned.seed is not None else series_module.draw_seed()
                ) if planned.shuffle else None
        job = judged.job(
            seed=seed, first_position=place, label=f"acquiring series {identity}",
            series=identity, setup=setup, conditions=conditions, directory=self.output,
            initials=who, request=words, instrument=self.instrument,
            instrument_path=self.instrument_path, cold_start=mode)
        record = RunRecord.open(self.output, next_stem(self.output, who),
                                request_id=identity, text=words, initials=who,
                                source=self.route)
        handle = self._submit_recorded(job, record)
        with self._guard:
            self._planned[handle.id] = count
        entry = judged.plans_entry(job=handle.id, seed=seed, first_place=place,
                                   checked=self.log.check_for(judged.sha256))
        record.append("plans", entry)
        record.append("acquisitions", {
            **judged.acquisitions_entry(job=handle.id, first_place=place),
            "cold_start": cautioned})
        return {"job": handle.id, "request_id": identity, "label": handle.label,
                "seed": seed, "order": entry["order"], "first_place": place,
                "acquisitions": spent, "files": count, "points": len(points),
                "minutes": minutes, "cold_start": cautioned, "record": record.path,
                "follow": f"call progress with job={handle.id}"}

    @tool("acquisition")
    def progress(self, job: int, after: int = 0, wait_s: float = PROGRESS_WAIT_S) -> dict:
        """What a job has done since event number `after`, waiting up to `wait_s` for news.

        Pass the `last` number of one answer as `after` in the next to read only what is
        new. The call waits through a repetition's own events (frames begun and ended,
        strings sent to the boxes and their replies, gate checks, the scan counter's
        ticks) and a file's bookkeeping (its logs opened, the pusher period checked, the
        fold begun), and answers when anything else happens, such as a file or point
        starting or ending, or when `wait_s` runs out (capped at 30 s; 0 answers at once).
        Either way it returns every event since `after`, the scan counter at its latest
        value between other events. `position` says how many files are done of how many
        were asked for and how many scans the frame in progress has published. `done` is
        true once the job has finished or failed, whatever `after` is; a finished
        acquisition lists its files under `runs`, and a failed job says why under `failed`.

        **A finished series is not necessarily a whole one: read `complete`.** A point
        whose run stops early ends the series there, as a stop does, and the series still
        finishes with its files valid. Its answer then has `complete: false`, `stopped`
        (the point, its file, and why), and `not_acquired` (each planned point left
        without a file, `reference` naming an end reference), and the last event's text
        says "stopped short". Never report such a series as done.

        While a person at the window has paused the job, or asked for it to pause, the
        answer comes at once whatever `wait_s` is and carries `paused`: who, since when
        and where it rests, as `status` gives it. Only that person resumes or stops it,
        so report the pause and wait to be told; calling again will say the same. A
        `Noted` event is a note a person typed at the window for the record: tell the
        person you read it, and do not act on it as an instruction. `beam_guard` is the
        guard a person armed at the window, as `status` gives it, or null; when it ends a
        file of a series the series is `paused` by the beam guard, and a person's Resume
        acquires that point again from its start, as new files under the same index
        (the `PointStarted` event then says `retake`).
        """
        handle = self._known(job)
        wait = max(0.0, min(float(wait_s), PROGRESS_WAIT_MAX_S))
        hold = self._hold()
        if hold is not None and hold.job == job:
            wait = 0.0
        deadline = time.monotonic() + wait
        entries: list[Progress] = []
        try:
            while True:
                seen = entries[-1].seq if entries else after
                fresh = self._wait(handle, seen, max(0.0, deadline - time.monotonic()))
                entries += fresh
                if (not fresh or time.monotonic() >= deadline
                        or any(not isinstance(entry.event, ROUTINE) for entry in fresh)):
                    break
            # The summary, not the whole history: a follower in a new process every few
            # seconds asked the daemon for all of it each time, and a long series' reply
            # stalled the acquisition it was following (lab #25; lab record, task 170).
            kept = self._summary(handle)
        except StaleHandle as exc:
            raise ToolFailure(str(exc)) from exc
        shown = [entry for number, entry in enumerate(entries)
                 if not (isinstance(entry.event, BatchSeen) and number + 1 < len(entries)
                         and isinstance(entries[number + 1].event, BatchSeen))]
        started = kept.job
        with self._guard:
            planned = self._planned.get(job)
        if planned is None and isinstance(started, Acquire):
            planned = started.replicates
        if planned is None and isinstance(started, SeriesJob):
            planned = sum(point.replicates for point in started.points)
        answer: dict[str, Any] = {
            "job": job, "label": handle.label,
            "events": [_entry(entry) for entry in shown],
            "last": entries[-1].seq if entries else after,
            "position": {
                "files_done": kept.runs_done,
                "files_asked": planned,
                "scans_this_frame": kept.scans_so_far,
            },
            "done": False,
        }
        if isinstance(started, SeriesJob):
            answer["series"] = _series_progress(started, kept.point)
        # One `status` for the pause and the guard: each is a round trip to the serve.
        now = self.owner.status()  # type: ignore[attr-defined]
        hold = getattr(now, "hold", None)
        if hold is not None and hold.job == job:
            answer["paused"] = _paused(hold)
        answer["beam_guard"] = _guarded(getattr(now, "guard", None))
        # Off the summary, not the events after `after`: a job asked about past its end
        # -- `after` its last number, or any number larger -- answered `done: false` for
        # ever, with nothing running (on the rack, 2026-09-28; lab record, task 96).
        if isinstance(kept.ended, JobFinished):
            answer["done"] = True
            answer["result"] = _result(kept.ended.result)
            ended = kept.ended.result
            if isinstance(ended, SeriesResult):
                # At the top, beside `done`: an agent read `done` and the end's line
                # and wrote a series that stopped short into its record as done (lab #26).
                answer["complete"] = ended.complete
                answer["not_acquired"] = _not_acquired(ended)
                if not ended.complete:
                    answer["stopped"] = ended.stopped
        elif isinstance(kept.ended, JobFailed):
            answer["done"] = True
            answer["failed"] = kept.ended.message
        if isinstance(answer.get("result"), list):
            answer["runs"] = answer.pop("result")
        elif isinstance(answer.get("result"), dict) and "runs" in answer["result"]:
            answer["runs"] = answer["result"].pop("runs")
        return answer

    @tool("acquisition", read_only=False, destructive=True)
    def stop(self, reason: str = "stopped at the request of the person it was for") -> dict:
        """Stop the acquisition in flight after its current repetition and its fold.

        What it leaves on disk is a short experiment, not a broken one; replicates not
        yet started are not acquired, and nor are the points of a series not yet begun.
        `reason` goes in the file's log, and the run record says who stopped it and, for
        a series, where it stopped. Refused while a person at the window has paused the
        run: a paused run is theirs to resume or stop.
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
        the limits in force, and how many acquisitions this daemon session has made.
        The count is the daemon session's, not this server's: `acquisitions_by` says
        how many each surface made, `cli` being the command line's. Under `limits`,
        `project` is the project's limits combined into the standing ones, and
        `project_templates` the entries they added or narrowed; `hand_written` is
        whether a hand-written method may be sent, which it may where the limits bound
        the electrodes and the RF; `problems` are entries that cannot be applied to the
        template they name in this library, each a mistake to put right; `not_served`
        are entries whose template this library does not hold, which is ordinary where
        the standing limits name several projects' templates and one project's are
        served. `paused` is null unless a person at the window has paused the series
        running or asked for it to pause: then it says the job, the grain (`point` or
        `repetition`), who asked and through which client, when, and since when and
        where the series rests (`since` null while it is still on its way there). While
        it is not null, every tool that would submit a job, `stop` and `stand_down` are
        refused, and nothing here resumes: report it and wait to be told. A pause whose
        `by` is `the beam guard` is the guard's: it ended a file of the series because
        the beam was lost, and a person's Resume acquires that point again. `beam_guard`
        is null unless a person at the window has armed it: then its floor in ions a
        repetition, how many repetitions in a row under it end a file, where the floor
        came from, and who armed it when. It watches every acquisition, yours included;
        no tool arms, changes or disarms it."""
        state = self.owner.status()  # type: ignore[attr-defined]
        armed = ({"method": state.armed.method, "stem": state.armed.stem}
                 if state.armed is not None else None)
        refused = guard_acquisition(self.owner, None, "status", limits=self.limits)
        limits = self.limits
        served = self._templates() if limits is not None else []
        session = self._session_of()[0]
        return {
            "limits": None if limits is None else {
                "path": limits.path, "description": limits.description,
                "project": limits.project or None,
                "project_description": limits.project_description or None,
                "cold_start": limits.cold_start, "boxes": list(limits.boxes),
                "hand_written": limits.hand_written,
                "caps": {cap.name: {"min": cap.min, "max": cap.max} for cap in limits.caps},
                "templates": [entry.key for entry in limits.templates],
                "project_templates": [entry.key for entry in limits.templates
                                      if entry.origin == "project"],
                "problems": limits.against(served, unmatched=False),
                "not_served": limits.unmatched(served),
            },
            "session": {
                "daemon_session": session,
                "acquisitions_by": self.log.acquisitions_by(session),
            },
            "fake": state.fake,
            "program": state.program,
            "console": state.console.text,
            "boxes": list(state.boxes),
            "running": _handle(state.running),
            "queued": [_handle(handle) for handle in state.queued],
            "stopping": state.stopping,
            "paused": _paused(getattr(state, "hold", None)),
            "beam_guard": _guarded(getattr(state, "guard", None)),
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
        chose, what you told the person, your reading of a file, why you stopped; a
        person's own verdict on a run goes to `verdict`. One note per call; `request_id`
        is the one `arm` answered. Answers the record's path and how many notes it holds.
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

    @tool("acquisition", read_only=False)
    def verdict(self, path: str, verdict: str, initials: str, words: str = "") -> dict:
        """Record a person's verdict on one run, in the run record beside its files.

        The person's judgement, not the machine's: a run that completed with nothing in
        it is `completed` and its verdict is `no_signal`. `verdict` is one of `worked`,
        `no_signal`, `saturated`, `wrong_sample` or `other`; `words` are the person's
        own, required with `other`. `initials` are the person's, who gave it, never the
        session's. `path` is either file of the run, raw or summed, or its stem, in the
        output directory or absolute. A second verdict on a run supersedes the first,
        which is kept. A file no record holds, such as one the window acquired, gets a
        record begun for it. Answers the record's path, the entry, and how many
        verdicts the run has had.
        """
        who = clean_initials(initials)
        if not who:
            raise ToolFailure("say whose verdict this is: give the initials of the person "
                              "who gave it")
        if verdict not in VERDICTS:
            raise ToolFailure(f"{verdict!r} is not a verdict; say one of "
                              f"{', '.join(VERDICTS)}, and anything more in words")
        if verdict == "other" and not words.strip():
            raise ToolFailure("a verdict of other says what it is: pass its words")
        directory, stem, files = _run_files(self._in_output(path))
        if not files:
            raise ToolFailure(f"no UIMF file of {stem} in {directory}")
        stamped = _series(files[0])
        identity = stamped["id"] if stamped is not None else None
        found = find_for_stem(directory, stem, identity)
        begun = found is None
        if begun:
            words_asked = self.log.requests().get(identity, "") if identity else ""
            record = RunRecord.open(directory, stem, request_id=identity or f"verdict-{stem}",
                                    text=words_asked or UNREQUESTED,
                                    source=self.route if identity else "window",
                                    initials=who)
            record.add_file(_found_file(stem, files))
        else:
            record = RunRecord(found)
        try:
            count = record.add_verdict(stem, verdict, who, words)
        except ValueError as exc:
            # Its request's record, still being written: the file's entry comes as the
            # owner reports the run.
            raise ToolFailure(f"{exc}; a verdict waits until the run's file is in its "
                              "record") from exc
        return {"record": record.path, "begun": begun, "stem": stem,
                "verdict": newest_verdict(record.read(), stem), "verdicts_on_stem": count}

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
        directory, stem, _ = _run_files(self._in_output(path))
        kept = find_for_stem(directory, stem, str(identity) if identity else None)
        try:
            found["verdict"] = None if kept is None else newest_verdict(
                RunRecord(kept).read(), stem)
        except (OSError, ValueError):
            found["verdict"] = None
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

    @tool("data", read_only=False)
    def seal_calls(self) -> dict:
        """End the audit log's live file, so a folder listed now is never changed.

        This call's own line is written last, then `mcp-calls.log` in the output
        directory is renamed `mcp-calls.<YYYYMMDD-HHMMSS>.log` and never written again;
        the next call starts a new live file. Every reader of the log reads all its
        segments, so nothing is lost to `status`, `list_files` or a request's words.
        Seal before listing or archiving a day's folder. Answers the segment under
        `sealed`: its name, size in bytes and MD5. The daemon's day record beside it,
        `day-record.jsonl`, is sealed the same way and answered under `sealed_day`.
        Needs no daemon.
        """
        if not self.log.path:
            raise ToolFailure("this server keeps no audit log, so there is nothing to seal")
        return {"directory": os.path.dirname(self.log.path), "log": LOG_NAME}

    @tool("data", read_only=False)
    def manifest(self, paths: list[str] | None = None, out: str = "") -> dict:
        """One row per run under some directories: what each file says it was, as a table.

        `paths` are directories, walked recursively, or UIMF files, in the output
        directory or absolute; the output directory itself if none. Each run is one row,
        its summed file where one exists and its raw file otherwise: file, kind, day,
        stem, initials, template and method hash, method name, sample, conditions, outcome with
        planned and acquired repetitions, series id, index, position and seed, the
        pusher period declared and measured and their ratio, the verdict and note count
        off the run record, the record's name, and `problem` for a file that would not
        open, which is a row and never dropped. Then one column per knob, mark
        (`<mark>_ms`, `<mark>_scan`) and label found across the set, empty where a
        file lacks it: a hand-written run has empty knob columns, not zeros. With `out`,
        a `.csv` path, the table is written there and the answer is its path and
        counts; without it the answer carries `columns` and `rows`.
        """
        where = [self._in_output(path) for path in paths] if paths else [self.output]
        if out and not out.lower().endswith(".csv"):
            raise ToolFailure(f"{out!r} is not a .csv path; the manifest is written as CSV")
        found = manifest_module.manifest(where)
        if not out:
            return {"paths": where, **found}
        written = manifest_module.write_csv(self._in_output(out), found["columns"],
                                            found["rows"])
        return {"paths": where, "out": written, "columns": found["columns"],
                "files": found["files"], "problems": found["problems"]}

    # -- routines ----------------------------------------------------------------

    @tool("routine")
    def list_routines(self) -> dict:
        """The instrument's routines: experiments run with nothing left to choose.

        Each routine is a template at fixed knob values, or a read-back of the boxes
        against documents, with the criteria that judge it. For each: its name, what it
        asks, whether it may run with nobody at the instrument (`unattended`), what it
        acquires or reads, each criterion in words with its source, and whether the
        standing limits allow its template. Run one with `run_routine`. Where the
        routine directory is not set or not there, `problem` says where it was looked
        for and how to name it.
        """
        routines = []
        missing = self._no_routines()
        if missing:
            return {"directory": self.routines, "routines": [], "problem": missing}
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
        limits and the cold-start check apply to it exactly, and it
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

    # -- the two ends of the day -------------------------------------------------

    @tool("hardware", read_only=False)
    def warm_up(self) -> dict:
        """Put the instrument's standing stack on the boxes in the morning, ramped.

        Sends the `setup` phase of the method the instrument document names under
        `[standing]`, and nothing after it: no table is loaded and no box is armed.
        Every DC bias channel, RF drive and ARB range that method declares is ramped
        from what the box reads back, in the document's `steps` equal steps `dwell_s`
        apart. The boxes are then read back and compared with the declaration as the
        stack audit compares them: `ok` is true when nothing differs, and `differences`
        lists what does. Refused while an acquisition, a send, a warm-up or a
        stand-down is running or queued. The daemon is left running; an `arm` is still
        needed before `acquire`. From the command line, `clockwork warm-up` starts a
        daemon first when none is running.
        """
        standing = self.instrument.standing
        if not standing.method:
            raise ToolFailure(
                "the instrument document names no standing method, so there is nothing "
                "to warm up to: add a [standing] table with method = the library path of "
                "the method whose setup is the instrument's stack")
        self._settle()
        self._refuse_if_busy("warm up")
        path = self._in_library(standing.method)
        try:
            loaded = method_module.load(path)
        except (OSError, tomllib.TOMLDecodeError, MethodError) as exc:
            raise ToolFailure(f"the standing method {standing.method} does not load: "
                              f"{exc}") from exc
        self._discover_for(loaded)
        event, handle = self._run_narrated(WarmUp(
            method=loaded, method_path=path, steps=standing.steps,
            dwell_s=standing.dwell_s, directory=self.output,
            stem=_standing_stem("warm-up"), instrument=self.instrument,
            instrument_path=self.instrument_path), self._standing_wait())
        result = event.result if isinstance(event, JobFinished) else None
        if not isinstance(result, StandingResult):
            return self._unfinished(handle, event)
        audited = routine_module.audit(loaded, result.snapshot.after, STANDING_SETTINGS)
        ok = audited["differences"] == 0
        return {
            "ok": ok,
            "method": standing.method,
            "hash": method_module.stamp(loaded)["method_hash"][:HASH_DIGITS],
            "boxes": [entry.name for entry in loaded.boxes],
            "steps": result.steps, "dwell_s": standing.dwell_s,
            "seconds": round(result.seconds, 1),
            "differences": audited["rows"], "unread": audited["unread"],
            "compared": audited["compared"],
            "send_log": result.send_log, "transcript": result.transcript_path,
            "read_back": "\n".join(state.render() for state in result.snapshot.after),
            "text": (f"warmed up to {standing.method}: every box holds its "
                     f"{audited['compared']} declared settings" if ok else
                     f"warmed up to {standing.method}, and {audited['differences']} "
                     "declared setting(s) do not read back as declared"),
        }

    @tool("hardware", read_only=False, destructive=True)
    def stand_down(self, stop: bool = False,
                   reason: str = "stopped for the instrument's stand-down") -> dict:
        """Put the instrument to rest for the night, and stop the daemon.

        Every box is taken out of table mode (`SMOD,LOC`); every DC bias channel, RF
        drive and ARB range is ramped to zero from what it reads back, in the instrument
        document's `[standing]` steps and dwell (RF frequency and mode are left alone);
        every digital output is lowered; the boxes are read back and anything not at zero
        is listed under `not_zero`. Then the daemon is shut down, which stops the
        acquisition console with it, and the answer says whether its process ended, the
        instrument lock came free and the console's port closed. Refused while an
        acquisition or a send is running or queued, naming it, unless `stop` is true:
        then the run ends after its current repetition (`reason` goes in its log) and the
        stand-down follows. Refused, `stop` or not, while a person at the window has
        paused the run. This ends the session's daemon: every later tool call finds
        none until one is started again. Its own line ends the audit log's live file,
        which it then seals as `seal_calls` does, answering the segment under `sealed`
        and the day record's under `sealed_day`.
        """
        self._settle()
        busy = self._busy()
        if busy and not stop:
            raise ToolFailure(
                f"job {busy.id} ({busy.label}) is "
                f"{'running' if self._is_running(busy) else 'queued'}: stand-down ends no "
                "acquisition by surprise. Wait for it, or pass stop to end it after its "
                "current repetition")
        if busy:
            self.owner.stop(reason)  # type: ignore[attr-defined]
            self._say(f"stopping job {busy.id} ({busy.label}) first")
            deadline = time.monotonic() + STOP_WAIT_S
            while self._busy() is not None:
                if time.monotonic() >= deadline:
                    raise ToolFailure(f"job {busy.id} did not end within {STOP_WAIT_S:.0f} "
                                      "s of being stopped; nothing was stood down")
                time.sleep(0.25)
        standing = self.instrument.standing
        if not self.owner.status().boxes and standing.method:  # type: ignore[attr-defined]
            try:
                self._discover_for(method_module.load(self._in_library(standing.method)))
            except (OSError, tomllib.TOMLDecodeError, MethodError) as exc:
                raise ToolFailure(f"no boxes are open, and the standing method "
                                  f"{standing.method} does not load to find them: "
                                  f"{exc}") from exc
        event, handle = self._run_narrated(StandDown(
            steps=standing.steps, dwell_s=standing.dwell_s, directory=self.output,
            stem=_standing_stem("stand-down")), self._standing_wait())
        result = event.result if isinstance(event, JobFinished) else None
        if not isinstance(result, StandingResult):
            return self._unfinished(handle, event)
        not_zero = _not_at_rest(result.snapshot.after)
        gone = self._shut_down_daemon("the instrument's stand-down")
        stopped = not any(gone.values())
        return {
            "ok": not not_zero and stopped,
            "boxes": [state.name for state in result.snapshot.after],
            "steps": result.steps, "dwell_s": standing.dwell_s,
            "seconds": round(result.seconds, 1),
            "not_zero": not_zero,
            "outputs_lowered": list(result.lowered),
            "daemon": {"stopped": stopped, **{f"{key}_still": value
                                              for key, value in gone.items()}},
            "send_log": result.send_log, "transcript": result.transcript_path,
            "read_back": "\n".join(state.render() for state in result.snapshot.after),
            "text": ("stood down: " + ("every box at zero" if not not_zero else
                                       f"{len(not_zero)} setting(s) not at zero")
                     + ("; the daemon and the console are stopped" if stopped else
                        "; the daemon has not finished stopping: "
                        + ", ".join(key for key, value in gone.items() if value)
                        + " still")),
        }

    @tool("hardware", read_only=False)
    def snapshot_boxes(self, note: str = "", directory: str = "") -> dict:
        """Read every box back, its state and its controller's health, into files.

        For the moment something has gone wrong, before anyone touches a box: getters
        only, no mode change, so a box that was armed is still armed afterwards. Each
        box is asked everything `read_box_state` asks, then `UPTIME`, `STATUS` (the
        last reset's cause and the TWI failure count), `THREADS`, `GDCPWR` and `GERR`.
        Written beside one another under the stem `snapshot-YYYYmmdd-HHMMSS`: a send
        log, a wire transcript and `<stem>.snapshot.json`, in `directory` if one is
        given and the server's output directory otherwise. `note` is the operator's
        words (what was seen, which run, their initials) and heads the send log.
        Refused while a send or acquisition is running or queued, naming it.
        """
        self._settle()
        busy = self._busy()
        if busy:
            raise ToolFailure(
                f"job {busy.id} ({busy.label}) is "
                f"{'running' if self._is_running(busy) else 'queued'}: a snapshot reads "
                "the boxes between jobs, never during one. Stop it or wait for it first")
        standing = self.instrument.standing
        if not self.owner.status().boxes and standing.method:  # type: ignore[attr-defined]
            try:
                self._discover_for(method_module.load(self._in_library(standing.method)))
            except (OSError, tomllib.TOMLDecodeError, MethodError) as exc:
                raise ToolFailure(f"no boxes are open, and the standing method "
                                  f"{standing.method} does not load to find them: "
                                  f"{exc}") from exc
        stem = _standing_stem("snapshot")
        where = os.path.abspath(directory) if directory else self.output
        event, handle = self._run(SnapshotBoxes(directory=where, stem=stem,
                                                note=note.strip()), JOB_WAIT_S)
        result = event.result if isinstance(event, JobFinished) else None
        if not isinstance(result, SnapshotResult):
            return self._unfinished(handle, event)
        boxes = {}
        for name, health in result.health.items():
            boxes[name] = {
                "uptime_min": health.uptime_min, "reset_cause": health.reset_cause,
                "twi_fails": health.twi_fails, "dc_power": health.dc_power,
                "last_error": health.last_error,
                "threads_disabled": [row.name for row in health.threads if not row.enabled],
                "not_asked": list(health.skipped), "refused": dict(health.refused),
            }
        read = len(result.states)
        return {
            "ok": not result.failed,
            "stem": stem, "taken_at": result.taken_at, "note": result.note,
            "boxes": boxes, "failed": dict(result.failed),
            "send_log": result.send_log, "transcript": result.transcript_path,
            "json": result.json_path, "seconds": round(result.seconds, 1),
            "read_back": "\n".join(
                [result.states[name].render() for name in result.states]
                + [health.render() for health in result.health.values()]),
            "text": (f"snapshot {stem}: {read} box(es) read"
                     + (f", {len(result.failed)} failed ("
                        + ", ".join(result.failed) + ")" if result.failed else "")
                     + f"; written to {result.json_path}"),
        }

    # -- helpers -----------------------------------------------------------------

    def _library_files(self) -> list[tuple[str, str]]:
        """Every document in the library, as (its library directory, its path)."""
        return [(library, path) for library in self.libraries
                for path in methodlib.library_files(library)]

    def _duplicates(self) -> dict[str, list[str]]:
        """Each path, relative to its library, that more than one library holds, with
        the full path of every copy.

        Compared as the file system compares them (`os.path.normcase`), which is how
        `_in_library` finds a path: `line-b.toml` in one library and `LINE-B.toml` in
        another are one name on Windows, refused as one, and so listed as one. Keyed by
        each copy's own spelling, since that is what a listing shows."""
        copies: dict[str, list[tuple[str, str]]] = {}
        for library, path in self._library_files():
            relative = os.path.relpath(path, library).replace(os.sep, "/")
            copies.setdefault(os.path.normcase(relative), []).append((relative, path))
        return {relative: [path for _, path in found]
                for found in copies.values() if len(found) > 1
                for relative, _ in found}

    def _library_text(self) -> str:
        """The library's directories as a refusal names them."""
        return ", ".join(self.libraries)

    @staticmethod
    def _is_template(path: str) -> bool:
        try:
            with open(path, "rb") as handle:
                return template_module.is_template(tomllib.load(handle))
        except (OSError, tomllib.TOMLDecodeError):
            return False

    def _in_library(self, path: str) -> str:
        """A path a tool was given, made absolute against the library.

        Searched in every library directory in order. A path in two of them is refused
        with both named, never quietly the first: a project's copy of a template and the
        instrument's are two experiments under one name. A path in none is placed in the
        last directory, which is a project's own when one adds its templates after the
        instrument's, so a rendered method a session writes lands there and never in
        the instrument's library unless that is the only one."""
        if os.path.isabs(path):
            return path
        if not self.libraries:
            raise ToolFailure(f"{path!r} is not an absolute path and no method library is "
                              f"set: start the server with --library, or set {INSTRUMENT_ENV} "
                              "to the instrument's folder, whose golden directory is the "
                              "library by default")
        found = [os.path.join(library, path) for library in self.libraries
                 if os.path.exists(os.path.join(library, path))]
        if len(found) > 1:
            raise ToolFailure(f"{path!r} is in more than one library, " + " and ".join(found)
                              + "; rename or remove one copy, or name it by its full path")
        return found[0] if found else os.path.join(self.libraries[-1], path)

    def _document(self, path: str, what: str) -> str:
        """`_in_library` for a document a tool is about to read: one sentence where it is
        not there, naming where it was looked for, rather than the open's own error."""
        found = self._in_library(path)
        if not os.path.isfile(found):
            where = (f"at {found}" if os.path.isabs(path)
                     else f"in the library ({self._library_text()})")
            raise ToolFailure(f"there is no {what} {path!r} {where}; "
                              f"list_{what}s shows what is there")
        return found

    def _write_rendered(self, rendered: Rendered, to: str, overwrite: bool) -> str:
        """Save a render with its `[rendered]` table; the absolute path it went to.

        An absolute `to` is written where it says. A relative one is a path in the
        library's last directory and nowhere else, and three things are refused rather
        than guessed at. A last directory that is an instrument's own library -- the only
        one served, or one named after a project's -- is the instrument's, kept by its
        people, and a session's render does not go into it (lab record, task 160). A last
        directory that is not there is a mistyped setting, not a folder to make. And a
        file of that name in an earlier directory is neither replaced, which `overwrite`
        would otherwise do to an instrument's file, nor shadowed by a second copy, which
        every tool would then refuse as a duplicate. `.toml` is added before any of this
        is looked up, so `to = "line-b"` is checked as the `line-b.toml` it would write."""
        if not to.lower().endswith(".toml"):
            to += ".toml"
        if os.path.isabs(to):
            path = os.path.abspath(to)
        else:
            if not self.libraries:
                self._in_library(to)  # refuses: no library to place it in
            last = self.libraries[-1]
            absolute = ("give `to` as an absolute path, such as a file in the folder of "
                        "the task this render is for")
            if _is_instrument_library(last):
                only = ("the only library served is" if len(self.libraries) == 1
                        else "the library's last directory is")
                raise ToolFailure(
                    f"{to!r} is not written: {only} the instrument's own ({last}), and a "
                    f"render does not go into it; {absolute}"
                    + ("" if len(self.libraries) == 1 else
                       ", or name the project's directory after the instrument's"))
            if not os.path.isdir(last):
                raise ToolFailure(
                    f"{to!r} is not written: the library's last directory, {last}, does "
                    f"not exist; correct --library, or {absolute}")
            elsewhere = [os.path.join(library, to) for library in self.libraries[:-1]
                         if os.path.exists(os.path.join(library, to))]
            if elsewhere:
                raise ToolFailure(
                    f"{to!r} is not written: {' and '.join(elsewhere)} already has that "
                    "name in another library directory, and a render neither replaces it "
                    f"nor puts a second copy beside it; name another file, or {absolute}")
            path = os.path.abspath(os.path.join(last, to))
        if os.path.exists(path) and not overwrite:
            raise ToolFailure(f"{path} already exists; pass overwrite to replace it, or name "
                              "another file")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        method_module.save(template_module.attached(rendered), path)
        return path

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
            path = self._document(method, "method")
            return method_module.load(path), None, path
        path = self._document(template, "template")
        rendered = template_module.render(template_module.load_template(path),
                                          dict(knobs or {}), dict(labels or {}))
        return rendered.method, rendered, path

    def _refuse_unless_allowed(self, loaded: Method, rendered: Rendered | None,
                               request: str) -> None:
        problems = guard_acquisition(self.owner, rendered, request, method=loaded,
                                     limits=self.limits)
        if problems:
            raise ToolFailure("; ".join(problems))

    # -- the standing envelope ---------------------------------------------------

    def _session_of(self) -> tuple[str, _dt.datetime]:
        """The daemon session this server works in, and when it began.

        A daemon's own, from `hello`, so every server and every restart of one over
        the same daemon writes under one session; an owner in this process is a
        session of its own, begun when this toolbox first asked."""
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

    def _cold_start_mode(self, rendered: Rendered | None) -> str:
        """The limits' rule for this method, or `caution` with none: a real owner with
        no limits never gets this far, and a rehearsal with none refuses nothing."""
        return self.limits.mode_for(rendered) if self.limits is not None else "caution"

    def _outside_limits(self, loaded: Method, rendered: Rendered | None) -> list[str]:
        """What the limits say about this method itself."""
        if self.limits is None:
            return []
        real = not self.owner.status().fake  # type: ignore[attr-defined]
        return envelope.check(loaded, rendered, self.limits, real=real)

    def _limits_of(self, template: Template) -> dict | None:
        if self.limits is None:
            return None
        entry = self.limits.entry_for(template.hash)
        if entry is None:
            return {"allowed": False, "problems": []}
        own = {knob.name for knob in template.knobs}
        return {
            "allowed": True,
            "origin": entry.origin,
            "knobs": {limit.name: {"min": limit.min, "max": limit.max}
                      for limit in entry.knobs},
            "caps": {cap.name: {"min": cap.min, "max": cap.max}
                     for cap in self.limits.caps if cap.name in own},
            "fixed": dict(entry.fixed),
            "cold_start": entry.cold_start or self.limits.cold_start,
            "problems": entry.problems_against(template),
        }

    def _templates(self) -> list[Template]:
        found = []
        for _, path in self._library_files():
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
                known = mint_id(words)
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
        """One past the highest place this request's files already hold, index or
        position, for a request continued by id from an earlier session. Under
        `_guard`."""
        highest = 0
        for path in glob.glob(os.path.join(self.output, "*" + SUMMED_SUFFIX)):
            stamped = _series(path)
            if stamped is not None and stamped["id"] == identity:
                highest = max(highest, int(stamped.get("index") or 0),
                              int(stamped.get("position") or 0))
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
            kept = self._summary(Handle(id=job, kind="", label=""))
            ended = kept.ended
            found = (ended.handle if isinstance(ended, (JobFinished, JobFailed))
                     else kept.handle if kept.job is not None else None)
        if found is None:
            raise ToolFailure(f"job {job} is not one the owner knows: never submitted, or "
                              "finished long enough ago that its progress was let go")
        with self._guard:
            self._handles.setdefault(found.id, found)
        return found

    @property
    def route(self) -> str:
        """The route this toolbox's requests come by, as a run record's `source` says it:
        its audit log's `via`, `mcp` or `cli`; `mcp` for a toolbox whose log names none,
        which is `build_server`'s in a test."""
        return self.log.via if self.log.via in ("mcp", "cli") else "mcp"

    def _submit_recorded(self, job: object, record: RunRecord) -> Handle:
        """`_submit`, for a job whose request's record is already open: a job the owner
        refuses to queue leaves a note there saying so, then the refusal is raised."""
        try:
            return self._submit(job)
        except Exception as exc:
            with contextlib.suppress(Exception):
                record.append("notes", {"text": f"the job was not queued: {exc}",
                                        "by": "server"})
            raise

    def _hold(self) -> Hold | None:
        """A person's pause on the series running, asked for or in force, or None."""
        return getattr(self.owner.status(), "hold", None)  # type: ignore[attr-defined]

    def _refuse_if_paused(self) -> None:
        hold = self._hold()
        if hold is not None:
            raise ToolFailure(held(hold))

    def _submit(self, job: object) -> Handle:
        self._refuse_if_paused()
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

    def _summary(self, handle: Handle) -> JobSummary:
        """Where the job has got to: the owner's `summary`, or for an owner without one
        (a stand-in in a test) the same worked out from its whole history."""
        asking = getattr(self.owner, "summary", None)
        if asking is not None:
            return asking(handle)
        return summarize(handle, self.owner.events(handle, 0))  # type: ignore[attr-defined]

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
        missing = self._no_routines()
        if missing:
            raise ToolFailure(f"there is no routine called {name!r}: {missing}")
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
        raise ToolFailure(f"there is no routine called {name!r} in {self.routines}; the "
                          f"routines are {', '.join(names) or 'none'}")

    def _no_routines(self) -> str:
        """Why there are no routines to list or run, or the empty string: no directory
        set, or one that is not there, with where it came from. Routines are looked for
        beside the library's *first* directory, so a project's templates named before the
        instrument's library point at a `routines` the project does not have."""
        if not self.routines:
            return ("no routine directory is set: start the server with --library or "
                    f"--routines, or set {INSTRUMENT_ENV} to the instrument's folder, "
                    "whose routines directory is found beside its library")
        if os.path.isdir(self.routines):
            return ""
        beside = (f", which is `{routine_module.ROUTINES_NAME}` beside the library's first "
                  f"directory ({self.libraries[0]})"
                  if self.libraries and _same_directory(
                      self.routines, routine_module.default_directory(self.libraries[0]))
                  else "")
        return (f"the routines were looked for in {self.routines}{beside}, and that folder "
                "does not exist: name the instrument's library first in --library, or "
                "give --routines")

    # -- the two ends of the day's helpers ---------------------------------------

    def _settle(self) -> None:
        """Wait for the jobs a daemon queues for itself as it starts -- the scan and the
        console -- so that a warm-up run straight after one finds the boxes it found."""
        deadline = time.monotonic() + JOB_WAIT_S
        while time.monotonic() < deadline:
            state = self.owner.status()  # type: ignore[attr-defined]
            pending = ([state.running] if state.running is not None else []) \
                + list(state.queued)
            if not any(handle.kind in STARTUP_KINDS for handle in pending):
                return
            time.sleep(0.2)

    def _busy(self) -> Handle | None:
        """The first job running or queued that a warm-up or stand-down must not
        overlap: anything that sends to the boxes or acquires."""
        state = self.owner.status()  # type: ignore[attr-defined]
        pending = ([state.running] if state.running is not None else []) + list(state.queued)
        return next((handle for handle in pending if handle.kind in BUSY_KINDS), None)

    def _is_running(self, handle: Handle) -> bool:
        running = self.owner.status().running  # type: ignore[attr-defined]
        return running is not None and running.id == handle.id

    def _refuse_if_busy(self, what: str) -> None:
        busy = self._busy()
        if busy is not None:
            raise ToolFailure(
                f"job {busy.id} ({busy.label}) is "
                f"{'running' if self._is_running(busy) else 'queued'}, so this will not "
                f"{what} now: a warm-up in the middle of a run is a mistake")

    def _standing_wait(self) -> float:
        """How long a warm-up or stand-down may take: its dwells, plus a margin for the
        readbacks and the strings, which are a few seconds a box."""
        standing = self.instrument.standing
        return JOB_WAIT_S + standing.steps * (standing.dwell_s + 5.0)

    def _run_narrated(self, job: object, timeout: float) -> tuple[Event | None, Handle]:
        """`_run`, telling `narrate` each ramp step and warning as it happens, for the
        person watching a command line through a minute of dwells."""
        handle = self._submit(job)
        deadline = time.monotonic() + timeout
        seen = 0
        while True:
            for entry in self._wait(handle, seen, max(0.0, deadline - time.monotonic())):
                seen = entry.seq
                event = entry.event
                if isinstance(event, JobFailed):
                    raise ToolFailure(event.message)
                if isinstance(event, JobFinished):
                    return event, handle
                if isinstance(event, (RampStepped, StandDownDone, Warned)):
                    self._say(event.text)
            if time.monotonic() >= deadline:
                return None, handle

    def _shut_down_daemon(self, reason: str) -> dict[str, bool]:
        """Shut the owner down and wait until it is gone; what is still there, if
        anything (`clockwork.app.serving.wait_gone`)."""
        owner = self.owner
        if not hasattr(owner, "hello"):
            # An owner in this process, `clockwork mcp --fake`'s: nothing to wait on
            # but its thread.
            owner.shutdown(reason)  # type: ignore[attr-defined]
            joined = owner.join(60) if hasattr(owner, "join") else True
            return {"answering": not joined}
        from ..app.serving import wait_gone

        said = owner.hello()  # type: ignore[attr-defined]
        self._say("shutting clockwork serve down")
        owner.shutdown(reason)  # type: ignore[attr-defined]
        return wait_gone(owner.endpoint, pid=int(said.pid),  # type: ignore[attr-defined]
                         fake=bool(said.fake))

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
                                request_id=identity, text=words, initials=who,
                                source=self.route)
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
        if ended is None:
            return self._unjudged(loaded, who, words, identity,
                                  f"the acquisition had not finished within "
                                  f"{ROUTINE_WAIT_S / 60:.0f} min; follow job {job} with "
                                  "progress", job=job)
        if isinstance(ended, JobFailed):
            return self._unjudged(loaded, who, words, identity,
                                  f"the acquisition failed: {ended.message}", job=job)
        runs = [run for run in (ended.result or []) if isinstance(run, Run)]
        files = [{"stem": stem_of(run), "raw_path": run.raw_path,
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
                                text=words, initials=who, source=self.route)
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
                                request_id=identity, text=words, initials=who,
                                source=self.route)
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
        if not self.owner.status().fake:  # type: ignore[attr-defined]
            routine_module.remember(_plain(entry), request_id=identity, record=record.path)
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


def _is_instrument_library(directory: str) -> bool:
    """Whether a library directory is an instrument directory's own: `golden` under a
    folder that holds the instrument document (`clockwork.instrument_dir`). Read off the
    folder rather than off `$CLOCKWORK_INSTRUMENT`, so a library named by hand is
    recognised too."""
    path = os.path.abspath(directory)
    return (os.path.normcase(os.path.basename(path)) == "golden"
            and os.path.isfile(os.path.join(os.path.dirname(path), INSTRUMENT_FILE)))


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


def _run_files(path: str) -> tuple[str, str, list[str]]:
    """The directory, stem and files on disk of the run `path` names: either of its
    files or its stem. The summed file first, where there is one."""
    directory, name = os.path.split(os.path.abspath(path))
    for suffix in (SUMMED_SUFFIX, RAW_SUFFIX):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    files = [candidate for candidate in (os.path.join(directory, name + SUMMED_SUFFIX),
                                         os.path.join(directory, name + RAW_SUFFIX))
             if os.path.isfile(candidate)]
    return directory, name, files


def _found_file(stem: str, files: list[str]) -> dict:
    """A run's entry in a record begun after it was acquired: its files and the summary
    of the first, as the owner's `file_entry` keeps one for a run it wrote."""
    summed = next((file for file in files if file.endswith(SUMMED_SUFFIX)), None)
    raw = next((file for file in files if not file.endswith(SUMMED_SUFFIX)), None)
    try:
        numbers: dict[str, Any] = brief(summary.summarize(files[0]))
    except Exception as exc:  # noqa: BLE001 -- the record says why, never raises
        numbers = {"problem": sentence(exc)}
    return {"stem": stem, "raw": raw, "summed": summed, "summary": numbers}


def _records(directory: str) -> dict[str, str]:
    """Every run record in `directory`, by the request id it records."""
    found: dict[str, str] = {}
    for path in glob.glob(os.path.join(glob.escape(directory), "*" + RECORD_SUFFIX)):
        try:
            identity = load_record(path).get("request", {}).get("id")
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


def _standing_stem(what: str) -> str:
    """The stem a warm-up's or stand-down's send log and transcript are named for:
    `warm-up-20260927-081502`, which no acquisition's stem can be."""
    return f"{what}-{_dt.datetime.now():%Y%m%d-%H%M%S}"


def _not_at_rest(states: tuple[BoxState, ...]) -> list[dict]:
    """Every setting a stand-down should have left at zero or idle and did not, one row
    each: DC bias setpoints, RF drive, ARB range, and a table still owning the box."""
    rows: list[dict] = []
    for state in states:
        for index, volts in enumerate(state.dc_bias_setpoints):
            if volts is None or abs(volts) > AT_REST:
                rows.append({"box": state.name, "setting": "dc_bias", "index": index + 1,
                             "held": volts})
        for reading in state.rf:
            if reading.drive_pct is None or abs(reading.drive_pct) > AT_REST:
                rows.append({"box": state.name, "setting": "rf drive_pct",
                             "index": reading.channel, "held": reading.drive_pct})
        for module in state.modules:
            held = state.module(module).get("GWFVRNG")
            try:
                volts = float(held) if held is not None else None
            except ValueError:
                volts = None
            if held is not None and (volts is None or abs(volts) > AT_REST):
                rows.append({"box": state.name, "setting": "GWFVRNG", "index": module,
                             "held": held})
        if not state.monitors_converting:
            rows.append({"box": state.name, "setting": "table", "index": 0,
                         "held": state.table_status})
    return rows


def _result(result: object) -> Any:
    """A finished job's result, cut to what a caller reads: never a method's text."""
    if isinstance(result, StandingResult):
        return {"steps": result.steps, "lowered": list(result.lowered),
                "seconds": round(result.seconds, 2), "send_log": result.send_log,
                "transcript": result.transcript_path}
    if isinstance(result, SeriesResult):
        return {"runs": [_run_summary(run) for run in result.runs],
                "points": result.points, "points_done": result.points_done,
                "complete": result.complete, "stopped": result.stopped or None,
                "stopped_at": result.stopped_at or None,
                "stopped_file": result.stopped_file or None,
                "not_acquired": _not_acquired(result),
                "replaced": list(result.replaced)}
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


def _not_acquired(result: SeriesResult) -> list[dict]:
    """The planned points a stop left without a file, as `progress` lists them."""
    return [{"index": index, "reference": reference or None}
            for index, reference in result.not_acquired]


def _paused(hold: Hold | None) -> dict | None:
    """A person's pause as `status` and `progress` answer it, or None for none."""
    if hold is None:
        return None
    return {"job": hold.job, "grain": hold.grain, "by": hold.by, "client": hold.client,
            "asked": hold.asked, "since": hold.since or None, "where": hold.where or None,
            "text": hold.text}


def _guarded(guarding: Guarding | None) -> dict | None:
    """The daemon's beam guard as `status` and `progress` answer it, or None for none.

    A person at the window arms it; no tool does (lab record, task 168). A file the
    guard ended says `failed` with its sentence, and a series it ended a file of is
    then paused by the guard (`paused`, with `by` the beam guard) until a person
    resumes it, which acquires that point again from its start."""
    if guarding is None:
        return None
    guard = guarding.guard
    return {"floor": guard.floor, "repetitions": guard.repetitions,
            "source": guard.source or None, "by": guarding.by, "since": guarding.since,
            "text": guarding.text}


def _series_progress(job: SeriesJob, point: PointStarted | None) -> dict:
    """Where a series job has got to: the point in flight, and the order planned."""
    return {
        "points": len(job.points), "seed": job.seed,
        "order": [entry.index for entry in job.points],
        "point": None if point is None else {
            "number": point.number, "index": point.index, "position": point.position,
            "knobs": dict(point.knobs), "reference": point.reference or None,
            "replicates": point.replicates, "sends": point.sends,
            "retake": point.retake, "row": point.name or None},
    }


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

