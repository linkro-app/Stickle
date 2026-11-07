"""Each field of a category, a mark, a colour of the user's own and a note
brought in says when it was last changed, and where, as a note's fields do;
and each such item, the change number it was last changed at. Rows from
before take the time they last changed."""

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import apsw
import pytest

from stickle.core.hlc import Hlc
from stickle.data import record_stamps
from stickle.data.colors import ColorRepository
from stickle.data.database import open_database
from stickle.data.imports import ImportRecords
from stickle.data.labels import LabelRepository
from stickle.data.notes import NoteRepository
from stickle.data.record_stamps import CATEGORY, COLOR, IMPORT, MARK, import_item
from stickle.data.schema import MIGRATIONS, open_store
from stickle.data.stamps import Stamp

KEY = secrets.token_bytes(32)
NOW = 1_762_483_200_000
HERE: Stamp = (Hlc(NOW), None)


class Millis:
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
def labels(connection: apsw.Connection, millis: Millis) -> LabelRepository:
    return LabelRepository(connection, millis=millis)


@pytest.fixture
def notes(connection: apsw.Connection, millis: Millis) -> NoteRepository:
    return NoteRepository(connection, millis=millis)


@pytest.fixture
def colors(connection: apsw.Connection, millis: Millis) -> ColorRepository:
    return ColorRepository(connection, millis=millis)


def test_a_new_category_mark_or_colour_has_no_stamp_but_counts_as_changed(
    connection: apsw.Connection, labels: LabelRepository, colors: ColorRepository
) -> None:
    category = labels.create_category("일", "blue")
    mark = labels.create_mark("나중에", "flag")
    color = colors.add("#336699")

    for kind, item in ((CATEGORY, category.id), (MARK, mark.id), (COLOR, color.id)):
        assert record_stamps.of(connection, kind, item) == {}
        assert record_stamps.change_seq(connection, kind, item) > 0
    seqs = [
        record_stamps.change_seq(connection, kind, item)
        for kind, item in ((CATEGORY, category.id), (MARK, mark.id), (COLOR, color.id))
    ]
    assert seqs == sorted(set(seqs))


def test_each_change_to_a_category_stamps_its_field(
    connection: apsw.Connection, labels: LabelRepository, millis: Millis
) -> None:
    home = labels.create_category("집", "yellow")
    labels.create_category("일", "blue")

    labels.rename_category(home.id, "우리 집")
    assert record_stamps.of(connection, CATEGORY, home.id) == {"name": HERE}
    millis.now += 1000
    labels.set_category_color(home.id, "green")
    labels.set_category_icon(home.id, "house")
    later: Stamp = (Hlc(NOW + 1000), None)
    assert record_stamps.of(connection, CATEGORY, home.id) == {
        "name": HERE,
        "color": later,
        "icon": later,
    }
    labels.move_category(home.id, 1)  # both move
    assert record_stamps.of(connection, CATEGORY, home.id)["position"] == later


def test_deleting_a_category_and_bringing_it_back_with_a_note(
    connection: apsw.Connection, labels: LabelRepository, notes: NoteRepository, millis: Millis
) -> None:
    home = labels.create_category("집", "yellow")
    note = notes.create("장보기")
    notes.set_category(note.id, home.id)
    notes.remove_category(home.id, with_notes=True)
    assert record_stamps.of(connection, CATEGORY, home.id) == {"deleted_at": HERE}

    millis.now += 1000
    notes.restore(note.id)

    back: Stamp = (Hlc(NOW + 1000), None)
    assert record_stamps.of(connection, CATEGORY, home.id) == {"deleted_at": back, "position": back}


def test_each_change_to_a_mark_stamps_its_field(
    connection: apsw.Connection, labels: LabelRepository, notes: NoteRepository
) -> None:
    own = labels.create_mark("나중에", "flag")
    labels.rename_mark("todo", "할 것")
    labels.set_mark_icon(own.id, "star")
    labels.move_mark(own.id, -1)
    notes.remove_mark("urgent")

    assert record_stamps.of(connection, MARK, "todo") == {"name": HERE}
    assert record_stamps.of(connection, MARK, own.id) == {"icon": HERE, "position": HERE}
    assert record_stamps.of(connection, MARK, "urgent") == {"deleted_at": HERE}
    assert record_stamps.of(connection, MARK, "important") == {}


def test_each_change_to_a_colour_stamps_its_field(
    connection: apsw.Connection, colors: ColorRepository
) -> None:
    first, second = colors.add("#336699"), colors.add("#993366")
    colors.move(second.id, -1)
    colors.remove(first.id)

    assert record_stamps.of(connection, COLOR, first.id) == {"position": HERE, "deleted_at": HERE}
    assert record_stamps.of(connection, COLOR, second.id) == {"position": HERE}


def test_changed_again_with_the_clock_set_back_it_is_still_later(
    connection: apsw.Connection, labels: LabelRepository, millis: Millis
) -> None:
    home = labels.create_category("집", "yellow")
    labels.rename_category(home.id, "우리 집")
    millis.now -= 60_000

    labels.rename_category(home.id, "본가")

    assert record_stamps.of(connection, CATEGORY, home.id) == {"name": (Hlc(NOW, 1), None)}


def test_a_note_brought_in_and_made_secret_stamps_its_field(
    connection: apsw.Connection, notes: NoteRepository, millis: Millis
) -> None:
    records = ImportRecords(connection, millis=millis)
    note = notes.create("장보기")
    records.record("sticky_notes", "a1", "장보기", note.id)
    records.record("sticky_notes", "a1", "장보기", note.id)  # there already: not again
    item = import_item("sticky_notes", "a1")
    field = record_stamps.imported_field(note.id)
    assert record_stamps.of(connection, IMPORT, item) == {field: HERE}
    seq = record_stamps.change_seq(connection, IMPORT, item)

    millis.now += 1000
    notes.make_secret(note.id, "비밀", b"sealed")

    assert record_stamps.of(connection, IMPORT, item) == {field: (Hlc(NOW + 1000), None)}
    assert record_stamps.change_seq(connection, IMPORT, item) > seq


# Rows from before


def make_v16(path: Path) -> None:
    connection = open_database(path, KEY)
    for number, step in enumerate(MIGRATIONS[:16], start=1):
        connection.execute(step)
        connection.pragma("user_version", number)
    connection.execute(
        "INSERT INTO categories (id, name, color, position, created_at, updated_at) VALUES"
        " ('home', '집', 'yellow', 1, '2026-10-01T09:00:00.000Z', '2026-11-06T12:34:56.789Z')"
    )
    connection.execute(
        "INSERT INTO marks (id, name, icon, position, updated_at) VALUES"
        " ('later', '나중에', 'flag', 5, '2026-10-02T10:00:00.000Z')"
    )
    connection.execute(
        "UPDATE marks SET name = '할 것', updated_at = ? WHERE id = 'todo'",
        ("2026-11-06T12:34:56.789Z",),
    )
    connection.execute(
        "INSERT INTO custom_colors (id, hex, position, updated_at) VALUES"
        " ('blue', '#336699', 1, '2026-11-06T12:34:56.789Z')"
    )
    connection.execute(
        "INSERT INTO import_records (source, source_id, content_hash, note_id, imported_at)"
        " VALUES ('xpad', 'n1', 'abc', 'note-1', '2026-11-06T12:34:56.789Z')"
    )
    connection.close()


def test_rows_from_before_take_the_time_they_last_changed(tmp_path: Path) -> None:
    path = tmp_path / "notes.db"
    make_v16(path)

    connection = open_store(path, KEY)
    try:
        last = datetime(2026, 11, 6, 12, 34, 56, 789_000, tzinfo=UTC)
        at: Stamp = (Hlc(int(last.timestamp() * 1000)), None)
        made = datetime(2026, 10, 2, 10, tzinfo=UTC)
        assert record_stamps.of(connection, CATEGORY, "home") == {
            name: at for name in ("name", "color", "icon", "position", "deleted_at")
        }
        assert record_stamps.of(connection, MARK, "todo") == {
            name: at for name in ("name", "icon", "position", "deleted_at")
        }
        assert set(record_stamps.of(connection, MARK, "later").values()) == {
            (Hlc(int(made.timestamp() * 1000)), None)
        }
        assert record_stamps.of(connection, MARK, "urgent") == {}  # built in, never changed
        assert record_stamps.of(connection, COLOR, "blue") == {
            name: at for name in ("hex", "position", "deleted_at")
        }
        assert record_stamps.of(connection, IMPORT, import_item("xpad", "n1")) == {
            "note:note-1": at
        }
        assert connection.execute(
            "SELECT id, created_at FROM marks WHERE id IN ('later', 'todo') ORDER BY id"
        ).fetchall() == [("later", "2026-10-02T10:00:00.000Z"), ("todo", None)]
        assert connection.execute("SELECT created_at FROM custom_colors").fetchall() == [
            ("2026-11-06T12:34:56.789Z",)
        ]
    finally:
        connection.close()
