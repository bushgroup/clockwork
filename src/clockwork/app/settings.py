"""What the window remembers between launches, in one place with one name each.

`QSettings` with an explicit organisation and application name, so that the registry
key a trainee's settings live under does not change when the executable is renamed or
moved. Everything here is a convenience: a clockwork that has forgotten all of it still
runs, asks for a method and a directory, and acquires. Nothing that decides what an
experiment *is* is stored here -- that is the method document and the instrument
document, and this remembers only where they were.

The six are the window design's (lab record, task 50): initials, output directory, last
method, the instrument document, the console executable and mainspring. The window's
geometry is kept too, because a window that opens off-screen on a second monitor is the
one remembered setting a trainee cannot fix from inside it -- `reset_geometry` is why.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QSettings

__all__ = ["ORGANISATION", "APPLICATION", "Settings"]

ORGANISATION = "BushLab"
APPLICATION = "clockwork"


class Settings:
    """Named accessors over `QSettings`, so no key is spelled twice.

    Every getter takes a default and every value is coerced on the way out:
    `QSettings` on Windows returns strings for everything it wrote, so an `int` stored
    and read back is a `str` unless something converts it, and a spinner given a
    string raises inside Qt rather than where the mistake is.
    """

    def __init__(self, backing: QSettings | None = None) -> None:
        self._settings = backing or QSettings(ORGANISATION, APPLICATION)

    # -- the six that matter -------------------------------------------------

    @property
    def initials(self) -> str:
        return str(self._settings.value("initials", "") or "")

    @initials.setter
    def initials(self, value: str) -> None:
        self._settings.setValue("initials", value)

    @property
    def output_dir(self) -> str:
        return str(self._settings.value("output_dir", "") or "")

    @output_dir.setter
    def output_dir(self, value: str) -> None:
        self._settings.setValue("output_dir", value)

    @property
    def method_path(self) -> str:
        return str(self._settings.value("method_path", "") or "")

    @method_path.setter
    def method_path(self, value: str) -> None:
        self._settings.setValue("method_path", value)

    @property
    def instrument_path(self) -> str:
        return str(self._settings.value("instrument_path", "") or "")

    @instrument_path.setter
    def instrument_path(self, value: str) -> None:
        self._settings.setValue("instrument_path", value)

    @property
    def console_path(self) -> str:
        return str(self._settings.value("console_path", "") or "")

    @console_path.setter
    def console_path(self, value: str) -> None:
        self._settings.setValue("console_path", value)

    @property
    def mainspring_path(self) -> str:
        """Where mainspring is, for the case the `.uimf` association does not answer.

        Empty is the ordinary state and not a gap: the association mainspring's own
        installer registers is tried first (Matt, 2026-09-17), and this is filled in
        only on a machine where opening a `.uimf` does the wrong thing or nothing.
        """
        return str(self._settings.value("mainspring_path", "") or "")

    @mainspring_path.setter
    def mainspring_path(self, value: str) -> None:
        self._settings.setValue("mainspring_path", value)

    # -- the run's own fields ------------------------------------------------

    @property
    def replicates(self) -> int:
        return _as_int(self._settings.value("replicates", 1), 1)

    @replicates.setter
    def replicates(self, value: int) -> None:
        self._settings.setValue("replicates", int(value))

    @property
    def conditions(self) -> str:
        """The last run's conditions note, offered again as a starting point.

        Remembered because an acquisition day is a series of runs whose sample,
        MCP voltage and collision energy mostly do not change, and retyping the note
        each time is how a note stops being written (lab record, task 40)."""
        return str(self._settings.value("conditions", "") or "")

    @conditions.setter
    def conditions(self, value: str) -> None:
        self._settings.setValue("conditions", value)

    @property
    def open_state_panels(self) -> frozenset[str]:
        """Which boxes' state panels were left open, by box name.

        Per pane rather than one flag for the window: a trainee watching one box's ARB
        modules through a tuning session has no use for the other two rack boxes' rows
        taking up the same screen, and which box that is, is the thing worth
        remembering (task 51).
        """
        raw = str(self._settings.value("open_state_panels", "") or "")
        return frozenset(name for name in raw.split(",") if name)

    @open_state_panels.setter
    def open_state_panels(self, value: object) -> None:
        self._settings.setValue("open_state_panels", ",".join(sorted(value)))

    # -- the window itself ---------------------------------------------------

    @property
    def geometry(self) -> QByteArray:
        value = self._settings.value("geometry", QByteArray())
        return value if isinstance(value, QByteArray) else QByteArray()

    @geometry.setter
    def geometry(self, value: QByteArray) -> None:
        self._settings.setValue("geometry", value)

    def reset_geometry(self) -> None:
        """Forget the remembered size and position.

        The one setting a trainee cannot fix from inside the window: a window
        restored onto a monitor that is no longer there opens where nobody can reach
        it, and the remedy has to be reachable from outside that window.
        """
        self._settings.remove("geometry")

    def sync(self) -> None:
        self._settings.sync()


def _as_int(value: object, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default
