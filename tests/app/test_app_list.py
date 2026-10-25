"""Offering, once, to put the AppImage in the application list, and switching it."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from stickle.app.app_list import icon_png, offer_app_list
from stickle.app.i18n import Translations
from stickle.app.notes import NoteManager
from stickle.app.settings_window import SettingsWindow
from stickle.data.schema import open_store
from stickle.data.settings import APP_MENU_ASKED, Settings
from stickle.platform.linux.appimage import AppMenuEntry

KEY = secrets.token_bytes(32)


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    connection: apsw.Connection = open_store(tmp_path / "notes.db", KEY)
    yield Settings(connection)
    connection.close()


@pytest.fixture
def entry(qtbot: QtBot, tmp_path: Path) -> AppMenuEntry:
    return AppMenuEntry(tmp_path / "Stickle.AppImage", icon_png(), tmp_path / "share")


def answer(monkeypatch: pytest.MonkeyPatch, reply: QMessageBox.StandardButton) -> list[str]:
    asked: list[str] = []

    def question(*arguments: object) -> QMessageBox.StandardButton:
        asked.append(str(arguments[2]))
        return reply

    monkeypatch.setattr(QMessageBox, "question", question)
    return asked


def test_yes_adds_it_and_it_is_not_asked_again(
    entry: AppMenuEntry, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = answer(monkeypatch, QMessageBox.StandardButton.Yes)

    offer_app_list(entry, settings)
    entry.remove()  # even if taken out later by hand
    offer_app_list(entry, settings)

    assert len(asked) == 1
    assert "list of applications" in asked[0]
    assert settings.get(APP_MENU_ASKED)


def test_no_is_remembered_too(
    entry: AppMenuEntry, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = answer(monkeypatch, QMessageBox.StandardButton.No)

    offer_app_list(entry, settings)
    offer_app_list(entry, settings)

    assert len(asked) == 1
    assert not entry.added


def test_nothing_is_asked_when_it_is_already_there(
    entry: AppMenuEntry, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = answer(monkeypatch, QMessageBox.StandardButton.Yes)
    entry.add()

    offer_app_list(entry, settings)

    assert asked == []


def test_the_icon_is_a_png(qtbot: QtBot) -> None:
    assert icon_png().startswith(b"\x89PNG\r\n\x1a\n")


def test_it_is_switched_in_the_settings_even_after_saying_no(
    qtbot: QtBot, entry: AppMenuEntry
) -> None:
    window = SettingsWindow(NoteManager(), Translations(), app_list=entry)
    qtbot.addWidget(window)
    window.present()
    box = window.app_list_box
    assert box.isVisibleTo(window) and not box.isChecked()
    assert box.text() == "Show Stickle in the &app list"

    box.click()
    assert entry.added and box.isChecked()

    box.click()
    assert not entry.added and not box.isChecked()


def test_the_settings_show_a_change_made_elsewhere(qtbot: QtBot, entry: AppMenuEntry) -> None:
    # Added from the first start's question, say, while the settings were open.
    window = SettingsWindow(NoteManager(), Translations(), app_list=entry)
    qtbot.addWidget(window)
    window.present()

    entry.add()
    QCoreApplication.sendEvent(window, QEvent(QEvent.Type.WindowActivate))

    assert window.app_list_box.isChecked()


def test_not_an_appimage_no_item(qtbot: QtBot) -> None:
    window = SettingsWindow(NoteManager(), Translations())
    qtbot.addWidget(window)

    assert window.app_list_box.isHidden()
