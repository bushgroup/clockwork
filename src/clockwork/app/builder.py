"""`File > Build a method…`: a template's knobs turned in the window, saved as one method.

Shape 2 of the method builder, one render only (lab record, task 164; Matt, 2026-11-18):
a person picks a template, sets each knob within its range and fills the labels, sees the
method that would be sent and what differs from the template's defaults, and saves it as
a method file carrying its `[rendered]` table, the same file `clockwork render-template
--to` writes. The window opens what was saved, attached, so its runs stamp the template's
hash, the knob values and the labels, with no terminal and no agent between the person
and the run.

It renders and saves; it runs nothing, so it has no worker and needs no daemon, and every
string still goes out through the window's own Send and Acquire. It turns knobs only: a
template's derivations are its author's (lab record, task 94). No grid:
one setting, one file. Rendering is pure Python over a small document, cheap enough to
redo on every change of a knob on the UI thread.
"""

from __future__ import annotations

import difflib
import os

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import method as method_module
from ..acq.loop import cautions, refusals
from ..method import template as templates
from ..method.template import Knob, Rendered, Template, TemplateError
from .methodlib import scan_library

__all__ = ["BuildDialog", "default_name"]

_PLACEHOLDER = "label"
"""What a required label left empty is filled with for the defaults' render alone, which
the diff compares against: labels render nothing, so it never shows in the diff."""


def default_name(template: Template, path: str, knobs: dict[str, object]) -> str:
    """A file name for a render: the template's file stem, then each knob that differs
    from its default as `name-value`, so two renders of one template save apart."""
    stem = os.path.splitext(os.path.basename(path))[0] or template.name or "rendered"
    moved = [f"{knob.name}-{templates.format_number(knobs[knob.name])}"
             for knob in template.knobs
             if knob.name in knobs and knobs[knob.name] != knob.default]
    return "_".join([stem, *moved]) + ".toml"


class BuildDialog(QDialog):
    """Pick a template, turn its knobs, save the render; `saved_path` is the file written.

    `library` is the window's library setting, whose templates the picker lists; `save_in`
    the folder Save offers first. `template_path` preselects one, and a template outside
    the library is reached with the picker's browse button.
    """

    def __init__(self, library: str = "", save_in: str = "", template_path: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Build a method from a template")
        self.resize(980, 640)
        self.save_in = save_in
        self.saved_path = ""
        """Set, and the dialog closed accepted, where Save wrote a file."""
        self.template: Template | None = None
        self.template_path = ""
        self.rendered: Rendered | None = None
        self.knob_fields: dict[str, QSpinBox | QDoubleSpinBox] = {}
        self.label_fields: dict[str, QLineEdit] = {}

        self.picker = QComboBox()
        self.picker.setToolTip("The templates the method library holds.")
        for entry in scan_library(library):
            if entry.ok and entry.is_template:
                self.picker.addItem(entry.name or os.path.basename(entry.path), entry.path)
        browse = QPushButton("…")
        browse.setMaximumWidth(32)
        browse.setToolTip("A template file outside the library.")
        browse.clicked.connect(self._browse)
        top = QHBoxLayout()
        top.addWidget(QLabel("Template:"))
        top.addWidget(self.picker, 1)
        top.addWidget(browse)

        self.description = QLabel()
        self.description.setWordWrap(True)
        self.form_box = QGroupBox("Knobs and labels")
        self.form = QFormLayout(self.form_box)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.description)
        left_layout.addWidget(self.form_box)
        left_layout.addStretch(1)

        fixed = QFont("Consolas")
        fixed.setStyleHint(QFont.StyleHint.Monospace)
        self.method_text = QPlainTextEdit()
        self.method_text.setReadOnly(True)
        self.method_text.setFont(fixed)
        self.diff_text = QPlainTextEdit()
        self.diff_text.setReadOnly(True)
        self.diff_text.setFont(fixed)
        tabs = QTabWidget()
        tabs.addTab(self.method_text, "The method")
        tabs.addTab(self.diff_text, "Against the template's defaults")
        self.tabs = tabs

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(tabs)
        splitter.setStretchFactor(1, 1)

        self.problems = QLabel()
        self.problems.setWordWrap(True)
        self.problems.setStyleSheet("color: #9c4221")

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                        | QDialogButtonBox.StandardButton.Cancel)
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("Save and open…")
        self.save_button.setToolTip(
            "Write this method, with the template and the knob values it was rendered "
            "at, and open it in the window. Its runs stamp as rendered until a pane is "
            "edited.")
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.problems)
        layout.addWidget(self.buttons)

        self.picker.currentIndexChanged.connect(
            lambda _: self.choose(self.picker.currentData() or ""))
        if template_path:
            self.choose(template_path)
        elif self.picker.count():
            self.choose(self.picker.currentData())
        else:
            self._show_problems(["the library holds no template; pick one with …"])

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
        self.template, self.template_path = template, path
        index = self.picker.findData(path)
        if index >= 0 and index != self.picker.currentIndex():
            self.picker.blockSignals(True)
            self.picker.setCurrentIndex(index)
            self.picker.blockSignals(False)
        elif index < 0:
            self.picker.blockSignals(True)
            self.picker.addItem(template.name or os.path.basename(path), path)
            self.picker.setCurrentIndex(self.picker.count() - 1)
            self.picker.blockSignals(False)
        description = str(template.body.get("metadata", {}).get("description", ""))
        self.description.setText(description)
        self.description.setVisible(bool(description))
        self._build_form(template)
        self.refresh()
        return True

    def _build_form(self, template: Template) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self.knob_fields, self.label_fields = {}, {}
        for knob in template.knobs:
            field = _knob_field(knob)
            field.valueChanged.connect(lambda _: self.refresh())
            self.knob_fields[knob.name] = field
            self.form.addRow(knob.name, field)
        for label in template.labels:
            field = QLineEdit()
            field.setPlaceholderText("required" if label.required else "optional")
            field.setToolTip(label.description)
            field.textChanged.connect(lambda _: self.refresh())
            self.label_fields[label.name] = field
            self.form.addRow(f"{label.name}{' *' if label.required else ''}", field)

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

    def refresh(self) -> None:
        """Render at the values set, and show the method, the diff and what is wrong."""
        template = self.template
        self.rendered = None
        if template is None:
            self.save_button.setEnabled(False)
            return
        knobs, labels = self.knobs(), self.labels()
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
        self.problems.setStyleSheet("color: #9c4221" if said else "")
        self.problems.setText("\n".join(said) if said else templates.describe(rendered))
        self.save_button.setEnabled(True)

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
        self.problems.setStyleSheet("color: #9c4221")
        self.problems.setText("\n".join(problems))
        self.save_button.setEnabled(False)

    # -- saving ------------------------------------------------------------------

    def save(self) -> None:
        """Ask where, write the render with its `[rendered]` table, and close accepted."""
        rendered, template = self.rendered, self.template
        if rendered is None or template is None:
            return
        suggested = os.path.join(self.save_in or os.path.dirname(self.template_path),
                                 default_name(template, self.template_path, rendered.knobs))
        path, _ = QFileDialog.getSaveFileName(self, "Save the method", suggested,
                                              "Method documents (*.toml)")
        if path:
            self.write(path)

    def write(self, path: str) -> bool:
        """Write the current render to `path`; a method of its own so a test can call it
        without the file dialog. True, and the dialog accepted, where it was written."""
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
        self.saved_path = path
        self.accept()
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
