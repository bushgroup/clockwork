"""The method library browser, and the two diff dialogs it opens into.

The Qt half of task 54, over `clockwork.app.methodlib`: a table of what a directory
setting finds, "Open into panes" for the panel this replaces MIPS_QT6's paste box with,
and two comparisons a trainee reaches from the same window -- today's method against the
one that worked, and today's method against what the boxes are holding right now.

Nothing here decides what differs. `methodlib.method_diff` and `methodlib.instrument_diff`
do that; every widget below only draws what they hand back.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..method import Method
from .boxstate import Reading
from .methodlib import (
    BoxDiff,
    FieldDiff,
    LibraryEntry,
    LineDiff,
    MethodDiff,
    method_diff,
    open_entry,
    scan_library,
)
from .statepanel import StatePanel

__all__ = ["InstrumentDiffDialog", "LibraryDialog", "MethodDiffDialog"]

_COLUMNS = ("Name", "Hash", "Date", "Description")


class LibraryDialog(QDialog):
    """A directory of methods, and the way into both diffs.

    Modal, like "Open method" and the console settings dialog: a trainee picking a
    method to open or compare is not doing anything else with the window in the
    meantime, and a non-modal browser left open behind the panes would be a second copy
    of "what method is this" for the window to keep in sync with the one it is running.
    """

    def __init__(
        self, directory: str, readings: Mapping[str, Reading],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Method library")
        self.resize(720, 420)
        self._readings = readings
        self.entries: list[LibraryEntry] = []
        self.chosen_path = ""
        """Set and the dialog closed accepted where "Open into panes" was pressed."""

        self.directory_field = QLineEdit(directory)
        self.directory_field.setToolTip(
            "Scanned recursively for *.toml documents, so a golden experiment's method "
            "in a directory of its own is found the same as a flat folder of saves.")
        browse = QPushButton("…")
        browse.setMaximumWidth(32)
        browse.clicked.connect(self._browse)
        rescan = QPushButton("Rescan")
        rescan.clicked.connect(self.rescan)
        directory_row = QHBoxLayout()
        directory_row.addWidget(self.directory_field, 1)
        directory_row.addWidget(browse)
        directory_row.addWidget(rescan)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemDoubleClicked.connect(lambda *_: self._open())

        self.open_button = QPushButton("Open into panes")
        self.open_button.setToolTip(
            "Put this document into the panes and the form, exactly as File > Open "
            "method does.")
        self.open_button.clicked.connect(self._open)
        self.diff_methods_button = QPushButton("Diff two methods…")
        self.diff_methods_button.setToolTip(
            "Compare the two selected documents: per box and phase, the strings added, "
            "removed and changed; the acquisition settings and the declared tables side "
            "by side.")
        self.diff_methods_button.clicked.connect(self._diff_methods)
        self.diff_instrument_button = QPushButton("Diff against the instrument…")
        self.diff_instrument_button.setToolTip(
            "Compare the selected document's setup phase against the boxes' last "
            "reading in this window: every setter whose getter is known, beside what "
            "the box currently reports.")
        self.diff_instrument_button.clicked.connect(self._diff_instrument)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self.open_button)
        buttons_row.addWidget(self.diff_methods_button)
        buttons_row.addWidget(self.diff_instrument_button)
        buttons_row.addStretch(1)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(directory_row)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons_row)
        layout.addWidget(close)

        self.rescan()

    def directory(self) -> str:
        return self.directory_field.text().strip()

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Method library", self.directory())
        if path:
            self.directory_field.setText(path)
            self.rescan()

    def rescan(self) -> None:
        self.entries = scan_library(self.directory())
        self.table.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            values = (
                entry.name or os.path.basename(entry.path),
                entry.hash,
                str(entry.created) if entry.created else "",
                entry.problem or entry.description,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(entry.path)
                if entry.problem:
                    item.setForeground(QBrush(QColor("#9c4221")))
                self.table.setItem(row, column, item)
        self._update_buttons()

    def _selected(self) -> list[LibraryEntry]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        return [self.entries[row] for row in rows if self.entries[row].ok]

    def _update_buttons(self) -> None:
        selected = self._selected()
        self.open_button.setEnabled(len(selected) == 1)
        self.diff_methods_button.setEnabled(len(selected) == 2)
        self.diff_instrument_button.setEnabled(len(selected) == 1)

    def _open(self) -> None:
        selected = self._selected()
        if len(selected) != 1:
            return
        self.chosen_path = selected[0].path
        self.accept()

    def _diff_methods(self) -> None:
        selected = self._selected()
        if len(selected) != 2:
            return
        try:
            a, b = (open_entry(entry) for entry in selected)
        except Exception as exc:  # noqa: BLE001 -- a document that changed on disk since the scan
            self._complain(str(exc))
            return
        dialog = MethodDiffDialog(method_diff(a, b), self)
        dialog.exec()

    def _diff_instrument(self) -> None:
        selected = self._selected()
        if len(selected) != 1:
            return
        try:
            picked = open_entry(selected[0])
        except Exception as exc:  # noqa: BLE001
            self._complain(str(exc))
            return
        dialog = InstrumentDiffDialog(picked, self._readings, self)
        dialog.exec()

    def _complain(self, message: str) -> None:
        QMessageBox.warning(self, "That document could not be opened", message)


# --- method-to-method -----------------------------------------------------------------

_CHANGED = "changed"
_ADDED = "added"
_REMOVED = "removed"

_TREE_COLUMNS = ("", "A", "B")


class MethodDiffDialog(QDialog):
    """One `MethodDiff`, as a tree: metadata and acquisition first, then the two
    method-level sequences, then every box's phases and declared tables.

    A section with nothing differing is collapsed; a section holding a difference opens
    itself, the same rule the state panel uses for the same reason (task 51) -- a
    trainee comparing two methods is looking for what moved, and a tree that opened
    every phase of every box for two methods that agree on all but one line would bury
    it.
    """

    def __init__(self, diff: MethodDiff, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{diff.name_a or '(untitled)'} vs {diff.name_b or '(untitled)'}")
        self.resize(760, 560)

        header = QLabel(f"<b>A:</b> {diff.name_a or '(untitled)'}"
                        f"&nbsp;&nbsp;&nbsp;<b>B:</b> {diff.name_b or '(untitled)'}")
        if diff.identical:
            header.setText(header.text() + "&nbsp;&nbsp;&mdash; identical")

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(_TREE_COLUMNS))
        self.tree.setHeaderLabels(list(_TREE_COLUMNS))
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header_view = self.tree.header()
        header_view.setStretchLastSection(False)
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.tree.setFont(mono)

        metadata_item = _section("Metadata", diff.metadata, ())
        self.tree.addTopLevelItem(metadata_item)
        metadata_item.setExpanded(any(f.differs for f in diff.metadata))

        acquisition_item = _section("Acquisition", diff.acquisition, ())
        self.tree.addTopLevelItem(acquisition_item)
        acquisition_item.setExpanded(any(f.differs for f in diff.acquisition))

        for title, rows in (("Start", diff.start), ("Reset", diff.reset)):
            if not rows:
                continue
            item = _section(title, (), rows)
            self.tree.addTopLevelItem(item)
            item.setExpanded(any(row.kind != "equal" for row in rows))

        for box in diff.boxes:
            self.tree.addTopLevelItem(_box_item(box))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(self.tree, 1)
        layout.addWidget(buttons)


def _section(
    title: str, fields: tuple[FieldDiff, ...], lines: tuple[LineDiff, ...],
) -> QTreeWidgetItem:
    changed = sum(1 for f in fields if f.differs) + sum(1 for r in lines if r.kind != "equal")
    label = title if not changed else f"{title} ({changed} changed)"
    item = QTreeWidgetItem([label, "", ""])
    _bold(item)
    for field_ in fields:
        item.addChild(_field_item(field_))
    for row in lines:
        item.addChild(_line_item(row))
    return item


def _box_item(box: BoxDiff) -> QTreeWidgetItem:
    title = box.name
    if not box.in_a:
        title += "  (only in B)"
    elif not box.in_b:
        title += "  (only in A)"
    changes = sum(
        1 for rows in box.phases.values() for row in rows if row.kind != "equal"
    ) + sum(1 for f in box.declared if f.differs)
    if changes:
        title += f"  ({changes} changed)"
    item = QTreeWidgetItem([title, "", ""])
    _bold(item)
    for phase in ("setup", "load", "arm"):
        rows = box.phases.get(phase, ())
        if not rows:
            continue
        child = _section(phase, (), rows)
        item.addChild(child)
        child.setExpanded(any(row.kind != "equal" for row in rows))
    if box.declared:
        child = _section("declared", box.declared, ())
        item.addChild(child)
        child.setExpanded(any(f.differs for f in box.declared))
    item.setExpanded(bool(changes))
    return item


def _field_item(field_: FieldDiff) -> QTreeWidgetItem:
    item = QTreeWidgetItem([field_.label, field_.a, field_.b])
    if field_.differs:
        _colour(item, _changed_colour())
    return item


def _line_item(row: LineDiff) -> QTreeWidgetItem:
    label = "" if row.kind == "equal" else row.kind
    item = QTreeWidgetItem([label, row.a, row.b])
    if row.kind == _CHANGED:
        _colour(item, _changed_colour())
    elif row.kind == _ADDED:
        _colour(item, _added_colour())
    elif row.kind == _REMOVED:
        _colour(item, _removed_colour())
    return item


def _bold(item: QTreeWidgetItem) -> None:
    font = QFont(item.font(0))
    font.setBold(True)
    item.setFont(0, font)


def _colour(item: QTreeWidgetItem, colour: QColor) -> None:
    brush = QBrush(colour)
    for column in range(len(_TREE_COLUMNS)):
        item.setForeground(column, brush)


def _changed_colour() -> QColor:
    return QColor("#9c4221")


def _added_colour() -> QColor:
    return QColor("#2f6f3e")


def _removed_colour() -> QColor:
    return QColor("#8a8a8a")


# --- method-to-instrument --------------------------------------------------------------


class InstrumentDiffDialog(QDialog):
    """One method's boxes, each a `StatePanel` marked against its last reading.

    Opened already expanded, unlike the panes' own collapsed-by-default panels: a
    trainee who asked for this comparison asked to see it, and there is no editor above
    it competing for the screen. The refresh button is hidden -- this dialog has no
    worker of its own to send a job to, so a fresher reading is taken from the box's own
    pane and this dialog reopened.
    """

    def __init__(
        self, method: Method, readings: Mapping[str, Reading],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{method.metadata.name or '(untitled)'} against the instrument")
        self.resize(640, 560)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        column = QVBoxLayout(inner)
        # `StatePanel` renders through `boxstate.state_table` itself, the same function
        # `methodlib.instrument_diff` is a thin wrapper over; the two never disagree
        # because there is only the one function.
        for box in method.boxes:
            panel = StatePanel(box.name)
            panel.refresh.setVisible(False)
            panel.show_state(readings.get(box.name, Reading()), box)
            panel.set_open(True)
            column.addWidget(panel)
        column.addStretch(1)
        scroll.setWidget(inner)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)
