"""Notes made with the 0.3 preview open, unchanged, in this version: with their
categories and marks, and the trash and deleted categories as they were left."""

import secrets
import shutil
from pathlib import Path

import pytest
from preview_fixture_0_3 import (
    DEFAULT_COLOR_SET,
    DROPPED,
    FIXTURE,
    GONE,
    IDEA,
    IDEA_ICON,
    IMPORTANT_NAME,
    KEY,
    LIVE_CATEGORIES,
    NOTES,
    OLD,
    PLACE,
    PLACED,
    SCHEMA_VERSION,
    SHORTCUTS_SET,
    URGENT_ICON,
)

from stickle.core.labels import BUILT_IN_MARKS
from stickle.core.layout import MAIN
from stickle.data.database import WrongKeyError, open_database
from stickle.data.labels import LabelRepository
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import latest_version, open_store, schema_version
from stickle.data.search import search
from stickle.data.settings import (
    DEFAULT_NOTE_COLOR,
    RECOVERY_KEY_KEPT,
    SHORTCUTS,
    THIS_DEVICE,
    USAGE,
    Settings,
)


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    """The preview's database, copied: the file in the repository is never opened for writing."""
    target = tmp_path / "notes.db"
    shutil.copyfile(FIXTURE, target)
    return target


def test_the_preview_database_is_the_expected_version(copy: Path) -> None:
    connection = open_database(copy, KEY)
    try:
        assert schema_version(connection) == SCHEMA_VERSION
    finally:
        connection.close()


def test_every_note_opens_with_its_category_and_marks(copy: Path) -> None:
    connection = open_store(copy, KEY)
    try:
        labels = LabelRepository(connection)

        def category_name(category_id: str | None) -> str | None:
            # Deleted or not: a note in the trash keeps the category deleted with it.
            category = labels.category(category_id) if category_id else None
            return category.name if category else None

        # Built-in marks by id, the user's own by name.
        mark_names = {
            m.id: m.id if m.id in BUILT_IN_MARKS else (m.name or "") for m in labels.marks()
        }
        notes = {note.body: note for note in NoteRepository(connection).all()}
        assert sorted(notes) == sorted(expected.body for expected in NOTES)
        for expected in NOTES:
            note = notes[expected.body]
            assert note.color == expected.color
            assert category_name(note.label) == expected.category
            assert sorted(mark_names[m] for m in note.marks) == sorted(expected.marks)
            assert note.hidden == expected.hidden
            assert note.locked == expected.locked
            assert (note.deleted_at is not None) == expected.deleted
        placed = notes[NOTES[PLACED].body]
        assert LayoutRepository(connection).places(placed.id) == {MAIN: PLACE}
    finally:
        connection.close()


def test_categories_and_marks_stay_as_the_user_left_them(copy: Path) -> None:
    connection = open_store(copy, KEY)
    try:
        labels = LabelRepository(connection)
        assert [c.name for c in labels.categories()] == LIVE_CATEGORIES
        marks = {m.id: m for m in labels.marks()}
        assert marks["important"].name == IMPORTANT_NAME
        assert marks["urgent"].icon == URGENT_ICON
        assert [(m.name, m.icon) for m in marks.values() if m.id not in BUILT_IN_MARKS] == [
            (IDEA, IDEA_ICON)
        ]
        assert DROPPED not in {m.name for m in marks.values()}
    finally:
        connection.close()


def test_a_note_in_the_trash_brings_its_deleted_category_back(copy: Path) -> None:
    connection = open_store(copy, KEY)
    try:
        notes = NoteRepository(connection)
        old = next(note for note in notes.deleted() if note.body == NOTES[-1].body)
        back = notes.restore(old.id)
        labels = LabelRepository(connection)
        category = labels.category(back.label) if back.label else None
        assert category is not None and category.name == OLD and not category.deleted
        assert GONE not in [c.name for c in labels.categories()]
    finally:
        connection.close()


def test_settings_stay(copy: Path) -> None:
    connection = open_store(copy, KEY)
    try:
        settings = Settings(connection)
        assert settings.get(DEFAULT_NOTE_COLOR) == DEFAULT_COLOR_SET
        assert settings.get(RECOVERY_KEY_KEPT) is True
        assert settings.get(USAGE) == THIS_DEVICE
        assert settings.get(SHORTCUTS) == SHORTCUTS_SET
    finally:
        connection.close()


def test_notes_are_found_by_search(copy: Path) -> None:
    connection = open_store(copy, KEY)
    try:
        by_body = {note.id: note.body for note in NoteRepository(connection).all()}
        assert NOTES[0].body in {by_body[i] for i in search(connection, "보고서")}
        assert NOTES[3].body in {by_body[i] for i in search(connection, "print")}
    finally:
        connection.close()


def test_the_preview_database_is_encrypted(copy: Path) -> None:
    data = FIXTURE.read_bytes()
    for expected in NOTES:
        assert expected.body.split("\n")[0].encode() not in data
    assert IMPORTANT_NAME.encode() not in data
    with pytest.raises(WrongKeyError):
        open_database(copy, secrets.token_bytes(32)).close()


def test_opening_at_the_same_version_leaves_the_file_as_it_was(copy: Path) -> None:
    if latest_version() != SCHEMA_VERSION:
        pytest.skip("the schema has moved on: an upgrade makes a backup instead")
    before = copy.read_bytes()
    open_store(copy, KEY).close()
    assert copy.read_bytes() == before
    assert not (copy.parent / "backups").exists()


def test_an_upgrade_keeps_a_backup_of_the_preview_database(copy: Path) -> None:
    if latest_version() == SCHEMA_VERSION:
        pytest.skip("no upgrade from the preview's version yet")
    open_store(copy, KEY).close()
    assert list((copy.parent / "backups").glob(f"*-v{SCHEMA_VERSION}.db"))
