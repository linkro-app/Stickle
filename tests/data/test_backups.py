import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from stickle.data import backups
from stickle.data.backups import (
    Contents,
    contents,
    due,
    finish_restore,
    prune,
    stage_restore,
    take,
    take_daily,
)
from stickle.data.database import KEY_BYTES, WrongKeyError, open_database
from stickle.data.notes import NoteRepository
from stickle.data.schema import latest_version, open_store

KEY = secrets.token_bytes(KEY_BYTES)


class FakeClock:
    def __init__(self, now: str = "2026-10-18T12:00:00.000Z") -> None:
        self.now = now

    def __call__(self) -> str:
        return self.now


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "notes.db"


@pytest.fixture
def connection(database: Path) -> Iterator[apsw.Connection]:
    connection = open_store(database, KEY)
    yield connection
    connection.close()


def bodies(connection: apsw.Connection) -> list[str]:
    return sorted(note.body for note in NoteRepository(connection).visible())


def test_a_backup_is_the_notes_encrypted_with_the_same_key(
    connection: apsw.Connection, database: Path
) -> None:
    NoteRepository(connection).create("비밀 SECRETWORD")

    path = take(connection, database, FakeClock())

    assert b"SECRETWORD" not in path.read_bytes()
    assert "비밀".encode() not in path.read_bytes()
    copy = open_database(path, KEY, read_only=True)
    assert bodies(copy) == ["비밀 SECRETWORD"]
    copy.close()
    with pytest.raises(WrongKeyError):
        open_database(path, secrets.token_bytes(KEY_BYTES), read_only=True)


def test_backups_are_kept_apart_from_the_upgrade_copies(
    connection: apsw.Connection, database: Path
) -> None:
    path = take(connection, database, FakeClock())

    assert path.parent == database.parent / "backups" / "daily"
    assert [backup.path for backup in backups.backups(database)] == [path]


def test_looking_at_a_backup_changes_nothing_in_it(
    connection: apsw.Connection, database: Path
) -> None:
    path = take(connection, database, FakeClock())
    before = path.read_bytes()

    assert contents(path, KEY, latest_version()) == Contents(0, newer=False)

    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_an_unfinished_copy_is_never_listed_and_is_cleared_next_time(
    connection: apsw.Connection, database: Path
) -> None:
    folder = backups.folder(database)
    folder.mkdir(parents=True)
    stale = folder / "notes-20261017T120000000Z.db.partial"
    stale.write_bytes(b"half")

    assert backups.backups(database) == []
    take(connection, database, FakeClock())

    assert not stale.exists()


def test_a_failed_copy_leaves_nothing_behind(
    connection: apsw.Connection, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full_disk(*_: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "replace", full_disk)

    with pytest.raises(OSError):
        take(connection, database, FakeClock())

    assert backups.backups(database) == []
    assert list(backups.folder(database).iterdir()) == []


def test_one_backup_a_day(connection: apsw.Connection, database: Path) -> None:
    clock = FakeClock("2026-10-18T12:00:00.000Z")
    assert due(database, clock)

    assert take_daily(connection, database, clock) is not None
    clock.now = "2026-10-18T12:30:00.000Z"
    assert take_daily(connection, database, clock) is None
    clock.now = "2026-10-19T12:00:00.000Z"
    assert take_daily(connection, database, clock) is not None

    assert len(backups.backups(database)) == 2


def test_backups_older_than_a_week_go(connection: apsw.Connection, database: Path) -> None:
    clock = FakeClock()
    for day in range(10, 19):
        clock.now = f"2026-10-{day:02d}T12:00:00.000Z"
        take_daily(connection, database, clock)

    days = sorted(backup.taken.day for backup in backups.backups(database))
    assert days == [11, 12, 13, 14, 15, 16, 17, 18]


def test_the_newest_backup_stays_however_old(connection: apsw.Connection, database: Path) -> None:
    clock = FakeClock("2026-10-01T12:00:00.000Z")
    take(connection, database, clock)
    clock.now = "2026-10-02T12:00:00.000Z"
    newest = take(connection, database, clock)

    clock.now = "2027-01-01T12:00:00.000Z"
    prune(database, clock)

    assert [backup.path for backup in backups.backups(database)] == [newest]


def test_the_contents_count_the_notes_not_in_the_trash(
    connection: apsw.Connection, database: Path
) -> None:
    notes = NoteRepository(connection)
    notes.create("하나")
    notes.create("둘")
    notes.delete(notes.create("지운 것").id)

    path = take(connection, database, FakeClock())

    assert contents(path, KEY, latest_version()) == Contents(2, newer=False)
    assert contents(path, secrets.token_bytes(KEY_BYTES), latest_version()) is None
    assert contents(path, KEY, latest_version() - 1) == Contents(0, newer=True)


def test_going_back_to_a_backup(database: Path) -> None:
    connection = open_store(database, KEY)
    notes = NoteRepository(connection)
    notes.create("그때 메모")
    backup = take(connection, database, FakeClock())
    notes.create("나중에 쓴 메모")
    key_file = database.with_name("keys.json")
    key_file.write_text("{}")

    stage_restore(database, backup)
    assert bodies(connection) == ["그때 메모", "나중에 쓴 메모"]  # nothing changed yet
    connection.close()
    assert finish_restore(database)

    reopened = open_store(database, KEY)
    assert bodies(reopened) == ["그때 메모"]
    reopened.close()
    assert key_file.read_text() == "{}"
    assert backup.exists()


def test_nothing_staged_nothing_restored(database: Path) -> None:
    open_store(database, KEY).close()

    assert not finish_restore(database)


@settings(max_examples=15, deadline=None)
@given(
    steps=st.lists(
        st.one_of(st.text(min_size=1, max_size=8).map(lambda s: s.replace("\0", "")), st.none()),
        min_size=1,
        max_size=6,
    )
)
def test_going_back_and_forth_never_loses_a_state(
    tmp_path_factory: pytest.TempPathFactory, steps: list[str | None]
) -> None:
    """Going back first backs up the notes as they are, so every state the user
    had stays reachable: write notes (text) or go back to the oldest backup (None)."""
    database = tmp_path_factory.mktemp("back") / "notes.db"
    clock = FakeClock("2026-10-18T00:00:00.000Z")
    seconds = 0
    seen: list[list[str]] = []
    for step in steps:
        connection = open_store(database, KEY)
        seconds += 1
        clock.now = f"2026-10-18T00:00:{seconds:02d}.000Z"
        if step is not None:
            NoteRepository(connection).create(step)
            take(connection, database, clock)
            seen.append(bodies(connection))
            connection.close()
            continue
        listed = backups.backups(database)
        if not listed:
            connection.close()
            continue
        take(connection, database, clock, before_restore=True)
        seen.append(bodies(connection))
        stage_restore(database, listed[-1].path)
        connection.close()
        finish_restore(database)
    reachable: list[list[str]] = []
    for backup in backups.backups(database):
        copy = open_database(backup.path, KEY, read_only=True)
        reachable.append(bodies(copy))
        copy.close()
    for state in seen:
        assert state in reachable
