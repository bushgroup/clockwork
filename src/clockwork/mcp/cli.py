r"""`clockwork <verb>`: every tool as a subcommand, the command-line twin of `clockwork mcp`.

One verb per tool in `clockwork.mcp.tools.TOOLS`, built from the registry rather than
written out, so a tool added later has its verb, its flags and its help the day it is
registered (lab record, task 73). The verb is the tool's name with hyphens
(`list_templates` is `list-templates`), each argument is a flag of the same name
(`request_id` is `--request-id`), and the tool's docstring is the verb's help. Each verb
builds a `Toolbox` over a `RemoteOwner` and makes one `Toolbox.call`, so a verb is
refused exactly where the MCP server's tool is -- the interlock and the standing limits
are inside the tools -- and writes the same audit line, marked `via: cli`.

**There is no in-process `--fake` for a verb.** An owner that lives only as long as one
verb's process holds nothing the next verb can use: `arm` then `acquire` as two
invocations would meet two different simulated racks. A rehearsal is `clockwork serve
--fake` with the verbs over it, which is also the only shape in which the verbs are
tested.

**Standard output is JSON and nothing else**: the tool's answer, indented, or under
`progress --follow` one compact line per event and a last line with the answer. What a
person watching needs -- the events of an `acquire` that waits for its run -- goes to
standard error, as does the one sentence of a refusal. Exit status 0 is an answer, 1 a
refusal or a job that failed, 2 a usage error (argparse's own), and 130 a follow cut
short with Ctrl-C, which leaves the job running.

**How a flag is typed**, from the tool's annotation:

    str, int, float           --name VALUE
    bool                      --name / --no-name
    list[int], list[str]      --name A B C
    dict[str, ...]            --name KEY=VALUE, repeated, or one JSON object
    str | dict[str, ...]      as the dict, or a single VALUE with no "=" for the text
    str | list                a JSON array, or anything else as the text

`KEY=VALUE` rather than JSON first because Windows PowerShell 5.1 strips the double
quotes out of a JSON argument on its way to a native program; a value is read as JSON
where the tool wants a number or a list (`--knobs b_ticks=400`,
`--windows precursor=[530.2,531.4]`), and as text where it wants text.

Qt-free, and imports no MCP SDK: a verb costs the toolbox and a socket.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import threading
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TextIO

from .tools import PROGRESS_WAIT_MAX_S, TOOLS, Tool, Toolbox, ToolFailure

__all__ = ["COMMON", "RECORD_WAIT_S", "Flag", "Verb", "add_verbs", "is_verb", "run",
           "verb_name", "verbs"]

COMMON = ("endpoint", "library", "output", "instrument", "limits", "routines")
"""The options every verb takes, which say where the tools are pointed rather than what
a tool is asked; no tool may take an argument of one of these names."""

ALIASES = {"run-routine": ("routine",)}
"""Shorter names a verb also answers to: `clockwork routine beam-check` for
`clockwork run-routine --name beam-check`, the one verb a trainee types by hand."""

POSITIONAL = {"run-routine": "name"}
"""A verb's argument that may also be given bare, after the verb."""

RECORD_WAIT_S = 60.0
"""How long a finished `acquire` waits for its run record's last file entry: the
summary of a file is computed as the owner reports it, and takes a second or two."""


@dataclass(frozen=True, slots=True)
class Flag:
    """One argument of a tool, as a verb's flag."""

    parameter: str
    """The tool's argument."""
    option: str
    """`--request-id` for `request_id`."""
    kind: str
    """`text`, `integer`, `number`, `switch`, `integers`, `texts`, `mapping`,
    `text-or-mapping` or `text-or-list`."""
    required: bool
    default: object
    values: object = None
    """For a mapping, the type of its values: `str`, a number, or a list."""

    @property
    def dest(self) -> str:
        return "tool_" + self.parameter


@dataclass(frozen=True, slots=True)
class Verb:
    """One tool as a subcommand."""

    name: str
    tool: Tool
    flags: tuple[Flag, ...]

    @property
    def summary(self) -> str:
        """The docstring's first sentence, cut at its colon when it runs long: the line
        `clockwork --help` lists the verb with."""
        first = self.tool.description.split("\n\n", 1)[0].replace("\n", " ")
        first = first.split(". ", 1)[0].rstrip(".")
        if len(first) > 80 and ":" in first:
            first = first.split(":", 1)[0]
        return first


def verb_name(tool: str) -> str:
    return tool.replace("_", "-")


def verbs() -> list[Verb]:
    """Every tool in `TOOLS` as a verb, in the registry's order. `TypeError` for a tool
    with an argument whose type no flag can carry, or named like a common option: a
    tool that cannot become a verb fails here, in the tests and in the self-check, the
    day it is registered."""
    made = []
    for entry in TOOLS:
        flags = []
        for parameter in entry.signature.parameters.values():
            if parameter.name in COMMON:
                raise TypeError(f"{entry.name}'s argument {parameter.name!r} is also an "
                                "option every verb takes")
            flags.append(_flag(entry.name, parameter))
        made.append(Verb(name=verb_name(entry.name), tool=entry, flags=tuple(flags)))
    return made


def is_verb(command: str | None) -> bool:
    return bool(command) and any(
        command == verb.name or command in ALIASES.get(verb.name, ()) for verb in verbs())


# -- the parser ----------------------------------------------------------------------


def add_verbs(commands: argparse._SubParsersAction) -> None:
    """One subparser per verb on `commands`, each with the common options."""
    for verb in verbs():
        parser = commands.add_parser(
            verb.name, aliases=list(ALIASES.get(verb.name, ())), help=verb.summary,
            description=verb.tool.description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="Answers as JSON on standard output. A refusal is one sentence on "
                   "standard error and exit status 1.")
        for flag in verb.flags:
            _add(parser, flag, positional=POSITIONAL.get(verb.name) == flag.parameter)
        if verb.name in POSITIONAL:
            parser.add_argument("bare", nargs="?", metavar=POSITIONAL[verb.name].upper(),
                                help=f"the {POSITIONAL[verb.name]}, as --"
                                     f"{verb_name(POSITIONAL[verb.name])} gives it")
        if verb.tool.name == "progress":
            parser.add_argument(
                "--follow", action="store_true",
                help="keep asking until the job ends, printing one JSON line per event "
                     "and a last line with the answer")
        if verb.tool.name == "acquire":
            parser.add_argument(
                "--no-wait", action="store_true",
                help="answer at once rather than when the run ends. The run record then "
                     "gains its files only when something follows the job with progress")
        common = parser.add_argument_group("where the tools are pointed")
        common.add_argument("--endpoint", metavar="ADDRESS", default="",
                            help="the daemon's command socket "
                                 "(default: tcp://127.0.0.1:5570)")
        common.add_argument("--library", metavar="DIR", default="",
                            help="the method and template library (default: the daemon's)")
        common.add_argument("--output", metavar="DIR", default="",
                            help="where runs are written and read back "
                                 "(default: the daemon's)")
        common.add_argument("--instrument", metavar="PATH", default="",
                            help="the instrument document runs are acquired under")
        common.add_argument("--limits", metavar="PATH", default="",
                            help="the instrument's standing limits (default: limits.toml "
                                 "beside --instrument)")
        common.add_argument("--routines", metavar="DIR", default="",
                            help="the instrument's routines (default: routines beside the "
                                 "library)")
        parser.set_defaults(verb=verb, verb_parser=parser)


def _flag(tool: str, parameter: inspect.Parameter) -> Flag:
    hint = parameter.annotation
    members = [hint]
    if typing.get_origin(hint) in (typing.Union, types.UnionType):
        members = [member for member in typing.get_args(hint) if member is not type(None)]
    required = parameter.default is inspect.Parameter.empty
    default = None if required else parameter.default

    def made(kind: str, values: object = None) -> Flag:
        return Flag(parameter=parameter.name, option="--" + verb_name(parameter.name),
                    kind=kind, required=required, default=default, values=values)

    def mapping_values(member: object) -> object | None:
        if typing.get_origin(member) in (dict, Mapping):
            arguments = typing.get_args(member)
            return arguments[1] if len(arguments) == 2 else str
        return None

    if len(members) == 1:
        only = members[0]
        simple = {str: "text", int: "integer", float: "number", bool: "switch"}
        if only in simple:
            return made(simple[only])
        if typing.get_origin(only) is list and typing.get_args(only) in ((int,), (str,)):
            return made("integers" if typing.get_args(only) == (int,) else "texts")
        values = mapping_values(only)
        if values is not None:
            return made("mapping", values)
    if len(members) == 2 and members[0] is str:
        other = members[1]
        values = mapping_values(other)
        if values is not None:
            return made("text-or-mapping", values)
        if other is list or typing.get_origin(other) is list:
            return made("text-or-list")
    raise TypeError(f"{tool}'s argument {parameter.name!r} is typed {hint!r}, which no "
                    "flag can carry; teach clockwork.mcp.cli._flag the type")


def _add(parser: argparse.ArgumentParser, flag: Flag, *, positional: bool = False) -> None:
    shown = "" if flag.required or flag.default in (None, "", {}, []) \
        else f" (default: {flag.default})"
    common: dict[str, Any] = {"dest": flag.dest, "default": argparse.SUPPRESS}
    if flag.required and not positional:
        common["required"] = True
    if flag.kind == "switch":
        parser.add_argument(flag.option, action=argparse.BooleanOptionalAction,
                            help=f"{flag.parameter}{shown}", **common)
        return
    if flag.kind in ("text", "integer", "number"):
        convert = {"text": str, "integer": int, "number": float}[flag.kind]
        parser.add_argument(flag.option, type=convert, metavar=flag.kind.upper(),
                            help=f"{flag.parameter}{shown}", **common)
        return
    if flag.kind in ("integers", "texts"):
        convert = int if flag.kind == "integers" else str
        parser.add_argument(flag.option, type=convert, nargs="+",
                            metavar="INTEGER" if convert is int else "TEXT",
                            help=f"{flag.parameter}, one or more", **common)
        return
    if flag.kind in ("mapping", "text-or-mapping"):
        either = " or a single name" if flag.kind == "text-or-mapping" else ""
        parser.add_argument(flag.option, action="append", type=_entry(flag),
                            metavar="KEY=VALUE",
                            help=f"{flag.parameter}: KEY=VALUE, repeatable, or one JSON "
                                 f"object{either}", **common)
        return
    parser.add_argument(flag.option, type=_text_or_list, metavar="JSON-OR-TEXT",
                        help=f"{flag.parameter}: a JSON array such as [500,510], or a "
                             "name", **common)


def _entry(flag: Flag) -> typing.Callable[[str], object]:
    """What one `KEY=VALUE` (or JSON object, or bare name) argument becomes."""
    def convert(text: str) -> object:
        stripped = text.strip()
        if stripped.startswith("{"):
            try:
                loaded = json.loads(stripped)
            except ValueError as exc:
                raise argparse.ArgumentTypeError(f"{text!r} is not a JSON object: "
                                                 f"{exc}") from None
            if not isinstance(loaded, dict):
                raise argparse.ArgumentTypeError(f"{text!r} is not a JSON object")
            return {str(key): value for key, value in loaded.items()}
        key, equals, value = stripped.partition("=")
        if not equals:
            if flag.kind == "text-or-mapping":
                return stripped
            raise argparse.ArgumentTypeError(f"{text!r} is not KEY=VALUE or a JSON object")
        if not key.strip():
            raise argparse.ArgumentTypeError(f"{text!r} names no key before the =")
        return {key.strip(): _value(value.strip(), flag.values)}
    convert.__name__ = flag.parameter  # argparse names the type in its error
    return convert


def _value(text: str, wanted: object) -> object:
    if wanted is str:
        return text
    try:
        loaded = json.loads(text)
    except ValueError:
        loaded = None
    numeric = typing.get_origin(wanted) in (typing.Union, types.UnionType) or wanted in (
        int, float)
    if numeric:
        if isinstance(loaded, (int, float)) and not isinstance(loaded, bool):
            return loaded
        raise argparse.ArgumentTypeError(f"{text!r} is not a number")
    if wanted is list or typing.get_origin(wanted) is list:
        if isinstance(loaded, list):
            return loaded
        raise argparse.ArgumentTypeError(f"{text!r} is not a JSON array such as [500,510]")
    return loaded if loaded is not None else text


def _text_or_list(text: str) -> object:
    stripped = text.strip()
    if not stripped.startswith("["):
        return stripped
    try:
        loaded = json.loads(stripped)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a JSON array: {exc}") from None
    if not isinstance(loaded, list):
        raise argparse.ArgumentTypeError(f"{text!r} is not a JSON array")
    return loaded


def _arguments(verb: Verb, args: argparse.Namespace,
               parser: argparse.ArgumentParser) -> dict[str, object]:
    """The flags given, as the tool's arguments: the ones not given are left to the
    tool's own defaults, and repeated `KEY=VALUE`s become one mapping."""
    found: dict[str, object] = {}
    bare = getattr(args, "bare", None)
    if bare is not None:
        chosen = next(flag for flag in verb.flags if flag.parameter == POSITIONAL[verb.name])
        if hasattr(args, chosen.dest) and getattr(args, chosen.dest) != bare:
            parser.error(f"the {chosen.parameter} is given twice, as {bare!r} and as "
                         f"{chosen.option}")
        setattr(args, chosen.dest, bare)
    for flag in verb.flags:
        if flag.required and not hasattr(args, flag.dest):
            parser.error(f"the following arguments are required: {flag.option}")
        if not hasattr(args, flag.dest):
            continue
        value = getattr(args, flag.dest)
        if flag.kind in ("mapping", "text-or-mapping"):
            texts = [item for item in value if isinstance(item, str)]
            if texts and (len(value) > 1 or flag.kind == "mapping"):
                parser.error(f"{flag.option} takes one name or KEY=VALUE pairs, not both")
            if texts:
                value = texts[0]
            else:
                merged: dict[str, object] = {}
                for item in value:
                    merged.update(item)
                value = merged
        found[flag.parameter] = value
    return found


# -- running one ---------------------------------------------------------------------


def run(args: argparse.Namespace, *, out: TextIO | None = None,
        err: TextIO | None = None) -> int:
    """Make the call `args` describes against the daemon, print, and answer the exit
    status. `args` is what `add_verbs`' parsers produced."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    verb: Verb = args.verb
    program = f"clockwork {verb.name}"
    arguments = _arguments(verb, args, args.verb_parser)

    from ..owner import RemoteOwner
    from ..owner.remote import DEFAULT_COMMAND, DaemonError
    from .audit import AuditLog
    from .server import instrument_and_limits

    owner = RemoteOwner(args.endpoint or DEFAULT_COMMAND)
    try:
        try:
            hello = owner.hello()
        except DaemonError as exc:
            _say(err, f"{program}: {exc}")
            return 1
        try:
            instrument, limits = instrument_and_limits(
                fake=bool(hello.fake), instrument_path=args.instrument,
                limits_path=args.limits)
        except ValueError as exc:
            _say(err, f"{program}: {exc}")
            return 1
        output = os.path.abspath(args.output or hello.output or os.getcwd())
        toolbox = Toolbox(owner, library=args.library or hello.library, output=output,
                          instrument=instrument, instrument_path=args.instrument,
                          log=AuditLog.beside(output, via="cli"), limits=limits,
                          routines=args.routines)
        if verb.tool.name == "run_routine":
            toolbox.narrate = lambda line: _say(err, line)
        if verb.tool.name == "progress" and args.follow:
            return _follow(toolbox, arguments, out, err, program)
        try:
            answer = toolbox.call(verb.tool.name, arguments)
        except ToolFailure as exc:
            _say(err, f"{program}: {exc}")
            return 1
        _print(out, answer, indent=2)
        if verb.tool.name == "acquire" and not args.no_wait:
            return _wait(toolbox, int(answer["job"]), err, program)
        return 0
    except KeyboardInterrupt:
        _say(err, f"{program}: interrupted; a job it started carries on, and "
                  "`clockwork stop` ends it")
        return 130
    finally:
        owner.close()


def _follow(toolbox: Toolbox, arguments: Mapping[str, object], out: TextIO, err: TextIO,
            program: str) -> int:
    """`progress` until the job ends: one JSON line per event, then the answer."""
    asked = dict(arguments)
    asked.setdefault("wait_s", PROGRESS_WAIT_MAX_S)
    while True:
        try:
            answer = toolbox.call("progress", asked)
        except ToolFailure as exc:
            _say(err, f"{program}: {exc}")
            return 1
        for event in answer["events"]:
            _print(out, event)
        asked["after"] = answer["last"]
        if answer["done"]:
            _print(out, {key: value for key, value in answer.items() if key != "events"})
            if answer.get("failed"):
                _say(err, f"{program}: job {answer['job']} failed: {answer['failed']}")
                return 1
            return 0


def _wait(toolbox: Toolbox, job: int, err: TextIO, program: str) -> int:
    """An `acquire`'s run, to its end: each event's text on standard error, then the
    run record's last entry, since the thread writing it dies with this process."""
    asked: dict[str, object] = {"job": job, "wait_s": PROGRESS_WAIT_MAX_S}
    while True:
        try:
            answer = toolbox.call("progress", asked)
        except ToolFailure as exc:
            _say(err, f"{program}: {exc}")
            return 1
        for event in answer["events"]:
            _say(err, event["text"])
        asked["after"] = answer["last"]
        if answer["done"]:
            break
    recorder: threading.Thread | None = toolbox.recorder(job)
    if recorder is not None:
        recorder.join(RECORD_WAIT_S)
    if answer.get("failed"):
        _say(err, f"{program}: job {job} failed: {answer['failed']}")
        return 1
    return 0


def _print(stream: TextIO, value: object, indent: int | None = None) -> None:
    # ASCII, escaped: a redirected stream on Windows encodes in the ANSI code page, and
    # the guard round it drops a line it cannot encode rather than raise.
    text = json.dumps(value, indent=indent, default=str,
                      separators=None if indent else (",", ":"))
    stream.write(text + "\n")
    stream.flush()


def _say(stream: TextIO, line: str) -> None:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        line.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        line = line.encode("ascii", "backslashreplace").decode("ascii")
    stream.write(line + "\n")
    stream.flush()
