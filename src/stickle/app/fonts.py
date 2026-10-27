"""Make sure Korean (and Chinese, Japanese) text has a font to draw with.

The system's fonts come first. Only when Korean text would come out as
empty "missing glyph" boxes, as on a minimal Linux install, is the bundled
Noto Sans CJK KR registered and added as a fallback family for the whole
application. The file is only bundled in Linux builds; Windows and macOS
always ship Korean fonts.
"""

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase, QFontMetricsF, QGuiApplication, QTextLayout

FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"
FALLBACK_FONT = FONTS_DIR / "NotoSansCJKkr-Regular.otf"
SAMPLE = "한글 입력 테스트"


def korean_is_drawable() -> bool:
    """Whether the application font, with Qt's fallbacks, has glyphs for Korean.

    Laying the text out is the direct test: font databases do not report
    writing-system support reliably on every platform plugin.
    """
    layout = QTextLayout(SAMPLE, QGuiApplication.font())
    layout.beginLayout()
    layout.createLine()
    layout.endLayout()
    runs = layout.glyphRuns()
    # Glyph 0 is the "missing glyph" box.
    return bool(runs) and all(0 not in run.glyphIndexes() for run in runs)


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
