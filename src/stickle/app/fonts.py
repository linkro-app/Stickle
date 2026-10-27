"""Make sure Korean (and Chinese, Japanese) text, and the alarm clock of
reminders, have a font to draw with.

The system's fonts come first. Only when Korean text would come out as
empty "missing glyph" boxes, as on a minimal Linux install, is the bundled
Noto Sans CJK KR registered and added as a fallback family for the whole
application. The files are only bundled in Linux builds; Windows and macOS
always ship Korean fonts, and the alarm clock.

The alarm clock (U+23F0) is an emoji: Qt draws it with the system's emoji
font whatever font it is given, and on Fedora that font's form of it comes
out as nothing. Asked for as a text character (U+FE0E after it), it is drawn
with the font given. So where it would not show, the bundled Noto Emoji is
registered, and where Stickle writes the alarm clock itself it gives it
that font, for that character alone: added to the families of all text, it
took spaces and digits too, set wide apart, on Fedora.
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
ALARM_CLOCK = "⏰"
AS_TEXT = chr(0xFE0E)  # after an emoji: drawn as a text character, with the font given
_alarm_clock_family: str | None = None  # the bundled font, where it is needed


def has_glyphs(text: str, font: QFont | None = None) -> bool:
    """Whether the font (the application's), with Qt's fallbacks, has glyphs for text.

    Laying the text out is the direct test: font databases do not report
    writing-system support reliably on every platform plugin.
    """
    layout = QTextLayout(text, font or QGuiApplication.font())
    layout.beginLayout()
    layout.createLine()
    layout.endLayout()
    runs = layout.glyphRuns()
    # Glyph 0 is the "missing glyph" box.
    return bool(runs) and all(0 not in run.glyphIndexes() for run in runs)


def korean_is_drawable() -> bool:
    return has_glyphs(SAMPLE)


def draws(text: str, font: QFont | None = None) -> bool:
    """Whether text, drawn with the font (the application's), leaves any mark:
    a font may have a glyph Qt cannot draw (a colour emoji form it does not read)."""
    image = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    drawn = QFont(font or QGuiApplication.font())
    drawn.setPixelSize(32)
    painter = QPainter(image)
    painter.setFont(drawn)
    painter.setPen(Qt.GlobalColor.black)
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return any(
        image.pixelColor(x, y).alpha() for y in range(image.height()) for x in range(image.width())
    )


def alarm_clock_is_drawable() -> bool:
    """Whether the alarm clock shows as it is, in the system's fonts."""
    return has_glyphs(ALARM_CLOCK) and draws(ALARM_CLOCK)


def ensure_alarm_clock_font(bundled: Path = EMOJI_FONT) -> str | None:
    """Register the bundled emoji font if the alarm clock would not show; return
    its family if so. It is only used where alarm_clock_family says."""
    global _alarm_clock_family
    if alarm_clock_is_drawable() or not bundled.exists():
        return None
    families = QFontDatabase.applicationFontFamilies(QFontDatabase.addApplicationFont(str(bundled)))
    _alarm_clock_family = families[0] if families else None
    return _alarm_clock_family


def alarm_clock_family() -> str | None:
    """The font to draw the alarm clock with, as a text character (ALARM_CLOCK +
    AS_TEXT); None where it shows as it is."""
    return _alarm_clock_family


def ensure_korean_font(fallback: Path = FALLBACK_FONT) -> str | None:
    """Register the bundled font if Korean is not drawable; return its family if used."""
    if korean_is_drawable() or not fallback.exists():
        return None
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
