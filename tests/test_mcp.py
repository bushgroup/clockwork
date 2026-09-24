"""The MCP server: the tool registry, a whole request through the SDK's own client, the
interlock, the audit log, the tools over a daemon, and `clockwork mcp` as a process.

Every owner here is `--fake` except the one the interlock is tested against, which is
built with a stand-in scan and never discovers, sends or starts anything: the refusal
is pinned before a job exists (lab record, task 69).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import pytest
from mcp import Client, StdioServerParameters

from clockwork import method as method_module
from clockwork.acq.uimf import SUMMED_SUFFIX
from clockwork.mcp import NOT_YET, TOOLS, Toolbox, ToolFailure, guard_acquisition
from clockwork.mcp.audit import AuditLog, hashed
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
    with open(os.path.join(output, "mcp-calls.log"), encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


# --- the registry -------------------------------------------------------------------


def test_every_tool_is_registered_with_its_arguments_and_description(fake_owner, library,
                                                                     tmp_path):
    async def body():
        async with served(fake_owner, library, str(tmp_path)) as client:
            return {entry.name: entry for entry in (await client.list_tools()).tools}

    listed = asyncio.run(body())
    assert list(listed) == [entry.name for entry in TOOLS]
    assert {entry.group for entry in TOOLS} == {"method", "hardware", "acquisition", "data"}
    for entry in TOOLS:
        assert listed[entry.name].description.strip(), entry.name
        assert (set(listed[entry.name].input_schema.get("properties", {}))
                == set(entry.signature.parameters)), entry.name
    assert set(listed["acquire"].input_schema["required"]) == {"request", "initials"}
    assert listed["progress"].annotations.read_only_hint is True
    assert listed["acquire"].annotations.read_only_hint is False


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
    assert guard_acquisition(real, None, REQUEST) == [NOT_YET]
    assert guard_acquisition(fake_owner, None, REQUEST) == []
    assert len(guard_acquisition(fake_owner, None, "  ")) == 1

    toolbox = Toolbox(real, library=library, output=str(tmp_path))
    chosen = {"template": "line-b.toml", "labels": LABELS, "initials": "zz"}
    for name in ("arm", "acquire"):
        with pytest.raises(ToolFailure, match="does not yet let an agent send"):
            toolbox.call(name, {"request": REQUEST, **chosen})
    assert toolbox.call("status", {})["sends_refused"] == NOT_YET
    assert real.status().queued == () and real.status().running is None
    assert toolbox.call("validate_method", {"template": "line-b.toml",
                                            "labels": LABELS})["ok"]
    real.shutdown()
    real.serve()

    fake = Toolbox(fake_owner, library=library, output=str(tmp_path))
    with pytest.raises(ToolFailure, match="says what it is for"):
        fake.call("arm", {"request": "", **chosen})
    with pytest.raises(ToolFailure, match="initials"):
        fake.call("arm", {"request": REQUEST, **{**chosen, "initials": "-"}})


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
