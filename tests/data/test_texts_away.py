"""The text last seen in a vault this device left, put away: only its digest
kept, keyed by the vault's key, to tell on coming back what was not changed
here since."""

import json
import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest

from stickle.crypto.secret_notes import seal_text
from stickle.data import sync_connection, sync_state
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store

KEY = secrets.token_bytes(32)
VAULT_KEYS = {1: secrets.token_bytes(32)}
OTHER_KEYS = {1: secrets.token_bytes(32)}


@pytest.fixture
def db(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


def seen_of(text: str | None, secret: bool = False) -> str:
    body: dict[str, object] = {"id": "v1", "deleted": False, "text": text}
    if secret:
        body["text"] = None
        body["secret"] = {"sealed": "AAAA", "bound": "n"}
    return json.dumps({"body": body, "fields": {}})


def synced(db: apsw.Connection, vault: str, note_id: str, seen: str) -> None:
    with db:
        db.execute(
            "INSERT INTO sync_notes (vault, note_id, seen) VALUES (?, ?, ?)", (vault, note_id, seen)
        )


def row(db: apsw.Connection, vault: str, note_id: str) -> tuple[object, object]:
    found = db.execute(
        "SELECT seen, text_digest FROM sync_notes WHERE vault = ? AND note_id = ?",
        (vault, note_id),
    ).fetchall()
    return found[0][0], found[0][1]


def test_put_away_only_a_digest_of_the_text_stays(db: apsw.Connection) -> None:
    note = NoteRepository(db).create("그때 글")
    seen = seen_of("그때 글")
    synced(db, "v", note.id, seen)

    with db:
        sync_state.put_texts_away(db, "v", VAULT_KEYS)

    kept, digest = row(db, "v", note.id)
    assert isinstance(kept, str) and "그때 글" not in kept
    assert json.loads(kept)["body"]["id"] == "v1"  # its version, to merge on
    assert isinstance(digest, str) and "그때 글" not in digest


def test_a_secret_notes_sealed_text_is_left_as_it_is(db: apsw.Connection) -> None:
    synced(db, "v", "n", seen_of(None, secret=True))
    with db:
        sync_state.put_texts_away(db, "v", VAULT_KEYS)
    assert row(db, "v", "n") == (seen_of(None, secret=True), None)


def test_back_unchanged_here_the_text_is_as_it_was(db: apsw.Connection) -> None:
    note = NoteRepository(db).create("그때 글")
    seen = seen_of("그때 글")
    synced(db, "v", note.id, seen)
    with db:
        sync_state.put_texts_away(db, "v", VAULT_KEYS)

    with db:
        sync_state.take_texts_back(db, "v", VAULT_KEYS)

    kept, digest = row(db, "v", note.id)
    assert isinstance(kept, str) and json.loads(kept) == json.loads(seen)
    assert digest is None


def test_back_changed_here_since_the_text_stays_away(db: apsw.Connection) -> None:
    # Its next version is then made on the one seen: this computer's change.
    notes = NoteRepository(db)
    note = notes.create("그때 글")
    synced(db, "v", note.id, seen_of("그때 글"))
    with db:
        sync_state.put_texts_away(db, "v", VAULT_KEYS)
    notes.update_body(note.id, "여기서 고침")

    with db:
        sync_state.take_texts_back(db, "v", VAULT_KEYS)

    kept, digest = row(db, "v", note.id)
    assert isinstance(kept, str) and "text" not in json.loads(kept)["body"]
    assert digest is None


def test_a_digest_tells_nothing_with_another_vaults_key(db: apsw.Connection) -> None:
    note = NoteRepository(db).create("그때 글")
    synced(db, "v", note.id, seen_of("그때 글"))
    with db:
        sync_state.put_texts_away(db, "v", VAULT_KEYS)
    with db:
        sync_state.take_texts_back(db, "v", OTHER_KEYS)
    kept, _digest = row(db, "v", note.id)
    assert isinstance(kept, str) and "text" not in json.loads(kept)["body"]


def test_made_secret_or_emptied_since_its_digest_goes_too(db: apsw.Connection) -> None:
    notes = NoteRepository(db)
    note = notes.create("통장 비밀번호")
    synced(db, "v", note.id, seen_of("통장 비밀번호"))
    with db:
        sync_state.put_texts_away(db, "v", VAULT_KEYS)

    notes.make_secret(note.id, "은행", seal_text(secrets.token_bytes(32), note.id, "통장 비밀번호"))

    assert row(db, "v", note.id)[1] is None


def login(vault: str, keys: dict[int, bytes]) -> sync_connection.Login:
    return sync_connection.Login(vault, sync_connection.WEBDAV, "", "", "", keys)


def test_connected_to_another_vault_the_one_before_has_its_texts_put_away(
    db: apsw.Connection,
) -> None:
    note = NoteRepository(db).create("그때 글")
    sync_connection.keep(db, login("before", VAULT_KEYS))
    synced(db, "before", note.id, seen_of("그때 글"))
    synced(db, "after", note.id, seen_of("그때 글"))

    sync_connection.keep(db, login("after", OTHER_KEYS))

    assert row(db, "before", note.id)[1] is not None  # put away
    assert row(db, "after", note.id) == (seen_of("그때 글"), None)  # the one synced with

    sync_connection.keep(db, login("before", VAULT_KEYS))  # back to it

    kept, digest = row(db, "before", note.id)
    assert isinstance(kept, str) and json.loads(kept) == json.loads(seen_of("그때 글"))
    assert digest is None
