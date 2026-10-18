"""Bringing notes in: once each, with what they carry, all or nothing."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest

from stickle.core.layout import MAIN, Place, Rect
from stickle.data.labels import LabelRepository
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.importers.base import ImportedNote, ImportResult
from stickle.importers.bring_in import bring_in, new_notes

KEY = secrets.token_bytes(32)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


def result(*notes: ImportedNote) -> ImportResult:
    return ImportResult(notes=list(notes))


def test_notes_come_with_what_they_carry(connection: apsw.Connection) -> None:
    LabelRepository(connection).create_category("회사", "blue")
    imported = result(
        ImportedNote("a", "일", "sky", category="회사", locked=True, place=Rect(10, 20, 200, 150)),
        ImportedNote("b", "집안일", "mint", category="집", hidden=True, always_on_top=False),
    )
    place = Place(1, "screen", 0.1, 0.2, 200, 150)

    brought = bring_in("xpad", imported, connection, place_for=lambda _: place)

    repo = NoteRepository(connection)
    first, second = (repo.get(note_id) for note_id in brought.note_ids)
    assert first is not None and second is not None
    categories = {c.name: c.id for c in LabelRepository(connection).categories()}
    assert set(categories) == {"회사", "집"}  # 회사 reused, 집 made
    assert (first.body, first.color, first.label, first.locked) == (
        "일",
        "sky",
        categories["회사"],
        True,
    )
    assert (second.hidden, second.always_on_top, second.label) == (True, False, categories["집"])
    assert LayoutRepository(connection).places(first.id) == {MAIN: place}


def test_bringing_in_again_brings_only_what_is_new(connection: apsw.Connection) -> None:
    bring_in("xpad", result(ImportedNote("a", "그대로", "yellow")), connection)

    again = result(ImportedNote("a", "그대로", "yellow"), ImportedNote("b", "새 것", "pink"))
    assert new_notes("xpad", again, connection) == 1
    brought = bring_in("xpad", again, connection)

    assert brought.already_here == 1
    assert len(brought.note_ids) == 1
    changed = result(ImportedNote("a", "고친 글", "yellow"))
    assert len(bring_in("xpad", changed, connection).note_ids) == 1  # changed since: comes again
    assert len(NoteRepository(connection).all()) == 3


def test_the_same_id_from_another_app_is_another_note(connection: apsw.Connection) -> None:
    bring_in("xpad", result(ImportedNote("a", "글", "yellow")), connection)

    assert (
        len(bring_in("other", result(ImportedNote("a", "글", "yellow")), connection).note_ids) == 1
    )


def test_where_a_note_came_from_ends_it_when_asked(connection: apsw.Connection) -> None:
    imported = result(ImportedNote("a", "장보기\n", "yellow"), ImportedNote("b", "", "yellow"))

    brought = bring_in("xpad", imported, connection, source_line="xpad에서 가져옴 · 2026-10-18")

    repo = NoteRepository(connection)
    bodies = [note.body for note_id in brought.note_ids if (note := repo.get(note_id))]
    assert bodies == [
        "장보기\n\n==xpad에서 가져옴 · 2026-10-18==",
        "==xpad에서 가져옴 · 2026-10-18==",
    ]
    # Recorded as it came: bringing in again knows them, line or not.
    assert new_notes("xpad", imported, connection) == 0


def test_all_or_nothing(connection: apsw.Connection) -> None:
    def broken(_: Rect) -> Place | None:
        raise OSError("screens gone")

    imported = result(
        ImportedNote("a", "첫째", "yellow"),
        ImportedNote("b", "둘째", "yellow", place=Rect(0, 0, 100, 100)),
    )

    with pytest.raises(OSError):
        bring_in("xpad", imported, connection, place_for=broken)

    assert NoteRepository(connection).all() == []
    assert new_notes("xpad", imported, connection) == 2
