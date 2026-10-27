"""The beam guard: a run that ends itself when the beam is lost at the source.

A queue left to run overnight is only as good as the beam under it. Lost at the
source -- a spray that stops, an emitter tip that blocks -- the instrument goes on
acquiring: every frame counts out, every fold writes, every row reads `done`, and
the loss is found the next morning from the files (lab record, task 134, where a
sweep walked fifty-three rows past one). Nothing the loop already watches can see
it, because nothing has failed. What changes is how many ions a repetition holds,
by one to two orders of magnitude, so that is what this watches.

**One number a repetition, and no knowledge of the sample.** The rule reads each
repetition's total ion count (`FrameRecord.total_counts`, the frame's TIC off the
data socket) against a floor, and a repetition under it is one with no beam. Every
ion and not a precursor window, so that a hold that fragments the precursor away
does not read as a lost beam, and so that the same rule serves any method without
an m/z in it.

**N in a row, not one.** A single repetition can read low on a good beam -- a
repetition in a zero mode, a sputter -- and the lowest ones on the lab's first
sweep came within 1.3x of the floor this was built around while the beam held
(lab record, task 134). So the run ends only when `repetitions` consecutive
repetitions read under the floor. A repetition that did not acquire neither counts
nor resets the count: it holds no data either way, and the loop's own
`ABORT_AFTER_FAILURES` is what ends a run of those.

**Counted within a run.** The count starts again with every run, so a run of
fewer than `repetitions` repetitions cannot trip it; a beam lost at the end of one
row is found within the first `repetitions` of the next.

**A trip is a failure, not a stop.** The run ends after the repetition in hand and
its fold, as the operator's Stop would end it, and its file says `failed` with the
guard's sentence as the reason, because what it holds is not the experiment its
method asked for (`clockwork.acq.loop`, `BeamLost`).

**The floor is a number, and where it came from is a sentence.** Usually it is a
fraction of the median per-repetition total of a run acquired on a beam known to
be good, the gate run of the day (`from_run`); `source` carries that so the run
log can say what the floor is a fraction of.
"""

from __future__ import annotations

import os
import statistics
from dataclasses import dataclass

from mainspring.uimf import SUMMED_SUFFIX, UimfFile

__all__ = [
    "GUARD_FRACTION",
    "GUARD_REPETITIONS",
    "BeamGuard",
    "from_run",
    "repetition_totals",
]

GUARD_REPETITIONS = 5
"""Consecutive repetitions under the floor that end a run.

Five, from the lab's first sweep: its two losses fell to 2-14 % of the gate run's
median within a row and stayed there, so five more repetitions is a few minutes
of a lost row, and no five consecutive repetitions of any of 277 runs with beam
fell under 0.1x (lab record, task 134)."""

GUARD_FRACTION = 0.1
"""The floor as a fraction of a good run's median total a repetition (`from_run`).

On the same sweep the lowest stretch of five repetitions with beam peaked at
0.35x its gate run's median and the losses sat at 0.01-0.15x, so 0.1x lies between
them with room on the good side for a beam that declines before it is refreshed
(lab record, task 134)."""


@dataclass(frozen=True, slots=True)
class BeamGuard:
    """The floor a repetition's total ion count must reach, and how many may miss it."""

    floor: float
    """Total ion counts a repetition, under which that repetition had no beam."""
    repetitions: int = GUARD_REPETITIONS
    """How many consecutive repetitions under `floor` end the run."""
    source: str = ""
    """Where `floor` came from, in words: `0.1x the median of 261027_BK_330 (2.78e6)`.
    Empty for a number typed in."""

    def __post_init__(self) -> None:
        if not self.floor > 0:
            raise ValueError(f"a beam guard's floor must be above zero, not {self.floor}")
        if self.repetitions < 1:
            raise ValueError("a beam guard needs at least one repetition to count, "
                             f"not {self.repetitions}")

    def under(self, total_counts: int) -> bool:
        """Whether a repetition with this many ions had no beam."""
        return total_counts < self.floor

    @property
    def text(self) -> str:
        """The guard in one line, as the run log states it when a queue starts."""
        where = f" ({self.source})" if self.source else ""
        return (f"a run stops when {self.repetitions} repetitions in a row read under "
                f"{self.floor:.3g} ions{where}")


def repetition_totals(path: str | os.PathLike[str]) -> list[float]:
    """Every repetition's total ion count in a per-repetition file, in frame order.

    The file's own `TIC` column added up per frame (`UimfFile.frame_totals`), which is
    what `FrameRecord.total_counts` reads off the data socket while the run acquires.
    Refuses the summed companion, whose one frame is every repetition added together.
    """
    text = os.fspath(path)
    if text.endswith(SUMMED_SUFFIX):
        raise ValueError(f"{os.path.basename(text)} is a summed companion, which holds "
                         "every repetition added together; choose the per-repetition "
                         "file beside it")
    totals = UimfFile(text).frame_totals()
    if not totals:
        raise ValueError(f"{os.path.basename(text)} holds no scans")
    return [totals[frame] for frame in sorted(totals)]


def from_run(path: str | os.PathLike[str], fraction: float = GUARD_FRACTION,
             repetitions: int = GUARD_REPETITIONS) -> BeamGuard:
    """A guard whose floor is `fraction` of a run's median total ion count a repetition."""
    if not 0 < fraction <= 1:
        raise ValueError(f"the fraction must be above 0 and at most 1, not {fraction}")
    median = statistics.median(repetition_totals(path))
    if median <= 0:
        raise ValueError(f"{os.path.basename(os.fspath(path))} counted no ions")
    stem = os.path.basename(os.fspath(path))
    stem = stem[: -len(".uimf")] if stem.endswith(".uimf") else stem
    return BeamGuard(floor=fraction * median, repetitions=repetitions,
                     source=f"{fraction:g}x the median of {stem} ({median:.3g})")
