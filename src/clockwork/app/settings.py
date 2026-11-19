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

from .saved import APPLICATION, ORGANISATION

__all__ = ["ORGANISATION", "APPLICATION", "Settings"]


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
    def limits_path(self) -> str:
        """The standing limits the daemon this window starts carries, or empty for the
        `limits.toml` beside the instrument document (lab record, task 165; the Setup
        tab that edits it is task 169's)."""
        return str(self._settings.value("limits_path", "") or "")

    @limits_path.setter
    def limits_path(self, value: str) -> None:
        self._settings.setValue("limits_path", value)

    @property
    def project_limits_path(self) -> str:
        """A project's limits the daemon this window starts carries, or empty for none."""
        return str(self._settings.value("project_limits_path", "") or "")

    @project_limits_path.setter
    def project_limits_path(self, value: str) -> None:
        self._settings.setValue("project_limits_path", value)

    @property
    def at_window(self) -> str:
        """The initials last typed in "at the window", the person who presses Start;
        empty until one is, when the window offers the file initials instead."""
        return str(self._settings.value("at_window", "") or "")

    @at_window.setter
    def at_window(self, value: str) -> None:
        self._settings.setValue("at_window", value)

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

    @property
    def library_dir(self) -> str:
        """Where the method library browser looks (lab record, task 54).

        No machine-independent default: the instrument's method library is lab material and
        a public clone has no opinion about where a trainee's methods live. The window
        offers `instrument_dir`'s `golden` directory as a starting point when it resolves
        and this is still empty, which is a one-time suggestion rather than a stored default
        -- once a trainee points the setting anywhere, that choice is what is remembered.
        """
        return str(self._settings.value("library_dir", "") or "")

    @library_dir.setter
    def library_dir(self, value: str) -> None:
        self._settings.setValue("library_dir", value)

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

    @property
    def tab(self) -> str:
        """The tab the window was left on, by its name (lab record, task 169); empty for
        the Method tab, the ordinary way to work."""
        return str(self._settings.value("tab", "") or "")

    @tab.setter
    def tab(self, value: str) -> None:
        self._settings.setValue("tab", str(value))

    @property
    def open_mainspring_on_acquire(self) -> bool:
        """Whether a run starting opens a viewer on the file it is writing (task 55;
        any client's run since task 169, the window revision's D4).

        Off by default, which is the decision and not caution: a window that started
        another program every time a button was pressed would be taking the screen at
        the moment a trainee was looking at something else, and the button beside the
        checkbox already answers for anyone who wants one run watched. Remembered
        because the day a trainee turns it on is a day they want it on all day.
        """
        return str(self._settings.value("open_mainspring_on_acquire", "")).lower() \
            in ("true", "1")

    @open_mainspring_on_acquire.setter
    def open_mainspring_on_acquire(self, value: bool) -> None:
        self._settings.setValue("open_mainspring_on_acquire", bool(value))

    @property
    def kept_root(self) -> str:
        """Where failed and reported runs' files are copied (`clockwork.keep`), or empty
        for the default. `$CLOCKWORK_REPORTS` outranks it (task 81)."""
        return str(self._settings.value("kept_root", "") or "")

    @kept_root.setter
    def kept_root(self, value: str) -> None:
        self._settings.setValue("kept_root", value)

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
