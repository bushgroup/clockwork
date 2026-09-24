"""An MCP server over the owner: the tools an agent drives the instrument with.

The surface an agent -- a Claude Code session on the instrument PC first -- uses to turn
a person's request into an experiment: find a template and render it, check the method,
arm the boxes, acquire, follow the run, and read the files back as numbers. Every tool
is a line or two over a function the package already has, through the owner protocol,
so the agent drives exactly what the window drives (lab record, task 69).

    tools.py   the registry and `Toolbox`, the tools themselves; no SDK import
    guard.py   the interlock every send passes before it is submitted, over
               `clockwork.envelope`
    audit.py   one JSON line per call, beside the files
    server.py  the tools registered with the MCP SDK, and `clockwork mcp`

Qt-free, like the owner beneath it: a server that drives the boxes never builds a
window.
"""

from __future__ import annotations

from ..envelope import HAND_WRITTEN, NO_LIMITS
from .audit import LOG_NAME, AuditLog
from .guard import guard_acquisition
from .tools import TOOLS, Tool, Toolbox, ToolFailure

__all__ = [
    "HAND_WRITTEN",
    "LOG_NAME",
    "NO_LIMITS",
    "TOOLS",
    "AuditLog",
    "Tool",
    "ToolFailure",
    "Toolbox",
    "guard_acquisition",
]
