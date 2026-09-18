"""The table an unattended series is written in, and the buttons that edit it.

The widget half of the run queue (lab record, task 53). `clockwork.app.runqueue` holds
the rows and the rules; this draws them, lets a trainee edit the ones that have not
started, and emits what it cannot do itself. It holds no worker and submits no job: the
window is the sequencer.

**Editable while running, and the running row is not.** The point of the queue is that a
trainee can add tomorrow's samples to it at ten o'clock while the eight o'clock row is
still acquiring, so every waiting row's note, replicate count and two flags stay
editable throughout. The row in flight does not: its values were read when it started
and were on the wire before the edit, so an editable cell would be a promise the
sequencer cannot keep.

**The whole table is rebuilt on every change.** A dozen rows of seven columns is nothing
to redraw, and the alternative -- keeping widget state in step with a model that a
background job mutates twice a row -- is where the bugs would be. The one thing kept
across a rebuild is the selection, by row number, because a trainee reordering a row
watches it move.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .runqueue import (
    DONE,
    FAILED,
    RUNNING,
    SKIPPED,
    STOPPED,
    QueueRow,
    RunQueue,
)

__all__ = ["HEADINGS", "QueuePanel"]

HEADINGS = ("", "state", "method", "sample or conditions", "reps", "setup",
            "go on if it fails", "outcome")

STATE_COLUMN = 1
METHOD_COLUMN = 2
CONDITIONS_COLUMN = 3
REPLICATES_COLUMN = 4
SETUP_COLUMN = 5
GO_ON_COLUMN = 6
OUTCOME_COLUMN = 7

EDITABLE = (CONDITIONS_COLUMN, REPLICATES_COLUMN)
"""The two columns a trainee types into. The method is a document, chosen with a
picker rather than typed, because a path typed wrong is a row that fails at three in
the morning; the two flags are check boxes; the rest is what the queue reports."""

STRETCH_COLUMN = OUTCOME_COLUMN
"""The column that absorbs the width left over. The outcome is the longest thing in
the table -- two stems, a stop reason and a silence count -- and the one whose tail a
trainee can afford to read in the tooltip."""


class QueuePanel(QWidget):
    """The rows of an unattended series, with add, remove, reorder and reset."""

    start_requested = Signal()
    stop_requested = Signal()
    add_requested = Signal()
    """Pick one or more method documents. The dialog is the window's, as every other
    file dialog in this application is."""
    add_open_requested = Signal()
    """Add a row for the method the panes currently hold."""
    changed = Signal()
    """A row was edited, added, removed or reordered. The window re-reads what may be
    pressed: a queue with nothing waiting in it cannot be started."""

    def __init__(self, queue: RunQueue, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.queue = queue
        self._busy = False
        """Whether the worker has a job of its own in flight. A queue cannot be started
        on top of a send or an acquisition a trainee pressed by hand: they are the same
        worker, and the queue's first act is to arm the boxes."""
        self._rebuilding = False
        """`itemChanged` fires while the tree is being filled, and a row written back
        from a half-built item would overwrite the model with the widget's defaults."""

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(HEADINGS))
        self.tree.setHeaderLabels(list(HEADINGS))
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed)
        header = self.tree.header()
        header.setStretchLastSection(False)
        for column in range(len(HEADINGS)):
            header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.Stretch if column == STRETCH_COLUMN
                else QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemSelectionChanged.connect(lambda: self._refresh_buttons())

        self.caption = QLabel("")
        self.caption.setWordWrap(True)

        self.add_button = QPushButton("Add method…")
        self.add_open_button = QPushButton("Add the open method")
        self.remove_button = QPushButton("Remove")
        self.up_button = QPushButton("Up")
        self.down_button = QPushButton("Down")
        self.reset_button = QPushButton("Reset")
        self.start_button = QPushButton("Start the queue")
        self.stop_button = QPushButton("Stop")
        for button, tip in (
            (self.add_button, "Pick one or more method documents. Each becomes a row "
                              "that is read off disk at the moment it starts, so a "
                              "method edited before the row runs is the one that runs."),
            (self.add_open_button, "A row for the method the panes are holding, with "
                                   "the conditions note and replicate count beside "
                                   "them. The method has to have been saved: a row "
                                   "names a document, not an editor."),
            (self.remove_button, "Take the selected rows out. The row in flight stays: "
                                 "its strings are already on the wire."),
            (self.up_button, "Move the selected row earlier. It cannot pass the row "
                             "that is running."),
            (self.down_button, "Move the selected row later."),
            (self.reset_button, "Put the selected rows back to waiting and clear what "
                                "the last attempt left on them, so Start runs them "
                                "again."),
            (self.start_button, "Run every waiting row in order on the one worker: the "
                                "row's method, sent, then its replicates, then the "
                                "next row. A failed row ends the series unless it says "
                                "to go on."),
            (self.stop_button, "End the series after the current repetition and its "
                               "fold. The row in flight keeps the files it has "
                               "written; everything after it is skipped."),
        ):
            button.setToolTip(tip)
        self.add_button.clicked.connect(lambda: self.add_requested.emit())
        self.add_open_button.clicked.connect(lambda: self.add_open_requested.emit())
        self.remove_button.clicked.connect(self._remove)
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button.clicked.connect(lambda: self._move(1))
        self.reset_button.clicked.connect(self._reset)
        self.start_button.clicked.connect(lambda: self.start_requested.emit())
        self.stop_button.clicked.connect(lambda: self.stop_requested.emit())

        buttons = QWidget()
        row = QHBoxLayout(buttons)
        row.setContentsMargins(0, 0, 0, 0)
        for button in (self.add_button, self.add_open_button, self.remove_button,
                       self.up_button, self.down_button, self.reset_button):
            row.addWidget(button)
        row.addStretch(1)
        row.addWidget(self.start_button)
        row.addWidget(self.stop_button)

        column = QVBoxLayout(self)
        column.setContentsMargins(4, 4, 4, 4)
        column.addWidget(self.tree, 1)
        column.addWidget(self.caption)
        column.addWidget(buttons)
        self.refresh()

    # -- drawing -------------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the table from the queue, keeping the selection by row number."""
        selected = self.selection()
        self._rebuilding = True
        try:
            self.tree.clear()
            for index, row in enumerate(self.queue.rows):
                self.tree.addTopLevelItem(self._item(index, row))
        finally:
            self._rebuilding = False
        for index in selected:
            if index < self.tree.topLevelItemCount():
                self.tree.topLevelItem(index).setSelected(True)
        self._refresh_caption()
        self._refresh_buttons()

    def _item(self, index: int, row: QueueRow) -> QTreeWidgetItem:
        item = QTreeWidgetItem(_reported(index, row))
        item.setToolTip(METHOD_COLUMN, row.method_path)
        item.setToolTip(OUTCOME_COLUMN, row.outcome)
        item.setTextAlignment(REPLICATES_COLUMN,
                              Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)
        item.setCheckState(SETUP_COLUMN, _checked(row.setup))
        item.setCheckState(GO_ON_COLUMN, _checked(row.go_on))

        # A row that has run, and the row in flight, are a record rather than a form:
        # editing either would describe an experiment that has already happened. The
        # flags are set after the check states, because `setCheckState` turns
        # `ItemIsUserCheckable` on by itself.
        settable = (Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsUserCheckable)
        flags = item.flags() & ~settable
        if row.state not in (RUNNING, DONE, FAILED, STOPPED):
            flags |= settable
        item.setFlags(flags)
        if row.state == RUNNING:
            _bold(item)
        colour = self._colour(row.state)
        if colour is not None:
            item.setForeground(STATE_COLUMN, QBrush(colour))
        return item

    def _colour(self, state: str) -> QColor | None:
        """Only the two states worth looking for in the morning are coloured.

        A queue of twenty rows that all went green is read by not reading it; what a
        trainee is looking for is the row that failed and the row that was stopped,
        which is the same argument the state panel makes for colouring `differs` alone
        (lab record, task 51).
        """
        if state not in (FAILED, STOPPED, SKIPPED):
            return None
        light = self.palette().window().color().lightness() > 128
        if state == FAILED:
            return QColor("#9b2c2c") if light else QColor("#fc8181")
        if state == STOPPED:
            return QColor("#9c4221") if light else QColor("#f6ad55")
        muted = QColor(self.palette().windowText().color())
        muted.setAlpha(140)
        return muted

    def _refresh_caption(self) -> None:
        if not self.queue.rows:
            self.caption.setText(
                "Nothing queued. Add methods here to run a series of samples or "
                "conditions unattended: each row is sent and acquired in turn on the "
                "one worker, and Stop ends the series after the current repetition.")
            return
        if self.queue.running:
            current = self.queue.current
            where = f"row {self.queue.index + 1} of {len(self.queue.rows)}"
            name = current.name if current is not None else "between rows"
            self.caption.setText(f"running {where}: {name}")
        else:
            self.caption.setText(
                f"{len(self.queue.rows)} row(s), {self.queue.waiting} waiting")

    def _refresh_buttons(self) -> None:
        running = self.queue.running
        selected = self.selection()
        editable = [index for index in selected if index != self.queue.index]
        self.remove_button.setEnabled(bool(editable))
        self.reset_button.setEnabled(bool(editable))
        self.up_button.setEnabled(len(editable) == 1)
        self.down_button.setEnabled(len(editable) == 1)
        self.start_button.setEnabled(
            not running and not self._busy and self.queue.waiting > 0)
        self.stop_button.setEnabled(running)

    def set_busy(self, busy: bool) -> None:
        """What the window knows and this panel does not: the worker has a job."""
        self._busy = busy
        self._refresh_buttons()

    def selection(self) -> list[int]:
        return sorted(self.tree.indexOfTopLevelItem(item)
                      for item in self.tree.selectedItems())

    # -- editing -------------------------------------------------------------

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """Write one edited cell back into its row, and put back what was not editable.

        The cell is normalised in place rather than by rebuilding the table: `refresh`
        clears the tree, and clearing it from inside `itemChanged` deletes the item that
        is emitting the signal. `ItemIsEditable` is per item and not per column, so a
        double-click on the outcome opens an editor over a cell the queue owns; what
        that cell says is put back here.
        """
        if self._rebuilding:
            return
        index = self.tree.indexOfTopLevelItem(item)
        if not 0 <= index < len(self.queue.rows) or index == self.queue.index:
            return
        row = self.queue.rows[index]
        self._rebuilding = True
        try:
            if column == CONDITIONS_COLUMN:
                row.conditions = item.text(column).strip()
                item.setText(column, row.conditions)
            elif column == REPLICATES_COLUMN:
                row.replicates = _as_count(item.text(column), row.replicates)
                item.setText(column, str(row.replicates))
            elif column == SETUP_COLUMN:
                row.setup = item.checkState(column) == Qt.CheckState.Checked
            elif column == GO_ON_COLUMN:
                row.go_on = item.checkState(column) == Qt.CheckState.Checked
            else:
                item.setText(column, _reported(index, row)[column])
        finally:
            self._rebuilding = False
        self.changed.emit()

    def _remove(self) -> None:
        for index in reversed(self.selection()):
            self.queue.remove(index)
        self.refresh()
        self.changed.emit()

    def _move(self, delta: int) -> None:
        selected = self.selection()
        if len(selected) != 1:
            return
        where = self.queue.move(selected[0], delta)
        self.refresh()
        self.tree.clearSelection()
        if where < self.tree.topLevelItemCount():
            self.tree.topLevelItem(where).setSelected(True)
        self.changed.emit()

    def _reset(self) -> None:
        for index in self.selection():
            if index != self.queue.index:
                self.queue.rows[index].reset()
        self.refresh()
        self.changed.emit()


# -- small helpers -------------------------------------------------------------------


def _reported(index: int, row: QueueRow) -> list[str]:
    """One row's eight cells of text, in `HEADINGS` order.

    One function rather than a literal in `_item`, because `_item_changed` has to be
    able to put a cell back exactly as the table drew it.
    """
    cells = [""] * len(HEADINGS)
    cells[0] = str(index + 1)
    cells[STATE_COLUMN] = (row.step or row.state) if row.state == RUNNING else row.state
    cells[METHOD_COLUMN] = row.name
    cells[CONDITIONS_COLUMN] = row.conditions
    cells[REPLICATES_COLUMN] = str(row.replicates)
    cells[OUTCOME_COLUMN] = row.outcome
    return cells


def _checked(value: bool) -> Qt.CheckState:
    return Qt.CheckState.Checked if value else Qt.CheckState.Unchecked


def _bold(item: QTreeWidgetItem) -> None:
    font = QFont(item.font(STATE_COLUMN))
    font.setBold(True)
    for column in range(item.columnCount()):
        item.setFont(column, font)


def _as_count(text: str, fallback: int) -> int:
    """A replicate count out of whatever was typed, never zero and never a crash.

    A cell a trainee cleared and tabbed out of is not an instruction to acquire nothing;
    it is a cell they will come back to, so the row keeps the count it had.
    """
    try:
        count = int(str(text).strip())
    except (TypeError, ValueError):
        return fallback
    return max(1, min(count, 999))
