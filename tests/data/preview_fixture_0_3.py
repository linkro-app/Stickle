"""A database as the 0.3 preview writes it, kept to prove later versions open it.

make() wrote fixtures/preview-0.3.0.db once, with the storage code of that
release (schema version 3, run from the v0.3.0-preview.1 tag). The file is
never changed afterwards: tests work on a copy. To see how it was made:

    uv run python tests/data/preview_fixture_0_3.py <path>

The notes are made up, and the key is a test key.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

from stickle.core.layout import MAIN, Place
from stickle.data.labels import LabelRepository
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import (
    DEFAULT_NOTE_COLOR,
    RECOVERY_KEY_KEPT,
    SHORTCUTS,
    THIS_DEVICE,
    USAGE,
    Settings,
)

FIXTURE = Path(__file__).parent / "fixtures" / "preview-0.3.0.db"
KEY = bytes(range(64, 96))  # a test key, for this made-up database only
SCHEMA_VERSION = 3

_times = iter(f"2026-10-11T09:{minute:02d}:00.000Z" for minute in range(60))


def clock() -> str:
    return next(_times)


# Categories, in their order: (name, colour). The last two are deleted: the first
# of them with its notes (into the trash), the other on its own.
WORK, HOME, OLD, GONE = "회사", "집", "옛 프로젝트", "지운 카테고리"
CATEGORIES = [(WORK, "blue"), (HOME, "mint"), (OLD, "coral"), (GONE, "lavender")]
LIVE_CATEGORIES = [WORK, HOME]

# Marks: the built-in ones by id; "important" renamed by the user, "urgent" given
# another icon; one of the user's own, and one of the user's own deleted.
IMPORTANT_NAME = "꼭"
URGENT_ICON = "flag"
IDEA, IDEA_ICON = "아이디어", "bulb"
DROPPED = "안 쓰는 표시"


@dataclass(frozen=True)
class Expected:
    body: str
    color: str = "yellow"
    category: str | None = None  # by name
    marks: tuple[str, ...] = field(default=())  # built-in ids, or names of the user's own
    hidden: bool = False
    locked: bool = False
    deleted: bool = False


NOTES = [
    Expected(
        "# 이번 주\n- [ ] 보고서\n  - [x] 초안\n  - [ ] 검토\n- [ ] 장보기",
        color="mint", category=WORK, marks=("todo", "urgent"),
    ),
    Expected("장보기 목록\n- [ ] 우유\n- [x] 달걀", category=HOME, marks=("todo",)),
    Expected("새 기능 아이디어", color="lavender", marks=(IDEA,)),
    Expected("코드 메모\n```\nprint('hi')\n```", color="blue"),
    Expected("중요한 메모", color="pink", marks=("important",), locked=True),
    Expected("숨긴 집 메모", category=HOME, hidden=True),
    Expected("옛 프로젝트 메모", color="coral", category=OLD, deleted=True),
]  # fmt: skip
PLACED = 0  # the note NOTES[0] was placed on screen

PLACE = Place(
    monitor=1, name="LG HDR 4K", rel_x=0.25, rel_y=0.15, width=300, height=340,
    setup=("LG HDR 4K",),
)  # fmt: skip

DEFAULT_COLOR_SET = "mint"
SHORTCUTS_SET = {"find": "Ctrl+Alt+F"}  # set by the user (it has no key at first)


def make(path: Path) -> list[str]:
    """Make the database; the ids of the notes, in the order of NOTES."""
    connection = open_store(path, KEY, clock)
    notes = NoteRepository(connection, clock)
    labels = LabelRepository(connection, clock)
    category_ids = {name: labels.create_category(name, color).id for name, color in CATEGORIES}
    labels.rename_mark("important", IMPORTANT_NAME)
    labels.set_mark_icon("urgent", URGENT_ICON)
    mark_ids = {mark: mark for mark in ("todo", "urgent", "important", "waiting")}
    mark_ids[IDEA] = labels.create_mark(IDEA, IDEA_ICON).id
    dropped = labels.create_mark(DROPPED, "question")

    ids: list[str] = []
    for index, expected in enumerate(NOTES):
        note = notes.create(expected.body, expected.color)
        ids.append(note.id)
        if expected.category is not None:
            notes.set_category(note.id, category_ids[expected.category])
        for mark in expected.marks:
            notes.set_mark(note.id, mark_ids[mark], True)
        if expected.locked:
            notes.set_locked(note.id, True)
        if expected.hidden:
            notes.set_hidden(note.id, True)
        if index == PLACED:
            LayoutRepository(connection, clock).save(note.id, {MAIN: PLACE})
    # A mark once on a note, then deleted: the note loses it.
    notes.set_mark(ids[1], dropped.id, True)
    owners = notes.remove_mark(dropped.id)
    assert owners == [ids[1]]
    # The old project's category deleted with its notes (into the trash), the other alone.
    notes.remove_category(category_ids[OLD], with_notes=True)
    notes.remove_category(category_ids[GONE], with_notes=False)

    settings = Settings(connection, clock)
    settings.set(DEFAULT_NOTE_COLOR, DEFAULT_COLOR_SET)
    settings.set(RECOVERY_KEY_KEPT, True)
    settings.set(USAGE, THIS_DEVICE)
    settings.set(SHORTCUTS, SHORTCUTS_SET)
    connection.pragma("wal_checkpoint", "TRUNCATE")
    connection.close()
    return ids


if __name__ == "__main__":
    make(Path(sys.argv[1]))
