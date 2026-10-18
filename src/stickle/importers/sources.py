"""The apps Stickle brings notes over from, and how to tell their files apart."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from stickle.importers import indicator_stickynotes, xpad
from stickle.importers.base import Found, ImportResult


@dataclass(frozen=True)
class Source:
    name: str  # as stored in import records
    title: str  # the app's own name, as shown
    find: Callable[[Path], Found | None]  # on this computer, from the home folder
    read: Callable[[Path], ImportResult]


SOURCES = (
    Source(xpad.SOURCE, "xpad", lambda home: xpad.find(home), xpad.read),
    Source(
        indicator_stickynotes.SOURCE,
        "Indicator Stickynotes",
        indicator_stickynotes.find,
        indicator_stickynotes.read,
    ),
)
BY_NAME = {source.name: source for source in SOURCES}


def found_here(home: Path) -> list[tuple[Source, Found]]:
    """The apps whose notes are on this computer."""
    return [(source, found) for source in SOURCES if (found := source.find(home)) is not None]


def recognise(path: Path) -> tuple[Source, Found] | None:
    """Which app a file or folder the user chose comes from, if any.

    An xpad folder, or one of its files (the folder is meant), or Indicator
    Stickynotes' file (JSON with notes in it).
    """
    folder = path if path.is_dir() else path.parent
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
