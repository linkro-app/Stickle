"""Icons in a note's title bar, on its left: what the note is about, and
whether it stays on top.

A category shows as the icon chosen for it (from Lucide, in icons/lucide,
ISC licence); a note without one shows the app's own icon, drawn in one
colour like the other title bar icons. A strip of tape across the icon's
corner says the note stays on top. These only show the note's state: they
are changed from the note's menu and the pin.
"""

from functools import cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QCoreApplication, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer

from stickle.core.labels import CATEGORY_ICONS, DEFAULT_CATEGORY_ICON

ICONS = Path(__file__).resolve().parent / "icons" / "lucide"
TAPE_OPACITY = 0.7
LIST_ICON_SIZE = 16


def icon_label(name: str) -> str:
    """What the icon shows, in the interface language: its tooltip and its name
    for screen readers where it is chosen."""
    labels = {
        "tag": QCoreApplication.translate("CategoryIcon", "Tag"),
        "folder": QCoreApplication.translate("CategoryIcon", "Folder"),
        "briefcase": QCoreApplication.translate("CategoryIcon", "Briefcase"),
        "building-2": QCoreApplication.translate("CategoryIcon", "Office"),
        "house": QCoreApplication.translate("CategoryIcon", "Home"),
        "users": QCoreApplication.translate("CategoryIcon", "People"),
        "user": QCoreApplication.translate("CategoryIcon", "Person"),
        "baby": QCoreApplication.translate("CategoryIcon", "Baby"),
        "heart": QCoreApplication.translate("CategoryIcon", "Heart"),
        "heart-pulse": QCoreApplication.translate("CategoryIcon", "Health"),
        "stethoscope": QCoreApplication.translate("CategoryIcon", "Doctor"),
        "pill": QCoreApplication.translate("CategoryIcon", "Medicine"),
        "dumbbell": QCoreApplication.translate("CategoryIcon", "Exercise"),
        "utensils": QCoreApplication.translate("CategoryIcon", "Food"),
        "coffee": QCoreApplication.translate("CategoryIcon", "Coffee"),
        "shopping-cart": QCoreApplication.translate("CategoryIcon", "Shopping"),
        "wallet": QCoreApplication.translate("CategoryIcon", "Wallet"),
        "piggy-bank": QCoreApplication.translate("CategoryIcon", "Savings"),
        "credit-card": QCoreApplication.translate("CategoryIcon", "Card"),
        "receipt": QCoreApplication.translate("CategoryIcon", "Receipt"),
        "book-open": QCoreApplication.translate("CategoryIcon", "Reading"),
        "graduation-cap": QCoreApplication.translate("CategoryIcon", "Study"),
        "pencil": QCoreApplication.translate("CategoryIcon", "Writing"),
        "file-text": QCoreApplication.translate("CategoryIcon", "Document"),
        "list-checks": QCoreApplication.translate("CategoryIcon", "Checklist"),
        "calendar": QCoreApplication.translate("CategoryIcon", "Calendar"),
        "clock": QCoreApplication.translate("CategoryIcon", "Time"),
        "alarm-clock": QCoreApplication.translate("CategoryIcon", "Alarm"),
        "mail": QCoreApplication.translate("CategoryIcon", "Mail"),
        "message-circle": QCoreApplication.translate("CategoryIcon", "Message"),
        "phone": QCoreApplication.translate("CategoryIcon", "Phone"),
        "smartphone": QCoreApplication.translate("CategoryIcon", "Mobile"),
        "laptop": QCoreApplication.translate("CategoryIcon", "Computer"),
        "code": QCoreApplication.translate("CategoryIcon", "Code"),
        "terminal": QCoreApplication.translate("CategoryIcon", "Terminal"),
        "flask-conical": QCoreApplication.translate("CategoryIcon", "Science"),
        "wrench": QCoreApplication.translate("CategoryIcon", "Repairs"),
        "hammer": QCoreApplication.translate("CategoryIcon", "Tools"),
        "plane": QCoreApplication.translate("CategoryIcon", "Travel"),
        "car": QCoreApplication.translate("CategoryIcon", "Car"),
        "bus": QCoreApplication.translate("CategoryIcon", "Transit"),
        "map-pin": QCoreApplication.translate("CategoryIcon", "Place"),
        "gift": QCoreApplication.translate("CategoryIcon", "Gift"),
        "music": QCoreApplication.translate("CategoryIcon", "Music"),
        "camera": QCoreApplication.translate("CategoryIcon", "Photos"),
        "film": QCoreApplication.translate("CategoryIcon", "Films"),
        "gamepad-2": QCoreApplication.translate("CategoryIcon", "Games"),
        "palette": QCoreApplication.translate("CategoryIcon", "Art"),
        "lightbulb": QCoreApplication.translate("CategoryIcon", "Ideas"),
        "sparkles": QCoreApplication.translate("CategoryIcon", "Sparkles"),
        "star": QCoreApplication.translate("CategoryIcon", "Star"),
        "flag": QCoreApplication.translate("CategoryIcon", "Flag"),
        "target": QCoreApplication.translate("CategoryIcon", "Goals"),
        "trophy": QCoreApplication.translate("CategoryIcon", "Trophy"),
        "rocket": QCoreApplication.translate("CategoryIcon", "Projects"),
        "bookmark": QCoreApplication.translate("CategoryIcon", "Bookmark"),
        "archive": QCoreApplication.translate("CategoryIcon", "Archive"),
        "lock": QCoreApplication.translate("CategoryIcon", "Private"),
        "key": QCoreApplication.translate("CategoryIcon", "Key"),
        "leaf": QCoreApplication.translate("CategoryIcon", "Nature"),
        "sun": QCoreApplication.translate("CategoryIcon", "Sun"),
        "cloud": QCoreApplication.translate("CategoryIcon", "Cloud"),
        "dog": QCoreApplication.translate("CategoryIcon", "Dog"),
        "cat": QCoreApplication.translate("CategoryIcon", "Cat"),
    }
    return labels.get(name, labels[DEFAULT_CATEGORY_ICON])


def category_icon(name: str, color: QColor) -> QIcon:
    """The icon in color, for lists and menus, at 1x and 2x."""
    icon = QIcon()
    for scale in (1.0, 2.0):
        icon.addPixmap(state_pixmap(name, color, None, LIST_ICON_SIZE, scale))
    return icon


@cache
def _svg(name: str) -> bytes:
    """The icon's drawing; the plain tag for a name this version lacks."""
    if name not in CATEGORY_ICONS:
        name = DEFAULT_CATEGORY_ICON
    return (ICONS / f"{name}.svg").read_bytes()


def paint_category(painter: QPainter, name: str, color: QColor, area: QRectF) -> None:
    """The category's icon in color, filling area."""
    drawing = _svg(name).replace(b"currentColor", color.name().encode())
    QSvgRenderer(QByteArray(drawing)).render(painter, area)


def paint_app_mark(painter: QPainter, color: QColor, area: QRectF) -> None:
    """The app's icon in outline, in one colour: a note turned a little, two
    lines of writing, and the strip of tape across its top."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(area.topLeft())
    size = min(area.width(), area.height())
    painter.scale(size / 24, size / 24)  # drawn on Lucide's 24-unit grid, to match
    pen = QPen(color, 2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.translate(12, 13)
    painter.rotate(-6)
    painter.drawRoundedRect(QRectF(-8.5, -8, 17, 17), 2.5, 2.5)
    painter.drawLine(QPointF(-4.5, -1.5), QPointF(4.5, -1.5))
    painter.drawLine(QPointF(-4.5, 3), QPointF(2, 3))
    painter.restore()


def paint_tape(painter: QPainter, color: QColor, area: QRectF) -> None:
    """A strip of tape across the icon's top left corner, a little past it,
    as one holding the icon on: the note stays on top."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    size = min(area.width(), area.height())
    painter.translate(area.left() + size * 0.22, area.top() + size * 0.2)
    painter.rotate(-40)
    length, width, tear = size * 0.7, size * 0.2, size * 0.06
    strip = QPainterPath()
    strip.moveTo(-length / 2, -width / 2)
    strip.lineTo(length / 2, -width / 2)
    strip.lineTo(length / 2 - tear, 0)  # torn ends
    strip.lineTo(length / 2, width / 2)
    strip.lineTo(-length / 2, width / 2)
    strip.lineTo(-length / 2 + tear, 0)
    strip.closeSubpath()
    painter.setPen(Qt.PenStyle.NoPen)
    see_through = QColor(color)
    see_through.setAlphaF(TAPE_OPACITY)  # as tape is: the icon shows under it
    painter.setBrush(see_through)
    painter.drawPath(strip)
    painter.restore()


def state_pixmap(
    category: str | None,
    color: QColor,
    tape: QColor | None,
    size: int,
    scale: float = 1.0,
) -> QPixmap:
    """The title bar's left icon at size logical pixels: the category's (or the
    app's, for None) in color, with tape in the tape colour when on top."""
    pixels = round(size * scale)
    pixmap = QPixmap(pixels, pixels)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # Room around the icon for the tape to reach past its corner.
    inner = QRectF(pixels * 0.12, pixels * 0.12, pixels * 0.88, pixels * 0.88)
    if category is None:
        paint_app_mark(painter, color, inner)
    else:
        paint_category(painter, category, color, inner)
    if tape is not None:
        paint_tape(painter, tape, QRectF(0, 0, pixels, pixels))
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap
