"""The interlock: what must be true before a tool sends a method to the boxes.

One function, called by every tool that puts strings on the wire -- `arm` and `acquire`
-- before it submits anything, so the refusal is the server's and never the driving
agent's judgement (lab record, task 69). What it checks today is deliberately little:

- **Every send says what it is for.** An empty request is refused under `--fake` too:
  the request is stamped into the file as its series and written into the run's log, and
  a file that cannot say what it was for is the one thing this surface exists to prevent.
- **Nothing reaches a real owner.** Against `clockwork serve` on the instrument every
  send is refused until the standing envelope -- each template's knob ranges narrowed by
  an instrument's own limits -- and the cold-start completeness check exist. Their body
  replaces the second rule here, and only here (lab record, task 71).

A rehearsal against the stand-ins is refused nothing else, so the whole tool set can be
driven end to end on a clone with no instrument.
"""

from __future__ import annotations

from ..method.template import Rendered

__all__ = ["NOT_YET", "guard_acquisition"]

NOT_YET = (
    "clockwork does not yet let an agent send to or acquire on the instrument: the check "
    "that keeps a method inside the instrument's standing limits is not built, and until "
    "it is every send through this server is refused outside --fake. Rehearse with "
    "`clockwork mcp --fake`, or run it from the window"
)
"""The refusal every send to a real owner gets until the standing envelope exists."""


def guard_acquisition(owner: object, rendered: Rendered | None, request: str) -> list[str]:
    """Why this method may not go to `owner`'s boxes for `request`; empty if it may.

    `owner` is anything with the owner protocol's `status()`; `rendered` is the render
    the method came from, or None for a hand-written method. One sentence per reason.
    """
    problems: list[str] = []
    if not request.strip():
        problems.append(
            "every send says what it is for: pass the request, in the words of the person "
            "it is for, or the name of the routine")
    if not owner.status().fake:  # type: ignore[attr-defined]
        problems.append(NOT_YET)
    return problems
