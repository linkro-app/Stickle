"""Notes and key files left by the 0.6 preview open in this version, as they
were: their text, colours, the secret note, the reminder, own colours, where
they were on screen, and the settings."""

import secrets
import shutil
from datetime import date, time
from pathlib import Path

import pytest
from old_places import upgraded
from preview_fixture_0_6 import (
    DEFAULT_COLOR_SET,
    FIXTURE,
    NEWS_SEEN_SET,
    NOTES,
    OWN_COLOR,
    PASSWORD,
    PLACES,
    RECOVERY_KEY,
    REMINDED,
    REMINDER,
    SAME,
    SCHEMA_VERSION,
    SECRET_PASSWORD,
    SECRET_TEXT,
    SECRET_TITLE,
    TRASHED,
    TWO,
    UNREADABLE,
)

from stickle.core.layout import Place, Size
from stickle.crypto.key_files import A, B, C
from stickle.crypto.secret_notes import open_text
from stickle.data.colors import ColorRepository
from stickle.data.database import WrongKeyError, open_database
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.reminders import ReminderRepository
from stickle.data.schema import latest_version, open_store, schema_version
from stickle.data.settings import DEFAULT_NOTE_COLOR, NEWS_SEEN, RECOVERY_KEY_KEPT, Settings
from stickle.platform.credentials import CredentialStore
from stickle.unlock import NeedPassword, Unlock


def no_store() -> CredentialStore:
    raise AssertionError("portable notes read no credential store")


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """The preview's files, copied: those in the repository are never opened for writing."""
    target = tmp_path / "preview-0.6.0"
    shutil.copytree(FIXTURE, target)
    return target


@pytest.fixture
def unlock(folder: Path) -> Unlock:
    return Unlock(folder, no_store, portable=True)


@pytest.fixture
def key(unlock: Unlock) -> bytes:
    assert unlock.outcome() == NeedPassword(create=False)
    return unlock.enter_password(PASSWORD)


def test_the_fixture_is_as_0_6_left_it(unlock: Unlock, key: bytes) -> None:
    names = sorted(p.name for p in FIXTURE.iterdir() if p.is_file())
    assert names == sorted([A, B, C, "notes.db"])
    connection = open_database(unlock.database, key)  # the copy: opening writes beside it
    try:
        assert schema_version(connection) == SCHEMA_VERSION
    finally:
        connection.close()


def test_every_note_opens_as_it_was(unlock: Unlock, key: bytes) -> None:
    connection = open_store(unlock.database, key)
    try:
        assert schema_version(connection) == latest_version()
        notes = NoteRepository(connection).all()
        plain = {note.body: note for note in notes if note.sealed is None}
        assert sorted(plain) == sorted([body for body, _, _ in NOTES] + [TRASHED])
        for body, color, hidden in NOTES:
            assert (plain[body].color, plain[body].hidden, plain[body].deleted) == (
                color,
                hidden,
                False,
            )
        assert plain[TRASHED].deleted
        assert [c.hex for c in ColorRepository(connection).colors()] == [OWN_COLOR]
    finally:
        connection.close()


def test_the_secret_note_opens_with_its_password_and_the_recovery_key(
    unlock: Unlock, key: bytes
) -> None:
    secret_key = unlock.open_secret_notes(SECRET_PASSWORD)
    assert unlock.open_secret_notes_with_recovery_key(RECOVERY_KEY) == secret_key
    assert unlock.open_with_recovery_key(RECOVERY_KEY) == key
    connection = open_store(unlock.database, key)
    try:
        [secret] = [note for note in NoteRepository(connection).all() if note.sealed is not None]
        assert secret.body == "" and secret.secret_title == SECRET_TITLE
        assert secret.sealed is not None
        assert open_text(secret_key, secret.id, secret.sealed) == SECRET_TEXT
    finally:
        connection.close()


def test_the_reminder_stays(unlock: Unlock, key: bytes) -> None:
    connection = open_store(unlock.database, key)
    try:
        by_body = {note.body: note.id for note in NoteRepository(connection).all()}
        reminder = ReminderRepository(connection).of_note(by_body[NOTES[REMINDED][0]])
        year, month, day, hour, minute, zone = REMINDER
        assert reminder is not None
        assert (reminder.rule.start, reminder.rule.at, reminder.rule.zone) == (
            date(year, month, day),
            time(hour, minute),
            zone,
        )
        assert reminder.next_day == date(year, month, day)
    finally:
        connection.close()


def test_places_on_screen_go_under_the_number_of_monitors_they_were_stored_with(
    unlock: Unlock, key: bytes
) -> None:
    connection = open_store(unlock.database, key)
    try:
        by_body = {note.body: note.id for note in NoteRepository(connection).all()}
        layouts = LayoutRepository(connection)

        def stored(index: int) -> tuple[dict[int, Place], Size | None]:
            note_id = by_body[NOTES[index][0]]
            return layouts.places(note_id), layouts.size(note_id)

        for index, rows in PLACES.items():
            assert stored(index) == upgraded({row[0]: row[1:] for row in rows})
        assert set(stored(TWO)[0]) == {1, 2}
        assert set(stored(SAME)[0]) == {2}  # the main place's
        assert stored(UNREADABLE) == ({}, None)
    finally:
        connection.close()


def test_settings_stay(unlock: Unlock, key: bytes) -> None:
    connection = open_store(unlock.database, key)
    try:
        settings = Settings(connection)
        assert settings.get(NEWS_SEEN) == NEWS_SEEN_SET
        assert settings.get(RECOVERY_KEY_KEPT) is True
        assert settings.get(DEFAULT_NOTE_COLOR) == DEFAULT_COLOR_SET
    finally:
        connection.close()


def test_the_preview_database_is_encrypted(folder: Path) -> None:
    data = (FIXTURE / "notes.db").read_bytes()
    for body, _, _ in NOTES:
        assert body.split("\n")[0].encode() not in data
    assert SECRET_TEXT.encode() not in data
    with pytest.raises(WrongKeyError):
        open_database(folder / "notes.db", secrets.token_bytes(32)).close()


def test_an_upgrade_keeps_a_backup_of_the_preview_database(unlock: Unlock, key: bytes) -> None:
    if latest_version() == SCHEMA_VERSION:
        pytest.skip("no upgrade from the preview's version yet")
    open_store(unlock.database, key).close()
    assert list((unlock.folder / "backups").glob(f"*-v{SCHEMA_VERSION}.db"))
