"""The interlock: what must be true before a tool sends a method to the boxes.

One function, called by every tool that puts strings on the wire -- `arm` and `acquire`
-- before it submits anything, so the refusal is the server's and never the driving
agent's judgement (lab record, task 69). Two rules:

- **Every send says what it is for.** An empty request is refused under `--fake` too:
  the request is stamped into the file as its series and written into the run's log, and
  a file that cannot say what it was for is the one thing this surface exists to prevent.
- **Every send is inside the standing envelope** (`clockwork.envelope.check`): a
  template the instrument's limits list, at knob values inside their ranges, on boxes
  they allow, inside the daemon session's budget. Against the instrument, no limits
  refuses everything and a hand-written method is refused outright; a rehearsal with no
  limits is refused nothing, and one given limits is held to them exactly as the
  instrument would be (lab record, task 71).

The cold-start check is the other half of the envelope and is not here: it needs the
boxes read back, which is a job, so the tools run it themselves once this has passed.
"""

from __future__ import annotations

from ..envelope import Ledger, Limits, check
from ..method import Method
from ..method.template import Rendered

__all__ = ["guard_acquisition"]


def guard_acquisition(owner: object, rendered: Rendered | None, request: str, *,
                      method: Method | None = None, limits: Limits | None = None,
                      ledger: Ledger | None = None,
                      replicates: int | None = None) -> list[str]:
    """Why this method may not go to `owner`'s boxes for `request`; empty if it may.

    `owner` is anything with the owner protocol's `status()`; `rendered` is the render
    the method came from, or None for a hand-written method, which is then `method`.
    Neither asks only whether anything may be sent at all, which is what `status`
    reports. One sentence per reason.
    """
    problems: list[str] = []
    if not request.strip():
        problems.append(
            "every send says what it is for: pass the request, in the words of the person "
            "it is for, or the name of the routine")
    if method is None and rendered is not None:
        method = rendered.method
    real = not owner.status().fake  # type: ignore[attr-defined]
    problems += check(method, rendered, limits, ledger or Ledger(), real=real,
                      replicates=replicates)
    return problems
