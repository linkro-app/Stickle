"""Earlier versions as a note is used: one for each edit, an edit lasting until
the note is left (its text, or for another window), not when its menu opens;
checkboxes ticked count as one edit too, and a new note's first text is its own."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFocusEvent, QTextCursor
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.versions import VersionRepository

KEY = secrets.token_bytes(32)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def manager(qtbot: QtBot, connection: apsw.Connection) -> Iterator[NoteManager]:
    manager = NoteManager(
        NoteRepository(connection), idle_ms=20, max_ms=200, versions=VersionRepository(connection)
    )
    yield manager
    for window in manager.windows:
        window.release()


def type_into(window: NoteWindow, text: str) -> None:
    window.editor.moveCursor(QTextCursor.MoveOperation.End)
    window.editor.insertPlainText(text)


def stored(connection: apsw.Connection, window: NoteWindow) -> str:
    if window.note_id is None:
        return ""
    note = NoteRepository(connection).get(window.note_id)
    return "" if note is None else note.body


def saved(qtbot: QtBot, connection: apsw.Connection, window: NoteWindow) -> None:
    """Once autosave has stored what the window holds."""
    qtbot.waitUntil(lambda: stored(connection, window) == window.text, timeout=3000)


def earlier(connection: apsw.Connection, window: NoteWindow) -> list[str]:
    assert window.note_id is not None
    kept = VersionRepository(connection)
    return [str(kept.text(window.note_id, v.id)) for v in kept.listed(window.note_id)]


def leave_text(window: NoteWindow) -> None:
    """The text loses the keyboard, as when Esc is pressed or a button is used."""
    out = QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.OtherFocusReason)
    QApplication.sendEvent(window.editor, out)


def open_menu(window: NoteWindow) -> None:
    """The text loses the keyboard to the note's own menu."""
    out = QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.PopupFocusReason)
    QApplication.sendEvent(window.editor, out)


def leave_note(qtbot: QtBot, window: NoteWindow) -> None:
    """Another window takes the keyboard, then the note has it again."""
    other = QWidget()
    qtbot.addWidget(other)
    other.show()
    other.activateWindow()
    qtbot.waitUntil(lambda: not window.isActiveWindow(), timeout=3000)
    other.close()
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow, timeout=3000)


def written(
    qtbot: QtBot, connection: apsw.Connection, manager: NoteManager, text: str
) -> NoteWindow:
    """A note written and left: its text stored, and its edit over."""
    window = manager.new_note()
    qtbot.waitExposed(window)
    type_into(window, text)
    saved(qtbot, connection, window)
    leave_text(window)
    return window


def test_one_edit_keeps_the_text_it_had_once_however_often_it_is_saved(
    qtbot: QtBot, connection: apsw.Connection, manager: NoteManager
) -> None:
    window = written(qtbot, connection, manager, "첫 글")
    assert earlier(connection, window) == []  # a new note's first text is its own

    for more in (" 더", " 또", " 그리고"):
        type_into(window, more)
        saved(qtbot, connection, window)
    leave_text(window)
    assert earlier(connection, window) == ["첫 글"]

    type_into(window, " 끝")
    saved(qtbot, connection, window)
    leave_text(window)
    assert earlier(connection, window) == ["첫 글 더 또 그리고", "첫 글"]


def test_its_own_menu_does_not_end_the_edit(
    qtbot: QtBot, connection: apsw.Connection, manager: NoteManager
) -> None:
    window = written(qtbot, connection, manager, "장보기")
    type_into(window, ": 우유")
    saved(qtbot, connection, window)
    open_menu(window)
    type_into(window, ", 빵")
    saved(qtbot, connection, window)
    leave_text(window)

    assert earlier(connection, window) == ["장보기"]


def test_another_window_ends_the_edit_and_what_waited_is_saved_first(
    qtbot: QtBot, connection: apsw.Connection, manager: NoteManager
) -> None:
    window = written(qtbot, connection, manager, "회의")
    type_into(window, " 10시")  # not saved yet
    leave_note(qtbot, window)
    assert stored(connection, window) == "회의 10시"
    type_into(window, " 3층")
    saved(qtbot, connection, window)
    leave_text(window)

    assert earlier(connection, window) == ["회의 10시", "회의"]


def test_checkboxes_ticked_until_the_note_is_left_are_one_edit(
    qtbot: QtBot, connection: apsw.Connection, manager: NoteManager
) -> None:
    window = written(qtbot, connection, manager, "- [ ] 우유\n- [ ] 빵\n- [ ] 달걀")
    window.show_formatted()
    assert not window.editing
    window.toggle_checkbox(0)
    window.toggle_checkbox(1)
    saved(qtbot, connection, window)
    leave_note(qtbot, window)
    window.toggle_checkbox(2)
    saved(qtbot, connection, window)
    leave_note(qtbot, window)

    assert earlier(connection, window) == [
        "- [x] 우유\n- [x] 빵\n- [ ] 달걀",
        "- [ ] 우유\n- [ ] 빵\n- [ ] 달걀",
    ]


def test_leaving_without_a_change_keeps_nothing(
    qtbot: QtBot, connection: apsw.Connection, manager: NoteManager
) -> None:
    window = written(qtbot, connection, manager, "그대로")
    for _ in range(3):
        leave_note(qtbot, window)
        leave_text(window)
    assert earlier(connection, window) == []


def test_at_start_earlier_texts_past_their_days_are_put_away(
    connection: apsw.Connection, manager: NoteManager
) -> None:
    notes = NoteRepository(connection)
    note = notes.create("글")
    connection.execute(
        "INSERT INTO note_versions (note_id, body, content_hash, made_at, kept_at)"
        " VALUES (?, '아주 옛 글', 'x', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')",
        (note.id,),
    )

    manager.empty_old_trash()

    assert connection.execute("SELECT count(*) FROM note_versions").fetchall()[0][0] == 0
