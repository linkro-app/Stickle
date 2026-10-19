"""Microsoft's sticky notes app on Windows (versions 3 to 6): plum.sqlite.

It is in %LOCALAPPDATA%\\Packages\\Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe
\\LocalState, a SQLite database whose Note table holds, per note:

    Text            one line per paragraph, each "\\id=<uuid>" and then the
                    text, with formatting as control words in the text:
                    \\b \\b0 bold, \\i \\i0 italic, \\ul \\ul0 underline,
                    \\strike \\strike0, \\l \\l0 a bulleted list. A control
                    word ends at a space (which is not text) or at the next
                    backslash; "\\\\" is a backslash in the text. Styles carry
                    on from line to line until turned off.
    Theme           Yellow, Green, Pink, Purple, Blue, Gray or Charcoal
    WindowPosition  "…;Position=x,y;Size=w,h", or none
    IsOpen          0 for a note closed (kept in the list only)
    IsAlwaysOnTop
    DeletedAt       a deleted note (also removed outright by recent versions)

Pictures in notes (the Media table) do not come along.
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path

import apsw

from stickle.core.colors import Rgb
from stickle.importers.base import (
    Found,
    ImportedNote,
    ImportResult,
    Rect,
    SourceError,
    nearest_color,
)
from stickle.importers.rich import RichText
from stickle.importers.sqlite_copy import opened_copy, tables

SOURCE = "windows-sticky-notes"
PACKAGE = "Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe"
# The colours the app shows for each theme.
THEMES = {
    "yellow": Rgb(0xFF, 0xF7, 0xD1),
    "green": Rgb(0xE4, 0xF9, 0xE0),
    "pink": Rgb(0xFF, 0xE4, 0xF1),
    "purple": Rgb(0xF2, 0xE6, 0xFF),
    "blue": Rgb(0xE2, 0xF1, 0xFF),
    "gray": Rgb(0xF3, 0xF2, 0xF1),
    "charcoal": Rgb(0x69, 0x69, 0x69),
}
# Its own colours by name, as the nearest of Stickle's would not always be the same.
THEME_COLORS = {
    "yellow": "yellow",
    "green": "green",
    "pink": "pink",
    "purple": "lavender",
    "blue": "blue",
    "gray": "gray",
    "charcoal": "gray",
}
# The paragraph's id, and the space that ends it when text follows.
_ID = re.compile(r"^\\id=[0-9A-Fa-f-]+ ?")
_TOKEN = re.compile(r"\\\\|\\([a-z]+)(-?\d+)? ?|[^\\]+|\\")
_PLACE = re.compile(r"Position=(-?\d+),(-?\d+);Size=(\d+),(\d+)")


def find(home: Path, env: Mapping[str, str] | None = None) -> Found | None:
    env = os.environ if env is None else env
    local = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
    path = local / "Packages" / PACKAGE / "LocalState" / "plum.sqlite"
    return Found(SOURCE, path) if path.is_file() else None


def _markdown(text: str, result: ImportResult) -> str:
    rich = RichText()
    bold = italic = listing = False
    for number, line in enumerate(text.split("\n")):
        if number:
            rich.new_line()
        line = _ID.sub("", line.rstrip("\r"))
        started = len(rich.lines) - 1
        for match in _TOKEN.finditer(line):
            token = match.group(0)
            word, value = match.group(1), match.group(2)
            if token == "\\\\":
                rich.add("\\", bold, italic)
            elif word is None:
                rich.add(token, bold, italic)
            elif word in ("b", "i", "ul", "strike", "l"):
                on = value != "0"
                if word == "b":
                    bold = on
                elif word == "i":
                    italic = on
                elif word == "l":
                    listing = on
                elif on:
                    result.drop("underline" if word == "ul" else "strikethrough")
        if listing and rich.lines[started]:
            rich.markers[started] = "- "
    return rich.markdown()


def _place(window: object) -> Rect | None:
    match = _PLACE.search(window) if isinstance(window, str) else None
    if match is None:
        return None
    x, y, width, height = (int(part) for part in match.groups())
    return Rect(x, y, width, height) if width > 0 and height > 0 else None


def _rows(connection: apsw.Connection) -> list[dict[str, object]]:
    if "Note" not in tables(connection):
        raise SourceError("no notes in this database")
    columns = [str(row[1]) for row in connection.execute('PRAGMA table_info("Note")')]
    return [
        dict(zip(columns, row, strict=True)) for row in connection.execute('SELECT * FROM "Note"')
    ]


def read(path: Path) -> ImportResult:
    with opened_copy(path) as connection:
        rows = _rows(connection)
    result = ImportResult()
    for row in rows:
        text, note_id = row.get("Text"), row.get("Id")
        if row.get("DeletedAt") or not isinstance(note_id, str):
            continue
        if not isinstance(text, str):
            result.unreadable += 1
            continue
        theme = str(row.get("Theme") or "Yellow").lower()
        color = THEMES.get(theme, THEMES["yellow"])
        result.notes.append(
            ImportedNote(
                source_id=note_id,
                body=_markdown(text.replace("\x00", ""), result),
                color=THEME_COLORS.get(theme) or nearest_color(color),
                source_color=color,
                hidden=row.get("IsOpen") == 0,
                always_on_top=bool(row.get("IsAlwaysOnTop")),
                place=_place(row.get("WindowPosition")),
            )
        )
    return result
