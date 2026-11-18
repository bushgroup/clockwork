"""The MCP server: the tool registry, a whole request through the SDK's own client, the
interlock, the standing envelope and the run record, the audit log, the tools over a
daemon, and `clockwork mcp` as a process.

Every owner here is `--fake` except the one the interlock is tested against, which is
built with a stand-in scan and never discovers, sends or starts anything: the refusal
is pinned before a job exists (lab record, tasks 69 and 71).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import types

import pytest
from mcp import Client, StdioServerParameters

from clockwork import envelope
from clockwork import method as method_module
from clockwork.acq.uimf import SUMMED_SUFFIX
from clockwork.mcp import (
    HAND_WRITTEN,
    NO_LIMITS,
    TOOLS,
    Toolbox,
    ToolFailure,
    guard_acquisition,
)
from clockwork.mcp import audit as audit_module
from clockwork.mcp.audit import LOG_NAME, AuditLog, SealFailure, hashed, segments
from clockwork.mcp.server import SIMULATED, build_server
from clockwork.method import template as template_module
from clockwork.mips import Discovery
from clockwork.owner import Acquire, JobFailed, LocalOwner, RemoteOwner, StartConsole
from clockwork.summary import summarize
from test_daemon import Daemon, ended

TEMPLATE = """\
template_schema = 1
renders = 2
start = [["box1", "TBLSTRT"]]
reset = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]

[knobs]
b_ticks = { default = 500, min = 100, max = 520, unit = "ticks", description = "line B high" }

[labels]
sample = { required = true, description = "what was sprayed" }

[constants]
tick_us = 129.0

[marks]
b_off = { ms = "b_ticks * tick_us / 1000", description = "line B falls" }

[metadata]
name = "mcp-test"
created = 2026-09-23
description = "One box, one table, a knob on line B."

[acquisition]
frames = 1
scans = 32
accumulations = 2
repetition_mode = "per_repetition"
keep_raw = true
file_stem = "mcp-test"
enable = { box = "box1", channel = "A" }

[[boxes]]
name = "box1"
port = "COM3"
setup = ["STBLCLK,EXT", "STBLTRG,POS"]
load = ["STBLDAT;0:[A:1,0:A:1:B:1,{b_ticks}:B:0,532:A:0,533:];"]
arm = ["SMOD,TBL"]
"""

REQUEST = "Run the test mixture once with line B held for 400 ticks, please"
LABELS = {"sample": "test mixture"}


@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "library"
    folder.mkdir()
    (folder / "line-b.toml").write_text(TEMPLATE, encoding="utf-8")
    rendered = template_module.render(template_module.loads_template(TEMPLATE),
                                      {}, LABELS)
    method_module.save(rendered.method, str(folder / "line-b-default.toml"))
    return str(folder)


@pytest.fixture
def fake_owner():
    owner = LocalOwner(fake=True, program="clockwork mcp test").start()
    owner.submit(StartConsole())
    yield owner
    owner.shutdown()
    owner.join(30)


def served(owner, library: str, output: str) -> Client:
    return Client(build_server(owner, library, output, instrument=SIMULATED))


async def call(client: Client, name: str, **arguments: object) -> dict:
    result = await client.call_tool(name, arguments)
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def refused(client: Client, name: str, **arguments: object) -> str:
    result = await client.call_tool(name, arguments)
    assert result.is_error, result.structured_content
    return result.content[0].text


async def follow(client: Client, job: int, timeout: float = 120.0) -> dict:
    """`progress` until the job is done, as a session following it would."""
    last, events = 0, []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        answer = await call(client, "progress", job=job, after=last, wait_s=5)
        events += answer["events"]
        last = answer["last"]
        if answer["done"]:
            return {**answer, "events": events}
    raise AssertionError(f"job {job} did not finish")


def audit_lines(output: str) -> list[dict]:
    """Every line of the folder's audit log, sealed segments and the live file in order."""
    return list(AuditLog(os.path.join(output, LOG_NAME)).lines())


def limits_text(library: str, *, templates: tuple[str, ...] = ("line-b.toml",),
                cold_start: str = "caution", hand_written: bool = False) -> str:
    """Limits over the library's templates: `b_ticks` 200 to 450, box1 only; with
    `hand_written`, the electrodes and the RF bounded, which admits a hand-written
    method."""
    entries = []
    for name in templates:
        loaded = template_module.load_template(os.path.join(library, name))
        key = name.removesuffix(".toml")
        entries.append(f'[templates.{key}]\nhash = "{loaded.hash[:12]}"\n'
                       f"[templates.{key}.knobs]\nb_ticks = {{ min = 200, max = 450 }}\n")
    bounded = ('[electrodes]\nmax_v = 1000.0\nnets = [\n'
               '  { name = "GUARD", dc = { box = "box1", channel = 16 } },\n'
               '  { name = "GROUND", volts = 0.0 },\n]\n'
               '[[rf_drive]]\nbox = "box1"\nchannel = 1\nmax_pct = 100.0\n')
    return (f'schema_version = 2\ncold_start = "{cold_start}"\n[allow]\nboxes = ["box1"]\n'
            + "".join(entries) + (bounded if hand_written else ""))


def followed(toolbox: Toolbox, job: int, timeout: float = 120.0) -> dict:
    """`progress` until the job is done, straight through the toolbox."""
    answer, last = {"done": False}, 0
    deadline = time.monotonic() + timeout
    while not answer["done"]:
        assert time.monotonic() < deadline, f"job {job} did not finish"
        answer = toolbox.call("progress", {"job": job, "after": last, "wait_s": 5})
        last = answer["last"]
    return answer


# --- the registry -------------------------------------------------------------------


def test_every_tool_is_registered_with_its_arguments_and_description(fake_owner, library,
                                                                     tmp_path):
    async def body():
        async with served(fake_owner, library, str(tmp_path)) as client:
            return {entry.name: entry for entry in (await client.list_tools()).tools}

    listed = asyncio.run(body())
    assert list(listed) == [entry.name for entry in TOOLS]
    assert {entry.group for entry in TOOLS} == {"method", "hardware", "acquisition", "data",
                                                "routine"}
    for entry in TOOLS:
        assert listed[entry.name].description.strip(), entry.name
        assert (set(listed[entry.name].input_schema.get("properties", {}))
                == set(entry.signature.parameters)), entry.name
    assert set(listed["acquire"].input_schema["required"]) == {"request", "initials"}
    assert listed["progress"].annotations.read_only_hint is True
    assert listed["acquire"].annotations.read_only_hint is False
    assert {name for name, entry in listed.items()
            if entry.annotations.destructive_hint} == {"stop", "stand_down"}
    assert not any(entry.read_only and entry.destructive for entry in TOOLS)


# --- a whole request ----------------------------------------------------------------


def test_a_request_goes_from_a_template_to_files_that_say_what_they_were_for(
        fake_owner, library, tmp_path):
    output = str(tmp_path / "runs")

    async def body():
        async with served(fake_owner, library, output) as client:
            templates = (await call(client, "list_templates"))["templates"]
            assert [entry["path"] for entry in templates] == ["line-b.toml"]
            knob = templates[0]["knobs"][0]
            assert (knob["name"], knob["min"], knob["max"], knob["integer"]) == (
                "b_ticks", 100, 520, True)
            assert [entry["path"] for entry in
                    (await call(client, "list_methods"))["methods"]] == ["line-b-default.toml"]

            chosen = {"template": "line-b.toml", "knobs": {"b_ticks": 400}, "labels": LABELS}
            rendered = await call(client, "render_template", **chosen)
            assert rendered["ok"] and rendered["knobs"] == {"b_ticks": 400}
            assert rendered["marks"] == [{"name": "b_off", "ms": 51.6, "scan": 400,
                                          "description": "line B falls"}]
            outside = await call(client, "render_template", template="line-b.toml",
                                 knobs={"b_ticks": 900}, labels=LABELS)
            assert not outside["ok"] and "b_ticks" in outside["problems"][0]
            assert (await call(client, "validate_method", **chosen))["ok"]

            found = await call(client, "discover_boxes", **chosen)
            assert found["boxes"] == ["box1"]
            armed = await call(client, "arm", request=REQUEST, initials="zz", **chosen)
            assert armed["stem"].endswith("_ZZ_001") and "box1" in armed["read_back"]

            other = await refused(client, "acquire", request=REQUEST, initials="zz",
                                  template="line-b.toml", knobs={"b_ticks": 300},
                                  labels=LABELS)
            assert "not holding this method" in other

            started = await call(client, "acquire", request=REQUEST, initials="zz", **chosen)
            assert started["request_id"] == armed["request_id"]
            assert started["first_place"] == 1
            done = await follow(client, started["job"])
            assert "failed" not in done, done
            kinds = [event["kind"] for event in done["events"]]
            assert kinds[0] == "JobStarted" and kinds[-1] == "JobFinished"
            assert "RunDone" in kinds
            [run] = done["runs"]
            assert done["position"]["files_done"] == 1
            assert done["position"]["files_asked"] == 1
            assert sum(event["kind"] == "BatchSeen" for event in done["events"]) <= len(
                [event for event in done["events"] if event["kind"] != "BatchSeen"])
            assert run["complete"] and os.path.isfile(run["summed_path"])

            listed = await call(client, "list_files")
            [entry] = listed["runs"]
            assert entry["stem"] == armed["stem"]
            assert entry["request"] == {"id": started["request_id"], "index": 1,
                                        "position": 1, "text": REQUEST}
            assert entry["send_log"] and entry["transcripts"] and entry["folded"]

            summary = await call(client, "summarize_file", path=entry["summed"]["name"])
            assert summary["request"]["text"] == REQUEST
            assert summary["request"]["id"] == started["request_id"]
            assert summary["template"]["knobs"] == {"BTicks": 400.0}
            assert summary["template"]["labels"] == {"Sample": "test mixture"}
            status = await call(client, "status")
            assert status["fake"] and status["sends_refused"] is None
            assert status["last_armed"]["stem"] == armed["stem"]
            return started, run

    started, run = asyncio.run(body())
    stamped = summarize(run["summed_path"])["clockwork"]
    assert stamped["ClockworkSeriesId"] == started["request_id"]
    stem = os.path.basename(run["raw_path"])[: -len(".uimf")]
    transcript = [name for name in os.listdir(output)
                  if name.startswith(stem) and name.endswith(".transcript.log")]
    with open(os.path.join(output, transcript[0]), encoding="utf-8") as handle:
        assert REQUEST in handle.read()

    lines = audit_lines(output)
    assert [line["tool"] for line in lines][:3] == ["list_templates", "list_methods",
                                                    "render_template"]
    acquired = next(line for line in lines if line["tool"] == "acquire"
                    and line["error"] is None)
    assert acquired["request"] == {"id": started["request_id"], "text": REQUEST}
    assert any(line["error"] and "not holding" in line["error"] for line in lines)


def test_replicates_take_consecutive_places_and_a_stop_ends_the_series(fake_owner,
                                                                         library, tmp_path):
    output = str(tmp_path)
    chosen = {"template": "line-b.toml", "labels": LABELS}

    async def body():
        async with served(fake_owner, library, output) as client:
            await call(client, "discover_boxes", **chosen)
            await call(client, "arm", request=REQUEST, initials="zz", **chosen)
            first = await call(client, "acquire", request=REQUEST, initials="zz",
                               replicates=2, **chosen)
            assert len((await follow(client, first["job"]))["runs"]) == 2
            again = await call(client, "acquire", request=REQUEST, initials="zz",
                               replicates=6, **chosen)
            assert again["first_place"] == 3
            while True:
                answer = await call(client, "progress", job=again["job"], wait_s=5)
                if any(event["kind"] == "RunDone" for event in answer["events"]):
                    break
            assert (await call(client, "stop", reason="the test has what it needs"))[
                "job"] == again["job"]
            return (await follow(client, again["job"]))["runs"]

    runs = asyncio.run(body())
    assert 1 <= len(runs) < 6
    places = sorted(summarize(path)["clockwork"]["ClockworkSeriesIndex"]
                    for path in (os.path.join(output, name) for name in os.listdir(output)
                                 if name.endswith(SUMMED_SUFFIX)))
    assert places == list(range(1, 3 + len(runs)))


# --- the interlock ------------------------------------------------------------------


def test_the_interlock_refuses_a_real_owner_and_an_empty_request(fake_owner, library,
                                                                  tmp_path):
    def no_scan(**_: object) -> Discovery:
        return Discovery()

    real = LocalOwner(program="clockwork mcp interlock test", discover=no_scan)
    assert guard_acquisition(real, None, REQUEST) == [NO_LIMITS]
    assert guard_acquisition(fake_owner, None, REQUEST) == []
    assert len(guard_acquisition(fake_owner, None, "  ")) == 1

    toolbox = Toolbox(real, library=library, output=str(tmp_path))
    chosen = {"template": "line-b.toml", "labels": LABELS, "initials": "zz"}
    for name in ("arm", "acquire"):
        with pytest.raises(ToolFailure, match="no standing limits are in force"):
            toolbox.call(name, {"request": REQUEST, **chosen})
    assert toolbox.call("status", {})["sends_refused"] == NO_LIMITS
    assert toolbox.call("validate_method", {"template": "line-b.toml",
                                            "labels": LABELS})["ok"]

    limited = Toolbox(real, library=library, output=str(tmp_path),
                      limits=envelope.loads(limits_text(library)))
    with pytest.raises(ToolFailure) as caught:
        limited.call("arm", {"request": REQUEST, "initials": "zz",
                             "method": "line-b-default.toml"})
    assert str(caught.value) == HAND_WRITTEN
    assert limited.call("status", {})["limits"]["hand_written"] is False
    switched = envelope.loads(limits_text(library, hand_written=True))
    hand = method_module.load(os.path.join(library, "line-b-default.toml"))
    assert guard_acquisition(real, None, REQUEST, method=hand, limits=switched) == []
    assert Toolbox(real, library=library, output=str(tmp_path), limits=switched).call(
        "status", {})["limits"]["hand_written"] is True
    with pytest.raises(ToolFailure, match="outside the standing limits"):
        limited.call("arm", {"request": REQUEST, **chosen, "knobs": {"b_ticks": 500}})
    assert limited.call("status", {})["sends_refused"] is None
    assert real.status().queued == () and real.status().running is None
    real.shutdown()
    real.serve()

    fake = Toolbox(fake_owner, library=library, output=str(tmp_path))
    with pytest.raises(ToolFailure, match="says what it is for"):
        fake.call("arm", {"request": "", **chosen})
    with pytest.raises(ToolFailure, match="initials"):
        fake.call("arm", {"request": REQUEST, **{**chosen, "initials": "-"}})


def test_inside_the_standing_limits_a_request_proceeds_and_nothing_counts_it_down(
        fake_owner, library, tmp_path):
    output = str(tmp_path / "runs")
    toolbox = Toolbox(fake_owner, library=library, output=output, instrument=SIMULATED,
                      limits=envelope.loads(limits_text(library)))
    chosen = {"template": "line-b.toml", "knobs": {"b_ticks": 400}, "labels": LABELS}
    asked = {"request": REQUEST, "initials": "zz", **chosen}

    [listed] = toolbox.call("list_templates")["templates"]
    assert listed["limits"]["allowed"]
    assert listed["limits"]["knobs"] == {"b_ticks": {"min": 200, "max": 450}}
    outside = toolbox.call("render_template", {**chosen, "knobs": {"b_ticks": 500}})
    assert not outside["ok"] and outside["knobs"] == {"b_ticks": 500}
    assert "outside the standing limits" in outside["limits"][0]

    toolbox.call("discover_boxes", chosen)
    with pytest.raises(ToolFailure, match="outside the standing limits"):
        toolbox.call("arm", {**asked, "knobs": {"b_ticks": 500}})
    assert toolbox.call("status")["last_armed"] is None

    armed = toolbox.call("arm", {**asked, "plan": "one run at 400 ticks, then report"})
    assert any("box1 DC bias channels are not declared" in line
               for line in armed["cold_start"]), armed["cold_start"]
    started = toolbox.call("acquire", asked)
    assert started["record"] == armed["record"]
    assert followed(toolbox, started["job"])["runs"][0]["complete"]

    status = toolbox.call("status")
    assert "budget" not in status and status["sends_refused"] is None
    assert sum(status["session"]["acquisitions_by"].values()) == 1

    noted = toolbox.call("note", {"request_id": armed["request_id"],
                                  "text": "one run was enough for the request"})
    assert noted["record"] == armed["record"] and noted["notes"] == 1
    with pytest.raises(ToolFailure, match="has no run record"):
        toolbox.call("note", {"request_id": "no-such-request", "text": "hello"})
    deadline = time.monotonic() + 30
    written: dict = {"files": []}
    while True:
        try:
            with open(armed["record"], encoding="utf-8") as handle:
                written = json.load(handle)
        except PermissionError:
            # Windows refuses the open while the record's writer is replacing the file.
            pass
        if written["files"] or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert written["request"]["id"] == armed["request_id"]
    assert written["request"]["text"] == REQUEST
    assert [plan["text"] for plan in written["plans"]] == ["one run at 400 ticks, then report"]
    [arming] = written["arms"]
    assert arming["template"]["knobs"] == {"b_ticks": 400} and arming["finished"]
    assert written["acquisitions"][0]["replicates"] == 1
    [made] = written["files"]
    assert made["stem"] == armed["stem"] and made["complete"]
    assert made["summary"]["frames"] >= 1
    assert written["notes"][0]["text"].startswith("one run was enough")
    [run] = toolbox.call("list_files")["runs"]
    assert run["record"] == os.path.basename(armed["record"])
    assert [line["session"] for line in audit_lines(output)
            if line["tool"] == "acquire"] == [status["session"]["daemon_session"]]


def test_the_cold_start_check_refuses_an_undeclared_dc_bias_before_anything_is_sent(
        fake_owner, tmp_path):
    folder = tmp_path / "library"
    folder.mkdir()
    fifteen = "".join(f"{channel} = 0.0\n" for channel in range(1, 16))
    (folder / "fifteen.toml").write_text(
        TEMPLATE + "\n[boxes.dc_bias]\n" + fifteen, encoding="utf-8")
    (folder / "sixteen.toml").write_text(
        TEMPLATE + "\n[boxes.dc_bias]\n" + fifteen + "16 = 0.0\n", encoding="utf-8")
    library = str(folder)
    toolbox = Toolbox(fake_owner, library=library, output=str(tmp_path / "runs"),
                      instrument=SIMULATED, limits=envelope.loads(limits_text(
                          library, templates=("fifteen.toml", "sixteen.toml"),
                          cold_start="refuse")))
    asked = {"request": REQUEST, "initials": "zz", "knobs": {"b_ticks": 400},
             "labels": LABELS}
    toolbox.call("discover_boxes", {"template": "fifteen.toml", "labels": LABELS})
    with pytest.raises(ToolFailure) as caught:
        toolbox.call("arm", {**asked, "template": "fifteen.toml"})
    assert "box1 DC bias channel is not declared by the method and holds 16: 0.00 V" \
        in str(caught.value)
    assert "Nothing was sent" in str(caught.value)
    assert toolbox.call("status")["last_armed"] is None
    assert not os.path.isdir(tmp_path / "runs") or not any(
        name.endswith(".request.json") for name in os.listdir(tmp_path / "runs"))

    armed = toolbox.call("arm", {**asked, "template": "sixteen.toml"})
    assert armed["cold_start"] == []
    assert toolbox.call("status")["last_armed"]["stem"] == armed["stem"]


def test_render_template_to_a_file_writes_a_rendered_method_and_will_not_overwrite(
        library, tmp_path):
    """`to` is `render-template --to` (lab record, task 94): the file carries its
    template, knobs and labels, reproduces, and lists as a rendered method."""
    toolbox = Toolbox(object(), library=library, output=str(tmp_path / "runs"))
    chosen = {"template": "line-b.toml", "knobs": {"b_ticks": 400}, "labels": LABELS}
    answer = toolbox.call("render_template", {**chosen, "to": "cells/b-400"})
    written = answer["written"]
    assert answer["ok"] and written == os.path.join(library, "cells", "b-400.toml")
    loaded = method_module.load(written)
    rendered, why = template_module.reproduce(loaded)
    assert why == "" and dict(loaded.rendered_from.knobs) == {"b_ticks": 400}
    assert method_module.stamp(loaded)["method_hash"][:12] == answer["hash"][:12]
    assert rendered.labels == LABELS

    with pytest.raises(ToolFailure, match="already exists; pass overwrite"):
        toolbox.call("render_template", {**chosen, "knobs": {"b_ticks": 300},
                                         "to": written})
    assert dict(method_module.load(written).rendered_from.knobs) == {"b_ticks": 400}
    toolbox.call("render_template", {**chosen, "knobs": {"b_ticks": 300}, "to": written,
                                     "overwrite": True})
    assert dict(method_module.load(written).rendered_from.knobs) == {"b_ticks": 300}

    # A render with a problem writes nothing.
    refused = toolbox.call("render_template", {**chosen, "knobs": {"b_ticks": 9999},
                                               "to": "cells/too-far.toml"})
    assert not refused["ok"] and not os.path.exists(
        os.path.join(library, "cells", "too-far.toml"))

    listed = {entry["path"]: entry for entry in toolbox.call("list_methods")["methods"]}
    assert listed["cells/b-400.toml"]["rendered"].startswith(
        "rendered from mcp-test at b_ticks 300 ticks")
    assert "rendered" not in listed["line-b-default.toml"]


def test_an_owner_refuses_a_render_that_does_not_reproduce_the_method(fake_owner, library,
                                                                       tmp_path):
    rendered = template_module.render(template_module.loads_template(TEMPLATE), {}, LABELS)
    toolbox = Toolbox(fake_owner, library=library, output=str(tmp_path))
    toolbox.call("discover_boxes", {"template": "line-b.toml", "labels": LABELS})
    toolbox.call("arm", {"request": REQUEST, "initials": "zz", "template": "line-b.toml",
                         "labels": LABELS})
    handle = fake_owner.submit(Acquire(
        method=rendered.method, instrument=SIMULATED, directory=str(tmp_path),
        initials="ZZ", template=TEMPLATE, knobs={"b_ticks": 200}, labels=LABELS,
        request=REQUEST, series="a-series"))
    failed = ended(fake_owner, handle)
    assert isinstance(failed, JobFailed) and "rendered" in failed.message


# --- the audit log ------------------------------------------------------------------


def test_the_audit_log_hashes_long_text_and_records_every_error(fake_owner, library,
                                                                 tmp_path):
    long = "x" * 500
    assert hashed({"a": long, "b": ["short", "two\nlines"]}) == {
        "a": {"sha256": hashed(long)["sha256"], "chars": 500},
        "b": ["short", {"sha256": hashed("two\nlines")["sha256"], "chars": 9}]}

    toolbox = Toolbox(fake_owner, library=library, output=str(tmp_path))
    toolbox.call("load_method", {"method": "line-b-default.toml"})
    with pytest.raises(ToolFailure):
        toolbox.call("load_method", {"method": "missing.toml"})
    with pytest.raises(ToolFailure, match="wrong arguments"):
        toolbox.call("load_method", {"methd": "line-b-default.toml"})
    with pytest.raises(ToolFailure, match="no tool called"):
        toolbox.call("launch", {})
    loaded, missing, wrong = audit_lines(str(tmp_path))
    assert loaded["error"] is None and loaded["result"]["text"]["chars"] > 200
    assert "sha256" in loaded["result"]["text"]
    assert missing["error"] and wrong["error"].startswith("load_method was given")
    assert AuditLog("").requests() == {}


def test_the_acquisition_count_is_the_daemon_sessions_and_says_which_surface_made_it(
        tmp_path):
    """A `clockwork routine` at the terminal during an agent's five-acquisition series
    left the agent reading 6 made (on the rack, 2026-09-28; lab record, task 96). Every
    surface counts in the one daemon session, and the count says which made what."""
    path = str(tmp_path / "mcp-calls.log")
    for via, tool, result in (("mcp", "series", {"acquisitions": 5}),
                              ("cli", "acquire", {"job": 34}),
                              ("cli", "arm", {"ok": True}),
                              ("", "acquire", {"job": 35})):
        log = AuditLog(path, via=via)
        log.session = "one"
        log.write(tool=tool, arguments={}, seconds=0.1, result=result)
    other = AuditLog(path, via="mcp")
    other.session = "another"
    other.write(tool="acquire", arguments={}, seconds=0.1, result={"job": 1})
    refused = AuditLog(path, via="cli")
    refused.session = "one"
    refused.write(tool="acquire", arguments={}, seconds=0.1, error="refused")
    reader = AuditLog(path)
    assert reader.acquisitions_by("one") == {"mcp": 5, "cli": 1, "in process": 1}
    assert reader.acquisitions("one") == 7 and reader.acquisitions("another") == 1
    assert reader.acquisitions_by("") == {}


def _calls(log: AuditLog, *made: tuple[str, dict | None, dict]) -> None:
    for tool, request, result in made:
        log.write(tool=tool, arguments={}, seconds=0.1, result=result, request=request)


def test_a_seal_mid_session_changes_nothing_the_log_is_read_for(tmp_path):
    """A call after a day folder was listed grew its listed log, twice (lab record, task
    161). A sealed segment is never written again, and every reader reads all of them."""
    path = str(tmp_path / LOG_NAME)
    log = AuditLog(path, via="mcp")
    log.session = "day"
    first = {"id": "r1", "text": "line B at 400"}
    _calls(log, ("arm", first, {"ok": True}), ("acquire", first, {"job": 1}),
           ("series", {"id": "r2", "text": "a grid"}, {"acquisitions": 4}))
    before = (log.acquisitions_by("day"), log.request_for("line B at 400", "day"),
              log.requests())

    sealed = log.seal()
    assert sealed is not None and not os.path.exists(path)
    segment = tmp_path / str(sealed["name"])
    assert re.fullmatch(r"mcp-calls\.\d{8}-\d{6}\.log", sealed["name"])
    held = segment.read_bytes()
    assert sealed["bytes"] == len(held) and sealed["md5"] == hashlib.md5(held).hexdigest()
    assert (log.acquisitions_by("day"), log.request_for("line B at 400", "day"),
            log.requests()) == before

    _calls(log, ("acquire", first, {"job": 2}))
    assert os.path.isfile(path), "the call after a seal starts a new live file"
    assert segment.read_bytes() == held, "and leaves the segment as it was sealed"
    assert segments(str(tmp_path)) == [str(segment), path]
    assert log.acquisitions_by("day") == {"mcp": 6}
    assert log.request_for("line B at 400", "day") == "r1"


def test_segments_are_read_in_the_order_of_their_first_lines_the_live_one_last(tmp_path):
    """A tail split off by hand (`mcp-calls.after-<stamp>.log`) is read as a segment,
    in its place by time, not by name; the latest words for a request win across files."""
    def line(time: str, words: str) -> str:
        return json.dumps({"time": time, "tool": "arm", "request": {"id": "r", "text": words},
                           "session": "s"}) + "\n"
    (tmp_path / "mcp-calls.after-20261112-210000.log").write_text(
        line("2026-11-12T21:00:00", "third"), encoding="utf-8")
    (tmp_path / "mcp-calls.20261112-200000.log").write_text(
        line("2026-11-12T09:00:00", "first") + "not json\n", encoding="utf-8")
    (tmp_path / "mcp-calls.20261113-080000.log").write_text(
        line("2026-11-12T20:00:00", "second"), encoding="utf-8")
    (tmp_path / LOG_NAME).write_text(line("2026-11-13T08:00:00", "fourth"),
                                     encoding="utf-8")
    log = AuditLog(str(tmp_path / LOG_NAME))
    assert [entry["request"]["text"] for entry in log.lines()] == [
        "first", "second", "third", "fourth"]
    assert log.requests() == {"r": "fourth"}
    os.remove(tmp_path / LOG_NAME)
    assert log.requests() == {"r": "third"}


def test_a_seal_never_overwrites_a_segment_and_seals_nothing_with_no_live_file(
        tmp_path, monkeypatch):
    path = tmp_path / LOG_NAME
    log = AuditLog(str(path))
    assert log.seal() is None and AuditLog("").seal() is None
    now = _dt.datetime(2026, 11, 14, 18, 30, 5)

    class Frozen(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 -- the signature replaced
            return now
    monkeypatch.setattr(audit_module, "_dt", types.SimpleNamespace(datetime=Frozen))
    taken = tmp_path / "mcp-calls.20261114-183005.log"
    taken.write_text("already sealed\n", encoding="utf-8")
    _calls(log, ("status", None, {}))
    assert log.seal()["name"] == "mcp-calls.20261114-183005-2.log"
    _calls(log, ("status", None, {}))
    assert log.seal()["name"] == "mcp-calls.20261114-183005-3.log"
    assert taken.read_text(encoding="utf-8") == "already sealed\n"


@pytest.mark.skipif(os.name != "nt", reason="an open file refuses a rename on Windows only")
def test_a_seal_waits_out_another_holder_and_then_refuses_with_its_error(tmp_path):
    path = tmp_path / LOG_NAME
    log = AuditLog(str(path))
    _calls(log, ("status", None, {}))
    with open(path, encoding="utf-8"):
        with pytest.raises(SealFailure, match="another process holds it open"):
            log.seal(wait_s=0.3)
    assert path.is_file(), "a refused seal leaves the live file where it was"
    holder = open(path, encoding="utf-8")  # noqa: SIM115 -- released by the timer
    threading.Timer(0.3, holder.close).start()
    assert log.seal(wait_s=5)["bytes"] > 0 and not path.exists()


def test_seal_calls_ends_with_its_own_line_and_leaves_no_live_file(tmp_path):
    """The seal's own line is the last of the segment it seals, so after it answers the
    folder has no live file, even when it had none before (Matt, 2026-11-14)."""
    toolbox = Toolbox(object(), output=str(tmp_path))
    with pytest.raises(ToolFailure):
        toolbox.call("load_method", {"method": "missing.toml"})
    answer = toolbox.call("seal_calls", {})
    sealed = answer["sealed"]
    assert answer["directory"] == str(tmp_path) and not (tmp_path / LOG_NAME).exists()
    lines = (tmp_path / sealed["name"]).read_text(encoding="utf-8").splitlines()
    assert [json.loads(text)["tool"] for text in lines] == ["load_method", "seal_calls"]

    again = toolbox.call("seal_calls", {})["sealed"]
    assert again["name"] != sealed["name"] and not (tmp_path / LOG_NAME).exists()
    [only] = (tmp_path / again["name"]).read_text(encoding="utf-8").splitlines()
    assert json.loads(only)["tool"] == "seal_calls"
    assert [line["tool"] for line in audit_lines(str(tmp_path))] == [
        "load_method", "seal_calls", "seal_calls"]


# --- over the daemon ----------------------------------------------------------------


def test_the_tools_drive_a_daemon_exactly_as_they_drive_an_owner_in_process(library,
                                                                            tmp_path):
    made = Daemon(tmp_path)
    client = RemoteOwner(made.endpoint, timeout=10)
    try:
        toolbox = Toolbox(client, library=library, output=str(tmp_path),
                          instrument=SIMULATED)
        chosen = {"template": "line-b.toml", "labels": LABELS}
        assert toolbox.call("discover_boxes", chosen)["boxes"] == ["box1"]
        deadline = time.monotonic() + 20
        while "ready" not in toolbox.call("status", {})["console"]:
            assert time.monotonic() < deadline
            time.sleep(0.1)
        toolbox.call("arm", {"request": REQUEST, "initials": "zz", **chosen})
        job = toolbox.call("acquire", {"request": REQUEST, "initials": "zz",
                                       **chosen})["job"]
        answer, last = {"done": False}, 0
        deadline = time.monotonic() + 120
        while not answer["done"]:
            assert time.monotonic() < deadline
            answer = toolbox.call("progress", {"job": job, "after": last, "wait_s": 5})
            last = answer["last"]
        assert answer["runs"][0]["complete"]
        assert toolbox.call("list_files", {})["runs"][0]["request"]["text"] == REQUEST
    finally:
        made.stop(client)
        client.close()


# --- the process --------------------------------------------------------------------


def _clockwork(*arguments: str) -> list[str]:
    return [sys.executable, "-c",
            "import sys; from clockwork.app import main; sys.exit(main(sys.argv[1:]))",
            *arguments]


def test_clockwork_mcp_fake_serves_a_client_over_stdio(library, tmp_path):
    command = _clockwork("mcp", "--fake", "--library", library, "--output", str(tmp_path))

    async def body():
        parameters = StdioServerParameters(command=command[0], args=command[1:],
                                           env={**os.environ})
        async with Client(parameters) as client:
            names = [entry.name for entry in (await client.list_tools()).tools]
            status = await call(client, "status")
            rendered = await call(client, "render_template", template="line-b.toml",
                                  labels=LABELS)
            return names, status, rendered

    names, status, rendered = asyncio.run(body())
    assert names == [entry.name for entry in TOOLS]
    assert status["fake"] and status["output"] == str(tmp_path)
    assert rendered["ok"]
    assert [line["tool"] for line in audit_lines(str(tmp_path))] == ["status",
                                                                     "render_template"]


def test_clockwork_mcp_with_no_daemon_exits_saying_so():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    done = subprocess.run(_clockwork("mcp", "--endpoint", f"tcp://127.0.0.1:{port}"),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 1
    assert "no clockwork serve answered" in done.stderr
    assert done.stdout == ""
