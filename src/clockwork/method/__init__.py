"""The saved experiment: per-box command strings plus acquisition settings.

A method is what a trainee loads, sends and acquires with; it is a flat document
on disk and is stamped into every acquisition it produces, so that any UIMF file
can be traced to the exact strings that made it. Filled by the lab record's
task 07. No Qt here.

The document is deliberately flat TOML, not the deferred Layer 1 Pydantic schema
(lab record, decision 0007) -- see `docs/method-file-format.md` in the code repo
for the format and the provenance stamp it feeds.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import tomllib
from dataclasses import dataclass, field

import tomli_w

import clockwork

SCHEMA_VERSION = 1


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
class Acquisition:
    frames: int
    scans: int
    accumulations: int
    file_stem: str


@dataclass(frozen=True, slots=True)
class BoxMethod:
    """One box's command strings, in the order they are sent."""

    name: str
    port: str
    strings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Method:
    metadata: Metadata
    acquisition: Acquisition
    boxes: tuple[BoxMethod, ...]
    schema_version: int = SCHEMA_VERSION


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


def _positive_int(value: object, path: str, problems: list[str]) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        problems.append(f"{path}: expected a positive integer")
        return None
    return value


def from_dict(data: dict) -> Method:
    """Build and validate a `Method` from a parsed TOML document.

    Collects every problem before raising `MethodError`, so a trainee's typo-laden
    method reports all of it at once rather than one round trip per fix.
    """
    problems: list[str] = []

    schema_version = data.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        problems.append(
            f"schema_version: expected {SCHEMA_VERSION}, got {schema_version!r}"
        )

    metadata_raw = _require_table(data, "metadata", problems)
    metadata = None
    if metadata_raw is not None:
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
        if None not in (frames, scans, accumulations, file_stem):
            acquisition = Acquisition(
                frames=frames, scans=scans, accumulations=accumulations, file_stem=file_stem
            )

    boxes_raw = data.get("boxes")
    boxes: list[BoxMethod] = []
    if not isinstance(boxes_raw, list) or not boxes_raw:
        problems.append("boxes: expected a non-empty array of tables")
    else:
        seen_names: set[str] = set()
        for i, box_raw in enumerate(boxes_raw):
            path = f"boxes[{i}]"
            if not isinstance(box_raw, dict):
                problems.append(f"{path}: expected a table")
                continue
            name = _non_empty_str(box_raw.get("name"), f"{path}.name", problems)
            port = _non_empty_str(box_raw.get("port"), f"{path}.port", problems)
            strings_raw = box_raw.get("strings")
            if not isinstance(strings_raw, list) or not strings_raw or not all(
                isinstance(s, str) and s.strip() for s in strings_raw
            ):
                problems.append(f"{path}.strings: expected a non-empty array of non-empty strings")
                strings = None
            else:
                strings = tuple(strings_raw)
            if name is not None:
                if name in seen_names:
                    problems.append(f"{path}.name: duplicate box name {name!r}")
                seen_names.add(name)
            if None not in (name, port, strings):
                boxes.append(BoxMethod(name=name, port=port, strings=strings))

    if problems:
        raise MethodError(problems)

    assert metadata is not None and acquisition is not None
    return Method(metadata=metadata, acquisition=acquisition, boxes=tuple(boxes))


def to_dict(method: Method) -> dict:
    """The plain dict `dumps`/`save` write, and what a stamp's hash covers."""
    return {
        "schema_version": method.schema_version,
        "metadata": {
            "name": method.metadata.name,
            "created": method.metadata.created,
            "description": method.metadata.description,
        },
        "acquisition": {
            "frames": method.acquisition.frames,
            "scans": method.acquisition.scans,
            "accumulations": method.acquisition.accumulations,
            "file_stem": method.acquisition.file_stem,
        },
        "boxes": [
            {"name": box.name, "port": box.port, "strings": list(box.strings)}
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
    "SCHEMA_VERSION",
    "MethodError",
    "Metadata",
    "Acquisition",
    "BoxMethod",
    "Method",
    "from_dict",
    "to_dict",
    "loads",
    "load",
    "dumps",
    "save",
    "stamp",
]
