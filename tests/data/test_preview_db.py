"""Notes made with the first public preview (0.1.2) open, unchanged, in this version."""

import secrets
import shutil
from pathlib import Path

import pytest
from old_places import upgraded
from preview_fixture import (
    DEFAULT_COLOR_SET,
    FIXTURE,
    KEY,
    NOTES,
    PLACED,
    PLACES,
    SCHEMA_VERSION,
)

from stickle.data.database import WrongKeyError, open_database
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import latest_version, open_store, schema_version
from stickle.data.search import search
from stickle.data.settings import (
    DEFAULT_NOTE_COLOR,
    RECOVERY_KEY_KEPT,
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
            assert (note.deleted_at is not None) == expected.deleted
        placed = notes[NOTES[PLACED].body]
        layouts = LayoutRepository(connection)
        places, size = upgraded(PLACES)
        assert set(places) == {1, 2}  # its spare place was stored with no names
        assert (layouts.places(placed.id), layouts.size(placed.id)) == (places, size)
    finally:
        connection.close()


def test_a_note_deleted_in_the_preview_is_in_the_trash(copy: Path) -> None:
    connection = open_store(copy, KEY)
    try:
        notes = NoteRepository(connection)
        trash = [note.body for note in notes.deleted()]
        assert trash == [expected.body for expected in NOTES if expected.deleted]
        assert notes.deletion_records() == []
    finally:
        connection.close()


def test_settings_stay(copy: Path) -> None:
    connection = open_store(copy, KEY)
    try:
        settings = Settings(connection)
        assert settings.get(DEFAULT_NOTE_COLOR) == DEFAULT_COLOR_SET
        assert settings.get(RECOVERY_KEY_KEPT) is True
        assert settings.get(USAGE) == THIS_DEVICE
    finally:
        connection.close()


def test_notes_are_found_by_search(copy: Path) -> None:
    connection = open_store(copy, KEY)
    try:
        by_body = {note.id: note.body for note in NoteRepository(connection).all()}
        # A short term and a longer one, both in Korean.
        assert {by_body[i] for i in search(connection, "회의")} >= {NOTES[0].body}
        assert NOTES[1].body in {by_body[i] for i in search(connection, "장보기")}
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
