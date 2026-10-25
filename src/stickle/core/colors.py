"""Note colours: the palette, and every other colour worked out from a background.

Only the background's key is stored ("yellow"), or for a colour the user
added, that colour itself ("#a3c9f0"), never the colours drawn:
the title bar, border, text and highlight colours follow from it, so a
later change to these rules (or a dark theme) applies to existing notes as
well. Text gets whichever of dark or light contrasts more, at least 4.5:1
(WCAG AA); a background that cannot reach that is darkened or lightened
until it does.
"""

import colorsys
import math
import re
from dataclasses import dataclass

DEFAULT_COLOR = "yellow"
# A colour the user added, stored as itself rather than a key: lower case only.
HEX = re.compile(r"#[0-9a-f]{6}")
MIN_CONTRAST = 4.5
ICON_CONTRAST = 3.0  # WCAG for icons and other graphics
QUIET_ICON = 0.6  # title bar icons at rest: this much of the text colour, if it reads


@dataclass(frozen=True)
class Rgb:
    red: int
    green: int
    blue: int

    @classmethod
    def from_hex(cls, value: str) -> Rgb:
        value = value.removeprefix("#")
        return cls(int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))

    @property
    def hex(self) -> str:
        return f"#{self.red:02x}{self.green:02x}{self.blue:02x}"

    def _hls(self) -> tuple[float, float, float]:
        return colorsys.rgb_to_hls(self.red / 255, self.green / 255, self.blue / 255)

    @classmethod
    def _from_hls(cls, hue: float, lightness: float, saturation: float) -> Rgb:
        red, green, blue = colorsys.hls_to_rgb(hue, min(max(lightness, 0.0), 1.0), saturation)
        return cls(round(red * 255), round(green * 255), round(blue * 255))

    def lighter(self, amount: float) -> Rgb:
        """Lightness moved by amount (negative: darker), on a 0-1 scale."""
        hue, lightness, saturation = self._hls()
        return Rgb._from_hls(hue, lightness + amount, saturation)

    def over(self, background: Rgb, alpha: float) -> Rgb:
        """This colour painted over background with the given opacity."""

        def mix(top: int, bottom: int) -> int:
            return round(top * alpha + bottom * (1 - alpha))

        return Rgb(
            mix(self.red, background.red),
            mix(self.green, background.green),
            mix(self.blue, background.blue),
        )


# Pastel, in the order the menu shows them. Keys are stored; names are translated in the UI.
PALETTE: dict[str, Rgb] = {
    key: Rgb.from_hex(value)
    for key, value in (
        ("yellow", "#ffec8c"),
        ("apricot", "#ffd5a3"),
        ("coral", "#ffbfae"),
        ("pink", "#ffc8dd"),
        ("lavender", "#e3cdf5"),
        ("blue", "#c3d8ff"),
        ("sky", "#bfeaf8"),
        ("mint", "#bff0dc"),
        ("green", "#d8f0b4"),
        ("sand", "#ece0c9"),
        ("gray", "#e2e2e2"),
        ("white", "#fbfbf8"),
    )
}

DARK_TEXT = Rgb(32, 32, 32)
LIGHT_TEXT = Rgb(255, 255, 255)
# Highlight colours in order of preference: the first that stands out enough on the note.
HIGHLIGHTS = (Rgb.from_hex("#ffe14d"), Rgb.from_hex("#ffab5e"), Rgb.from_hex("#8fd3ff"))
HIGHLIGHT_DIFFERENCE = 30  # delta E: clearly a different colour at a glance
# How much of the text colour is mixed into the background: a shade, not a new hue.
TITLE_BAR_SHADE = 0.07
BORDER_SHADE = 0.22
CODE_SHADE = 0.09


def _linear(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def luminance(color: Rgb) -> float:
    """WCAG relative luminance."""
    return (
        0.2126 * _linear(color.red) + 0.7152 * _linear(color.green) + 0.0722 * _linear(color.blue)
    )


def contrast(first: Rgb, second: Rgb) -> float:
    """WCAG contrast ratio, from 1 to 21."""
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def text_on(background: Rgb) -> Rgb:
    """Dark or light text, whichever is easier to read on background."""
    return max((DARK_TEXT, LIGHT_TEXT), key=lambda text: contrast(text, background))


def readable(background: Rgb, text: Rgb | None = None) -> Rgb:
    """background, moved away from text (by default its own best text colour)
    until that text contrasts enough on it."""
    text = text or text_on(background)
    step = 0.02 if text == DARK_TEXT else -0.02
    for _ in range(50):
        if contrast(text, background) >= MIN_CONTRAST:
            break
        background = background.lighter(step)
    return background


def quiet(color: Rgb, background: Rgb) -> Rgb:
    """color faded towards background, but no further than icons stay clear (3:1)."""
    alpha = QUIET_ICON
    while alpha < 1 and contrast(color.over(background, alpha), background) < ICON_CONTRAST:
        alpha = min(1.0, alpha + 0.05)
    return color.over(background, alpha)


def clear_on(color: Rgb, background: Rgb) -> Rgb:
    """color, deepened (or lightened on a dark background) just until it shows
    as an icon does (3:1), keeping its hue."""
    step = -0.04 if luminance(background) > 0.18 else 0.04
    for _ in range(40):
        if contrast(color, background) >= ICON_CONTRAST:
            break
        color = color.lighter(step)
    return color


def _lab(color: Rgb) -> tuple[float, float, float]:
    """CIE L*a*b* (D65), to judge how different two colours look."""
    red, green, blue = (_linear(c) for c in (color.red, color.green, color.blue))
    x = (0.4124 * red + 0.3576 * green + 0.1805 * blue) / 0.95047
    y = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    z = (0.0193 * red + 0.1192 * green + 0.9505 * blue) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def difference(first: Rgb, second: Rgb) -> float:
    """How different two colours look (CIE76 delta E; about 2 is just noticeable)."""
    return math.dist(_lab(first), _lab(second))


@dataclass(frozen=True)
class NoteColors:
    background: Rgb
    title_bar: Rgb
    border: Rgb
    text: Rgb
    title_text: Rgb
    title_icon: Rgb  # title bar icons at rest: quieter than the text, still clear
    highlight: Rgb
    code_background: Rgb


def colors_for(background: Rgb) -> NoteColors:
    background = readable(background)
    text = text_on(background)
    # Shaded towards the text: darker on a light note, lighter on a dark one.
    title_bar = readable(text.over(background, TITLE_BAR_SHADE))
    highlight = next(
        (color for color in HIGHLIGHTS if difference(color, background) >= HIGHLIGHT_DIFFERENCE),
        max(HIGHLIGHTS, key=lambda color: difference(color, background)),
    )
    return NoteColors(
        background=background,
        title_bar=title_bar,
        border=text.over(background, BORDER_SHADE),
        text=text,
        title_text=text_on(title_bar),
        title_icon=quiet(text_on(title_bar), title_bar),
        # The note's text is written on these, so they must suit it.
        highlight=readable(highlight, text),
        code_background=readable(text.over(background, CODE_SHADE), text),
    )


def is_hex(value: str) -> bool:
    """Whether value is a colour of the user's own as stored: #rrggbb, lower case."""
    return HEX.fullmatch(value) is not None


def is_color(value: str) -> bool:
    """A palette key or a colour of the user's own."""
    return value in PALETTE or is_hex(value)


def background_of(key: str) -> Rgb:
    """The background a note stored with key asks for: a palette colour, or one
    of the user's own. An unknown key (written by a newer version, say) asks
    for the default colour; the stored key is left as it is."""
    if is_hex(key):
        return Rgb.from_hex(key)
    return PALETTE.get(key, PALETTE[DEFAULT_COLOR])


def needs_moving(background: Rgb) -> bool:
    """Whether text cannot reach 4.5:1 on background as it is, so that the note
    is drawn a little lighter or darker than chosen."""
    return readable(background) != background


def note_colors(key: str) -> NoteColors:
    """The colours of a note stored with key (see background_of)."""
    return colors_for(background_of(key))
