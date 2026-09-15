"""The saved experiment: per-box command strings plus acquisition settings.

A method is what a trainee loads, sends and acquires with; it is a flat document
on disk and is stamped into every acquisition it produces, so that any UIMF file
can be traced to the exact strings that made it. Filled by the lab record's
task 07, reshaped by its task 14. No Qt here.

The document is deliberately flat TOML, not the deferred Layer 1 Pydantic schema
(lab record, decision 0007) -- see `docs/method-file-format.md` in the code repo
for the format and the provenance stamp it feeds.

Schema 2 splits a box's strings into three phases and lifts the start out of
them:

    setup   persists on the box; sent on demand, not per acquisition
    load    sent once per acquisition (a table, a compression table)
    arm     puts the box in table mode and waits for it to be ready
    start   a method-level ordered list of (box, command) steps, run once
            per console frame; cross-box order is part of the experiment
    reset   the steps a technical replicate needs before starting again

`acquisition.enable` is optional and names the digital output that gates the
digitizer, which is the one instrument fact the acquisition loop cannot infer
from the strings (see `Enable`).

Schema 1, which stored one flat `strings` list per box and had no start list, is
rejected rather than mapped onto this shape (lab record, task 14): it predates
the first real experiment on record and no method written against it can express
one.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import tomllib
from dataclasses import dataclass, field

import tomli_w

import clockwork

SCHEMA_VERSION = 2

REPETITION_MODES = ("per_repetition", "single_frame")
"""How a method frame's repetitions are divided into console frames.

`per_repetition`: each repetition is its own console frame of `scans`, released
by its own start edge. `single_frame`: one console frame of
`scans * accumulations` covers the whole method frame, released once.
"""

DEFAULT_REPETITION_MODE = "per_repetition"
"""What a document that omits `repetition_mode` means.

`per_repetition` is the decision of record (lab record, task 02). The number
that could change it is the console's per-repetition restart gap, which the lab
record's task 03 measures; changing the default is an edit to this line.
"""

DEFAULT_KEEP_RAW = True
"""Whether the raw per-repetition file survives the fold step by default.

Kept, because discarding it is irreversible and it is the only record of how one
repetition differed from the next (lab record, task 02).
"""

NOTIFY_ON_SCANS_COUNT = 500
"""The console's batch size: `NotifyOnScansCount` in its `config.txt`.

Not a method setting -- it belongs to the console and clockwork only reads it --
but the enable window below is measured in it, so the number this package
assumes has to be written down somewhere. A run that meets a console configured
differently passes its own value to `enable_fall_tick`.
"""


class MethodError(ValueError):
    """A method document failed to parse or validate.

    Carries every problem found, not just the first, in `.problems`.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True, slots=True)
class Metadata:
    name: str
    created: _dt.date
    description: str = ""


@dataclass(frozen=True, slots=True)
class Enable:
    """The digital output that gates the digitizer, as a method declares it.

    `box` is the sequencer among this method's boxes and `channel` one of the
    MIPS digital outputs it drives: on this instrument DIOA, into the card's
    Control I/O 2. Declared rather than assumed, because which line carries the
    enable is a fact about how an instrument is cabled and not a property of a
    document -- nothing else in this package knows it, and the strings a method
    carries do not say.

    What it buys is the one thing a table cannot do for itself. A `single_frame`
    method's table loops on the box and raises the enable once, so a second
    method frame would be released against a gate that is already high; with
    this declared, `clockwork.acq.loop` lowers the line by command between
    method frames instead and acquires the trainee's table as written. Without
    it that combination is still refused (`clockwork.acq.loop.refusals`).

    The lab record's task 31 wants the same pair for a different question: which
    table to read the enable's edges out of when a method is checked against
    itself.
    """

    box: str
    channel: str


@dataclass(frozen=True, slots=True)
class Acquisition:
    frames: int
    scans: int
    accumulations: int
    file_stem: str
    repetition_mode: str = DEFAULT_REPETITION_MODE
    keep_raw: bool = DEFAULT_KEEP_RAW
    enable: Enable | None = None
    """The digitizer's gate line, or None where the document does not say."""

    @property
    def frame_length(self) -> int:
        """Scans in one console frame, which the repetition mode decides.

        `per_repetition` acquires one ion mobility experiment per frame, so a
        frame is `scans` long; `single_frame` acquires a whole method frame at
        once, so it is `scans * accumulations` long. The fold step reduces
        either shape by `ScanNum` modulo `scans` (lab record, task 02).
        """
        if self.repetition_mode == "single_frame":
            return self.scans * self.accumulations
        return self.scans

    @property
    def console_frames(self) -> int:
        """Console frames one method frame costs: `accumulations`, or one."""
        if self.repetition_mode == "single_frame":
            return 1
        return self.accumulations


def enable_fall_tick(
    frame_length: int, notify_on_scans_count: int = NOTIFY_ON_SCANS_COUNT
) -> int:
    """The tick a sequencer table must lower the digitizer's enable on.

    One number, in one place, because three things derive it and they must not
    drift: the note the design lives in, the bench script that rewrites a table
    for a shorter run, and the tests. `table_period` is the loop period that
    goes with it.

    Why a whole batch is `docs/console-protocol.md`, "A frame needs triggers
    past the ones it counts": the console holds a batch until it has seen the
    next trigger's marker and fetches markers a whole batch of hunks at a time,
    and a record in which everything was suppressed carries one hunk, which is
    the worst case. So the margin is `notify_on_scans_count` and not the one
    push that the count alone would suggest.

    Erring late costs the frame nothing: a push past the last counted scan is
    one the console has already stopped counting. Erring early leaves the
    console one batch short and the frame never finishes -- measured, a whole
    `NotifyOnScansCount` short, on every frame of every run that tried it (lab
    record, task 33).
    """
    return frame_length + notify_on_scans_count


def table_period(
    frame_length: int, notify_on_scans_count: int = NOTIFY_ON_SCANS_COUNT
) -> int:
    """The loop period that goes with `enable_fall_tick`: one tick past it."""
    return enable_fall_tick(frame_length, notify_on_scans_count) + 1


@dataclass(frozen=True, slots=True)
class BoxMethod:
    """One box's strings, in three phases, each sent in the order written."""

    name: str
    port: str
    setup: tuple[str, ...] = field(default_factory=tuple)
    load: tuple[str, ...] = field(default_factory=tuple)
    arm: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Step:
    """One step of the method-level `start` or `reset` sequence."""

    box: str
    command: str


@dataclass(frozen=True, slots=True)
class Method:
    metadata: Metadata
    acquisition: Acquisition
    boxes: tuple[BoxMethod, ...]
    start: tuple[Step, ...] = field(default_factory=tuple)
    reset: tuple[Step, ...] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSION
    warnings: tuple[str, ...] = field(default_factory=tuple, compare=False)
    """What was repaired on the way in, one line each.

    Excluded from equality and from `to_dict`, so a document that loaded with
    warnings still round-trips: `dumps` writes the repaired strings, and loading
    that text again warns about nothing.
    """

    def box(self, name: str) -> BoxMethod:
        """The box named `name`. `KeyError` if the method has no such box."""
        for entry in self.boxes:
            if entry.name == name:
                return entry
        raise KeyError(name)


def _require_table(data: dict, key: str, problems: list[str]) -> dict | None:
    value = data.get(key)
    if not isinstance(value, dict):
        problems.append(f"{key}: missing or not a table")
        return None
    return value


def _non_empty_str(value: object, path: str, problems: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{path}: expected a non-empty string")
        return None
    return value


def _identifier(value: object, path: str, problems: list[str]) -> str | None:
    """A name or a port: non-empty, and not surrounded by whitespace.

    Whitespace here is rejected rather than stripped, unlike a command string:
    a box named `"box2 "` matches nothing in the start list, and "no such box" is a
    worse message than the one this produces.
    """
    text = _non_empty_str(value, path, problems)
    if text is None:
        return None
    if text != text.strip():
        problems.append(f"{path}: has leading or trailing whitespace ({text!r})")
        return None
    return text


def _command(value: object, path: str, problems: list[str], warnings: list[str]) -> str | None:
    """One string bound for a box: non-empty, stripped, and warned about.

    The strings a trainee pastes carry whatever whitespace their source file
    had, and a stray tab on the end of a command is invisible in an editor and
    real on the wire. Stripping is the repair; the warning is what makes it
    visible.
    """
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{path}: expected a non-empty string")
        return None
    if value != value.strip():
        warnings.append(f"{path}: stripped surrounding whitespace from {value!r}")
    return value.strip()


def _no_unknown_keys(
    table: dict, path: str, known: tuple[str, ...], problems: list[str]
) -> None:
    """Reject a key this schema does not define, with a hint where one exists.

    A misspelled `scan` would otherwise be silently ignored and the method would
    run at the wrong length. The hint matters for `start` and `reset`, which are
    method-level keys and land inside the last `[[boxes]]` table whenever they
    are written after it: TOML assigns a bare key to the table it follows.
    """
    for key in table:
        if key in known:
            continue
        hint = ""
        if path.startswith("boxes[") and key in ("start", "reset"):
            hint = (
                f"; {key!r} is a method-level key and must be written before the first "
                "[[boxes]] table, or TOML reads it as part of the last one"
            )
        problems.append(f"{path}.{key}: not a key of schema {SCHEMA_VERSION}{hint}")


def _positive_int(value: object, path: str, problems: list[str]) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        problems.append(f"{path}: expected a positive integer")
        return None
    return value


def _phase(
    box_raw: dict, key: str, path: str, problems: list[str], warnings: list[str]
) -> tuple[str, ...] | None:
    """One of a box's three phases: an array of command strings, possibly empty."""
    raw = box_raw.get(key, [])
    if not isinstance(raw, list):
        problems.append(f"{path}.{key}: expected an array of strings")
        return None
    strings = [_command(s, f"{path}.{key}[{i}]", problems, warnings) for i, s in enumerate(raw)]
    if None in strings:
        return None
    return tuple(s for s in strings if s is not None)


def _enable(acquisition_raw: dict, problems: list[str]) -> Enable | None:
    """`acquisition.enable`, which a document may leave out entirely.

    The channel is not checked against the digital outputs here. Which letters
    MIPS accepts is the wire protocol's business and this module does not hold a
    second copy of it, so a channel that is not an output is caught by
    `clockwork.acq.loop.refusals`, which builds the command through
    `clockwork.mips.dio_command` and reports what it says.
    """
    raw = acquisition_raw.get("enable")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append(
            "acquisition.enable: expected a table with box and channel, as in "
            '{ box = "auklet", channel = "A" }'
        )
        return None
    _no_unknown_keys(raw, "acquisition.enable", ("box", "channel"), problems)
    box = _identifier(raw.get("box"), "acquisition.enable.box", problems)
    channel = _identifier(raw.get("channel"), "acquisition.enable.channel", problems)
    if box is None or channel is None:
        return None
    return Enable(box=box, channel=channel)


def _sequence(
    data: dict,
    key: str,
    known_boxes: set[str],
    problems: list[str],
    warnings: list[str],
) -> tuple[Step, ...] | None:
    """The `start` or `reset` list: `[["box", "COMMAND"], ...]` in send order."""
    raw = data.get(key, [])
    if not isinstance(raw, list):
        problems.append(f"{key}: expected an array of [box, command] pairs")
        return None
    steps: list[Step] = []
    ok = True
    for i, entry in enumerate(raw):
        path = f"{key}[{i}]"
        if not isinstance(entry, list) or len(entry) != 2:
            problems.append(f"{path}: expected a [box, command] pair")
            ok = False
            continue
        box = _identifier(entry[0], f"{path}[0]", problems)
        command = _command(entry[1], f"{path}[1]", problems, warnings)
        if box is None or command is None:
            ok = False
            continue
        if known_boxes and box not in known_boxes:
            problems.append(f"{path}[0]: no box named {box!r} in this method")
            ok = False
            continue
        steps.append(Step(box=box, command=command))
    return tuple(steps) if ok else None


def from_dict(data: dict) -> Method:
    """Build and validate a `Method` from a parsed TOML document.

    Collects every problem before raising `MethodError`, so a trainee's typo-laden
    method reports all of it at once rather than one round trip per fix. Repairs
    that are safe to make silently are made and reported in `Method.warnings`
    instead.
    """
    problems: list[str] = []
    warnings: list[str] = []

    schema_version = data.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        problems.append(
            f"schema_version: expected {SCHEMA_VERSION}, got {schema_version!r}"
        )
        if schema_version == 1:
            problems.append(
                "schema_version: version 1 is not supported; a box's strings are now the "
                "setup, load and arm phases, and the start sequence is a method-level list"
            )

    _no_unknown_keys(
        data,
        "(document)",
        ("schema_version", "metadata", "acquisition", "boxes", "start", "reset"),
        problems,
    )

    metadata_raw = _require_table(data, "metadata", problems)
    metadata = None
    if metadata_raw is not None:
        _no_unknown_keys(metadata_raw, "metadata", ("name", "created", "description"), problems)
        name = _non_empty_str(metadata_raw.get("name"), "metadata.name", problems)
        created = metadata_raw.get("created")
        if not isinstance(created, _dt.date):
            problems.append("metadata.created: expected a TOML date (YYYY-MM-DD)")
            created = None
        description = metadata_raw.get("description", "")
        if not isinstance(description, str):
            problems.append("metadata.description: expected a string")
            description = ""
        if name is not None and created is not None:
            metadata = Metadata(name=name, created=created, description=description)

    acquisition_raw = _require_table(data, "acquisition", problems)
    acquisition = None
    if acquisition_raw is not None:
        _no_unknown_keys(
            acquisition_raw,
            "acquisition",
            (
                "frames",
                "scans",
                "accumulations",
                "file_stem",
                "repetition_mode",
                "keep_raw",
                "enable",
            ),
            problems,
        )
        frames = _positive_int(acquisition_raw.get("frames"), "acquisition.frames", problems)
        scans = _positive_int(acquisition_raw.get("scans"), "acquisition.scans", problems)
        accumulations = _positive_int(
            acquisition_raw.get("accumulations"), "acquisition.accumulations", problems
        )
        file_stem = _non_empty_str(
            acquisition_raw.get("file_stem"), "acquisition.file_stem", problems
        )
        if file_stem is not None and ("/" in file_stem or "\\" in file_stem):
            problems.append("acquisition.file_stem: must not contain a path separator")
            file_stem = None
        mode = acquisition_raw.get("repetition_mode", DEFAULT_REPETITION_MODE)
        if mode not in REPETITION_MODES:
            problems.append(
                "acquisition.repetition_mode: expected one of "
                + ", ".join(repr(m) for m in REPETITION_MODES)
                + f", got {mode!r}"
            )
            mode = None
        keep_raw = acquisition_raw.get("keep_raw", DEFAULT_KEEP_RAW)
        if not isinstance(keep_raw, bool):
            problems.append("acquisition.keep_raw: expected true or false")
            keep_raw = None
        enable = _enable(acquisition_raw, problems)
        if None not in (frames, scans, accumulations, file_stem, mode, keep_raw):
            acquisition = Acquisition(
                frames=frames,
                scans=scans,
                accumulations=accumulations,
                file_stem=file_stem,
                repetition_mode=mode,
                keep_raw=keep_raw,
                enable=enable,
            )

    boxes_raw = data.get("boxes")
    boxes: list[BoxMethod] = []
    seen_names: set[str] = set()
    if not isinstance(boxes_raw, list) or not boxes_raw:
        problems.append("boxes: expected a non-empty array of tables")
    else:
        any_load = False
        for i, box_raw in enumerate(boxes_raw):
            path = f"boxes[{i}]"
            if not isinstance(box_raw, dict):
                problems.append(f"{path}: expected a table")
                continue
            _no_unknown_keys(
                box_raw, path, ("name", "port", "setup", "load", "arm"), problems
            )
            name = _identifier(box_raw.get("name"), f"{path}.name", problems)
            port = _identifier(box_raw.get("port"), f"{path}.port", problems)
            phases = {
                key: _phase(box_raw, key, path, problems, warnings)
                for key in ("setup", "load", "arm")
            }
            if name is not None:
                if name in seen_names:
                    problems.append(f"{path}.name: duplicate box name {name!r}")
                seen_names.add(name)
            if phases["load"]:
                any_load = True
            if name is not None and port is not None and None not in phases.values():
                boxes.append(
                    BoxMethod(
                        name=name,
                        port=port,
                        setup=phases["setup"],
                        load=phases["load"],
                        arm=phases["arm"],
                    )
                )
        if not any_load:
            problems.append(
                "boxes: no box has anything to load; every acquisition sends at least one "
                "load string"
            )

    if acquisition is not None and acquisition.enable is not None:
        if acquisition.enable.box not in seen_names:
            problems.append(
                f"acquisition.enable.box: {acquisition.enable.box!r} is not a box this "
                "method declares"
            )

    start = _sequence(data, "start", seen_names, problems, warnings)
    if start is not None and not start:
        problems.append(
            "start: expected a non-empty array of [box, command] pairs; the start sequence is "
            "what releases an acquisition"
        )
        start = None
    reset = _sequence(data, "reset", seen_names, problems, warnings)

    if problems:
        raise MethodError(problems)

    assert metadata is not None and acquisition is not None
    assert start is not None and reset is not None
    return Method(
        metadata=metadata,
        acquisition=acquisition,
        boxes=tuple(boxes),
        start=start,
        reset=reset,
        warnings=tuple(warnings),
    )


def to_dict(method: Method) -> dict:
    """The plain dict `dumps`/`save` write, and what a stamp's hash covers.

    `acquisition.enable` is written only where the method declares one, so a
    document that says nothing about the gate line round-trips to a document
    that still says nothing, and its stamp hashes the same bytes as before the
    key existed.
    """
    enable = method.acquisition.enable
    return {
        "schema_version": method.schema_version,
        "start": [[step.box, step.command] for step in method.start],
        "reset": [[step.box, step.command] for step in method.reset],
        "metadata": {
            "name": method.metadata.name,
            "created": method.metadata.created,
            "description": method.metadata.description,
        },
        "acquisition": {
            "frames": method.acquisition.frames,
            "scans": method.acquisition.scans,
            "accumulations": method.acquisition.accumulations,
            "repetition_mode": method.acquisition.repetition_mode,
            "keep_raw": method.acquisition.keep_raw,
            "file_stem": method.acquisition.file_stem,
            **({"enable": {"box": enable.box, "channel": enable.channel}}
               if enable is not None else {}),
        },
        "boxes": [
            {
                "name": box.name,
                "port": box.port,
                "setup": list(box.setup),
                "load": list(box.load),
                "arm": list(box.arm),
            }
            for box in method.boxes
        ],
    }


def loads(text: str) -> Method:
    """Parse and validate a method document from TOML text."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise MethodError([f"invalid TOML: {exc}"]) from exc
    return from_dict(data)


def load(path: str) -> Method:
    """Parse and validate a method document from a TOML file."""
    with open(path, "rb") as handle:
        try:
            data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise MethodError([f"invalid TOML: {exc}"]) from exc
    return from_dict(data)


def dumps(method: Method) -> str:
    """Serialize a method to canonical TOML text.

    Canonical in the sense the provenance stamp relies on: the same `Method`
    always dumps to the same bytes, which is what makes `stamp()`'s hash mean
    anything.
    """
    return tomli_w.dumps(to_dict(method))


def save(method: Method, path: str) -> None:
    """Write a method document to a TOML file."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(dumps(method))


def stamp(method: Method, *, console_version: str | None = None) -> dict[str, object]:
    """The provenance record for one acquisition made with `method`.

    Fields: the method's name, a hash and the full text of the method that
    produced the acquisition, the `clockwork` version, and the acquisition
    console's version if known. Where this lands -- `Global_Params` fields or a
    sidecar file beside the UIMF -- is for the writer that consumes it (lab
    record, task 06) to decide; this function only produces the record.
    """
    text = dumps(method)
    return {
        "method_name": method.metadata.name,
        "method_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "method_text": text,
        "clockwork_version": clockwork.__version__,
        "console_version": console_version,
    }


__all__ = [
    "DEFAULT_KEEP_RAW",
    "DEFAULT_REPETITION_MODE",
    "NOTIFY_ON_SCANS_COUNT",
    "REPETITION_MODES",
    "SCHEMA_VERSION",
    "MethodError",
    "Metadata",
    "Acquisition",
    "Enable",
    "BoxMethod",
    "Method",
    "Step",
    "from_dict",
    "to_dict",
    "loads",
    "load",
    "dumps",
    "enable_fall_tick",
    "save",
    "stamp",
    "table_period",
]
