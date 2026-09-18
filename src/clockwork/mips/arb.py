"""What an ARB module does to a setting between being told it and being asked for it.

One fact, and it is enough to need a module of its own: a module's waveform frequency
comes off an integer divider, so `SWFREQ` is a request and `GWFREQ` is what the divider
could actually make of it. `docs/mips-wire-format.md` section 6.2 is the source and this
implements it; nothing here decides protocol.

Separate from `state.py` because both ends need it -- the state table marks a readback
against it, and the simulated box in `transport.py` answers with it -- and `state.py`
sits above `transport.py` in the import order.

No Qt, no hardware, no I/O.
"""

from __future__ import annotations

__all__ = [
    "ARB_MODULE_MCK_HZ",
    "ARB_POINTS_PER_PERIOD",
    "ARB_PPP_RANGE",
    "arb_frequency",
    "arb_points_per_period",
]

ARB_MODULE_MCK_HZ = 42_000_000
"""The ARB module board's master clock, which its waveform divider runs off.

The module's own figure and not the controller's: the MIPS controller is an Arduino Due
at 84 MHz, and `SetFrequency` in the module firmware states 42 MHz (section 6.2).
"""

ARB_POINTS_PER_PERIOD = 32
"""Points per waveform period (`SARBPPP`) assumed where the reading does not say.

Every ARB module on this instrument is at 32, which is the firmware's own documented
value and what makes `SWFREQ,n,15000` read back as 14914. It is not read: changing it
needs a reboot, and `GARBPPP` is four more round trips per box for a number that has not
moved. `arb_points_per_period` covers the case where it has, so a module at another
value is recognised rather than reported as a disagreement.
"""

ARB_PPP_RANGE = range(8, 129)
"""What `SARBPPP` accepts (section 6.2), for `arb_points_per_period` to search."""


def arb_frequency(requested: float,
                  points_per_period: int = ARB_POINTS_PER_PERIOD,
                  *, mode: str = "TWAVE") -> int | None:
    """What `GWFREQ` answers after `SWFREQ,<mod>,<requested>`. Wire format section 6.2.

    A module's output clock is an integer divider off its master clock, so it cannot
    produce an arbitrary frequency: the firmware takes the nearest divider at or below
    the request and reports what that gives. `SWFREQ,n,15000` in TWAVE mode at 32 points
    per period is divider 44 and therefore **14914 Hz**, on every module of both ARB
    boxes -- which is the correct answer and not a failed send, and which a host that
    compared the readback with the request reported as eight disagreements on every run
    (lab record, tasks 40 and 56).

    Integer division throughout, exactly as the firmware does it. Returns None for a
    request no divider can be computed for at all.
    """
    period = points_per_period if mode.strip().upper() != "ARB" else 1
    if requested <= 0 or period <= 0:
        return None
    divisor = 2 * period * int(requested)
    if divisor <= 0:
        return None
    clock_divider = ARB_MODULE_MCK_HZ // divisor + 1
    return ARB_MODULE_MCK_HZ // (2 * period * clock_divider)


def arb_points_per_period(requested: float, achieved: float,
                          *, mode: str = "TWAVE") -> int | None:
    """Which `SARBPPP` would make `achieved` the divider's answer to `requested`.

    The reading back-solved rather than the setting read, so a module at a
    points-per-period other than `ARB_POINTS_PER_PERIOD` is recognised for what it is
    instead of reported as a disagreement. Returns the smallest value that fits, or None
    where no points-per-period explains the reading -- which is the one case that *is* a
    disagreement: a module holding a frequency nobody asked it for.
    """
    for period in ARB_PPP_RANGE:
        if arb_frequency(requested, period, mode=mode) == int(achieved):
            return period
    return None
