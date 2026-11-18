r"""`clockwork mcp`: the tools, served over stdio to an MCP client such as Claude Code.

`build_server(owner, library, output)` registers every tool in `clockwork.mcp.tools.TOOLS`
with the MCP Python SDK's `MCPServer`, each as a function with the tool's own signature
and docstring that calls `Toolbox.call`, so the audit line and the one-sentence error are
the toolbox's and the SDK only carries them. `run` is the subcommand: an owner of its
own under `--fake`, or a `RemoteOwner` to `clockwork serve`, and the server on stdio
until the client closes it.

**The SDK is imported here and nowhere else**, and only when a server is built: it costs
about a second and a half to import, which neither the window nor the daemon should pay.

**stdout is the protocol.** The SDK's stdio transport takes the process's stdout for
itself and points file descriptor 1 at stderr for everything else, so a stray `print`
reaches the client's log rather than corrupting a message. What this module says about
itself goes to stderr for the same reason.
"""

from __future__ import annotations

import functools
import os
import sys
from collections.abc import Callable
from typing import Any, TextIO

from .. import __version__
from ..envelope import Limits
from ..instrument import UNCALIBRATED, Instrument
from ..sitting import SIMULATED, instrument_and_limits
from .audit import AuditLog
from .tools import TOOLS, Tool, Toolbox, ToolFailure

__all__ = ["INSTRUCTIONS", "PROGRAM", "SIMULATED", "build_server", "fake_output",
           "instrument_and_limits", "run", "server_for"]

PROGRAM = "clockwork mcp"

ORIGIN = "Claude, through clockwork mcp"
"""How this server's jobs are named to the daemon's other clients (`Handle.origin`), so
that the window can say whose acquisition it is showing. The lab's MCP client is Claude;
the words would read wrongly for another client, and nothing depends on them."""

INSTRUCTIONS = """\
clockwork controls an ion mobility mass spectrometer: MIPS boxes that apply the
voltages and timing of an experiment, and a digitizer that records it into UIMF files.
Work from a request, in the words of the person it is for. list_templates says what an
experiment can vary; render_template and validate_method check a choice before anything
is sent; arm sends it to the boxes; acquire records it and answers a job number to
follow with progress; list_files and the data tools read the files back as numbers.
Run discover_boxes once before the first arm. Every arm and acquire carries the
request, quoted in the person's own words, and their initials; state your plan to the
person and pass it to arm as plan. Knobs the person does not mention take their
defaults; say which defaults were used when reporting back. The instrument's standing
limits bound which templates may run and how far each knob may turn: list_templates
shows them, and a refusal from them is final for this session, not something to work
around. Report every cold_start caution arm and acquire answer. Use note to record why
you chose the next acquisition, and what you told the person. The instrument's routines
(list_routines) are experiments with nothing left to choose, such as checking the beam:
run_routine runs one through arm and acquire to a verdict, pass, fail or could not judge,
and its text is the report to give the person. On the real instrument
the person owns the sample and the source: confirm with them that the sample is
spraying before acquiring. When status says fake is true, nothing is real: the boxes
and the digitizer are simulated."""


def server_for(toolbox: Toolbox) -> object:
    """An `MCPServer` offering every tool in `TOOLS` over `toolbox`."""
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer(PROGRAM, instructions=INSTRUCTIONS, version=__version__)
    for entry in TOOLS:
        server.add_tool(
            _adapter(toolbox, entry), name=entry.name, description=entry.description,
            annotations=ToolAnnotations(readOnlyHint=entry.read_only,
                                        destructiveHint=entry.destructive,
                                        openWorldHint=False))
    return server


def build_server(owner: object, library: str = "", output: str = "", *,
                 instrument: Instrument = UNCALIBRATED, instrument_path: str = "",
                 log: AuditLog | None = None, limits: Limits | None = None,
                 routines: str = "") -> object:
    """An `MCPServer` over `owner`, listing `library` and writing into `output`."""
    return server_for(Toolbox(owner, library=library, output=output, instrument=instrument,
                              instrument_path=instrument_path, log=log, limits=limits,
                              routines=routines))


def _adapter(toolbox: Toolbox, entry: Tool) -> Callable[..., dict]:
    """`entry` as the SDK wants a tool: a function with its arguments and docstring.

    The SDK builds the tool's schema from the signature and reports a `ToolError`'s
    message to the client as the result; anything else it reports as an unexplained
    failure, so every `ToolFailure` is turned into one here. The result is declared
    `dict[str, Any]`, the one dict annotation the SDK sends as structured content as
    well as text."""
    from mcp.server.mcpserver.exceptions import ToolError

    @functools.wraps(entry.function)
    def call(**arguments: object) -> dict:
        try:
            return toolbox.call(entry.name, arguments)
        except ToolFailure as exc:
            raise ToolError(str(exc)) from None

    del call.__wrapped__
    call.__signature__ = entry.signature.replace(  # type: ignore[attr-defined]
        return_annotation=dict[str, Any])
    return call


def fake_output() -> str:
    r"""Where a `--fake` session's files go by default: `%LOCALAPPDATA%\clockwork\fake-runs`.

    Not the working directory, which for a client launched from a checkout is the
    repository: a rehearsal's stand-in files do not belong beside the code."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return os.path.join(base, "clockwork", "fake-runs")


def run(*, fake: bool = False, library: str = "", output: str = "", endpoint: str = "",
        instrument_path: str = "", limits_path: str = "", routines: str = "",
        project_limits_path: str = "", stream: TextIO | None = None) -> int:
    """Serve the tools on stdio until the client closes. Returns the exit code.

    Under `fake` an owner of simulated boxes and a simulated console lives in this
    process and the console is started at once, as `clockwork serve --fake` does;
    otherwise the tools drive `clockwork serve` at `endpoint`, and a daemon that does
    not answer is one sentence on stderr and exit code 1. `library` and `output`
    default to the daemon's own (`hello`), or under `fake` to the instrument's library
    where an instrument directory resolves (`clockwork.instrument_dir`) and to
    `fake_output()`. `library` may name several directories (`clockwork.library_dirs`).
    `routines` defaults to the `routines` directory beside the library's first.
    `instrument_path`, `limits_path` and `project_limits_path` default to the daemon's
    own (`hello`, `clockwork.sitting.from_daemon`). An instrument neither named nor the
    daemon's defaults, except under `fake` or over a daemon that is itself `--fake`, to
    the instrument directory's document (`clockwork.instrument_file`), so the limits
    beside it are in force. A `$CLOCKWORK_INSTRUMENT` that names nothing is said on the
    line after the first (`clockwork.instrument_unresolved`).

    `limits_path` is the instrument's standing limits (`clockwork.envelope`), by default
    the `limits.toml` beside `instrument_path` if there is one, and
    `project_limits_path` a project's, combined into them. Limits named and not found,
    found and not valid, or a project's that would widen the standing ones, are one
    sentence and exit code 1: a server that
    quietly ran without the limits it was pointed at would refuse every send and not
    say why. With none, every send to the instrument is refused, and a rehearsal is
    refused nothing.
    """
    say = stream if stream is not None else sys.stderr
    from .. import instrument_dir, instrument_file, instrument_unresolved
    from ..owner import LocalOwner, RemoteOwner, StartConsole
    from ..owner.remote import DEFAULT_COMMAND, DaemonError
    from ..sitting import from_daemon

    simulated = fake
    if fake:
        try:
            instrument, limits = instrument_and_limits(
                fake=True, instrument_path=instrument_path, limits_path=limits_path,
                project_limits_path=project_limits_path)
        except ValueError as exc:
            print(f"{PROGRAM}: {exc}", file=say)
            return 1
        owner: object = LocalOwner(fake=True, program=f"{PROGRAM} --fake").start()
        owner.limits = limits  # type: ignore[attr-defined]
        owner.submit(StartConsole(label="starting the simulated console"))  # type: ignore[attr-defined]
        named_library = bool(library)
        library = library or instrument_dir("golden") or ""
        output = output or fake_output()
    else:
        # Read before any daemon is asked for, as it always was: with no daemon to name
        # a sitting, a document of this server's own or the default that does not load
        # is the sentence back, rather than the daemon's absence. A daemon that answers
        # then decides which documents are loaded, below.
        early: ValueError | None = None
        try:
            instrument_and_limits(
                fake=False, instrument_path=instrument_path or instrument_file() or "",
                limits_path=limits_path, project_limits_path=project_limits_path)
        except ValueError as exc:
            early = exc
        owner = RemoteOwner(endpoint or DEFAULT_COMMAND, origin=ORIGIN)
        try:
            hello = owner.hello()  # type: ignore[attr-defined]
        except DaemonError as exc:
            print(f"{PROGRAM}: {early or exc}", file=say)
            owner.close()  # type: ignore[attr-defined]
            return 1
        simulated = bool(hello.fake)
        # The daemon's sitting where this server names none of its own (lab record,
        # task 165): every client of one daemon enforces one set of limits. Over
        # `clockwork serve --fake` the instrument's own document is never defaulted.
        instrument_path, limits_path, project_limits_path, _ = from_daemon(
            hello, instrument_path=instrument_path, limits_path=limits_path,
            project_limits_path=project_limits_path, default_instrument=instrument_file)
        try:
            instrument, limits = instrument_and_limits(
                fake=simulated, instrument_path=instrument_path, limits_path=limits_path,
                project_limits_path=project_limits_path)
        except ValueError as exc:
            print(f"{PROGRAM}: {exc}", file=say)
            owner.close()  # type: ignore[attr-defined]
            return 1
        named_library = bool(library or hello.library)
        library = library or hello.library
        output = output or hello.output or os.getcwd()
    os.makedirs(output, exist_ok=True)
    print(f"{PROGRAM} {__version__}{' --fake' if fake else ''}: library "
          f"{library or '(none)'}, files to {output}, calls logged to "
          f"{AuditLog.beside(output).path}, limits "
          f"{limits.path if limits is not None else '(none)'}"
          f"{' with ' + limits.project if limits is not None and limits.project else ''}",
          file=say)
    # A variable that names nothing is said whatever else was given; no instrument
    # directory at all only where it has left this server without a library or, against
    # the instrument, without limits.
    unresolved = instrument_unresolved(
        when_unset=not named_library or (limits is None and not simulated))
    if unresolved:
        print(f"{PROGRAM}: {unresolved}", file=say)
    try:
        build_server(owner, library, output, instrument=instrument,
                     instrument_path=instrument_path, log=AuditLog.beside(output, via="mcp"),
                     limits=limits, routines=routines).run("stdio")  # type: ignore[attr-defined]
    finally:
        if fake:
            owner.shutdown("the MCP client closed")  # type: ignore[attr-defined]
            owner.join(30)  # type: ignore[attr-defined]
        else:
            owner.close()  # type: ignore[attr-defined]
    return 0
