"""Zhorn Stickies (Windows), version 10: stickies.db in %APPDATA%\\stickies.

A SQLite database, as its own help (tables.xlsx) describes it:

    stickies             key, noteclass (1 desktop, 2 attached to a window,
                         3 sleeping, 5 closed, 6 stored in a folder), flags
                         (1 a picture note, 4 encrypted), title, colour (an
                         RGB value, 0x00BBGGRR), category (the folder of a
                         stored note)
    stickiesproperties   key, type, and one value of t_b, t_i, t_d, t_s:
                         1 on top, 4 read-only, 9 rolled up, 18 x, 19 y,
                         23 width, 24 height (-1 for its text's height),
                         36 hidden
    content_rtf          key, rtf: the note's text
    categories           key, name: folders of stored notes

Notes on the desktop or attached to a window come shown; sleeping, closed
and stored ones come hidden, a stored one with its folder as its category.
Picture notes and encrypted notes cannot be read and are counted as such.
"""

import os
from collections.abc import Mapping
from pathlib import Path

from stickle.core.colors import Rgb
from stickle.core.layout import Rect
from stickle.importers.base import Found, ImportedNote, ImportResult, SourceError, nearest_color
from stickle.importers.rtf import rtf_to_markdown
from stickle.importers.sqlite_copy import opened_copy, tables

SOURCE = "zhorn-stickies"
SHOWN = {1, 2, None}  # desktop, attached; None: not saved with a class yet
STORED = 6
PICTURE, ENCRYPTED = 1, 4
ON_TOP, READ_ONLY, X, Y, WIDTH, HEIGHT, HIDDEN = 1, 4, 18, 19, 23, 24, 36
AUTO_HEIGHT = 160  # for a note sized to its text (-1): a place to start from


def find(home: Path, env: Mapping[str, str] | None = None) -> Found | None:
    env = os.environ if env is None else env
    roaming = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    path = roaming / "stickies" / "stickies.db"
    return Found(SOURCE, path) if path.is_file() else None


def _colour(value: object) -> Rgb | None:
    if not isinstance(value, int) or value < 0:
        return None
    return Rgb(value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)


def _place(properties: dict[int, object]) -> Rect | None:
    x, y = properties.get(X), properties.get(Y)
    width, height = properties.get(WIDTH), properties.get(HEIGHT)
    if not (isinstance(x, int) and isinstance(y, int) and isinstance(width, int)):
        return None
    if not isinstance(height, int) or height <= 0:
        height = AUTO_HEIGHT
    return Rect(x, y, width, height) if width > 0 else None


def read(path: Path) -> ImportResult:
    with opened_copy(path) as connection:
        if not {"stickies", "stickiesproperties", "content_rtf"} <= tables(connection):
            raise SourceError("not a Stickies database")
        notes = list(
            connection.execute(
                "SELECT key, noteclass, flags, title, colour, category FROM stickies"
            )
        )
        properties: dict[int, dict[int, object]] = {}
        for key, kind, flag, number, _date, text in connection.execute(
            "SELECT key, type, t_b, t_i, t_d, t_s FROM stickiesproperties"
        ):
            if isinstance(key, int) and isinstance(kind, int):
                properties.setdefault(key, {})[kind] = next(
                    (v for v in (flag, number, text) if v is not None), None
                )
        contents = {
            key: rtf
            for key, rtf in connection.execute("SELECT key, rtf FROM content_rtf")
            if isinstance(key, int) and isinstance(rtf, str)
        }
        folders = (
            {
                key: name
                for key, name in connection.execute("SELECT key, name FROM categories")
                if isinstance(key, int) and isinstance(name, str)
            }
            if "categories" in tables(connection)
            else {}
        )
    result = ImportResult()
    for key, kind, flags, title, colour, category in notes:
        if not isinstance(key, int):
            continue
        flags = flags if isinstance(flags, int) else 0
        rtf = contents.get(key)
        if flags & (PICTURE | ENCRYPTED) or rtf is None:
            result.unreadable += 1
            if flags & PICTURE:
                result.drop("pictures")
            continue
        body = rtf_to_markdown(rtf.replace("\x00", ""), result)
        if isinstance(title, str) and title.strip() and not body.startswith(title.strip()):
            body = f"# {title.strip()}\n{body}" if body else f"# {title.strip()}"
        own = properties.get(key, {})
        background = _colour(colour)
        folder = folders.get(category) if kind == STORED and isinstance(category, int) else None
        result.notes.append(
            ImportedNote(
                source_id=str(key),
                body=body,
                color=nearest_color(background) if background else "yellow",
                source_color=background,
                hidden=kind not in SHOWN or own.get(HIDDEN) == 1,
                locked=own.get(READ_ONLY) == 1,
                always_on_top=own.get(ON_TOP) == 1,
                category=folder.strip() if folder and folder.strip() else None,
                place=_place(own),
            )
        )
    return result
