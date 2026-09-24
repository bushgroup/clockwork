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
from ..instrument import UNCALIBRATED, Instrument, Vertical
from .audit import AuditLog
from .tools import TOOLS, Tool, Toolbox, ToolFailure

__all__ = ["INSTRUCTIONS", "PROGRAM", "SIMULATED", "build_server", "fake_output",
           "instrument_and_limits", "run", "server_for"]

PROGRAM = "clockwork mcp"

SIMULATED = Instrument(
    name="simulated instrument",
    description="The stand-in a --fake session acquires under when given no document.",
    vertical=Vertical(full_scale_v=0.5, offset_v=0.251, inverted=False))
"""What `--fake` acquires under with no `--instrument`. The loop refuses a document that
states no channel offset, as the window does, and a rehearsal has no instrument to have
written one for; this states the lab's full scale and the stand-in console's own
offset, and names itself so that no file mistakes it for a measurement."""

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
                                        destructiveHint=False, openWorldHint=False))
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


def instrument_and_limits(*, fake: bool, instrument_path: str = "",
                          limits_path: str = "") -> tuple[Instrument, Limits | None]:
    """The instrument document runs are acquired under, and the standing limits in force.

    `SIMULATED` under `fake` and `UNCALIBRATED` otherwise when no document is named.
    `limits_path` defaults to the `limits.toml` beside `instrument_path` if there is one.
    `ValueError` with one sentence for a document or limits that were named or found and
    could not be read: a server that quietly ran without the limits it was pointed at
    would refuse every send and not say why. Shared by `clockwork mcp` and the verbs."""
    from .. import envelope
    from .. import instrument as instrument_module

    instrument = SIMULATED if fake else UNCALIBRATED
    if instrument_path:
        try:
            instrument = instrument_module.load(instrument_path)
        except (OSError, ValueError) as exc:
            raise ValueError(f"the instrument document {instrument_path} could not be "
                             f"read: {exc}") from exc
    chosen = limits_path or envelope.default_path(instrument_path)
    if not chosen or not (limits_path or os.path.isfile(chosen)):
        return instrument, None
    try:
        return instrument, envelope.load(chosen)
    except (OSError, envelope.LimitsError) as exc:
        raise ValueError(f"the standing limits {chosen} could not be read: {exc}") from exc


def run(*, fake: bool = False, library: str = "", output: str = "", endpoint: str = "",
        instrument_path: str = "", limits_path: str = "", routines: str = "",
        stream: TextIO | None = None) -> int:
    """Serve the tools on stdio until the client closes. Returns the exit code.

    Under `fake` an owner of simulated boxes and a simulated console lives in this
    process and the console is started at once, as `clockwork serve --fake` does;
    otherwise the tools drive `clockwork serve` at `endpoint`, and a daemon that does
    not answer is one sentence on stderr and exit code 1. `library` and `output`
    default to the daemon's own (`hello`), or under `fake` to the lab's golden
    experiments where a lab checkout is beside this one and to `fake_output()`.
    `routines` defaults to the `routines` directory beside the library.

    `limits_path` is the instrument's standing limits (`clockwork.envelope`), by default
    the `limits.toml` beside `instrument_path` if there is one. Limits named and not
    found, or found and not valid, are one sentence and exit code 1: a server that
    quietly ran without the limits it was pointed at would refuse every send and not
    say why. With none, every send to the instrument is refused, and a rehearsal is
    refused nothing.
    """
    say = stream if stream is not None else sys.stderr
    from .. import lab_dir
    from ..owner import LocalOwner, RemoteOwner, StartConsole
    from ..owner.remote import DEFAULT_COMMAND, DaemonError

    try:
        instrument, limits = instrument_and_limits(
            fake=fake, instrument_path=instrument_path, limits_path=limits_path)
    except ValueError as exc:
        print(f"{PROGRAM}: {exc}", file=say)
        return 1

    if fake:
        owner: object = LocalOwner(fake=True, program=f"{PROGRAM} --fake").start()
        owner.submit(StartConsole(label="starting the simulated console"))  # type: ignore[attr-defined]
        library = library or lab_dir("golden") or ""
        output = output or fake_output()
    else:
        owner = RemoteOwner(endpoint or DEFAULT_COMMAND)
        try:
            hello = owner.hello()  # type: ignore[attr-defined]
        except DaemonError as exc:
            print(f"{PROGRAM}: {exc}", file=say)
            owner.close()  # type: ignore[attr-defined]
            return 1
        library = library or hello.library
        output = output or hello.output or os.getcwd()
    os.makedirs(output, exist_ok=True)
    print(f"{PROGRAM} {__version__}{' --fake' if fake else ''}: library {library or '(none)'}"
          f", files to {output}, calls logged to "
          f"{AuditLog.beside(output).path}, limits "
          f"{limits.path if limits is not None else '(none)'}", file=say)
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
