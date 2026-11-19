"""The Template tab: a template's knobs turned in the window, and run as they stand.

The window revision's E4 and R10 (lab record, task 169; Matt, 2026-11-18), replacing task
164's modal builder. A person picks a template, sets each knob and fills the labels, and
sees the method that would be sent and what differs from the template's defaults. Each
knob is shown against two ranges: the template's own, which its field cannot leave, and
the agent's, the limits the daemon's sitting carries, which a person is never held to. A
value outside the agent's range is marked, the run goes ahead, and its run record says so
(`outside_limits`, written by the owner).

**Acquire runs the render with no file on disk**: the job carries the template text, the
knob values and the labels, and the owner renders it again and stamps it, as it does a run
Claude acquires from a template. "Save as a method" writes the render as a method file
carrying its `[rendered]` table, byte for byte the file `clockwork render-template --to`
writes, and "Open in the Method tab" writes the same file and opens it there, attached, for
a person whose next move is to change what the template fixes.

It turns knobs only: a template's derivations are its author's (lab record, task 94), and
no tab edits a template. No grid (E5): one setting, one run. Rendering is pure Python over
a small document, cheap enough to redo on every change on the UI thread. Nothing here
talks to the instrument; the window assembles the job.
"""

from __future__ import annotations

import difflib
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import method as method_module
from ..acq.loop import cautions, refusals
from ..envelope import AgentRange, Limits, agent_ranges
from ..method import template as templates
from ..method.template import Knob, Rendered, Template, TemplateError
from .methodlib import scan_library

__all__ = ["TemplateTab", "default_name"]

_PLACEHOLDER = "label"
"""What a required label left empty is filled with for the defaults' render alone, which
the diff compares against: labels render nothing, so it never shows in the diff."""

_WARN = "#9c4221"


def default_name(template: Template, path: str, knobs: dict[str, object]) -> str:
    """A file name for a render: the template's file stem, then each knob that differs
    from its default as `name-value`, so two renders of one template save apart."""
    stem = os.path.splitext(os.path.basename(path))[0] or template.name or "rendered"
    moved = [f"{knob.name}-{templates.format_number(knobs[knob.name])}"
             for knob in template.knobs
             if knob.name in knobs and knobs[knob.name] != knob.default]
    return "_".join([stem, *moved]) + ".toml"


class TemplateTab(QWidget):
    """Pick a template, turn its knobs, run the render or write it as a method file.

    The window sets the library (`set_library`), the limits the sitting carries
    (`set_limits`) and what greys Acquire (`set_blocking`); it answers `acquire_requested`
    with a job of its own and `open_requested` by opening the file written, attached.
    """

    acquire_requested = Signal()
    open_requested = Signal(str)
    """The path of the method file just written, for the Method tab to open."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.save_in = ""
        self.template: Template | None = None
        self.template_path = ""
        self.rendered: Rendered | None = None
        self.knob_fields: dict[str, QSpinBox | QDoubleSpinBox] = {}
        self.knob_notes: dict[str, QLabel] = {}
        self.label_fields: dict[str, QLineEdit] = {}
        self.limits: Limits | None = None
        self._limits_said = ""
        self.ranges: dict[str, AgentRange] | None = None
        self._blocking: list[str] = []
        self._busy = False

        self.picker = QComboBox()
        self.picker.setToolTip("The templates the method library holds.")
        browse = QPushButton("…")
        browse.setMaximumWidth(32)
        browse.setToolTip("A template file outside the library.")
        browse.clicked.connect(self._browse)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(QLabel("Template"))
        top.addWidget(self.picker, 1)
        top.addWidget(browse)

        self.description = QLabel()
        self.description.setWordWrap(True)
        self.limits_line = QLabel()
        """Which limits the knobs are shown against, or that there are none."""
        self.limits_line.setWordWrap(True)
        self.form_box = QGroupBox("Knobs and labels")
        self.form = QFormLayout(self.form_box)
        self.form.setVerticalSpacing(4)

        self.replicates = QSpinBox()
        self.replicates.setRange(1, 999)
        self.replicates.setPrefix("replicates: ")
        self.replicates.setToolTip(
            "Files acquired from this render, the first and its technical replicates, "
            "one request and one run record between them.")
        self.conditions = QPlainTextEdit()
        self.conditions.setPlaceholderText(
            "sample, MCP voltage, pusher period, collision energy…")
        self.conditions.setMaximumHeight(56)
        self.conditions.setToolTip(
            "The part of the experiment no getter reads, stamped into every file.")
        self.stem_line = QLabel()
        self.stem_line.setToolTip("The name the first file takes, from the output "
                                  "directory and the file initials in Setup.")

        self.acquire_button = QPushButton("Acquire")
        self.acquire_button.setToolTip(
            "Send the boxes what they do not already hold of this render, then acquire. "
            "No method file is written: the run stamps the template, its knob values "
            "and its labels, and its run record says where a value lies outside the "
            "agent's limits.")
        self.save_button = QPushButton("Save as a method…")
        self.save_button.setToolTip(
            "Write this render as a method file carrying the template and the knob "
            "values it was rendered at, the file render-template --to writes.")
        self.open_button = QPushButton("Open in the Method tab…")
        self.open_button.setToolTip(
            "Write the same file and open it in the Method tab, attached, to change what "
            "the template fixes. Its runs stamp as rendered until a pane is edited.")
        self.acquire_button.clicked.connect(lambda: self.acquire_requested.emit())
        self.save_button.clicked.connect(lambda: self.save())
        self.open_button.clicked.connect(lambda: self.save(open_after=True))

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addLayout(top)
        left_layout.addWidget(self.description)
        left_layout.addWidget(self.limits_line)
        left_layout.addWidget(self.form_box)
        left_layout.addWidget(self.conditions)
        run_row = QHBoxLayout()
        run_row.setContentsMargins(0, 0, 0, 0)
        run_row.addWidget(self.acquire_button)
        run_row.addWidget(self.replicates)
        run_row.addWidget(self.stem_line, 1)
        left_layout.addLayout(run_row)
        files_row = QHBoxLayout()
        files_row.setContentsMargins(0, 0, 0, 0)
        files_row.addWidget(self.save_button)
        files_row.addWidget(self.open_button)
        files_row.addStretch(1)
        left_layout.addLayout(files_row)
        left_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        fixed = QFont("Consolas")
        fixed.setStyleHint(QFont.StyleHint.Monospace)
        self.method_text = QPlainTextEdit()
        self.method_text.setReadOnly(True)
        self.method_text.setFont(fixed)
        self.diff_text = QPlainTextEdit()
        self.diff_text.setReadOnly(True)
        self.diff_text.setFont(fixed)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.method_text, "The method")
        self.tabs.addTab(self.diff_text, "Against the template's defaults")

        splitter = QSplitter()
        splitter.addWidget(scroll)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        self.problems = QLabel()
        self.problems.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.problems)

        self.picker.currentIndexChanged.connect(
            lambda _: self.choose(self.picker.currentData() or ""))
        self.set_limits(None, "")

    # -- what the window sets ----------------------------------------------------

    def set_library(self, library: str) -> None:
        """List the library's templates; keep the one chosen where it is still listed."""
        chosen = self.template_path
        self.picker.blockSignals(True)
        self.picker.clear()
        for entry in scan_library(library):
            if entry.ok and entry.is_template:
                self.picker.addItem(entry.name or os.path.basename(entry.path), entry.path)
        self.picker.blockSignals(False)
        if chosen:
            self.choose(chosen)
        elif self.picker.count():
            self.choose(self.picker.currentData())
        else:
            self._show_problems(["the library holds no template; pick one with …"])

    def set_limits(self, limits: Limits | None, said: str) -> None:
        """The limits the daemon's sitting carries, or None with the sentence why none:
        the agent's range each knob is shown against (R10)."""
        self.limits = limits
        self._limits_said = said
        self._show_ranges()
        self.refresh()

    def set_blocking(self, reasons: list[str], *, busy: bool) -> None:
        """What keeps Acquire grey besides the render itself: no initials, no offset,
        the instrument lock, a job in flight."""
        self._blocking = list(reasons)
        self._busy = busy
        self._refresh_buttons()

    def set_stem(self, stem: str) -> None:
        self.stem_line.setText(f"first file {stem}" if stem else "")

    # -- choosing ----------------------------------------------------------------

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open a template", os.path.dirname(self.template_path) or "",
            "Templates (*.toml);;All files (*)")
        if path:
            self.choose(path)

    def choose(self, path: str) -> bool:
        """Load the template at `path` and build its knobs; False, said, where it is none."""
        if not path:
            return False
        try:
            template = templates.load_template(path)
        except (OSError, TemplateError) as exc:
            self._show_problems([f"{os.path.basename(path)}: {exc}"])
            return False
        same = template == self.template and path == self.template_path
        self.template, self.template_path = template, path
        index = self.picker.findData(path)
        self.picker.blockSignals(True)
        if index < 0:
            self.picker.addItem(template.name or os.path.basename(path), path)
            index = self.picker.count() - 1
        self.picker.setCurrentIndex(index)
        self.picker.blockSignals(False)
        description = str(template.body.get("metadata", {}).get("description", ""))
        self.description.setText(description)
        self.description.setVisible(bool(description))
        if not same:
            self._build_form(template)
        self._show_ranges()
        self.refresh()
        return True

    def _build_form(self, template: Template) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self.knob_fields, self.knob_notes, self.label_fields = {}, {}, {}
        for knob in template.knobs:
            field = _knob_field(knob)
            field.valueChanged.connect(lambda _: self.refresh())
            note = QLabel()
            note.setWordWrap(True)
            self.knob_fields[knob.name] = field
            self.knob_notes[knob.name] = note
            cell = QWidget()
            column = QVBoxLayout(cell)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(0)
            column.addWidget(field)
            column.addWidget(note)
            self.form.addRow(knob.name, cell)
        for label in template.labels:
            field = QLineEdit()
            field.setPlaceholderText("required" if label.required else "optional")
            field.setToolTip(label.description)
            field.textChanged.connect(lambda _: self.refresh())
            self.label_fields[label.name] = field
            self.form.addRow(f"{label.name}{' *' if label.required else ''}", field)

    def _show_ranges(self) -> None:
        """Which agent's range each knob is shown against, and the line that says so."""
        template = self.template
        if template is None:
            self.ranges = None
            self.limits_line.setText("")
            return
        if self.limits is None:
            self.ranges = None
            self.limits_line.setText(
                "Each knob is shown against the template's own range alone: "
                + (self._limits_said or "clockwork serve carries no limits")
                + ", so there is no agent's range to show.")
            return
        self.ranges = agent_ranges(template.hash, self.limits,
                                   [knob.name for knob in template.knobs])
        if self.ranges is None:
            self.limits_line.setText(
                "The limits in force do not admit this template, so an agent could not "
                "run it at any setting; a person may, and the run record says so.")
        else:
            self.limits_line.setText(
                "Each knob is shown against the template's range, which its field "
                "cannot leave, and the agent's, from the limits clockwork serve "
                "carries. A value outside the agent's is run anyway, and the run record "
                "says so.")

    # -- rendering ---------------------------------------------------------------

    def knobs(self) -> dict[str, int | float]:
        """Every knob's value as set, an integer knob's as an int."""
        if self.template is None:
            return {}
        values: dict[str, int | float] = {}
        for knob in self.template.knobs:
            value = self.knob_fields[knob.name].value()
            values[knob.name] = int(value) if knob.integer else float(value)
        return values

    def labels(self) -> dict[str, str]:
        """Every label given; one left empty is left out."""
        return {name: field.text().strip() for name, field in self.label_fields.items()
                if field.text().strip()}

    def outside(self) -> list[str]:
        """The knobs whose value lies outside the agent's range, by name."""
        if self.template is None or self.ranges is None:
            return []
        values = self.knobs()
        return [name for name, span in self.ranges.items()
                if name in values and not span.holds(values[name])]

    def refresh(self) -> None:
        """Render at the values set, and show the method, the diff and what is wrong."""
        template = self.template
        self.rendered = None
        if template is None:
            self._refresh_buttons()
            return
        knobs, labels = self.knobs(), self.labels()
        self._mark_knobs(template, knobs)
        try:
            rendered = templates.render(template, knobs, labels)
        except TemplateError as exc:
            self.method_text.setPlainText("")
            self.diff_text.setPlainText("")
            self._show_problems(list(exc.problems))
            return
        self.rendered = rendered
        text = method_module.dumps(rendered.method)
        self.method_text.setPlainText(text)
        self.diff_text.setPlainText(self._diff(template, labels, text))
        said = [f"refused: {line}" for line in refusals(rendered.method)]
        said += [f"caution: {line}" for line in cautions(rendered.method)]
        self.problems.setStyleSheet(f"color: {_WARN}" if said else "")
        self.problems.setText("\n".join(said) if said else templates.describe(rendered))
        self._refresh_buttons()

    def _mark_knobs(self, template: Template, values: dict[str, int | float]) -> None:
        for knob in template.knobs:
            note = self.knob_notes.get(knob.name)
            if note is None:
                continue
            unit = f" {knob.unit}" if knob.unit else ""
            own = (f"template {templates.format_number(knob.min)} to "
                   f"{templates.format_number(knob.max)}{unit}")
            span = self.ranges.get(knob.name) if self.ranges is not None else None
            if self.limits is None:
                note.setText(own)
                note.setStyleSheet("")
                continue
            if span is None:
                note.setText(f"{own}; the agent: not admitted")
                note.setStyleSheet(f"color: {_WARN}")
                continue
            agent = f"the agent {span.text(knob.unit)}"
            if span.holds(values.get(knob.name, knob.default)):
                note.setText(f"{own}; {agent}")
                note.setStyleSheet("")
            else:
                note.setText(f"{own}; {agent}: outside the agent's, run anyway and "
                             "recorded")
                note.setStyleSheet(f"color: {_WARN}; font-weight: bold;")

    @staticmethod
    def _diff(template: Template, labels: dict[str, str], text: str) -> str:
        filled = dict(labels)
        for label in template.labels:
            if label.required and label.name not in filled:
                filled[label.name] = _PLACEHOLDER
        try:
            reference = method_module.dumps(templates.render(template, {}, filled).method)
        except TemplateError as exc:
            return f"the template's defaults do not render: {exc}"
        lines = list(difflib.unified_diff(
            reference.splitlines(), text.splitlines(), "at the defaults", "as set",
            lineterm="", n=1))
        return "\n".join(lines) if lines else "every knob at its default: no difference"

    def _show_problems(self, problems: list[str]) -> None:
        self.problems.setStyleSheet(f"color: {_WARN}")
        self.problems.setText("\n".join(problems))
        self.rendered = None
        self._refresh_buttons()

    def acquire_problems(self) -> list[str]:
        """Why Acquire is grey, beyond a job in flight: the render's refusals and the
        window's own reasons."""
        if self.rendered is None:
            return ["nothing renders yet"]
        return list(refusals(self.rendered.method)) + self._blocking

    def _refresh_buttons(self) -> None:
        rendered = self.rendered is not None
        self.save_button.setEnabled(rendered)
        self.open_button.setEnabled(rendered)
        problems = self.acquire_problems()
        self.acquire_button.setEnabled(not problems and not self._busy)
        self.acquire_button.setToolTip(
            "Acquire is not available: " + "; ".join(problems) if problems else
            "Send the boxes what they do not already hold of this render, then acquire. "
            "No method file is written: the run stamps the template, its knob values "
            "and its labels, and its run record says where a value lies outside the "
            "agent's limits.")

    # -- saving ------------------------------------------------------------------

    def save(self, *, open_after: bool = False) -> str:
        """Ask where, and write the render with its `[rendered]` table; with
        `open_after`, hand the file to the Method tab. Answers the path written."""
        rendered, template = self.rendered, self.template
        if rendered is None or template is None:
            return ""
        suggested = os.path.join(self.save_in or os.path.dirname(self.template_path),
                                 default_name(template, self.template_path, rendered.knobs))
        path = self._ask_path(suggested)
        if path and self.write(path):
            if not path.lower().endswith(".toml"):
                path += ".toml"
            if open_after:
                self.open_requested.emit(path)
            return path
        return ""

    def _ask_path(self, suggested: str) -> str:
        """The save dialog, a method of its own so that a test can answer it."""
        path, _ = QFileDialog.getSaveFileName(self, "Save the method", suggested,
                                              "Method documents (*.toml)")
        return path

    def write(self, path: str) -> bool:
        """Write the current render to `path`, the file `render-template --to` writes.
        True where it was written."""
        if self.rendered is None:
            return False
        if not path.lower().endswith(".toml"):
            path += ".toml"
        if os.path.abspath(path) == os.path.abspath(self.template_path):
            QMessageBox.warning(self, "Not saved", "That is the template's own file.")
            return False
        try:
            method_module.save(templates.attached(self.rendered), path)
        except OSError as exc:
            QMessageBox.warning(self, "Not saved", str(exc))
            return False
        self.save_in = os.path.dirname(os.path.abspath(path))
        return True


def _knob_field(knob: Knob) -> QSpinBox | QDoubleSpinBox:
    """A spin box over the knob's whole range and nothing outside it."""
    field: QSpinBox | QDoubleSpinBox
    if knob.integer:
        field = QSpinBox()
        field.setRange(int(knob.min), int(knob.max))
        field.setValue(int(knob.default))
    else:
        field = QDoubleSpinBox()
        decimals = max(_decimals(value) for value in (knob.min, knob.max, knob.default))
        field.setDecimals(max(decimals, 2))
        field.setRange(float(knob.min), float(knob.max))
        field.setValue(float(knob.default))
    field.setStepType(QAbstractSpinBox.StepType.AdaptiveDecimalStepType)
    if knob.unit:
        field.setSuffix(f" {knob.unit}")
    unit = f" {knob.unit}" if knob.unit else ""
    span = (f"{templates.format_number(knob.min)} to {templates.format_number(knob.max)}"
            f"{unit}, default {templates.format_number(knob.default)}")
    field.setToolTip(f"{knob.description}\n{span}" if knob.description else span)
    return field


def _decimals(value: float) -> int:
    text = templates.format_number(value)
    return len(text.split(".", 1)[1]) if "." in text else 0

