r"""The window's saved settings, read without Qt, for a verb that has no window.

`clockwork warm-up` run from a desktop shortcut is given no flags: the installer cannot
know where the lab keeps its method library or its instrument document, and the window
already remembers both (lab record, task 95). `QSettings` on Windows is the registry, one
string value per key under `HKEY_CURRENT_USER\Software\BushLab\clockwork`, so this reads
those values with `winreg` and answers them as `clockwork.app.settings.Settings` would.
Off Windows `QSettings` writes an INI file this does not parse, and a verb there answers
nothing saved, which leaves it to its flags.

Only reads. Nothing here writes a setting, and nothing here imports PySide6.
"""

from __future__ import annotations

import sys

__all__ = ["APPLICATION", "KEYS", "ORGANISATION", "saved_settings"]

ORGANISATION = "BushLab"
APPLICATION = "clockwork"

KEYS = ("library_dir", "instrument_path", "output_dir", "console_path")
"""The settings a verb that starts a daemon or reads an instrument document needs."""


def saved_settings(keys: tuple[str, ...] = KEYS) -> dict[str, str]:
    """Each of `keys` the window has saved, as text; a key never saved is left out."""
    if sys.platform != "win32":
        return {}
    import winreg

    found: dict[str, str] = {}
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             rf"Software\{ORGANISATION}\{APPLICATION}")
    except OSError:
        return {}
    with key:
        for name in keys:
            try:
                value, _ = winreg.QueryValueEx(key, name)
            except OSError:
                continue
            if isinstance(value, str) and value.strip():
                found[name] = value.strip()
    return found
