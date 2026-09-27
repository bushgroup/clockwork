"""Every run under some directories as one table: a row per run, read off the files alone.

    manifest(paths)          the rows and their columns, JSON-ready
    to_csv(columns, rows)    the same as CSV text

**The file is the record and the manifest is derived** (lab record, task 89): nothing is
written at acquisition time for this, and a manifest of last month's directories is as
good as one of today's. Every column comes from a file's `Global_Params` -- clockwork's
stamp, a rendered run's knobs, labels and marks, its series, mainspring's run outcome --
from its first frame's `AverageTOFLength`, or from the run record beside it
(`clockwork.record`). Only `mainspring.uimf`'s ordinary reader opens a file, and no frame's
data is read, so a directory of a hundred runs is a second's work.

**One row per run, not per file.** A run's `<stem>.summed.uimf` is its row's file where
one exists and its raw `<stem>.uimf` otherwise (`clockwork.summary`'s pairing), so a
`keep_raw = false` run and a run that never folded both appear once. Paths are files or
directories, walked recursively for `.uimf`, in any number. **Nothing is dropped**: a file
that will not open, or a path that is not there, is a row whose `problem` says so, with
what its name alone gives (day, stem, initials).

**Columns** are `FIXED`, in that order, then one per knob, per mark and per label found
anywhere in the set, in the order first met. A knob is named as its template names it
(`pulse_ms`), a mark as `<mark>_ms` and `<mark>_scan`, a label other than `sample` as
`label_<name>`; a name that would repeat an earlier column is prefixed with its kind
(`mark_`, `knob_`). A file that lacks a column has `None` there, an empty CSV cell: a
hand-written run has empty knob columns, never zeros, and a run with no record has no note
count rather than a count of 0.

**What the fixed columns mean:**

- `file`: the absolute path read. `kind`: `raw`, `summed` or `foreign`
  (`clockwork.summary.file_kind`), `None` for a file that would not open.
- `day`, `stem`, `initials`: off the name where it is clockwork's
  `YYMMDD_<initials>_<number>` (`clockwork.naming.parse_stem`); otherwise the stem is the
  name without its suffix and the day is the date part of `DateStarted`.
- `template_hash`, `method_hash`: `ClockworkTemplateHash` and `ClockworkMethodHash`, whole.
  `method`: the method's name, PNNL's `AcquisitionMethod`.
- `sample`: a rendered run's `sample` label. `conditions`: the operator's free text
  (`ClockworkConditions`), its lines joined with `; `, which is where a hand-written run's
  sample is if it is anywhere.
- `outcome`, `reason`, `repetitions_planned`, `repetitions_acquired`: mainspring's run
  outcome record, `unknown` and empty for a file written before there was one.
- `series_id`, `series_index`, `series_position`, `series_seed`: `ClockworkSeries...`.
- `declared_us`, `declared_by`, `measured_us`, `ratio`: the pusher-period check as the
  acquisition makes it (`clockwork.acq.check_pusher_period`), the declared period being the
  template's `ClockworkTickUs` (`template`) or else the instrument document's
  `ClockworkPusherPeriodUs` (`instrument`), and the ratio measured over declared.
- `verdict`: the newest verdict on this stem in the run record's `verdicts` section, where
  a record has one; `notes`: how many notes the record holds (a record is a request's, so
  every run of one request shows the same count); `record`: the record's file name. The
  record is the one whose request id the file stamps as its series, or else one whose
  `files` section, or own first stem, names this stem.
- `problem`: why the row is short, or `None`.

Qt-free.
"""

from __future__ import annotations

import csv
import datetime as _dt
import glob
import io
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from mainspring.uimf import SUMMED_SUFFIX, UimfFile

from .acq.loop import check_pusher_period
from .acq.uimf import KNOB_PREFIX, LABEL_PREFIX, MARK_PREFIX, RAW_SUFFIX
from .method import template as template_module
from .naming import parse_stem
from .record import RECORD_SUFFIX, holds, newest_verdict
from .summary import file_kind

__all__ = ["FIXED", "manifest", "to_csv", "write_csv"]

FIXED = (
    "file", "kind", "day", "stem", "initials", "template_hash", "method_hash", "method",
    "sample",
    "conditions", "outcome", "reason", "repetitions_planned", "repetitions_acquired",
    "series_id", "series_index", "series_position", "series_seed", "declared_us",
    "declared_by", "measured_us", "ratio", "verdict", "notes", "record", "problem",
)
"""The columns every manifest has, in order; the per-template ones follow them."""

_DECLARED_BY = {"the template": "template", "the instrument document": "instrument"}


# --- finding the runs ---------------------------------------------------------------


def _stem_of(path: str) -> str:
    name = os.path.basename(path)
    if name.lower().endswith(SUMMED_SUFFIX):
        return name[: -len(SUMMED_SUFFIX)]
    return name[: -len(RAW_SUFFIX)] if name.lower().endswith(RAW_SUFFIX) else name


def _uimf_under(directory: str) -> list[str]:
    found = []
    for root, folders, names in os.walk(directory):
        folders.sort()
        found += [os.path.join(root, name) for name in sorted(names)
                  if name.lower().endswith(RAW_SUFFIX)]
    return found


def _runs(paths: Iterable[str]) -> tuple[list[str], list[str]]:
    """The file to read for each run under `paths`, and the paths that are not there.

    Grouped by directory and stem, the summed file chosen where one is on disk, so a raw
    file named on its own still gives its run's summed row."""
    chosen: dict[tuple[str, str], str] = {}
    missing = []
    for given in paths:
        path = os.path.abspath(os.fspath(given))
        if os.path.isdir(path):
            files = _uimf_under(path)
        elif os.path.isfile(path):
            files = [path]
        else:
            missing.append(path)
            continue
        for file in files:
            stem = _stem_of(file)
            key = (os.path.normcase(os.path.dirname(file)), stem)
            summed = os.path.join(os.path.dirname(file), stem + SUMMED_SUFFIX)
            chosen[key] = summed if os.path.isfile(summed) else file
    return sorted(chosen.values(), key=lambda p: (os.path.dirname(p), _stem_of(p))), missing


# --- the run records ----------------------------------------------------------------


class _Records:
    """The run records of each directory met, read once each."""

    def __init__(self) -> None:
        self._seen: dict[str, list[tuple[str, dict]]] = {}

    def of(self, directory: str) -> list[tuple[str, dict]]:
        key = os.path.normcase(directory)
        if key not in self._seen:
            found = []
            pattern = os.path.join(glob.escape(directory), "*" + RECORD_SUFFIX)
            for path in sorted(glob.glob(pattern)):
                try:
                    with open(path, encoding="utf-8") as handle:
                        data = json.load(handle)
                except (OSError, ValueError):
                    continue
                if isinstance(data, dict):
                    found.append((os.path.basename(path), data))
            self._seen[key] = found
        return self._seen[key]

    def find(self, directory: str, stem: str, request_id: str | None
             ) -> tuple[str, dict] | None:
        records = self.of(directory)
        if request_id:
            for entry in records:
                if (entry[1].get("request") or {}).get("id") == request_id:
                    return entry
        for entry in records:
            if holds(entry[1], stem):
                return entry
        return None


def _verdict(data: Mapping[str, Any], stem: str) -> str | None:
    """The newest verdict's word on `stem` (`clockwork.record.newest_verdict`)."""
    newest = newest_verdict(data, stem)
    return None if newest is None else str(newest["verdict"])


# --- one row ------------------------------------------------------------------------


def _snake(camel: str) -> str:
    """`DurationMs` as `duration_ms`: the fallback where the template text is not there."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()


_TEMPLATES: dict[str, Any] = {}


def _names(extra: Mapping[str, str]) -> dict[str, str]:
    """The stamped spelling of every knob, label and mark, to the template's own name.

    `camel_case` does not invert (`a_1` and `a1` stamp alike, and a template may not hold
    both), so the names are read off the template text the file carries, once per
    template hash; a file without it gets the spelling undone by rule."""
    text = extra.get("ClockworkTemplateText")
    if not text:
        return {}
    key = extra.get("ClockworkTemplateHash") or text
    if key not in _TEMPLATES:
        try:
            loaded = template_module.loads_template(text)
        except Exception:  # noqa: BLE001 -- the rule below names the columns instead
            loaded = None
        _TEMPLATES[key] = loaded
    loaded = _TEMPLATES[key]
    if loaded is None:
        return {}
    camel = template_module.camel_case
    names = {}
    for knob in loaded.knobs:
        names[KNOB_PREFIX + camel(knob.name)] = knob.name
    for label in loaded.labels:
        names[LABEL_PREFIX + camel(label.name)] = label.name
    for mark in loaded.marks:
        names[MARK_PREFIX + camel(mark.name)] = mark.name
    return names


def _number(text: str | None, integer: bool = False) -> int | float | None:
    if text is None or text == "":
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if integer and value.is_integer() else value


def _per_template(extra: Mapping[str, str]) -> list[tuple[str, str, object]]:
    """`(kind, name, value)` for each knob, mark and label the file stamps, in that order.

    A mark is two entries, `<mark>_ms` and `<mark>_scan`; the `sample` label is left to
    its fixed column."""
    names = _names(extra)
    knobs, marks, labels = [], [], []
    for key, text in extra.items():
        if key.startswith(KNOB_PREFIX):
            name = names.get(key) or _snake(key[len(KNOB_PREFIX):])
            knobs.append(("knob", name, _number(text)))
        elif key.startswith(MARK_PREFIX):
            for suffix, integer in (("Scan", True), ("Ms", False)):
                if key.endswith(suffix):
                    base = key[: -len(suffix)]
                    name = names.get(base) or _snake(base[len(MARK_PREFIX):])
                    marks.append(("mark", f"{name}_{suffix.lower()}",
                                  _number(text, integer)))
                    break
        elif key.startswith(LABEL_PREFIX):
            name = names.get(key) or _snake(key[len(LABEL_PREFIX):])
            if name != "sample":
                labels.append(("label", f"label_{name}", text))
    order = {name: index for index, name in enumerate(names.values())}
    marks.sort(key=lambda entry: (order.get(entry[1].rsplit("_", 1)[0], len(order)),
                                  entry[1].endswith("_scan")))
    knobs.sort(key=lambda entry: order.get(entry[1], len(order)))
    return knobs + marks + labels


def _day(stem: str, date_started: str) -> str | None:
    parsed = parse_stem(stem)
    if parsed is not None:
        try:
            return _dt.datetime.strptime(parsed[0], "%y%m%d").date().isoformat()
        except ValueError:
            pass
    text = (date_started or "").strip()
    return text.split("T")[0].split(" ")[0] or None


def _blank(path: str) -> dict[str, Any]:
    stem = _stem_of(path)
    parsed = parse_stem(stem)
    return {**dict.fromkeys(FIXED), "file": path, "stem": stem,
            "day": _day(stem, ""), "initials": parsed[1] if parsed else None}


def _row(path: str, records: _Records) -> tuple[dict[str, Any], list[tuple[str, str, object]]]:
    row = _blank(path)
    try:
        uimf = UimfFile(path)
        globals_ = uimf.global_params()
        numbers = list(uimf.frame_numbers())
        period_ns = (float(uimf.frame_params(numbers[0]).average_tof_length_ns)
                     if numbers else None)
    except Exception as exc:  # noqa: BLE001 -- the row says why, never raises
        row["problem"] = f"{type(exc).__name__}: {exc}"
        return row, []
    extra = globals_.extra

    def text(key: str) -> str | None:
        value = extra.get(key)
        return value if value not in (None, "") else None

    stem = row["stem"]
    row.update(
        kind=file_kind(path, globals_),
        day=_day(stem, globals_.date_started),
        template_hash=text("ClockworkTemplateHash"),
        method_hash=text("ClockworkMethodHash"),
        method=text("AcquisitionMethod"),
        sample=text(LABEL_PREFIX + "Sample"),
        conditions="; ".join(line.strip() for line in (text("ClockworkConditions") or "")
                             .splitlines() if line.strip()) or None,
        outcome=globals_.run_outcome,
        reason=globals_.run_reason or None,
        repetitions_planned=globals_.repetitions_planned,
        repetitions_acquired=globals_.repetitions_acquired,
        series_id=text("ClockworkSeriesId"),
        series_index=_number(text("ClockworkSeriesIndex"), True),
        series_position=_number(text("ClockworkSeriesPosition"), True),
        series_seed=_number(text("ClockworkSeriesSeed"), True),
    )
    if period_ns is not None and period_ns > 0:
        tick = _number(text("ClockworkTickUs"))
        expected = _number(text("ClockworkPusherPeriodUs"))
        checked = check_pusher_period(
            period_ns / 1000.0, tick_us=tick if tick and tick > 0 else None,
            instrument_period_us=expected if expected and expected > 0 else None)
        # To a tenth of a nanosecond, which is finer than the console reports it.
        row.update(measured_us=round(checked.measured_us, 4),
                   declared_us=checked.declared_us,
                   declared_by=_DECLARED_BY.get(checked.declared_by,
                                                checked.declared_by or None),
                   ratio=None if checked.ratio is None else round(checked.ratio, 6))
    found = records.find(os.path.dirname(path), stem, row["series_id"])
    if found is not None:
        name, data = found
        notes = data.get("notes")
        row.update(record=name, verdict=_verdict(data, stem),
                   notes=len(notes) if isinstance(notes, list) else None)
    return row, _per_template(extra)


# --- the table ----------------------------------------------------------------------


def manifest(paths: Sequence[str | os.PathLike[str]]) -> dict[str, Any]:
    """One row per run under `paths`: `columns`, and `rows` as dicts keyed by them.

    Also `files`, how many rows, and `problems`, how many of them carry one."""
    records = _Records()
    runs, missing = _runs(os.fspath(path) for path in paths)
    rows: list[dict[str, Any]] = []
    extras: list[list[tuple[str, str, object]]] = []
    for path in runs:
        row, per_template = _row(path, records)
        rows.append(row)
        extras.append(per_template)
    for path in missing:
        row = _blank(path)
        row["problem"] = "no such file or directory"
        rows.append(row)
        extras.append([])

    columns = list(FIXED)
    named: dict[tuple[str, str], str] = {}
    for per_template in extras:
        for kind, name, _value in per_template:
            if (kind, name) in named:
                continue
            column = name if name not in columns else f"{kind}_{name}"
            while column in columns:
                column = f"{kind}_{column}"
            named[(kind, name)] = column
            columns.append(column)
    for row, per_template in zip(rows, extras, strict=True):
        row.update(dict.fromkeys(columns[len(FIXED):]))
        for kind, name, value in per_template:
            row[named[(kind, name)]] = value
    return {"columns": columns, "rows": rows, "files": len(rows),
            "problems": sum(1 for row in rows if row["problem"])}


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def to_csv(columns: Sequence[str], rows: Iterable[Mapping[str, object]]) -> str:
    """The rows as CSV text, a header line first, LF line ends, `None` as an empty cell."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row.get(column)) for column in columns])
    return buffer.getvalue()


def write_csv(path: str | os.PathLike[str], columns: Sequence[str],
              rows: Iterable[Mapping[str, object]]) -> str:
    """`to_csv` into `path`, UTF-8, through a temporary file and a rename; the path back."""
    text = os.path.abspath(os.fspath(path))
    os.makedirs(os.path.dirname(text) or ".", exist_ok=True)
    temporary = text + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="") as handle:
        handle.write(to_csv(columns, rows))
    os.replace(temporary, text)
    return text
