"""Choosing a colour of one's own for a note.

Not the system's colour window, which may keep what it shows in the system's
settings, nor Qt's, whose own "custom colours" would stand beside the user's.
A colour is picked from suggestions, set by hue, saturation and lightness, or
typed as #rrggbb, all by keyboard too; the note is shown as it would look. A
colour text cannot be read on as it is is drawn a little lighter or darker:
that is said, and the colour is kept as chosen.
"""

import colorsys
import itertools
import re
from typing import override

from PySide6.QtCore import QEvent, QRectF, QSize, Qt
from PySide6.QtGui import QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from stickle.app.palette import qcolor, swatch_icon
from stickle.core.colors import Rgb, is_hex, needs_moving, note_colors

TYPED = re.compile(r"#?([0-9a-fA-F]{6})")
SUGGESTION_SIZE = 22
PREVIEW_SIZE = QSize(150, 96)
PREVIEW_RADIUS = 6
PREVIEW_TITLE = 18


def suggestions() -> list[str]:
    """Colours to start from: each hue light and deeper, and a few neutrals."""
    found: list[str] = []
    for hue in range(0, 360, 30):
        for saturation, lightness in ((0.65, 0.82), (0.55, 0.62)):
            red, green, blue = colorsys.hls_to_rgb(hue / 360, lightness, saturation)
            found.append(Rgb(round(red * 255), round(green * 255), round(blue * 255)).hex)
    return [*found, "#f5f5f5", "#cfd3d6", "#3c3f44", "#1f2430"]


class NotePreview(QWidget):
    """A note of the colour, small: its title bar, border and text."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.color = "#ffec8c"
        self.sample = ""
        self.setFixedSize(PREVIEW_SIZE)

    def show_color(self, color: str) -> None:
        self.color = color
        self.update()

    @override
    def paintEvent(self, event: QPaintEvent) -> None:
        colors = note_colors(self.color)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        outline = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        shape = QPainterPath()
        shape.addRoundedRect(outline, PREVIEW_RADIUS, PREVIEW_RADIUS)
        painter.fillPath(shape, qcolor(colors.background))
        painter.setClipPath(shape)
        painter.fillRect(0, 0, self.width(), PREVIEW_TITLE, qcolor(colors.title_bar))
        painter.setClipping(False)
        painter.setPen(QPen(qcolor(colors.border), 1))
        painter.drawPath(shape)
        painter.setPen(qcolor(colors.text))
        text_area = QRectF(8, PREVIEW_TITLE + 6, self.width() - 16, self.height() - 30)
        painter.drawText(text_area, Qt.TextFlag.TextWordWrap, self.sample)
        painter.end()


class ColorDialog(QDialog):
    """chosen() is the colour (#rrggbb) once accepted."""

    def __init__(self, start: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = start if is_hex(start) else note_colors(start).background.hex
        self._showing = False  # the controls being set to the colour: not a choice

        self.suggestions = QListWidget(self)
        self.suggestions.setViewMode(QListView.ViewMode.IconMode)
        self.suggestions.setFlow(QListView.Flow.LeftToRight)
        self.suggestions.setWrapping(True)
        self.suggestions.setMovement(QListView.Movement.Static)
        self.suggestions.setIconSize(QSize(SUGGESTION_SIZE, SUGGESTION_SIZE))
        self.suggestions.setSpacing(2)
        self.suggestions.setFixedHeight(4 * (SUGGESTION_SIZE + 12))
        for value in suggestions():
            item = QListWidgetItem(swatch_icon(value), "")
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, value)
            item.setToolTip(value)
            self.suggestions.addItem(item)
        self.suggestions.currentItemChanged.connect(self._suggestion_chosen)
        self.suggestions_label = QLabel(self)
        self.suggestions_label.setBuddy(self.suggestions)

        self.hue = QSlider(Qt.Orientation.Horizontal, self)
        self.saturation = QSlider(Qt.Orientation.Horizontal, self)
        self.lightness = QSlider(Qt.Orientation.Horizontal, self)
        self.hue.setRange(0, 359)
        self.saturation.setRange(0, 100)
        self.lightness.setRange(0, 100)
        for slider in (self.hue, self.saturation, self.lightness):
            slider.valueChanged.connect(self._slid)
        self.hue_label = QLabel(self)
        self.saturation_label = QLabel(self)
        self.lightness_label = QLabel(self)
        self.hue_label.setBuddy(self.hue)
        self.saturation_label.setBuddy(self.saturation)
        self.lightness_label.setBuddy(self.lightness)

        self.code = QLineEdit(self)
        self.code.setMaxLength(7)
        self.code.textEdited.connect(self._typed)
        self.code_label = QLabel(self)
        self.code_label.setBuddy(self.code)

        self.preview = NotePreview(self)
        self.contrast = QLabel(self)
        self.contrast.setWordWrap(True)

        self.buttons = QDialogButtonBox(self)
        self.ok_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.RejectRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow(self.hue_label, self.hue)
        form.addRow(self.saturation_label, self.saturation)
        form.addRow(self.lightness_label, self.lightness)
        form.addRow(self.code_label, self.code)
        layout = QVBoxLayout(self)
        layout.addWidget(self.suggestions_label)
        layout.addWidget(self.suggestions)
        layout.addLayout(form)
        layout.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.contrast)
        layout.addWidget(self.buttons)
        self.ok_button.setDefault(True)  # once in the window (see MasterPasswordDialog)
        self._set_tab_order()
        self.setMinimumWidth(380)
        self.retranslate()
        self._show(self._color)
        self.code.setFocus()
        self.code.selectAll()

    def _set_tab_order(self) -> None:
        box = self.buttons.layout()
        buttons = [
            widget
            for index in range(box.count() if box is not None else 0)
            if box is not None
            and (item := box.itemAt(index)) is not None
            and isinstance(widget := item.widget(), QPushButton)
        ]
        order: list[QWidget] = [
            self.suggestions,
            self.hue,
            self.saturation,
            self.lightness,
            self.code,
            *buttons,
        ]
        for before, after in itertools.pairwise(order):
            QWidget.setTabOrder(before, after)

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Add a colour"))
        self.suggestions_label.setText(self.tr("&Suggestions:"))
        self.suggestions.setAccessibleName(self.tr("Suggested colours"))
        self.hue_label.setText(self.tr("&Hue:"))
        self.saturation_label.setText(self.tr("Sat&uration:"))
        self.lightness_label.setText(self.tr("&Lightness:"))
        self.hue.setAccessibleName(self.tr("Hue"))
        self.saturation.setAccessibleName(self.tr("Saturation"))
        self.lightness.setAccessibleName(self.tr("Lightness"))
        self.code_label.setText(self.tr("Colour &code:"))
        self.code.setAccessibleName(self.tr("Colour code"))
        self.code.setPlaceholderText("#rrggbb")
        self.preview.sample = self.tr("A note of this colour")
        self.preview.setAccessibleName(self.tr("A note of this colour"))
        self.ok_button.setText(self.tr("Add"))
        self.cancel_button.setText(self.tr("Cancel"))
        self._show_contrast()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def chosen(self) -> str:
        return self._color

    def _show(self, color: str, keep: QWidget | None = None) -> None:
        """Every control set to color but keep (the one it was chosen with)."""
        self._color = color
        self._showing = True
        try:
            rgb = Rgb.from_hex(color)
            hue, lightness, saturation = colorsys.rgb_to_hls(
                rgb.red / 255, rgb.green / 255, rgb.blue / 255
            )
            if keep not in (self.hue, self.saturation, self.lightness):
                self.hue.setValue(round(hue * 360) % 360)
                self.saturation.setValue(round(saturation * 100))
                self.lightness.setValue(round(lightness * 100))
            if keep is not self.code:
                self.code.setText(color)
            if keep is not self.suggestions:
                row = next(
                    (
                        r
                        for r in range(self.suggestions.count())
                        if self.suggestions.item(r).data(Qt.ItemDataRole.UserRole) == color
                    ),
                    -1,
                )
                self.suggestions.setCurrentRow(row)  # -1: not among them
        finally:
            self._showing = False
        self.preview.show_color(color)
        self._show_contrast()

    def _show_contrast(self) -> None:
        moved = needs_moving(Rgb.from_hex(self._color))
        self.contrast.setText(
            self.tr(
                "Text is hard to read on this colour, so notes of it are drawn a little"
                " lighter or darker."
            )
            if moved
            else ""
        )
        self.contrast.setVisible(moved)

    def _suggestion_chosen(self, item: QListWidgetItem | None) -> None:
        if self._showing or item is None:
            return
        value = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(value, str):
            self._show(value, keep=self.suggestions)

    def _slid(self) -> None:
        if self._showing:
            return
        red, green, blue = colorsys.hls_to_rgb(
            self.hue.value() / 360, self.lightness.value() / 100, self.saturation.value() / 100
        )
        color = Rgb(round(red * 255), round(green * 255), round(blue * 255)).hex
        sender = self.sender()
        self._show(color, keep=sender if isinstance(sender, QSlider) else None)

    def _typed(self, text: str) -> None:
        match = TYPED.fullmatch(text.strip())
        if match is not None:
            self._show("#" + match.group(1).lower(), keep=self.code)

    @override
    def accept(self) -> None:
        typed = TYPED.fullmatch(self.code.text().strip())
        if typed is None:
            self.code.setFocus()
            self.code.selectAll()
            return  # what is typed is not a colour: nothing chosen yet
        self._color = "#" + typed.group(1).lower()
        super().accept()
