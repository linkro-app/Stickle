"""A note sync put in the trash, or that went (emptied from the trash elsewhere,
made one with another note), while its window was still on screen: what is
typed in it is never lost. Put in the trash, it comes back out with it; gone,
it is stored as a new note, its window staying. With nothing typed in it, the
window closes once sync says so. However one note fails, quitting, logging out
and sleeping save the others."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.core.note import Note
from stickle.data.labels import LabelRepository
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store

KEY = secrets.token_bytes(32)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def notes(connection: apsw.Connection) -> NoteRepository:
    return NoteRepository(connection)


@pytest.fixture
def manager(
    qtbot: QtBot, connection: apsw.Connection, notes: NoteRepository
) -> Iterator[NoteManager]:
    manager = NoteManager(
        notes,
        idle_ms=20,
        max_ms=200,
        layouts=LayoutRepository(connection),
        labels=LabelRepository(connection),
    )
    yield manager
    for window in manager.windows:
        window.release()


def opened(manager: NoteManager, note_id: str) -> NoteWindow:
    manager.open_stored()
    window = manager.window_for(note_id)
    assert window is not None
    return window


def type_into(window: NoteWindow, text: str) -> None:
    """Typed at its end, as keys are (ASCII: a key typed here gives no other)."""
    window.editor.moveCursor(QTextCursor.MoveOperation.End)
    QTest.keyClicks(window.editor, text)


def edited(manager: NoteManager, note_id: str) -> bool:
    with manager.editing.taking() as being:
        return being(note_id)


def edits_told(manager: NoteManager, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """What sync is told from now on of the notes' edits: (begin or end, note id)."""
    told: list[tuple[str, str]] = []
    begin, end = manager.editing.begin, manager.editing.end

    def begun(note_id: str) -> None:
        told.append(("begin", note_id))
        begin(note_id)

    def ended(note_id: str) -> None:
        told.append(("end", note_id))
        end(note_id)

    monkeypatch.setattr(manager.editing, "begin", begun)
    monkeypatch.setattr(manager.editing, "end", ended)
    return told


def bodies(notes: NoteRepository) -> list[str]:
    return sorted(note.body for note in notes.all())


# Put in the trash elsewhere


def test_typed_in_after_it_went_into_the_trash_it_comes_back_with_what_was_typed(
    manager: NoteManager, notes: NoteRepository, connection: apsw.Connection
) -> None:
    category = LabelRepository(connection).create_category("일", "green")
    note_id = notes.create("회의 9시", "blue").id
    notes.set_category(note_id, category.id)
    window = opened(manager, note_id)
    window.edit()
    type_into(window, " 3F")
    notes.delete(note_id)  # taken in from the laptop, not shown yet

    assert manager.flush(window)

    note = notes.get(note_id)
    assert note is not None and not note.deleted
    assert note.body == "회의 9시 3F"
    assert (note.color, note.label) == ("blue", category.id)
    assert not window.unsaved and manager.window_for(note_id) is window
    assert notes.deleted() == []


def test_back_out_of_the_trash_its_window_shows_the_category_it_went_back_into(
    manager: NoteManager, notes: NoteRepository, connection: apsw.Connection
) -> None:
    labels = LabelRepository(connection)
    old = labels.create_category("일", "green")
    note_id = notes.create("회의 9시").id
    notes.set_category(note_id, old.id)
    window = opened(manager, note_id)
    window.edit()
    type_into(window, " 3F")
    notes.remove_category(old.id, with_notes=True)  # on the laptop, its notes with it
    new = labels.create_category("일", "blue")  # one of the same name made since

    manager.flush(window)

    assert notes.get(note_id).label == new.id  # pyright: ignore[reportOptionalMemberAccess]
    assert window.category is not None and window.category.id == new.id


def test_back_out_of_the_trash_the_lists_are_told(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    window.edit()
    type_into(window, " 3F")
    notes.delete(note_id)

    with qtbot.waitSignal(manager.changed, timeout=1000):
        manager.flush(window)


def test_brought_back_its_window_stays_once_sync_says_it_went_into_the_trash(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    window.edit()
    type_into(window, " 3F")
    notes.delete(note_id)
    manager.take_in([note_id])  # the round's end shown: kept, being edited

    window.show_formatted()  # Esc: saved, the edit over

    qtbot.waitUntil(lambda: not edited(manager, note_id), timeout=3000)
    assert manager.window_for(note_id) is window
    note = notes.get(note_id)
    assert note is not None and not note.deleted and note.body == "회의 9시 3F"


# Gone elsewhere


def test_typed_in_after_it_was_gone_it_is_stored_as_a_new_note_its_window_staying(
    manager: NoteManager, notes: NoteRepository, connection: apsw.Connection
) -> None:
    category = LabelRepository(connection).create_category("일", "green")
    note_id = notes.create("회의 9시", "blue").id
    notes.set_category(note_id, category.id)
    window = opened(manager, note_id)
    window.edit()
    type_into(window, " 3F")
    notes.delete(note_id)
    notes.take_in_emptied(note_id)  # emptied from the trash on the laptop

    assert manager.flush(window)

    assert window.note_id is not None and window.note_id != note_id
    note = notes.get(window.note_id)
    assert note is not None and not note.deleted
    assert note.body == "회의 9시 3F"
    assert (note.color, note.label) == ("blue", category.id)
    assert manager.window_for(window.note_id) is window and not window.unsaved
    assert notes.get(note_id) is None


def test_stored_anew_it_is_the_new_note_that_is_being_edited(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    window.edit()
    type_into(window, " 3F")
    notes.drop(note_id)  # made one with the laptop's
    told = edits_told(manager, monkeypatch)

    manager.flush(window)

    new_id = window.note_id
    assert new_id is not None
    qtbot.waitUntil(lambda: ("begin", new_id) in told, timeout=3000)
    assert told[0] == ("end", note_id)


def test_gone_while_edited_its_window_stays_until_what_was_typed_is_kept(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    window.edit()
    type_into(window, " 3F")  # waiting to be saved
    notes.drop(note_id)

    manager.take_in([note_id])  # the round's end shown, before it is saved
    assert manager.window_for(note_id) is window

    window.show_formatted()
    qtbot.waitUntil(
        lambda: window.note_id != note_id and not manager.waiting_to_save(), timeout=3000
    )
    assert window in manager.windows
    assert bodies(notes) == ["회의 9시 3F"]


def test_gone_shown_to_edit_with_nothing_typed_it_closes_as_sync_says_so(
    manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    window.edit()
    notes.drop(note_id)

    manager.take_in([note_id])

    assert window not in manager.windows
    assert notes.all() == []


def test_emptied_to_nothing_and_gone_nothing_is_stored(
    manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    window.edit()
    window.editor.selectAll()
    window.editor.textCursor().removeSelectedText()
    notes.drop(note_id)

    assert manager.flush(window)

    assert window.note_id is None and notes.all() == []


# The first key typed after it went


@pytest.mark.parametrize("went", ["trash", "gone"])
def test_the_first_key_typed_after_sync_took_it_away_is_kept(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository, went: str
) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    notes.delete(note_id)
    if went == "gone":
        notes.take_in_emptied(note_id)
    window.edit(0)

    QTest.keyClicks(window.editor, "!")

    assert window in manager.windows and window.text == "!회의 9시"
    qtbot.waitUntil(lambda: not manager.waiting_to_save(), timeout=3000)
    assert window.note_id is not None and (window.note_id == note_id) == (went == "trash")
    note = notes.get(window.note_id)
    assert note is not None and not note.deleted and note.body == "!회의 9시"


def test_shown_to_edit_with_nothing_typed_its_window_closes_as_sync_says_so(
    manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    notes.delete(note_id)
    window.edit()

    manager.take_in([note_id])

    assert window not in manager.windows
    note = notes.get(note_id)
    assert note is not None and note.deleted


def test_a_checkbox_ticked_on_a_note_put_in_the_trash_brings_it_back_ticked(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("- [ ] 우유").id
    window = opened(manager, note_id)
    notes.delete(note_id)

    window.toggle_checkbox(0)  # an edit, of what is shown

    qtbot.waitUntil(lambda: not manager.waiting_to_save(), timeout=3000)
    note = notes.get(note_id)
    assert note is not None and not note.deleted and note.body == "- [x] 우유"
    assert manager.window_for(note_id) is window


# Closed or deleted with nothing typed


@pytest.mark.parametrize("went", ["trash", "gone"])
@pytest.mark.parametrize("how", ["close", "delete"])
def test_closed_or_deleted_after_sync_did_so_it_just_closes(
    manager: NoteManager, notes: NoteRepository, went: str, how: str
) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    notes.delete(note_id)
    if went == "gone":
        notes.take_in_emptied(note_id)

    if how == "close":
        manager.hide(window)
    else:
        manager.delete(window)

    assert window not in manager.windows
    note = notes.get(note_id)
    assert (note is None) if went == "gone" else (note is not None and note.deleted)


# Quitting, logging out, sleeping


@pytest.mark.parametrize("how", ["quit", "save all"])
def test_one_note_failing_to_save_the_others_are_saved_all_the_same(
    manager: NoteManager, notes: NoteRepository, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    first, second = notes.create("하나").id, notes.create("둘").id
    failing, other = opened(manager, first), opened(manager, second)
    type_into(failing, " more")
    type_into(other, " more")
    stored = NoteRepository.update_body

    def update_body(
        self: NoteRepository, note_id: str, body: str, keep_earlier: bool = False
    ) -> Note:
        if note_id == first:
            raise RuntimeError("something nobody thought of")
        return stored(self, note_id, body, keep_earlier)

    monkeypatch.setattr(NoteRepository, "update_body", update_body)

    if how == "quit":
        manager.prepare_to_quit()
    else:
        manager.save_all()

    assert notes.get(second).body == "둘 more"  # pyright: ignore[reportOptionalMemberAccess]
    assert failing.unsaved and not other.unsaved
