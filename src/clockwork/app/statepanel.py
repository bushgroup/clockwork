"""The collapsible state table under a box pane, and the button that refreshes it.

Decision 6 of the window design (lab record, task 50), and the one thing trainees still
opened MIPS_QT6 for: seeing what a box is actually holding. `clockwork.app.boxstate`
decides what the rows say; this draws them, remembers which sections were open, and asks
the worker for a new reading. It holds no `Box` and sends nothing.

**Collapsed by default, and the header still says what matters.** A whole reading is
sixty-odd rows against a pane that is mostly editor, so the panel starts shut and the
header carries the count of what disagrees with the method. A section holding a
disagreement opens itself when a reading arrives, because a mark nobody expands is a
mark nobody reads.

**Refresh is a job, not a call.** The button submits a `ReadState` to the one worker
thread and goes insensitive until it comes back; forty round trips on the UI thread
would freeze the window for the seconds they cost, and a box in table mode answers
them slowly.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..method import BoxMethod
from .boxstate import AGREES, DIFFERS, FOUND, Reading, Row, Section, state_table

__all__ = ["StatePanel"]

MARK_COLUMN = 3
"""Which column carries the mark, so the header and the rows agree on four."""

HEADINGS = ("setting", "reads", "declared", "")

VALUE_COLUMN = 1
"""The column that takes whatever width is left over.

Three panes share the width of a window, so a panel's columns cannot all size to their
contents: one of them has to absorb the slack and elide, or the mark falls off the right
edge of every row and the panel scrolls sideways to say what it was built to say. The
value is the one to elide, because the label names the setting and the mark is three
words -- and the whole row is in the tooltip either way.
"""

VISIBLE_ROWS = 14
"""How many rows the open panel shows before it scrolls.

Enough for a section and its neighbours' headers, and little enough that the editor
above it is still an editor. The panel is under the pane rather than in a window of its
own because what a box holds and what it is about to be sent are read together.
"""


class StatePanel(QWidget):
    """One box's last reading, shut by default, with a button that asks for a new one."""

    refresh_requested = Signal(str)
    """The box's name, when the trainee presses Read state."""

    toggled = Signal(str, bool)
    """`(box name, open)` whenever the panel is opened or shut, for the window to
    remember per pane."""

    def __init__(self, box: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.box = box
        self.reading = Reading()
        self.method: BoxMethod | None = None
        self._expanded: set[str] = set()
        """Which sections are open, by title, kept across the rebuild every re-mark
        costs. Only ever added to: a trainee who shuts one and then types gets it back,
        which is a smaller annoyance than one that shuts while being read."""

        self.toggle = QToolButton()
        self.toggle.setText("Box state")
        self.toggle.setCheckable(True)
        self.toggle.setChecked(False)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setToolTip(
            "What this box is holding, as its getters answered: DC bias setpoints and "
            "monitors, RF heads, ARB modules and the table engine. Every row the "
            "method could have named is marked as declared, differing, or left as "
            "found.")

        # Beside the toggle rather than inside its text: a summary long enough to be
        # worth reading is long enough to take the whole row's width from a
        # `QToolButton`, which sizes itself to its label and leaves the caption wrapping
        # in what is left.
        self.summary = QLabel("")
        summary_font = QFont(self.summary.font())
        summary_font.setBold(True)
        self.summary.setFont(summary_font)

        self.caption = QLabel("not read yet")
        self.caption.setWordWrap(True)
        muted = QColor(self.palette().windowText().color())
        muted.setAlpha(170)
        self.caption.setStyleSheet(
            f"color: {muted.name(QColor.NameFormat.HexArgb)};")

        self.refresh = QPushButton("Read state")
        self.refresh.setToolTip(
            "Ask this box every getter that describes its persistent state: about "
            "forty round trips, or two seconds more the first time, when the box's "
            "own command listing has to be fetched. It writes nothing.")
        self.refresh.clicked.connect(lambda: self.refresh_requested.emit(self.box))

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(HEADINGS))
        self.tree.setHeaderLabels(list(HEADINGS))
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.tree.header()
        header.setStretchLastSection(False)
        for column in range(len(HEADINGS)):
            header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.Stretch if column == VALUE_COLUMN
                else QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setVisible(False)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.tree.setFont(mono)
        # A whole reading is sixty-odd rows and the pane is mostly editor, so the open
        # panel takes a fixed slice of it and scrolls. Measured off the font rather than
        # set in pixels, or the panel is a different fraction of the pane on every
        # machine an instrument PC might be.
        rows = QFontMetrics(mono).height() * VISIBLE_ROWS
        self.tree.setMinimumHeight(rows // 2)
        self.tree.setMaximumHeight(rows)

        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.addWidget(self.toggle)
        heading.addWidget(self.summary, 1)
        heading.addWidget(self.refresh)

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addLayout(heading)
        # On its own line, full width: what the caption says is a sentence about when
        # the reading was taken and how much of it a sequencer overlay superseded, and
        # a sentence squeezed between two buttons is a sentence nobody finishes.
        column.addWidget(self.caption)
        column.addWidget(self.tree)

        self.toggle.toggled.connect(self._toggled)
        self._render()

    # -- what the window sets ------------------------------------------------

    def show_state(self, reading: Reading, method: BoxMethod | None = None) -> None:
        """Draw a new reading, keeping whether the panel itself is open."""
        self.reading = reading
        self.method = method
        self._render()

    def show_method(self, method: BoxMethod | None) -> None:
        """Re-mark the rows against an edited method, without re-reading the box.

        A trainee who adds the `SWFDIR` line the panel told them was missing should see
        the row stop saying "left as found" when they type it, not when they next spend
        five seconds on a readback.
        """
        self.method = method
        self._render()

    def set_open(self, opened: bool) -> None:
        """Open or shut the panel without emitting `toggled` back at the window."""
        blocked = self.toggle.blockSignals(True)
        self.toggle.setChecked(opened)
        self.toggle.blockSignals(blocked)
        self._toggled(opened, quiet=True)

    def set_busy(self, busy: bool) -> None:
        """Grey the button while any job owns the worker, since all of them queue."""
        self.refresh.setEnabled(not busy)

    @property
    def opened(self) -> bool:
        return self.toggle.isChecked()

    # -- drawing -------------------------------------------------------------

    def _toggled(self, opened: bool, quiet: bool = False) -> None:
        self.toggle.setArrowType(
            Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)
        self.tree.setVisible(opened and self.tree.topLevelItemCount() > 0)
        if not quiet:
            self.toggled.emit(self.box, opened)

    def _render(self) -> None:
        table = state_table(self.reading, self.method)
        differing = len(table.differing)
        self.summary.setText(
            "" if not differing else
            f"{differing} setting{'s' if differing != 1 else ''} "
            f"{'disagree' if differing != 1 else 'disagrees'} with the method")
        _colour(self.summary, self._warn_colour())
        self.caption.setText(table.caption)

        # Every re-mark rebuilds the tree, and a re-mark happens on every debounced
        # re-read of the pane above. A trainee typing the line a row asked for must not
        # watch the section they opened to read it shut itself under them, so which
        # sections are open is remembered across the rebuild and only added to.
        self._expanded |= {
            self.tree.topLevelItem(index).text(0)
            for index in range(self.tree.topLevelItemCount())
            if self.tree.topLevelItem(index).isExpanded()
        }
        self.tree.clear()
        for section in table.sections:
            item = self._section_item(section)
            self.tree.addTopLevelItem(item)
            # After the insertion, never before: an item that is not in a tree yet has
            # nothing to expand and nothing to span, and Qt drops both calls without
            # saying so. A section that hides a disagreement is the panel failing at
            # its one job, so one opens itself.
            item.setExpanded(bool(section.differing) or section.title in self._expanded)
            if section.note:
                item.child(0).setFirstColumnSpanned(True)
        self.tree.setVisible(self.opened and self.tree.topLevelItemCount() > 0)

    def _section_item(self, section: Section) -> QTreeWidgetItem:
        item = QTreeWidgetItem([section.title, section.summary, "", ""])
        font = QFont(item.font(0))
        font.setBold(True)
        item.setFont(0, font)
        if section.note:
            item.setToolTip(0, section.note)
            # First, so the sentence is above the rows it is about rather than below
            # the sixteen it explains.
            item.addChild(QTreeWidgetItem([section.note, "", "", ""]))
        for row in section.rows:
            item.addChild(self._row_item(row))
        return item

    def _row_item(self, row: Row) -> QTreeWidgetItem:
        value = row.value
        if row.note:
            value = f"{value}   ({row.note})"
        item = QTreeWidgetItem([row.label, value, row.declared, row.mark])
        # On every column, because the value column elides: a row too narrow to read is
        # a row whose tooltip has to carry it, and the getter is what a trainee needs
        # beside a MIPS_QT6 screenshot or the wire format.
        whole = "   ".join(part for part in (row.label, value, row.declared, row.mark)
                           if part)
        if row.getter:
            whole += f"   (read with {row.getter})"
        for column in range(len(HEADINGS)):
            item.setToolTip(column, whole)
        if row.mark == DIFFERS:
            _colour(item, self._warn_colour())
        elif row.mark in (FOUND, AGREES):
            muted = QColor(self.palette().windowText().color())
            muted.setAlpha(150)
            item.setForeground(MARK_COLUMN, QBrush(muted))
        return item

    def _warn_colour(self) -> QColor:
        light = self.palette().window().color().lightness() > 128
        return QColor("#9c4221") if light else QColor("#f6ad55")


def _colour(target: QTreeWidgetItem | QLabel, colour: QColor | None) -> None:
    if isinstance(target, QLabel):
        target.setStyleSheet(
            "" if colour is None
            else f"color: {colour.name(QColor.NameFormat.HexRgb)};")
        return
    brush = QBrush(colour)
    for column in range(len(HEADINGS)):
        target.setForeground(column, brush)
