import secrets
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import apsw
import pytest

from stickle.core.labels import BUILT_IN_MARKS, Mark
from stickle.data.database import KEY_BYTES
from stickle.data.labels import CategoryNameError, LabelRepository, MarkNameError
from stickle.data.notes import NoteDeletedError, NoteRepository
from stickle.data.schema import open_store

KEY = secrets.token_bytes(KEY_BYTES)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def labels(db: apsw.Connection) -> LabelRepository:
    return LabelRepository(db)


@pytest.fixture
def notes(db: apsw.Connection) -> NoteRepository:
    return NoteRepository(db)


def test_a_new_database_has_the_built_in_marks_and_no_categories(labels: LabelRepository) -> None:
    assert [mark.id for mark in labels.marks()] == list(BUILT_IN_MARKS)
    assert all(mark.name is None for mark in labels.marks())  # shown in the chosen language
    assert labels.categories() == []


def test_categories_are_made_in_order(labels: LabelRepository) -> None:
    work = labels.create_category("  Work ", "blue")
    home = labels.create_category("Home", "green")

    assert work.name == "Work"
    assert [c.id for c in labels.categories()] == [work.id, home.id]
    assert labels.category(home.id) == home


@pytest.mark.parametrize("name", ["", "   ", "\n"])
def test_a_category_needs_a_name(labels: LabelRepository, name: str) -> None:
    with pytest.raises(CategoryNameError):
        labels.create_category(name, "blue")


def test_two_categories_cannot_share_a_name(labels: LabelRepository) -> None:
    labels.create_category("회사", "blue")
    with pytest.raises(CategoryNameError):
        labels.create_category("회사", "pink")
    labels.create_category("Work", "blue")
    with pytest.raises(CategoryNameError):
        labels.create_category("WORK", "pink")


def test_a_category_colour_is_a_palette_key(labels: LabelRepository) -> None:
    with pytest.raises(ValueError):
        labels.create_category("Work", "#123456")


def test_a_note_gets_a_category_and_loses_it(
    labels: LabelRepository, notes: NoteRepository
) -> None:
    work = labels.create_category("Work", "blue")
    note = notes.create("Plan", "pink")

    on = notes.set_category(note.id, work.id)
    off = notes.set_category(note.id, None)

    assert on.label == work.id and on.change_seq > note.change_seq
    assert off.label is None
    assert off.body == note.body and off.color == note.color


def test_an_unknown_category_is_refused(notes: NoteRepository) -> None:
    note = notes.create("Plan")
    with pytest.raises(KeyError):
        notes.set_category(note.id, "no-such-category")
    assert notes.get(note.id) == note


def test_marks_are_put_on_and_taken_off(notes: NoteRepository) -> None:
    note = notes.create("Plan")

    notes.set_mark(note.id, "urgent", True)
    marked = notes.set_mark(note.id, "todo", True)
    unmarked = notes.set_mark(note.id, "urgent", False)

    assert marked.marks == {"urgent", "todo"}
    assert unmarked.marks == {"todo"}
    assert unmarked.change_seq > marked.change_seq > note.change_seq
    assert notes.visible()[0].marks == {"todo"}


def test_marking_again_changes_nothing(notes: NoteRepository) -> None:
    note = notes.set_mark(notes.create("Plan").id, "todo", True)
    assert notes.set_mark(note.id, "todo", True) == note
    assert notes.set_mark(note.id, "urgent", False) == note


def test_an_unknown_mark_is_refused(notes: NoteRepository) -> None:
    note = notes.create("Plan")
    with pytest.raises(KeyError):
        notes.set_mark(note.id, "no-such-mark", True)
    assert notes.get(note.id) == note


def test_a_locked_note_still_takes_a_category_and_marks(
    labels: LabelRepository, notes: NoteRepository
) -> None:
    work = labels.create_category("Work", "blue")
    note = notes.set_locked(notes.create("Plan").id, True)
    notes.set_category(note.id, work.id)
    marked = notes.set_mark(note.id, "important", True)
    assert marked.label == work.id and marked.marks == {"important"} and marked.locked


def test_a_deleted_note_cannot_be_marked(notes: NoteRepository) -> None:
    note = notes.delete(notes.create("Plan").id)
    with pytest.raises(NoteDeletedError):
        notes.set_mark(note.id, "todo", True)


def test_marks_stay_in_the_trash_and_go_when_it_is_emptied(
    db: apsw.Connection, notes: NoteRepository
) -> None:
    note = notes.set_mark(notes.create("Plan").id, "todo", True)
    notes.delete(note.id)
    assert notes.deleted()[0].marks == {"todo"}

    notes.purge(note.id)

    assert db.execute("SELECT count(*) FROM note_marks").fetchall() == [(0,)]


# Managing categories


def test_a_category_is_renamed_and_keeps_its_notes(
    labels: LabelRepository, notes: NoteRepository
) -> None:
    work = labels.create_category("Work", "blue")
    note = notes.set_category(notes.create("Plan").id, work.id)

    renamed = labels.rename_category(work.id, " 업무 ")

    assert renamed.name == "업무" and renamed.id == work.id
    assert notes.get(note.id) == note  # the note itself is untouched


def test_a_rename_to_a_name_taken_is_refused(labels: LabelRepository) -> None:
    work = labels.create_category("Work", "blue")
    labels.create_category("Home", "green")
    with pytest.raises(CategoryNameError):
        labels.rename_category(work.id, "home")
    with pytest.raises(CategoryNameError):
        labels.rename_category(work.id, "  ")
    assert labels.rename_category(work.id, "WORK").name == "WORK"  # its own name, recased


def test_a_category_colour_changes(labels: LabelRepository) -> None:
    work = labels.create_category("Work", "blue")
    assert labels.set_category_color(work.id, "coral").color == "coral"
    with pytest.raises(ValueError):
        labels.set_category_color(work.id, "red")


def test_a_category_has_an_icon_of_its_own(labels: LabelRepository) -> None:
    work = labels.create_category("Work", "blue")
    assert work.icon == "tag"  # until one is chosen
    home = labels.create_category("Home", "mint", "house")
    assert home.icon == "house"
    assert labels.set_category_icon(work.id, "briefcase").icon == "briefcase"
    assert [c.icon for c in labels.categories()] == ["briefcase", "house"]
    with pytest.raises(ValueError):
        labels.set_category_icon(work.id, "no-such-icon")
    with pytest.raises(ValueError):
        labels.create_category("Other", "blue", "no-such-icon")


def test_an_icon_from_a_later_version_is_kept(labels: LabelRepository, db: apsw.Connection) -> None:
    """As synced from a newer copy of Stickle: shown as the tag, never overwritten."""
    work = labels.create_category("Work", "blue")
    db.execute("UPDATE categories SET icon = 'hologram' WHERE id = ?", (work.id,))
    assert labels.category(work.id) == replace(work, icon="hologram")


def test_categories_move_in_the_order_but_not_past_its_ends(labels: LabelRepository) -> None:
    a, b, c = (labels.create_category(name, "blue") for name in "ABC")

    assert [x.id for x in labels.move_category(c.id, -1)] == [a.id, c.id, b.id]
    assert [x.id for x in labels.move_category(a.id, -1)] == [a.id, c.id, b.id]
    assert [x.id for x in labels.move_category(a.id, 1)] == [c.id, a.id, b.id]
    assert [x.id for x in labels.move_category(b.id, 1)] == [c.id, a.id, b.id]


def test_removing_a_category_takes_it_off_its_notes(
    labels: LabelRepository, notes: NoteRepository
) -> None:
    work = labels.create_category("Work", "blue")
    kept = notes.create("회의록")
    ids = [notes.set_category(notes.create(text).id, work.id).id for text in ("a", "b")]
    notes.set_hidden(ids[1], True)

    removed = notes.remove_category(work.id, with_notes=False)

    assert sorted(removed) == sorted(ids)
    assert labels.categories() == []
    for note_id, body in zip(ids, ("a", "b"), strict=True):
        note = notes.get(note_id)
        assert note is not None and note.label is None and not note.deleted and note.body == body
    assert notes.get(kept.id) == kept


def test_removing_a_category_with_its_notes_puts_them_in_the_trash_together(
    labels: LabelRepository, notes: NoteRepository
) -> None:
    work = labels.create_category("Work", "blue")
    ids = [notes.set_category(notes.create(text).id, work.id).id for text in ("a", "b")]
    notes.set_locked(ids[0], True)
    other = notes.create("other")

    notes.remove_category(work.id, with_notes=True)

    trash = notes.deleted()
    assert sorted(n.id for n in trash) == sorted(ids)
    assert {n.label for n in trash} == {work.id}  # kept, to come back with them
    assert len({n.deleted_at for n in trash}) == 1
    assert sorted(n.id for n in notes.deleted_with(ids[0])) == sorted(ids)
    assert not notes.get(other.id).deleted  # pyright: ignore[reportOptionalMemberAccess]


def test_a_note_back_from_the_trash_brings_its_category_back(
    labels: LabelRepository, notes: NoteRepository
) -> None:
    work = labels.create_category("Work", "blue")
    note = notes.set_category(notes.create("a").id, work.id)
    notes.remove_category(work.id, with_notes=True)

    back = notes.restore(note.id)

    assert back.label == work.id and not back.deleted
    assert [c.id for c in labels.categories()] == [work.id]


def test_back_from_the_trash_into_a_category_of_the_same_name_made_since(
    labels: LabelRepository, notes: NoteRepository
) -> None:
    work = labels.create_category("Work", "blue")
    note = notes.set_category(notes.create("a").id, work.id)
    notes.remove_category(work.id, with_notes=True)
    again = labels.create_category("work", "green")

    back = notes.restore(note.id)

    assert back.label == again.id
    assert [c.id for c in labels.categories()] == [again.id]  # not two of one name


def test_a_category_cannot_be_removed_twice(labels: LabelRepository, notes: NoteRepository) -> None:
    work = labels.create_category("Work", "blue")
    notes.remove_category(work.id, with_notes=False)
    with pytest.raises(KeyError):
        notes.remove_category(work.id, with_notes=False)
    with pytest.raises(KeyError):
        labels.rename_category(work.id, "Again")


# Managing marks


def test_a_mark_is_made_with_a_name_and_an_icon(labels: LabelRepository) -> None:
    call = labels.create_mark(" 전화 ", "person")
    assert (call.name, call.icon) == ("전화", "person")
    assert labels.marks()[-1] == call
    with pytest.raises(ValueError):
        labels.create_mark("Other", "no-such-icon")
    with pytest.raises(MarkNameError):
        labels.create_mark("  ", "flag")


def test_a_mark_name_is_free_of_stored_and_shown_names(labels: LabelRepository) -> None:
    def shown(mark: Mark) -> str:
        return mark.name or {"urgent": "긴급"}.get(mark.id, mark.id)

    labels.create_mark("Call", "person")
    for taken in ("call", "긴급", "TODO"):
        with pytest.raises(MarkNameError):
            labels.create_mark(taken, "flag", shown)


def test_a_built_in_mark_is_renamed_and_back(labels: LabelRepository) -> None:
    assert labels.rename_mark("urgent", "급함").name == "급함"
    assert labels.rename_mark("urgent", "").name is None  # its own, translated name again
    own = labels.create_mark("Call", "person")
    with pytest.raises(MarkNameError):
        labels.rename_mark(own.id, "")
    assert labels.rename_mark(own.id, "CALL").name == "CALL"  # its own name, recased


def test_a_mark_icon_and_place_change(labels: LabelRepository) -> None:
    assert labels.set_mark_icon("important", "flag").icon == "flag"
    order = [m.id for m in labels.move_mark("waiting", -1)]
    assert order == ["todo", "urgent", "waiting", "important"]


def test_removing_a_mark_takes_it_off_every_note_but_keeps_them(
    labels: LabelRepository, notes: NoteRepository
) -> None:
    kept = notes.set_mark(notes.create("a").id, "urgent", True)
    trashed = notes.set_mark(notes.create("b").id, "urgent", True)
    notes.delete(trashed.id)

    removed = notes.remove_mark("urgent")

    assert removed == [kept.id]
    assert "urgent" not in [m.id for m in labels.marks()]
    after = notes.get(kept.id)
    assert after is not None and after.marks == frozenset() and after.body == "a"
    assert notes.deleted()[0].marks == frozenset()
    with pytest.raises(KeyError):
        notes.remove_mark("urgent")
