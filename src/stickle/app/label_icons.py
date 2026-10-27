"""The icons users choose for their categories and marks, and the icon on the
left of a note's title bar.

Categories and marks show as icons the user chose from a set kept with the
app (from Lucide, in icons/lucide, ISC licence). What the app itself shows of
a note's state is drawn by the app instead: on the left, a note without a
category shows the app's own icon in one colour, and a strip of tape across
the icon's corner says the note stays on top. The left icon only shows the
note's state: it is changed from the note's menu and the pin.
"""

from functools import cache
from pathlib import Path
from typing import Literal

from PySide6.QtCore import QByteArray, QCoreApplication, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer

from stickle.core.labels import DEFAULT_ICON, LABEL_ICONS

ICONS = Path(__file__).resolve().parent / "icons" / "lucide"
TAPE_OPACITY = 0.7
LIST_ICON_SIZE = 16
BOLD_STROKE = b'stroke-width="2.6"'  # on the 24-unit grid; Lucide draws at 2


def icon_label(name: str) -> str:
    """What the icon shows, in the interface language: its tooltip and its name
    for screen readers where it is chosen."""
    labels = {
        "tag": QCoreApplication.translate("LabelIcon", "Tag"),
        "folder": QCoreApplication.translate("LabelIcon", "Folder"),
        "briefcase": QCoreApplication.translate("LabelIcon", "Briefcase"),
        "building-2": QCoreApplication.translate("LabelIcon", "Office"),
        "house": QCoreApplication.translate("LabelIcon", "Home"),
        "users": QCoreApplication.translate("LabelIcon", "People"),
        "user": QCoreApplication.translate("LabelIcon", "Person"),
        "baby": QCoreApplication.translate("LabelIcon", "Baby"),
        "heart": QCoreApplication.translate("LabelIcon", "Heart"),
        "heart-pulse": QCoreApplication.translate("LabelIcon", "Health"),
        "stethoscope": QCoreApplication.translate("LabelIcon", "Doctor"),
        "pill": QCoreApplication.translate("LabelIcon", "Medicine"),
        "dumbbell": QCoreApplication.translate("LabelIcon", "Exercise"),
        "utensils": QCoreApplication.translate("LabelIcon", "Food"),
        "coffee": QCoreApplication.translate("LabelIcon", "Coffee"),
        "shopping-cart": QCoreApplication.translate("LabelIcon", "Shopping"),
        "wallet": QCoreApplication.translate("LabelIcon", "Wallet"),
        "piggy-bank": QCoreApplication.translate("LabelIcon", "Savings"),
        "credit-card": QCoreApplication.translate("LabelIcon", "Card"),
        "receipt": QCoreApplication.translate("LabelIcon", "Receipt"),
        "book-open": QCoreApplication.translate("LabelIcon", "Reading"),
        "graduation-cap": QCoreApplication.translate("LabelIcon", "Study"),
        "pencil": QCoreApplication.translate("LabelIcon", "Writing"),
        "file-text": QCoreApplication.translate("LabelIcon", "Document"),
        "list-checks": QCoreApplication.translate("LabelIcon", "Checklist"),
        "calendar": QCoreApplication.translate("LabelIcon", "Calendar"),
        "clock": QCoreApplication.translate("LabelIcon", "Time"),
        "alarm-clock": QCoreApplication.translate("LabelIcon", "Alarm"),
        "mail": QCoreApplication.translate("LabelIcon", "Mail"),
        "message-circle": QCoreApplication.translate("LabelIcon", "Message"),
        "phone": QCoreApplication.translate("LabelIcon", "Phone"),
        "smartphone": QCoreApplication.translate("LabelIcon", "Mobile"),
        "laptop": QCoreApplication.translate("LabelIcon", "Computer"),
        "code": QCoreApplication.translate("LabelIcon", "Code"),
        "terminal": QCoreApplication.translate("LabelIcon", "Terminal"),
        "flask-conical": QCoreApplication.translate("LabelIcon", "Science"),
        "wrench": QCoreApplication.translate("LabelIcon", "Repairs"),
        "hammer": QCoreApplication.translate("LabelIcon", "Tools"),
        "plane": QCoreApplication.translate("LabelIcon", "Travel"),
        "car": QCoreApplication.translate("LabelIcon", "Car"),
        "bus": QCoreApplication.translate("LabelIcon", "Transit"),
        "map-pin": QCoreApplication.translate("LabelIcon", "Place"),
        "gift": QCoreApplication.translate("LabelIcon", "Gift"),
        "music": QCoreApplication.translate("LabelIcon", "Music"),
        "camera": QCoreApplication.translate("LabelIcon", "Photos"),
        "film": QCoreApplication.translate("LabelIcon", "Films"),
        "gamepad-2": QCoreApplication.translate("LabelIcon", "Games"),
        "palette": QCoreApplication.translate("LabelIcon", "Art"),
        "lightbulb": QCoreApplication.translate("LabelIcon", "Ideas"),
        "sparkles": QCoreApplication.translate("LabelIcon", "Sparkles"),
        "star": QCoreApplication.translate("LabelIcon", "Star"),
        "flag": QCoreApplication.translate("LabelIcon", "Flag"),
        "target": QCoreApplication.translate("LabelIcon", "Goals"),
        "trophy": QCoreApplication.translate("LabelIcon", "Trophy"),
        "rocket": QCoreApplication.translate("LabelIcon", "Projects"),
        "bookmark": QCoreApplication.translate("LabelIcon", "Bookmark"),
        "archive": QCoreApplication.translate("LabelIcon", "Archive"),
        "lock": QCoreApplication.translate("LabelIcon", "Private"),
        "key": QCoreApplication.translate("LabelIcon", "Key"),
        "leaf": QCoreApplication.translate("LabelIcon", "Nature"),
        "sun": QCoreApplication.translate("LabelIcon", "Sun"),
        "cloud": QCoreApplication.translate("LabelIcon", "Cloud"),
        "dog": QCoreApplication.translate("LabelIcon", "Dog"),
        "cat": QCoreApplication.translate("LabelIcon", "Cat"),
        "square-check": QCoreApplication.translate("LabelIcon", "Checkbox"),
        "triangle-alert": QCoreApplication.translate("LabelIcon", "Warning"),
        "circle-help": QCoreApplication.translate("LabelIcon", "Question"),
    }
    return labels.get(name, labels[DEFAULT_ICON])


def label_icon(name: str, color: QColor) -> QIcon:
    """The icon in color, for lists and menus, at 1x and 2x."""
    icon = QIcon()
    for scale in (1.0, 2.0):
        icon.addPixmap(state_pixmap(name, color, None, LIST_ICON_SIZE, scale))
    return icon


@cache
def _svg(name: str) -> bytes:
    """The icon's drawing; the plain tag for a name this version lacks."""
    if name not in LABEL_ICONS:
        name = DEFAULT_ICON
    return (ICONS / f"{name}.svg").read_bytes()


def paint_label(
    painter: QPainter, name: str, color: QColor, area: QRectF, bold: bool = False
) -> None:
    """The icon in color, filling area; bold draws its lines heavier, for a
    mark, which must stand out at the title bar's small size."""
    drawing = _svg(name).replace(b"currentColor", color.name().encode())
    if bold:
        drawing = drawing.replace(b'stroke-width="2"', BOLD_STROKE)
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


def paint_keyhole(painter: QPainter, color: QColor, area: QRectF, filled: bool) -> None:
    """A keyhole, a secret note's sign: filled while it is locked, in outline
    while it is open (it locks again by itself)."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(area.topLeft())
    size = min(area.width(), area.height())
    painter.scale(size / 24, size / 24)  # Lucide's 24-unit grid, as the app's icon
    shape = QPainterPath()
    shape.addEllipse(QPointF(12, 8.5), 5, 5)
    stem = QPainterPath()
    stem.moveTo(10, 11)
    stem.lineTo(8, 21)
    stem.lineTo(16, 21)
    stem.lineTo(14, 11)
    stem.closeSubpath()
    pen = QPen(color, 2)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(color if filled else Qt.BrushStyle.NoBrush)
    painter.drawPath(shape.united(stem))
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


def paint_alarm(painter: QPainter, color: QColor, area: QRectF) -> None:
    """A small alarm clock at the icon's bottom right corner, cut out of what is
    under it: the note has a reminder coming."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    size = min(area.width(), area.height())
    radius = size * 0.22
    centre = QPointF(area.right() - radius - size * 0.03, area.bottom() - radius - size * 0.03)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(Qt.GlobalColor.black)
    painter.drawEllipse(centre, radius + size * 0.07, radius + size * 0.07)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    pen = QPen(color, max(1.0, size * 0.07))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(centre, radius, radius)
    painter.drawLine(centre, centre + QPointF(0, -radius * 0.55))
    painter.drawLine(centre, centre + QPointF(radius * 0.45, 0))
    # Its two bells, on top.
    for side in (-1, 1):
        bell = centre + QPointF(side * radius * 0.85, -radius * 0.95)
        painter.drawPoint(bell)
    painter.restore()


def state_pixmap(
    category: str | None,
    color: QColor,
    tape: QColor | None,
    size: int,
    scale: float = 1.0,
    secret: Literal["locked", "open"] | None = None,
    alarm: bool = False,
) -> QPixmap:
    """The title bar's left icon at size logical pixels: the category's (or the
    app's, for None) in color, with tape in the tape colour when on top and a
    small alarm clock with a reminder coming. A secret note shows a keyhole
    instead, whatever its category."""
    pixels = round(size * scale)
    pixmap = QPixmap(pixels, pixels)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # Room around the icon for the tape to reach past its corner.
    inner = QRectF(pixels * 0.12, pixels * 0.12, pixels * 0.88, pixels * 0.88)
    if secret is not None:
        paint_keyhole(painter, color, inner, filled=secret == "locked")
    elif category is None:
        paint_app_mark(painter, color, inner)
    else:
        paint_label(painter, category, color, inner)
    if tape is not None:
        paint_tape(painter, tape, QRectF(0, 0, pixels, pixels))
    if alarm:
        paint_alarm(painter, color, QRectF(0, 0, pixels, pixels))
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap
