"""Indicator Stickynotes (Linux): one JSON file, ~/.config/indicator-stickynotes.

    {"notes": [{"uuid", "body", "last_modified", "cat": <category id>,
                "properties": {"position": [x, y], "size": [w, h], "locked": bool}}],
     "categories": {<id>: {"name", "bgcolor_hsv": [h, s, v] (0 to 1),
                           "textcolor": [r, g, b] (0 to 1), "font"}},
     "properties": {"default_cat": <id>, "all_visible": bool}}

A note without a category (or with one no longer there) takes the default
category's look; with no colour anywhere, the app's own yellow.
"""

import colorsys
import json
from pathlib import Path
from typing import cast

from stickle.core.colors import Rgb
from stickle.importers.base import (
    Found,
    ImportedNote,
    ImportResult,
    Rect,
    SourceError,
    clean_text,
    nearest_color,
)

SOURCE = "indicator-stickynotes"
FALLBACK_HSV = (48 / 360, 1.0, 1.0)  # the app's own default yellow


def find(home: Path) -> Found | None:
    path = home / ".config" / "indicator-stickynotes"
    return Found(SOURCE, path) if path.is_file() else None


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast(dict[object, object], value).items()}


def _numbers(value: object, count: int) -> list[float] | None:
    if not isinstance(value, list):
        return None
    items = cast(list[object], value)
    if len(items) != count or not all(
        isinstance(n, int | float) and not isinstance(n, bool) for n in items
    ):
        return None
    return [float(cast(float, n)) for n in items]


def _hsv_color(hsv: list[float]) -> Rgb:
    red, green, blue = colorsys.hsv_to_rgb(*(min(1.0, max(0.0, part)) for part in hsv))
    return Rgb(round(red * 255), round(green * 255), round(blue * 255))


def read(path: Path) -> ImportResult:
    try:
        data = _mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise SourceError("the notes file could not be read") from error
    categories = {key: _mapping(value) for key, value in _mapping(data.get("categories")).items()}
    default = str(_mapping(data.get("properties")).get("default_cat") or "")
    result = ImportResult()
    notes = data.get("notes")
    for raw in cast(list[object], notes) if isinstance(notes, list) else []:
        note = _mapping(raw)
        body, uuid = note.get("body"), note.get("uuid")
        if not isinstance(body, str) or not isinstance(uuid, str):
            result.unreadable += 1
            continue
        category_id = str(note.get("cat") or "")
        own = category_id in categories
        look = categories.get(category_id if own else default, {})
        hsv = _numbers(look.get("bgcolor_hsv"), 3) or list(FALLBACK_HSV)
        background = _hsv_color(hsv)
        if look.get("font"):
            result.drop("fonts")
        if look.get("textcolor") is not None:
            result.drop("text colours")
        name = categories.get(category_id, {}).get("name") if own else None
        properties = _mapping(note.get("properties"))
        position = _numbers(properties.get("position"), 2)
        size = _numbers(properties.get("size"), 2)
        place = (
            Rect(int(position[0]), int(position[1]), int(size[0]), int(size[1]))
            if position and size and size[0] > 0 and size[1] > 0
            else None
        )
        result.notes.append(
            ImportedNote(
                source_id=uuid,
                body=clean_text(body),
                color=nearest_color(background),
                source_color=background,
                locked=properties.get("locked") is True,
                category=name.strip() if isinstance(name, str) and name.strip() else None,
                place=place,
            )
        )
    return result
