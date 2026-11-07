"""Each field of a note says when it was last changed, and where: every change
after the value it replaces, whatever the clock; nothing for a note never
changed, nor for its text, which goes by versions."""

import secrets
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import apsw
import pytest

from stickle.core.hlc import Hlc
from stickle.crypto.secret_notes import seal_text
from stickle.data import stamps
from stickle.data.database import open_database
from stickle.data.labels import LabelRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import MIGRATIONS, open_store

KEY = secrets.token_bytes(32)
SECRET_KEY = secrets.token_bytes(32)
LAPTOP = "0b6f3c2e-8f14-4d52-9a57-2f3c8d9e1a40"
NOW = 1_762_483_200_000


class Millis:
    """The time fields are stamped with, set by the test."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def millis() -> Millis:
    return Millis()


@pytest.fixture
def notes(connection: apsw.Connection, millis: Millis) -> NoteRepository:
    return NoteRepository(connection, millis=millis)


def stamped(connection: apsw.Connection, note_id: str) -> dict[str, stamps.Stamp]:
    return stamps.of(connection, note_id)


CHANGES: dict[str, Callable[[NoteRepository, str], object]] = {
    "color": lambda notes, note_id: notes.set_color(note_id, "blue"),
    "opacity": lambda notes, note_id: notes.set_opacity(note_id, 0.5),
    "always_on_top": lambda notes, note_id: notes.set_always_on_top(note_id, False),
    "hidden": lambda notes, note_id: notes.set_hidden(note_id, True),
    "collapsed": lambda notes, note_id: notes.set_collapsed(note_id, True),
    "locked": lambda notes, note_id: notes.set_locked(note_id, True),
    "deleted_at": lambda notes, note_id: notes.delete(note_id),
}


@pytest.mark.parametrize("field", CHANGES)
def test_a_change_stamps_its_field_with_the_time_and_here(
    notes: NoteRepository, connection: apsw.Connection, field: str
) -> None:
    note = notes.create("장보기")

    CHANGES[field](notes, note.id)

    assert stamped(connection, note.id) == {field: (Hlc(NOW), None)}


def test_a_category_and_a_secret_title_are_fields_too(
    notes: NoteRepository, connection: apsw.Connection
) -> None:
    category = LabelRepository(connection).create_category("집", "yellow")
    note = notes.create("장보기")

    notes.set_category(note.id, category.id)
    notes.make_secret(note.id, "비밀", seal_text(SECRET_KEY, note.id, "장보기"))

    assert set(stamped(connection, note.id)) == {"category", "secret_title"}


def test_a_new_note_and_its_text_have_none(
    notes: NoteRepository, connection: apsw.Connection
) -> None:
    note = notes.create("처음 글", color="blue")

    notes.update_body(note.id, "고친 글", keep_earlier=True)
    notes.replace_text(note.id, "노트북에서 쓴 글", came_from=LAPTOP)
    notes.set_color(note.id, "blue")  # no change

    assert stamped(connection, note.id) == {}


def test_changed_again_with_the_clock_set_back_it_is_still_later(
    notes: NoteRepository, connection: apsw.Connection, millis: Millis
) -> None:
    note = notes.create("")
    notes.set_color(note.id, "blue")

    millis.now -= 9 * 3_600_000
    notes.set_color(note.id, "pink")

    assert stamped(connection, note.id)["color"] == (Hlc(NOW, 1), None)


def test_a_value_from_a_clock_far_ahead_is_beaten_by_the_next_change_here(
    notes: NoteRepository, connection: apsw.Connection
) -> None:
    note = notes.create("")
    far = Hlc(NOW + 365 * 24 * 3_600_000)
    connection.execute(
        "INSERT INTO note_stamps (note_id, field, hlc, device) VALUES (?, 'hidden', ?, ?)",
        (note.id, far.text, LAPTOP),
    )

    notes.set_hidden(note.id, True)

    assert stamped(connection, note.id)["hidden"] == (far.following(), None)


def test_each_mark_is_a_field_of_its_own(
    notes: NoteRepository, connection: apsw.Connection, millis: Millis
) -> None:
    labels = LabelRepository(connection)
    own = labels.create_mark("검토", "star")
    shopping, trashed = notes.create("장보기"), notes.create("지난 일")
    notes.set_mark(shopping.id, "todo", True)
    notes.set_mark(shopping.id, own.id, True)
    notes.set_mark(trashed.id, own.id, True)
    notes.delete(trashed.id)

    millis.now += 1000
    notes.set_mark(shopping.id, "todo", False)
    notes.remove_mark(own.id)  # off every note that had it, in the trash too

    assert stamped(connection, shopping.id) == {
        "mark:todo": (Hlc(NOW + 1000), None),
        stamps.mark_field(own.id): (Hlc(NOW + 1000), None),
    }
    assert stamped(connection, trashed.id)[stamps.mark_field(own.id)] == (Hlc(NOW + 1000), None)


def test_a_category_deleted_with_or_without_its_notes(
    notes: NoteRepository, connection: apsw.Connection
) -> None:
    labels = LabelRepository(connection)
    home, work = labels.create_category("집", "yellow"), labels.create_category("일", "blue")
    at_home, at_work = notes.create("장보기"), notes.create("회의")
    notes.set_category(at_home.id, home.id)
    notes.set_category(at_work.id, work.id)

    notes.remove_category(home.id, with_notes=True)
    notes.remove_category(work.id, with_notes=False)

    assert set(stamped(connection, at_home.id)) == {"category", "deleted_at"}
    assert set(stamped(connection, at_work.id)) == {"category"}


def test_they_go_with_the_note(notes: NoteRepository, connection: apsw.Connection) -> None:
    emptied, discarded = notes.create("지난 일"), notes.create("")
    notes.set_color(emptied.id, "blue")
    notes.set_color(discarded.id, "blue")
    notes.delete(emptied.id)

    notes.empty_trash()
    notes.discard(discarded.id)

    assert connection.execute("SELECT count(*) FROM note_stamps").fetchall() == [(0,)]


# Notes from before: their fields take the time each note last changed


def make_v14(path: Path, rows: list[tuple[str, str, str]]) -> None:
    """A database as the version before made it: notes (id, made, last changed)."""
    connection = open_database(path, KEY)
    for number, step in enumerate(MIGRATIONS[:14], start=1):
        connection.execute(step)
        connection.pragma("user_version", number)
    for note_id, made, changed in rows:
        connection.execute(
            "INSERT INTO notes (id, body, color, created_at, updated_at, content_hash,"
            " change_seq) VALUES (?, '글', 'yellow', ?, ?, '', 1)",
            (note_id, made, changed),
        )
    connection.execute("INSERT INTO note_marks (note_id, mark_id) VALUES ('changed', 'todo')")
    connection.execute("INSERT INTO note_marks (note_id, mark_id) VALUES ('never', 'todo')")
    connection.close()


def test_fields_from_before_take_the_time_the_note_last_changed(tmp_path: Path) -> None:
    path = tmp_path / "notes.db"
    made = "2026-10-01T09:00:00.000Z"
    make_v14(
        path,
        [
            ("changed", made, "2026-11-06T12:34:56.789Z"),
            ("never", made, made),  # not changed since it was made: what's new, say
            ("unreadable", made, "어제"),
        ],
    )

    connection = open_store(path, KEY)
    changed, never = stamps.of(connection, "changed"), stamps.of(connection, "never")
    unreadable = stamps.of(connection, "unreadable")
    connection.close()

    last_changed = datetime(2026, 11, 6, 12, 34, 56, 789_000, tzinfo=UTC)
    at = (Hlc(int(last_changed.timestamp() * 1000)), None)
    assert changed == {
        "color": at,
        "opacity": at,
        "category": at,
        "always_on_top": at,
        "hidden": at,
        "collapsed": at,
        "locked": at,
        "deleted_at": at,
        "secret_title": at,
        "mark:todo": at,
    }
    assert never == {}
    assert set(unreadable.values()) == {(Hlc(0), None)}  # as never changed
