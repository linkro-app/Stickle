"""The apps Stickle brings notes over from, and how to tell their files apart."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from stickle.importers import indicator_stickynotes, sticky_notes, xpad, zhorn
from stickle.importers.base import Found, ImportResult


@dataclass(frozen=True)
class Source:
    name: str  # as stored in import records
    title: str  # the app's own name, as shown
    find: Callable[[Path], Found | None]  # on this computer, from the home folder
    read: Callable[[Path], ImportResult]


SOURCES = (
    Source(
        sticky_notes.SOURCE,
        "Microsoft Sticky Notes",
        lambda home: sticky_notes.find(home),
        sticky_notes.read,
    ),
    Source(zhorn.SOURCE, "Zhorn Stickies", lambda home: zhorn.find(home), zhorn.read),
    Source(xpad.SOURCE, "xpad", lambda home: xpad.find(home), xpad.read),
    Source(
        indicator_stickynotes.SOURCE,
        "Indicator Stickynotes",
        indicator_stickynotes.find,
        indicator_stickynotes.read,
    ),
)
BY_NAME = {source.name: source for source in SOURCES}
SQLITE = b"SQLite format 3\x00"


def found_here(home: Path) -> list[tuple[Source, Found]]:
    """The apps whose notes are on this computer."""
    return [(source, found) for source in SOURCES if (found := source.find(home)) is not None]


def recognise(path: Path) -> tuple[Source, Found] | None:
    """Which app a file or folder the user chose comes from, if any.

    An xpad folder, or one of its files (the folder is meant), or Indicator
    Stickynotes' file (JSON with notes in it).
    """
    folder = path if path.is_dir() else path.parent
    if path.is_file() and path.name.lower().startswith("plum.sqlite"):
        source = BY_NAME[sticky_notes.SOURCE]
        return source, Found(source.name, path.with_name("plum.sqlite"))
    if path.is_file() and path.suffix.lower() == ".db" and path.read_bytes()[:16] == SQLITE:
        source = BY_NAME[zhorn.SOURCE]
        return source, Found(source.name, path)
    if path.name.startswith(("info-", "content-")) or (
        path.is_dir() and any(folder.glob("info-*"))
    ):
        return BY_NAME[xpad.SOURCE], Found(xpad.SOURCE, folder)
    if path.is_file():
        try:
            with path.open(encoding="utf-8") as file:
                head: object = json.loads(file.read(16 * 1024 * 1024))
        except OSError, UnicodeDecodeError, ValueError:
            return None
        if isinstance(head, dict) and "notes" in head:
            source = BY_NAME[indicator_stickynotes.SOURCE]
            return source, Found(source.name, path)
    return None
