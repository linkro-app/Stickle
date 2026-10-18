"""What every importer gives: the other app's notes, as Stickle can hold them.

Importers read only. They never change, move or delete the other app's
files, so that app goes on working as before whatever happens here. What a
note cannot keep (a font, an underline) is not silently lost: it is named,
for the preview to say before anything is brought in.
"""

from dataclasses import dataclass, field
from pathlib import Path

from stickle.core.colors import PALETTE, Rgb, difference
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


def nearest_color(color: Rgb) -> str:
    """The palette colour that looks closest to color."""
    return min(PALETTE, key=lambda key: difference(PALETTE[key], color))


def clean_text(text: str) -> str:
    """Text as a note can hold it: no NUL, line ends as \\n."""
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class Found:
    """Another app's notes, found on this computer."""

    source: str  # the importer's name, as stored in import records
    path: Path
