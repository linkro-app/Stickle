"""A database as the 0.2 preview writes it, kept to prove later versions open it.

make() wrote fixtures/preview-0.2.0.db once, with the storage code of that
release (schema version 2, run from the v0.2.0-preview.1 tag). The file is
never changed afterwards: tests work on a copy. To see how it was made:

    uv run python tests/data/preview_fixture_0_2.py <path>

The notes are made up, and the key is a test key.
"""

import sys
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).parent / "fixtures" / "preview-0.2.0.db"
KEY = bytes(range(32, 64))  # a test key, for this made-up database only
SCHEMA_VERSION = 2

_times = iter(f"2026-10-01T12:{minute:02d}:00.000Z" for minute in range(60))


def clock() -> str:
    return next(_times)


@dataclass(frozen=True)
class Expected:
    body: str
    color: str = "yellow"
    hidden: bool = False
    always_on_top: bool = True
    collapsed: bool = False
    opacity: float = 1.0
    locked: bool = False
    deleted: bool = False


NOTES = [
    Expected("# 이번 주\n- [ ] 보고서\n  - [x] 초안\n  - [ ] 검토\n- [ ] 장보기", color="mint"),
    Expected("반투명 메모", color="blue", opacity=0.5),
    Expected("잠근 메모 — 옮기거나 고칠 수 없음", color="pink", locked=True),
    Expected("Hidden note", color="lavender", hidden=True),
    Expected("접고 핀 끈 메모\n둘째 줄", color="apricot", collapsed=True, always_on_top=False),
    Expected("휴지통에 있는 메모", color="coral", deleted=True),
]
EMPTIED = "휴지통에서 비운 메모"  # only its deletion record stays

# Where NOTES[PLACED] was placed, its main place as that version kept it:
# (monitor number, its name, rel_x, rel_y, width, height, the names of the
# monitors connected then). Plain values, so that this file imports in later
# versions.
PLACE = (1, "DELL U2720Q", 0.3, 0.1, 280, 320, ("DELL U2720Q",))
PLACED = 0  # the note NOTES[0] was placed on screen

DEFAULT_COLOR_SET = "blue"
SHORTCUTS_SET = {"hide-all": ""}  # turned off by the user


def make(path: Path) -> list[str]:
    """Make the database; the ids of the notes, in the order of NOTES. Run with
    the 0.2 code, whose names and arguments differ from later ones'."""
    from stickle.data.notes import NoteRepository
    from stickle.data.schema import open_store
    from stickle.data.settings import DEFAULT_NOTE_COLOR, SHORTCUTS, THIS_DEVICE, USAGE, Settings

    layout: Any = import_module("stickle.core.layout")
    layouts: Any = import_module("stickle.data.layouts")
    settings_then: Any = import_module("stickle.data.settings")
    connection = open_store(path, KEY, clock)
    notes = NoteRepository(connection, clock)
    ids: list[str] = []
    for index, expected in enumerate(NOTES):
        note = notes.create(expected.body, expected.color)
        ids.append(note.id)
        if not expected.always_on_top:
            notes.set_always_on_top(note.id, False)
        if expected.opacity != 1.0:
            notes.set_opacity(note.id, expected.opacity)
        if expected.locked:
            notes.set_locked(note.id, True)
        if expected.collapsed:
            notes.set_collapsed(note.id, True)
        if expected.hidden:
            notes.set_hidden(note.id, True)
        if expected.deleted:
            notes.delete(note.id)
        if index == PLACED:
            places = {layout.MAIN: layout.Place(*PLACE)}
            layouts.LayoutRepository(connection, clock).save(note.id, places)
    emptied = notes.create(EMPTIED)
    notes.delete(emptied.id)
    notes.purge(emptied.id)
    settings = Settings(connection, clock)
    settings.set(DEFAULT_NOTE_COLOR, DEFAULT_COLOR_SET)
    settings.set(settings_then.RECOVERY_KEY_KEPT, True)
    settings.set(USAGE, THIS_DEVICE)
    settings.set(SHORTCUTS, SHORTCUTS_SET)
    connection.pragma("wal_checkpoint", "TRUNCATE")
    connection.close()
    return ids


if __name__ == "__main__":
    make(Path(sys.argv[1]))
