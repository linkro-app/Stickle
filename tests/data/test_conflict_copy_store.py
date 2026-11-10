"""Conflict copies kept here: which note each copies, whose text it holds and
when that was edited; the copies out of the trash, and what can be done with
one: both kept, or its text put in its original (the two change places, the
copy to the trash). Each a change sync takes across (a field's stamp)."""

import json
import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest

from stickle.core.clock import millis_at
from stickle.core.note import Conflict
from stickle.data import stamps, sync_state
from stickle.data.database import KEY_BYTES, open_database
from stickle.data.export import JSON_NAME, export_all
from stickle.data.notes import (
    Copy,
    NotACopyError,
    NoteDeletedError,
    NoteIsSecretError,
    NoteRepository,
)
from stickle.data.schema import MIGRATIONS, open_store
from stickle.data.sync_state import NoteState
from stickle.data.versions import VersionRepository
from stickle.importers.stickle_export import bring_in_export, read_export

KEY = secrets.token_bytes(KEY_BYTES)
LAPTOP = "1a2b3c4d-0000-4000-8000-000000000000"
EDITED = 1_762_483_200_000
COPY = "c0c0c0c0-0000-4000-8000-000000000000"


class Clock:
    def __init__(self) -> None:
        self.now = "2026-01-01T09:00:00.000Z"

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
def repo(db: apsw.Connection, clock: Clock) -> NoteRepository:
    return NoteRepository(db, clock, millis=lambda: millis_at(clock.now))


def copy_of(repo: NoteRepository, original: str, text: str, copy_id: str = COPY) -> str:
    """A copy as sync takes one in: of original, holding the laptop's text."""
    columns = {"conflict_of": original, "conflict_device": LAPTOP, "conflict_edited": EDITED}
    repo.take_in(copy_id, text, LAPTOP, columns, {}, {})
    return copy_id


def test_a_copy_says_what_it_copies(repo: NoteRepository) -> None:
    original = repo.create("회의 11시")
    copy_id = copy_of(repo, original.id, "회의 10시")

    copy = repo.get(copy_id)
    assert copy is not None and copy.conflict == Conflict(original.id, LAPTOP, EDITED)
    assert repo.get(original.id).conflict is None  # pyright: ignore[reportOptionalMemberAccess]
    assert repo.copies() == [Copy(copy_id, Conflict(original.id, LAPTOP, EDITED), True, True)]


def test_only_copies_out_of_the_trash_are_listed(repo: NoteRepository) -> None:
    original = repo.create("원본")
    first = copy_of(repo, original.id, "첫 사본", "c1c1c1c1-0000-4000-8000-000000000000")
    second = copy_of(repo, original.id, "둘째 사본", "c2c2c2c2-0000-4000-8000-000000000000")
    repo.delete(first)
    assert [each.id for each in repo.copies()] == [second]

    repo.delete(original.id)
    assert [(each.id, each.original) for each in repo.copies()] == [(second, False)]
    repo.purge(original.id)
    assert [(each.id, each.original, each.plain) for each in repo.copies()] == [
        (second, False, False)
    ]


def test_keeping_both_makes_it_an_ordinary_note(repo: NoteRepository, db: apsw.Connection) -> None:
    original = repo.create("원본")
    copy_id = copy_of(repo, original.id, "사본")
    before = repo.get(copy_id)
    assert before is not None

    after = repo.keep_both(copy_id)

    assert after.conflict is None and after.body == "사본" and after.deleted_at is None
    assert after.change_seq > before.change_seq
    assert stamps.of(db, copy_id)["conflict_of"][1] is None  # changed here
    assert repo.copies() == []
    again = repo.keep_both(copy_id)  # nothing more to do
    assert again.change_seq == after.change_seq


def test_using_the_copy_trades_the_texts(
    repo: NoteRepository, db: apsw.Connection, clock: Clock
) -> None:
    original = repo.create("회의 11시")
    copy_id = copy_of(repo, original.id, "회의 10시")
    clock.now = "2026-01-01T10:00:00.000Z"

    note = repo.use_copy(copy_id)

    assert (note.id, note.body, note.deleted_at) == (original.id, "회의 10시", None)
    copy = repo.get(copy_id)
    assert copy is not None and copy.body == "회의 11시"
    assert copy.deleted_at == "2026-01-01T10:00:00.000Z"
    # It holds the original's text now: written here, when that was.
    assert copy.conflict == Conflict(original.id, None, millis_at("2026-01-01T09:00:00.000Z"))
    rows = db.execute("SELECT id, text_from FROM notes WHERE id IN (?, ?)", (copy_id, original.id))
    assert {str(each): came for each, came in rows} == {original.id: LAPTOP, copy_id: None}
    earlier = VersionRepository(db, clock=clock).listed(original.id)
    assert [each.first_line for each in earlier] == ["회의 11시"]
    assert {"conflict_of", "deleted_at"} <= set(stamps.of(db, copy_id))


def test_taken_out_of_the_trash_it_changes_them_back(repo: NoteRepository, clock: Clock) -> None:
    original = repo.create("회의 11시")
    copy_id = copy_of(repo, original.id, "회의 10시")
    repo.use_copy(copy_id)
    clock.now = "2026-01-01T10:00:00.000Z"

    repo.restore(copy_id)
    note = repo.use_copy(copy_id)

    assert note.body == "회의 11시"
    copy = repo.get(copy_id)
    assert copy is not None and copy.body == "회의 10시" and copy.deleted_at is not None
    assert copy.conflict is not None and copy.conflict.device == LAPTOP


def test_a_locked_original_takes_the_copys_text_too(repo: NoteRepository) -> None:
    original = repo.create("잠근 메모")
    repo.set_locked(original.id, True)
    copy_id = copy_of(repo, original.id, "따로 고친 글")

    note = repo.use_copy(copy_id)

    assert note.body == "따로 고친 글" and note.locked


def test_what_cannot_change_places_is_left_as_it_is(repo: NoteRepository) -> None:
    ordinary = repo.create("보통 메모")
    with pytest.raises(NotACopyError):
        repo.use_copy(ordinary.id)

    secret = repo.create("비밀이 될 메모")
    copy_id = copy_of(repo, secret.id, "사본 글")
    repo.make_secret(secret.id, "제목", b"sealed", seal_earlier=lambda text: b"sealed")
    with pytest.raises(NoteIsSecretError):
        repo.use_copy(copy_id)
    copy = repo.get(copy_id)
    assert copy is not None and copy.body == "사본 글" and copy.deleted_at is None
    [listed] = repo.copies()
    assert (listed.original, listed.plain) == (True, False)

    trashed = repo.create("휴지통의 원본")
    other = copy_of(repo, trashed.id, "다른 사본", "c3c3c3c3-0000-4000-8000-000000000000")
    repo.delete(trashed.id)
    with pytest.raises(NoteDeletedError):
        repo.use_copy(other)
    assert repo.get(other).body == "다른 사본"  # pyright: ignore[reportOptionalMemberAccess]


# Copies made before the database kept this: marked at the next round


def test_the_upgrade_has_copies_made_before_merged_again(tmp_path: Path) -> None:
    path = tmp_path / "notes.db"
    connection = open_database(path, KEY)
    for number, step in enumerate(MIGRATIONS[:24], start=1):
        connection.execute(step)
        connection.pragma("user_version", number)
    copy = '{"body": {}, "fields": {"conflict_of": {"value": {"note": "x"}}}}'
    plain = '{"body": {}, "fields": {"color": {"value": "blue"}}}'
    for note_id, seen in (("copy", copy), ("plain", plain), ("odd", "not json")):
        connection.execute(
            "INSERT INTO notes (id, body, color, created_at, updated_at, content_hash,"
            " change_seq) VALUES (?, '글', 'yellow', '2026-11-01T00:00:00.000Z',"
            " '2026-11-01T00:00:00.000Z', '', 5)",
            (note_id,),
        )
        sync_state.put(connection, "vault", note_id, NoteState(seen, "tag", None, 5))
    connection.close()

    connection = open_store(path, KEY)
    try:
        assert sync_state.changed(connection, "vault") == ["copy"]
        assert NoteRepository(connection).get("copy").conflict is None  # pyright: ignore[reportOptionalMemberAccess]
    finally:
        connection.close()


def test_an_export_keeps_the_copy_and_brings_it_back_as_an_ordinary_note(
    repo: NoteRepository, db: apsw.Connection, tmp_path: Path
) -> None:
    original = repo.create("원본")
    copy_id = copy_of(repo, original.id, "사본")
    out = tmp_path / "out"
    out.mkdir()
    folder = export_all(db, out, "0.7.0").folder

    document = json.loads((folder / JSON_NAME).read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in document["tables"]["notes"]}
    assert rows[copy_id]["conflict_of"] == original.id
    assert rows[copy_id]["conflict_device"] == LAPTOP

    other = open_store(tmp_path / "other.db", KEY)
    try:
        bring_in_export(read_export(folder), other)
        brought = NoteRepository(other).get(copy_id)
        assert brought is not None and brought.body == "사본" and brought.conflict is None
    finally:
        other.close()
