"""Every job, event, handle and result, to and from data that JSON can carry.

`to_wire(obj)` turns anything that crosses the owner interface into dicts, lists,
strings, numbers, booleans and None; `from_wire(data)` turns that back into the objects.
`dumps` and `loads` are the same through `json`. One codec for all of them rather than a
method on each class, because the rule is the same for every one -- a frozen dataclass
is `{"@type": its class name, field: value, ...}` -- and a class that needs anything else
is one of the four named below.

**The exceptions, each for a reason.**

- A `Method` travels as its canonical TOML text (`method.dumps`) plus its load-time
  warnings, which the text does not carry. The document's path, where a job has one,
  is already a field of the job.
- An `Instrument` travels as its document's text (`instrument.dumps`), which is the one
  form of it that holds a date.
- A `Batch` travels without `mz`. The summed spectrum is a display product of hundreds
  of kilobytes a batch, published about fifteen times a second, and nothing on the far
  side draws it: mainspring is the only viewer. `tic` and `time_stamps` travel, so
  `scans` reads the same on both sides; `mz` comes back empty.
- A `Found` travels without its open `Box`, and a `Discovery` without its `_boxes`.
  A port is held by the owner that opened it and cannot be sent anywhere; the far side
  learns names and ports, which is all a front end ever did with them.

A private field (a leading underscore) never travels. Everything else must be one of the
types above -- a dataclass only if `wire_types` names it, so a `Box` that happens to be
a dataclass is refused rather than half-sent -- and anything that is not raises
`TypeError` naming the class and field. That is the whole of what makes
`tools/check_public.py`'s round trip of every `Event` subclass mean something: a new
event with a field this codec cannot carry fails there, the day it is written, rather
than the day a daemon first tries to send it.

A JSON array always comes back as a tuple, because every sequence field on every class
here is one. The one result that is a list in process -- the `Run`s of an `Acquire` --
comes back as a tuple of them.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import types
import typing
from collections.abc import Mapping

import numpy as np

from .. import instrument as _instrument
from .. import method as _method
from ..acq import Event, FoldRecord, FrameRecord, Run, Snapshot
from ..acq.wire import Batch
from ..instrument import Instrument
from ..method import Method, RfChannel
from ..mips import BoxState, Discovery, Found, Silent
from ..mips.discovery import PortInfo
from .interface import Handle, OwnerStatus, Progress, Said
from .jobs import Armed, ConsoleStatus, Discover, Job, SendResult
from .lock import Holder

__all__ = ["TYPE_KEY", "dumps", "example", "from_wire", "loads", "to_wire", "wire_types"]

TYPE_KEY = "@type"
"""The key that marks an object as a class rather than a mapping. Chosen so that it
cannot collide with a key a mapping here holds: box commands, `config.txt` keys and box
names never begin with `@`."""

_PLAIN = (Snapshot, BoxState, Run, FrameRecord, FoldRecord, SendResult, Armed,
          ConsoleStatus, Discovery, Found, Silent, PortInfo, Handle, Progress, OwnerStatus,
          Holder, RfChannel)
"""The dataclasses that cross the interface other than jobs and events, which are
collected by walking their subclasses so a new one is included without a list.
`RfChannel` is here for `wire_fingerprint`, which carries a method's RF declarations
as they are: without it a `Send` of any method that declares an RF head finished in
the daemon and never, as far as a client could tell, anywhere else (lab record,
task 73)."""

_LEFT_BEHIND = {(Found, "box")}
"""Fields that never cross a process boundary, beside the private ones."""


def _subclasses(root: type) -> list[type]:
    """`root` and everything below it, without the ghosts `slots=True` leaves.

    `dataclass(slots=True)` builds a second class and discards the one it was given,
    but the discarded one stays in its base's `__subclasses__()` until it is collected,
    so every slotted event would be found twice under one name. The one kept is the
    one its module actually holds under that name.
    """
    found, stack = [], [root]
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        module = sys.modules.get(cls.__module__)
        held = getattr(module, cls.__qualname__, cls) if module is not None else cls
        if held is cls or not isinstance(held, type):
            found.append(cls)
    return found


def wire_types() -> dict[str, type]:
    """Every class the codec knows, by the name it travels under.

    Worked out on each call rather than once at import, so an `Event` subclass defined
    in a module imported later is known as soon as it exists. Two classes of one name
    would make the tag ambiguous, and are refused here rather than decoded as whichever
    came first.
    """
    table: dict[str, type] = {}
    for cls in (*_subclasses(Event), *_subclasses(Job), *_PLAIN, Batch, Method,
                Instrument):
        other = table.setdefault(cls.__name__, cls)
        if other is not cls:
            raise TypeError(f"two wire types are both called {cls.__name__}: "
                            f"{other.__module__} and {cls.__module__}")
    return table


# -- out ---------------------------------------------------------------------------


def to_wire(obj: object) -> object:
    """`obj` as JSON-ready data. `TypeError` for anything the codec cannot carry."""
    return _out(obj, "value", set(wire_types().values()))


def _out(obj: object, where: str, known: set[type]) -> object:
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, (int, float)):
        return obj
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, (tuple, list)):
        return [_out(item, f"{where}[]", known) for item in obj]
    if isinstance(obj, Method):
        return {TYPE_KEY: "Method", "text": _method.dumps(obj),
                "warnings": list(obj.warnings)}
    if isinstance(obj, Instrument):
        return {TYPE_KEY: "Instrument", "text": _instrument.dumps(obj)}
    if isinstance(obj, Batch):
        return {TYPE_KEY: "Batch",
                "tic": obj.tic.tolist(), "tic_dtype": str(obj.tic.dtype),
                "time_stamps": obj.time_stamps.tolist(),
                "time_stamps_dtype": str(obj.time_stamps.dtype),
                "received_at": obj.received_at}
    if type(obj) in known:
        cls = type(obj)
        data: dict[str, object] = {TYPE_KEY: cls.__name__}
        for spec in dataclasses.fields(obj):
            if spec.name.startswith("_") or (cls, spec.name) in _LEFT_BEHIND:
                continue
            data[spec.name] = _out(getattr(obj, spec.name),
                                   f"{cls.__name__}.{spec.name}", known)
        return data
    if isinstance(obj, Mapping):
        out: dict[str, object] = {}
        for key, value in obj.items():
            if not isinstance(key, str) or key.startswith("@"):
                raise TypeError(f"{where}: a mapping key must be a string not beginning "
                                f"with @, not {key!r}")
            out[key] = _out(value, f"{where}[{key!r}]", known)
        return out
    raise TypeError(f"{where}: {type(obj).__name__} has no wire form")


# -- in ----------------------------------------------------------------------------


def from_wire(data: object) -> object:
    """The objects `to_wire` was given, from what it returned (or its JSON)."""
    return _in(data, wire_types())


def _in(data: object, table: dict[str, type]) -> object:
    if isinstance(data, list):
        return tuple(_in(item, table) for item in data)
    if not isinstance(data, dict):
        return data
    if TYPE_KEY not in data:
        return {key: _in(value, table) for key, value in data.items()}
    name = data[TYPE_KEY]
    if name == "Method":
        loaded = _method.loads(data["text"])
        return dataclasses.replace(loaded, warnings=tuple(data.get("warnings", ())))
    if name == "Instrument":
        return _instrument.loads(data["text"])
    if name == "Batch":
        return Batch(mz=np.zeros(0),
                     tic=np.asarray(data["tic"], dtype=data["tic_dtype"]),
                     time_stamps=np.asarray(data["time_stamps"],
                                            dtype=data["time_stamps_dtype"]),
                     received_at=data["received_at"])
    cls = table.get(name)
    if cls is None:
        raise TypeError(f"no wire type called {name!r}")
    return cls(**{key: _in(value, table) for key, value in data.items()
                  if key != TYPE_KEY})


def dumps(obj: object) -> str:
    return json.dumps(to_wire(obj), ensure_ascii=False, separators=(",", ":"))


def loads(text: str) -> object:
    return from_wire(json.loads(text))


# -- one of each -------------------------------------------------------------------

_EXAMPLE_METHOD = f"""\
schema_version = {_method.SCHEMA_VERSION}
start = [["box1", "TBLSTRT"]]

[metadata]
name = "wire-example"
created = 2026-09-22

[acquisition]
frames = 1
scans = 10
accumulations = 1
repetition_mode = "per_repetition"
keep_raw = true
file_stem = "wire-example"

[[boxes]]
name = "box1"
port = "COM3"
setup = ["STBLCLK,EXT"]
load = ["STBLDAT;0:[A:1,10:];"]
arm = ["SMOD,TBL"]
"""


def example(cls: type) -> object:
    """One instance of `cls`, every field filled with something of its declared type.

    For the round trips in `tools/check_public.py` and the tests: built from the type
    hints, so a class gains an example the moment it is written and nobody keeps a list.
    A `Union` takes its first type that is not None, so an optional field is exercised
    with a value rather than left at its default.
    """
    if cls in (Method, Instrument, Batch):
        return _sample(cls, cls.__name__)
    return _filled(cls)


def _filled(cls: type) -> object:
    hints = typing.get_type_hints(cls)
    values: dict[str, object] = {}
    for spec in dataclasses.fields(cls):
        if not spec.init or spec.name.startswith("_") or (cls, spec.name) in _LEFT_BEHIND:
            continue
        values[spec.name] = _sample(hints[spec.name], f"{cls.__name__}.{spec.name}")
    return cls(**values)


def _sample(hint: object, where: str) -> object:
    origin = typing.get_origin(hint)
    arguments = typing.get_args(hint)
    if origin in (typing.Union, types.UnionType):
        return _sample(next(arg for arg in arguments if arg is not type(None)), where)
    if hint is str or hint is object or hint is typing.Any:
        return "x"
    if hint is bool:
        return True
    if hint is int:
        return 3
    if hint is float:
        return 1.5
    if hint is tuple:
        return ()
    if origin is tuple:
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return (_sample(arguments[0], where),)
        return tuple(_sample(arg, where) for arg in arguments)
    if origin in (dict, Mapping):
        return {"key": _sample(arguments[1], where)}
    if hint is Method:
        return _method.loads(_EXAMPLE_METHOD)
    if hint is Instrument:
        return Instrument(name="wire example")
    if hint is Batch:
        return Batch(mz=np.zeros(0), tic=np.array([4, 5], dtype=np.int64),
                     time_stamps=np.array([0, 258000], dtype=np.int64), received_at=2.5)
    if hint is Event:
        return Said(line="x")
    if hint is Job:
        return Discover()
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        return _filled(hint)
    raise TypeError(f"{where}: no example for {hint!r}")
