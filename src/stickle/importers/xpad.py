"""xpad (Linux): one info-* file per note, naming its content-* file.

The folder is $XDG_CONFIG_HOME/xpad (~/.config/xpad), or ~/.xpad for old
versions. An info file has one "key value" per line: width, height, x, y,
sticky (on every workspace), hidden, the colours (back and text, written
"rgb(r,g,b)" since xpad 5, or back_red/back_green/back_blue in 16 bits
before), fontname and content (the content file's name). The content is the
text, with bold, italic, underline and strikethrough marked by the private
character U+E000 around the tag's name ("\\ue000bold\\ue000…\\ue000/bold\\ue000").
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path

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

SOURCE = "xpad"
TAG = ""
_TAGGED = re.compile(f"{TAG}(/?)([a-z]+){TAG}")
_RGB = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")
MARKS = {"bold": "**", "italic": "*"}
DROPPED_TAGS = {"underline": "underline", "strikethrough": "strikethrough"}


def find(home: Path, env: Mapping[str, str] | None = None) -> Found | None:
    env = os.environ if env is None else env
    config = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    for folder in (config / "xpad", home / ".xpad"):
        if any(folder.glob("info-*")):
            return Found(SOURCE, folder)
    return None


def _info(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key:
            values[key] = value.strip()
    return values


def _flag(values: dict[str, str], key: str) -> bool:
    try:
        return int(values.get(key, "0")) != 0
    except ValueError:
        return False


def _number(values: dict[str, str], key: str) -> int | None:
    try:
        return int(values[key])
    except KeyError, ValueError:
        return None


def _background(values: dict[str, str]) -> Rgb | None:
    if match := _RGB.match(values.get("back", "")):
        red, green, blue = (min(255, int(part)) for part in match.groups())
        return Rgb(red, green, blue)
    parts = [_number(values, f"back_{name}") for name in ("red", "green", "blue")]
    if all(part is not None for part in parts):
        red, green, blue = (min(255, (part or 0) >> 8) for part in parts)
        return Rgb(red, green, blue)
    return None


def _place(values: dict[str, str]) -> Rect | None:
    x, y = _number(values, "x"), _number(values, "y")
    width, height = _number(values, "width"), _number(values, "height")
    if x is None or y is None or width is None or height is None or width <= 0 or height <= 0:
        return None
    return Rect(x, y, width, height)


def _markdown(content: str, result: ImportResult) -> str:
    """The content's text, with bold and italic as Markdown; other tags dropped."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(2)
        if name in MARKS:
            return MARKS[name]
        if name in DROPPED_TAGS:
            result.drop(DROPPED_TAGS[name])
        return ""

    return _TAGGED.sub(replace, content).replace(TAG, "")


def read(folder: Path) -> ImportResult:
    if not folder.is_dir():
        raise SourceError("no xpad folder")
    result = ImportResult()
    for info_file in sorted(folder.glob("info-*")):
        try:
            values = _info(info_file.read_text(encoding="utf-8", errors="replace"))
            name = Path(values.get("content", "")).name
            if not name.startswith("content-"):
                raise OSError("no content file named")
            content = (folder / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            result.unreadable += 1
            continue
        if not _flag(values, "follow_font") and values.get("fontname"):
            result.drop("fonts")
        if not _flag(values, "follow_color") and values.get("text", values.get("text_red")):
            result.drop("text colours")
        background = None if _flag(values, "follow_color") else _background(values)
        result.notes.append(
            ImportedNote(
                source_id=info_file.name,
                body=clean_text(_markdown(content, result)).rstrip("\n"),
                color=nearest_color(background) if background else "yellow",
                source_color=background,
                hidden=_flag(values, "hidden"),
                place=_place(values),
            )
        )
    return result
