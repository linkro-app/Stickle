"""A note whose text was all taken away, closed or deleted: into the trash if
it has earlier versions (the text it had before is one of them, there when it
is brought back); one with nothing ever kept of it goes for good, as before."""

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
from stickle.data.versions import VersionRepository

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
def versions(connection: apsw.Connection) -> VersionRepository:
    return VersionRepository(connection)


@pytest.fixture
def manager(
    qtbot: QtBot, notes: NoteRepository, versions: VersionRepository
) -> Iterator[NoteManager]:
    manager = NoteManager(notes, idle_ms=20, max_ms=200, versions=versions)
    yield manager
    for window in manager.windows:
        window.release()


def opened(manager: NoteManager, note_id: str) -> NoteWindow:
    manager.open_stored()
    window = manager.window_for(note_id)
    assert window is not None
    return window


def emptied(window: NoteWindow) -> None:
    """Every character of it selected and taken away."""
    window.edit()
    window.editor.selectAll()
    window.editor.textCursor().removeSelectedText()


def earlier_titles(versions: VersionRepository, note_id: str) -> list[str | None]:
    return [version.first_line for version in versions.listed(note_id)]


@pytest.mark.parametrize("how", ["close", "delete"])
def test_emptied_and_closed_or_deleted_it_goes_into_the_trash_its_text_kept(
    manager: NoteManager, notes: NoteRepository, versions: VersionRepository, how: str
) -> None:
    note_id = notes.create("장보기 목록").id
    window = opened(manager, note_id)
    emptied(window)

    if how == "close":
        manager.hide(window)
    else:
        manager.delete(window)

    assert window not in manager.windows
    note = notes.get(note_id)
    assert note is not None and note.deleted and note.body == ""
    assert earlier_titles(versions, note_id) == ["장보기 목록"]


def test_brought_back_it_is_empty_its_text_an_earlier_version(
    manager: NoteManager, notes: NoteRepository, versions: VersionRepository
) -> None:
    note_id = notes.create("장보기 목록").id
    window = opened(manager, note_id)
    emptied(window)
    manager.hide(window)

    manager.restore_note(note_id)

    back = manager.window_for(note_id)
    assert back is not None and back.text == ""
    assert manager.has_earlier(note_id)
    assert earlier_titles(versions, note_id) == ["장보기 목록"]


def test_deleted_from_the_list_emptied_it_goes_into_the_trash(
    manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("장보기 목록").id
    notes.update_body(note_id, "", keep_earlier=True)  # emptied, then closed
    notes.set_hidden(note_id, True)

    manager.delete_note(note_id)

    note = notes.get(note_id)
    assert note is not None and note.deleted


def test_typed_and_taken_away_in_one_go_it_leaves_nothing(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    window = manager.new_note()
    window.editor.insertPlainText("잠깐")
    manager.flush(window)  # stored: its first text is this edit's
    note_id = window.note_id
    assert note_id is not None
    window.editor.moveCursor(QTextCursor.MoveOperation.End)
    window.editor.selectAll()
    window.editor.textCursor().removeSelectedText()

    manager.hide(window)

    assert notes.get(note_id) is None


def test_one_that_was_always_empty_closed_leaves_nothing(
    manager: NoteManager, notes: NoteRepository
) -> None:
    note_id = notes.create("").id
    window = opened(manager, note_id)

    manager.hide(window)

    assert notes.get(note_id) is None
