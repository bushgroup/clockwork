"""`clockwork <verb>`: the verbs built from the tool registry, their flags, every verb once
through `main(argv)` over a `--fake` daemon, and the exit codes.

Every verb runs in a fresh `Toolbox` over a fresh `RemoteOwner`, as it does from a shell,
so `arm` then `acquire` here are the two processes the command line makes of them: what
the boxes hold is the daemon's to remember, not a verb's (lab record, task 73).
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import socket
import subprocess
import sys
import time

import pytest

from clockwork import method as method_module
from clockwork.app import main
from clockwork.mcp import TOOLS
from clockwork.mcp.cli import _flag, verbs
from clockwork.method import template as template_module
from clockwork.owner import RemoteOwner
from test_daemon import Daemon
from test_mcp import LABELS, REQUEST, TEMPLATE

# One RF head declared, so a send's fingerprint carries an `RfChannel` across the wire,
# which it could not until this task: a send of any method declaring RF finished in the
# daemon and never reached a client.
RF_TEMPLATE = TEMPLATE + """
[boxes.rf.1]
frequency_hz = 943000
drive_pct = 50.0
mode = "MANUAL"
"""

INSTRUMENT = """\
schema_version = 1

[instrument]
name = "cli test"

[vertical]
full_scale_v = 0.5
offset_v = 0.251
inverted = false

[calibration]
slope = 0.738123
intercept = 0.07690495
measured = 2026-09-09
"""


@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "library"
    folder.mkdir()
    (folder / "line-b.toml").write_text(RF_TEMPLATE, encoding="utf-8")
    rendered = template_module.render(template_module.loads_template(RF_TEMPLATE),
                                      {}, LABELS)
    method_module.save(rendered.method, str(folder / "line-b-default.toml"))
    return str(folder)


@pytest.fixture
def daemon(tmp_path, library):
    (tmp_path / "runs").mkdir()
    made = Daemon(tmp_path / "runs", library=library)
    yield made
    made.stop()


class Shell:
    """`main(argv)` as a shell runs it, against one daemon: exit status, stdout, stderr."""

    def __init__(self, capsys, endpoint: str, instrument: str) -> None:
        self.capsys = capsys
        self.endpoint = endpoint
        self.instrument = instrument

    def __call__(self, verb: str, *arguments: str) -> tuple[int, str, str]:
        self.capsys.readouterr()
        code = main([verb, *arguments, "--endpoint", self.endpoint,
                     "--instrument", self.instrument])
        out, err = self.capsys.readouterr()
        return code, out, err

    def json(self, verb: str, *arguments: str) -> dict:
        code, out, err = self(verb, *arguments)
        assert code == 0, err
        return json.loads(out)


@pytest.fixture
def shell(capsys, daemon, tmp_path):
    path = tmp_path / "instrument.toml"
    path.write_text(INSTRUMENT, encoding="utf-8")
    client = RemoteOwner(daemon.endpoint, timeout=10)
    try:
        # The daemon starts its console as it opens; a verb that acquires before it is
        # ready is refused, as the window is.
        for _ in range(200):
            if client.status().console.state == "ready":
                break
            time.sleep(0.1)
    finally:
        client.close()
    return Shell(capsys, daemon.endpoint, str(path))


# --- the table ----------------------------------------------------------------------


def test_every_tool_is_a_verb_with_a_flag_per_argument():
    made = verbs()
    assert [verb.tool.name for verb in made] == [entry.name for entry in TOOLS]
    assert [verb.name for verb in made][:3] == ["list-templates", "list-methods",
                                                "load-method"]
    arm = next(verb for verb in made if verb.name == "arm")
    kinds = {flag.option: (flag.kind, flag.required) for flag in arm.flags}
    assert kinds["--request"] == ("text", True)
    assert kinds["--request-id"] == ("text", False)
    assert kinds["--knobs"] == ("mapping", False)
    assert kinds["--setup"] == ("switch", False)
    kinds = {flag.option: flag.kind for verb in made for flag in verb.flags}
    assert kinds["--frames"] == "integers" and kinds["--boxes"] == "texts"
    assert kinds["--windows"] == "text-or-mapping" and kinds["--mz"] == "text-or-list"
    assert kinds["--wait-s"] == "number" and kinds["--replicates"] == "integer"


def test_a_tool_whose_argument_no_flag_can_carry_fails_when_it_is_made_a_verb():
    def tool(self, spans: tuple[float, float]) -> dict: ...

    parameter = inspect.signature(tool).parameters["spans"]
    with pytest.raises(TypeError, match="no flag can carry"):
        _flag("tool", parameter)


def test_flags_parse_as_the_tool_wants_them(capsys):
    parser = argparse.ArgumentParser(prog="clockwork")
    commands = parser.add_subparsers(dest="command")
    from clockwork.mcp.cli import _arguments, add_verbs

    add_verbs(commands)

    def parsed(*words: str) -> dict:
        args = parser.parse_args(list(words))
        return _arguments(args.verb, args, args.verb_parser)

    assert parsed("render-template", "--template", "t.toml", "--knobs", "b_ticks=400",
                  "--knobs", "{\"other\": 1.5}", "--labels", "sample=a = b") == {
        "template": "t.toml", "knobs": {"b_ticks": 400, "other": 1.5},
        "labels": {"sample": "a = b"}}
    assert parsed("windowed-intensities", "--path", "f", "--windows", "bradykinin") == {
        "path": "f", "windows": "bradykinin"}
    assert parsed("windowed-intensities", "--path", "f", "--windows", "p=[530,532]",
                  "--scans", "10", "20") == {
        "path": "f", "windows": {"p": [530, 532]}, "scans": [10, 20]}
    assert parsed("arrival-time-distribution", "--path", "f", "--mz", "[500, 510]") == {
        "path": "f", "mz": [500, 510]}
    assert parsed("arm", "--request", "r", "--initials", "zz", "--no-setup",
                  "--template", "t") == {"request": "r", "initials": "zz", "setup": False,
                                         "template": "t"}
    for words, said in ((["render-template", "--template", "t", "--knobs", "b=x"],
                         "is not a number"),
                        (["render-template", "--template", "t", "--knobs", "b"],
                         "is not KEY=VALUE"),
                        (["windowed-intensities", "--path", "f", "--windows", "p=5"],
                         "is not a JSON array")):
        with pytest.raises(SystemExit) as caught:
            parser.parse_args(words)
        assert caught.value.code == 2 and said in capsys.readouterr().err
    with pytest.raises(SystemExit) as caught:
        parsed("windowed-intensities", "--path", "f", "--windows", "a", "--windows", "b=[1,2]")
    assert caught.value.code == 2


# --- every verb, over the daemon ----------------------------------------------------


def test_a_request_from_the_shell_one_verb_per_process(shell, library, tmp_path):
    output = str(tmp_path / "runs")
    status = shell.json("status")
    assert status["fake"] and status["output"] == output and status["last_armed"] is None
    assert [entry["path"] for entry in shell.json("list-templates")["templates"]] == [
        "line-b.toml"]
    [method] = shell.json("list-methods")["methods"]
    assert method["path"] == "line-b-default.toml"
    assert shell.json("load-method", "--method", "line-b-default.toml")["name"] == "mcp-test"
    assert shell.json("diff-methods", "--a", "line-b-default.toml",
                      "--b", "line-b-default.toml")["identical"]
    code, out, err = shell("load-method", "--method", "line-b.toml")
    assert code == 1 and out == "" and err.count("\n") == 1 and "clockwork load-method:" in err
    chosen = ("--template", "line-b.toml", "--labels", f"sample={LABELS['sample']}")
    rendered = shell.json("render-template", *chosen, "--knobs", "b_ticks=400")
    assert rendered["ok"] and rendered["knobs"] == {"b_ticks": 400}
    assert shell.json("validate-method", *chosen)["ok"]

    assert shell.json("discover-boxes", *chosen)["boxes"] == ["box1"]
    assert "box1" in shell.json("read-box-state")["boxes"]
    asked = (*chosen, "--knobs", "b_ticks=400", "--request", REQUEST, "--initials", "zz")
    armed = shell.json("arm", *asked, "--plan", "one run at 400 ticks")
    assert armed["stem"] == "260924_ZZ_001" or armed["stem"].endswith("_ZZ_001")
    assert shell.json("status")["last_armed"] == {"method": "mcp-test",
                                                  "stem": armed["stem"]}

    code, out, err = shell("acquire", *chosen, "--knobs", "b_ticks=300", "--request",
                           REQUEST, "--initials", "zz")
    assert code == 1 and out == "" and "the boxes are not holding this method" in err

    code, out, err = shell("acquire", *asked)
    assert code == 0, err
    started = json.loads(out)
    assert started["request_id"] == armed["request_id"], "the words continue the request"
    assert "done" in err.splitlines()[-1]
    with open(started["record"], encoding="utf-8") as handle:
        record = json.load(handle)
    assert [entry["stem"] for entry in record["files"]] == [armed["stem"]]

    answer = shell.json("progress", "--job", str(started["job"]), "--wait-s", "0")
    assert answer["done"] and answer["runs"][0]["complete"]
    code, out, err = shell("progress", "--job", str(started["job"]), "--follow")
    lines = [json.loads(line) for line in out.splitlines()]
    assert code == 0 and lines[-1]["done"] and "events" not in lines[-1]
    assert all("kind" in line for line in lines[:-1]) and len(lines) > 2
    code, _, err = shell("progress", "--job", "999")
    assert code == 1 and "is not one the owner knows" in err

    noted = shell.json("note", "--request-id", armed["request_id"], "--text", "from a shell")
    assert noted["notes"] == 1
    [run] = shell.json("list-files")["runs"]
    assert run["folded"] and run["request"]["text"] == REQUEST
    summed = run["summed"]["name"]
    assert shell.json("summarize-file", "--path", summed)["frames"]["count"] == 1
    windowed = shell.json("windowed-intensities", "--path", summed,
                          "--windows", "low=[100,500]", "--windows", "high=[500,1500]",
                          "--reference", "low")
    assert set(windowed["windows"]) >= {"low", "high"}
    assert "peak" in shell.json("arrival-time-distribution", "--path", summed,
                                "--mz", "[100,1500]")
    assert shell.json("stop")["stopping"] is False

    with open(os.path.join(output, "mcp-calls.log"), encoding="utf-8") as handle:
        logged = [json.loads(line) for line in handle]
    assert {line["via"] for line in logged} == {"cli"}
    assert {line["tool"] for line in logged} >= {entry.name for entry in TOOLS}


def test_acquire_no_wait_answers_at_once_and_a_later_follower_fills_the_record(shell):
    chosen = ("--template", "line-b.toml", "--labels", f"sample={LABELS['sample']}",
              "--request", REQUEST, "--initials", "zz")
    shell.json("discover-boxes", "--template", "line-b.toml",
               "--labels", f"sample={LABELS['sample']}")
    armed = shell.json("arm", *chosen)
    started = shell.json("acquire", *chosen, "--no-wait", "--replicates", "2")
    code, out, err = shell("progress", "--job", str(started["job"]), "--follow")
    assert code == 0, err
    assert json.loads(out.splitlines()[-1])["position"]["files_done"] == 2
    with open(started["record"], encoding="utf-8") as handle:
        record = json.load(handle)
    assert len(record["files"]) == 2 and record["files"][0]["stem"] == armed["stem"]


# --- exit codes ---------------------------------------------------------------------


def test_no_daemon_is_one_sentence_and_exit_one(capsys):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    code = main(["status", "--endpoint", f"tcp://127.0.0.1:{port}"])
    out, err = capsys.readouterr()
    assert code == 1 and out == ""
    assert err.startswith("clockwork status: no clockwork serve answered") \
        and err.count("\n") == 1


def test_a_usage_error_is_exit_two_and_fake_is_not_a_verb_s(capsys):
    for words in (["arm", "--initials", "zz"], ["--fake", "status"],
                  ["render-template", "--template", "t", "--knobs", "b=x"]):
        with pytest.raises(SystemExit) as caught:
            main(words)
        assert caught.value.code == 2, words
    assert "clockwork serve --fake" in capsys.readouterr().err


PROBE = """
import sys
from clockwork.app import main
try:
    main(["serve", "--help"])
except SystemExit:
    pass
print("clockwork.mcp.tools" in sys.modules)
"""


def test_a_command_that_is_not_a_verb_does_not_build_the_verbs():
    done = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True,
                          timeout=60)
    assert done.stdout.strip().splitlines()[-1] == "False", done.stderr


def test_the_command_line_document_has_a_line_for_every_verb():
    path = os.path.join(os.path.dirname(__file__), "..", "docs", "command-line.md")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    missing = [verb.name for verb in verbs() if f"- `{verb.name}`:" not in text]
    assert not missing, f"docs/command-line.md has no line for {missing}"
