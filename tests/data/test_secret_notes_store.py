"""Secret notes in the database: their text only ever encrypted, and none of it
left behind when a note is made secret."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest

from stickle.crypto.secret_notes import open_text, seal_text
from stickle.data.notes import NoteIsSecretError, NoteNotBlankError, NoteRepository
from stickle.data.schema import open_store
from stickle.data.search import search

KEY = secrets.token_bytes(32)
SECRET_KEY = secrets.token_bytes(32)
TEXT = "은행 비밀번호 힌트 zqxjkv-plorbit"


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


def index_holds(connection: apsw.Connection, word: str) -> bool:
    """Whether any of the search index's stored segments holds the word's bytes."""
    rows = connection.execute("SELECT block FROM notes_fts_data").fetchall()
    data = b"".join(block for (block,) in rows if isinstance(block, bytes))
    return word.encode("utf-8") in data


def test_made_secret_its_text_is_only_encrypted(connection: apsw.Connection) -> None:
    notes = NoteRepository(connection)
    note = notes.create(TEXT)
    assert search(connection, "plorbit") == [note.id]
    assert index_holds(connection, "plo")

    secret = notes.make_secret(note.id, "은행", seal_text(SECRET_KEY, note.id, TEXT))

    assert secret.secret and secret.body == "" and secret.secret_title == "은행"
    assert search(connection, "plorbit") == []
    assert not index_holds(connection, "plo")  # gone from the index's segments too
    assert connection.pragma("secure_delete") == 1  # freed pages overwritten
    stored = connection.execute("SELECT body, secret_body FROM notes").fetchall()
    assert stored[0][0] == "" and isinstance(stored[0][1], bytes)
    assert TEXT.encode() not in stored[0][1]
    assert open_text(SECRET_KEY, note.id, secret.sealed or b"") == TEXT


def test_its_text_is_never_written_as_body(connection: apsw.Connection) -> None:
    notes = NoteRepository(connection)
    note = notes.make_secret(notes.create("x").id, "t", seal_text(SECRET_KEY, "n", ""))

    with pytest.raises(NoteIsSecretError):
        notes.update_body(note.id, "slipped out")
    sealed = seal_text(SECRET_KEY, note.id, "changed")
    assert notes.update_secret_body(note.id, sealed).sealed == sealed


def test_a_locked_secret_note_is_never_taken_for_an_empty_one(
    connection: apsw.Connection,
) -> None:
    # Closed with its X while locked, an empty-looking note would be removed for good.
    notes = NoteRepository(connection)
    note = notes.make_secret(notes.create("").id, "", seal_text(SECRET_KEY, "n", ""))

    with pytest.raises(NoteNotBlankError):
        notes.discard(note.id)
    assert notes.get(note.id) is not None


def test_made_ordinary_again_it_is_found_again(connection: apsw.Connection) -> None:
    notes = NoteRepository(connection)
    note = notes.create(TEXT)
    notes.make_secret(note.id, "은행", seal_text(SECRET_KEY, note.id, TEXT))

    back = notes.make_ordinary(note.id, TEXT)

    assert not back.secret and back.body == TEXT and back.secret_title is None
    assert search(connection, "plorbit") == [note.id]
