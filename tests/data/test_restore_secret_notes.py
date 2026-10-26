"""Going back to a backup never brings back, in the clear, the earlier text of a
note made secret since: in the copy put in place it is sealed with the secret
notes' key, or the note keeps its text as it is now. The backup is not changed."""

import hashlib
import secrets
import shutil
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from preview_fixture_0_5 import FIXTURE, NOTES, STORE_KEY

from stickle.crypto.secret_notes import open_text, seal_text
from stickle.data.backups import (
    RESTORING,
    finish_restore,
    keep_secret_notes_secret,
    old_secret_texts,
    secret_notes_now,
    stage_restore,
    take,
)
from stickle.data.database import KEY_BYTES, open_database
from stickle.data.notes import NoteRepository
from stickle.data.schema import latest_version, open_store, schema_version
from stickle.data.search import search

KEY = secrets.token_bytes(KEY_BYTES)
SECRET_KEY = secrets.token_bytes(32)
EARLIER = "통장 비밀번호 PLAINWORD 1234"


class FakeClock:
    def __init__(self, now: str = "2026-10-27T09:00:00.000Z") -> None:
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


def seal(note_id: str, text: str) -> bytes:
    return seal_text(SECRET_KEY, note_id, text)


def made_secret(notes: NoteRepository, note_id: str, text: str, title: str = "은행") -> None:
    notes.make_secret(note_id, title, seal(note_id, text))


def staged(database: Path) -> Path:
    return database.with_name(database.name + RESTORING)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def row(path: Path, key: bytes, note_id: str) -> tuple[str, bytes | None, str | None]:
    copy = open_database(path, key, read_only=True)
    try:
        body, sealed, title = copy.execute(
            "SELECT body, secret_body, secret_title FROM notes WHERE id = ?", (note_id,)
        ).fetchall()[0]
        return str(body), None if sealed is None else bytes(sealed), title  # pyright: ignore[reportArgumentType, reportReturnType]
    finally:
        copy.close()


def found_in(path: Path, key: bytes, term: str) -> list[str]:
    copy = open_database(path, key, read_only=True)
    try:
        return search(copy, term)
    finally:
        copy.close()


def test_only_notes_secret_now_and_plain_in_the_backup_are_counted(
    connection: apsw.Connection, database: Path
) -> None:
    notes = NoteRepository(connection)
    plain_then = notes.create(EARLIER)
    secret_then = notes.create("이미 비밀")
    made_secret(notes, secret_then.id, "이미 비밀")
    empty_then = notes.create("")
    ordinary = notes.create("보통 메모")
    backup = take(connection, database, FakeClock())
    made_secret(notes, plain_then.id, EARLIER)
    notes.update_body(empty_then.id, "나중에 쓴 글")
    made_secret(notes, empty_then.id, "나중에 쓴 글")

    now = secret_notes_now(connection)

    assert set(now) == {plain_then.id, secret_then.id, empty_then.id}
    assert ordinary.id not in now
    assert old_secret_texts(backup, KEY, now) == 1  # the others: secret, or empty, then
    assert old_secret_texts(backup, KEY, {}) == 0


def test_with_the_key_their_earlier_text_comes_back_sealed_and_out_of_search(
    connection: apsw.Connection, database: Path
) -> None:
    notes = NoteRepository(connection)
    note = notes.create(EARLIER)
    other = notes.create("다른 메모 OTHERWORD")
    backup = take(connection, database, FakeClock())
    made_secret(notes, note.id, "지금 글")
    before = digest(backup)
    stage_restore(database, backup)

    kept = keep_secret_notes_secret(database, KEY, secret_notes_now(connection), seal)

    assert kept == 1
    body, sealed, title = row(staged(database), KEY, note.id)
    assert body == "" and title == "은행"
    assert sealed is not None and open_text(SECRET_KEY, note.id, sealed) == EARLIER
    assert found_in(staged(database), KEY, "PLAINWORD") == []
    assert found_in(staged(database), KEY, "비밀번호") == []
    assert found_in(staged(database), KEY, "OTHERWORD") == [other.id]  # the rest as it was
    assert digest(backup) == before  # the backup itself as it was
    assert row(backup, KEY, note.id)[0] == EARLIER


def test_without_the_key_they_keep_their_text_as_now(
    connection: apsw.Connection, database: Path
) -> None:
    notes = NoteRepository(connection)
    note = notes.create(EARLIER)
    backup = take(connection, database, FakeClock())
    made_secret(notes, note.id, "지금 글", title="새 제목")
    now = secret_notes_now(connection)
    stage_restore(database, backup)

    assert keep_secret_notes_secret(database, KEY, now, None) == 1

    body, sealed, title = row(staged(database), KEY, note.id)
    assert (body, sealed, title) == ("", now[note.id].sealed, "새 제목")
    assert open_text(SECRET_KEY, note.id, now[note.id].sealed) == "지금 글"
    assert found_in(staged(database), KEY, "PLAINWORD") == []


def test_one_in_the_trash_is_kept_secret_too(connection: apsw.Connection, database: Path) -> None:
    notes = NoteRepository(connection)
    note = notes.create(EARLIER)
    notes.delete(note.id)
    backup = take(connection, database, FakeClock())
    notes.restore(note.id)
    made_secret(notes, note.id, EARLIER)
    notes.delete(note.id)
    stage_restore(database, backup)

    assert keep_secret_notes_secret(database, KEY, secret_notes_now(connection), seal) == 1

    body, sealed, _title = row(staged(database), KEY, note.id)
    assert body == "" and sealed is not None


def test_nothing_to_keep_leaves_the_copy_as_the_backup(
    connection: apsw.Connection, database: Path
) -> None:
    NoteRepository(connection).create("보통 메모")
    backup = take(connection, database, FakeClock())
    stage_restore(database, backup)

    assert keep_secret_notes_secret(database, KEY, secret_notes_now(connection), seal) == 0
    assert digest(staged(database)) == digest(backup)


def test_a_backup_from_before_secret_notes_is_upgraded_in_the_copy_only(
    tmp_path: Path, database: Path
) -> None:
    # A backup 0.5.1 made: no column for secret notes yet.
    backup = tmp_path / "backups" / "daily" / "notes-20261001T090000000Z.db"
    backup.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE / "store" / "notes.db", backup)
    shutil.copyfile(backup, database)
    connection = open_store(database, STORE_KEY)
    notes = NoteRepository(connection)
    note = next(n for n in notes.visible() if n.body == NOTES[0])
    made_secret(notes, note.id, NOTES[0])
    now = secret_notes_now(connection)
    before = digest(backup)

    assert old_secret_texts(backup, STORE_KEY, now) == 1
    stage_restore(database, backup)
    assert keep_secret_notes_secret(database, STORE_KEY, now, seal) == 1
    connection.close()

    copy = open_database(staged(database), STORE_KEY, read_only=True)
    assert schema_version(copy) == latest_version()
    copy.close()
    body, sealed, _title = row(staged(database), STORE_KEY, note.id)
    assert body == "" and sealed is not None
    assert open_text(SECRET_KEY, note.id, sealed) == NOTES[0]
    assert digest(backup) == before
    assert not list((tmp_path / "backups").glob("notes.db*"))  # no copy of the copy aside

    assert finish_restore(database)  # as Stickle does once closed, then starts again
    connection = open_store(database, STORE_KEY)
    restored = NoteRepository(connection).get(note.id)
    assert restored is not None and restored.secret and restored.body == ""
    assert search(connection, "0.5에서") == []
    connection.close()
