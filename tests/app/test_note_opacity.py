"""How see-through a note is while another window is in use: opaque while in use."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from stickle.app.i18n import Translations
from stickle.app.note_window import OPACITIES, NoteWindow
from stickle.app.notes import NoteManager
from stickle.core.note import Note
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store

KEY = secrets.token_bytes(32)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def manager(qtbot: QtBot, connection: apsw.Connection) -> Iterator[NoteManager]:
    manager = NoteManager(NoteRepository(connection))
    yield manager
    for window in manager.windows:
        window.release()


def stored(window: NoteWindow, connection: apsw.Connection) -> Note:
    assert window.note_id is not None
    note = NoteRepository(connection).get(window.note_id)
    assert note is not None
    return note


def note(manager: NoteManager, text: str = "메모") -> NoteWindow:
    window = manager.new_note()
    window.editor.insertPlainText(text)
    manager.save(window)
    return window


def in_use(window: NoteWindow, monkeypatch: pytest.MonkeyPatch, active: bool) -> None:
    """As when the window system hands the keyboard to the note, or takes it away."""
    monkeypatch.setattr(window, "isActiveWindow", lambda: active)
    QApplication.sendEvent(window, QEvent(QEvent.Type.ActivationChange))


def test_a_new_note_is_opaque(manager: NoteManager) -> None:
    window = note(manager)

    assert window.opacity == 1.0
    assert window.opacity_actions[1.0].isChecked()
    assert [round(level * 100) for level in OPACITIES] == [100, 90, 80, 70, 60]


def test_a_chosen_opacity_is_stored_and_shows_only_while_another_window_is_in_use(
    manager: NoteManager, connection: apsw.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = note(manager)
    in_use(window, monkeypatch, active=True)

    window.opacity_actions[0.7].trigger()

    assert stored(window, connection).opacity == 0.7
    assert window.windowOpacity() == pytest.approx(1.0)  # in use: opaque
    in_use(window, monkeypatch, active=False)
    assert window.windowOpacity() == pytest.approx(0.7, abs=0.01)
    in_use(window, monkeypatch, active=True)
    assert window.windowOpacity() == pytest.approx(1.0)


def test_it_is_kept_when_the_note_comes_back(
    qtbot: QtBot, manager: NoteManager, connection: apsw.Connection
) -> None:
    window = note(manager)
    window.opacity_actions[0.8].trigger()
    manager.hide(window)
    manager.show_hidden(str(stored_id(connection)))

    again = manager.windows[0]
    assert again.opacity == 0.8 and again.opacity_actions[0.8].isChecked()

    manager.set_all_aside()
    manager.bring_back()
    assert again.opacity == 0.8

    reopened = NoteManager(NoteRepository(connection))
    reopened.open_stored()
    assert reopened.windows[0].opacity == 0.8
    for other in reopened.windows:
        other.release()


def stored_id(connection: apsw.Connection) -> str:
    return NoteRepository(connection).all()[0].id


def test_a_note_not_stored_yet_takes_its_opacity_when_first_stored(
    manager: NoteManager, connection: apsw.Connection
) -> None:
    window = manager.new_note()
    window.opacity_actions[0.6].trigger()

    window.editor.insertPlainText("이제 저장")
    manager.save(window)

    assert stored(window, connection).opacity == 0.6


def test_if_it_cannot_be_stored_the_menu_shows_what_is_kept(
    manager: NoteManager, connection: apsw.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = note(manager)

    def fail(*_: object) -> Note:
        raise apsw.FullError("database or disk is full")

    monkeypatch.setattr(NoteRepository, "set_opacity", fail)
    window.opacity_actions[0.7].trigger()

    assert window.opacity == 1.0
    assert window.opacity_actions[1.0].isChecked()
    assert stored(window, connection).opacity == 1.0


def test_only_the_offered_levels_are_stored(connection: apsw.Connection) -> None:
    notes = NoteRepository(connection)
    stored_note = notes.create("메모")
    with pytest.raises(ValueError):
        notes.set_opacity(stored_note.id, 0.0)  # invisible: never


def test_the_menu_is_translated(qtbot: QtBot, translations: Translations) -> None:
    window = NoteWindow(text="메모")
    qtbot.addWidget(window)

    translations.apply("ko")

    assert window.opacity_menu.title() == "쓰지 않을 때 불투명도"
    window.release()
