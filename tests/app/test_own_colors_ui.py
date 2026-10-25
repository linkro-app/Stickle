"""Colours of one's own in a note's menu and the settings, a colour at random for
new notes, and going through the colours by keyboard."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QDialog, QWidget
from pytestqt.qtbot import QtBot

from stickle.app import notes, settings_window
from stickle.app.color_dialog import ColorDialog
from stickle.app.i18n import Translations
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.settings_window import SettingsWindow
from stickle.core.colors import PALETTE
from stickle.data.colors import MAX_OWN_COLORS, ColorRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import LAST_USED_COLOR, Settings

KEY = secrets.token_bytes(32)
SKY = "#a3c9f0"


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def manager(qtbot: QtBot, connection: apsw.Connection) -> Iterator[NoteManager]:
    manager = NoteManager(
        NoteRepository(connection),
        settings=Settings(connection),
        colors=ColorRepository(connection),
    )
    yield manager
    for window in manager.windows:
        window.release()


def stored_color(connection: apsw.Connection, window: NoteWindow) -> str:
    assert window.note_id is not None
    note = NoteRepository(connection).get(window.note_id)
    assert note is not None
    return note.color


def a_note(manager: NoteManager, text: str = "메모") -> NoteWindow:
    window = manager.new_note()
    window.editor.insertPlainText(text)
    manager.save(window)
    return window


def choose_in_dialog(monkeypatch: pytest.MonkeyPatch, typed: str | None) -> list[ColorDialog]:
    asked: list[ColorDialog] = []

    def answer(dialog: QDialog) -> bool:
        assert isinstance(dialog, ColorDialog)
        asked.append(dialog)
        if typed is None:
            return False
        dialog.code.setText(typed)
        dialog.accept()
        return dialog.result() == QDialog.DialogCode.Accepted.value

    monkeypatch.setattr(notes, "ask_color", answer)
    monkeypatch.setattr(settings_window, "show_color_dialog", answer)
    return asked


def test_a_colour_added_from_the_menu_paints_the_note_and_joins_the_menu(
    qtbot: QtBot,
    manager: NoteManager,
    connection: apsw.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = a_note(manager)
    choose_in_dialog(monkeypatch, "#A3C9F0")  # typed in capitals: kept in small letters

    window.add_color_action.trigger()

    assert window.color == SKY and stored_color(connection, window) == SKY
    assert manager.own_colors() == [SKY]
    window.color_menu.aboutToShow.emit()
    action = window.own_color_actions[SKY]
    assert action.text() == "My colour 1" and action.isChecked()
    other = a_note(manager, "다른 메모")
    other.color_menu.aboutToShow.emit()
    other.own_color_actions[SKY].trigger()
    assert other.color == SKY and stored_color(connection, other) == SKY


def test_when_twelve_are_kept_the_note_is_painted_and_it_is_said(
    qtbot: QtBot, manager: NoteManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(MAX_OWN_COLORS):
        assert manager.add_own_color(f"#1000{n:02x}")
    told: list[str] = []

    def information(_parent: QWidget, _title: str, text: str) -> None:
        told.append(text)

    monkeypatch.setattr(notes.QMessageBox, "information", information)
    window = a_note(manager)
    choose_in_dialog(monkeypatch, SKY)

    window.add_color_action.trigger()

    assert window.color == SKY
    assert SKY not in manager.own_colors()
    assert told and "12 at most" in told[0]


def test_cancelled_nothing_changes(
    qtbot: QtBot, manager: NoteManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = a_note(manager)
    choose_in_dialog(monkeypatch, None)

    window.add_color_action.trigger()

    assert window.color == "yellow" and manager.own_colors() == []


def test_the_keys_go_round_the_palette_then_ones_own(
    qtbot: QtBot, manager: NoteManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager.add_own_color(SKY)
    window = a_note(manager)
    said: list[str] = []
    monkeypatch.setattr(window, "announce", said.append)

    window.next_color_action.trigger()
    assert window.color == "apricot" and said == ["Apricot"]
    manager.set_color(window, list(PALETTE)[-1])
    window.next_color_action.trigger()
    assert window.color == SKY and said[-1] == "My colour 1"
    window.next_color_action.trigger()
    assert window.color == "yellow"  # round to the start
    window.previous_color_action.trigger()
    assert window.color == SKY  # and back


def test_from_a_colour_no_longer_ones_own_the_keys_start_at_either_end(
    qtbot: QtBot, manager: NoteManager
) -> None:
    window = a_note(manager)
    manager.set_color(window, "#123456")  # removed from one's own since

    window.next_color_action.trigger()
    assert window.color == "yellow"
    manager.set_color(window, "#123456")
    window.previous_color_action.trigger()
    assert window.color == list(PALETTE)[-1]


def test_the_keys_are_ctrl_shift_brackets(qtbot: QtBot, manager: NoteManager) -> None:
    window = a_note(manager)
    keys = {sequence.toString() for sequence in window.next_color_action.shortcuts()}
    assert "Ctrl+Shift+]" in keys
    keys = {sequence.toString() for sequence in window.previous_color_action.shortcuts()}
    assert "Ctrl+Shift+[" in keys


def test_at_random_never_the_colour_used_just_before(
    qtbot: QtBot, manager: NoteManager, connection: apsw.Connection
) -> None:
    manager.add_own_color(SKY)
    manager.set_random_color(True)
    settings = Settings(connection)
    seen: set[str] = set()

    for _ in range(60):
        before = settings.get(LAST_USED_COLOR)
        window = manager.new_note()
        assert window.color != before
        seen.add(window.color)
        window.release()

    assert SKY in seen and len(seen) > 6  # all of them, one's own too


def test_a_note_made_from_a_note_keeps_its_colour_even_at_random(
    qtbot: QtBot, manager: NoteManager
) -> None:
    manager.set_random_color(True)
    window = a_note(manager)
    manager.set_color(window, "mint")

    made = manager.new_note_from(window)

    assert made.color == "mint"


def test_without_random_new_notes_take_the_colour_last_chosen(
    qtbot: QtBot, manager: NoteManager
) -> None:
    window = a_note(manager)
    manager.set_color(window, SKY)

    assert manager.new_note().color == SKY


# The colour window


def test_the_colour_window_follows_whichever_way_a_colour_is_given(qtbot: QtBot) -> None:
    dialog = ColorDialog("yellow")
    qtbot.addWidget(dialog)
    assert dialog.code.text() == "#ffec8c"  # the palette colour, as a code

    dialog.code.setText("")
    dialog.code.insert("#808080")  # as typed: the rest follows
    assert dialog.preview.color == "#808080"
    assert dialog.lightness.value() == 50
    assert dialog.contrast.isVisibleTo(dialog)  # text cannot read on it as it is
    dialog.lightness.setValue(90)
    assert dialog.code.text() == "#e6e6e6" and not dialog.contrast.isVisibleTo(dialog)
    dialog.suggestions.setCurrentRow(0)
    first = dialog.suggestions.item(0).data(Qt.ItemDataRole.UserRole)
    assert dialog.code.text() == first == dialog.chosen()


def test_what_is_not_a_colour_is_not_taken(qtbot: QtBot) -> None:
    dialog = ColorDialog(SKY)
    qtbot.addWidget(dialog)
    dialog.code.setText("#12")

    dialog.accept()

    assert dialog.result() != QDialog.DialogCode.Accepted.value


# The settings


@pytest.fixture
def settings(qtbot: QtBot, manager: NoteManager) -> SettingsWindow:
    window = SettingsWindow(manager, Translations())
    qtbot.addWidget(window)
    return window


def press(widget: QWidget, key: Qt.Key, modifiers: Qt.KeyboardModifier) -> None:
    QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key, modifiers))


def test_the_settings_keep_random_and_ones_own_colours(
    qtbot: QtBot, manager: NoteManager, settings: SettingsWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not settings.random_box.isChecked()
    settings.random_box.click()
    assert manager.random_color

    choose_in_dialog(monkeypatch, SKY)
    settings.add_own_button.click()
    choose_in_dialog(monkeypatch, "#2b2b40")
    settings.add_own_button.click()
    texts = [settings.own_list.item(r).text() for r in range(settings.own_list.count())]
    assert texts == [f"My colour 1  {SKY}", "My colour 2  #2b2b40"]

    settings.own_list.setCurrentRow(1)
    press(settings.own_list, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    assert manager.own_colors() == ["#2b2b40", SKY]
    assert settings.own_list.currentRow() == 0  # it moved with the keyboard

    settings.remove_own_button.click()
    assert manager.own_colors() == [SKY]
    assert settings.own_list.count() == 1


def test_adding_stops_at_twelve(
    qtbot: QtBot, manager: NoteManager, settings: SettingsWindow
) -> None:
    for n in range(MAX_OWN_COLORS):
        manager.add_own_color(f"#2000{n:02x}")

    assert not settings.add_own_button.isEnabled()
