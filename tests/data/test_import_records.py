"""A note brought in from another app and made secret keeps no hash of its text,
and is not brought in again, as it was or changed."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest

from stickle.data import schema
from stickle.data.database import KEY_BYTES
from stickle.data.imports import SECRET_MARK, ImportRecords, text_hash
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store

KEY = secrets.token_bytes(KEY_BYTES)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


def hashes(connection: apsw.Connection, note_id: str) -> list[str]:
    rows = connection.execute(
        "SELECT content_hash FROM import_records WHERE note_id = ? ORDER BY content_hash",
        (note_id,),
    )
    return [str(row[0]) for row in rows]


def test_made_secret_its_hashes_go_and_it_is_not_brought_in_again(
    connection: apsw.Connection,
) -> None:
    notes, records = NoteRepository(connection), ImportRecords(connection)
    note = notes.create("통장 비밀번호 1234")
    records.record("sticky", "s1", "통장 비밀번호", note.id)  # as it came first
    records.record("sticky", "s1", "통장 비밀번호 1234", note.id)  # changed, came again
    other = notes.create("보통 메모")
    records.record("sticky", "s2", "보통 메모", other.id)

    notes.make_secret(note.id, "은행", b"sealed")

    assert hashes(connection, note.id) == [SECRET_MARK]
    assert records.brought_in("sticky", "s1", "통장 비밀번호 1234")
    assert records.brought_in("sticky", "s1", "changed in the other app since")
    assert not records.brought_in("sticky", "s3", "통장 비밀번호 1234")  # another note
    assert hashes(connection, other.id) == [text_hash("보통 메모")]  # the others as they were


def test_the_upgrade_marks_the_secret_notes_there_were(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "notes.db"
    monkeypatch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:9])
    connection = open_store(path, KEY)
    note = NoteRepository(connection).create("통장 비밀번호")
    ImportRecords(connection).record("sticky", "s1", "통장 비밀번호", note.id)
    with connection:  # made secret before version 10 marked the records
        connection.execute(
            "UPDATE notes SET body = '', secret_body = x'00', secret_title = '은행' WHERE id = ?",
            (note.id,),
        )
    connection.close()
    monkeypatch.undo()

    connection = open_store(path, KEY)
    try:
        assert hashes(connection, note.id) == [SECRET_MARK]
        assert ImportRecords(connection).brought_in("sticky", "s1", "통장 비밀번호")
    finally:
        connection.close()
