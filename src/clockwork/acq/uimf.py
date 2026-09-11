"""The UIMF files an acquisition produces, and the fold that makes the second one.

The console owns `Frame_Scans` and nothing else: it opens the file it is told to open
read-write, appends scan rows, and never creates a file, a schema or a parameter. The
rest is clockwork's, and this module is where clockwork does it. The writer itself is
`mainspring.uimf.UimfWriter`, so that the code which creates a UIMF file and the code
which reads one are the same code; what lives here is everything that needs to know
what an *experiment* is, which mainspring deliberately does not.

Two files come out of one acquisition (lab record, task 02).

    <stem>.uimf           the raw file: one frame per ion mobility experiment,
                          `Accumulations` = 1, written by the console
    <stem>.summed.uimf    the summed companion: one frame per method frame,
                          `Accumulations` = A, written here by the fold

The raw file keeps the plain name because it is the one that exists first, grows during
the run and can be watched live (Matt, 2026-09-10). The companion is today's file shape,
which is what trainees and PNNL's tools open, and `keep_raw = false` in the method leaves
only it behind.

**Two phases per frame.** `Recording.begin_frame` writes the frame's parameters and hands
back the `FrameRequest` to give the console; `Recording.end_frame` writes what only the
end of the frame could say -- how long it took, and the completion marker without which
nothing in the file distinguishes a frame that finished from one that was cut off. They
bracket `run_frame` and are the reason a power cut leaves an honest file rather than a
plausible one:

    with Recording.create(directory, method, geometry) as recording:
        for method_frame in range(1, method.acquisition.frames + 1):
            for repetition in range(1, method.acquisition.console_frames + 1):
                request = recording.begin_frame(method_frame, repetition)
                run_frame(console, stream, request, timeout=timeout)
                recording.end_frame()
            recording.fold(method_frame)

Nothing here talks to a box or to the console. Which strings go to which box and in what
order is `clockwork.method` and the caller's; this module is handed a method and produces
the files that method implies.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable, Iterator

import numpy as np
from mainspring.uimf import (
    FrameSpec,
    GlobalSpec,
    SparseFrame,
    UimfFile,
    UimfWriter,
    sum_frames,
)
from mainspring.uimf.writer import CLIENT_PARAM_ID_BASE, ParamDef

from ..instrument import UNCALIBRATED, Instrument
from ..method import Method, stamp
from .wire import FrameRequest, TofWidth

__all__ = [
    "PROVENANCE_KEYS",
    "RAW_SUFFIX",
    "SA220P_DETECTOR_BITS",
    "SUMMED_SUFFIX",
    "Geometry",
    "Recording",
    "fold_scans",
    "raw_path",
    "stamp_globals",
    "summed_path",
]

RAW_SUFFIX = ".uimf"
SUMMED_SUFFIX = ".summed.uimf"

PROVENANCE_KEYS: tuple[tuple[ParamDef, str], ...] = (
    (ParamDef(CLIENT_PARAM_ID_BASE + 1, "ClockworkMethodHash", "System.String",
              "SHA-256 of the canonical text of the method that produced this file"),
     "method_hash"),
    (ParamDef(CLIENT_PARAM_ID_BASE + 2, "ClockworkMethodText", "System.String",
              "The method document that produced this file, in full"),
     "method_text"),
    (ParamDef(CLIENT_PARAM_ID_BASE + 3, "ClockworkVersion", "System.String",
              "Version of clockwork that acquired this file"),
     "clockwork_version"),
    (ParamDef(CLIENT_PARAM_ID_BASE + 4, "ClockworkConsoleVersion", "System.String",
              "Version string the acquisition console reported to info"),
     "console_version"),
    (ParamDef(CLIENT_PARAM_ID_BASE + 5, "ClockworkChannelOffset", "System.Double",
              "Channel 1 input offset in volts, as clockwork set it for this acquisition"),
     "channel_offset_v"),
    (ParamDef(CLIENT_PARAM_ID_BASE + 6, "ClockworkFullScale", "System.Double",
              "Channel 1 full scale in volts in force for this acquisition, as configured; "
              "the console reads it from config.txt at startup and does not report it back"),
     "full_scale_v"),
)
"""The stamp's fields as `Global_Params` parameters, each paired with the `stamp()` key
it carries.

IDs in the client block above mainspring's own, which is where a client is entitled to
put what only it means; UIMF-Library skips an ID it does not recognise rather than
failing on it. The names are prefixed for the same reason mainspring prefixes its own:
both parameter tables are keyed by name as well as by ID, and UIMF-Library resolves an
unrecognised name by parsing it against its enum, so a bare `MethodHash` would silently
become a standard parameter the day PNNL defines one.

The method's *name* is not here. PNNL already has a name for it, `AcquisitionMethod`,
and one fact under two keys is two things to keep in step. The instrument's name is not
here either, for the same reason: it goes under PNNL's `InstrumentName`.

The last two are the vertical settings in force, which the stamp did not record until
task 25. Two files acquired through different ranges, or on either side of an
attenuator, are otherwise indistinguishable once they leave the instrument, and the
chain in front of this digitizer changed on the day it was cabled up. The offset is
clockwork's own `vertical` command and the console confirms it; the full scale is a
`config.txt` key the console reads at startup and does not report back, so what the file
carries is the value the lab configured. That distinction is in the parameter's own
description rather than in a third key, because `ParamDescription` is written into
`Global_Params` beside the value and a reader has both.
"""

SA220P_DETECTOR_BITS = 14
"""What the SA220P digitises to, for `Global_Params`.

Not in PNNL's parameter set and not derivable from anything that is, so a viewer that
wants intensity as a fraction of full scale has had to take it from a user setting
defaulting to 8 -- right for the card FALKOR drives and wrong for this one. A file that
states it is believed.
"""


def raw_path(directory: str | os.PathLike[str], stem: str) -> str:
    """The file the console appends to: `<directory>/<stem>.uimf`."""
    return os.path.join(os.fspath(directory), stem + RAW_SUFFIX)


def summed_path(raw: str | os.PathLike[str]) -> str:
    """The fold's companion beside a raw file: `<stem>.summed.uimf`."""
    text = os.fspath(raw)
    if not text.endswith(RAW_SUFFIX):
        raise ValueError(f"{text!r} is not a {RAW_SUFFIX} path")
    return text[: -len(RAW_SUFFIX)] + SUMMED_SUFFIX


# --- the axis the console's own measurement implies ----------------------------------


class Geometry:
    """The bin axis and the pusher period, as the console's measurement fixes them.

    Every number in a UIMF file's `Global_Params` that describes the time-of-flight axis
    follows from three things: the sample rate, the post-trigger delay, and the pusher
    period the console measured when the acquisition chain was built. Deriving them in
    one place rather than at each call site is what stops a file claiming an axis its
    rows do not sit on.

    `bins` is `TofWidth.num_samples`, which the console defines as the record size plus
    the post-trigger samples -- and `offset_bins` puts exactly those post-trigger samples
    into the leading zero run of every scan, so the two agree by construction and a
    stored bin index never runs past the axis.
    """

    __slots__ = ("bins", "bin_width_ns", "time_offset_ns", "average_tof_length_ns",
                 "offset_bins")

    def __init__(
        self,
        *,
        bins: int,
        bin_width_ns: float,
        time_offset_ns: float,
        average_tof_length_ns: float,
        offset_bins: int,
    ) -> None:
        self.bins = int(bins)
        self.bin_width_ns = float(bin_width_ns)
        self.time_offset_ns = float(time_offset_ns)
        self.average_tof_length_ns = float(average_tof_length_ns)
        self.offset_bins = int(offset_bins)
        if self.bins <= 0:
            raise ValueError(f"bins must be positive, not {self.bins}")
        if self.offset_bins > self.bins:
            raise ValueError(
                f"offset_bins {self.offset_bins} is past the {self.bins}-bin axis; the "
                "post-trigger delay cannot be longer than the record it precedes"
            )

    def __repr__(self) -> str:
        return (f"Geometry(bins={self.bins}, bin_width_ns={self.bin_width_ns}, "
                f"average_tof_length_ns={self.average_tof_length_ns})")

    @classmethod
    def from_tof_width(
        cls,
        width: TofWidth,
        *,
        sample_rate_hz: float,
        post_trigger_samples: int,
    ) -> Geometry:
        """Build the axis from what `acquire` or `tof width` replied.

        `post_trigger_samples` is the console's `PostTriggerDelay` in samples, which the
        client has to know anyway because it is what `FrameRequest.offset_bins` must be
        set to: the console adds it to each scan's leading zero run, and a file whose
        `TimeOffset` says something else describes an axis its own rows do not use.
        """
        if sample_rate_hz <= 0:
            raise ValueError(f"sample_rate_hz must be positive, not {sample_rate_hz}")
        bin_width_ns = 1e9 / sample_rate_hz
        return cls(
            bins=int(width.num_samples),
            bin_width_ns=bin_width_ns,
            time_offset_ns=post_trigger_samples * bin_width_ns,
            average_tof_length_ns=width.pusher_pulse_width * bin_width_ns,
            offset_bins=int(post_trigger_samples),
        )


# --- the fold ------------------------------------------------------------------------


def fold_scans(frame: SparseFrame, period: int) -> SparseFrame:
    """Sum a frame's scans modulo `period`, into a frame `period` scans long.

    What `repetition_mode = "single_frame"` needs: there the whole method frame is one
    console frame of `scans * accumulations` rows, and the repetitions are consecutive
    blocks of `scans` within it, so the fold happens inside one frame rather than across
    several. `ScanNum` modulo `Scans` is the rule (lab record, task 02).

    A dropped trigger shifts every later push into the wrong scan for the rest of the
    frame and nothing here can see that, because the console stores no trigger timestamp
    in the file. That is a known cost of the single-frame mode and the reason
    `per_repetition` is the default: there a dropped trigger spoils one repetition and
    the next re-synchronises on its own start edge.
    """
    period = int(period)
    if period <= 0:
        raise ValueError(f"period must be positive, not {period}")
    if frame.scans % period:
        raise ValueError(
            f"a {frame.scans}-scan frame does not divide into repetitions of {period}"
        )
    if frame.scans == period:
        return frame
    blocks = frame.scans // period
    pieces = [
        SparseFrame.from_scans(
            frame=frame.frame,
            scans=period,
            bins=frame.bins,
            points={scan: frame.scan(block * period + scan) for scan in range(period)},
            provisional=frame.provisional,
        )
        for block in range(blocks)
    ]
    total = sum_frames(pieces)
    assert total is not None  # no cancel callback was passed
    return total


def _scan_rows(frame: SparseFrame) -> Iterator[tuple[int, np.ndarray, np.ndarray]]:
    """A frame's non-empty scans, in the shape `UimfWriter.write_scans` takes."""
    for scan in range(frame.scans):
        bin_index, intensity = frame.scan(scan)
        if bin_index.size:
            yield scan, bin_index, intensity


# --- one acquisition's files ----------------------------------------------------------


class Recording:
    """The raw file and its summed companion, through one acquisition.

    Created before the first `acquire frame`, because the console opens a file and never
    makes one. Closed at the end, which is when `keep_raw = false` takes effect: the raw
    file is removed only once every fold that needed it has been written, so a run that
    fails half way through leaves the per-repetition data rather than nothing.

    Not thread safe, and not meant to be: it is a file being written, and clockwork's
    acquisition runs on one worker thread.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "a Recording is made by Recording.create(directory, method, geometry), which "
            "creates the file as well as the object"
        )

    def _init(
        self,
        method: Method,
        geometry: Geometry,
        writer: UimfWriter,
        globals_: GlobalSpec,
        instrument: Instrument,
        clock: Callable[[], float],
        started: float,
        overwrite: bool,
    ) -> None:
        self.method = method
        self.geometry = geometry
        self.acquisition = method.acquisition
        self.keep_raw = method.acquisition.keep_raw
        self.instrument = instrument
        self.calibration = (instrument.calibration.slope, instrument.calibration.intercept)
        self._clock = clock
        self._started = started
        self._overwrite = overwrite
        self._raw = writer
        self._raw_globals = globals_
        self._summed: UimfWriter | None = None
        self._folded: set[int] = set()
        self._open_frame: tuple[int, int, int, float] | None = None
        self._frames: dict[int, list[int]] = {}
        self._elapsed: dict[int, float] = {}
        self._began: dict[int, float] = {}
        self._closed = False

    @classmethod
    def create(
        cls,
        directory: str | os.PathLike[str],
        method: Method,
        geometry: Geometry,
        *,
        instrument: Instrument = UNCALIBRATED,
        detector_bits: int | None = SA220P_DETECTOR_BITS,
        adc_name: str = "",
        console_version: str = "",
        clock: Callable[[], float] = time.monotonic,
        started: float | None = None,
        overwrite: bool = False,
    ) -> Recording:
        """Create the raw file, with its schema and global parameters, and return the
        recording that will fill it.

        `instrument` is the machine's own settings -- the m/z calibration, the vertical
        window, the instrument's name -- and defaults to `UNCALIBRATED`, which writes
        `CalibrationDone = 0` and stamps no vertical settings, exactly the file this code
        wrote before `clockwork.instrument` existed. **None of the three is a method
        field**: the same strings run after a recalibration, or through a different
        attenuator, are the same method and a different file (lab record, tasks 06
        and 25). A file acquired uncalibrated is calibratable afterwards from its own
        parameters, which is why nothing ever blocked on this.

        `console_version` is what to stamp as the console that acquired the file, and
        `Console.info().text` is the string to pass: it names the card, its firmware, the
        console's version and commit, and on the lab's build the fork and branch as well.

        `clock` and `started` are the run clock every frame's `StartTime` is measured
        against, and they default to this recording's own creation. **The acquisition
        loop owns the clock and passes both down** (`clockwork.acq.loop`, lab record,
        task 23), so that a run's own record of when a frame began and the file's
        `StartTime` share one origin rather than two that nearly agree. The default is
        what makes a `Recording` driven by hand -- a bench rig, a test -- write true
        start times anyway, instead of writing zeros unless the caller remembers to say
        otherwise (lab record, task 25). The same clock times the frames, so a test that
        substitutes one controls both.

        The summed companion is not created here. It is created by the first `fold`, so
        that a run which never gets that far does not leave an empty second file.
        """
        stem = method.acquisition.file_stem
        path = raw_path(directory, stem)
        globals_ = GlobalSpec(
            bins=geometry.bins,
            bin_width_ns=geometry.bin_width_ns,
            instrument_name=instrument.name,
            tof_intensity_type="ADC",
            time_offset_ns=int(round(geometry.time_offset_ns)),
            prescan_tof_pulses=method.acquisition.scans,
            prescan_accumulations=method.acquisition.accumulations,
            detector_bits=detector_bits,
            extra=stamp_globals(method, instrument=instrument, adc_name=adc_name,
                                console_version=console_version),
        )
        writer = UimfWriter(path, globals_, overwrite=overwrite)
        recording = cls.__new__(cls)
        recording._init(method, geometry, writer, globals_, instrument, clock,
                        clock() if started is None else float(started), overwrite)
        return recording

    # --- paths -------------------------------------------------------------------------

    @property
    def raw_path(self) -> str:
        """The file the console appends `Frame_Scans` rows to."""
        return self._raw.path

    @property
    def summed_path(self) -> str:
        """Where the fold writes, whether or not it has written yet."""
        return summed_path(self._raw.path)

    def frames_of(self, method_frame: int) -> list[int]:
        """The raw frame numbers one method frame produced, in acquisition order."""
        return list(self._frames.get(int(method_frame), ()))

    # --- the two phases ----------------------------------------------------------------

    def begin_frame(self, method_frame: int, repetition: int = 1) -> FrameRequest:
        """Phase one: write the frame's parameters, and return the console's request.

        The frame number the writer assigns is the `frame_number` the request carries, so
        the rows the console appends land under the parameters just written. That pairing
        is the whole reason this returns the request rather than taking one.

        `repetition` counts from 1 within `method_frame`, and in `single_frame` mode
        there is only ever one: that console frame holds every repetition, so the frame
        it writes names its method frame and claims to be no repetition of it.

        **`StartTime` is written here**, as minutes since the run clock's origin, which
        is what a viewer lays a session out along and what was 0.0 on every frame until
        task 25. Minutes because that is the unit `StartTimeMinutes` is defined in and
        the legacy `StartTime` column holds; the arithmetic is in one place so that the
        two files cannot disagree about it.
        """
        self._require_open()
        if self._open_frame is not None:
            raise ValueError(
                f"frame {self._open_frame[0]} was begun and never ended; end_frame() "
                "writes the completion marker and has to be called before the next frame"
            )
        method_frame = int(method_frame)
        repetition = int(repetition)
        acquisition = self.acquisition
        if not 1 <= repetition <= acquisition.console_frames:
            raise ValueError(
                f"repetition {repetition} outside 1..{acquisition.console_frames} for "
                f"repetition_mode {acquisition.repetition_mode!r}"
            )
        slope, intercept = self.calibration
        # `repetitions` is what the method asked for, so it goes on every frame in
        # both modes and both files; `repetition` is which one this frame is, and only
        # a frame that is one of them has it. In `single_frame` mode the one console
        # frame holds every repetition, so it names its method frame and claims to be
        # no repetition of it. Together those two say all three cases a viewer that has
        # never seen the method has to tell apart: ungrouped, one repetition of a method
        # frame, or the whole of one.
        whole = acquisition.repetition_mode == "single_frame"
        now = self._clock()
        # The first repetition's start time is the method frame's, and the fold writes it
        # on the summed frame: a summed frame begins when its method frame began, not
        # when the fold got round to it.
        self._began.setdefault(method_frame, now)
        spec = FrameSpec(
            scans=acquisition.frame_length,
            accumulations=1,
            calibration_slope=slope,
            calibration_intercept=intercept,
            average_tof_length_ns=self.geometry.average_tof_length_ns,
            start_time_minutes=(now - self._started) / 60.0,
            method_frame=method_frame,
            repetition=None if whole else repetition,
            repetitions=acquisition.accumulations,
        )
        frame = self._raw.add_frame(spec)
        self._frames.setdefault(method_frame, []).append(frame)
        self._open_frame = (frame, method_frame, repetition, now)
        return FrameRequest(
            frame_length=acquisition.frame_length,
            file_name=self._raw.path,
            frame_number=frame,
            nbr_accumulations=1,
            offset_bins=self.geometry.offset_bins,
            nbr_samples=self.geometry.bins,
        )

    def end_frame(self, *, duration_s: float | None = None, complete: bool = True) -> int:
        """Phase two: write the completion marker. Returns the frame.

        **No duration by default, on purpose.** `add_frame` leaves a zero
        `DurationSeconds` row for the console to update, and the console fills it from
        the card's own sample clock, which is a better measurement of a frame than any
        wall clock on this side. Writing one here would overwrite it. Pass `duration_s`
        only to say something the console cannot, which in practice means a frame the
        console never wrote to (lab record, task 16).

        `complete = False` leaves the frame provisional, which is what to do for a frame
        known to have been cut short: an empty frame, a console that died, a run the
        trainee stopped. A provisional frame is not a broken one. Its rows are as good as
        they are, and the file says not to trust its length.
        """
        self._require_open()
        if self._open_frame is None:
            raise ValueError("no frame is open; begin_frame() first")
        frame, method_frame, _repetition, started = self._open_frame
        self._open_frame = None
        elapsed = self._clock() - started
        span = self._elapsed.get(method_frame, 0.0)
        self._elapsed[method_frame] = span + elapsed
        self._raw.finalise_frame(
            frame,
            duration_s=None if duration_s is None else float(duration_s),
            complete=complete,
        )
        return frame

    @contextlib.contextmanager
    def frame(self, method_frame: int, repetition: int = 1) -> Iterator[FrameRequest]:
        """`begin_frame` and `end_frame` around a block, marking the frame incomplete if
        the block raises.

        The shape a caller actually wants, and the one that cannot forget the marker. An
        acquisition that throws part way through a frame leaves that frame provisional
        and every earlier one complete, which is exactly what the file should say.
        """
        request = self.begin_frame(method_frame, repetition)
        try:
            yield request
        except BaseException:
            self.end_frame(complete=False)
            raise
        self.end_frame()

    # --- the fold ----------------------------------------------------------------------

    def fold(self, method_frame: int) -> int:
        """Sum a method frame's repetitions into the companion file. Returns rows written.

        Runs after the last repetition of `method_frame` and can run while the next
        method frame acquires: it opens the raw file through the ordinary reader, which
        takes a short read lock per query and never blocks the console for longer than
        one of them.

        The two repetition modes fold differently and produce the same frame. In
        `per_repetition` the repetitions are separate raw frames and the fold adds them;
        in `single_frame` they are consecutive blocks of `Scans` inside one raw frame and
        the fold adds those. Either way the summed frame is `scans` long with
        `Accumulations` = A, which is the shape FALKOR writes today.

        **The two files mean different things by `DurationSeconds`.** A raw frame's is
        the console's, counted off the card's own sample clock. A summed frame's is the
        wall time its whole method frame took, the per-repetition restart included,
        because no console ever touches that file and there is no better number to have.
        So differencing the two measures the restart gap, which is a real quantity but
        not one either column was written to report.
        """
        self._require_open()
        method_frame = int(method_frame)
        numbers = self._frames.get(method_frame)
        if not numbers:
            raise KeyError(f"no frames were acquired for method frame {method_frame}")
        if method_frame in self._folded:
            raise ValueError(
                f"method frame {method_frame} has already been folded; a second fold "
                "would add a second summed frame for it"
            )
        if self._open_frame is not None:
            raise ValueError(
                f"frame {self._open_frame[0]} is still open; a fold reads rows the "
                "console may still be writing"
            )

        acquisition = self.acquisition
        source = UimfFile(self._raw.path)
        pieces = [fold_scans(source.read_frame(number), acquisition.scans)
                  for number in numbers]
        total = sum_frames(pieces)
        assert total is not None  # no cancel callback was passed

        writer = self._summed_writer()
        frame = writer.add_frame(FrameSpec(
            scans=acquisition.scans,
            accumulations=acquisition.accumulations,
            calibration_slope=self.calibration[0],
            calibration_intercept=self.calibration[1],
            average_tof_length_ns=self.geometry.average_tof_length_ns,
            # The method frame's first repetition, not the fold's own moment: a summed
            # frame stands for when the experiment happened, and the fold can run a whole
            # method frame later than the rows it is adding up.
            start_time_minutes=(self._began.get(method_frame, self._started)
                                - self._started) / 60.0,
            method_frame=method_frame,
            repetitions=acquisition.accumulations,
        ))
        rows = writer.write_scans(frame, _scan_rows(total))
        # A summed frame is the whole of its method frame, so it names the method frame
        # and claims to be no repetition of it. Its duration is how long that method
        # frame took to acquire, the console's restart between repetitions included,
        # which is the number `DurationSeconds` means in the files this shape comes
        # from. No console ever touches this file, so nothing else will fill it in.
        writer.finalise_frame(frame, duration_s=self._elapsed.get(method_frame, 0.0))
        self._folded.add(method_frame)
        return rows

    def _summed_writer(self) -> UimfWriter:
        """The companion's writer, created at the first fold and not before.

        Refuses an existing companion on the same terms as the raw file, rather than
        replacing it: with `keep_raw = false` a leftover `<stem>.summed.uimf` can be the
        only surviving copy of an earlier acquisition, and the raw file whose absence
        would have raised at `create` was deleted on purpose.
        """
        if self._summed is None:
            globals_ = GlobalSpec(
                bins=self.geometry.bins,
                bin_width_ns=self.geometry.bin_width_ns,
                instrument_name=self._raw_globals.instrument_name,
                tof_intensity_type=self._raw_globals.tof_intensity_type,
                time_offset_ns=self._raw_globals.time_offset_ns,
                prescan_tof_pulses=self.acquisition.scans,
                prescan_accumulations=self.acquisition.accumulations,
                detector_bits=self._raw_globals.detector_bits,
                extra=dict(self._raw_globals.extra),
            )
            self._summed = UimfWriter(self.summed_path, globals_,
                                      overwrite=self._overwrite)
        return self._summed

    # --- closing -------------------------------------------------------------------------

    def close(self) -> None:
        """Close both files, and honour `keep_raw`. Idempotent.

        The raw file is removed only when the method says to discard it **and** a summed
        companion exists to have replaced it. Discarding is irreversible and the raw file
        is the only record of how one repetition differed from the next, which is what a
        drift correction would need (lab record, task 02); a run that produced no
        companion has nothing to be replaced by.
        """
        if self._closed:
            return
        self._closed = True
        self._raw.close()
        if self._summed is not None:
            self._summed.close()
        if not self.keep_raw and self._folded:
            with contextlib.suppress(OSError):
                os.remove(self._raw.path)

    def __enter__(self) -> Recording:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (f"{type(self).__name__}({self._raw.path!r}, "
                f"frames={sum(len(v) for v in self._frames.values())})")

    def _require_open(self) -> None:
        if self._closed:
            raise ValueError(f"{self._raw.path}: recording is closed")


def stamp_globals(
    method: Method,
    *,
    instrument: Instrument = UNCALIBRATED,
    adc_name: str = "",
    console_version: str = "",
) -> dict[str, object]:
    """The provenance of one acquisition, as `Global_Params` values.

    `clockwork.method.stamp` produces the record -- the method's name, a hash of its
    canonical text, that text in full, the clockwork version and the console's -- and
    this is where it lands. **In the file rather than beside it**: what leaves the
    instrument is a `.uimf` on its own, and a sidecar is a file a trainee can copy
    without (lab record, task 06).

    The whole record goes under `PROVENANCE_KEYS`, which are clockwork's own parameter
    IDs; UIMF-Library skips an ID it does not recognise rather than failing, so they
    cost a downstream PNNL tool nothing. The method's *name* is written a second time
    under PNNL's own `AcquisitionMethod`, which every tool that reads UIMF at all reads,
    so a file says whose method made it even to something that has never heard of
    clockwork.
    """
    record = stamp(method, console_version=console_version or None)
    # The vertical settings are not the method's and `stamp()` does not produce them, so
    # they join the record here rather than there. `None` for either is written by
    # nothing: a rig with no configured window states no window (lab record, task 25).
    record["channel_offset_v"] = instrument.vertical.offset_v
    record["full_scale_v"] = instrument.vertical.full_scale_v
    values: dict[str, object] = {"AcquisitionMethod": record["method_name"]}
    if adc_name:
        values["ADCName"] = adc_name
    for key, field in PROVENANCE_KEYS:
        value = record.get(field)
        # `None` and the empty string are "not known" and are left out; 0.0 is not. A
        # channel offset of zero volts is a real setting and a falsy one, which is why
        # this is not the truth test it looks like it wants to be.
        if value is not None and value != "":
            values[key] = value
    return values
