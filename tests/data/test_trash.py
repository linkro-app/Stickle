"""The trash: deleted notes, emptied only on purpose or after a year."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest

from stickle.core.layout import MAIN, Place
from stickle.data.database import KEY_BYTES
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import KEEP_DAYS, NoteNotBlankError, NoteNotDeletedError, NoteRepository
from stickle.data.schema import open_store, search_index_is_consistent
from stickle.data.search import search

KEY = secrets.token_bytes(KEY_BYTES)


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
    return NoteRepository(db, clock)


PLACE = Place(monitor=1, name="", rel_x=0.1, rel_y=0.1, width=260, height=240)


def test_the_trash_lists_deleted_notes_latest_first(repo: NoteRepository, clock: Clock) -> None:
    kept, first, second = (repo.create(t) for t in ("남길 것", "먼저 지움", "나중에 지움"))
    repo.delete(first.id)
    clock.now = "2026-01-02T09:00:00.000Z"
    repo.delete(second.id)

    assert [n.id for n in repo.deleted()] == [second.id, first.id]
    assert kept.id not in [n.id for n in repo.deleted()]


def test_emptying_a_note_leaves_only_a_deletion_record(
    repo: NoteRepository, db: apsw.Connection, clock: Clock
) -> None:
    note = repo.create("회의록을 내일까지")
    LayoutRepository(db, clock).save(note.id, {MAIN: PLACE})
    deleted = repo.delete(note.id)
    clock.now = "2026-01-05T09:00:00.000Z"

    repo.purge(note.id)

    assert repo.get(note.id) is None
    assert repo.deleted() == []
    assert LayoutRepository(db).places(note.id) == {}
    assert search(db, "회의록") == []
    assert search_index_is_consistent(db)
    assert repo.deletion_records() == [(note.id, deleted.deleted_at, "2026-01-05T09:00:00.000Z")]
    assert repo.last_deleted() is None  # nothing left to bring back


def test_a_note_with_no_text_is_removed_past_the_trash(
    repo: NoteRepository, db: apsw.Connection, clock: Clock
) -> None:
    earlier = repo.create("먼저 지운 메모")
    repo.delete(earlier.id)
    note = repo.create("잠깐")
    LayoutRepository(db, clock).save(note.id, {MAIN: PLACE})
    repo.update_body(note.id, " \n\t")

    repo.discard(note.id)

    assert repo.get(note.id) is None
    assert [n.id for n in repo.deleted()] == [earlier.id]
    assert repo.last_deleted() == repo.get(earlier.id)  # still the one to bring back
    assert LayoutRepository(db).places(note.id) == {}
    assert [record[0] for record in repo.deletion_records()] == [note.id]
    assert search_index_is_consistent(db)


def test_a_note_with_text_is_never_removed_past_the_trash(repo: NoteRepository) -> None:
    note = repo.create("남길 글")

    with pytest.raises(NoteNotBlankError):
        repo.discard(note.id)
    stored = repo.get(note.id)
    assert stored is not None and not stored.deleted
    assert repo.deletion_records() == []


def test_only_a_note_in_the_trash_can_be_emptied(repo: NoteRepository) -> None:
    note = repo.create("살아 있는 메모")

    with pytest.raises(NoteNotDeletedError):
        repo.purge(note.id)
    assert repo.get(note.id) is not None


def test_emptying_the_trash_takes_every_deleted_note_and_nothing_else(
    repo: NoteRepository,
) -> None:
    kept = repo.create("남길 것")
    for text in ("하나", "둘"):
        repo.delete(repo.create(text).id)

    assert repo.empty_trash() == 2
    assert [n.id for n in repo.all()] == [kept.id]
    assert len(repo.deletion_records()) == 2


def test_emptying_is_all_or_nothing(
    repo: NoteRepository, db: apsw.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    for text in ("하나", "둘"):
        repo.delete(repo.create(text).id)
    real_purge = repo.purge
    calls: list[str] = []

    def fails_on_the_second(note_id: str) -> None:
        calls.append(note_id)
        if len(calls) == 2:
            raise apsw.IOError("disk went away")
        real_purge(note_id)

    monkeypatch.setattr(repo, "purge", fails_on_the_second)
    with pytest.raises(apsw.IOError):
        repo.empty_trash()

    assert len(repo.deleted()) == 2
    assert repo.deletion_records() == []


def test_a_year_in_the_trash_empties_a_note_and_a_year_on_forgets_it(
    repo: NoteRepository, clock: Clock
) -> None:
    old = repo.create("오래된 것")
    repo.delete(old.id)
    clock.now = "2026-06-01T09:00:00.000Z"
    recent = repo.create("최근 것")
    repo.delete(recent.id)

    clock.now = "2027-01-01T09:00:01.000Z"  # a year and a second after the first deletion
    assert repo.purge_expired() == 1
    assert [n.id for n in repo.deleted()] == [recent.id]
    assert [record[0] for record in repo.deletion_records()] == [old.id]

    clock.now = "2028-01-01T09:00:02.000Z"
    repo.purge_expired()
    assert [record[0] for record in repo.deletion_records()] == [recent.id]
    assert KEEP_DAYS == 365


def test_notes_younger_than_a_year_stay(repo: NoteRepository, clock: Clock) -> None:
    note = repo.create("지운 지 364일")
    repo.delete(note.id)
    clock.now = "2026-12-31T09:00:00.000Z"

    assert repo.purge_expired() == 0
    assert [n.id for n in repo.deleted()] == [note.id]
