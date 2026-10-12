"""Asking for a new category (its name, colour and icon), and for another icon
for a category or a mark."""

from collections.abc import Callable
from typing import override

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from stickle.app.label_icons import icon_label, label_icon
from stickle.app.labels import dot_color
from stickle.app.palette import color_name, qcolor, rgb, swatch_icon
from stickle.core.colors import PALETTE, clear_on
from stickle.core.labels import (
    BUILT_IN_MARK_ICONS,
    DEFAULT_ICON,
    LABEL_ICONS,
    NAME_LENGTH,
    Category,
)
from stickle.data.labels import CategoryNameError

MIN_WIDTH = 320
GRID_COLUMNS = 8
GRID_CELL = 32
GRID_ICON = 20
ICON_NAME = Qt.ItemDataRole.UserRole
# A mark's icons start with the built-in marks' own.
MARK_FIRST = tuple(BUILT_IN_MARK_ICONS.values())

# Makes a category from its name, colour and icon.
type CreateCategory = Callable[[str, str, str], Category]

# The dot is beside a note of any colour: these stand out on most of them.
DOT_COLORS = ("blue", "coral", "green", "lavender", "apricot", "sky", "pink", "mint")


def next_dot_color(taken: int) -> str:
    """A colour for the next category, going round DOT_COLORS."""
    return DOT_COLORS[taken % len(DOT_COLORS)]


class IconGrid(QListWidget):
    """The icons a category or a mark can have, in a grid, those in first
    first: the arrow keys move, Enter or a double-click chooses. Each has its
    name for the tooltip and screen readers."""

    def __init__(self, parent: QWidget | None, color: QColor, first: tuple[str, ...] = ()) -> None:
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setIconSize(QSize(GRID_ICON, GRID_ICON))
        self.setGridSize(QSize(GRID_CELL, GRID_CELL))
        # The chosen one marked across its whole cell (its text, under it, is empty).
        cell = GRID_CELL - 4
        self.setStyleSheet(
            f"QListWidget::item {{ width: {cell}px; height: {cell}px; border-radius: 4px; }}"
            "QListWidget::item:selected { background: transparent;"
            " border: 2px solid palette(highlight); }"
        )
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._names = [*first, *(name for name in LABEL_ICONS if name not in first)]
        for name in self._names:
            item = QListWidgetItem("")
            item.setData(ICON_NAME, name)
            self.addItem(item)
        rows = -(-len(LABEL_ICONS) // GRID_COLUMNS)
        frame = 2 * self.frameWidth() + 4
        self.setFixedSize(GRID_COLUMNS * GRID_CELL + frame, rows * GRID_CELL + frame)
        self.set_color(color)
        self.setCurrentRow(0)
        self.retranslate()

    def set_color(self, color: QColor) -> None:
        """The icons in color, deepened to show here as on a note."""
        shown = qcolor(clear_on(rgb(color), rgb(self.palette().base().color())))
        for row in range(self.count()):
            item = self.item(row)
            item.setIcon(label_icon(str(item.data(ICON_NAME)), shown))

    def retranslate(self) -> None:
        self.setAccessibleName(self.tr("Icons"))
        for row in range(self.count()):
            item = self.item(row)
            name = icon_label(str(item.data(ICON_NAME)))
            item.setToolTip(name)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, name)

    def chosen(self) -> str:
        row = self.currentRow()
        return str(self.item(row).data(ICON_NAME)) if row >= 0 else DEFAULT_ICON

    def choose(self, name: str) -> None:
        self.setCurrentRow(self._names.index(name) if name in self._names else 0)


class IconDialog(QDialog):
    """Another icon for a category or a mark (for_mark), from the grid."""

    def __init__(
        self, parent: QWidget | None, current: str, color: QColor, for_mark: bool = False
    ) -> None:
        super().__init__(parent)
        self._for_mark = for_mark
        self.grid = IconGrid(self, color, MARK_FIRST if for_mark else ())
        self.grid.choose(current)
        self.grid.itemActivated.connect(self.accept)
        self.label = QLabel(self)
        self.label.setBuddy(self.grid)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.label)
        layout.addWidget(self.grid)
        layout.addWidget(self.buttons)
        self.retranslate()
        self.grid.setFocus()

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Mark icon") if self._for_mark else self.tr("Category icon"))
        self.label.setText(self.tr("&Icon:"))
        self.grid.retranslate()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)


def _ask_for_icon(parent: QWidget, current: str, color: QColor, for_mark: bool) -> str | None:
    dialog = IconDialog(parent, current, color, for_mark)
    return dialog.grid.chosen() if dialog.exec() == QDialog.DialogCode.Accepted else None


# Asks, and returns once answered: the icon chosen, or None. Replaced in tests.
choose_icon: Callable[[QWidget, str, QColor, bool], str | None] = _ask_for_icon


class NewCategoryDialog(QDialog):
    """A name, a colour and an icon. create makes the category, or raises
    CategoryNameError, which is shown here while the dialog stays open."""

    def __init__(self, parent: QWidget | None, create: CreateCategory, color: str) -> None:
        super().__init__(parent)
        self._create = create
        self.created: Category | None = None
        # Narrower, and window managers cut its title short.
        self.setMinimumWidth(MIN_WIDTH)

        self.name = QLineEdit(self)
        self.name.setMaxLength(NAME_LENGTH)
        self.color = QComboBox(self)
        for key in PALETTE:
            self.color.addItem(swatch_icon(key), "", key)
        self.color.setCurrentIndex(max(0, self.color.findData(color)))
        self.icon = IconGrid(self, dot_color(color))
        self.color.currentIndexChanged.connect(self._recolor_icons)
        self.problem = QLabel(self)
        self.problem.setWordWrap(True)
        self.problem.hide()

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.name.textChanged.connect(self._name_changed)

        self.form = QFormLayout()
        self.name_label = QLabel(self)
        self.name_label.setBuddy(self.name)
        self.color_label = QLabel(self)
        self.color_label.setBuddy(self.color)
        self.icon_label = QLabel(self)
        self.icon_label.setBuddy(self.icon)
        self.form.addRow(self.name_label, self.name)
        self.form.addRow(self.color_label, self.color)
        self.form.addRow(self.icon_label, self.icon)
        layout = QVBoxLayout(self)
        layout.addLayout(self.form)
        layout.addWidget(self.problem)
        layout.addWidget(self.buttons)

        self.retranslate()
        self._name_changed("")

    @property
    def ok_button(self) -> QPushButton:
        button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert button is not None
        return button

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("New category"))
        self.name_label.setText(self.tr("&Name:"))
        self.color_label.setText(self.tr("&Color:"))
        self.icon_label.setText(self.tr("&Icon:"))
        for index in range(self.color.count()):
            self.color.setItemText(index, color_name(str(self.color.itemData(index))))
        self.icon.retranslate()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def _recolor_icons(self) -> None:
        self.icon.set_color(dot_color(str(self.color.currentData())))

    def _name_changed(self, text: str) -> None:
        self.ok_button.setEnabled(bool(text.strip()))
        self.problem.hide()

    @override
    def accept(self) -> None:
        try:
            self.created = self._create(
                self.name.text(), str(self.color.currentData()), self.icon.chosen()
            )
        except CategoryNameError:
            self.problem.setText(self.tr("There is already a category with this name."))
            self.problem.show()
            self.name.setFocus()
            self.name.selectAll()
            return
        super().accept()
