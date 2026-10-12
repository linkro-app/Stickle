"""Categories and marks on screen: names, drawn icons, and their place in a
note's title bar.

A note's category shows at the left of its title bar as its icon (drawn by
label_icons), which only shows it. Its marks show at the right as small
icons, as many as fit, then a count of the rest; clicking them opens
the note menu.
"""

from typing import override

from PySide6.QtCore import QCoreApplication, QLocale, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QIcon,
    QPainter,
    QPaintEvent,
)
from PySide6.QtWidgets import QSizePolicy, QToolButton, QWidget

from stickle.app.label_icons import label_icon, paint_label, state_pixmap
from stickle.app.palette import qcolor, rgb
from stickle.core.colors import PALETTE, clear_on, colors_for
from stickle.core.labels import Category, Mark

ICON = 12  # as the title bar's other icons
STROKE = 1.1
HEIGHT = 20  # as the title bar's buttons
GAP = 3
DOT = 8
PADDING = 2


def dot_color(key: str) -> QColor:
    """A category's dot: its palette colour's deeper shade, which shows on pastel notes."""
    return qcolor(colors_for(PALETTE.get(key, PALETTE["blue"])).border)


def mark_name(mark: Mark) -> str:
    """The name the user gave the mark, or a built-in mark's own, translated."""
    if mark.name is not None:
        return mark.name
    names = {
        "todo": QCoreApplication.translate("Mark", "To do"),
        "urgent": QCoreApplication.translate("Mark", "Urgent"),
        "important": QCoreApplication.translate("Mark", "Important"),
        "waiting": QCoreApplication.translate("Mark", "Waiting"),
    }
    return names.get(mark.id, mark.id)


def mark_icon(icon: str, color: QColor | None = None) -> QIcon:
    """A mark's icon for lists and menus."""
    return label_icon(icon, color or QColor(32, 32, 32))


def category_item_icon(category: Category, background: QColor) -> QIcon:
    """A category's icon beside its name in a list or menu: its colour,
    deepened (or lightened, on a dark theme) to show on background."""
    shown = clear_on(rgb(dot_color(category.color)), rgb(background))
    return label_icon(category.icon, qcolor(shown))


def mark_item_icon(mark: Mark, ink: QColor) -> QIcon:
    """A mark's icon beside its name in a list or menu, in the text's colour."""
    return mark_icon(mark.icon, ink)


def draw_mark(painter: QPainter, icon: str, x: float, y: float, color: QColor) -> None:
    """The mark's icon, ICON across, with its top left at (x, y): its lines a
    little heavier than a category's, so that it stands out at this size."""
    paint_label(painter, icon, color, QRectF(x, y, ICON, ICON), bold=True)


class _TitleBarPart(QToolButton):
    """Drawn in the title bar's text colour; a click opens the note menu."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAutoRaise(True)
        self.setFixedHeight(HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.color = QColor(Qt.GlobalColor.black)
        self.hide()

    def set_color(self, color: QColor) -> None:
        self.color = color
        self.update()


class CategoryTag(_TitleBarPart):
    """The note's state at the left of its title bar: its category's icon (the
    app's own for none), with a strip of tape across it while the note stays
    on top. As large as the title's letters. It only shows the state: the
    category is changed from the note menu, staying on top with the pin."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.category: Category | None = None
        self.on_top = False
        self.background = QColor(Qt.GlobalColor.white)
        # Not a button: a press here starts moving the note, as on the bar.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.show()

    def set_category(self, category: Category | None) -> None:
        self.category = category
        self.retranslate()
        self.update()

    def set_on_top(self, on_top: bool) -> None:
        self.on_top = on_top
        self.retranslate()
        self.update()

    def set_background(self, background: QColor) -> None:
        self.background = background
        self.update()

    def retranslate(self) -> None:
        if self.category is None:
            text = QCoreApplication.translate("CategoryTag", "No category")
        else:
            text = QCoreApplication.translate("CategoryTag", "Category: %1").replace(
                "%1", self.category.name
            )
        if self.on_top:
            text = QCoreApplication.translate("CategoryTag", "%1, stays on top").replace("%1", text)
        self.setAccessibleName(text)
        self.setToolTip(text)

    def icon_size(self) -> int:
        """As tall as the title's letters, with room for the tape at its corner."""
        return QFontMetrics(self.font()).height()

    def icon_color(self) -> QColor:
        """The category's colour, deepened until it shows on the note; the
        title's colour for the app's own icon."""
        if self.category is None:
            return self.color
        shown = clear_on(rgb(dot_color(self.category.color)), rgb(self.background))
        return qcolor(shown)

    @override
    def sizeHint(self) -> QSize:
        return QSize(self.icon_size() + 2 * PADDING, self.height())

    @override
    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    @override
    def paintEvent(self, event: QPaintEvent) -> None:
        size = self.icon_size()
        icon = state_pixmap(
            self.category.icon if self.category is not None else None,
            self.icon_color(),
            self.color if self.on_top else None,
            size,
            self.devicePixelRatioF(),
        )
        painter = QPainter(self)
        painter.drawPixmap(QPointF(PADDING, (self.height() - size) / 2), icon)
        painter.end()


class MarksBadge(_TitleBarPart):
    """The note's marks: their icons, as many as fit, then "+n" for the rest."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.marks: list[Mark] = []

    def set_marks(self, marks: list[Mark]) -> None:
        self.marks = marks
        self.setVisible(bool(marks))
        self.retranslate()
        self.updateGeometry()
        self.update()

    def retranslate(self) -> None:
        names = ", ".join(mark_name(mark) for mark in self.marks)
        text = QCoreApplication.translate("MarksBadge", "Marks: %1").replace("%1", names)
        self.setAccessibleName(text)
        self.setToolTip(text)

    def _more(self, count: int) -> str:
        return "+" + QLocale().toString(count)

    def _width_for(self, icons: int, more: int) -> int:
        width = PADDING + icons * ICON + max(0, icons - 1) * GAP + PADDING
        if more:
            width += GAP + QFontMetrics(self.font()).horizontalAdvance(self._more(more))
        return width

    def shown(self) -> tuple[int, int]:
        """(icons drawn, marks counted in "+n") for the width it has."""
        total = len(self.marks)
        for icons in range(total, 0, -1):
            if self.width() >= self._width_for(icons, total - icons):
                return icons, total - icons
        return min(1, total), max(0, total - 1)

    @override
    def sizeHint(self) -> QSize:
        return QSize(self._width_for(len(self.marks), 0), self.height())

    @override
    def minimumSizeHint(self) -> QSize:
        total = len(self.marks)
        return QSize(self._width_for(min(1, total), max(0, total - 1)), self.height())

    @override
    def paintEvent(self, event: QPaintEvent) -> None:
        icons, more = self.shown()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = (self.height() - ICON) / 2
        x = float(PADDING)
        for mark in self.marks[:icons]:
            draw_mark(painter, mark.icon, x, top, self.color)
            x += ICON + GAP
        if more:
            painter.setPen(self.color)
            painter.drawText(
                QRectF(x, 0, self.width() - x, self.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self._more(more),
            )
        painter.end()
