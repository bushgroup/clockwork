"""The acquisition console in the status bar, and the six settings the window exposes.

A trainee never sees a console window: clockwork starts the process, waits the ~5.4 s a
card open costs, watches it, restarts it and stops it on exit (lab record, task 50,
decision 4, landed as `clockwork.acq.process`). What is left for the window is to
say which of those is happening and to offer the settings that change what the data is.

**Six keys get widgets** (Matt, 2026-09-17): the five the fork moves out of the source
that decide what the card records -- the trigger level and slope, the full scale, and the
zero-suppress threshold and hysteresis -- plus `AcquisitionTimeoutMs`, which is here
because a value left behind from a timing measurement aborted a CLOCK run at 250 ms and
nothing in the window said so (lab record, task 47). The other nine keys are shown
read-only: `ResourceName` and the buffer pool are not things an instrument day should
change, and changing them from here would make a bad afternoon possible without making
a good one easier.

**Every change is a write and a restart.** The console reads `config.txt` once, at
startup, so a written key that is not followed by a restart changes nothing at all --
which is exactly the failure task 47 found. The dialog says so, the restart is offered
in the same click, and the status bar shows the drift where a file and a running process
have parted company.
"""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..acq import KEYS, ConsoleConfig

__all__ = ["EXPOSED", "ConsoleBar", "ConsoleSettings", "exposed_values"]

EXPOSED = (
    "TriggerLevel",
    "TriggerSlope",
    "FullScaleRange",
    "ZeroSuppressThreshold",
    "ZeroSuppressHysteresis",
    "AcquisitionTimeoutMs",
)
"""Which `config.txt` keys the window lets a trainee change (Matt, 2026-09-17).

The order is the order they appear in the dialog, which is the order they matter on an
instrument day rather than the order the console reads them.
"""

_KEYS = {key.name: key for key in KEYS}


class ConsoleBar(QWidget):
    """The console's state, a restart and a way into the settings. For the status bar.

    Shows only what the process itself said. A console whose `info` has not arrived
    reports "starting", never a guess, because the five seconds a card open takes is
    exactly the window in which a trainee would otherwise be told the instrument is
    ready.
    """

    restart_requested = Signal()
    settings_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.label = QLabel("console: not started")
        self.restart = QPushButton("Restart")
        self.restart.setToolTip(
            "Stop the console and start it again, about 5.4 s. This is what makes a "
            "changed config.txt take effect: the console reads that file only at "
            "startup.")
        self.settings = QPushButton("Console settings…")
        self.restart.clicked.connect(self.restart_requested)
        self.settings.clicked.connect(self.settings_requested)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.label)
        layout.addWidget(self.restart)
        layout.addWidget(self.settings)

    def show_status(self, status: object) -> None:
        """Render a `clockwork.app.worker.ConsoleStatus`."""
        state = getattr(status, "state", "not started")
        self.label.setText(getattr(status, "text", state))
        tip = [getattr(status, "info", "") or "", getattr(status, "endpoint", "") or ""]
        drift = tuple(getattr(status, "drift", ()) or ())
        if drift:
            self.label.setText(self.label.text() + f"  ({len(drift)} setting(s) drifted)")
            tip += ["config.txt and the running console disagree on:", *drift,
                    "A restart is what makes them agree."]
        self.label.setToolTip("\n".join(part for part in tip if part))
        self.restart.setEnabled(state in ("ready", "stopped", "failed"))
        self.settings.setEnabled(state == "ready")


class ConsoleSettings(QDialog):
    """The six editable keys, with their ranges and what each one does.

    Built from `ConsoleConfig.KEYS` rather than from a list written here, so a key that
    gains a range in `docs/console-protocol.md` gains it in this dialog with no edit:
    the document is the source and the table mirrors it (`clockwork.acq.process`).
    """

    def __init__(self, config: ConsoleConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Console settings")
        self._config = config
        self._fields: dict[str, QWidget] = {}

        form = QFormLayout()
        for name in EXPOSED:
            key = _KEYS[name]
            widget = _widget_for(key, config.in_force(name))
            widget.setToolTip(_tooltip(key))
            self._fields[name] = widget
            label = QLabel(name)
            label.setToolTip(_tooltip(key))
            form.addRow(label, widget)

        fixed = QLabel(self._fixed_text())
        fixed.setTextFormat(Qt.TextFormat.PlainText)
        fixed.setStyleSheet("color: palette(mid);")

        warning = QLabel(
            "Saving writes config.txt and restarts the console, about 5.4 s. The "
            "console reads that file only at startup, so a change without a restart "
            "does nothing.")
        warning.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(
            "Save and restart")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(warning)
        layout.addWidget(QLabel("Shipped in config.txt, not editable here:"))
        layout.addWidget(fixed)
        layout.addWidget(buttons)

    def values(self) -> dict[str, object]:
        """Only the keys whose value the dialog actually changed.

        A dialog that wrote all six every time would rewrite `config.txt` and restart
        the console for an OK pressed on an unchanged form, and would make a key the
        file deliberately leaves out (so that the console's compiled-in default is in
        force) into one the file states.

        Compared through `ConsoleConfig.value_of`, which parses a value the way the
        console does, so that `0.4` and `0.40` are one setting. Comparing the rendered
        strings would have reported a change, rewritten the file and cost a restart
        for a number nobody touched.
        """
        changed: dict[str, object] = {}
        for name, widget in self._fields.items():
            now = _value_of(widget)
            before = self._config.value_of(name)
            if isinstance(before, float) and isinstance(now, (int, float)):
                if float(now) != before:
                    changed[name] = now
            elif str(now) != str(before):
                changed[name] = now
        return changed

    def _fixed_text(self) -> str:
        rows = [f"  {key.name} = {self._config.in_force(key.name)}"
                for key in KEYS if key.name not in EXPOSED]
        return "\n".join(rows)


def _widget_for(key: object, current: str) -> QWidget:
    """The right widget for one key: a choice where the fork allows only two values."""
    allowed = tuple(getattr(key, "allowed", ()) or ())
    kind = getattr(key, "kind", "text")
    if allowed:
        box = QComboBox()
        box.addItems(list(allowed))
        index = box.findText(current)
        box.setCurrentIndex(index if index >= 0 else 0)
        return box
    if kind == "int":
        spin = QSpinBox()
        low = getattr(key, "low", None)
        high = getattr(key, "high", None)
        spin.setRange(int(low) if low is not None else -2_147_483_648,
                      int(high) if high is not None else 2_147_483_647)
        spin.setValue(_int(current, spin.minimum()))
        return spin
    if kind == "float":
        spin = QDoubleSpinBox()
        spin.setDecimals(6)
        low = getattr(key, "low", None)
        high = getattr(key, "high", None)
        spin.setRange(float(low) if low is not None else -1e9,
                      float(high) if high is not None else 1e9)
        spin.setSingleStep(0.01)
        spin.setValue(_float(current, 0.0))
        return spin
    box = QComboBox()
    box.setEditable(True)
    box.setCurrentText(current)
    return box


def _tooltip(key: object) -> str:
    parts = [str(getattr(key, "what", ""))]
    low, high = getattr(key, "low", None), getattr(key, "high", None)
    if low is not None and high is not None:
        parts.append(f"The fork refuses anything outside {low} to {high}.")
    allowed = tuple(getattr(key, "allowed", ()) or ())
    if allowed:
        parts.append("Allowed: " + ", ".join(allowed) + ".")
    parts.append(f"Compiled-in default: {getattr(key, 'default', '')}. "
                 "A change needs a console restart.")
    return "\n".join(part for part in parts if part)


def _value_of(widget: QWidget) -> object:
    if isinstance(widget, QComboBox):
        return widget.currentText()
    if isinstance(widget, QSpinBox):
        return widget.value()
    if isinstance(widget, QDoubleSpinBox):
        return widget.value()
    return ""


def _int(text: str, fallback: int) -> int:
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return fallback


def _float(text: str, fallback: float) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return fallback


def exposed_values(config: ConsoleConfig) -> Mapping[str, str]:
    """What the six editable keys are set to now, for a caller that only wants to show
    them. Reads through `in_force`, so a key the file does not state reports the
    console's compiled-in default rather than a blank."""
    return {name: config.in_force(name) for name in EXPOSED}
