"""The Day tab: the day's requests and jobs in order, as the records word them.

The widget over `daylist` (the window revision's D2, D3 and D5; lab record, task 169).
One row per request opened on the day in the output directory and per line of the day
record, then the daemon's jobs still running or queued; only failed, stopped and held
are coloured. It reads files and draws them; it computes nothing, and it sends nothing.
"""

from __future__ import annotations

import datetime as _dt

from PySide6.QtCore import QDate, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .daylist import DayLine, day_lines

__all__ = ["DAY_HEADINGS", "DayPanel"]

DAY_HEADINGS = ("at", "route", "who", "purpose", "files", "how it ended",
                "routine's result", "verdict")

_COLOURS = {"failed": QColor("#c53030"), "stopped": QColor("#b7791f"),
            "held": QColor("#b7791f")}


class DayPanel(QWidget):
    """The day's list over a date picker; `show_day` redraws it."""

    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.directory = ""
        self.lines: list[DayLine] = []
        self.date = QDateEdit(QDate.currentDate())
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        self.date.setToolTip("The day shown: requests opened on it in the output "
                             "directory, and the lines its day record holds from it.")
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip("Read the day folder again.")
        self.caption = QLabel("")
        self.caption.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(DAY_HEADINGS))
        self.tree.setHeaderLabels(list(DAY_HEADINGS))
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setColumnWidth(0, 64)
        self.tree.setColumnWidth(1, 80)
        self.tree.setColumnWidth(2, 60)
        self.tree.setColumnWidth(3, 240)
        self.tree.setColumnWidth(4, 160)
        self.tree.setColumnWidth(5, 260)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(QLabel("Day"))
        top.addWidget(self.date)
        top.addWidget(self.refresh_button)
        top.addWidget(self.caption, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(top)
        layout.addWidget(self.tree, 1)

        self.refresh_button.clicked.connect(lambda: self.refresh_requested.emit())
        self.date.dateChanged.connect(lambda _: self.refresh_requested.emit())

    def day(self) -> _dt.date:
        chosen = self.date.date()
        return _dt.date(chosen.year(), chosen.month(), chosen.day())

    def show_day(self, directory: str, live: list[tuple[str, str, str]] = ()) -> None:
        """Read `directory` for the day picked, and add the daemon's `live` jobs as
        `(state, label, origin)`."""
        self.directory = directory
        self.lines = day_lines(directory, self.day())
        self.tree.clear()
        for line in self.lines:
            item = QTreeWidgetItem([
                line.time, line.route, line.person, line.purpose, ", ".join(line.files),
                line.ended, line.result, line.verdict])
            if line.path:
                item.setToolTip(3, line.path)
            colour = _COLOURS.get(line.state)
            if colour is not None:
                for column in range(len(DAY_HEADINGS)):
                    item.setForeground(column, QBrush(colour))
            self.tree.addTopLevelItem(item)
        for state, label, origin in live:
            self.tree.addTopLevelItem(QTreeWidgetItem(
                ["now", "live", origin, label, "", state, "", ""]))
        requests = sum(1 for line in self.lines if line.route != "day record")
        self.caption.setText(
            f"{requests} request(s) and {len(self.lines) - requests} day-record line(s) "
            f"in {directory}" if directory else "no output directory is set")
