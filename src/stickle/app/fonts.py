"""Make sure Korean (and Chinese, Japanese) text, and the alarm clock of
reminders, have a font to draw with.

The system's fonts come first. Only when Korean text would come out as
empty "missing glyph" boxes, as on a minimal Linux install, is the bundled
Noto Sans CJK KR registered and added as a fallback family for the whole
application; likewise the bundled Noto Emoji when the alarm clock would not
be drawn (Fedora's emoji font has it, in a form Qt draws as nothing). The
files are only bundled in Linux builds; Windows and macOS always ship both.
"""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QPainter,
    QTextLayout,
)

FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"
FALLBACK_FONT = FONTS_DIR / "NotoSansCJKkr-Regular.otf"
SAMPLE = "한글 입력 테스트"
EMOJI_FONT = FONTS_DIR / "NotoEmoji.ttf"
ALARM_CLOCK = "⏰"  # how reminders are written in a note, and shown in lists


def has_glyphs(text: str) -> bool:
    """Whether the application font, with Qt's fallbacks, has glyphs for text.

    Laying the text out is the direct test: font databases do not report
    writing-system support reliably on every platform plugin.
    """
    layout = QTextLayout(text, QGuiApplication.font())
    layout.beginLayout()
    layout.createLine()
    layout.endLayout()
    runs = layout.glyphRuns()
    # Glyph 0 is the "missing glyph" box.
    return bool(runs) and all(0 not in run.glyphIndexes() for run in runs)


def korean_is_drawable() -> bool:
    return has_glyphs(SAMPLE)


def draws(text: str) -> bool:
    """Whether text, drawn with the application font, leaves any mark: a font
    may have a glyph Qt cannot draw (a colour emoji format it does not read)."""
    image = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    font = QFont(QGuiApplication.font())
    font.setPixelSize(32)
    painter = QPainter(image)
    painter.setFont(font)
    painter.setPen(Qt.GlobalColor.black)
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return any(
        image.pixelColor(x, y).alpha() for y in range(image.height()) for x in range(image.width())
    )


def alarm_clock_is_drawable() -> bool:
    return has_glyphs(ALARM_CLOCK) and draws(ALARM_CLOCK)


def ensure_korean_font(fallback: Path = FALLBACK_FONT) -> str | None:
    """Register the bundled font if Korean is not drawable; return its family if used."""
    if korean_is_drawable() or not fallback.exists():
        return None
    return _add_fallback(fallback)


def ensure_alarm_clock_font(fallback: Path = EMOJI_FONT) -> str | None:
    """Register the bundled emoji font if the alarm clock is not drawn; return
    its family if used. Emoji in notes are then drawn with it too, in one colour."""
    if alarm_clock_is_drawable() or not fallback.exists():
        return None
    return _add_fallback(fallback)


def _add_fallback(fallback: Path) -> str | None:
    """The font, after the application's own: for what those cannot draw."""
    font_id = QFontDatabase.addApplicationFont(str(fallback))
    families = QFontDatabase.applicationFontFamilies(font_id)
    if not families:
        return None
    font = QGuiApplication.font()
    current = font.families() or [font.family()]
    font.setFamilies([*current, *families])
    QGuiApplication.setFont(font)
    return families[0]


CODE_FONT = FONTS_DIR / "NotoSansMono-Regular.ttf"
_code_family: str | None = None


def lines_up(font: QFont) -> bool:
    """Whether narrow and wide letters take the same width, as code needs. (Not
    the font's fixed-pitch flag: FreeType clears it for a monospaced font that
    also has some double-width glyphs, as Noto Sans Mono does.)"""
    metrics = QFontMetricsF(font)
    return metrics.horizontalAdvance("iiii") == metrics.horizontalAdvance("MMMM")


def code_family(bundled: Path = CODE_FONT) -> str:
    """The family that draws code: the system's fixed-pitch font, or where it has
    none (a minimal Linux install), the bundled Noto Sans Mono. Korean in code
    still comes from the Korean font, through Qt's fallbacks."""
    global _code_family
    if _code_family is None:
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        _code_family = fixed.family()
        if not lines_up(fixed) and bundled.exists():
            families = QFontDatabase.applicationFontFamilies(
                QFontDatabase.addApplicationFont(str(bundled))
            )
            if families:
                _code_family = families[0]
    return _code_family
