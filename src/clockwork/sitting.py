"""The sitting: the instrument document and the limits in force, as one daemon carries them.

Before clockwork 1.2.0 each client loaded its own: the MCP server and every verb of the
command line took `--instrument`, `--limits` and `--project-limits`, and the window took
none. Two clients of one daemon could therefore enforce two sets of limits, and a run
started at the window was held to none and recorded against none. Now `clockwork serve`
takes the three flags, `hello` reports what it loaded, and every client defaults to the
daemon's where its own flags are absent, so every client shows and enforces one set (Matt,
2026-11-18, the window revision's E6; lab record, task 165).

`instrument_and_limits` is what every route loads through: moved here from
`clockwork.mcp.server`, which still offers it under the same name, so that the daemon
loads the limits without importing the MCP tools. `Sitting` is the loaded result with the
paths it came from; its `entry` is the day record's first line (`clockwork.dayrecord`),
with each document's SHA-256 so that a reader can confirm months later which limits a
day's runs were checked against.

Qt-free.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass

from . import envelope
from . import instrument as instrument_module
from .envelope import Limits
from .instrument import UNCALIBRATED, Instrument, Vertical

__all__ = ["SIMULATED", "Sitting", "digest", "from_daemon", "instrument_and_limits", "load"]

SIMULATED = Instrument(
    name="simulated instrument",
    description="The stand-in a --fake session acquires under when given no document.",
    vertical=Vertical(full_scale_v=0.5, offset_v=0.251, inverted=False))
"""What `--fake` acquires under with no `--instrument`. The loop refuses a document that
states no channel offset, as the window does, and a rehearsal has no instrument to have
written one for; this states the lab's full scale and the stand-in console's own
offset, and names itself so that no file mistakes it for a measurement."""


def instrument_and_limits(*, fake: bool, instrument_path: str = "", limits_path: str = "",
                          project_limits_path: str = "") -> tuple[Instrument, Limits | None]:
    """The instrument document runs are acquired under, and the limits in force.

    `SIMULATED` under `fake` and `UNCALIBRATED` otherwise when no document is named.
    `limits_path` defaults to the `limits.toml` beside `instrument_path` if there is one;
    `project_limits_path`, never defaulted, is a project's limits, combined into those
    (`clockwork.envelope.combine`). `ValueError` with one sentence for a document or
    limits that were named or found and could not be read, and for a project's limits
    with no standing limits to narrow: a server that quietly ran without the limits it
    was pointed at would refuse every send and not say why. Shared by `clockwork serve`,
    `clockwork mcp` and the verbs."""
    instrument = SIMULATED if fake else UNCALIBRATED
    if instrument_path:
        try:
            instrument = instrument_module.load(instrument_path)
        except (OSError, ValueError) as exc:
            raise ValueError(f"the instrument document {instrument_path} could not be "
                             f"read: {exc}") from exc
    chosen = limits_path or envelope.default_path(instrument_path)
    if not chosen or not (limits_path or os.path.isfile(chosen)):
        if project_limits_path:
            raise ValueError(f"the project's limits {project_limits_path} narrow the "
                             "instrument's standing limits, and none were found: give "
                             "--limits, or --instrument with a limits.toml beside it")
        return instrument, None
    try:
        standing = envelope.load(chosen)
    except (OSError, envelope.LimitsError) as exc:
        raise ValueError(f"the standing limits {chosen} could not be read: {exc}") from exc
    if not project_limits_path:
        return instrument, standing
    try:
        project = envelope.load(project_limits_path, project=True)
    except (OSError, envelope.LimitsError) as exc:
        raise ValueError(f"the project's limits {project_limits_path} could not be read: "
                         f"{exc}") from exc
    try:
        return instrument, envelope.combine(standing, project)
    except envelope.LimitsError as exc:
        raise ValueError(f"the project's limits {project_limits_path} would widen the "
                         f"standing limits {chosen}: {exc}") from exc


def digest(path: str) -> dict[str, str] | None:
    """`{path, sha256}` of the file at `path`, as the day record keeps a document; None
    for no path. A file that cannot be read keeps its path and an empty hash."""
    if not path:
        return None
    absolute = os.path.abspath(path)
    try:
        with open(absolute, "rb") as handle:
            sha = hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        sha = ""
    return {"path": absolute, "sha256": sha}


@dataclass(frozen=True, slots=True)
class Sitting:
    """What one daemon was started with: the instrument document and the limits, loaded,
    and the paths they were loaded from (empty for none)."""

    instrument: Instrument = UNCALIBRATED
    limits: Limits | None = None
    instrument_path: str = ""
    limits_path: str = ""
    """The standing limits' file, named or found beside the instrument document."""
    project_limits_path: str = ""

    def entry(self) -> dict[str, object]:
        """The three documents as the day record's `sitting` line keeps them."""
        return {"instrument": digest(self.instrument_path),
                "limits": digest(self.limits_path),
                "project_limits": digest(self.project_limits_path)}

    def hello(self) -> dict[str, str]:
        """The three paths as `hello` reports them, absolute, empty for none."""
        return {"instrument": _absolute(self.instrument_path),
                "limits": _absolute(self.limits_path),
                "project_limits": _absolute(self.project_limits_path)}


def from_daemon(hello: object, *, instrument_path: str = "", limits_path: str = "",
                project_limits_path: str = "",
                default_instrument: Callable[[], str] | None = None,
                ) -> tuple[str, str, str, bool]:
    """The three paths a client of a daemon loads, each its own flag where given and the
    daemon's (`hello`) where not: `(instrument, limits, project_limits, defaulted)`.

    The standing limits are the daemon's only where the client named no instrument of
    its own, whose `limits.toml` is otherwise the default as it always was. An instrument
    neither named nor the daemon's is `default_instrument()`'s, except over a daemon that
    is `--fake`, whose simulated boxes never run under the instrument's own document;
    `defaulted` says it was taken so."""
    instrument = instrument_path or str(getattr(hello, "instrument", "") or "")
    limits = limits_path or ("" if instrument_path
                             else str(getattr(hello, "limits", "") or ""))
    project = project_limits_path or str(getattr(hello, "project_limits", "") or "")
    defaulted = False
    if not instrument and not getattr(hello, "fake", False) and default_instrument:
        instrument = default_instrument() or ""
        defaulted = bool(instrument)
    return instrument, limits, project, defaulted


def _absolute(path: str) -> str:
    return os.path.abspath(path) if path else ""


def load(*, fake: bool, instrument_path: str = "", limits_path: str = "",
         project_limits_path: str = "") -> Sitting:
    """`instrument_and_limits`, kept with the paths it read. The standing limits' path is
    the one actually loaded, which is the `limits.toml` beside the instrument document
    where none was named. `ValueError` as there."""
    instrument, limits = instrument_and_limits(
        fake=fake, instrument_path=instrument_path, limits_path=limits_path,
        project_limits_path=project_limits_path)
    return Sitting(instrument=instrument, limits=limits, instrument_path=instrument_path,
                   limits_path=limits.path if limits is not None else "",
                   project_limits_path=project_limits_path if limits is not None else "")
