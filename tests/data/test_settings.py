import json
import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest

from stickle.data import schema
from stickle.data.database import KEY_BYTES
from stickle.data.schema import open_store
from stickle.data.settings import (
    DEFAULT_NOTE_COLOR,
    DEVICE_ID,
    NEWS_SEEN,
    SETTINGS,
    InvalidSettingError,
    Settings,
)

KEY = secrets.token_bytes(KEY_BYTES)
NOW = "2026-09-26T10:00:00.000Z"
AN_ID = "0b9b6b4e-6f86-4a53-9d8e-2d7a4c1f0a11"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


def test_unset_settings_read_as_their_defaults(db: apsw.Connection) -> None:
    settings = Settings(db)
    assert settings.get(DEVICE_ID) is None
    assert settings.get(DEFAULT_NOTE_COLOR) == "yellow"


def test_values_survive_reopening(tmp_path: Path) -> None:
    path = tmp_path / "notes.db"
    connection = open_store(path, KEY)
    Settings(connection).set(DEVICE_ID, AN_ID)
    Settings(connection).set(DEFAULT_NOTE_COLOR, "blue")
    connection.close()

    connection = open_store(path, KEY)
    assert Settings(connection).get(DEVICE_ID) == AN_ID
    assert Settings(connection).get(DEFAULT_NOTE_COLOR) == "blue"
    connection.close()


def test_device_and_shared_settings_are_kept_apart(db: apsw.Connection) -> None:
    settings = Settings(db, clock=lambda: NOW)
    settings.set(DEVICE_ID, AN_ID)
    settings.set(DEFAULT_NOTE_COLOR, "green")

    device = db.execute("SELECT key FROM device_settings ORDER BY key").fetchall()
    shared = db.execute("SELECT key FROM shared_settings").fetchall()
    assert ("device_id",) in device
    assert shared == [("default_color",)]


def test_shared_settings_record_when_and_where_they_changed(db: apsw.Connection) -> None:
    settings = Settings(db, clock=lambda: NOW)
    settings.set(DEFAULT_NOTE_COLOR, "pink")

    rows = db.execute("SELECT updated_at, updated_by FROM shared_settings").fetchall()
    assert rows == [(NOW, settings.device_id())]


def test_invalid_values_are_refused(db: apsw.Connection) -> None:
    settings = Settings(db)
    with pytest.raises(InvalidSettingError):
        settings.set(DEVICE_ID, "not a uuid")
    with pytest.raises(InvalidSettingError):
        settings.set(DEFAULT_NOTE_COLOR, "not a colour")
    assert settings.get(DEVICE_ID) is None


def test_stored_value_that_no_longer_fits_falls_back_to_the_default(
    db: apsw.Connection,
) -> None:
    with db:
        db.execute(
            "INSERT INTO shared_settings VALUES ('default_color', '\"a b\"', ?, ?)", (NOW, AN_ID)
        )
    assert Settings(db).get(DEFAULT_NOTE_COLOR) == "yellow"


def test_device_id_is_created_once_and_kept(db: apsw.Connection) -> None:
    settings = Settings(db)
    assert settings.get(DEVICE_ID) is None
    first = settings.device_id()

    assert str(uuid.UUID(first)) == first
    assert settings.device_id() == first
    assert Settings(db).get(DEVICE_ID) == first


def test_the_device_id_is_a_device_setting() -> None:
    # Copying the database to another computer must not make both claim one identity
    # once sync exists; keeping it out of shared settings is the first half of that.
    assert DEVICE_ID.scope == "device"


def test_every_setting_is_registered_under_its_key() -> None:
    assert all(key == setting.key for key, setting in SETTINGS.items())
    assert {DEVICE_ID.key, DEFAULT_NOTE_COLOR.key} <= SETTINGS.keys()


def test_the_version_of_the_last_whats_new_note_is_a_device_setting() -> None:
    # Shared, a device on an older version would give a newer one's deleted note again.
    assert NEWS_SEEN.scope == "device"


def shared_until_0_6(
    path: Path, monkeypatch: pytest.MonkeyPatch, here: str | None
) -> apsw.Connection:
    """A database where 0.6 kept news_seen in common (and this device, here, its
    own), opened by this version."""
    monkeypatch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:12])
    connection = open_store(path, KEY)
    with connection:
        connection.execute(
            "INSERT INTO shared_settings VALUES ('news_seen', '\"0.6.0\"', ?, ?)", (NOW, AN_ID)
        )
        if here is not None:
            connection.execute(
                "INSERT INTO device_settings VALUES ('news_seen', ?)", (json.dumps(here),)
            )
    connection.close()
    monkeypatch.undo()
    return open_store(path, KEY)


def test_the_upgrade_moves_what_was_kept_in_common_to_this_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = shared_until_0_6(tmp_path / "notes.db", monkeypatch, here=None)
    try:
        assert Settings(connection).get(NEWS_SEEN) == "0.6.0"
        shared = connection.execute("SELECT key FROM shared_settings").fetchall()
        assert ("news_seen",) not in shared
    finally:
        connection.close()


def test_the_upgrade_keeps_what_this_device_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = shared_until_0_6(tmp_path / "notes.db", monkeypatch, here="0.6.1")
    try:
        assert Settings(connection).get(NEWS_SEEN) == "0.6.1"
    finally:
        connection.close()
