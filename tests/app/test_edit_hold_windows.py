"""A note's window, as sync is told whether it is being edited (stickle.core.being_edited):
from the moment its text is shown to edit, or something of it waits to be saved,
until that is over. What came from elsewhere meanwhile shows when the edit
ends; its line says so. What came just before an edit begins shows first, and
a checkbox ticked on a text that is gone is let go of. An empty note left
without the keyboard is not being edited."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtGui import QTextCursor
from pytestqt.qtbot import QtBot

from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store

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


def came(notes: NoteRepository, note_id: str, text: str | None = None, **columns: object) -> None:
    """What a round takes in from the laptop, as it does (stickle.sync.local.take)."""
    notes.take_in(note_id, text, LAPTOP, columns, {}, {})


def test_an_edit_is_told_from_its_start_to_its_end(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    assert not edited(manager, note_id)

    window.edit()
    assert edited(manager, note_id)
    window.editor.insertPlainText("x")
    window.show_formatted()  # Esc
    assert edited(manager, note_id)  # waiting to be saved
    qtbot.waitUntil(lambda: not edited(manager, note_id), timeout=3000)
    assert notes.get(note_id).body.endswith("x")  # pyright: ignore[reportOptionalMemberAccess]


def test_what_came_just_before_the_edit_began_shows_first(
    manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    came(notes, note_id, "회의 10시")  # taken in, not shown yet

    window.edit()

    assert window.editing and window.text == "회의 10시"
    assert not manager.waiting_to_save()


def test_a_checkbox_ticked_on_a_text_that_is_gone_is_let_go_of(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("- [ ] 우유\n- [ ] 빵").id
    window = opened(manager, note_id)
    came(notes, note_id, "- [ ] 우유\n- [ ] 빵\n- [ ] 달걀")

    window.toggle_checkbox(0)

    assert window.text == "- [ ] 우유\n- [ ] 빵\n- [ ] 달걀"
    assert not manager.waiting_to_save()
    assert notes.get(note_id).body == "- [ ] 우유\n- [ ] 빵\n- [ ] 달걀"  # pyright: ignore[reportOptionalMemberAccess]
    assert not edited(manager, note_id)


def test_what_comes_while_it_is_edited_shows_once_it_is_over(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    window.edit()
    came(notes, note_id, None, color="blue", hidden=1, collapsed=0)
    manager.take_in([note_id])

    assert window.color == "blue"  # at once: nothing of what is typed
    assert manager.window_for(note_id) is window  # hidden: once the edit is over
    manager.show_held([note_id])
    assert window.held

    window.show_formatted()
    qtbot.waitUntil(lambda: manager.window_for(note_id) is None, timeout=3000)


def test_the_held_line_goes_and_sync_is_told_when_the_edit_is_over(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    window.edit()
    manager.show_held([note_id])
    assert window.held

    with qtbot.waitSignal(manager.held_released, timeout=3000):
        window.show_formatted()
    assert not window.held


def test_the_held_line_is_said_and_tells_what_happens(
    manager: NoteManager, notes: NoteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    said: list[str] = []

    def announce(window: NoteWindow, text: str) -> None:
        said.append(text)

    monkeypatch.setattr(NoteWindow, "announce", announce)
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    window.edit()

    manager.show_held([note_id])
    manager.show_held([note_id])  # said once

    line = window._held_line  # pyright: ignore[reportPrivateUsage]
    assert line is not None
    assert line.text() == "Changes from another computer show up when you finish editing."
    assert said == [
        "Changes from another computer show up when you finish editing."
        " If you changed it here too, both are kept."
    ]


def test_a_held_line_is_only_for_a_note_still_being_edited(
    manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)

    manager.show_held([note_id])

    assert not window.held


def test_an_empty_note_without_the_keyboard_is_not_being_edited(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("").id
    window = opened(manager, note_id)
    assert window.editing  # an empty note is shown ready to type in
    window.editor.clearFocus()
    qtbot.waitUntil(lambda: not edited(manager, note_id), timeout=3000)

    came(notes, note_id, "노트북에서 쓴 글")
    manager.take_in([note_id])
    assert window.text == "노트북에서 쓴 글"


def test_typing_in_an_empty_note_is_an_edit(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("").id
    window = opened(manager, note_id)
    window.show()
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow, timeout=3000)
    window.editor.setFocus()
    qtbot.waitUntil(lambda: edited(manager, note_id), timeout=3000)

    came(notes, note_id, "노트북에서 쓴 글")
    manager.take_in([note_id])
    assert window.text == ""


def test_quitting_ends_every_edit(manager: NoteManager, notes: NoteRepository) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    window.edit()
    window.editor.moveCursor(QTextCursor.MoveOperation.End)
    window.editor.insertPlainText(" 3층")

    manager.prepare_to_quit()

    assert not edited(manager, note_id)
    assert notes.get(note_id).body == "회의 9시 3층"  # pyright: ignore[reportOptionalMemberAccess]


def test_folding_that_comes_while_the_note_is_dragged_waits_for_the_drop(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("회의 9시").id
    window = opened(manager, note_id)
    window.show_formatted()
    window.dragging = True
    came(notes, note_id, None, collapsed=1, color="green")

    manager.take_in([note_id])
    assert window.color == "green" and not window.collapsed

    window.geometry_settled.emit()  # dropped
    qtbot.waitUntil(lambda: window.collapsed, timeout=3000)
