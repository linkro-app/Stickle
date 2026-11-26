"""A note's edit, for sync, begins with the first thing typed in it (a key that
types or deletes, a paste or a drop, a character being composed), told before it
goes in: until then, what comes from another computer shows in the text being
looked at, the cursor where it was. What came just before that first key is put
in first, and the key typed on it: neither covers the other."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QMimeData, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.versions import VersionRepository

KEY = secrets.token_bytes(32)
LAPTOP = "1a2b3c4d-0000-4000-8000-000000000000"


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def notes(connection: apsw.Connection) -> NoteRepository:
    return NoteRepository(connection)


@pytest.fixture
def manager(qtbot: QtBot, notes: NoteRepository) -> Iterator[NoteManager]:
    manager = NoteManager(notes, idle_ms=20, max_ms=200)
    yield manager
    for window in manager.windows:
        window.release()


def opened(manager: NoteManager, note_id: str) -> NoteWindow:
    manager.open_stored()
    window = manager.window_for(note_id)
    assert window is not None
    return window


def edited(manager: NoteManager, note_id: str) -> bool:
    with manager.editing.taking() as being:
        return being(note_id)


def came(notes: NoteRepository, note_id: str, text: str) -> None:
    """What a round takes in from the laptop, as it does (stickle.sync.local.take)."""
    notes.take_in(note_id, text, LAPTOP, {}, {}, {})


def body(notes: NoteRepository, note_id: str) -> str:
    note = notes.get(note_id)
    assert note is not None
    return note.body


def compose(window: NoteWindow, preedit: str, commit: str = "") -> None:
    event = QInputMethodEvent(preedit, [])
    if commit:
        event.setCommitString(commit)
    QApplication.sendEvent(window.editor, event)


@pytest.fixture
def meeting(manager: NoteManager, notes: NoteRepository) -> tuple[str, NoteWindow]:
    note_id = notes.create("회의 9시").id
    return note_id, opened(manager, note_id)


# Shown to edit, nothing typed yet


def test_shown_to_edit_it_is_not_being_edited_until_something_is_typed(
    manager: NoteManager, meeting: tuple[str, NoteWindow]
) -> None:
    note_id, window = meeting

    window.edit(3)

    assert window.editing and not edited(manager, note_id)


def test_what_comes_before_anything_is_typed_shows_in_the_text_the_cursor_kept(
    manager: NoteManager, notes: NoteRepository, meeting: tuple[str, NoteWindow]
) -> None:
    note_id, window = meeting
    window.edit(3)
    came(notes, note_id, "회의 10시")

    manager.take_in([note_id])

    assert window.editing and window.text == "회의 10시"
    assert window.editor.textCursor().position() == 3
    assert not window.held and not manager.waiting_to_save()


def test_keys_that_only_move_or_select_begin_no_edit(
    manager: NoteManager, meeting: tuple[str, NoteWindow]
) -> None:
    note_id, window = meeting
    window.edit(0)

    for key in (Qt.Key.Key_Right, Qt.Key.Key_End, Qt.Key.Key_Shift):
        QTest.keyClick(window.editor, key)
    QTest.keyClick(window.editor, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(window.editor, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)

    assert not edited(manager, note_id)


# The first thing typed


@pytest.mark.parametrize("key", [Qt.Key.Key_X, Qt.Key.Key_Backspace, Qt.Key.Key_Return])
def test_a_key_that_types_or_deletes_begins_the_edit(
    manager: NoteManager, meeting: tuple[str, NoteWindow], key: Qt.Key
) -> None:
    note_id, window = meeting
    window.edit()

    QTest.keyClick(window.editor, key)

    assert edited(manager, note_id)


def test_once_typed_in_what_comes_waits_for_the_edit(
    manager: NoteManager, notes: NoteRepository, meeting: tuple[str, NoteWindow]
) -> None:
    note_id, window = meeting
    window.edit()
    QTest.keyClicks(window.editor, " 3F")
    came(notes, note_id, "회의 10시")

    manager.take_in([note_id])
    manager.show_held([note_id])

    assert window.text == "회의 9시 3F" and window.held


@pytest.mark.parametrize("how", ["key", "composed", "pasted", "dropped"])
def test_what_came_just_before_the_first_input_is_put_in_first_and_typed_on(
    qtbot: QtBot,
    manager: NoteManager,
    notes: NoteRepository,
    meeting: tuple[str, NoteWindow],
    how: str,
) -> None:
    # Taken in by a round, its end not shown yet, as the first key is typed.
    note_id, window = meeting
    window.edit(0)
    came(notes, note_id, "회의 10시")

    typed = "!" if how == "key" else "급"  # a key typed here only gives ASCII
    match how:
        case "key":
            QTest.keyClicks(window.editor, typed)
        case "composed":
            compose(window, "ㄱ")
            assert window.text == "회의 10시"  # in first: the character composed on it
            compose(window, "", commit="급")
        case "pasted":
            QApplication.clipboard().setText("급")
            window.editor.paste()
        case _:
            dropped = QMimeData()
            dropped.setText("급")
            window.editor.insertFromMimeData(dropped)

    assert window.text == f"{typed}회의 10시"
    qtbot.waitUntil(lambda: body(notes, note_id) == f"{typed}회의 10시", timeout=3000)


def test_typed_on_what_came_the_edit_keeps_what_came_as_its_earlier_text(
    qtbot: QtBot,
    manager: NoteManager,
    notes: NoteRepository,
    connection: apsw.Connection,
    meeting: tuple[str, NoteWindow],
) -> None:
    note_id, window = meeting
    window.edit(0)
    came(notes, note_id, "회의 10시")

    QTest.keyClicks(window.editor, "!")
    window.show_formatted()

    qtbot.waitUntil(lambda: not edited(manager, note_id), timeout=3000)
    assert body(notes, note_id) == "!회의 10시"
    earlier = [v.first_line for v in VersionRepository(connection).listed(note_id)]
    assert earlier[0] == "회의 10시"


# Each time it is shown to edit


def test_left_and_shown_to_edit_again_it_begins_anew_with_what_is_typed(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository, meeting: tuple[str, NoteWindow]
) -> None:
    note_id, window = meeting
    window.edit()
    QTest.keyClicks(window.editor, " 3F")
    window.show_formatted()  # Esc
    qtbot.waitUntil(lambda: not edited(manager, note_id), timeout=3000)

    window.edit(0)
    came(notes, note_id, "회의 10시")
    manager.take_in([note_id])

    assert not edited(manager, note_id)
    assert window.editing and window.text == "회의 10시"


def test_an_empty_note_with_the_keyboard_is_edited_once_typed_in(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("").id
    window = opened(manager, note_id)
    window.show()
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow, timeout=3000)
    window.editor.setFocus()
    qtbot.wait(20)
    assert not edited(manager, note_id)
    came(notes, note_id, "노트북에서 쓴 글")
    manager.take_in([note_id])
    assert window.editing and window.text == "노트북에서 쓴 글"

    QTest.keyClicks(window.editor, "!")

    assert edited(manager, note_id)
