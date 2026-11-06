"""Notes made with the 0.2 preview open, unchanged, in this version."""

import secrets
import shutil
from pathlib import Path

import pytest
from old_places import upgraded
from preview_fixture_0_2 import (
    DEFAULT_COLOR_SET,
    FIXTURE,
    KEY,
    NOTES,
    PLACE,
    PLACED,
    SCHEMA_VERSION,
    SHORTCUTS_SET,
)

from stickle.core.labels import BUILT_IN_MARKS
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


def test_every_note_opens_as_the_preview_left_it(copy: Path) -> None:
    connection = open_store(copy, KEY)
    try:
        notes = {note.body: note for note in NoteRepository(connection).all()}
        assert sorted(notes) == sorted(expected.body for expected in NOTES)
        for expected in NOTES:
            note = notes[expected.body]
            assert note.color == expected.color
            assert note.hidden == expected.hidden
            assert note.always_on_top == expected.always_on_top
            assert note.collapsed == expected.collapsed
            assert note.opacity == expected.opacity
            assert note.locked == expected.locked
            assert (note.deleted_at is not None) == expected.deleted
        placed = notes[NOTES[PLACED].body]
        layouts = LayoutRepository(connection)
        assert (layouts.places(placed.id), layouts.size(placed.id)) == upgraded({"main": PLACE})
    finally:
        connection.close()


def test_the_trash_and_what_was_emptied_from_it_stay(copy: Path) -> None:
    connection = open_store(copy, KEY)
    try:
        notes = NoteRepository(connection)
        trash = [note.body for note in notes.deleted()]
        assert trash == [expected.body for expected in NOTES if expected.deleted]
        assert len(notes.deletion_records()) == 1
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


def test_after_the_upgrade_notes_have_no_category_or_mark_yet(copy: Path) -> None:
    connection = open_store(copy, KEY)
    try:
        assert all(n.label is None and not n.marks for n in NoteRepository(connection).all())
        assert LabelRepository(connection).categories() == []
        assert [mark.id for mark in LabelRepository(connection).marks()] == list(BUILT_IN_MARKS)
    finally:
        connection.close()


def test_notes_are_found_by_search(copy: Path) -> None:
    connection = open_store(copy, KEY)
    try:
        by_body = {note.id: note.body for note in NoteRepository(connection).all()}
        assert NOTES[0].body in {by_body[i] for i in search(connection, "보고서")}
        assert NOTES[2].body in {by_body[i] for i in search(connection, "잠근")}
    finally:
        connection.close()


def test_the_preview_database_is_encrypted(copy: Path) -> None:
    data = FIXTURE.read_bytes()
    for expected in NOTES:
        assert expected.body.split("\n")[0].encode() not in data
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
