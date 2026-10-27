"""A note's reminder as stored: set, dealt with, snoozed, turned off."""

import secrets
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path

import apsw
import pytest

from stickle.core.reminders import Rule
from stickle.data.database import KEY_BYTES
from stickle.data.notes import NoteRepository
from stickle.data.reminders import ReminderPastError, ReminderRepository
from stickle.data.schema import open_store

KEY = secrets.token_bytes(KEY_BYTES)
SEOUL = "Asia/Seoul"
NINE = time(9, 0)
DEVICE = "8f6c3c1e-3a3b-4c43-9a55-2f1d3f7f1e0a"


class Clock:
    def __init__(self) -> None:
        self.now = "2026-11-02T03:00:00.000Z"  # Monday, noon in Seoul

    def __call__(self) -> str:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def notes(db: apsw.Connection, clock: Clock) -> NoteRepository:
    return NoteRepository(db, clock)


@pytest.fixture
def reminders(db: apsw.Connection, clock: Clock) -> ReminderRepository:
    return ReminderRepository(db, clock, DEVICE)


def test_a_reminder_rings_at_its_time_where_it_was_set(
    notes: NoteRepository, reminders: ReminderRepository
) -> None:
    note = notes.create("회의 준비")
    reminder = reminders.set(note.id, Rule(date(2026, 11, 3), NINE, SEOUL))

    assert reminders.of_note(note.id) == reminder
    assert reminders.pending() == [reminder]
    assert reminder.rings_at() == datetime(2026, 11, 3, 0, 0, tzinfo=UTC)


def test_one_already_past_is_refused_unless_it_repeats(
    notes: NoteRepository, reminders: ReminderRepository
) -> None:
    note = notes.create("회의 준비")
    with pytest.raises(ReminderPastError):
        reminders.set(note.id, Rule(date(2026, 11, 2), NINE, SEOUL))
    assert reminders.of_note(note.id) is None

    # Repeating since last week: it rings next tomorrow, today's 9:00 being past.
    weekly = reminders.set(note.id, Rule(date(2026, 10, 26), NINE, SEOUL, "daily"))
    assert weekly.next_day == date(2026, 11, 3)


def test_setting_again_takes_the_place_of_the_old_one(
    notes: NoteRepository, reminders: ReminderRepository
) -> None:
    note = notes.create("회의 준비")
    first = reminders.set(note.id, Rule(date(2026, 11, 3), NINE, SEOUL))
    reminders.snooze(first.id, datetime(2026, 11, 3, 0, 10, tzinfo=UTC))
    second = reminders.set(note.id, Rule(date(2026, 11, 5), time(15, 0), SEOUL), hide_note=True)

    assert second.id == first.id
    assert second.snoozed_until is None
    assert second.hide_note
    assert reminders.pending() == [second]


def test_dealt_with_it_moves_past_every_time_gone_by(
    notes: NoteRepository, reminders: ReminderRepository, clock: Clock, db: apsw.Connection
) -> None:
    note = notes.create("물 마시기")
    reminder = reminders.set(note.id, Rule(date(2026, 11, 3), NINE, SEOUL, "daily"))
    clock.now = "2026-11-06T05:00:00.000Z"  # three mornings later, 14:00 in Seoul

    dealt = reminders.dealt_with(reminder.id)

    assert dealt.next_day == date(2026, 11, 7)
    row = db.execute("SELECT acked_at, acked_device FROM reminders").fetchall()
    assert row == [(clock.now, DEVICE)]


def test_once_dealt_with_one_that_does_not_repeat_is_done(
    notes: NoteRepository, reminders: ReminderRepository, clock: Clock
) -> None:
    note = notes.create("회의 준비")
    reminder = reminders.set(note.id, Rule(date(2026, 11, 3), NINE, SEOUL))
    clock.now = "2026-11-03T00:01:00.000Z"

    done = reminders.dealt_with(reminder.id)

    assert done.next_day is None
    assert done.rings_at() is None
    assert reminders.pending() == []


def test_snoozed_it_rings_then_and_dealt_with_the_snooze_goes(
    notes: NoteRepository, reminders: ReminderRepository, clock: Clock
) -> None:
    note = notes.create("물 마시기")
    reminder = reminders.set(note.id, Rule(date(2026, 11, 3), NINE, SEOUL, "daily"))
    clock.now = "2026-11-03T00:00:30.000Z"
    later = datetime(2026, 11, 3, 0, 10, tzinfo=UTC)

    snoozed = reminders.snooze(reminder.id, later)
    assert snoozed.rings_at() == later

    clock.now = "2026-11-03T00:10:05.000Z"
    dealt = reminders.dealt_with(reminder.id)
    assert dealt.snoozed_until is None
    assert dealt.next_day == date(2026, 11, 4)


def test_dealt_with_before_its_time_it_still_moves_on(
    notes: NoteRepository, reminders: ReminderRepository
) -> None:
    note = notes.create("물 마시기")
    reminder = reminders.set(note.id, Rule(date(2026, 11, 3), NINE, SEOUL, "daily"))
    assert reminders.dealt_with(reminder.id).next_day == date(2026, 11, 4)


def test_turned_off_it_is_gone_but_its_row_stays_for_sync(
    notes: NoteRepository, reminders: ReminderRepository, db: apsw.Connection
) -> None:
    note = notes.create("회의 준비")
    reminders.set(note.id, Rule(date(2026, 11, 3), NINE, SEOUL))

    reminders.turn_off(note.id)

    assert reminders.of_note(note.id) is None
    assert reminders.pending() == []
    assert db.execute("SELECT count(*) FROM reminders WHERE deleted_at IS NOT NULL").fetchall() == [
        (1,)
    ]


def test_in_the_trash_it_waits_and_emptied_it_goes(
    notes: NoteRepository, reminders: ReminderRepository, db: apsw.Connection
) -> None:
    note = notes.create("회의 준비")
    reminder = reminders.set(note.id, Rule(date(2026, 11, 3), NINE, SEOUL))

    notes.delete(note.id)
    assert reminders.pending() == []
    notes.restore(note.id)
    assert reminders.pending() == [reminder]

    notes.delete(note.id)
    notes.purge(note.id)
    assert db.execute("SELECT count(*) FROM reminders").fetchall() == [(0,)]


def test_a_row_of_a_later_version_is_left_as_it_is(
    notes: NoteRepository, reminders: ReminderRepository, db: apsw.Connection
) -> None:
    note = notes.create("음력 생일")
    db.execute(
        "INSERT INTO reminders (id, note_id, kind, calendar, start_day, at_time, time_zone,"
        " repeat, next_day, created_at, updated_at) VALUES ('later', ?, 'note', 'korean',"
        " '2026-L08-15', '09:00', ?, 'yearly', '2026-10-25', '2026-10-01', '2026-10-01')",
        (note.id, SEOUL),
    )

    assert reminders.pending() == []
    assert reminders.of_note(note.id) is None

    # Set here, it takes that one's place: a note has one of its own.
    reminder = reminders.set(note.id, Rule(date(2026, 11, 3), NINE, SEOUL))
    assert reminder.id == "later"
    assert db.execute("SELECT count(*) FROM reminders").fetchall() == [(1,)]
