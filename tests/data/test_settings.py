import json
import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import apsw
import pytest

from stickle.core.clock import millis_at
from stickle.core.hlc import Hlc
from stickle.data import record_stamps, record_state, schema
from stickle.data import settings as settings_module
from stickle.data.database import KEY_BYTES
from stickle.data.record_stamps import SETTING, VALUE
from stickle.data.schema import open_store
from stickle.data.settings import (
    DEFAULT_NOTE_COLOR,
    DEVICE_ID,
    MAGNET,
    NEWS_SEEN,
    RECOVERY_KEY_KEPT,
    RECOVERY_KEY_KEPT_UNTIL_0_6,
    SETTINGS,
    SHARED,
    SYNC_EVERY,
    InvalidSettingError,
    KeptRecoveryKey,
    Setting,
    Settings,
    let_go,
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
    settings.set(DEFAULT_NOTE_COLOR, "pink")  # before this device has its id: none made
    assert settings.get(DEVICE_ID) is None
    settings.set(DEVICE_ID, AN_ID)
    settings.set(DEFAULT_NOTE_COLOR, "blue")

    rows = db.execute("SELECT updated_at, updated_by FROM shared_settings").fetchall()
    assert rows == [(NOW, AN_ID)]


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
    declared: list[Setting[object]] = [
        cast(Setting[object], each)
        for each in vars(settings_module).values()
        if isinstance(each, Setting)
    ]
    assert {each.key for each in declared} == SETTINGS.keys()  # sync takes only those listed


def settings_changed(db: apsw.Connection) -> list[str]:
    """The settings sync would merge with a vault, changed here since."""
    return [item for kind, item in record_state.changed(db, "vault") if kind == SETTING]


def test_a_shared_setting_changed_is_a_change_for_the_other_devices(db: apsw.Connection) -> None:
    settings = Settings(db, clock=lambda: NOW, millis=lambda: 1_800_000_000_000)
    settings.set(MAGNET, False)
    settings.set(SYNC_EVERY, 60)  # this device's own: nothing for the others

    assert record_stamps.of(db, SETTING, MAGNET.key) == {VALUE: (Hlc(1_800_000_000_000), None)}
    assert settings_changed(db) == [MAGNET.key]
    assert record_stamps.of(db, SETTING, SYNC_EVERY.key) == {}


def test_set_as_it_is_a_shared_setting_does_not_change(db: apsw.Connection) -> None:
    times = iter([1_800_000_000_000, 1_800_000_001_000, 1_800_000_002_000])
    settings = Settings(db, clock=lambda: NOW, millis=lambda: next(times))
    settings.set(MAGNET, False)
    seq = record_stamps.change_seq(db, SETTING, MAGNET.key)

    settings.set(MAGNET, False)
    assert record_stamps.change_seq(db, SETTING, MAGNET.key) == seq
    assert record_stamps.of(db, SETTING, MAGNET.key)[VALUE][0] == Hlc(1_800_000_000_000)
    settings.set(MAGNET, True)
    assert record_stamps.of(db, SETTING, MAGNET.key)[VALUE][0] == Hlc(1_800_000_001_000)


def test_a_shared_setting_is_set_to_its_default_not_cleared(db: apsw.Connection) -> None:
    with pytest.raises(AssertionError):
        Settings(db).clear(MAGNET)


def test_let_go_it_is_as_never_changed_here_and_merged_again(db: apsw.Connection) -> None:
    settings = Settings(db, millis=lambda: 1_800_000_000_000)
    settings.set(MAGNET, False)
    seq = record_stamps.change_seq(db, SETTING, MAGNET.key)

    let_go(db)

    assert settings.get(MAGNET) is False  # until the vault's comes
    assert record_stamps.of(db, SETTING, MAGNET.key) == {VALUE: (Hlc(0), None)}
    assert record_stamps.change_seq(db, SETTING, MAGNET.key) > seq
    assert record_stamps.of(db, SETTING, DEFAULT_NOTE_COLOR.key) == {}  # none here: nothing


def test_the_settings_that_follow_the_user() -> None:
    assert set(SHARED) == {
        "default_color",
        "last_used_color",
        "random_color",
        "magnet",
        "morning",
        "snooze_minutes",
        "recent_calendars",
        "relock_minutes",
        "lock_on_leave",
        "recovery_key_kept_id",
    }


def test_the_upgrade_stamps_each_shared_setting_when_it_last_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "notes.db"
    monkeypatch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:29])
    connection = open_store(path, KEY)
    with connection:
        connection.execute(
            "INSERT INTO shared_settings VALUES ('magnet', 'false', ?, ?)", (NOW, AN_ID)
        )
    connection.close()
    monkeypatch.undo()

    connection = open_store(path, KEY)
    try:
        when = Hlc(millis_at(NOW))
        assert record_stamps.of(connection, SETTING, MAGNET.key) == {VALUE: (when, None)}
        assert settings_changed(connection) == [MAGNET.key]
        assert Settings(connection).get(MAGNET) is False
    finally:
        connection.close()


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


FIRST, SECOND = "a" * 64, "b" * 64  # ids of two recovery keys


class Notes:
    """Which recovery key the notes have: one, another, or none."""

    def __init__(self, key_id: str | None) -> None:
        self.key_id = key_id


def test_kept_counts_only_for_the_recovery_key_the_notes_have(db: apsw.Connection) -> None:
    notes = Notes(FIRST)
    kept = KeptRecoveryKey(Settings(db), lambda: notes.key_id)
    assert not kept.is_kept()

    kept.keep()
    assert kept.is_kept()

    notes.key_id = SECOND  # another one made since, here or on another device
    assert not kept.is_kept()
    notes.key_id = None  # none any more, or its file unreadable
    assert not kept.is_kept()


def test_a_new_one_on_the_way_is_not_kept(db: apsw.Connection) -> None:
    notes = Notes(FIRST)
    kept = KeptRecoveryKey(Settings(db), lambda: notes.key_id)
    kept.keep()

    kept.forget()

    assert not kept.is_kept()


def test_kept_with_no_recovery_key_marks_nothing(db: apsw.Connection) -> None:
    kept = KeptRecoveryKey(Settings(db), lambda: None)

    kept.keep()

    assert Settings(db).get(RECOVERY_KEY_KEPT) is None


def test_which_one_is_kept_will_follow_to_other_devices() -> None:
    # The same recovery key on every device once sync exists.
    assert RECOVERY_KEY_KEPT.scope == "shared"


@pytest.mark.parametrize(("until_0_6", "kept_now"), [(True, True), (False, False), (None, False)])
def test_what_0_6_kept_is_taken_for_the_key_there_is(
    db: apsw.Connection, until_0_6: bool | None, kept_now: bool
) -> None:
    settings = Settings(db)
    if until_0_6 is not None:
        settings.set(RECOVERY_KEY_KEPT_UNTIL_0_6, until_0_6)
    kept = KeptRecoveryKey(settings, lambda: FIRST)

    assert kept.carry_over() == kept_now
    assert not kept.carry_over()  # once

    assert kept.is_kept() == kept_now
    assert (
        db.execute("SELECT key FROM device_settings WHERE key = 'recovery_key_kept'").fetchall()
        == []
    )


def test_what_0_6_kept_waits_for_a_key_that_can_be_read(db: apsw.Connection) -> None:
    settings = Settings(db)
    settings.set(RECOVERY_KEY_KEPT_UNTIL_0_6, True)
    notes = Notes(None)
    kept = KeptRecoveryKey(settings, lambda: notes.key_id)

    kept.carry_over()
    assert settings.get(RECOVERY_KEY_KEPT_UNTIL_0_6)  # not lost

    notes.key_id = FIRST
    kept.carry_over()
    assert kept.is_kept()


def test_the_upgrade_keeps_what_this_device_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = shared_until_0_6(tmp_path / "notes.db", monkeypatch, here="0.6.1")
    try:
        assert Settings(connection).get(NEWS_SEEN) == "0.6.1"
    finally:
        connection.close()
