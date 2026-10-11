"""The app's icon: a note held to the desktop by a strip of tape.

Drawn in code at each size rather than scaled from one picture, so that it
stays sharp from the tray (16 px) to the Store and app lists (512 px).
"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256, 512)
UNITS = 512  # the drawing's own grid; every size is this, scaled
FADED = 0.4  # how much of the icon shows while the notes are hidden for now

NOTE = QColor("#ffd43b")
NOTE_SHADE = QColor(245, 184, 0, 140)  # the note's lower edge, a little deeper
LINES = QColor("#7a5a00")
TAPE = QColor(95, 211, 179, 225)  # see-through mint
TAPE_EDGE = QColor(255, 255, 255, 128)


def paint_icon(painter: QPainter, size: int) -> None:
    """The icon in a size x size square, at the painter's origin."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / UNITS, size / UNITS)
    painter.setPen(Qt.PenStyle.NoPen)

    # The note, turned a little, as one stuck on by hand.
    painter.save()
    _turn(painter, -6, QPointF(256, 276))
    painter.setBrush(NOTE)
    painter.drawRoundedRect(QRectF(86, 104, 340, 340), 28, 28)
    painter.setBrush(NOTE_SHADE)
    painter.drawRoundedRect(QRectF(86, 380, 340, 64), 28, 28)
    painter.setBrush(LINES)
    painter.drawRoundedRect(QRectF(140, 214, 232, 30), 15, 15)
    painter.drawRoundedRect(QRectF(140, 278, 170, 30), 15, 15)
    painter.restore()

    # The tape across its top, turned the other way.
    _turn(painter, 8, QPointF(256, 108))
    painter.setBrush(TAPE)
    painter.drawRoundedRect(QRectF(170, 70, 172, 76), 6, 6)
    if size >= 48:  # its torn edge, too fine to see smaller
        edge = QPainterPath(QPointF(170, 70))
        for step in range(1, 7):
            edge.lineTo(170 + (10 if step % 2 else 0), 70 + step * 12.6)
        painter.setPen(QPen(TAPE_EDGE, 3))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(edge)
    painter.restore()


def _turn(painter: QPainter, degrees: float, around: QPointF) -> None:
    painter.translate(around)
    painter.rotate(degrees)
    painter.translate(-around)


def icon_pixmap(size: int, opacity: float = 1.0) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setOpacity(opacity)
    paint_icon(painter, size)
    painter.end()
    return pixmap


def make_icon(faded: bool = False) -> QIcon:
    """The app's icon, drawn at every size it is shown at; faded while the
    notes are hidden for now (in the tray)."""
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(icon_pixmap(size, FADED if faded else 1.0))
    return icon
