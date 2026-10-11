"""Note colours on screen: Qt colours, translated names and menu swatches."""

from functools import cache

from PySide6.QtCore import QCoreApplication, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from stickle.core.colors import PALETTE, Rgb, colors_for

SWATCH_SIZE = 14


def qcolor(color: Rgb) -> QColor:
    return QColor(color.red, color.green, color.blue)


def rgb(color: QColor) -> Rgb:
    return Rgb(color.red(), color.green(), color.blue())


def color_name(key: str) -> str:
    """The palette colour's name in the interface language (the key if unknown)."""
    names = {
        "yellow": QCoreApplication.translate("NoteColor", "Yellow"),
        "apricot": QCoreApplication.translate("NoteColor", "Apricot"),
        "coral": QCoreApplication.translate("NoteColor", "Coral"),
        "pink": QCoreApplication.translate("NoteColor", "Pink"),
        "lavender": QCoreApplication.translate("NoteColor", "Lavender"),
        "blue": QCoreApplication.translate("NoteColor", "Blue"),
        "sky": QCoreApplication.translate("NoteColor", "Sky"),
        "mint": QCoreApplication.translate("NoteColor", "Mint"),
        "green": QCoreApplication.translate("NoteColor", "Green"),
        "sand": QCoreApplication.translate("NoteColor", "Sand"),
        "gray": QCoreApplication.translate("NoteColor", "Gray"),
        "white": QCoreApplication.translate("NoteColor", "White"),
    }
    return names.get(key, key)


@cache  # every note's menu shows the same twelve
def swatch_icon(key: str) -> QIcon:
    """A small square of the colour with its border, drawn at 1x and 2x."""
    colors = colors_for(PALETTE[key])
    icon = QIcon()
    for scale in (1, 2):
        size = SWATCH_SIZE * scale
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(qcolor(colors.border), scale))
        painter.setBrush(qcolor(colors.background))
        inset = scale / 2
        square = QRectF(inset, inset, size - scale, size - scale)
        painter.drawRoundedRect(square, 2 * scale, 2 * scale)
        painter.end()
        icon.addPixmap(pixmap)
    return icon
