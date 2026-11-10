"""The app's icon: a note held to the desktop by a strip of tape.

Drawn in code at each size rather than scaled from one picture, so that it
stays sharp from the tray (16 px) to the Store and app lists (512 px). On a
computer that syncs, the tray's has a corner of the note lifted on a cloud.
"""

from enum import Enum

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256, 512)
TRAY_SIZES = (16, 20, 24, 32, 40, 48, 64)
UNITS = 512  # the drawing's own grid; every size is this, scaled
FADED = 0.4  # how much of the icon shows while the notes are hidden for now

NOTE = QColor("#ffd43b")
NOTE_SHADE = QColor(245, 184, 0, 140)  # the note's lower edge, a little deeper
LINES = QColor("#7a5a00")
TAPE = QColor(95, 211, 179, 225)  # see-through mint
TAPE_EDGE = QColor(255, 255, 255, 128)
FOLD = QColor("#e0a100")  # the back of the lifted corner
SKY = QColor("#3b82f6")
CLOUD = QColor("#ffffff")
HALTED = QColor("#f08c00")

CORNER = 124  # how far up and along the note its lifted corner goes, in units
SMALL_CORNER = 190  # the same where it is only a few pixels: larger, to be made out
SMALL = 40  # pixels below which the corner is drawn that way
AWAY = 0.4  # how much of the corner shows with no network
HALTED_AT = QPointF(412, 392)  # the dot's centre
HALTED_RADIUS = 58
HALO = 22  # clear around the dot, so that it stands off what is under it
FLAP = 0.5  # how far the lifted flap reaches towards the corner of the square it leaves


class SyncSign(Enum):
    """What the tray's icon says of sync, on a computer that syncs."""

    ON = "on"
    OFFLINE = "offline"
    HALTED = "halted"  # stopped until the user does something, or the cloud is full


def _lifted_corner(painter: QPainter, size: int, sign: SyncSign) -> None:
    """The note's bottom right corner lifted, sky and a cloud under it. In the
    note's own frame."""
    corner = CORNER if size >= SMALL else SMALL_CORNER
    right, bottom = 426.0, 444.0
    up, along = QPointF(right, bottom - corner), QPointF(right - corner, bottom)
    away = painter.opacity()
    if sign is SyncSign.OFFLINE:
        painter.setOpacity(away * AWAY)
    painter.setBrush(SKY)
    painter.drawPolygon([up, along, QPointF(right, bottom)])
    # The cloud, in the corner, as large as the corner is.
    scale = corner / CORNER
    cloud = QPainterPath(QPointF(362, 432))
    for control, end in (
        ((340, 432), (340, 412)),
        ((340, 394), (358, 392)),
        ((363, 372), (385, 372)),
        ((403, 372), (410, 388)),
        ((430, 388), (430, 408)),
        ((430, 432), (408, 432)),
    ):
        cloud.quadTo(QPointF(*control), QPointF(*end))
    cloud.closeSubpath()
    painter.save()
    painter.translate(right, bottom)
    painter.scale(scale, scale)
    painter.translate(-right, -bottom)
    painter.setBrush(CLOUD)
    painter.drawPath(cloud)
    painter.restore()
    painter.setOpacity(away)
    painter.setBrush(FOLD)
    middle = (up + along) / 2
    inner = QPointF(right - corner, bottom - corner)
    painter.drawPolygon([up, along, middle + (inner - middle) * FLAP])


def paint_halted(painter: QPainter, centre: QPointF, radius: float) -> None:
    """The dot that says sync stopped: on the tray's icon, and before its line."""
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(HALTED)
    painter.drawEllipse(centre, radius, radius)


def halted_pixmap(size: int, scale: float = 1.0) -> QPixmap:
    """The dot alone, at size logical pixels."""
    pixels = round(size * scale)
    pixmap = QPixmap(pixels, pixels)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    paint_halted(painter, QPointF(pixels / 2, pixels / 2), pixels * 0.32)
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap


def halted_icon() -> QIcon:
    """The dot, for a menu."""
    icon = QIcon()
    for size in (16, 20, 24, 32):
        icon.addPixmap(halted_pixmap(size))
    return icon


def paint_icon(painter: QPainter, size: int, sign: SyncSign | None = None) -> None:
    """The icon in a size x size square, at the painter's origin; sign: what it
    says of sync, on a computer that syncs."""
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
    if sign is not None:
        _lifted_corner(painter, size, sign)
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
    if sign is SyncSign.HALTED:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(size / UNITS, size / UNITS)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.GlobalColor.black)
        painter.drawEllipse(HALTED_AT, HALTED_RADIUS + HALO, HALTED_RADIUS + HALO)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        paint_halted(painter, HALTED_AT, HALTED_RADIUS)
        painter.restore()


def _turn(painter: QPainter, degrees: float, around: QPointF) -> None:
    painter.translate(around)
    painter.rotate(degrees)
    painter.translate(-around)


def icon_pixmap(size: int, opacity: float = 1.0, sign: SyncSign | None = None) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setOpacity(opacity)
    paint_icon(painter, size, sign)
    painter.end()
    return pixmap


def make_icon(faded: bool = False, sign: SyncSign | None = None) -> QIcon:
    """The app's icon, drawn at every size it is shown at; faded while the
    notes are hidden for now (in the tray); sign: what the tray's says of sync,
    at the sizes a tray shows."""
    icon = QIcon()
    for size in SIZES if sign is None else TRAY_SIZES:
        icon.addPixmap(icon_pixmap(size, FADED if faded else 1.0, sign))
    return icon
