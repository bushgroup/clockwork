"""One box's pane: the strings a trainee used to paste, with the phase in the margin.

This is decision 2 of `notes/window.md` made into a widget. The pane is plain text, one
string per line, exactly what went into MIPS_QT6's terminal -- and beside every line,
in a margin the trainee cannot type in, the phase clockwork read it as. Nothing is
rewritten as they type: the margin is a reading of the text, and the text is the
document.

**The margin is the whole of the interpretation clockwork admits to.** `classify` puts a
line in one of five phases from its command word, `parse_pane` settles the groups, and
what those two decided is shown rather than applied silently. A line the classifier
cannot place is tagged `unplaced` and drawn in the warning colour, because a sentence
left in a pane -- and both golden trainee files end in one -- must not reach a box, and
a trainee has to be able to see which of their lines will and which will not. The remedy
is a `# clockwork: <phase>` tag and never a refusal.

**Parsing is debounced, not deferred.** Re-parsing on every keystroke is cheap (both
golden panes are tens of lines) but it makes the margin flicker through nonsense while a
command word is half typed. A short pause after the last key is enough to make the
margin look like it is reading rather than guessing.

The pane holds no `Box` and never sends anything. What it produces is a `PaneResult`,
which is what the window assembles a `Method` out of.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QTextOption
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..method import BoxMethod
from ..method.text import PaneResult, parse_pane
from .boxstate import Reading
from .statepanel import StatePanel

__all__ = ["BoxPane", "PaneEditor"]

PARSE_DELAY_MS = 150
"""How long the margin waits after the last keystroke before re-reading the pane.

Long enough that a command word being typed does not flash through `unplaced` on its
way to `setup`; short enough that a trainee who has stopped typing sees the tag
appear rather than waits for it.
"""

MARGIN_TAGS = {
    "setup": "setup",
    "load": "load",
    "arm": "arm",
    "start": "start",
    "reset": "reset",
    "comment": "note",
    "directive": "tag",
    "blank": "",
    "unplaced": "not sent",
}
"""What each of `clockwork.method.text.TAGS` is called in the margin.

Shortened where the phase name is not the useful word: a comment is a `note` to the
person reading it, a directive is the `tag` they wrote, and `unplaced` is rendered as
`not sent`, which is the consequence rather than the category and is the thing a
trainee needs to notice.
"""


class _Margin(QWidget):
    """The strip down the left of the editor that shows each line's tag.

    A sibling widget painted from the editor's own block geometry, the way a line
    number margin is done, rather than text inserted into the document: the document
    is the trainee's and must round-trip to TOML unchanged, so nothing clockwork
    decides may ever live in it.
    """

    def __init__(self, editor: PaneEditor) -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802 -- Qt's name
        return QSize(self._editor.margin_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802, ANN001 -- Qt's name
        self._editor.paint_margin(self, event.rect())


class PaneEditor(QPlainTextEdit):
    """A plain-text editor with a phase margin, and the parse that fills it."""

    parsed = Signal(object)
    """The `PaneResult` for the current text, after each debounced re-read."""

    def __init__(self, box: str = "box", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.box = box
        self.result: PaneResult = parse_pane("", box)

        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(max(9, self.font().pointSize()))
        self.setFont(font)
        self.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        self._margin = _Margin(self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(PARSE_DELAY_MS)
        self._timer.timeout.connect(self.reparse)

        self.blockCountChanged.connect(lambda _: self._fit_margin())
        self.updateRequest.connect(self._scroll_margin)
        self.textChanged.connect(self._timer.start)
        self._fit_margin()

    # -- the parse -----------------------------------------------------------

    def reparse(self) -> PaneResult:
        """Read the pane now, whatever the timer was going to do."""
        self._timer.stop()
        self.result = parse_pane(self.toPlainText(), self.box)
        self._margin.update()
        self.parsed.emit(self.result)
        return self.result

    def set_text(self, text: str) -> None:
        """Replace the pane's text and re-read it at once, without a debounce.

        For text the window put there -- a method being opened, a trainee file being
        split -- where there is no typing to wait for and the caller wants the result
        before it returns.
        """
        self.setPlainText(text)
        self.reparse()

    # -- the margin ----------------------------------------------------------

    def margin_width(self) -> int:
        widest = max(QFontMetrics(self.font()).horizontalAdvance(text)
                     for text in MARGIN_TAGS.values())
        return widest + 14

    def _fit_margin(self) -> None:
        self.setViewportMargins(self.margin_width(), 0, 0, 0)

    def _scroll_margin(self, rect: QRect, dy: int) -> None:
        if dy:
            self._margin.scroll(0, dy)
        else:
            self._margin.update(0, rect.y(), self._margin.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._fit_margin()

    def resizeEvent(self, event) -> None:  # noqa: N802, ANN001 -- Qt's name
        super().resizeEvent(event)
        area = self.contentsRect()
        self._margin.setGeometry(
            QRect(area.left(), area.top(), self.margin_width(), area.height()))

    def paint_margin(self, margin: QWidget, rect: QRect) -> None:
        """Draw the tag beside every visible line.

        Walks the editor's own blocks so that the tag stays with its line through a
        scroll, a resize and a font change; the tags come from the last parse, which
        may be one debounce behind the text, and a line past the end of that parse is
        simply blank rather than guessed at.
        """
        painter = QPainter(margin)
        painter.fillRect(rect, self.palette().window())
        tags = {line.number: line.tag for line in self.result.lines}
        muted = QColor(self.palette().windowText().color())
        muted.setAlpha(150)
        warn = QColor("#c05621") if self.palette().window().color().lightness() > 128 \
            else QColor("#f6ad55")

        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block).translated(
            self.contentOffset()).top())
        height = round(self.blockBoundingRect(block).height())
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, self.font().pointSizeF() - 1.0))
        painter.setFont(font)
        while block.isValid() and top <= rect.bottom():
            if block.isVisible() and top + height >= rect.top():
                tag = MARGIN_TAGS.get(tags.get(block.blockNumber() + 1, "blank"), "")
                if tag:
                    painter.setPen(warn if tag == MARGIN_TAGS["unplaced"] else muted)
                    painter.drawText(
                        0, top, margin.width() - 7, height,
                        int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                        tag)
            block = block.next()
            top += height
            height = round(self.blockBoundingRect(block).height())
        painter.end()


class BoxPane(QWidget):
    """One box: its identity above, its strings in the middle, its state below.

    The header is the box as the *rack* reports it, not as the method describes it --
    the `GNAME` it answered to, the port it answered on and its firmware -- because a
    method's port map is a hint and the discovery is the answer. A box the method names
    that nothing answered for still gets a pane, marked "off or absent"; its text is
    still editable and still saves, because a trainee editing a method for tomorrow
    should not need the instrument switched on.

    Under the editor is the state panel (task 51): what the box last answered, marked
    against what this pane's strings declare. Shut by default, so a pane is an editor
    until a trainee asks it to be more.
    """

    parsed = Signal(str, object)
    """`(box name, PaneResult)` after each re-read."""

    read_state_requested = Signal(str)
    """The box's name, when its state panel asks for a fresh reading."""

    def __init__(self, box: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.box = box

        self.title = QLabel(box)
        title_font = QFont(self.title.font())
        title_font.setBold(True)
        self.title.setFont(title_font)
        self.status = QLabel("")
        self.status.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Preferred)
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight
                                 | Qt.AlignmentFlag.AlignVCenter)

        self.editor = PaneEditor(box, self)
        self.editor.parsed.connect(lambda result: self.parsed.emit(self.box, result))

        self.state = StatePanel(box, self)
        self.state.refresh_requested.connect(self.read_state_requested)

        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.addWidget(self.title)
        heading.addWidget(self.status)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addLayout(heading)
        layout.addWidget(self.editor, 1)
        layout.addWidget(self.state)

    # -- what the window sets ------------------------------------------------

    def describe(self, port: str = "", version: str = "", answered: bool = False) -> None:
        """The header line, from what the box answered and not from the method.

        "off or absent" rather than a port for a box that did not answer: all four
        MIPS-class ports enumerate with every box powered off, so naming a port for a
        silent box would state as fact the one thing port presence cannot establish
        (lab record, task 37).
        """
        if answered:
            parts = [part for part in (port, version) if part]
            self.status.setText("  ".join(parts) or "answered")
        else:
            self.status.setText("off or absent")
        muted = QColor(self.palette().windowText().color())
        muted.setAlpha(255 if answered else 150)
        self.status.setStyleSheet(f"color: {muted.name(QColor.NameFormat.HexArgb)};")

    @property
    def result(self) -> PaneResult:
        return self.editor.result

    def text(self) -> str:
        return self.editor.toPlainText()

    def set_text(self, text: str) -> None:
        self.editor.set_text(text)

    def reparse(self) -> PaneResult:
        return self.editor.reparse()

    # -- the state panel -----------------------------------------------------

    def show_state(self, reading: Reading, method: BoxMethod | None = None) -> None:
        """Hand the panel a new reading of this box."""
        self.state.show_state(reading, method)

    def show_method(self, method: BoxMethod | None) -> None:
        """Re-mark the panel's rows against the method as the pane now reads."""
        self.state.show_method(method)
