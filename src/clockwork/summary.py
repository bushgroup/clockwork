"""A clockwork UIMF file read back as numbers: what it holds, in windows, against arrival time.

Four functions, each returning a small JSON-serialisable dict, so that a caller that reasons
on numbers -- an agent's tool, a bench script, a test -- never has to hold a frame or plot one.
mainspring stays the only viewer; this is its reader and nothing else of it (lab record,
task 72).

    summarize(path)              which file of the pair, frames, scans, accumulations, total
                                 counts, the TIC per frame and against scan, the base peak,
                                 the calibration, the pusher period, saturation, and every
                                 `Clockwork*` parameter typed
    windowed(path, windows)      summed intensity in named m/z windows, optionally over a
                                 scan range, and each window's ratio to a reference window
    atd(path, mz)                intensity against scan in one m/z window, decimated, with
                                 the peak's position in scans and in milliseconds
    ion_events(path)             ion arrivals push by push in a file of single pushes: how
                                 many per push, their heights, widths and areas (lab
                                 record, task 76)

**Only `mainspring.uimf`'s ordinary reader is used**: `UimfFile.frame_numbers`,
`frame_params`, `global_params` and `read_frame`, the `SparseFrame` and `Calibration` they
hand back, and `summed_companion` for the pair. Nothing here opens the database itself.

**Which file of the pair.** A clockwork run writes `<stem>.uimf`, one frame per ion mobility
experiment, and `<stem>.summed.uimf`, one frame per method frame (`clockwork.acq.uimf`).
Every result says which of the two it read (`"file"`: `"raw"`, `"summed"`, or `"foreign"`
for a UIMF file clockwork did not write, such as FALKOR's) and names the other one if it is on
disk. The two give the same windowed intensities and the same arrival-time distribution for
the same run, because the summed file *is* the raw file's frames added up; what differs is
what can be said about saturation, and how long a read takes.

**A `single_frame` raw file is folded on read.** There a method frame is one console frame of
`Scans` x `Accumulations` pushes, and the repetitions are consecutive blocks of the method's
`Scans` within it (lab record, task 02). A raw frame that carries a `MainspringRepetitions`
count and no `MainspringRepetition` index is that shape, and every scan number here is then
taken modulo `Scans / Repetitions`, which is the fold `Recording.fold` applies, so a scan
range, a TIC profile and an arrival-time distribution mean the same thing on either file.

**Scans count from 0**, as `ScanNum` does, and a scan range is half-open, `[start, stop)`, as
`UimfFile.read_frame`'s is. A scan's arrival time is `scan * AverageTOFLength`, the frame's own
pusher period, which is what the viewer's axis is too.

**m/z needs a calibration the file vouches for.** A window is placed with each frame's own
`CalibrationSlope` and `CalibrationIntercept` through `mainspring.uimf.Calibration`, a bin
belonging to a window when its m/z lies within the window's closed interval. A frame that says
`CalibrationDone` 0, or whose pair produces no axis, is refused with `SummaryError` rather than
measured on a plausible one. `summarize` needs no calibration and reports whether there is one.
"""

from __future__ import annotations

import numbers
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from mainspring.uimf import (
    SUMMED_SUFFIX,
    Calibration,
    FrameParams,
    GlobalParams,
    SparseFrame,
    UimfFile,
    summed_companion,
)

from .acq.uimf import (
    KNOB_PREFIX,
    LABEL_PREFIX,
    MARK_PREFIX,
    PROVENANCE_KEYS,
    RAW_SUFFIX,
    RENDER_KEYS,
    SERIES_KEYS,
)

__all__ = [
    "DEFAULT_POINTS",
    "HEIGHT_PERCENTILES",
    "PRESETS",
    "STORED_CEILING",
    "TEXT_LIMIT",
    "Preset",
    "SummaryError",
    "atd",
    "file_kind",
    "ion_events",
    "summarize",
    "windowed",
]

STORED_CEILING = 65535
"""The largest value one stored sample can hold: the card's top code, as the file holds it.

The console stores each gated sample as its signed 16-bit code plus 32768
(`docs/console-protocol.md`, "What the console does with the digitizer"), so the top of the
card's input window, +32767, is 65535 in the file whatever the card's own bit depth. A stored
point at this value is a sample the ADC clipped: the ion, or the pile-up of ions, that made it
was larger than the window, by an amount nothing in the file records.
"""

DEFAULT_POINTS = 200
"""How many points a profile is decimated to unless the caller asks for another number.

Enough to see a CLOCK experiment's release structure in a 5000-scan frame (25 scans a point)
and small enough that a whole summary fits comfortably in one tool result.
"""

TEXT_LIMIT = 512
"""How long a `Clockwork*` text parameter may be before a summary abbreviates it.

The method text, the box state and a template's text run to several kilobytes each; they are
reported as their length and line count unless the caller asks for `texts=True`.
"""


class SummaryError(ValueError):
    """A file, or a request against it, that cannot be answered honestly.

    Always a sentence saying what was asked and what the file lacks, so that a tool result can
    carry the message as it stands.
    """


# --- presets --------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    """A named set of m/z windows and the one the others are taken as ratios to.

    Each window is one or more closed m/z intervals, summed together; a window whose
    intervals overlapped would count the overlap twice, and none here do.
    """

    description: str
    windows: Mapping[str, tuple[tuple[float, float], ...]]
    reference: str


PRESETS: Mapping[str, Preset] = {
    "bradykinin-clock": Preset(
        description=(
            "Bradykinin in a CLOCK experiment: the [M+2H]2+ isotope cluster, its water loss "
            "9.0 m/z below on the 2+ ion, and the three singly charged fragments the golden "
            "CLOCK spectrum shows, summed. The windows of the lab record's task 09 golden "
            "comparison, where a ratio is meaningful against a same-day reference rather "
            "than on its own (lab record, task 64)."
        ),
        windows={
            "precursor": ((530.3, 532.4),),
            "water_loss": ((521.3, 523.4),),
            "fragments": ((806.9, 809.1), (885.9, 888.1), (903.9, 906.1)),
        },
        reference="precursor",
    ),
}
"""The window sets a request may name instead of stating its windows.

A result that used one quotes every interval back, so a number always says what it measured
(Matt, 2026-09-23).
"""


# --- the file ---------------------------------------------------------------------------


def file_kind(path: str | os.PathLike[str], globals_: GlobalParams | None = None) -> str:
    """`"raw"`, `"summed"` or `"foreign"`: which file of a clockwork pair `path` is, if any.

    Clockwork's files are the ones carrying its `ClockworkVersion` stamp; of those, the
    companion is the one named `<stem>.summed.uimf`. Anything else -- FALKOR's files, PNNL's,
    a file written by hand with mainspring's writer -- is foreign, and is summarised with no
    saturation ceiling, since nothing in it says what the card's top code was.
    """
    if globals_ is None:
        globals_ = UimfFile(path).global_params()
    if "ClockworkVersion" not in globals_.extra:
        return "foreign"
    return "summed" if os.fspath(path).lower().endswith(SUMMED_SUFFIX) else "raw"


def _companion(path: str, kind: str) -> str | None:
    """The other file of the pair, if it is on disk now."""
    if kind == "raw":
        return summed_companion(path)
    if kind == "summed":
        raw = path[: -len(SUMMED_SUFFIX)] + RAW_SUFFIX
        return raw if os.path.isfile(raw) else None
    return None


@dataclass
class _Opened:
    path: str
    uimf: UimfFile
    kind: str
    globals_: GlobalParams
    numbers: list[int]
    companion: str | None

    def header(self) -> dict:
        return {"path": self.path, "file": self.kind, "companion": self.companion}


def _open(path: str | os.PathLike[str]) -> _Opened:
    text = os.path.abspath(os.fspath(path))
    if not os.path.isfile(text):
        raise SummaryError(f"{text}: no such file")
    uimf = UimfFile(text)
    globals_ = uimf.global_params()
    kind = file_kind(text, globals_)
    return _Opened(text, uimf, kind, globals_, list(uimf.frame_numbers()),
                   _companion(text, kind))


def _select(opened: _Opened, frames: Iterable[int] | None) -> list[int]:
    """The frame numbers asked for, every one of them in the file, in file order."""
    if frames is None:
        if not opened.numbers:
            raise SummaryError(f"{opened.path}: the file holds no frames")
        return list(opened.numbers)
    wanted = sorted({int(number) for number in frames})
    missing = sorted(set(wanted) - set(opened.numbers))
    if not wanted or missing:
        raise SummaryError(
            f"{opened.path}: frames {missing or wanted} are not in the file, which holds "
            f"frames {_span(opened.numbers)}"
        )
    return wanted


def _span(numbers: Sequence[int]) -> str:
    if not numbers:
        return "none"
    return f"{numbers[0]}..{numbers[-1]}" if len(numbers) > 1 else f"{numbers[0]}"


def _fold_period(kind: str, params: FrameParams) -> int | None:
    """The method's `Scans`, where a raw frame holds several repetitions end to end."""
    repetitions = params.repetitions or 1
    if (kind == "raw" and params.repetition is None and repetitions > 1
            and params.scans % repetitions == 0):
        return params.scans // repetitions
    return None


@dataclass
class _Frame:
    number: int
    params: FrameParams
    frame: SparseFrame
    scan: np.ndarray
    """Every stored point's scan, folded where the frame is a `single_frame` repetition block."""
    length: int
    """How many scans that folded axis has."""
    period: int | None


def _frames(opened: _Opened, numbers: Sequence[int]) -> Iterator[_Frame]:
    for number in numbers:
        params = opened.uimf.frame_params(number)
        frame = opened.uimf.read_frame(number)
        period = _fold_period(opened.kind, params)
        scan = frame.scan_of().astype(np.int64)
        if period is not None:
            scan %= period
        yield _Frame(number, params, frame, scan, period or frame.scans, period)


def _calibration(opened: _Opened, item: _Frame) -> Calibration:
    """A frame's calibration, refused unless the file vouches for it and it is an axis."""
    calibration = item.params.calibration(opened.globals_.bin_width_ns)
    if not item.params.calibration_done:
        raise SummaryError(
            f"{opened.path}: frame {item.number} says CalibrationDone 0, so it has a bin "
            "axis and no m/z axis; an m/z window cannot be placed on it"
        )
    if not calibration.usable:
        raise SummaryError(
            f"{opened.path}: frame {item.number}'s calibration (slope "
            f"{calibration.slope}, intercept {calibration.intercept}) produces no m/z axis"
        )
    return calibration


# --- shaping numbers for a result -------------------------------------------------------


def _py(value: object) -> object:
    """A numpy scalar as the plain Python number JSON can carry."""
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    return value


def _exact(values: np.ndarray, integral: bool) -> np.ndarray:
    """`bincount`'s float64 sums back as int64 where the intensities were integers.

    Exact rather than rounded: every sum here is of integers far below 2**53.
    """
    return np.rint(values).astype(np.int64) if integral else values


def _decimate(values: np.ndarray, points: int) -> tuple[int, list]:
    """`values` summed in consecutive blocks so that at most `points` remain.

    Returns the block length and the sums. The last block is short when the length does not
    divide, and is still a sum, not a mean -- a caller comparing it to the others divides.
    """
    points = int(points)
    if points < 1:
        raise SummaryError(f"points must be at least 1, not {points}")
    if values.size == 0:
        return 1, []
    step = -(-values.size // points)
    sums = np.add.reduceat(values, np.arange(0, values.size, step))
    return int(step), [_py(value) for value in sums]


def _integral(frame: SparseFrame) -> bool:
    return frame.intensity.dtype.kind in "iu"


# --- windows ------------------------------------------------------------------------------


def _is_number(value: object) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _ranges(spec: object, what: str) -> tuple[tuple[float, float], ...]:
    """A window as closed m/z intervals, from `(lo, hi)` or a sequence of such pairs.

    Lists are accepted wherever a tuple is, since that is what a JSON request carries.
    """
    if isinstance(spec, str | bytes) or not isinstance(spec, Sequence):
        raise SummaryError(f"{what}: expected [lo, hi] or a list of them, not {spec!r}")
    pairs = [spec] if len(spec) == 2 and all(_is_number(x) for x in spec) else list(spec)
    out = []
    for pair in pairs:
        if (isinstance(pair, str | bytes) or not isinstance(pair, Sequence) or len(pair) != 2
                or not all(_is_number(x) for x in pair)):
            raise SummaryError(f"{what}: expected [lo, hi] m/z pairs, not {pair!r}")
        lo, hi = float(pair[0]), float(pair[1])
        if not (np.isfinite(lo) and np.isfinite(hi) and 0.0 <= lo < hi):
            raise SummaryError(f"{what}: [{lo}, {hi}] is not an m/z interval")
        out.append((lo, hi))
    if not out:
        raise SummaryError(f"{what}: no m/z interval given")
    return tuple(out)


def _preset(name: str) -> Preset:
    try:
        return PRESETS[name]
    except KeyError:
        raise SummaryError(
            f"no preset {name!r}; the presets are {', '.join(sorted(PRESETS))}"
        ) from None


def _in_ranges(mz: np.ndarray, ranges: Sequence[tuple[float, float]]) -> np.ndarray:
    """Per-interval masks over `mz`, one row each, so a window's intervals sum separately.

    Separately rather than as one union, so that the arithmetic is the lab record's own
    (task 09): each interval's sum, then the sum of those.
    """
    return np.stack([(mz >= lo) & (mz <= hi) for lo, hi in ranges])


def _scan_mask(item: _Frame, scans: tuple[int, int] | None) -> np.ndarray | None:
    if scans is None:
        return None
    return (item.scan >= scans[0]) & (item.scan < scans[1])


def _scan_range(scans: object) -> tuple[int, int] | None:
    if scans is None:
        return None
    if (isinstance(scans, str | bytes) or not isinstance(scans, Sequence) or len(scans) != 2
            or not all(_is_number(x) and float(x).is_integer() for x in scans)):
        raise SummaryError(f"scans: expected [start, stop), two scan numbers, not {scans!r}")
    start, stop = int(scans[0]), int(scans[1])
    if not 0 <= start < stop:
        raise SummaryError(f"scans: [{start}, {stop}) is not a range of scans from 0")
    return start, stop


# --- provenance -----------------------------------------------------------------------------


_FIXED_TYPES = ({definition.name: definition.data_type for definition, _ in PROVENANCE_KEYS}
                | {definition.name: definition.data_type
                   for definition in RENDER_KEYS + SERIES_KEYS})


def _data_type(name: str) -> str:
    """A `Clockwork*` parameter's declared type, from its name alone.

    `GlobalParams.extra` is name to text and carries no type, so the fixed keys take theirs
    from `clockwork.acq.uimf`'s own declarations and the per-template ones from the rule
    `provenance_globals` writes them by: every knob a Double, every label a String, a mark's
    `...Ms` a Double and its `...Scan` an Int32.
    """
    if name in _FIXED_TYPES:
        return _FIXED_TYPES[name]
    if name.startswith(KNOB_PREFIX):
        return "System.Double"
    if name.startswith(MARK_PREFIX) and name.endswith("Scan"):
        return "System.Int32"
    if name.startswith(MARK_PREFIX) and name.endswith("Ms"):
        return "System.Double"
    return "System.String"


def _typed(name: str, text: str) -> object:
    data_type = _data_type(name)
    try:
        if data_type == "System.Double":
            return float(text)
        if data_type == "System.Int32":
            number = float(text)
            return int(number) if number.is_integer() else number
    except ValueError:
        return text
    return text


def _abbreviated(value: object, texts: bool) -> object:
    if texts or not isinstance(value, str) or len(value) <= TEXT_LIMIT:
        return value
    return {"chars": len(value), "lines": value.count("\n") + 1,
            "omitted": "longer than TEXT_LIMIT; ask with texts=True for the whole text"}


def _provenance(globals_: GlobalParams, texts: bool) -> tuple[dict, dict | None]:
    """Every `Clockwork*` global typed, and a rendered run's knobs, labels and marks grouped.

    The groups are keyed by the name as the file spells it (`BiasHoldV`), since the file
    keeps no other spelling.
    """
    flat = {name: _typed(name, text) for name, text in sorted(globals_.extra.items())
            if name.startswith("Clockwork")}
    knobs = {name[len(KNOB_PREFIX):]: value for name, value in flat.items()
             if name.startswith(KNOB_PREFIX)}
    labels = {name[len(LABEL_PREFIX):]: value for name, value in flat.items()
              if name.startswith(LABEL_PREFIX)}
    marks: dict[str, dict] = {}
    for name, value in flat.items():
        if not name.startswith(MARK_PREFIX):
            continue
        stem = name[len(MARK_PREFIX):]
        for suffix, key in (("Scan", "scan"), ("Ms", "ms")):
            if stem.endswith(suffix):
                marks.setdefault(stem[: -len(suffix)], {})[key] = value
                break
    template = None
    if "ClockworkTemplateHash" in flat or knobs or labels or marks:
        template = {"hash": flat.get("ClockworkTemplateHash"),
                    "tick_us": flat.get("ClockworkTickUs"),
                    "knobs": knobs, "labels": labels, "marks": marks}
    return {name: _abbreviated(value, texts) for name, value in flat.items()}, template


# --- the three questions --------------------------------------------------------------------


def summarize(
    path: str | os.PathLike[str],
    *,
    frames: Iterable[int] | None = None,
    points: int = DEFAULT_POINTS,
    texts: bool = False,
) -> dict:
    """What one UIMF file holds, as numbers: the first thing to ask of a run.

    Every frame is read unless `frames` names some. The result:

    - `file`, `path`, `companion`: which file of the pair this is and where the other is.
    - `frames`: how many were read, the first and last number, how many are still
      provisional (a file being acquired), and how many method frames they belong to.
    - `scans_per_frame`, `accumulations`: the distinct values over the frames read.
      `fold_period` is the method's `Scans` where a `single_frame` raw file was folded.
    - `total_counts`: every stored intensity summed. `stored_points`: how many there are.
    - `frame_tic`: each frame's total, in frame order; `tic_profile`: the total against
      scan over every frame read, on the folded axis. Both summed in blocks down to at most
      `points` values, the block length given as `frames_per_point` / `scans_per_point`.
    - `base_peak`: the bin with the most intensity over every frame read, its m/z where the
      first frame's calibration is usable and vouched for, and the intensity.
    - `calibration`: the first frame's pair, whether the file says it is done, whether it
      makes an axis, and whether every frame read carries the same pair.
    - `pusher_period_ns`: the first frame's `AverageTOFLength`, and its spread over the rest.
    - `saturation`: see below.
    - `method`, `instrument`, `date_started`: PNNL's `AcquisitionMethod`, `InstrumentName`
      and `DateStarted`.
    - `clockwork`: every `Clockwork*` global parameter, typed; texts longer than
      `TEXT_LIMIT` are given as their length unless `texts` is true. `template`: a rendered
      run's template hash, tick, knobs, labels and marks, or `None` for a hand-written
      method (lab record, task 66).

    **Saturation** is the fraction of stored points at `STORED_CEILING`, the card's top code,
    and what that fraction means depends on the file (`saturation.bound`):

    - a **raw** file's points are single pushes, so it is **exact**: those samples clipped.
      `pushes_at_ceiling` is how many pushes held at least one.
    - a **summed** file's points are sums of `Accumulations` pushes, each between 0 and the
      ceiling, so a point below the ceiling holds no clipped sample and one at or above it
      may. The fraction is an **upper** bound, often a loose one; the raw file, where it
      survives, is `companion` and gives the exact figure.
    - a **foreign** file states no ceiling -- FALKOR's U1084A files are 8-bit sums with a
      pedestal nothing in the file records -- and saturation is not reported (`bound` None).

    The operating point behind the ceiling is 0.5 V full scale at -254 mV offset, inverted,
    where one median ion is about 5200 codes above the baseline and the window holds about
    12 of them (lab record, task 16, the detector chain).
    """
    opened = _open(path)
    numbers_read = _select(opened, frames)
    spectrum = np.zeros(int(opened.globals_.bins), dtype=np.float64)
    profile = np.zeros(0, dtype=np.float64)
    frame_totals: list[float] = []
    scans_seen: set[int] = set()
    accumulations_seen: set[int] = set()
    periods: list[float] = []
    method_frames: set[int] = set()
    fold_periods: set[int] = set()
    first_params: FrameParams | None = None
    same_calibration = True
    provisional = 0
    stored_points = 0
    at_ceiling = 0
    pushes = 0
    pushes_at_ceiling = 0
    integral = True
    for item in _frames(opened, numbers_read):
        params, frame = item.params, item.frame
        if first_params is None:
            first_params = params
        elif (params.calibration_slope, params.calibration_intercept,
              params.calibration_done) != (first_params.calibration_slope,
                                           first_params.calibration_intercept,
                                           first_params.calibration_done):
            same_calibration = False
        integral = integral and _integral(frame)
        weights = frame.intensity.astype(np.float64)
        spectrum += np.bincount(frame.bin_index, weights=weights, minlength=spectrum.size)
        if item.length > profile.size:
            profile = np.concatenate((profile, np.zeros(item.length - profile.size)))
        profile[: item.length] += np.bincount(item.scan, weights=weights,
                                              minlength=item.length)
        frame_totals.append(float(weights.sum()))
        scans_seen.add(int(params.scans))
        accumulations_seen.add(int(params.accumulations))
        periods.append(float(params.average_tof_length_ns))
        if params.method_frame is not None:
            method_frames.add(int(params.method_frame))
        if item.period is not None:
            fold_periods.add(item.period)
        provisional += bool(frame.provisional)
        stored_points += len(frame)
        clipped = np.flatnonzero(frame.intensity >= STORED_CEILING)
        at_ceiling += int(clipped.size)
        pushes += int(frame.scans)
        if clipped.size:
            owners = np.searchsorted(frame.scan_start, clipped, side="right") - 1
            pushes_at_ceiling += int(np.unique(owners).size)
    assert first_params is not None  # _select refuses an empty file

    calibration = first_params.calibration(opened.globals_.bin_width_ns)
    base_bin = int(np.argmax(spectrum)) if spectrum.size else 0
    base_intensity = spectrum[base_bin] if spectrum.size else 0.0
    base_mz = (float(calibration.mz(float(base_bin)))
               if first_params.calibration_done and calibration.usable else None)
    frames_per_point, frame_tic = _decimate(_exact(np.asarray(frame_totals), integral), points)
    scans_per_point, tic_profile = _decimate(_exact(profile, integral), points)
    clockwork, template = _provenance(opened.globals_, texts)
    extra = opened.globals_.extra
    return opened.header() | {
        "written_by": opened.globals_.written_by or None,
        "method": extra.get("AcquisitionMethod") or None,
        "instrument": opened.globals_.instrument_name or None,
        "date_started": opened.globals_.date_started or None,
        "frames": {"count": len(numbers_read), "first": numbers_read[0],
                   "last": numbers_read[-1], "provisional": provisional,
                   "method_frames": len(method_frames) or None},
        "scans_per_frame": sorted(scans_seen),
        "accumulations": sorted(accumulations_seen),
        "fold_period": (sorted(fold_periods)[0] if len(fold_periods) == 1
                        else sorted(fold_periods) or None),
        "bins": int(opened.globals_.bins),
        "bin_width_ns": float(opened.globals_.bin_width_ns),
        "pusher_period_ns": periods[0],
        "pusher_period_spread_ns": max(periods) - min(periods),
        "total_counts": _py(_exact(np.asarray([spectrum.sum()]), integral)[0]),
        "stored_points": stored_points,
        "frame_tic": {"frames_per_point": frames_per_point, "values": frame_tic},
        "tic_profile": {"scans_per_point": scans_per_point, "values": tic_profile},
        "base_peak": {"bin": base_bin, "mz": base_mz,
                      "intensity": _py(_exact(np.asarray([base_intensity]), integral)[0])},
        "calibration": {"done": bool(first_params.calibration_done),
                        "usable": bool(calibration.usable),
                        "slope": float(calibration.slope),
                        "intercept_us": float(calibration.intercept),
                        "same_on_every_frame": same_calibration},
        "saturation": _saturation(opened.kind, stored_points, at_ceiling, pushes,
                                  pushes_at_ceiling),
        "clockwork": clockwork,
        "template": template,
    }


def _saturation(kind: str, points: int, at_ceiling: int, pushes: int,
                pushes_at_ceiling: int) -> dict:
    if kind == "foreign":
        return {"bound": None, "ceiling": None,
                "definition": "not defined: the file does not say what its card's top code was"}
    fraction = at_ceiling / points if points else 0.0
    if kind == "raw":
        return {"bound": "exact", "ceiling": STORED_CEILING, "points_at_ceiling": at_ceiling,
                "fraction": fraction, "pushes": pushes, "pushes_at_ceiling": pushes_at_ceiling,
                "definition": "stored single-push samples at the card's top code, of all "
                              "stored samples"}
    return {"bound": "upper", "ceiling": STORED_CEILING, "points_at_ceiling": at_ceiling,
            "fraction": fraction,
            "definition": "summed points at or above one push's top code, of all stored "
                          "points: a point below it holds no clipped push, one above it may; "
                          "the raw file gives the exact figure"}


def windowed(
    path: str | os.PathLike[str],
    windows: str | Mapping[str, object],
    *,
    reference: str | None = None,
    scans: Sequence[int] | None = None,
    frames: Iterable[int] | None = None,
) -> dict:
    """Summed intensity in named m/z windows, and each window's ratio to a reference one.

    `windows` is a preset's name (`PRESETS`) or a mapping of name to window, a window being
    `[lo, hi]` in m/z or a list of such intervals, summed. `reference` defaults to the
    preset's own; with explicit windows and no reference, no ratios are given. `scans`, a
    half-open `[start, stop)`, restricts the sum to those scans -- a mobility range -- on the
    folded axis; `frames` to those frames. Every frame read must carry a calibration it
    vouches for (the module docstring).

    Each window's intensity is the sum, over the frames read, of every stored point whose
    bin's m/z lies inside one of its intervals, the intervals summed separately. That is task
    09's golden comparison exactly, and on a one-frame file it reproduces that script's
    figures (lab record, task 09); on a file of several frames it is their sum, so a raw
    file and its companion give the same numbers. A ratio is only meaningful against a
    reference measured the same way the same day (lab record, task 64).

    The result quotes every interval back under `windows`, with its intensity, beside
    `ratios`, `reference`, `preset`, `frames`, `scans`, `fold_period` and `total`, the whole
    intensity in the scan range whatever its m/z.
    """
    opened = _open(path)
    preset_name = windows if isinstance(windows, str) else None
    if preset_name is not None:
        chosen = _preset(preset_name)
        stated = dict(chosen.windows)
        reference = chosen.reference if reference is None else reference
    elif isinstance(windows, Mapping) and windows:
        stated = {str(name): _ranges(spec, f"window {name!r}") for name, spec in windows.items()}
    else:
        raise SummaryError("windows: expected a preset's name or a mapping of name to window")
    if reference is not None and reference not in stated:
        raise SummaryError(
            f"reference {reference!r} is not one of the windows ({', '.join(stated)})"
        )
    scan_range = _scan_range(scans)
    numbers_read = _select(opened, frames)
    sums = {name: [0.0] * len(ranges) for name, ranges in stated.items()}
    total = 0.0
    integral = True
    fold_periods: set[int] = set()
    for item in _frames(opened, numbers_read):
        calibration = _calibration(opened, item)
        integral = integral and _integral(item.frame)
        intensity = item.frame.intensity.astype(np.float64)
        bins = item.frame.bin_index.astype(np.float64)
        keep = _scan_mask(item, scan_range)
        if keep is not None:
            intensity, bins = intensity[keep], bins[keep]
        if item.period is not None:
            fold_periods.add(item.period)
        mz = np.asarray(calibration.mz(bins))
        total += float(intensity.sum())
        for name, ranges in stated.items():
            for index, mask in enumerate(_in_ranges(mz, ranges)):
                sums[name][index] += float(intensity[mask].sum())

    def number(value: float) -> object:
        return int(round(value)) if integral else value

    results = {name: {"mz": [list(pair) for pair in stated[name]],
                      "intensity": number(sum(parts)),
                      "per_interval": [number(part) for part in parts]}
               for name, parts in sums.items()}
    ratios = None
    if reference is not None:
        denominator = sum(sums[reference])
        ratios = {name: (sum(parts) / denominator if denominator > 0 else None)
                  for name, parts in sums.items()}
    return opened.header() | {
        "preset": preset_name,
        "frames": {"count": len(numbers_read), "first": numbers_read[0],
                   "last": numbers_read[-1]},
        "scans": list(scan_range) if scan_range else None,
        "fold_period": sorted(fold_periods)[0] if len(fold_periods) == 1 else None,
        "windows": results,
        "reference": reference,
        "ratios": ratios,
        "total": number(total),
    }


def atd(
    path: str | os.PathLike[str],
    mz: object,
    *,
    preset: str | None = None,
    frames: Iterable[int] | None = None,
    points: int = DEFAULT_POINTS,
) -> dict:
    """Intensity against scan in one m/z window: an arrival-time distribution, with its peak.

    `mz` is a window, `[lo, hi]` or a list of intervals, or -- with `preset` -- the name of
    one of that preset's windows. Every frame read is summed onto one scan axis, the folded
    one for a `single_frame` raw file, and every frame must carry a calibration it vouches
    for (the module docstring).

    Positions are taken on the undecimated profile, in scans and, through the first frame's
    `AverageTOFLength`, in milliseconds:

    - `peak.argmax_scan`: the scan with the most intensity, the first of any tie.
    - `peak.centroid_scan`: the intensity-weighted mean scan over the contiguous run of scans
      around the argmax that stay at or above half its height, `peak.half_max_scans` being
      that run's first and last scan. A plain centroid, no fitting; on a sparse profile (a
      raw file's single pushes, a weak window) the run can be one scan wide.
    - `mean_scan`: the weighted mean over the whole profile, which on a profile of several
      peaks is between them and is not a peak position.

    `profile` is the distribution summed in blocks down to at most `points` values, block
    length `scans_per_point`; `peak` is `None` for a window with nothing in it.
    """
    opened = _open(path)
    if preset is not None:
        chosen = _preset(preset)
        if not isinstance(mz, str) or mz not in chosen.windows:
            raise SummaryError(
                f"preset {preset!r} has windows {', '.join(chosen.windows)}, not {mz!r}"
            )
        ranges = chosen.windows[mz]
        window_name = mz
    else:
        ranges = _ranges(mz, "mz")
        window_name = None
    numbers_read = _select(opened, frames)
    profile = np.zeros(0, dtype=np.float64)
    integral = True
    period_ns: float | None = None
    fold_periods: set[int] = set()
    for item in _frames(opened, numbers_read):
        calibration = _calibration(opened, item)
        integral = integral and _integral(item.frame)
        if period_ns is None:
            period_ns = float(item.params.average_tof_length_ns)
        if item.period is not None:
            fold_periods.add(item.period)
        inside = _in_ranges(np.asarray(calibration.mz(item.frame.bin_index.astype(np.float64))),
                            ranges).any(axis=0)
        if item.length > profile.size:
            profile = np.concatenate((profile, np.zeros(item.length - profile.size)))
        profile[: item.length] += np.bincount(
            item.scan[inside], weights=item.frame.intensity[inside].astype(np.float64),
            minlength=item.length)
    assert period_ns is not None  # _select refuses an empty file

    def ms(scan: float) -> float:
        return float(scan) * period_ns * 1e-6

    total = float(profile.sum())
    peak = None
    mean_scan = None
    if total > 0:
        top = int(np.argmax(profile))
        half = profile[top] / 2.0
        below = np.flatnonzero(profile[:top] < half)
        above = np.flatnonzero(profile[top + 1:] < half)
        first = int(below[-1]) + 1 if below.size else 0
        last = top + int(above[0]) if above.size else profile.size - 1
        run = profile[first:last + 1]
        centroid = float((np.arange(first, last + 1) * run).sum() / run.sum())
        peak = {"argmax_scan": top, "argmax_ms": ms(top),
                "height": _py(_exact(np.asarray([profile[top]]), integral)[0]),
                "centroid_scan": centroid, "centroid_ms": ms(centroid),
                "half_max_scans": [first, last]}
        mean_scan = float((np.arange(profile.size) * profile).sum() / total)
    scans_per_point, values = _decimate(_exact(profile, integral), points)
    return opened.header() | {
        "preset": preset,
        "window": window_name,
        "mz": [list(pair) for pair in ranges],
        "frames": {"count": len(numbers_read), "first": numbers_read[0],
                   "last": numbers_read[-1]},
        "scans": int(profile.size),
        "fold_period": sorted(fold_periods)[0] if len(fold_periods) == 1 else None,
        "pusher_period_ns": period_ns,
        "total": _py(_exact(np.asarray([total]), integral)[0]),
        "peak": peak,
        "mean_scan": mean_scan,
        "mean_ms": ms(mean_scan) if mean_scan is not None else None,
        "profile": {"scans_per_point": scans_per_point, "values": values},
    }


HEIGHT_PERCENTILES = (5, 25, 50, 75, 95, 99)
"""The points of the pulse-height distribution `ion_events` reports, besides its maximum."""


def ion_events(
    path: str | os.PathLike[str],
    *,
    frames: Iterable[int] | None = None,
) -> dict:
    """Ion arrivals push by push: how many there are, how tall and how wide.

    An **event** is a run of consecutive stored bins within one push: one ion, or ions
    arriving closer together than the pulse width, which zero suppression stores as one
    stretch of samples above its threshold. This is the per-push view the detection-response
    experiment exists to give, and it needs single pushes: every raw file clockwork writes,
    or any file whose frames say `Accumulations` 1. A summed file of more than one push per
    row is refused with `SummaryError`, since its runs are sums of several pushes' ions.

    The result:

    - `pushes`: how many pushes the frames read hold, `pushes_with_events` how many stored
      at least one event, `occupancy` their fraction.
    - `events` and `events_per_push`.
    - `height`: each event's peak stored value, at `HEIGHT_PERCENTILES` and its maximum,
      in stored units, and under `height_mv` the same in millivolts above the bottom of the
      card's window where the file stamps its full scale (`ClockworkFullScale`): one stored
      unit is the full scale over 65536, 7.63 uV at 0.5 V. A foreign file has no such stamp
      and no `height_mv`.
    - `width_bins`: each event's length in bins, its mode, median and 95th percentile.
    - `area_median`: the median of each event's summed intensity.
    - `railed_events` and `railed_fraction`: events whose peak is at `STORED_CEILING`, the
      card's top code, so their height is a lower bound. None on a foreign file.
    - `counts_per_push`: every stored intensity summed, over the pushes: the total ion
      current per push.

    Scans are not folded here: a push is a push wherever it sits in a repetition.
    """
    opened = _open(path)
    numbers_read = _select(opened, frames)
    heights: list[np.ndarray] = []
    widths: list[np.ndarray] = []
    areas: list[np.ndarray] = []
    pushes = 0
    pushes_with = 0
    total = 0.0
    for number in numbers_read:
        params = opened.uimf.frame_params(number)
        if opened.kind != "raw" and int(params.accumulations) != 1:
            raise SummaryError(
                f"{opened.path}: frame {number} sums {params.accumulations} pushes per row, "
                "so its runs of bins are several pushes' ions added together; ask the raw "
                "file, whose rows are single pushes"
            )
        frame = opened.uimf.read_frame(number)
        pushes += int(frame.scans)
        count = len(frame)
        if count == 0:
            continue
        scan = np.repeat(np.arange(frame.scans), np.diff(frame.scan_start))
        bins = frame.bin_index.astype(np.int64)
        values = frame.intensity.astype(np.int64)
        starts = np.ones(count, dtype=bool)
        starts[1:] = (bins[1:] != bins[:-1] + 1) | (scan[1:] != scan[:-1])
        first = np.flatnonzero(starts)
        widths.append(np.diff(np.append(first, count)))
        heights.append(np.maximum.reduceat(values, first))
        areas.append(np.add.reduceat(values, first))
        pushes_with += int(np.unique(scan[first]).size)
        total += float(values.sum())

    height = np.concatenate(heights) if heights else np.zeros(0, dtype=np.int64)
    width = np.concatenate(widths) if widths else np.zeros(0, dtype=np.int64)
    area = np.concatenate(areas) if areas else np.zeros(0, dtype=np.int64)
    full_scale = opened.globals_.extra.get("ClockworkFullScale")
    try:
        unit_mv = float(full_scale) * 1000.0 / 65536.0 if full_scale is not None else None
    except ValueError:
        unit_mv = None

    def spread(values: np.ndarray, scale: float = 1.0) -> dict | None:
        if values.size == 0:
            return None
        found = {f"p{point}": float(np.percentile(values, point)) * scale
                 for point in HEIGHT_PERCENTILES}
        found["max"] = float(values.max()) * scale
        return found

    railed = int((height >= STORED_CEILING).sum()) if opened.kind != "foreign" else None
    return opened.header() | {
        "frames": {"count": len(numbers_read), "first": numbers_read[0],
                   "last": numbers_read[-1]},
        "pushes": pushes,
        "pushes_with_events": pushes_with,
        "occupancy": pushes_with / pushes if pushes else None,
        "events": int(height.size),
        "events_per_push": height.size / pushes if pushes else None,
        "height": spread(height),
        "height_mv": spread(height, unit_mv) if unit_mv is not None else None,
        "full_scale_v": float(full_scale) if unit_mv is not None else None,
        "width_bins": None if width.size == 0 else {
            "mode": int(np.bincount(width).argmax()),
            "median": float(np.median(width)),
            "p95": float(np.percentile(width, 95))},
        "area_median": float(np.median(area)) if area.size else None,
        "railed_events": railed,
        "railed_fraction": (railed / height.size if railed is not None and height.size
                            else (0.0 if railed is not None else None)),
        "counts_per_push": total / pushes if pushes else None,
    }
