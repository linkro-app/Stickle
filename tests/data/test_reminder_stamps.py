"""Each change to a note's reminders stamps a field of the note and counts the
note as changed, for sync; a time written anew in its text starts afresh."""

import secrets
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path

import apsw
import pytest

from stickle.core.hlc import Hlc
from stickle.core.reminders import Rule
from stickle.data import stamps
from stickle.data.notes import NoteRepository
from stickle.data.reminders import ReminderRepository, text_field
from stickle.data.schema import open_store

KEY = secrets.token_bytes(32)
NOW = 1_762_483_200_000  # 2025-11-07T03:20Z
HERE = (Hlc(NOW), None)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def notes(connection: apsw.Connection) -> NoteRepository:
    return NoteRepository(connection, clock=lambda: "2025-11-07T03:20:00.000Z", millis=lambda: NOW)


@pytest.fixture
def reminders(connection: apsw.Connection) -> ReminderRepository:
    return ReminderRepository(
        connection, clock=lambda: "2025-11-07T03:20:00.000Z", device="d", millis=lambda: NOW
    )


def test_setting_one_stamps_it_and_how_it_was_dealt_with(
    notes: NoteRepository, reminders: ReminderRepository, connection: apsw.Connection
) -> None:
    note = notes.create("회의")

    reminders.set(note.id, Rule(date(2025, 11, 10), time(9), "Asia/Seoul"))

    assert stamps.of(connection, note.id) == {"reminder": HERE, "reminder_state": HERE}
    assert notes.get(note.id).change_seq > note.change_seq  # pyright: ignore[reportOptionalMemberAccess]
    rows = connection.execute("SELECT first_day, next_day FROM reminders").fetchall()
    assert rows == [("2025-11-10", "2025-11-10")]


def test_turned_off_dealt_with_and_snoozed(
    notes: NoteRepository, reminders: ReminderRepository, connection: apsw.Connection
) -> None:
    note = notes.create("회의")
    reminder = reminders.set(note.id, Rule(date(2025, 11, 10), time(9), "Asia/Seoul", "daily"))
    connection.execute("DELETE FROM note_stamps")

    reminders.snooze(reminder.id, datetime(2025, 11, 10, 1, tzinfo=UTC))
    assert set(stamps.of(connection, note.id)) == {"reminder_state"}
    reminders.dealt_with(reminder.id)
    reminders.turn_off(note.id)

    assert set(stamps.of(connection, note.id)) == {"reminder_state", "reminder"}


def test_a_time_written_anew_starts_afresh_one_followed_does_not(
    notes: NoteRepository, reminders: ReminderRepository, connection: apsw.Connection
) -> None:
    note = notes.create("회의")
    at = datetime(2025, 11, 10, 15)

    reminders.match_text(note.id, [at], fresh=False)
    assert stamps.of(connection, note.id) == {}
    reminders.match_text(note.id, [])
    reminders.match_text(note.id, [at])

    assert stamps.of(connection, note.id) == {text_field("2025-11-10", "15:00"): HERE}
