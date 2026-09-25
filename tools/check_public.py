"""Self-check for a fresh clone: no MIPS box, no digitizer, no console, no lab repo needed.

Every check here must pass in a bare public clone. Checks that need something a clone does
not ship -- a serial port with a box on it, a running acquisition console, the lab
repository -- are reported as SKIPPED when it is absent, never as FAIL.

What it covers today: the package imports, the version declarations agree, the three lower
layers stay free of Qt, the module layout is complete, lab-directory resolution behaves,
nothing shipped cites the development record by a path only a lab checkout resolves, a
method document round-trips through its phases, start sequence and repetition modes, the
MIPS sender drives a simulated box through a table load, a TBLRPT round trip, arming and a
rejection, the console client drives a simulated console from `info` through a whole
frame to `finished acquire`, and a frame that published nothing and a frame the console
reported an error on are both refused rather than reported as successes, and a whole
acquisition goes through the UIMF path -- clockwork creates the file, a simulated console
appends its scans, mainspring reads them back, and the fold sums a method frame's
repetitions to exactly A times one of them, and a whole stand-in acquisition run with a
wire transcript open leaves a file naming every frame it acquired -- while the same run
with none open emits no record at all. Tasks add sections as they land code.

Run:  uv run tools/check_public.py
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

FAIL: list[str] = []
SKIPPED: list[str] = []


def check_true(name: str, cond: object) -> None:
    print("{:4s} {}".format("OK" if cond else "FAIL", name))
    if not cond:
        FAIL.append(name)


def skip(name: str, why: str) -> None:
    print(f"SKIP {name} ({why})")
    SKIPPED.append(name)


def section(title: str) -> None:
    print()
    print("--- " + title + " " + "-" * max(3, 72 - len(title)))


def check_owner() -> None:
    """The owner interface, its wire forms and the instrument lock (lab record, task 67).

    **Every `Event` subclass round-trips through JSON**, found by walking the class tree
    rather than listed, so an event added to the loop or the owner without a field the
    codec can carry fails here the day it is written. Then the lock refuses a second
    owner with the first one's name, and an owner under a held lock refuses its jobs
    before its scan runs -- which is the property that matters: the refusal comes before
    any port is opened.
    """
    import json
    import tempfile

    from clockwork.acq import Event
    from clockwork.method import Method
    from clockwork.mips import Box, Discovery, FakeBox, Found
    from clockwork.owner import (
        Discover,
        Discovered,
        InstrumentLock,
        JobFailed,
        JobFinished,
        LocalOwner,
        LockHeld,
        wire,
    )
    from clockwork.owner.lock import read_holder

    def round_trips(cls: type) -> bool:
        data = wire.to_wire(wire.example(cls))
        back = wire.from_wire(json.loads(json.dumps(data)))
        return type(back) is cls and wire.to_wire(back) == data

    table = wire.wire_types()
    events = sorted(name for name, cls in table.items() if issubclass(cls, Event))
    broken: list[str] = []
    for name in events:
        try:
            if not round_trips(table[name]):
                broken.append(name)
        except Exception as exc:  # noqa: BLE001 -- reporting, not handling
            broken.append(f"{name} ({type(exc).__name__}: {exc})")
    check_true(f"every Event subclass round-trips through JSON ({len(events)} of them)"
               + (": " + ", ".join(broken) if broken else ""),
               len(events) > 20 and not broken)
    others = sorted(name for name, cls in table.items() if not issubclass(cls, Event))
    broken = []
    for name in others:
        try:
            if not round_trips(table[name]):
                broken.append(name)
        except Exception as exc:  # noqa: BLE001
            broken.append(f"{name} ({type(exc).__name__}: {exc})")
    check_true(f"every job, result and record round-trips too ({len(others)} of them)"
               + (": " + ", ".join(broken) if broken else ""), not broken)

    box = Box(transport=FakeBox(), name="box1")
    carried = wire.to_wire(Discovery(found=(Found(name="box1", port="COM3", box=box),)))
    back = wire.from_wire(carried)
    check_true("an open box stays with the owner that opened it: a Discovery crosses "
               "with its names and ports and without its Box",
               "box" not in carried["found"][0]
               and back.found[0].port == "COM3" and back.found[0].box is None)
    box.close()

    with tempfile.TemporaryDirectory() as scratch:
        path = os.path.join(scratch, "instrument.lock")
        first = InstrumentLock("the first holder", path)
        first.acquire()
        second = InstrumentLock("the second", path)
        try:
            second.acquire()
            refused = ""
        except LockHeld as exc:
            refused = str(exc)
        check_true(f"a second owner is refused, naming the first ({refused!r})",
                   "the first holder" in refused and f"pid {os.getpid()}" in refused)
        first.release()
        check_true("the record is cleared on release", read_holder(path) is None)
        second.acquire()
        check_true("the lock is free again once its holder lets go", second.held)
        second.release()

        method = wire.example(Method)
        owner = LocalOwner(fake=True, lock_path=path).start()
        handle = owner.submit(Discover(method=method))
        ended = _ended(owner, handle)
        status = owner.status()
        check_true(
            "a --fake owner takes no lock, runs a Discover and reports it as numbered "
            f"progress ({[type(p.event).__name__ for p in owner.events(handle)]})",
            isinstance(ended, JobFinished) and status.boxes == ("box1",)
            and status.holder is None and read_holder(path) is None
            and any(isinstance(p.event, Discovered) for p in owner.events(handle)))
        seqs = [p.seq for p in owner.events(handle)]
        check_true("progress numbers rise, and `after` returns only what is newer",
                   seqs == sorted(seqs) and owner.events(handle, after=seqs[-1]) == []
                   and len(owner.events(handle, after=seqs[0])) == len(seqs) - 1)
        owner.shutdown()
        owner.join(10)

        scans: list[object] = []

        def scan(**_: object) -> Discovery:
            scans.append(_)
            return Discovery()

        holder = InstrumentLock("the clockwork window", path)
        holder.acquire()
        owner = LocalOwner(program="clockwork serve", lock_path=path, discover=scan).start()
        ended = _ended(owner, owner.submit(Discover()))
        check_true(
            f"an owner under a held lock refuses its job before the scan runs "
            f"({owner.refused!r})",
            isinstance(ended, JobFailed) and ended.message == owner.refused
            and "the clockwork window" in owner.refused and not scans)
        holder.release()
        ended = _ended(owner, owner.submit(Discover()))
        check_true(
            "and takes the lock on the next job once the holder has gone",
            isinstance(ended, JobFinished) and len(scans) == 1 and not owner.refused
            and owner.status().holder is not None
            and owner.status().holder.program == "clockwork serve")
        owner.shutdown()
        owner.join(10)
        check_true("an owner that shuts down lets go of the lock",
                   read_holder(path) is None)


def check_daemon() -> None:
    """`clockwork serve` over the stand-ins, driven through its socket (lab record, task 68).

    A `--fake` daemon on ports the operating system picks, so a real daemon on this
    machine is never met, driven by `RemoteOwner` exactly as a client in another process
    would drive it: discover, send, two replicates, the events in order, and a shutdown
    from the client. Then the lock: a daemon that is not `--fake` holds it and refuses a
    second owner by name, and a daemon started under someone else's lock exits naming
    them. The daemons that are not `--fake` scan no port, clear no console port and start
    no console, so this is as safe on the instrument PC as on a bare clone. Between the
    two, the command line's round trip: `clockwork status` and `clockwork progress
    --follow` as verbs over the `--fake` daemon, each a toolbox of its own (task 73).
    """
    import contextlib
    import datetime as dt
    import io
    import json
    import tempfile
    import threading

    from clockwork import instrument as instrument_module
    from clockwork import method as method_module
    from clockwork.app import main as clockwork_main
    from clockwork.mips import Discovery
    from clockwork.owner import (
        Acquire,
        Discover,
        InstrumentLock,
        JobFinished,
        JobStarted,
        LocalOwner,
        RunDone,
        Send,
        daemon,
    )
    from clockwork.owner.lock import read_holder
    from clockwork.owner.remote import PROTOCOL, RemoteOwner

    scans = 32
    table = (f"STBLDAT;0:[A:1,0:A:1:B:1,500:B:0,{method_module.enable_fall_tick(scans)}:"
             f"A:0,{method_module.table_period(scans)}:];")
    method = method_module.from_dict({
        "schema_version": 2,
        "metadata": {"name": "daemon self-check", "created": dt.date(2026, 9, 23)},
        "acquisition": {"frames": 1, "scans": scans, "accumulations": 2,
                        "file_stem": "260923_SC_001", "repetition_mode": "per_repetition",
                        "keep_raw": True, "enable": {"box": "box1", "channel": "A"}},
        "boxes": [{"name": "box1", "port": "COM3", "setup": ["STBLCLK,EXT"],
                   "load": [table], "arm": ["SMOD,TBL"]}],
        "start": [["box1", "TBLSTRT"]],
        "reset": [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]],
    })
    instrument = instrument_module.from_dict({
        "schema_version": 1, "instrument": {"name": "daemon self-check"},
        "vertical": {"full_scale_v": 0.5, "offset_v": 0.251, "inverted": False}})

    def start(scratch: str, **options: object) -> tuple[threading.Thread, list, io.StringIO]:
        bound: list[object] = []
        codes: list[int] = []
        log = io.StringIO()
        ready = threading.Event()

        def body() -> None:
            codes.append(daemon.run(
                command="tcp://127.0.0.1:*", events="tcp://127.0.0.1:*", output=scratch,
                log_file=os.path.join(scratch, "serve.log"), stream=log,
                on_ready=lambda server: (bound.append(server), ready.set()),
                handle_signals=False, **options))
            ready.set()

        thread = threading.Thread(target=body, name="self-check daemon")
        thread.start()
        ready.wait(30)
        return thread, bound + codes, log

    def no_scan(**_: object) -> Discovery:
        return Discovery()

    with tempfile.TemporaryDirectory() as scratch:
        thread, (server, *_), log = start(scratch, fake=True)
        client = RemoteOwner(server.command_endpoint, timeout=10)
        try:
            hello = client.hello()
            check_true(f"a --fake daemon answers hello with protocol {hello.protocol}",
                       hello.protocol == PROTOCOL and hello.fake and hello.holder is None)
            finished = [_ended(client, client.submit(job), 180) for job in (
                Discover(method=method), Send(method=method),
                Acquire(method=method, instrument=instrument, initials="SC",
                        replicates=2))]
            runs = finished[-1].result if isinstance(finished[-1], JobFinished) else ()
            check_true(
                "a client in another process's shoes discovers, sends and acquires two "
                f"replicates through it ({[type(f).__name__ for f in finished]}, "
                f"{len(runs)} runs)",
                all(isinstance(f, JobFinished) for f in finished) and len(runs) == 2
                and all(run.complete and os.path.isfile(run.summed_path) for run in runs))
            handle = finished[-1].handle if finished[-1] is not None else None
            progress = client.events(handle) if handle is not None else []
            seqs = [entry.seq for entry in progress]
            check_true(
                "the series' events arrive numbered in order, started first, finished "
                "last, one RunDone per replicate",
                bool(progress) and seqs == sorted(set(seqs))
                and isinstance(progress[0].event, JobStarted)
                and isinstance(progress[-1].event, JobFinished)
                and sum(isinstance(e.event, RunDone) for e in progress) == 2)

            def verb(*words: str) -> tuple[int, str, str]:
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = clockwork_main([*words, "--endpoint", server.command_endpoint])
                return code, out.getvalue(), err.getvalue()

            # The command line's round trip (lab record, task 73): verbs in toolboxes of
            # their own, over the same daemon, reading what another client did to it.
            code, out, err = verb("status")
            said = json.loads(out) if code == 0 else {}
            check_true(
                "clockwork status, a verb over the daemon, answers JSON naming what another "
                f"client armed ({(said.get('last_armed') or {}).get('method')!r})",
                said.get("fake") is True
                and (said.get("last_armed") or {}).get("method") == "daemon self-check")
            job = str(handle.id) if handle is not None else "0"
            code, out, err = verb("progress", "--job", job, "--follow")
            last = json.loads(out.splitlines()[-1]) if code == 0 and out else {}
            check_true(
                "clockwork progress --follow reads out another client's finished job, "
                "one JSON line per event and the answer last",
                last.get("done") is True and len(last.get("runs", [])) == 2)
            code, out, err = verb("progress", "--job", "999")
            check_true(f"a refused verb is exit 1 and one sentence ({err.strip()!r})",
                       code == 1 and out == "" and err.count("\n") == 1)
            client.shutdown("the self-check is done")
            thread.join(60)
        finally:
            client.close()
            if thread.is_alive():
                server.request_shutdown("the self-check is done")
                thread.join(60)
        check_true("a client's shutdown ends the daemon cleanly",
                   not thread.is_alive() and "clockwork serve stopped" in log.getvalue())

        lock = os.path.join(scratch, "instrument.lock")
        quiet = {"fake": False, "lock_path": lock, "discover": no_scan,
                 "clear_port": False, "start_console": False}
        thread, (server, *_), log = start(scratch, **quiet)
        try:
            holder = read_holder(lock)
            second = LocalOwner(program="the clockwork window", lock_path=lock,
                                discover=no_scan)
            check_true(
                f"a daemon that is not --fake holds the lock and a second owner is refused "
                f"by name ({second.refused!r})",
                holder is not None and holder.program == "clockwork serve"
                and "clockwork serve" in second.refused)
        finally:
            server.request_shutdown("the self-check is done")
            thread.join(60)
        check_true("and lets go of it when it stops", read_holder(lock) is None)

        window = InstrumentLock("the clockwork window", lock)
        window.acquire()
        try:
            log = io.StringIO()
            code = daemon.run(command="tcp://127.0.0.1:*", events="tcp://127.0.0.1:*",
                              log_file=os.path.join(scratch, "refused.log"), stream=log,
                              handle_signals=False, **quiet)
        finally:
            window.release()
        check_true("a daemon started under someone else's lock exits 1 naming them",
                   code == 1 and "owned by the clockwork window" in log.getvalue())


def check_mcp() -> None:
    """The MCP server over the stand-ins, through the SDK's own client (lab record, task 69).

    A `--fake` owner in this process and the server built over it, driven by
    `mcp.Client` in process exactly as a Claude Code session drives it over stdio: the
    tool list is the registry, a template is rendered, the boxes are armed for a
    request, one run is acquired and followed to its end, and the file it wrote names
    the request. Then the interlock: an owner that is not `--fake` -- built with a
    stand-in scan, so nothing is opened -- and no standing limits is refused `arm`
    before any job exists (lab record, task 71).
    """
    import asyncio
    import datetime as dt
    import json
    import tempfile

    from mcp import Client

    from clockwork import summary
    from clockwork.mcp import NO_LIMITS, TOOLS, Toolbox, ToolFailure
    from clockwork.mcp.server import SIMULATED, build_server
    from clockwork.mips import Discovery
    from clockwork.owner import LocalOwner, StartConsole

    template = f"""\
template_schema = 1
renders = 2
start = [["box1", "TBLSTRT"]]
reset = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]
[knobs]
b_ticks = {{ default = 500, min = 100, max = 520, unit = "ticks", description = "line B" }}
[labels]
sample = {{ required = true, description = "what was sprayed" }}
[metadata]
name = "mcp self-check"
created = {dt.date(2026, 9, 23).isoformat()}
[acquisition]
frames = 1
scans = 32
accumulations = 2
repetition_mode = "per_repetition"
keep_raw = true
file_stem = "mcp-self-check"
enable = {{ box = "box1", channel = "A" }}
[[boxes]]
name = "box1"
port = "COM3"
setup = ["STBLCLK,EXT"]
load = ["STBLDAT;0:[A:1,0:A:1:B:1,{{b_ticks}}:B:0,532:A:0,533:];"]
arm = ["SMOD,TBL"]
"""
    request = "one self-check run with line B held for 400 ticks"
    chosen = {"template": "line-b.toml", "knobs": {"b_ticks": 400},
              "labels": {"sample": "nothing"}}

    async def drive(owner: object, library: str, output: str) -> dict:
        async with Client(build_server(owner, library, output,
                                       instrument=SIMULATED)) as client:
            async def call(name: str, **arguments: object) -> dict:
                result = await client.call_tool(name, arguments)
                if result.is_error:
                    raise RuntimeError(result.content[0].text)
                return result.structured_content

            seen: dict = {"tools": [entry.name for entry in
                                    (await client.list_tools()).tools]}
            seen["rendered"] = await call("render_template", **chosen)
            await call("discover_boxes", **chosen)
            seen["armed"] = await call("arm", request=request, initials="sc", **chosen)
            started = await call("acquire", request=request, initials="sc", **chosen)
            seen["started"] = started
            last, answer = 0, {"done": False}
            for _ in range(24):
                answer = await call("progress", job=started["job"], after=last, wait_s=5)
                last = answer["last"]
                if answer["done"]:
                    break
            seen["done"] = answer
            seen["files"] = await call("list_files")
            return seen

    with tempfile.TemporaryDirectory() as scratch:
        library = os.path.join(scratch, "library")
        output = os.path.join(scratch, "runs")
        os.makedirs(library)
        with open(os.path.join(library, "line-b.toml"), "w", encoding="utf-8",
                  newline="\n") as handle:
            handle.write(template)
        owner = LocalOwner(fake=True, program="clockwork mcp self-check").start()
        owner.submit(StartConsole())
        try:
            seen = asyncio.run(drive(owner, library, output))
        except Exception as exc:  # noqa: BLE001 -- reporting, not handling
            check_true(f"the MCP server drives a whole request over the stand-ins ({exc})",
                       False)
            seen = None
        finally:
            owner.shutdown()
            owner.join(30)
        if seen is not None:
            check_true(f"the server lists every tool in the registry ({len(TOOLS)})",
                       seen["tools"] == [entry.name for entry in TOOLS])
            check_true("render_template answers the knob values and the method's hash",
                       seen["rendered"]["ok"] and seen["rendered"]["knobs"] == {"b_ticks": 400})
            runs = seen["done"].get("runs") or []
            check_true(
                "arm, then acquire for a request, followed with progress to a complete run "
                f"({seen['done'].get('failed') or (runs[0]['text'] if runs else 'no run')})",
                seen["done"]["done"] and len(runs) == 1 and runs[0]["complete"])
            listed = seen["files"]["runs"]
            request_seen = listed[0]["request"] if listed else None
            check_true(
                f"list_files names the request each run served ({request_seen})",
                request_seen is not None and request_seen["text"] == request
                and request_seen["id"] == seen["started"]["request_id"])
            stamped = (summary.summarize(runs[0]["summed_path"])["clockwork"]
                       .get("ClockworkSeriesId") if runs else None)
            check_true("the file stamps the request's id as its series",
                       stamped == seen["started"]["request_id"])
            with open(os.path.join(output, "mcp-calls.log"), encoding="utf-8") as handle:
                lines = [json.loads(line) for line in handle]
            check_true(
                f"every call is one line of the audit log ({len(lines)} lines), the request "
                "on the acquire",
                [line["tool"] for line in lines][:2] == ["render_template", "discover_boxes"]
                and any(line["tool"] == "acquire" and line["request"]
                        and line["request"]["text"] == request for line in lines))

        def no_scan(**_: object) -> Discovery:
            return Discovery()

        real = LocalOwner(program="clockwork mcp self-check", discover=no_scan,
                          lock_path=os.path.join(scratch, "instrument.lock"))
        toolbox = Toolbox(real, library=library, output=scratch)
        try:
            toolbox.call("arm", {"request": request, "initials": "sc", **chosen})
            refusal = ""
        except ToolFailure as exc:
            refusal = str(exc)
        check_true(
            "against an owner that is not --fake and no standing limits, arm is refused by "
            "the interlock before any job is submitted",
            refusal == NO_LIMITS and real.status().queued == ()
            and real.status().running is None)
        real.shutdown()
        real.serve()


def check_envelope() -> None:
    """The standing envelope and the cold-start check (lab record, task 71).

    A limits document loaded and held against a template: a render inside it passes,
    and each refusal is its own sentence -- a knob outside the range, a fixed knob
    moved, a box not allowed, the budget's next acquisition, a hand-written method
    against the instrument. Then the cold-start check over stand-in boxes read back
    with getters, as a send reads them: the one DC bias channel the method leaves
    undeclared is a refusal, and an ARB stand-in's two RF heads at 0 % drive are not.
    Last, the tools over a `--fake` owner given the limits refuse that method's `arm`
    before any send, and begin the run record of the one they accept.
    """
    import dataclasses
    import json
    import tempfile

    from clockwork import envelope
    from clockwork.envelope import HAND_WRITTEN, Ledger, check, cold_start, judge
    from clockwork.mcp import Toolbox, ToolFailure
    from clockwork.mcp.server import SIMULATED
    from clockwork.method import template as template_module
    from clockwork.mips import Box, FakeBox, read_state
    from clockwork.owner import LocalOwner, StartConsole

    fifteen = "".join(f"{channel} = 0.0\n" for channel in range(1, 15))
    template_text = """\
template_schema = 1
renders = 2
start = [["box1", "TBLSTRT"]]
reset = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]
[knobs]
b_ticks = { default = 500, min = 100, max = 520, unit = "ticks", description = "line B" }
level_v = { default = 5.0, min = 0.0, max = 20.0, unit = "V", description = "a level" }
[metadata]
name = "envelope self-check"
created = 2026-09-24
[acquisition]
frames = 1
scans = 32
accumulations = 2
repetition_mode = "per_repetition"
keep_raw = true
file_stem = "envelope-self-check"
enable = { box = "box1", channel = "A" }
[[boxes]]
name = "box1"
port = "COM3"
setup = ["STBLCLK,EXT", "SDCB,16,{level_v}"]
load = ["STBLDAT;0:[A:1,0:A:1:B:1,{b_ticks}:B:0,532:A:0,533:];"]
arm = ["SMOD,TBL"]
[boxes.dc_bias]
""" + fifteen
    full_text = template_text.replace('"SDCB,16,{level_v}"', '"SDCB,15,0", "SDCB,16,{level_v}"')
    loaded = template_module.loads_template(template_text)
    full = template_module.loads_template(full_text)

    def entry(key: str, digest: str) -> str:
        return (f'[templates.{key}]\nhash = "{digest[:12]}"\n'
                f"[templates.{key}.knobs]\nb_ticks = {{ min = 200, max = 450 }}\n"
                f"[templates.{key}.fixed]\nlevel_v = 5.0\n")

    head = ('schema_version = 1\n[allow]\nboxes = ["box1"]\n[budget]\nmax_runs = 1\n'
            "max_replicates_per_run = 2\nmax_hours = 8\n")
    limits = envelope.loads(head + entry("fifteen", loaded.hash))
    check_true("a limits document loads and applies to the template it names",
               limits.against([loaded]) == [])

    def refused(**knobs: float) -> list[str]:
        rendered = template_module.render(loaded, knobs, {})
        return check(rendered.method, rendered, limits, Ledger(), real=True)

    check_true("a render inside the standing limits is refused nothing",
               refused(b_ticks=300) == [])
    outside = refused(b_ticks=500)
    check_true(f"a knob outside the limits is refused ({outside[:1]})",
               len(outside) == 1 and "outside the standing limits" in outside[0])
    moved = refused(b_ticks=300, level_v=6.0)
    check_true("a fixed knob moved is refused",
               len(moved) == 1 and "is fixed at 5.0 V" in moved[0])
    rendered = template_module.render(loaded, {"b_ticks": 300}, {})
    renamed = dataclasses.replace(rendered.method, boxes=(dataclasses.replace(
        rendered.method.boxes[0], name="box9"),))
    boxed = check(renamed, rendered, limits, Ledger(), real=True)
    check_true("a box the limits do not allow is refused",
               any("do not allow box9" in line for line in boxed))
    spent = check(rendered.method, rendered, limits, Ledger(runs=1), real=True)
    check_true("the budget refuses the acquisition after its last",
               len(spent) == 1 and "made its 1 acquisitions" in spent[0])
    check_true("a hand-written method is refused against the instrument, not in a "
               "rehearsal",
               check(rendered.method, None, limits, Ledger(), real=True) == [HAND_WRITTEN]
               and check(rendered.method, None, limits, Ledger(), real=False) == [])

    sequencer = Box(transport=FakeBox(rf_channels=2), name="box1")
    arb = Box(transport=FakeBox(arb_modules=4, rf_channels=2, dcb_channels=0), name="box2")
    try:
        states = [read_state(sequencer), read_state(arb)]
    finally:
        sequencer.close()
        arb.close()
    refusals, _ = judge(cold_start(rendered.method, states), "refuse")
    check_true(
        "the cold-start check refuses the one DC bias channel the method leaves "
        f"undeclared ({refusals})",
        len(refusals) == 1 and "holds 15: 0.00 V" in refusals[0])
    arb_only = dataclasses.replace(rendered.method, boxes=(dataclasses.replace(
        rendered.method.boxes[0], name="box2", setup=(), dc_bias=()),))
    check_true("an ARB box's RF heads at 0 % drive and its missing DC bias bank are not "
               "findings",
               not any(finding.setting in ("rf", "dc_bias")
                       for finding in cold_start(arb_only, states)))

    with tempfile.TemporaryDirectory() as scratch:
        library = os.path.join(scratch, "library")
        output = os.path.join(scratch, "runs")
        os.makedirs(library)
        for name, text in (("fifteen.toml", template_text), ("sixteen.toml", full_text)):
            with open(os.path.join(library, name), "w", encoding="utf-8",
                      newline="\n") as handle:
                handle.write(text)
        both = envelope.loads(head + entry("fifteen", loaded.hash)
                              + entry("sixteen", full.hash))
        owner = LocalOwner(fake=True, program="clockwork envelope self-check").start()
        owner.submit(StartConsole())
        try:
            toolbox = Toolbox(owner, library=library, output=output, instrument=SIMULATED,
                              limits=both)
            asked = {"request": "one self-check run", "initials": "sc",
                     "knobs": {"b_ticks": 300}}
            toolbox.call("discover_boxes", {"template": "fifteen.toml"})
            try:
                toolbox.call("arm", {**asked, "template": "fifteen.toml"})
                cold = ""
            except ToolFailure as exc:
                cold = str(exc)
            check_true(
                "through the tools, arm refuses the undeclared channel before any send",
                "holds 15: 0.00 V" in cold and "Nothing was sent" in cold
                and toolbox.call("status")["last_armed"] is None)
            armed = toolbox.call("arm", {**asked, "template": "sixteen.toml",
                                         "plan": "arm once to prove the record"})
            with open(armed["record"], encoding="utf-8") as handle:
                written = json.load(handle)
            check_true(
                "the arm it accepts begins the request's run record beside the files",
                written["request"]["text"] == asked["request"]
                and written["plans"][0]["text"] == "arm once to prove the record"
                and written["arms"][0]["template"]["knobs"]["b_ticks"] == 300)
        except Exception as exc:  # noqa: BLE001 -- reporting, not handling
            check_true(f"the tools apply the standing envelope over the stand-ins ({exc})",
                       False)
        finally:
            owner.shutdown()
            owner.join(30)


def check_routines() -> None:
    """Instrument routines over the stand-ins (lab record, task 76).

    A public fixture routine, written here, run by `run_routine` over a `--fake` owner:
    one template, two files, judged on `ion_events`. The stand-in console's per-push
    spectrum is three separate one-bin events in fifteen pushes of every sixteen, so the
    run has exactly 2.8125 events per push, and the fixture holds it to that within 1 %,
    to the pair's own spread and, the second time, to the last passing run. Then an
    audit routine that acquires nothing: the one DC bias channel the template declares
    reads back 0 V before any send and 5 V after, so the audit fails, then passes. Last,
    limits that do not list the template turn the refused arm into `could not judge`.
    """
    import json
    import tempfile

    from clockwork import envelope, routine, summary
    from clockwork.mcp import Toolbox, ToolFailure
    from clockwork.mcp.server import SIMULATED
    from clockwork.owner import LocalOwner, StartConsole

    template = """\
template_schema = 1
renders = 2
start = [["box1", "TBLSTRT"]]
reset = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]
[labels]
sample = { required = true, description = "what was sprayed" }
[metadata]
name = "routine self-check"
created = 2026-09-24
[acquisition]
frames = 1
scans = 32
accumulations = 2
repetition_mode = "per_repetition"
keep_raw = true
file_stem = "routine-self-check"
enable = { box = "box1", channel = "A" }
[[boxes]]
name = "box1"
port = "COM3"
setup = ["STBLCLK,EXT"]
load = ["STBLDAT;0:[A:1,0:A:1:B:1,500:B:0,532:A:0,533:];"]
arm = ["SMOD,TBL"]
[boxes.dc_bias]
16 = 5.0
"""
    check = """\
routine_schema = 1
[routine]
name = "fixture-check"
description = "two stand-in files, judged on their events"
[acquire]
template = "fixture.toml"
labels = { sample = "nothing" }
replicates = 2
[[measure]]
name = "events"
function = "ion_events"
file = "raw"
[[criterion]]
name = "beam"
value = "events.events_per_push"
at_least = 0.5
unmet = "no beam"
[[criterion]]
name = "events per push"
value = "events.events_per_push"
reference = "golden"
golden = 2.8125
factor = 1.01
[[criterion]]
name = "the pair agrees"
value = "events.events_per_push"
reference = "pair"
factor = 1.01
[[criterion]]
name = "height, last pass"
value = "events.height.p50"
reference = "last-passing"
factor = 1.05
[report]
show = ["events.occupancy"]
"""
    audit = """\
routine_schema = 1
[routine]
name = "fixture-audit"
unattended = true
[[audit]]
name = "stack"
document = "fixture.toml"
[[criterion]]
value = "stack.differences"
at_most = 0
"""
    try:
        loaded = routine.loads(check)
        check_true("a routine document loads, with its criteria in words",
                   loaded.criteria[1].describe() == "within 1.01x of 2.8125")
    except routine.RoutineError as exc:
        check_true(f"a routine document loads ({exc})", False)
        return
    try:
        routine.loads(check.replace('function = "ion_events"', 'function = "occupancy"'))
        refused = ""
    except routine.RoutineError as exc:
        refused = str(exc)
    check_true("a routine naming a function that does not exist is refused by name",
               "'occupancy' is not one of" in refused)

    with tempfile.TemporaryDirectory() as scratch:
        library = os.path.join(scratch, "library")
        routines = os.path.join(scratch, "routines")
        output = os.path.join(scratch, "runs")
        for folder, name, text in ((library, "fixture.toml", template),
                                   (routines, "fixture-check.toml", check),
                                   (routines, "fixture-audit.toml", audit)):
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, name), "w", encoding="utf-8",
                      newline="\n") as handle:
                handle.write(text)
        owner = LocalOwner(fake=True, program="clockwork routine self-check").start()
        owner.submit(StartConsole())
        try:
            toolbox = Toolbox(owner, library=library, output=output, instrument=SIMULATED)
            check_true("the routine directory defaults to routines beside the library",
                       toolbox.routines == routines)
            cold = toolbox.call("run_routine", {"name": "fixture-audit", "initials": "sc"})
            check_true(
                "an audit routine before any send finds the declared channel at 0 V and "
                f"fails ({cold['reason']})",
                cold["verdict"] == "fail" and cold["audit"]["stack"]["rows"][0]["held"] == 0.0)
            first = toolbox.call("run_routine", {"name": "fixture-check", "initials": "sc"})
            check_true(f"run_routine arms, acquires two files and judges them: {first['text']}",
                       first["verdict"] == "pass" and len(first["files"]) == 2)
            events = summary.ion_events(first["files"][0]["raw_path"])
            check_true(
                "ion_events counts the stand-in's spectrum exactly: 2.8125 events per push, "
                "15/16 of pushes occupied",
                events["events_per_push"] == 2.8125 and events["occupancy"] == 15 / 16)
            second = toolbox.call("run_routine", {"name": "fixture-check", "initials": "sc"})
            compared = next(entry for entry in second["criteria"]
                            if entry["reference"] == "last-passing")
            check_true("the second run is compared with the first, its last pass",
                       second["verdict"] == "pass" and compared["met"] is True
                       and compared["reference_request"] == first["request_id"])
            warm = toolbox.call("run_routine", {"name": "fixture-audit", "initials": "sc"})
            check_true("after a send the audit finds the stack held, and passes",
                       warm["verdict"] == "pass")
            with open(first["record"], encoding="utf-8") as handle:
                written = json.load(handle)
            check_true("the routine's report is in its request's run record",
                       written["routines"][0]["verdict"] == "pass"
                       and written["request"]["text"].startswith("routine fixture-check"))
            limited = Toolbox(owner, library=library, output=output, instrument=SIMULATED,
                              limits=envelope.loads(
                                  'schema_version = 1\n[allow]\nboxes = ["box1"]\n'
                                  "[budget]\nmax_runs = 5\nmax_replicates_per_run = 2\n"
                                  "max_hours = 8\n"))
            report = limited.call("run_routine", {"name": "fixture-check", "initials": "sc"})
            check_true(
                "limits that do not list the template make the routine could-not-judge, "
                "with the refused arm as the reason",
                report["verdict"] == "could not judge"
                and "not in the standing limits" in report["reason"])
            try:
                toolbox.call("run_routine", {"name": "no-such-routine", "initials": "sc"})
                unknown = ""
            except ToolFailure as exc:
                unknown = str(exc)
            check_true("an unknown routine is refused with the names of those there are",
                       "fixture-audit, fixture-check" in unknown)
        except Exception as exc:  # noqa: BLE001 -- reporting, not handling
            check_true(f"routines run over the stand-ins ({exc})", False)
        finally:
            owner.shutdown()
            owner.join(30)


def _ended(owner: object, handle: object, timeout: float = 10.0) -> object | None:
    """The `JobFinished` or `JobFailed` of `handle`, polled for; None on a timeout."""
    import time as _time

    from clockwork.owner import JobFailed, JobFinished

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        for entry in owner.events(handle):  # type: ignore[attr-defined]
            if isinstance(entry.event, (JobFinished, JobFailed)):
                return entry.event
        _time.sleep(0.01)
    return None


def declared_versions() -> dict[str, str]:
    """The version as each file that hand-carries it states it.

    Nothing derives one from another; they only agree because someone keeps them
    agreeing, which is why this is checked rather than trusted. The Inno Setup
    script joins the set once packaging exists.
    """
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as handle:
        pyproject = tomllib.load(handle)["project"]["version"]
    out = {"pyproject.toml": pyproject}
    iss = os.path.join(ROOT, "packaging", "clockwork.iss")
    if os.path.isfile(iss):
        found = re.search(r'^#define\s+MyAppVersion\s+"([^"]+)"',
                          open(iss, encoding="utf-8").read(), re.MULTILINE)
        out["packaging/clockwork.iss"] = found.group(1) if found else "(not found)"
    return out


LOWER_LAYERS = ("clockwork.mips", "clockwork.acq", "clockwork.method",
                "clockwork.method.template", "clockwork.instrument", "clockwork.transcript",
                "clockwork.naming", "clockwork.owner", "clockwork.owner.wire",
                "clockwork.owner.remote", "clockwork.owner.daemon", "clockwork.summary",
                "clockwork.mcp", "clockwork.mcp.server", "clockwork.mcp.cli",
                "clockwork.envelope", "clockwork.record", "clockwork.routine",
                "clockwork.report", "clockwork.keep")
QT_PREFIXES = ("PySide6", "PyQt", "pyqtgraph", "shiboken")

# --- opaque lab references ------------------------------------------------------------
#
# A reader of a public clone must never meet a citation they cannot follow. Everything
# shipped here -- code, documents, packaging, tools, tests -- cites the development
# record by task number ("lab record, task 32") and never by a path that resolves only
# in the lab repository. This is scanned rather than remembered, because it is the kind
# of rule a task lands twenty violations of in one afternoon without noticing.
#
# `CLAUDE.md` is deliberately not scanned: it is the file that states this rule, and
# saying which repository the record lives in is its job.

SHIPPED_ROOTS = ("src", "docs", "tools", "tests", "packaging", "README.md")
SHIPPED_SUFFIXES = (".py", ".md", ".ps1", ".iss", ".spec", ".svg", ".toml", ".txt", ".cfg")
SHIPPED_SKIP = ("__pycache__", ".venv", ".git", "dist", "build",
                # Gitignored build-time output under packaging/ (tools/warm_numba_cache.py,
                # tools/stage_console.py): never shipped in a git clone, so a citation
                # inside one -- the lab's own config.txt, staged verbatim, cites lab
                # notes freely -- is not a public-repo violation to scan for.
                "numba_cache_seed", "console_payload")

LAB_DIRECTORIES = ("notes", "tasks", "explorations", "golden", "literature", "vendor",
                   "falkor")
"""Top-level directories that exist only in the lab repository.

Assembled into a pattern rather than written out as one so that this file does not
match its own source: a checker that flags itself is a checker nobody keeps.
"""

LAB_DIRECTORY_PATH = re.compile(r"(?<![\w./-])(?:" + "|".join(LAB_DIRECTORIES) + r")/")
SIBLING_LAB_PATH = re.compile(r"\.\./[A-Za-z0-9_]+-lab/")
"""A path reaching into a sibling lab checkout.

The trailing separator is the whole point. `clockwork.lab_dir` documents its own
resolution order and has to name the sibling repository to do it; what the rule
forbids is naming *material inside* one.
"""

DOCUMENT_CITATION = re.compile(r"(?<![\w:/.-])([A-Za-z0-9][\w./-]*\.md)\b")
"""A citation of a Markdown document, wherever in a line it appears.

The lookbehind is what keeps a URL out of it: in `https://host/a/b.md` every place
a match could otherwise start is preceded by a dot or by a separator.
"""


def shipped_files() -> list[str]:
    """Every text file a public clone ships, as paths relative to the root."""
    out = []
    for entry in SHIPPED_ROOTS:
        start = os.path.join(ROOT, entry)
        if os.path.isfile(start):
            out.append(entry)
            continue
        for here, dirs, names in os.walk(start):
            dirs[:] = [d for d in dirs if d not in SHIPPED_SKIP]
            for name in names:
                if name.endswith(SHIPPED_SUFFIXES):
                    out.append(os.path.relpath(os.path.join(here, name), ROOT))
    return sorted(path.replace(os.sep, "/") for path in out)


def documents_here() -> tuple[set[str], set[str]]:
    """Every Markdown document in this clone, by path and by bare name."""
    paths, names = set(), set()
    for here, dirs, found in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SHIPPED_SKIP]
        for name in found:
            if name.endswith(".md"):
                rel = os.path.relpath(os.path.join(here, name), ROOT)
                paths.add(rel.replace(os.sep, "/"))
                names.add(name)
    return paths, names


def lab_side_references() -> list[str]:
    """Every citation in the shipped files that a public clone cannot follow.

    Two kinds. A path under one of the lab repository's own directories, or into a
    sibling `*-lab` checkout, is one outright. A document citation that resolves to no
    file in this clone is the other, and is what catches a lab note cited by its bare
    name, with the directory dropped on the way past.
    """
    paths, names = documents_here()
    out = []
    for rel in shipped_files():
        with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        here = os.path.dirname(rel)
        for number, line in enumerate(text.splitlines(), 1):
            for pattern in (LAB_DIRECTORY_PATH, SIBLING_LAB_PATH):
                found = pattern.search(line)
                if found:
                    out.append(f"{rel}:{number} {found.group(0)!r}")
            for cited in DOCUMENT_CITATION.findall(line):
                if cited in paths or os.path.normpath(
                        os.path.join(here, cited)).replace(os.sep, "/") in paths:
                    continue
                if "/" not in cited and cited in names:
                    continue
                out.append(f"{rel}:{number} {cited!r}")
    return out


def isolate_run_pointer() -> None:
    """Send this run's mainspring run pointer to a scratch file of its own.

    The sections below acquire, which publishes the file being written as the run in
    progress (lab record, task 58). On an instrument PC that pointer is the real one,
    read by whatever mainspring the operator has open, so a self-check run during an
    acquisition would drag their window onto a stand-in's invented spectrum and then
    withdraw the pointer the real run had published. The variable is mainspring's own
    override, meant for exactly this; the name is imported rather than spelled out.
    """
    import atexit
    import shutil
    import tempfile

    from mainspring.interface import LIVE_POINTER_ENV, LIVE_POINTER_NAME

    scratch = tempfile.mkdtemp(prefix="clockwork-self-check-")
    atexit.register(shutil.rmtree, scratch, True)
    os.environ[LIVE_POINTER_ENV] = os.path.join(scratch, LIVE_POINTER_NAME)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    isolate_run_pointer()

    section("package and versions")
    import clockwork

    check_true("clockwork imports and carries a version", bool(clockwork.__version__))
    declared = declared_versions() | {"clockwork.__version__": clockwork.__version__}
    check_true(
        "every version declaration agrees ("
        + ", ".join(f"{where} {what}" for where, what in declared.items()) + ")",
        len(set(declared.values())) == 1,
    )
    from importlib import metadata

    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as handle:
        wanted = next((spec for spec in tomllib.load(handle)["project"]["dependencies"]
                       if re.match(r"mcp\b", spec)), "")
    try:
        importlib.import_module("mcp.server")
        installed = metadata.version("mcp")
    except Exception as exc:  # noqa: BLE001 -- reporting, not handling
        installed = f"(not importable: {exc!r})"
    pinned = re.search(r"~=\s*(\d+)\.", wanted)
    check_true(
        f"the MCP SDK imports and is the major version pyproject.toml asks for "
        f"({installed} against {wanted!r})",
        bool(pinned) and installed.split(".")[0] == pinned.group(1))

    payload_dir = os.path.join(ROOT, "packaging", "console_payload")
    app_h = os.path.join(payload_dir, "app.h")
    if not os.path.isfile(app_h):
        skip("the staged console build matches the commit tools/stage_console.py pins",
             "no console payload staged (tools/stage_console.py); a public clone, or a "
             "build made before one, ships clockwork alone")
    else:
        pin_text = open(os.path.join(ROOT, "tools", "stage_console.py"),
                        encoding="utf-8").read()
        pinned = re.search(r'^EXPECTED_CONSOLE_COMMIT = "([^"]*)"', pin_text, re.MULTILINE)
        staged = re.search(r'^\s*#define\s+GIT_COMMIT_HASH\s+"([^"]*)"',
                           open(app_h, encoding="utf-8").read(), re.MULTILINE)
        check_true(
            "the staged console build matches the commit tools/stage_console.py pins "
            f"({staged.group(1) if staged else '(not found)'!r} vs "
            f"{pinned.group(1) if pinned else '(not found)'!r})",
            bool(pinned) and bool(staged) and pinned.group(1) == staged.group(1),
        )

    section("module layout and the no-Qt seam")
    for name in LOWER_LAYERS + ("clockwork.app",):
        try:
            importlib.import_module(name)
            check_true(f"{name} imports", True)
        except Exception as exc:  # noqa: BLE001 -- reporting, not handling
            check_true(f"{name} imports ({exc!r})", False)
    probe = (
        "import importlib, sys\n"
        f"for n in {LOWER_LAYERS!r}: importlib.import_module(n)\n"
        f"print(','.join(sorted(m for m in sys.modules if m.startswith({QT_PREFIXES!r}))))\n"
    )
    leaked = subprocess.run([sys.executable, "-c", probe], check=True,
                            capture_output=True, text=True).stdout.strip()
    check_true("the lower layers import without pulling in Qt"
               + (f" (leaked: {leaked})" if leaked else ""), leaked == "")

    section("lab-directory resolution")
    lab = clockwork.lab_dir()
    if lab is None:
        skip("lab repo resolves",
             "no lab checkout beside this clone; a public clone is expected to lack it")
    else:
        check_true(f"lab repo resolves to a task system ({lab})",
                   os.path.isfile(os.path.join(lab, "tasks", "README.md")))
        for sub in ("tasks", "notes"):
            check_true(f"lab_dir({sub!r}) resolves", clockwork.lab_dir(sub) is not None)
    try:
        clockwork.lab_dir("not-a-lab-directory")
        check_true("lab_dir rejects an unknown directory name", lab is None)
    except ValueError:
        check_true("lab_dir rejects an unknown directory name", True)

    section("opaque lab references")
    shipped = shipped_files()
    check_true(f"there are shipped files to scan ({len(shipped)})", len(shipped) > 20)
    found = lab_side_references()
    check_true(
        "nothing shipped cites the lab record by a path only a lab checkout resolves"
        + ("\n     " + "\n     ".join(found) if found else ""),
        not found,
    )

    section("method file")
    from clockwork import method

    sample = (
        f"schema_version = {method.SCHEMA_VERSION}\n\n"
        'start = [["box2", "TARBTRG"], ["box1", "TBLSTRT"]]\n'
        'reset = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]\n\n'
        '[metadata]\nname = "check_public-sample"\ncreated = 2026-09-06\n\n'
        "[acquisition]\nframes = 1\nscans = 100\naccumulations = 10\n"
        'repetition_mode = "per_repetition"\nkeep_raw = true\n'
        'file_stem = "check_public-sample"\n\n'
        '[[boxes]]\nname = "box1"\nport = "COM3"\nsetup = ["STBLCLK,EXT"]\n'
        'load = ["STBLDAT;0:[A:1,100:];"]\narm = ["SMOD,TBL"]\n\n'
        '[[boxes]]\nname = "box2"\nport = "COM4"\nsetup = ["SWFREQ,1,15000"]\n'
    )
    m = method.loads(sample)
    check_true("a sample method document loads", m.metadata.name == "check_public-sample")
    check_true("dumps then loads round-trips the method", method.loads(method.dumps(m)) == m)
    check_true(
        "the start sequence keeps its cross-box order",
        [(step.box, step.command) for step in m.start]
        == [("box2", "TARBTRG"), ("box1", "TBLSTRT")],
    )
    check_true(
        "one console frame per repetition is one ion mobility experiment long "
        f"(frame_length {m.acquisition.frame_length}, {m.acquisition.console_frames} frames)",
        m.acquisition.frame_length == m.acquisition.scans
        and m.acquisition.console_frames == m.acquisition.accumulations,
    )
    single = method.loads(sample.replace("per_repetition", "single_frame"))
    check_true(
        "and one frame per method frame is the whole thing "
        f"(frame_length {single.acquisition.frame_length})",
        single.acquisition.frame_length
        == single.acquisition.scans * single.acquisition.accumulations
        and single.acquisition.console_frames == 1,
    )
    warned = method.loads(sample.replace('"SWFREQ,1,15000"', '"SWFREQ,1,15000\\t"'))
    check_true(
        "a string with trailing whitespace is stripped and warned about",
        warned.boxes[1].setup == ("SWFREQ,1,15000",) and len(warned.warnings) == 1,
    )
    stamp = method.stamp(m, console_version="0.0.0-check")
    check_true(
        "stamp() carries a hash, text and versions",
        stamp["method_hash"] and stamp["method_text"] and stamp["clockwork_version"],
    )
    check_true(
        "the stamp hash covers the start sequence",
        method.stamp(method.loads(sample.replace('["box2", "TARBTRG"], ', "")))["method_hash"]
        != stamp["method_hash"],
    )
    try:
        method.loads(f"schema_version = {method.SCHEMA_VERSION}\n")
        check_true("an incomplete method document is rejected", False)
    except method.MethodError:
        check_true("an incomplete method document is rejected", True)
    try:
        method.loads(
            sample.replace(f"schema_version = {method.SCHEMA_VERSION}", "schema_version = 1")
        )
        check_true("a schema-1 method document is rejected", False)
    except method.MethodError as exc:
        check_true(
            "a schema-1 method document is rejected, saying why",
            "not supported" in str(exc),
        )

    section("method templates")
    # A template is a method with holes in its strings and the knobs that fill them
    # (docs/template-file-format.md). The sample above with two holes cut in box1's table:
    # rendered at its defaults it must be that method, string for string and by hash, which
    # is the anchoring test every template written from an existing method gets.
    from clockwork.acq import loop as acq_loop
    from clockwork.method import template as templates

    template_sample = (
        "template_schema = 1\nrenders = 2\n\n"
        'start = [["box2", "TARBTRG"], ["box1", "TBLSTRT"]]\n'
        'reset = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]\n\n'
        "[knobs]\n"
        'cycles = { default = 1, min = 1, max = 100, unit = "" }\n'
        'length_ms = { default = 10.0, min = 1.0, max = 100.0, unit = "ms" }\n\n'
        "[labels]\nsample = { required = true }\n\n"
        "[constants]\ntick_us = 100.0\n\n"
        '[derive]\nend_tick = "round(length_ms * 1000 / tick_us)"\n\n'
        '[marks]\nend = { ms = "end_tick * tick_us / 1000", '
        'description = "the table ends; an event at tick n is taken to fall in record n" }\n\n'
        '[metadata]\nname = "check_public-sample"\ncreated = 2026-09-06\n\n'
        "[acquisition]\nframes = 1\nscans = 100\naccumulations = 10\n"
        'repetition_mode = "per_repetition"\nkeep_raw = true\n'
        'file_stem = "check_public-sample"\n\n'
        '[[boxes]]\nname = "box1"\nport = "COM3"\nsetup = ["STBLCLK,EXT"]\n'
        'load = ["STBLDAT;0:[A:{cycles},{end_tick}:];"]\narm = ["SMOD,TBL"]\n\n'
        '[[boxes]]\nname = "box2"\nport = "COM4"\nsetup = ["SWFREQ,1,15000"]\n'
    )
    t = templates.loads_template(template_sample)
    rendered = templates.render(t, labels={"sample": "check"})
    check_true(
        "a template renders at its defaults to the method it was written from, by hash",
        rendered.method == m
        and method.stamp(rendered.method)["method_hash"] == stamp["method_hash"],
    )
    check_true(
        "its mark is a time and an expected scan (10 ms is scan 100 at a 100 us tick)",
        rendered.marks[0].ms == 10.0 and rendered.marks[0].scan == 100
        and rendered.tick_us == 100.0,
    )
    turned = templates.render(t, {"length_ms": 12.5, "cycles": 3}, {"sample": "check"})
    check_true(
        "turning a knob moves every hole it reaches",
        turned.method.boxes[0].load == ("STBLDAT;0:[A:3,125:];",)
        and turned.derived == {"end_tick": 125},
    )
    check_true(
        "the acquisition loop's refusals and cautions do not change on a rendered method",
        acq_loop.refusals(rendered.method) == acq_loop.refusals(m)
        and acq_loop.cautions(rendered.method) == acq_loop.cautions(m),
    )
    check_true(
        "a hole's number is written in its shortest form at four decimals",
        templates.format_number(208.0) == "208" and templates.format_number(16.7628) == "16.7628"
        and templates.format_number(16.76284) == "16.7628",
    )
    try:
        templates.render(t, {"length_ms": 500.0}, {"sample": "check"})
        check_true("a knob outside its declared range is refused", False)
    except templates.TemplateError as exc:
        check_true(
            "a knob outside its declared range is refused, naming the range",
            "outside the range" in str(exc) and "1 to 100 ms" in str(exc),
        )
    unrounded = templates.loads_template(
        template_sample.replace("round(length_ms * 1000 / tick_us)", "length_ms * 1000 / tick_us")
    )
    try:
        templates.render(unrounded, {"length_ms": 12.55}, {"sample": "check"})
        check_true("a tick count that is not whole is refused", False)
    except templates.TemplateError as exc:
        check_true(
            "a tick count that is not whole is refused, asking for round()",
            "round() its derivation" in str(exc),
        )
    try:
        templates.loads_template(template_sample.replace("/ tick_us)", "/ period_us)"))
        check_true("a name nothing defines is refused when the template is loaded", False)
    except templates.TemplateError as exc:
        check_true(
            "a name nothing defines is refused when the template is loaded",
            "no knob, constant or derivation is named 'period_us'" in str(exc),
        )
    try:
        method.loads(template_sample)
        check_true("the method loader refuses a template, saying what it is", False)
    except method.MethodError as exc:
        check_true(
            "the method loader refuses a template, saying what it is",
            "method template" in str(exc),
        )
    check_true(
        "a template's hash does not depend on line endings",
        templates.loads_template(template_sample.replace("\n", "\r\n")).hash == t.hash,
    )

    section("pane text")
    from clockwork.method import text as pane_text

    check_true(
        "the command table classifies a load, an arm, a start and an unknown word",
        [pane_text.classify(command) for command in
         ("STBLDAT;0:[A:1,100:];", "SARBCTBL,J10[HRr]1", "SMOD,TBL", "SMOD,LOC",
          "TARBTRG", "TBLSTRT", "SWFREQ,1,15000", "SNEVERHEARDOFIT,1", "# a note")]
        == ["load", "load", "arm", "setup", "start", "start", "setup", "setup",
            "comment"],
    )
    check_true(
        "prose in a pane is unplaced rather than sent as setup",
        [line.text for line in
         pane_text.parse_pane("SWFREQ,1,1\nIon Mobility Scans = 5000", "box1").unplaced]
        == ["Ion Mobility Scans = 5000"],
    )
    panes = {box.name: pane_text.parse_pane(
        pane_text.render_pane(box, m.start, m.reset), box.name) for box in m.boxes}
    check_true(
        "every box's phases survive a trip out to pane text and back",
        all(panes[box.name].setup == box.setup and panes[box.name].load == box.load
            and panes[box.name].arm == box.arm for box in m.boxes),
    )
    check_true(
        "and the start order derived from the panes is the method's own",
        pane_text.start_order(panes) == m.start,
    )
    check_true(
        "a reset written as a trainee states it comes back as the list a replicate sends",
        [step.command for step in pane_text.parse_pane(
            "STBLDAT;0:[A:1,100:];\n\nSMOD,TBL\n\nTBLSTRT\n\nSMOD,LOC",
            "box1").reset] == ["SMOD,LOC", "SMOD,TBL"],
    )
    commented = method.loads(sample.replace(
        'setup = ["STBLCLK,EXT"]', 'setup = ["# the clock source", "STBLCLK,EXT"]'))
    check_true(
        "a comment loads as a string, warns about nothing, and changes the stamp",
        commented.boxes[0].setup == ("# the clock source", "STBLCLK,EXT")
        and commented.warnings == ()
        and method.stamp(commented)["method_hash"] != stamp["method_hash"],
    )

    section("MIPS serial")
    from clockwork import mips

    fake = mips.FakeBox(name="check_public-box")
    box = mips.Box(transport=fake, name="fake")
    check_true(f"a simulated box answers GVER ({box.version()})", bool(box.version()))
    check_true("and GNAME", box.box_name() == "check_public-box")

    example = "STBLDAT;25:[A:10,10:A:1,25:A:0:5:34.5,100:];"
    load = box.send_table(example)
    check_true(
        f"a table loads and is ACKed ({load.bytes_sent} bytes, "
        f"{load.predicted.byte_size} on the box)",
        load.prediction_error is None,
    )
    check_true("TBLRPT reads back the table that was sent", box.verify_table(load) == [])

    # `[A:1,` opens a table named 'A' and drives nothing; a reader sees an event raising
    # DIOA. Asking the compiled table rather than the string is the only way to tell.
    named_loop = mips.compile_table("STBLDAT;0:[A:1,0:B:1,500:B:0,5001:A:0,5002:];")
    raises_it = mips.compile_table("STBLDAT;0:[A:1,0:A:1:B:1,500:B:0,5500:A:0,5501:];")
    check_true(
        "a loop header is not read as an event on the line it is named after",
        mips.digital_events(named_loop, "A") == ((0, 5001, "0"),)
        and mips.digital_events(raises_it, "A") == ((0, 0, "1"), (0, 5500, "0")),
    )

    # A compression table's `]N` is the third place the accumulation count is written
    # (lab record, task 31). The walk mirrors the firmware's: an op, the digits that may
    # follow it, and the one raw character five of the ops then take, which is why `HR`
    # and `m1C` cannot be counted by looking for brackets alone (§6.6).
    check_true(
        "a compression table's pass count is read off the string",
        mips.compression_passes("SARBCTBL,J10[HRsm1CD12m1ND4.0272r]100") == (100,)
        and mips.compression_passes(
            "SARBCTBL,J30[HRsD90m3CD10m3ND208rD16.7628sD10.0253r]100") == (100,)
        and mips.compression_passes("SARBCTBL,J10[HRr]") == (1,)
        and mips.compression_passes("SARBCTBL,HR") == (),
    )

    # A long table has to be chunked: the box's 4096-byte input buffer drops
    # what overruns it without saying so (docs/mips-wire-format.md §1).
    long_events = ",".join(f"{tick}:A:1" for tick in range(100, 2000, 2))
    long_table = f"STBLDAT;0:[A:1,{long_events},4000:];"
    long_load = box.send_table(long_table)
    check_true(
        f"a table of {len(long_table)} bytes streams without losing characters",
        fake.dropped_bytes == 0 and box.verify_table(long_load) == [],
    )

    # `SDIO` is accepted in every mode and only moves a line in LOC: in table mode
    # the latch belongs to the table's timer, so the write is staged and `GDIO`,
    # which answers from the image, reports it as done anyway (§4).
    box.set_dio("A", True)
    moved_in_local = fake.dio_pins["A"] and box.command("GDIO,A", value=True) == "1"
    box.arm()
    box.set_dio("A", False)
    check_true(
        "SDIO moves a line in local mode, is staged in table mode, and reads back as "
        "done either way",
        moved_in_local and fake.dio_pins["A"] and box.command("GDIO,A", value=True) == "0",
    )
    try:
        mips.dio_command("Q", True)
        check_true("a digital input is refused before it reaches a box", False)
    except ValueError:
        check_true("a digital input is refused before it reaches a box", True)
    box.local()
    box.arm()

    box.trigger()
    seen = box.drain(0.05)
    check_true(
        "arming and a pass report TBLRDY, TBLTRIG, TBLCMPLT and the re-arm ("
        + ", ".join(event.name for event in seen) + ")",
        seen == [mips.TableEvent.TRIGGERED, mips.TableEvent.COMPLETE, mips.TableEvent.READY],
    )
    try:
        box.command("NOSUCHCMD")
        check_true("a bad command is rejected with the box's own error code", False)
    except mips.BoxRejected as exc:
        check_true(
            f"a bad command is rejected with the box's own error code ({exc.code})",
            exc.code == 1,
        )

    # The ARB half of the same stand-in. An instrument ARB box takes a block of
    # these once and is read back afterwards; a box with no modules refuses
    # them with the firmware's own code for it (docs/mips-wire-format.md §6).
    arb = mips.Box(transport=mips.FakeBox(arb_modules=4), name="fake-arb")
    for setting in ("SWFREQ,1,15000", "SWFVRNG,1,15", "SALTWFM,1,REV", "ARBSYNC"):
        arb.command(setting)
    check_true(
        "an ARB setup block is accepted and reads back "
        f"({arb.command('GWFREQ,1', value=True)} Hz, "
        f"{arb.command('GALTWFM,1', value=True)})",
        # 14914 and not 15000: a module's waveform clock is an integer divider and
        # `GWFREQ` reports what it could make of the request (wire format 6.2).
        arb.command("GWFREQ,1", value=True) == "14914"
        and arb.command("GALTWFM,1", value=True) == "REV",
    )
    table = "J10[HRsm1CD12m1ND4.0272r]100"
    arb.command(f"SARBCTBL,{table}")
    check_true(
        "a compression table reads back byte for byte, which is the only check "
        "there is of one (§6.6)",
        arb.command("GARBCTBL", value=True) == table,
    )
    try:
        mips.Box(transport=mips.FakeBox()).command("SWFREQ,1,15000")
        check_true("a box with no ARB modules refuses an ARB command", False)
    except mips.BoxRejected as exc:
        check_true(
            f"a box with no ARB modules refuses an ARB command ({exc.code}, "
            f"{mips.error_text(exc.code)})",
            exc.code == 115,
        )

    section("acquisition console")
    from clockwork import acq

    request = acq.FrameRequest(
        frame_length=5000, file_name="check_public-sample.uimf", frame_number=2,
        nbr_accumulations=100, offset_bins=20000,
    )
    check_true(
        "a frame request round-trips through protobuf and Snappy "
        f"({len(request.encode())} bytes on the wire)",
        acq.FrameRequest.decode(request.encode()) == request,
    )
    check_true(
        "a frame past the ScanNum column's declared range is warned about, not refused",
        len(acq.FrameRequest(frame_length=500_000, offset_bins=20000).warnings()) == 1,
    )
    # The instrument's own numbers at 2 GS/s: a 129.0036 us pusher period, the
    # 10 us post-trigger delay and the 2.048 us rearm dead time.
    record = acq.record_size_samples(258007, 20000, 4096)
    check_true(
        f"the record size follows the console's arithmetic ({record} samples, "
        f"{record + 20000} with the post-trigger delay)",
        record == 233888 and record % acq.GATE_GRANULARITY_SAMPLES == 0,
    )

    with acq.FakeConsole(subscriber_wait_s=2.0) as fake:
        with acq.DataStream(fake.data_endpoint) as stream, \
                acq.Console(fake.command_endpoint, timeout=5.0) as console:
            info = console.info()
            check_true(
                f"a simulated console answers info ({info.model}, serial {info.serial})",
                info.model == "SA220P" and info.is_fork,
            )
            check_true("and num instruments", console.num_instruments() == 1)
            try:
                console.acquire_frame(acq.FrameRequest(frame_length=10))
                check_true("acquire frame before acquire is refused by the client", False)
            except acq.ConsoleStateError:
                check_true("acquire frame before acquire is refused by the client",
                           not fake.died)
            try:
                console.request("reset timestamps", timeout=0.1)
                check_true("a command the console never answers is refused, not waited on",
                           False)
            except acq.ConsoleStateError:
                check_true("a command the console never answers is refused, not waited on",
                           True)

            # The other two ways to break the ordering rule, checked without
            # sending anything: a start while an acquisition is unstopped
            # destroys a thread the console never joined, and kills it.
            sent_so_far = len(fake.commands)
            with acq.Console(fake.command_endpoint, timeout=1.0) as guard:
                guard.acquiring = guard.running = True
                refused = 0
                for attempt in (lambda: guard.acquire(timeout=1.0),
                                lambda: guard.acquire_frame(acq.FrameRequest(frame_length=10))):
                    try:
                        attempt()
                    except acq.ConsoleStateError:
                        refused += 1
                check_true(
                    "a start while an acquisition is unstopped is refused, both ways",
                    refused == 2 and not fake.died
                    and len(fake.commands) == sent_so_far,
                )

            console.configure(offset_v=0.251)
            check_true(
                "configure sends init, horizontal, vertical, invert and the enable input",
                [name for name, *_ in fake.commands[-5:]]
                == ["init", "horizontal", "vertical", "invert", "enable io port"],
            )
            width = acq.start_chain(console, stream, timeout=5.0, settle=2.0)
            check_true(
                f"acquire replies with a period ({width.pusher_pulse_width} samples) "
                f"that passes its own SHA-256, and a record of {width.num_samples}",
                width.num_samples == fake.num_samples,
            )
            check_true(
                "and the open-ended acquisition it starts is stopped and cleared away",
                console.acquiring and not console.running and stream.poll(0.1) is None,
            )
            # The bootstrap, and the default since 2026-09-14. The period measurement
            # inside `acquire` needs twenty triggers and the card counts none while the
            # enable input is held low, which is where a sequencer's DIO sits before its
            # table has ever run, so a cold instrument has no other way to open a chain.
            # The enable goes back on whatever happened (lab record, task 26).
            sent = [name for name, *_ in fake.commands]
            check_true(
                "a chain disables the enable input for the measurement and enables it "
                "again after, which is how a cold instrument opens one at all",
                sent.index("disable io port") < sent.index("acquire")
                < len(sent) - 1 - sent[::-1].index("enable io port")
                and fake.io_ports_enabled == [2],
            )

            batches: list[acq.Batch] = []
            end = acq.run_frame(
                console, stream, acq.FrameRequest(frame_length=250, offset_bins=20000),
                timeout=10.0, on_batch=batches.append,
            )
            check_true(
                f"one frame's {len(batches)} batches all arrive, then finished on its own "
                "topic",
                end.is_finished and end.topic == acq.TOPIC_STATUS
                and sum(batch.scans for batch in batches) == 250,
            )
            check_true(
                "and the frame was stopped, so the next one may start",
                not console.running and not fake.died and fake.ignored_frames == 0,
            )

            # A frame that acquired nothing ends with exactly the `finished` a
            # whole frame ends with, so a client that takes that at face value
            # reports a dead acquisition as a good one (lab record, task 20).
            fake.frame_batches = 0
            try:
                acq.run_frame(console, stream, acq.FrameRequest(frame_length=250),
                              timeout=10.0, settle=0.5)
                check_true("a frame that published no scans is not called a success", False)
            except acq.EmptyFrameError:
                check_true("a frame that published no scans is not called a success",
                           not console.running and not fake.died)
            fake.frame_error = "Invalid value (1000) for parameter nbrElementsToFetch"
            try:
                acq.run_frame(console, stream, acq.FrameRequest(frame_length=250),
                              timeout=10.0, settle=0.5)
                check_true("and an error the console publishes is raised in its own words",
                           False)
            except acq.ConsoleAcquisitionError as exc:
                check_true("and an error the console publishes is raised in its own words",
                           "nbrElementsToFetch" in str(exc))
            fake.frame_batches, fake.frame_error = None, None

            console.stop_acquire()
            check_true(
                "and stop acquire is followed by finished acquire",
                stream.wait_for_status(
                    acq.FINISHED_ACQUIRE, timeout=10.0
                ).is_finished_acquire,
            )

            # A command that fails inside the console answers with one `error`
            # frame in place of its reply, and the session goes on. A console
            # without that boundary exits instead, and the client meets the
            # exit as a request that timed out (lab record, task 21).
            fake.refuse["tof width"] = "measured pusher period is outside the believable band"
            try:
                console.tof_width(timeout=10.0)
                check_true("a command the console refuses is raised, not taken for a reply",
                           False)
            except acq.ConsoleCommandError as exc:
                check_true("a command the console refuses is raised, not taken for a reply",
                           "believable band" in str(exc))
            fake.refuse.clear()
            check_true(
                "and the refusal cost the session nothing: the next command is answered",
                console.tof_width(timeout=10.0).pusher_pulse_width
                == fake.pusher_period_samples,
            )

    section("UIMF files")
    # The whole of task 06's clockwork side without hardware: a file created with its
    # schema and parameters, a console that appends `Frame_Scans` to it and nothing
    # else, the two-phase completion marker, and the fold. The equality at the end is
    # the bench's own acceptance test for the fold, run against a stand-in whose
    # invented spectrum repeats so that it can be an equality (lab record, task 18).
    import datetime as _dt
    import tempfile

    from mainspring.uimf import SUMMED_SUFFIX, UimfFile

    from clockwork import instrument as instrument_module
    from clockwork import method as method_module
    from clockwork import mips as mips_module

    # The one name of the three the code repo's own rule names -- writer names,
    # parameter constants, launch words -- that was still retyped rather than imported,
    # until task 59. `is` rather than `==`: the point is that clockwork's module binds
    # the same object mainspring exports, not a second string that happens to match it
    # today (lab record, task 59).
    check_true(
        "the summed suffix is mainspring's own, imported rather than retyped, and "
        "summed_path builds the companion's name on it",
        acq.SUMMED_SUFFIX is SUMMED_SUFFIX
        and acq.summed_path("stem" + acq.RAW_SUFFIX) == "stem" + SUMMED_SUFFIX,
    )

    # An instrument document the way SLIMPHONY's reads: a calibration, so the file has a
    # mass axis and `CalibrationDone` is 1, and the window the acquisition ran through
    # (lab record, task 25). Every value here is one a public clone can hold; the lab's
    # own document is lab material.
    machine = instrument_module.Instrument(
        name="self-check",
        calibration=instrument_module.Calibration.from_tenths_of_ns(
            7.38123e-05, 769.0495, _dt.date(2026, 9, 9)
        ),
        vertical=instrument_module.Vertical(full_scale_v=0.5, offset_v=0.251,
                                            inverted=True),
    )
    check_true("the two forms of one calibration agree to a part in 1e9",
               abs(machine.calibration.slope - 0.738123) < 1e-9
               and abs(machine.calibration.intercept - 0.07690495) < 1e-11)

    accumulations, scans = 3, 32
    document = {
        "schema_version": method_module.SCHEMA_VERSION,
        "metadata": {"name": "self-check", "created": _dt.date(2026, 9, 10)},
        "acquisition": {"frames": 1, "scans": scans, "accumulations": accumulations,
                        "file_stem": "selfcheck"},
        "boxes": [{"name": "box1", "port": "COM1", "load": ["STBLDAT;..."]}],
        "start": [["box1", "TBLSTRT"]],
    }
    recipe = method_module.from_dict(document)
    with tempfile.TemporaryDirectory() as directory:
        with acq.FakeConsole() as fake:
            geometry = acq.Geometry.from_tof_width(
                fake.tof_width(), sample_rate_hz=2e9,
                post_trigger_samples=fake.post_trigger_samples,
            )
            with acq.DataStream(fake.data_endpoint) as stream, \
                    acq.Console(fake.command_endpoint) as console:
                console.configure(offset_v=0.251)
                acq.start_chain(console, stream, timeout=10.0, settle=2.0, quiet=0.1)
                # The snapshot a run's `send_phases` returns, made here by hand
                # because this section drives a `Recording` rather than the loop: what
                # every box was holding, as found and after setup, and the conditions
                # note nothing can read off a wire (lab record, task 40).
                state_box = mips_module.Box(
                    transport=mips_module.FakeBox(name="MIPS-A", dcb_channels=16,
                                                  rf_channels=2),
                    name="box1")
                state_snapshot = acq.Snapshot(
                    before=(mips_module.read_state(state_box),),
                    after=(mips_module.read_state(state_box),),
                    conditions="self-check, no instrument",
                )
                recording = acq.Recording.create(directory, recipe, geometry,
                                                 instrument=machine,
                                                 box_state=state_snapshot.render(),
                                                 conditions=state_snapshot.conditions)
                raw, summed = recording.raw_path, recording.summed_path
                check_true("the file exists before the first frame is asked for",
                           os.path.isfile(raw))
                with recording:
                    for repetition in range(1, accumulations + 1):
                        with recording.frame(1, repetition) as request:
                            acq.run_frame(console, stream, request, timeout=10.0)
                        check_true(
                            f"repetition {repetition} is marked complete once it ends",
                            UimfFile(raw).frame_params(repetition).marked_complete,
                        )
                    rows = recording.fold(1)

                # `keep_raw = false` under a reader that has the file open (lab
                # record, task 57). The scene is an ordinary one since the run in
                # progress is published: a mainspring with `Live` ticked follows the
                # raw file and holds it for the length of each query, and on Windows a
                # file another handle has open cannot be deleted. The handle here is
                # this process's own, which is the same refusal from the filesystem's
                # point of view and needs no viewer, no console and no box.
                discarding = method_module.from_dict({
                    **document,
                    "acquisition": {**document["acquisition"], "accumulations": 1,
                                    "keep_raw": False,
                                    "file_stem": "selfcheck-discard"},
                })
                held = acq.Recording.create(directory, discarding, geometry,
                                            instrument=machine)
                with held.frame(1, 1) as request:
                    acq.run_frame(console, stream, request, timeout=10.0)
                held.fold(1)
                with open(held.raw_path, "rb"):
                    held.close()
                if os.path.isfile(held.raw_path):
                    check_true(
                        "a raw file something holds open outlives `keep_raw = false`, "
                        "and the recording says why rather than leaving it looking "
                        f"like a file the method asked to keep ({held.discard_error})",
                        held.discard_error is not None
                        and os.path.isfile(held.summed_path),
                    )
                else:
                    skip("a raw file something holds open outlives `keep_raw = false`, "
                         "and the recording says why",
                         "this platform deletes a file that is open, so the refusal "
                         "cannot be provoked here; it is a Windows fact and the "
                         "instrument PCs are Windows")
                console.stop_acquire()

        check_true(f"the fold wrote a summed companion ({rows} rows)",
                   os.path.isfile(summed) and rows > 0)
        opened = UimfFile(summed)
        parameters = opened.frame_params(1)
        check_true("the companion is today's shape: one frame, Accumulations = A",
                   opened.frame_numbers() == [1]
                   and parameters.accumulations == accumulations
                   and parameters.scans == scans)
        check_true("the companion says which method frame it came from",
                   parameters.method_frame == 1)

        # What the run record adds to a file beyond the method (lab record, task 25).
        raw_file = UimfFile(raw)
        check_true("every frame carries a calibration, so the file has a mass axis",
                   all(raw_file.frame_params(n).calibration_done
                       for n in raw_file.frame_numbers())
                   and parameters.calibration_done)
        starts = [float(raw_file.frame_params(n).extra["StartTimeMinutes"])
                  for n in raw_file.frame_numbers()]
        # Not `starts[0] == 0.0`. The run clock's origin is the run's, not the first
        # frame's, so the first frame begins however long the setup in front of it took:
        # a fraction of a millisecond here, the whole acquisition chain in a real run.
        # The equality only ever held because `time.monotonic` ticks at 15.6 ms on
        # Windows and usually rounded that gap to nothing; when a tick landed inside it,
        # this line failed. The clock is `perf_counter` now and the gap is real, so what
        # is asserted is that the first frame starts at the origin rather than a run in.
        check_true("StartTime runs off a run clock rather than staying 0 "
                   f"({starts[0]:.6f} minutes at the first frame, {starts[-1]:.6f} at "
                   "the last)",
                   0.0 <= starts[0] < 0.001 and starts[-1] > starts[0]
                   and starts == sorted(starts))
        check_true("the summed frame starts when its first repetition did",
                   float(parameters.extra["StartTimeMinutes"]) == starts[0])
        stamped = opened.global_params().extra
        check_true("the vertical settings in force are stamped into both files",
                   stamped["ClockworkFullScale"] == "0.5"
                   and stamped["ClockworkChannelOffset"] == "0.251"
                   and stamped["ClockworkInverted"] == "1"
                   and raw_file.global_params().extra["ClockworkFullScale"] == "0.5")
        check_true("the boxes' state and the conditions note are stamped too, so a file "
                   "says what shaped the beam and not only what strings were sent "
                   "(lab record, task 40)",
                   "as found" in stamped.get("ClockworkBoxState", "")
                   and "DCB 1" in stamped.get("ClockworkBoxState", "")
                   and stamped.get("ClockworkConditions")
                   == "self-check, no instrument")
        one = UimfFile(raw).read_frame(1)
        total = opened.read_frame(1)
        exact = all(
            list(total.scan(n)[0]) == list(one.scan(n)[0])
            and list(total.scan(n)[1]) == [v * accumulations for v in one.scan(n)[1]]
            for n in range(scans)
        )
        check_true(f"and it is exactly {accumulations} times one repetition, bin for bin",
                   exact and len(one) > 0)

        # The run pointer: what a mainspring already open with `Live` ticked reads to
        # find the acquisition, published by the recording that owns the raw file and
        # withdrawn when it closes (lab record, task 58). The schema is mainspring's
        # and is imported, here as in `clockwork.acq.uimf`, so that no spelling of it
        # exists in this repository to disagree with the one in that one.
        from mainspring.interface import read_live_pointer

        published = acq.Recording.create(directory, recipe, geometry, stem="pointer",
                                         publish=True)
        named = read_live_pointer()
        check_true("a published recording names its raw file as the run in progress, "
                   "for a viewer that is already open to follow",
                   named is not None and named.path == published.raw_path
                   and named.writer == "clockwork"
                   and os.path.isfile(published.raw_path))
        published.close()
        check_true("and closing it withdraws the pointer, so a viewer is not left "
                   "following a run that has ended",
                   read_live_pointer() is None and published.live_pointer is None)
        with acq.Recording.create(directory, recipe, geometry,
                                  stem="unpublished") as quiet:
            check_true("while a recording that did not publish leaves the pointer "
                       "alone, a file not being a run",
                       quiet.live_pointer is None and read_live_pointer() is None)

        # Every writer publishes to one pointer file, so a close has to ask whether
        # what is standing there is still its own run before taking it away (lab
        # record, task 57).
        first = acq.Recording.create(directory, recipe, geometry, stem="pointer-first",
                                     publish=True)
        second = acq.Recording.create(directory, recipe, geometry, stem="pointer-second",
                                      publish=True)
        first.close()
        standing = read_live_pointer()
        check_true("and a close withdraws the pointer only while it still names its own "
                   "run, so a finished acquisition cannot take a later one's away",
                   standing is not None and standing.path == second.raw_path)
        second.close()

        # How a rendered run's file says what made it (lab record, task 66): the
        # template's hash and text, one typed parameter per knob, label and mark, the
        # pusher period the marks were counted on, and a series. The template is the
        # method-templates section's sample, turned off its defaults so the values read
        # back are the ones set rather than ones anybody would have guessed.
        import contextlib as _contextlib
        import sqlite3 as _sqlite3

        turned_render = templates.render(t, {"length_ms": 12.5}, {"sample": "check"})
        with acq.Recording.create(
            directory, turned_render.method, geometry, stem="rendered",
            provenance=acq.Provenance(
                rendered=turned_render,
                series=acq.Series("check-series", index=2, position=5, seed=99)),
        ) as rendered_recording:
            pass  # the stamp is written at creation, before any frame
        rendered_extra = UimfFile(rendered_recording.raw_path).global_params().extra
        # `closing`, because a connection's own `with` commits and leaves it open, and
        # Windows will not remove the directory around a file still open.
        with _contextlib.closing(_sqlite3.connect(rendered_recording.raw_path)) as conn:
            rendered_types = dict(conn.execute(
                "SELECT ParamName, ParamDataType FROM Global_Params"))
            rendered_ids = dict(conn.execute("SELECT ParamName, ParamID FROM Global_Params"))
        check_true(
            "a rendered run stamps its template: hash and full text",
            rendered_extra.get("ClockworkTemplateHash") == t.hash
            and rendered_extra.get("ClockworkTemplateText") == template_sample,
        )
        check_true(
            "each knob reads back as a Double equal to what was set",
            rendered_types.get("ClockworkKnobLengthMs") == "System.Double"
            and float(rendered_extra["ClockworkKnobLengthMs"]) == 12.5
            and rendered_types.get("ClockworkKnobCycles") == "System.Double"
            and float(rendered_extra["ClockworkKnobCycles"]) == 1.0,
        )
        check_true(
            "each mark reads back in ms as a Double and as an expected scan as an Int32, "
            "beside the tick it was counted on (12.5 ms is scan 125 at 100 us)",
            float(rendered_extra["ClockworkMarkEndMs"]) == 12.5
            and rendered_types.get("ClockworkMarkEndScan") == "System.Int32"
            and rendered_extra["ClockworkMarkEndScan"] == "125"
            and float(rendered_extra["ClockworkTickUs"]) == 100.0,
        )
        check_true(
            "the label and the series are stamped, the seed as an Int32",
            rendered_extra.get("ClockworkLabelSample") == "check"
            and (rendered_extra.get("ClockworkSeriesId"),
                 rendered_extra.get("ClockworkSeriesIndex"),
                 rendered_extra.get("ClockworkSeriesPosition"),
                 rendered_extra.get("ClockworkSeriesSeed")) == ("check-series", "2", "5",
                                                                "99")
            and rendered_types.get("ClockworkSeriesSeed") == "System.Int32",
        )
        check_true(
            "the per-template parameters number upward from the documented base, and the "
            "fixed ones sit below it",
            sorted(rendered_ids[name] for name in rendered_ids
                   if name.startswith(("ClockworkKnob", "ClockworkLabel", "ClockworkMark")))
            == list(range(acq.TEMPLATE_PARAM_ID_BASE, acq.TEMPLATE_PARAM_ID_BASE + 5))
            and all(rendered_ids[key.name] < acq.TEMPLATE_PARAM_ID_BASE
                    for key in (*acq.RENDER_KEYS, *acq.SERIES_KEYS)),
        )
        hand_written = UimfFile(raw).global_params().extra
        check_true(
            "a hand-written method's file carries none of them: absence means unplanned",
            not [name for name in hand_written
                 if name.startswith(("ClockworkTemplate", "ClockworkKnob", "ClockworkLabel",
                                     "ClockworkMark", "ClockworkTickUs",
                                     "ClockworkSeries"))],
        )
        try:
            acq.Recording.create(directory, recipe, geometry, stem="detached",
                                 provenance=acq.Provenance(rendered=turned_render))
            check_true("a render of some other method is refused, not stamped", False)
        except ValueError as exc:
            check_true("a render of some other method is refused, not stamped",
                       "hand-written" in str(exc)
                       and not os.path.exists(acq.raw_path(directory, "detached")))

    section("one whole acquisition")
    # Task 23's loop, which is the section above and the two before it composed: the
    # boxes loaded and armed, one console frame per repetition with the start list
    # walked into each one after `acquire frame`, the silence before the completion
    # marker, the fold, and a technical replicate. Nothing above this layer is allowed
    # to hold a second copy of that order, so this is where it is checked.
    import time as _time

    class SlowBox(mips_module.FakeBox):
        """A box whose every write costs what a USB round trip costs.

        The enable-gate guard watches the window between `acquire frame` and the end of
        the start list, and against a stand-in that answers instantly there is no
        window at all. Only the gate check below needs one.
        """

        def write(self, data: bytes) -> None:
            _time.sleep(0.25)
            super().write(data)

    # The run's first frame is held open before anything is released, so that a
    # digitizer already recording has time to publish and prove it. The instrument's
    # dwell is derived from the pusher period the console measured, about 165 ms; the
    # stand-in has no period, only the hold below, so the checks that are not about the
    # dwell run at one comfortably shorter than it.
    dwell = 0.01

    loop_document = dict(document)
    loop_document["metadata"] = {"name": "self-check loop",
                                 "created": _dt.date(2026, 9, 11)}
    loop_document["acquisition"] = dict(document["acquisition"]) | {
        "file_stem": "selfcheck-loop",
        # Which output carries the digitizer's gate is a fact about the cabling and not
        # a property of a document, so a method states it; nothing in the package knows
        # it otherwise, and the checks below read the enable's edges out of the table.
        "enable": {"box": "box1", "channel": "A"}}
    loop_document["boxes"] = [{
        "name": "box1", "port": "COM1", "setup": ["STBLCLK,EXT"],
        # DIOA and DIOB raised together at the loop's tick 0, DIOA lowered a whole
        # console batch past the last counted scan. Both numbers off `clockwork.method`
        # rather than written out, because a table that drives the digitizer's enable
        # somewhere other than where the rule says is the way a `per_repetition` frame
        # fails, and it fails looking like a cabling fault.
        "load": [f"STBLDAT;0:[A:1,0:A:1:B:1,10:B:0,"
                 f"{method_module.enable_fall_tick(scans)}:A:0,"
                 f"{method_module.table_period(scans)}:];"],
        "arm": ["SMOD,TBL"],
    }]
    loop_document["reset"] = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]
    recipe = method_module.from_dict(loop_document)

    # A `single_frame` method carries the other table shape, which is the trainee's own:
    # the loop runs on the box for `accumulations` passes of `scans` ticks and the enable
    # is raised once before it and never lowered. Both shapes are written out here
    # because a method whose table is the wrong one for its mode is refused for
    # contradicting itself (lab record, task 31), so the self-check has to hold each.
    single_frame_boxes = [dict(loop_document["boxes"][0]) | {
        "load": [f"STBLDAT;0:A:1[A:{accumulations},0:B:1,10:B:0,{scans}:];"]}]

    def single_frame_document(**acquisition: object) -> dict:
        return loop_document | {
            "boxes": single_frame_boxes,
            "acquisition": dict(loop_document["acquisition"])
            | {"repetition_mode": "single_frame", "frames": 2} | acquisition,
        }

    ungoverned = single_frame_document()
    ungoverned["acquisition"] = {key: value
                                 for key, value in ungoverned["acquisition"].items()
                                 if key != "enable"}
    check_true(
        "single_frame with more than one method frame is refused, and nothing else is",
        len(acq.refusals(method_module.from_dict(ungoverned))) == 1
        and acq.refusals(recipe) == [],
    )

    # Task 31: the counts `[acquisition]` states against the counts the trainee's own
    # strings embed. Every one of these fails silently on the instrument -- a fold that
    # sums the wrong pushes, a frame that never completes, a frame that acquires nothing
    # -- so each is a refusal before a box is opened, and the message names both numbers.
    def one_refusal(**acquisition: object) -> str:
        problems = acq.refusals(method_module.from_dict(
            loop_document | {"acquisition": dict(loop_document["acquisition"])
                             | acquisition}))
        return problems[0] if len(problems) == 1 else f"{len(problems)} refusals"

    check_true(
        "a per_repetition table whose period is not the method's scans is refused, "
        "naming both numbers",
        f"{scans + 1}" in one_refusal(scans=scans + 1)
        and str(method_module.enable_fall_tick(scans)) in one_refusal(scans=scans + 1),
    )
    dropped = dict(loop_document["boxes"][0])
    dropped["load"] = [dropped["load"][0].replace("0:A:1:B:1", "0:B:1")]
    check_true(
        "a sequencer table that never raises the digitizer's enable is refused, since "
        "the loop header reads like the event and is not it",
        [problem for problem in acq.refusals(method_module.from_dict(
            loop_document | {"boxes": [dropped]})) if "never raises A" in problem],
    )
    check_true(
        "a single_frame table that loops a different number of times from the "
        "accumulations is refused, and the trainee's own shape is not",
        acq.refusals(method_module.from_dict(single_frame_document(frames=1))) == []
        and len(acq.refusals(method_module.from_dict(
            single_frame_document(frames=1, accumulations=accumulations + 1)))) == 1,
    )
    compressed = dict(loop_document["boxes"][0])
    compressed["load"] = [*compressed["load"], "SARBCTBL,J10[HRsm1CD12r]2"]
    check_true(
        "a compression table's pass count is read off the string and refused where it "
        "disagrees, and a method that could not be read is warned about instead",
        len(acq.refusals(method_module.from_dict(
            loop_document | {"boxes": [compressed]}))) == 1
        and acq.cautions(method_module.from_dict(
            loop_document | {"boxes": [dict(compressed)
                                       | {"load": ["SARBCTBL,J10[HRsm1CD12r"]}]})),
    )

    with tempfile.TemporaryDirectory() as directory:
        boxes = {"box1": mips_module.Box(transport=mips_module.FakeBox(), name="box1")}
        seen: list[acq.Event] = []
        acq.send_phases(recipe, boxes, progress=seen.append)
        sent = [(event.phase, event.command) for event in seen
                if isinstance(event, acq.PhaseSent)]
        phase_events = [event for event in seen if isinstance(event, acq.PhaseSent)]
        check_true(
            "send_phases sends setup, load and arm in the method's order, and waits "
            "for the box to say it is ready",
            [phase for phase, _ in sent] == ["setup", "setup", "load", "arm"]
            and "TBLRDY" in phase_events[-1].detail,
        )
        check_true(
            "and a setup phase carrying a LOC-only command drops the box out of table "
            "mode first, so a second acquisition is not refused on its first string",
            sent[0] == ("setup", "SMOD,LOC")
            and [command for _, command in sent].count("SMOD,LOC") == 1,
        )
        # Both golden methods have an empty sequencer setup, so their first string is
        # the load phase's table -- and `STBLDAT` needs local mode as much as `STBLCLK`
        # does. A box left armed by its own previous run refused one on the instrument
        # (lab record, task 41); nothing above the guard can tell that from a bad table.
        empty_setup = method_module.from_dict(
            loop_document | {"boxes": [dict(loop_document["boxes"][0]) | {"setup": []}]})
        bare: list[acq.Event] = []
        acq.send_phases(
            empty_setup,
            {"box1": mips_module.Box(transport=mips_module.FakeBox(), name="box1")},
            progress=bare.append)
        check_true(
            "and a load phase does the same for its table, so a method with no setup "
            "of its own can still be sent twice in a row",
            [(event.phase, event.command) for event in bare
             if isinstance(event, acq.PhaseSent)][:2]
            == [("load", "SMOD,LOC"), ("load", empty_setup.boxes[0].load[0])],
        )

        # --- what the boxes were holding (lab record, task 40) ----------------
        states = [event for event in seen if isinstance(event, acq.StateRead)]
        check_true(
            "send_phases reads each box's state back before the setup phase, again in "
            f"the seam before the load phase, and once more armed ({len(states)} "
            "readbacks)",
            [event.when for event in states]
            == [acq.WHEN_BEFORE, acq.WHEN_AFTER, acq.WHEN_ARMED],
        )
        check_true(
            "and the readback asks only what the box's own GCMDS listing names, so a "
            "getter this firmware lacks is never sent",
            bool(states) and states[0].state.listed and not states[0].state.refused,
        )
        blind = mips_module.Box(transport=mips_module.FakeBox(strict=True), name="blind")
        blind_state = mips_module.read_state(blind, listing=frozenset({"GVER"}))
        check_true(
            "a getter outside the listing is skipped rather than sent, and says so",
            blind_state.version and not blind_state.refused
            and "GDCBALL" in blind_state.skipped,
        )

        analog = mips_module.FakeBox(name="MIPS-A", dcb_channels=16, rf_channels=2)
        declared = method_module.from_dict(
            loop_document | {"boxes": [dict(loop_document["boxes"][0]) | {
                "dc_bias": {"15": 0.0, "16": 5.0},
                "rf": {"1": {"frequency_hz": 943000, "drive_pct": 50.0,
                             "mode": "MANUAL"}},
            }]})
        analog_box = {"box1": mips_module.Box(transport=analog, name="box1")}
        agreed: list[acq.Event] = []
        snapshot = acq.send_phases(declared, analog_box, progress=agreed.append)
        check_true(
            "a method may declare DC bias and RF, and the declaration reaches the box "
            "as ordinary setter strings at the end of the setup phase",
            analog.dc_bias[15] == 5.0 and analog.dc_bias[14] == 0.0
            and analog.rf[1]["SRFDRV"] == "50.00",
        )
        check_true(
            "and a box that agrees with what its method declared is warned about "
            "nothing",
            not [event for event in agreed if isinstance(event, acq.Warned)
                 and "declared" in event.message],
        )
        check_true(
            "the snapshot renders all three readings and the conditions note, which is "
            "what the file's stamp holds and what the send log carries",
            "as found" in snapshot.render() and acq.WHEN_AFTER in snapshot.render()
            and acq.WHEN_ARMED in snapshot.render(),
        )

        # The DC bias monitors stop converting the moment a box enters table mode and
        # answer a frozen array afterwards (docs/mips-wire-format.md §8.2), which is
        # why the reading above is taken between the setup and load phases. Both
        # halves of that are checked here: the stand-in freezes, and the comparison
        # declines a frozen monitor in one line rather than reading it as a fault.
        armed_state = mips_module.read_state(analog_box["box1"])
        analog.dc_bias_monitor[15] = 3.75  # 0.750 of setpoint, as AUKLET's froze
        frozen = mips_module.read_state(analog_box["box1"])
        check_true(
            f"the DC bias monitors freeze in table mode ({armed_state.table_status}) "
            "and the comparison says so rather than reading one as a disagreement",
            not armed_state.monitors_converting
            and frozen.dc_bias_readback(16) == 3.75 and frozen.dc_bias(16) == 5.0
            and acq.declared_differences(declared.boxes[0], frozen)
            == [f"box1 DC bias monitors were not compared: the readback was taken "
                f"with the table {frozen.table_status}, where they do not convert"],
        )
        check_true(
            "and the reading the send actually keeps was taken where they convert, so "
            "an ordinary acquisition warns about no monitor at all",
            snapshot.after[0].monitors_converting
            and not [event for event in agreed if isinstance(event, acq.Warned)
                     and "monitors" in event.message],
        )

        # The failure this exists to catch: a channel moved at the front panel after
        # the method declared it. Nothing refuses, and the difference is named.
        analog_box["box1"].local()
        analog.dc_bias[15] = 37.0
        moved = acq.declared_differences(declared.boxes[0],
                                         mips_module.read_state(analog_box["box1"]))
        check_true(
            "a DC bias the box does not hold at what the method declared is warned "
            "about, with both numbers, and refuses nothing",
            len(moved) == 1 and "5.00 V" in moved[0] and "37.00 V" in moved[0],
        )

        arb = mips_module.Box(transport=mips_module.FakeBox(arb_modules=4), name="arb")
        loose = acq.left_as_found(declared.boxes[0], mips_module.read_state(arb))
        check_true(
            "and every ARB module setting the method's setup does not name is reported "
            f"as left as found ({len(loose)} settings)",
            any("WFDIR is left as found" in line for line in loose),
        )

        with acq.FakeConsole(notify_on_scans_count=scans // 4) as fake:
            # The stand-in publishes a frame from inside the handler for `acquire
            # frame`, so without a hold its batches race the start list the loop walks
            # to release that frame, and the enable-gate guard fires at random.
            fake.frame_hold_s = 0.05
            # Four batches a frame with the last two after that frame's own `finished`,
            # which is the console's ordering and the reason the loop waits for a frame
            # to count out rather than taking `finished` for the end (lab record,
            # task 34). A one-batch frame would exercise none of it.
            #
            # The batch size above is the one deliberate untruth in this section. The
            # stand-in's default is the console's own `NotifyOnScansCount` of 500, and
            # four real batches would be a 2000-scan frame acquired several times over,
            # which is where this section's runtime was before task 34 cut it. What the
            # ordering costs to exercise does not depend on the size, so the size gives
            # way; nothing here reads a batch count as a measurement.
            fake.trailing_batches = 2
            with acq.DataStream(fake.data_endpoint) as stream, \
                    acq.Console(fake.command_endpoint) as console:
                console.configure(offset_v=0.251)
                run = acq.run_acquisition(
                    recipe, boxes=boxes, console=console, stream=stream,
                    directory=directory,
                    post_trigger_samples=fake.post_trigger_samples,
                    silence=0.3, gate_dwell=dwell, progress=seen.append,
                    snapshot=acq.Snapshot(
                        before=tuple(event.state for event in states
                                     if event.when == acq.WHEN_BEFORE),
                        after=tuple(event.state for event in states
                                    if event.when == acq.WHEN_AFTER),
                        armed=tuple(event.state for event in states
                                    if event.when == acq.WHEN_ARMED),
                        conditions="self-check, no instrument",
                    ),
                )
                check_true(
                    f"the loop ran a whole acquisition ({run.text})",
                    run.complete and len(run.frames) == accumulations
                    and run.scans_published == scans * accumulations,
                )
                check_true(
                    "and waited for each frame to count out, batches trailing its own "
                    "finished, before marking it complete",
                    all(record.ended_by == "counted" for record in run.frames)
                    and all(record.trailing_batches == 2 for record in run.frames)
                    and all(record.wait_seconds < 0.3 for record in run.frames)
                    and all(UimfFile(run.raw_path).frame_params(n).marked_complete
                            for n in range(1, accumulations + 1)),
                )
                check_true(
                    "and folded the method frame into a companion of today's shape",
                    len(run.folds) == 1 and run.folds[0].error is None
                    and UimfFile(run.summed_path).frame_params(1).scans == scans,
                )
                check_true(
                    "and withdrew the run pointer on its way out, so a viewer that "
                    "followed this run is not still being told it is in progress",
                    read_live_pointer() is None,
                )

                # A second run with no `send_phases` between it and the first, which is
                # every acquisition after the first press: a replicate, a second press
                # of the same button, or the next line of a bench script. All three
                # re-arm the rack before their own first frame, which is what a second
                # plain Acquire did not do on 2026-09-21 (lab record, task 63).
                walked: list[tuple[str, str]] = []
                replicate = acq.run_acquisition(
                    recipe, boxes=boxes, console=console, stream=stream,
                    directory=directory,
                    post_trigger_samples=fake.post_trigger_samples,
                    stem="selfcheck-loop-2", replicate=True, silence=0.3,
                    gate_dwell=dwell,
                    progress=lambda event: walked.append((event.phase, event.command))
                    if isinstance(event, acq.PhaseSent)
                    and event.phase in ("reset", "enable") else None,
                )
                check_true(
                    "a second acquisition on the same rack acquires again into a new "
                    f"file ({os.path.basename(replicate.raw_path)})",
                    replicate.complete and replicate.replicate
                    and os.path.isfile(run.raw_path),
                )
                check_true(
                    "and re-armed the rack first -- the method's reset list, then the "
                    "declared enable line down by command -- rather than starting "
                    "behind whatever the previous run's table left on it",
                    walked == [("reset", "SMOD,LOC"), ("reset", "SMOD,TBL"),
                               ("enable", "SMOD,LOC"), ("enable", "SDIO,A,0"),
                               ("enable", "SMOD,TBL")],
                )

                # The one failure that produces a full frame of plausible data at the
                # wrong offset, from a table that left the enable high or an enable
                # lead off a pulled-up input (lab record, task 05). The start list is
                # three serial round trips on the instrument and instant against a
                # stand-in, so the window the guard watches has to be put back. With
                # `gate_dwell=0` it is the only window there is, which is the half of
                # the guard this check is about.
                fake.frame_hold_s = 0.0
                slow = {"box1": mips_module.Box(transport=SlowBox(), name="box1")}
                acq.send_phases(recipe, slow)
                stalled = acq.run_acquisition(
                    recipe, boxes=slow, console=console, stream=stream,
                    directory=directory,
                    post_trigger_samples=fake.post_trigger_samples,
                    stem="selfcheck-loop-gate", silence=0.3, abort_after=1,
                    gate_dwell=0.0,
                )
                check_true(
                    "a batch published before the start list has finished fails its "
                    "frame rather than being acquired",
                    stalled.frames[0].outcome == "EnableGateError"
                    and not UimfFile(stalled.raw_path).frame_params(1).marked_complete,
                )

                # The other half of the same guard, and the only positive evidence a
                # run has that its gate was ever shut: the first frame is held open
                # where nothing should arrive, and a chain whose enable input was left
                # disabled publishes into that window (lab record, task 26). Here the
                # stand-in publishes the moment a frame is asked for, which is the same
                # shape.
                ungated = acq.run_acquisition(
                    recipe, boxes=boxes, console=console, stream=stream,
                    directory=directory,
                    post_trigger_samples=fake.post_trigger_samples,
                    stem="selfcheck-loop-dwell", silence=0.3, abort_after=1,
                    gate_dwell=0.3,
                )
                check_true(
                    "a batch published during the dwell, before anything was released, "
                    "fails the run's first frame",
                    ungated.frames[0].outcome == "EnableGateError"
                    and "before anything had been released" in ungated.frames[0].detail,
                )

                # The third part of the same guard, and the only one that looks at what
                # happened *after* a frame. A `per_repetition` table lowers the
                # digitizer's enable itself one batch past the last counted scan and
                # then ends, so the `TBLCMPLT` the box prints is that repetition's
                # evidence its gate came down; with no clock on the box's trigger input
                # the table stops at tick 0 and the line never falls, and every frame
                # behind it still counts out, folds and verifies exactly as though it
                # had been gated (lab record, tasks 42 and 46).
                fake.frame_hold_s = 0.05
                stalled_table = mips_module.FakeBox()
                stalled_table.clocked = False
                unwitnessed = {"box1": mips_module.Box(transport=stalled_table,
                                                       name="box1")}
                acq.send_phases(recipe, unwitnessed)
                missed: list[acq.Event] = []
                blind = acq.run_acquisition(
                    recipe, boxes=unwitnessed, console=console, stream=stream,
                    directory=directory,
                    post_trigger_samples=fake.post_trigger_samples,
                    stem="selfcheck-loop-witness", silence=0.3, gate_dwell=dwell,
                    progress=missed.append,
                )
                check_true(
                    "a repetition whose table never said TBLCMPLT is warned about, and "
                    "the second in a row ends the run though every frame counted out",
                    acq.enable_witness(recipe) == "box1"
                    and len(blind.frames) == 2
                    and all(record.acquired and record.ended_by == "counted"
                            and record.table_completed_s is None
                            for record in blind.frames)
                    and blind.stopped_early is not None
                    and len([event for event in missed
                             if isinstance(event, acq.Warned)
                             and "TBLCMPLT" in event.message]) == 2,
                )

                # `single_frame` with more than one method frame, which is refused
                # unless the method says which output carries the enable. With it named
                # the loop lowers the line itself between method frames, in local mode,
                # because a host `SDIO` in table mode is latched by the table's next
                # event and not by the host (lab record, task 26).
                fake.frame_hold_s = 0.05
                looping = method_module.from_dict(single_frame_document(
                    file_stem="selfcheck-loop-single"))
                check_true(
                    "a single_frame method that names its gate line is not refused",
                    acq.refusals(looping) == [],
                )
                gated = {"box1": mips_module.Box(transport=mips_module.FakeBox(),
                                                 name="box1")}
                acq.send_phases(looping, gated)
                # What the previous method frame leaves behind: a table that raised the
                # enable at its tick 0 and has nothing in it to lower the line again.
                gated["box1"].transport.dio_image["A"] = True
                gated["box1"].transport.dio_pins["A"] = True
                lowered: list[acq.Event] = []
                single = acq.run_acquisition(
                    looping, boxes=gated, console=console, stream=stream,
                    directory=directory,
                    post_trigger_samples=fake.post_trigger_samples,
                    silence=0.3, gate_dwell=dwell, progress=lowered.append,
                )
                check_true(
                    f"and acquires both frames ({single.text})",
                    single.complete and len(single.frames) == 2,
                )
                check_true(
                    "having put the box in local mode, cleared the line and armed it "
                    "again ahead of each of the two -- the run's own first frame "
                    "included, since what a previous run left on the line is not this "
                    "table's business",
                    [event.command for event in lowered
                     if isinstance(event, acq.PhaseSent) and event.phase == "enable"]
                    == ["SMOD,LOC", "SDIO,A,0", "SMOD,TBL"] * 2
                    and gated["box1"].transport.dio_pins["A"] is False,
                )
                console.stop_acquire()

    section("reading a file back")
    # Task 72: `clockwork.summary`, the numbers an agent's tools hand back instead of a
    # plot. The stand-in's invented spectrum repeats every sixteen scans, so a run of A
    # repetitions holds exactly A copies of one, in both files; the raw file and its
    # companion must then give the same number to every question but saturation.
    import json as _json

    import numpy as np
    from mainspring.uimf import FrameSpec, GlobalSpec, SparseFrame, UimfWriter

    from clockwork import summary

    with tempfile.TemporaryDirectory() as directory:
        boxes = {"box1": mips_module.Box(transport=mips_module.FakeBox(), name="box1")}
        acq.send_phases(recipe, boxes)
        with acq.FakeConsole() as fake:
            fake.frame_hold_s = 0.05
            with acq.DataStream(fake.data_endpoint) as stream, \
                    acq.Console(fake.command_endpoint) as console:
                console.configure(offset_v=0.251)
                read_run = acq.run_acquisition(
                    recipe, boxes=boxes, console=console, stream=stream,
                    directory=directory, instrument=machine,
                    post_trigger_samples=fake.post_trigger_samples,
                    stem="selfcheck-read", silence=0.3, gate_dwell=dwell,
                )
                console.stop_acquire()
            one = [fake._scan_spectrum(scan) for scan in range(scans)]
        raw_summary = summary.summarize(read_run.raw_path)
        summed_summary = summary.summarize(read_run.summed_path)
        per_scan = [accumulations * sum(values) for _bins, values in one]
        check_true(
            "a stand-in acquisition reads back as exactly A copies of the spectrum it "
            f"invented, in both files ({summed_summary['total_counts']} counts)",
            read_run.complete
            and raw_summary["total_counts"] == summed_summary["total_counts"] == sum(per_scan)
            and raw_summary["tic_profile"]["values"] == per_scan
            and summed_summary["tic_profile"]["values"] == per_scan,
        )
        check_true(
            "and each file says which of the pair it is and where the other one is",
            (raw_summary["file"], summed_summary["file"]) == ("raw", "summed")
            and raw_summary["companion"] == os.path.abspath(read_run.summed_path)
            and summed_summary["companion"] == os.path.abspath(read_run.raw_path),
        )
        check_true(
            "and saturation is exact in the raw file and an upper bound in the companion",
            raw_summary["saturation"]["bound"] == "exact"
            and summed_summary["saturation"]["bound"] == "upper"
            and raw_summary["saturation"]["points_at_ceiling"] == 0,
        )
        tallest = one[3][0][1]
        calibration = UimfFile(read_run.raw_path).frame_params(1).calibration(
            UimfFile(read_run.raw_path).global_params().bin_width_ns)
        window = [float(calibration.mz(tallest - 0.5)), float(calibration.mz(tallest + 0.5))]
        answers = [(summary.windowed(path, {"tall": window})["windows"]["tall"]["intensity"],
                    summary.atd(path, window)["profile"])
                   for path in (read_run.raw_path, read_run.summed_path)]
        check_true(
            "and one m/z window gives the same intensity and the same arrival-time "
            f"distribution in both ({answers[0][0]})",
            answers[0] == answers[1]
            and answers[0][0] == accumulations * one[3][1][1] * (scans // 16),
        )
        check_true(
            "and every answer is plain JSON, small enough for a tool result "
            f"({len(_json.dumps(raw_summary))} bytes for the summary)",
            len(_json.dumps(raw_summary)) < 8000,
        )

        uncalibrated = os.path.join(directory, "uncalibrated.uimf")
        with UimfWriter(uncalibrated, GlobalSpec(bins=64)) as writer:
            number = writer.add_frame(FrameSpec(scans=4))
            writer.write_sparse_frame(number, SparseFrame.from_scans(
                frame=number, scans=4, bins=64,
                points={1: (np.asarray([10], dtype=np.int32),
                            np.asarray([5], dtype=np.int32))}))
            writer.finalise_frame(number)
        try:
            summary.windowed(uncalibrated, "bradykinin-clock")
            refused = ""
        except summary.SummaryError as exc:
            refused = str(exc)
        check_true(
            "a file that says CalibrationDone 0 is refused m/z windows with a sentence, "
            "and still summarised",
            "CalibrationDone 0" in refused
            and summary.summarize(uncalibrated)["total_counts"] == 5,
        )

    golden = clockwork.lab_dir("golden")
    golden_file = (os.path.join(golden, "bradykinin-clock", "260825_BK_025.uimf")
                   if golden else None)
    if golden_file is None or not os.path.isfile(golden_file):
        skip("the golden CLOCK file reproduces task 09's water-loss and fragment ratios",
             "the golden experiments are lab material; a public clone has none")
    else:
        ratios = summary.windowed(golden_file, "bradykinin-clock")["ratios"]
        check_true(
            "the golden CLOCK file reproduces task 09's water-loss and fragment ratios "
            f"({ratios['water_loss']:.3f}, {ratios['fragments']:.3f})",
            (round(ratios["water_loss"], 3), round(ratios["fragments"], 3))
            == (0.504, 1.182),
        )

    section("the console process")
    # Task 49. Nothing here launches an executable except the last check, which is
    # skipped without one: what a clone can establish is the key table, the refusals
    # the fork would apply at startup, and that the file-against-process comparison
    # tells the two apart. That comparison is the whole point of the section -- the
    # console reads `config.txt` once and reports back only its full scale, so a file
    # that has been edited since says something the card is not doing, which is how a
    # bench evening acquired two 64.5 s frames through a 250 ms timeout without any
    # record of it (lab record, tasks 42 and 47).
    from clockwork.acq import process as console_process

    sample_config = (
        "# a lab config.txt\n"
        "PostTriggerDelay=0.00001\nNotifyOnScansCount=500\nAcquisitionTimeoutMs=100\n"
        "TriggerLevel=0.4\nTriggerSlope=rising\nFullScaleRange=0.5\n"
        "ZeroSuppressThreshold=-32667\nZeroSuppressHysteresis=100\nControlIoPort=2\n"
    )
    config = console_process.ConsoleConfig(sample_config)
    check_true("a config.txt round-trips with its comments and spacing",
               config.dumps() == sample_config)
    check_true(
        f"and reads back the settings clockwork has an opinion about (full scale "
        f"{config.full_scale_v} V, batch {config.notify_on_scans_count}, timeout "
        f"{config.acquisition_timeout_ms} ms, Control I/O {config.control_io_port})",
        config.full_scale_v == 0.5 and config.notify_on_scans_count == 500
        and config.acquisition_timeout_ms == 100 and config.control_io_port == 2,
    )
    check_true("a key the file omits is in force at the console's own literal, "
               "not blank",
               config.get("AcquisitionMaxBufferCount") is None
               and config.in_force("AcquisitionMaxBufferCount")
               == console_process.KEYS_BY_NAME["AcquisitionMaxBufferCount"].default)
    check_true("a good config has nothing the fork would refuse to start on",
               config.problems() == [])
    bad = console_process.ConsoleConfig(
        sample_config.replace("ZeroSuppressHysteresis=100", "ZeroSuppressHysteresis=4")
        .replace("FullScaleRange=0.5", "FullScaleRange=1.0"))
    check_true(
        "and every value the fork refuses is caught before a console is launched "
        f"({'; '.join(bad.problems())})",
        len(bad.problems()) == 2,
    )
    check_true(
        "the console's own spelling of a value is not a disagreement (std::to_string "
        "writes 0.00001 as 0.000010)",
        config.differences({"PostTriggerDelay": "0.000010",
                            "TriggerLevel": "0.400000"}) == {},
    )
    check_true(
        "but a file that has drifted from a running console names the key",
        config.differences({"AcquisitionTimeoutMs": "250"})
        == {"AcquisitionTimeoutMs": ("250", "100")},
    )
    rearm = console_process.ConsoleConfig(
        sample_config + "TriggerRearmDeadTime=0.000002048\n")
    check_true(
        "a value the console's own log cannot print faithfully is not a drift "
        "(std::to_string writes six decimals, so 0.000002048 is logged 0.000002)",
        rearm.differences({"TriggerRearmDeadTime": "0.000002"}) == {},
    )
    check_true(
        f"and is named as the blind spot it is ({rearm.blind_spots()})",
        rearm.blind_spots() == {"TriggerRearmDeadTime": "0.000002"}
        and config.blind_spots() == {},
    )
    started = console_process.read_startup_block([
        "[info] Logger initialized",
        'Config value "AcquisitionTimeoutMs" found, value set to 2000',
        "[info] Logger initialized",
        'Config value "AcquisitionTimeoutMs" found, value set to 100',
        'Config value "ControlIoPort" not found, value defaulted to 2',
        "[info] and on with the day",
    ])
    check_true(
        "the startup block read back is the last start's, and holds both of the "
        f"console's message shapes ({started})",
        started == {"AcquisitionTimeoutMs": "100", "ControlIoPort": "2"},
    )

    # The supervisor interface, against the stand-in that needs no executable. What
    # this shows is that a window's status bar, restart button and `--fake` have the
    # same six calls to make whether or not there is a console on the machine.
    with console_process.FakeConsoleProcess() as supervisor:
        supervisor.start()
        supervisor.wait_ready()
        check_true(
            f"a simulated console starts, answers and reports itself ({supervisor})",
            supervisor.alive and supervisor.info is not None
            and supervisor.command_endpoint.startswith("tcp://"),
        )
        supervisor.restart()
        check_true("and restarts, which is the path a config.txt change takes",
                   supervisor.alive and supervisor.info is not None)
    check_true("and is stopped on the way out", not supervisor.alive)

    found = console_process.find_console()
    if found is None:
        skip("a real console process starts, answers info and stops",
             "no console executable on this machine; lab record, task 49")
    else:
        proc = console_process.ConsoleProcess(found)
        try:
            proc.start()
            seconds = proc.wait_ready()
            check_true(
                f"the console at {os.path.basename(found)} starts and answers info in "
                f"{seconds:.1f} s ({proc.info.text if proc.info else ''})",
                proc.info is not None,
            )
            check_true(
                f"and said what it read from config.txt at startup "
                f"({len(proc.startup)} keys)",
                bool(proc.startup),
            )
            check_true("and the config.txt beside it agrees with what it is holding",
                       proc.needs_restart() == {})
        finally:
            proc.stop()
        check_true("and stops when it is told to", not proc.alive)

    section("wire transcript")
    # Task 29. The three bench scripts and the window write one of these beside every
    # run, and it is the only record of what the boxes and the console actually said;
    # everything else in either repo is what some caller chose to compute. So the check
    # is that a whole stand-in acquisition leaves a file naming the frames it acquired,
    # with both links and the loop's own narrative in it, and that with no transcript
    # open the same run emits nothing at all.
    from clockwork import transcript as transcript_module

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, transcript_module.default_name("selfcheck"))
        boxes = {"box1": mips_module.Box(transport=mips_module.FakeBox(), name="box1")}
        with acq.FakeConsole() as fake:
            fake.frame_hold_s = 0.05
            with acq.DataStream(fake.data_endpoint) as stream, \
                    acq.Console(fake.command_endpoint) as console:
                console.configure(offset_v=0.251)
                with transcript_module.to_file(
                    path, header="tools/check_public.py, against the stand-ins"
                ):
                    acq.send_phases(recipe, boxes)
                    transcribed = acq.run_acquisition(
                        recipe, boxes=boxes, console=console, stream=stream,
                        directory=directory,
                        post_trigger_samples=fake.post_trigger_samples,
                        stem="selfcheck-transcript", silence=0.3, gate_dwell=0.01,
                    )
                console.stop_acquire()

        written = open(path, encoding="utf-8").read()
        check_true(
            f"a transcript of a whole acquisition is written and closed "
            f"({os.path.getsize(path)} bytes)",
            transcribed.complete and written.endswith("\n")
            and "closed after" in written,
        )
        check_true(
            "and it names every frame the run acquired",
            all(f"file frame {number}" in written
                for number in range(1, accumulations + 1)),
        )
        check_true(
            "and carries both links and the loop's own narrative",
            "mips.wire" in written and "acq.wire" in written
            and "acq.stream" in written and "acq.loop" in written,
        )
        check_true(
            "the box's bytes are there as repr, with the framing visible",
            r"b'SMOD,TBL\n'" in written and "! TBLRDY" in written,
        )
        check_true(
            "the table's chunk boundaries are there, and so is the string itself",
            ">   chunk 1/" in written and "of stall margin" in written
            and "string: STBLDAT;" in written,
        )
        check_true(
            "the console's commands, its replies and the frames it was asked for "
            "are there",
            "> acquire frame | <" in written and "< ack" in written
            and "acquire frame 1:" in written,
        )
        check_true(
            "a batch is one summary line and never a payload",
            all(len(line) < 200 for line in written.splitlines()
                if " batch " in line),
        )

        # And the other half: nothing is emitted when nothing is listening. The package
        # attaches no handler and sets no level, so every call site is a level check
        # that fails and no record is ever built.
        import logging as _logging

        counted: list[_logging.LogRecord] = []

        class _Count(_logging.Handler):
            def emit(self, record: _logging.LogRecord) -> None:
                counted.append(record)

        quiet = _Count()
        _logging.getLogger("clockwork").addHandler(quiet)
        try:
            silent = mips_module.Box(transport=mips_module.FakeBox(), name="box1")
            silent.send_table("STBLDAT;0:[A:1,100:];")
            silent.arm()
        finally:
            _logging.getLogger("clockwork").removeHandler(quiet)
        check_true("and a transcript that is not open costs no records at all",
                   counted == [])

    section("send log")
    # Task 39. Addison's request from the instrument day: a plain-text file beside the
    # UIMF holding each complete string sent to each box, because the lab troubleshoots
    # by comparing a run's strings against the ones it knows work on SLIMPHONY. It is a
    # filtered view of the records the wire transcript already carries, so the check is
    # that a whole stand-in acquisition leaves a file with every string of its method in
    # it exactly once and in order, each answered, and with none of the chunk
    # bookkeeping, batch summaries or byte `repr`s that make the transcript unreadable
    # at a bench.

    with tempfile.TemporaryDirectory() as directory:
        stem = "selfcheck-send-log"
        path = os.path.join(directory, transcript_module.send_log_name(stem))
        boxes = {"box1": mips_module.Box(transport=mips_module.FakeBox(), name="box1")}
        with acq.FakeConsole() as fake:
            fake.frame_hold_s = 0.05
            with acq.DataStream(fake.data_endpoint) as stream, \
                    acq.Console(fake.command_endpoint) as console:
                console.configure(offset_v=0.251)
                header = transcript_module.run_header(
                    method=recipe, method_path="selfcheck.toml",
                    instrument=instrument_module.Instrument(
                        name="SLIM3",
                        vertical=instrument_module.Vertical(0.5, 0.251, False)),
                    console=console.info(),
                    boxes=[("box1", "COM1", boxes["box1"].box_name(),
                            boxes["box1"].version())],
                    conditions="bradykinin, self-check, no instrument",
                )
                with transcript_module.send_log(path, header=header):
                    acq.send_phases(recipe, boxes, snapshot=True)
                    logged = acq.run_acquisition(
                        recipe, boxes=boxes, console=console, stream=stream,
                        directory=directory,
                        post_trigger_samples=fake.post_trigger_samples,
                        stem=stem, silence=0.3, gate_dwell=dwell,
                    )
                console.stop_acquire()

        sent = open(path, encoding="utf-8").read()
        lines = sent.splitlines()
        entry = recipe.box("box1")
        strings = list(entry.setup) + list(entry.load) + list(entry.arm)

        def where(string: str) -> list[int]:
            """Every line that is this string being sent, by position in the file."""
            mark = f"{transcript_module.TO_BOX} {string}"
            return [at for at, line in enumerate(lines) if line.endswith(mark)]

        check_true(
            f"a send log is written beside the UIMF the run acquired "
            f"({os.path.basename(path)}, {len(lines)} lines)",
            logged.complete and os.path.isfile(os.path.join(directory, stem + ".uimf")),
        )
        # `SMOD,TBL` is the arm phase, and it is also in the reset list and in the
        # enable steps the run walks ahead of its own first frame, so it goes three
        # times in a log that holds a send and an acquisition (lab record, task 63).
        # The "once" rule is about the strings only a send delivers; the order rule
        # still covers all three, since the arm's copy is the first of the three.
        once = [string for string in strings if string != "SMOD,TBL"]
        check_true(
            "and holds every string the method sent, whole, once for the strings only a "
            "send delivers, and in the order they went",
            [len(where(string)) for string in once] == [1] * len(once)
            and len(where("SMOD,TBL")) == 3
            and [where(string)[0] for string in strings]
            == sorted(where(string)[0] for string in strings),
        )
        check_true(
            "with the start list once per repetition, which is how often it was walked",
            all(len(where(step.command)) == accumulations for step in recipe.start),
        )
        check_true(
            "with what each box answered, the lines it raised on its own, and what "
            "the run decided",
            f"{transcript_module.FROM_BOX} ACK" in sent
            and f"{transcript_module.UNPROMPTED} TBLRDY" in sent
            and "the gate is shut" in sent,
        )
        check_true(
            "naming the method and its hash, the window, the console and the box it drove",
            "selfcheck.toml" in sent and "sha256 " in sent
            and "full scale 0.5 V" in sent and "not inverted" in sent
            and "SA220P" in sent and "box1        COM1" in sent,
        )
        check_true(
            "with the boxes' state readback in it, as notes under each box's own name, "
            "titled with where in the phases each one was taken",
            all(sent.count(f"{transcript_module.ASIDE} state {when}") == 1
                for when in (acq.WHEN_BEFORE, acq.WHEN_AFTER, acq.WHEN_ARMED))
            and f"box1        {transcript_module.ASIDE}   DCB 1" in sent,
        )
        check_true(
            "and the operator's conditions note in the header, where a replicate's log "
            "carries it too",
            "\nconditions\n" in sent and "bradykinin, self-check" in sent,
        )
        check_true(
            "and none of the chunk bookkeeping, batch summaries or byte reprs the "
            "transcript keeps",
            "chunk 1/" not in sent and "batch 1:" not in sent
            and "BatchSeen" not in sent and "b'" not in sent,
        )
        check_true(
            "written with LF line endings, which is what the repo pins",
            b"\r\n" not in open(path, "rb").read(),
        )

    section("the owner")
    check_owner()

    section("the daemon")
    check_daemon()

    section("the MCP server")
    check_mcp()

    section("the standing envelope")
    check_envelope()

    section("instrument routines")
    check_routines()

    section("the window")
    # Task 50. Nothing here opens a window: what a clone can establish without a
    # display is the two halves of `clockwork.app` that import no Qt -- what a run is
    # called, and how a file is handed to mainspring -- plus the box discovery the
    # window opens with, driven against a stand-in rack. The window itself is
    # `tests/test_app.py`, which needs pytest-qt and an offscreen platform.
    import datetime as _dt
    import tempfile

    from clockwork.app import naming
    from clockwork.app.launch import open_with
    from clockwork.mips import Box, FakeBox, discover, mips_ports

    with tempfile.TemporaryDirectory() as scratch:
        for name in ("260825_BK_025.uimf", "260825_BK_037.summed.uimf",
                     "260904_BK_094.uimf", "260904_BK_094.sent.txt",
                     "260917_QQ_400.uimf"):
            open(os.path.join(scratch, name), "w").close()
        stem = naming.next_stem(scratch, "BK", _dt.date(2026, 9, 17))
        check_true(
            f"the file counter is one past the highest these initials already use "
            f"({stem}), across dates and ignoring other initials",
            stem == "260917_BK_095",
        )
        check_true(
            "a stem reserved this session moves it before the file exists",
            naming.next_stem(scratch, "BK", _dt.date(2026, 9, 17),
                             taken=[stem]) == "260917_BK_096",
        )
    check_true(
        "a name in another shape is not a stem, so a trainee's own file is left alone",
        naming.parse_stem("Tables_BradykininCLOCK.txt") is None
        and naming.parse_stem("260904_BK_094.summed.uimf") == ("260904", "BK", 94),
    )
    check_true(
        "initials are cleaned to what a stem can be parsed back out of",
        naming.clean_initials("m_b 2!") == "MB2",
    )
    check_true(
        "handing a file to a program that is not there reports rather than raises",
        not open_with("no-such-program.exe", __file__),
    )

    found = discover(ports=["COM-A", "COM-B"], opener=lambda port, **_: Box(
        transport=FakeBox(), name=port))
    check_true(
        "two boxes answering one GNAME are reported and neither is addressable, "
        "because a method cannot say which of them it means",
        found.boxes == {} and len(found.unusable) == 2,
    )
    found.close()
    def refuses(port: str, **_: object) -> Box:
        raise OSError(f"could not open {port}")

    silent = discover(ports=["COM-GONE"], opener=refuses)
    check_true(
        "a port that will not open is a row in the scan and not the end of it",
        silent.found == () and len(silent.silent) == 1,
    )
    first = discover(ports=["COM-A"], opener=lambda port, **_: Box(
        transport=FakeBox(name="MIPS-A"), name=port))
    again = discover(ports=["COM-A"], opener=refuses,
                     held={entry.port: entry.box for entry in first.found if entry.box})
    check_true(
        "a rescan asks the boxes it holds over their own handles and reopens none, "
        "since a close resets the box behind it (lab record, task 62)",
        again.boxes == first.boxes and not again.silent,
    )
    again.close()
    ports = mips_ports(strict=False)
    if ports:
        check_true(
            f"this machine enumerates {len(ports)} serial port(s), "
            f"{sum(1 for info in ports if info.mips_class)} of them MIPS-class",
            True,
        )
    else:
        skip("MIPS-class ports are found by their USB identity",
             "no serial ports on this machine; port presence is not evidence of a box "
             "either way (lab record, task 37)")

    section("the box state panel")
    # Task 51. `clockwork.app.boxstate` is the panel's whole judgement and imports no
    # Qt, so which rows are marked left as found, which disagree with the method, and
    # the two things it refuses to print all run on a clone with no display. The widget
    # that draws them is `tests/test_app.py`.
    from clockwork.app.boxstate import AGREES, DIFFERS, FOUND, Reading, state_table

    panel_box = Box(transport=FakeBox(name="MIPS-A", version="1.243t", dcb_channels=4,
                                      rf_channels=1, arb_modules=2), name="auklet")
    panel_box.transport.dc_bias = [12.0, -70.0, 0.0, 5.0]
    panel_box.transport.dc_bias_error = -0.03
    panel_box.transport.arb[2]["SWFDIR"] = "REV"
    local_state = mips_module.read_state(panel_box)
    panel_method = method_module.BoxMethod(
        name="auklet", port="COM3", setup=("SWFDIR,1,FWD",),
        dc_bias=((1, 12.0), (2, -60.0)))
    table = state_table(Reading(state=local_state, when="on demand"), panel_method)
    marks = {row.label: row for part in table.sections for row in part.rows}
    check_true(
        "a module setting the method does not name is marked left as found, which is "
        "what told two indistinguishable files apart (lab record, task 40)",
        marks["module 2 direction"].mark == FOUND
        and marks["module 2 direction"].value == "REV"
        and marks["module 1 direction"].mark == AGREES,
    )
    check_true(
        "a declared DC bias the box disagrees with is marked, and agrees with the run "
        "log's own warning about it: the tolerances are the loop's",
        marks["channel 2"].mark == DIFFERS and marks["channel 1"].mark == AGREES
        and any("DC bias 2 was declared" in line
                for line in acq.declared_differences(panel_method, local_state)),
    )
    check_true(
        "a monitor reading is shown beside its setpoint while the box is local",
        "monitors" in marks["channel 1"].note,
    )
    # The 100 ms service task that maintains the monitor array does not run in table
    # mode, so from `SMOD,TBL` the array is frozen where it was: neither the output nor
    # the last true reading (section 8.2, lab record, task 43).
    panel_box.transport.mode, panel_box.transport.status = "TBL", "READY"
    armed = state_table(
        Reading(state=mips_module.read_state(panel_box), when="armed"), panel_method)
    frozen = {row.label: row for part in armed.sections for row in part.rows}
    check_true(
        "a monitor read in table mode is named as not converting rather than printed",
        frozen["channel 1"].note == "not converting in table mode",
    )
    external = state_table(
        Reading(state=local_state),
        method_module.BoxMethod(name="auklet", port="COM3", setup=("STBLCLK,EXT",)))
    clock = {row.label: row for part in external.sections for row in part.rows}
    check_true(
        "GTBLFRQ is not shown as a frequency under an external clock, where the "
        "firmware prints an uninitialised local (wire format section 4)",
        clock["clock"].value == "external, EXT",
    )
    sequencer = state_table(Reading(
        state=local_state, when="after setup, before load",
        sequencer=mips_module.read_sequencer(panel_box), sequencer_when="armed"))
    check_true(
        "a two-getter sequencer reading updates the table engine and leaves the rest "
        "of the panel saying when it was read",
        "two getters" in sequencer.caption
        and "after setup, before load" in sequencer.caption,
    )
    check_true(
        "a box nothing has been read off says so instead of showing empty rows",
        state_table(Reading()).empty,
    )

    section("the run queue")
    # Task 53. `clockwork.app.runqueue` is the whole of what an unattended series
    # decides -- which row runs next, what a row's outcome says in the morning, and when
    # a failure ends the night -- and it imports no Qt, so a clone with no display
    # checks the rules the table draws. The table itself is `tests/test_app.py`.
    from clockwork.app import runqueue

    queue = runqueue.RunQueue([
        runqueue.QueueRow(method_path=os.path.join("methods", f"{name}.toml"))
        for name in ("blank", "sample", "wash")])
    check_true(
        "a queue names its rows by their document rather than by their path",
        [row.name for row in queue.rows] == ["blank", "sample", "wash"],
    )
    check_true(
        "Start takes the first waiting row, and the row in flight can then be neither "
        "removed nor moved: its strings are already on the wire",
        queue.begin() is queue.rows[0] and not queue.remove(0)
        and queue.move(0, 1) == 0,
    )
    check_true(
        "a waiting row may be brought forward to just behind the row in flight and no "
        "further, the positions before it having been run or skipped",
        queue.move(2, -1) == 1 and queue.move(1, -1) == 1,
    )
    check_true(
        "a failed row ends the series and the rest carry the reason they did not run",
        queue.finish(runqueue.FAILED, "TBLSTRT was refused") is False
        and queue.advance() is None
        and [row.state for row in queue.rows] == [
            runqueue.FAILED, runqueue.SKIPPED, runqueue.SKIPPED]
        and "failed" in queue.rows[1].problem,
    )
    going_on = runqueue.RunQueue([
        runqueue.QueueRow(method_path="a.toml", go_on=True),
        runqueue.QueueRow(method_path="b.toml")])
    going_on.begin()
    check_true(
        "a row that says go on is walked past instead, because a series of independent "
        "samples should not lose the night to one of them",
        going_on.finish(runqueue.FAILED, "the console would not start") is True
        and going_on.advance() is going_on.rows[1],
    )
    stopping = runqueue.RunQueue([runqueue.QueueRow(method_path="a.toml"),
                                  runqueue.QueueRow(method_path="b.toml")])
    stopping.begin()
    stopping.cancel("stopped by the operator")
    check_true(
        "Stop skips everything that has not started and leaves the row in flight "
        "running, which is what ending after the current repetition and its fold means",
        stopping.rows[0].state == runqueue.RUNNING
        and stopping.rows[1].state == runqueue.SKIPPED,
    )

    queued_method = method_module.from_dict({
        "schema_version": 2,
        "metadata": {"name": "self-check queue", "created": _dt.date(2026, 9, 18)},
        "acquisition": {"frames": 1, "scans": 8, "accumulations": 2,
                        "file_stem": "260918_QQ_001",
                        "repetition_mode": "per_repetition"},
        "boxes": [{"name": "box1", "port": "COM3",
                   "load": ["STBLDAT;0:[A:1,0:A:1,100:A:0,200:];"],
                   "arm": ["SMOD,TBL"]}],
        "start": [["box1", "TBLSTRT"]],
    })

    def queued_run(stem: str, *, silence: int = 0, stopped: str | None = None):
        return acq.Run(
            method=queued_method, raw_path=f"{stem}.uimf",
            summed_path=f"{stem}.summed.uimf",
            frames=tuple(
                acq.FrameRecord(method_frame=1, repetition=index + 1,
                                frame_number=index + 1, outcome="acquired",
                                ended_by="silence" if index < silence else "counted")
                for index in range(2)),
            folds=(acq.FoldRecord(method_frame=1, frames_folded=(1, 2), rows=8),),
            warnings=(), seconds=1.0, stopped_early=stopped)

    reported = runqueue.QueueRow(method_path="sample.toml")
    state = runqueue.outcome_of(
        reported, [queued_run("260918_QQ_001"), queued_run("260918_QQ_002", silence=1)])
    check_true(
        "a finished row carries the stems its replicates were written under and the "
        "repetitions that ended on the silence rather than on their own count",
        state == runqueue.DONE
        and reported.stems == ("260918_QQ_001", "260918_QQ_002")
        and "260918_QQ_001, 260918_QQ_002" in reported.outcome
        and "1 repetition(s) ended on the silence" in reported.outcome,
    )
    halted = runqueue.QueueRow(method_path="sample.toml")
    check_true(
        "a row the operator stopped is neither done nor failed: its run folded the "
        "frame it was in, so what it left on disk is a short experiment",
        runqueue.outcome_of(halted, [queued_run("260918_QQ_003", stopped="by hand")])
        == runqueue.STOPPED and "stopped: by hand" in halted.outcome,
    )

    section("hardware")
    skip("a MIPS box answers GVER", "no serial hardware in a self-check; lab record, task 04")
    skip("the acquisition console answers info", "no console in a self-check; lab record, task 03")

    print()
    print(f"{len(FAIL)} failed, {len(SKIPPED)} skipped")
    for name in FAIL:
        print("  FAIL", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
