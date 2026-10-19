"""What every importer gives: the other app's notes, as Stickle can hold them.

Importers read only. They never change, move or delete the other app's
files, so that app goes on working as before whatever happens here. What a
note cannot keep (a font, an underline) is not silently lost: it is named,
for the preview to say before anything is brought in.
"""

import colorsys
from dataclasses import dataclass, field
from pathlib import Path

from stickle.core.colors import PALETTE, Rgb
from stickle.core.layout import Rect

__all__ = ["Found", "ImportResult", "ImportedNote", "Rect", "SourceError"]


@dataclass(frozen=True)
class ImportedNote:
    source_id: str  # the other app's own id for the note, for not bringing it twice
    body: str  # Markdown
    color: str  # a palette key, the nearest to the note's own colour
    source_color: Rgb | None = None  # the colour it had, for the preview
    hidden: bool = False
    locked: bool = False
    always_on_top: bool | None = None  # None: the other app does not say
    category: str | None = None  # by name
    place: Rect | None = None  # where it was, in screen pixels


@dataclass
class ImportResult:
    notes: list[ImportedNote] = field(default_factory=list[ImportedNote])
    # Formatting that does not come along, in plain words for the preview
    # ("underline", "fonts"), each named once.
    dropped: list[str] = field(default_factory=list[str])
    # Notes that could not be read (their file was damaged, say), by count.
    unreadable: int = 0

    def drop(self, what: str) -> None:
        if what not in self.dropped:
            self.dropped.append(what)


class SourceError(Exception):
    """The other app's data could not be read at all."""


GREYS = ("gray", "white")


def _hsv(color: Rgb) -> tuple[float, float, float]:
    return colorsys.rgb_to_hsv(color.red / 255, color.green / 255, color.blue / 255)


def nearest_color(color: Rgb) -> str:
    """The palette colour of the same kind: by hue first, then by how strong.

    Other apps' backgrounds are paler or stronger than Stickle's pastels, so
    plain closeness takes a pale pink or blue for grey. Nearly colourless
    ones become grey or white, dark ones grey.
    """
    hue, saturation, value = _hsv(color)
    if value < 0.5 or saturation < 0.05:
        return "white" if value >= 0.95 else "gray"

    def distance(key: str) -> float:
        other_hue, other_saturation, _ = _hsv(PALETTE[key])
        turn = abs(hue - other_hue) % 1.0
        return min(turn, 1 - turn) * 360 + abs(saturation - other_saturation) * 60

    return min((key for key in PALETTE if key not in GREYS), key=distance)


def clean_text(text: str) -> str:
    """Text as a note can hold it: no NUL, line ends as \\n."""
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class Found:
    """Another app's notes, found on this computer."""

    source: str  # the importer's name, as stored in import records
    path: Path
