"""The settings: what is set once and left, a page for each kind."""

import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QPushButton, QWidget
from pytestqt.qtbot import QtBot
from test_shortcuts import System
from test_stickle_window import mnemonics

from stickle.app.i18n import Translations
from stickle.app.notes import NoteManager
from stickle.app.settings_window import RecoveryKeys, SettingsWindow
from stickle.app.shortcuts import GlobalShortcuts
from stickle.app.stickle_window import StickleWindow
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import MAGNET, SHORTCUTS, Settings
from stickle.platform.autostart import Autostart, Places
from stickle.platform.linux.appimage import AppMenuEntry

KEY = secrets.token_bytes(32)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


class Made:
    """Settings windows made in a test, gone before its database closes: a later
    language switch reaches every window."""

    def __init__(self) -> None:
        self.windows: list[QWidget] = []

    def __call__(self, window: SettingsWindow) -> SettingsWindow:
        self.windows.append(window)
        return window


@pytest.fixture
def made(qtbot: QtBot) -> Iterator[Made]:
    made = Made()
    yield made
    for window in made.windows:
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def everything(connection: apsw.Connection, tmp_path: Path, made: Made) -> SettingsWindow:
    """With all it can show."""
    places = Places(home=tmp_path, config=tmp_path / "config", appdata=tmp_path)
    recovery = RecoveryKeys(exists=lambda: True, make=lambda: "key", kept=lambda: None)
    return made(
        SettingsWindow(
            NoteManager(NoteRepository(connection), settings=Settings(connection)),
            Translations(),
            autostart=Autostart(places, "linux", ["/x"]),
            app_list=AppMenuEntry(tmp_path / "Stickle.AppImage", b"png", tmp_path / "share"),
            recovery=recovery,
            login_may_be_blocked=True,
            shortcuts=GlobalShortcuts(Settings(connection), System()),
        )
    )


def shown_tabs(window: SettingsWindow) -> list[str]:
    tabs = window.tabs
    return [tabs.tabText(i) for i in range(tabs.count()) if tabs.isTabVisible(i)]


def test_a_page_for_each_kind_of_setting(
    connection: apsw.Connection, tmp_path: Path, made: Made
) -> None:
    window = everything(connection, tmp_path, made)

    assert window.windowTitle() == "Stickle Settings"
    assert shown_tabs(window) == ["General", "Notes", "Protection", "Shortcuts", "Connections"]
    assert window.tabs.currentWidget() is window.general


def test_a_page_with_nothing_on_it_is_not_shown(qtbot: QtBot) -> None:
    # Measuring, say: no recovery key to make, no shortcuts from anywhere.
    window = SettingsWindow(NoteManager(), Translations())
    qtbot.addWidget(window)

    assert shown_tabs(window) == ["General", "Notes", "Connections"]
    window.present(window.shortcuts_page)  # asked for a page not shown: the one before
    assert window.tabs.currentWidget() is window.general


def test_it_opens_once_on_the_page_asked_for_or_the_one_last_shown(
    qtbot: QtBot, connection: apsw.Connection, tmp_path: Path, made: Made
) -> None:
    window = everything(connection, tmp_path, made)
    window.present(window.shortcuts_page)
    qtbot.waitExposed(window)
    assert window.tabs.currentWidget() is window.shortcuts_page
    assert window.tabs.tabBar().hasFocus() or not window.isActiveWindow()

    window.close()
    window.present()
    assert window.tabs.currentWidget() is window.shortcuts_page


def test_escape_closes_it(
    qtbot: QtBot, connection: apsw.Connection, tmp_path: Path, made: Made
) -> None:
    window = everything(connection, tmp_path, made)
    window.present()
    qtbot.waitExposed(window)

    QTest.keyClick(window.language_box, Qt.Key.Key_Escape)

    assert not window.isVisible()


def test_ctrl_tab_moves_between_its_pages(
    qtbot: QtBot, connection: apsw.Connection, tmp_path: Path, made: Made
) -> None:
    window = everything(connection, tmp_path, made)
    window.present()
    qtbot.waitExposed(window)

    QTest.keyClick(window.autostart_box, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
    assert window.tabs.currentWidget() is window.notes_page
    QTest.keyClick(window.tabs.tabBar(), Qt.Key.Key_Backtab, Qt.KeyboardModifier.ControlModifier)
    assert window.tabs.currentWidget() is window.general


def test_enter_in_a_field_presses_no_button(
    qtbot: QtBot, connection: apsw.Connection, tmp_path: Path, made: Made
) -> None:
    # A dialog's buttons take Enter by default: it made a new recovery key.
    window = everything(connection, tmp_path, made)
    window.present(window.protection)
    qtbot.waitExposed(window)
    pressed: list[None] = []
    window.recovery_button.clicked.disconnect()
    window.recovery_button.clicked.connect(lambda: pressed.append(None))

    window.tabs.tabBar().setFocus()
    QTest.keyClick(window.tabs.tabBar(), Qt.Key.Key_Return)

    assert pressed == []
    assert not any(b.autoDefault() for b in window.findChildren(QPushButton))


@pytest.mark.parametrize("language", ["en", "ko"])
def test_no_two_things_on_a_page_share_a_key(
    connection: apsw.Connection,
    tmp_path: Path,
    made: Made,
    translations: Translations,
    language: str,
) -> None:
    translations.apply(language)
    window = everything(connection, tmp_path, made)
    for page in (window.general, window.notes_page, window.protection, window.shortcuts_page):
        texts = [b.text() for b in page.findChildren(QAbstractButton)]
        texts += [
            label.text()
            for label in page.findChildren(QLabel)
            if cast(QWidget | None, label.buddy()) is not None
        ]
        keys = mnemonics(texts)
        assert keys, page
        assert len(set(keys)) == len(keys), sorted(keys)


@pytest.mark.parametrize("language", ["en", "ko"])
def test_its_words_follow_the_language(
    qtbot: QtBot,
    connection: apsw.Connection,
    tmp_path: Path,
    made: Made,
    translations: Translations,
    language: str,
) -> None:
    window = everything(connection, tmp_path, made)
    translations.apply(language)

    expected = {
        "en": ["General", "Notes", "Protection", "Shortcuts", "Connections"],
        "ko": ["일반", "메모", "메모 보호", "단축키", "연결"],
    }
    assert shown_tabs(window) == expected[language]
    assert window.windowTitle() == {"en": "Stickle Settings", "ko": "Stickle 설정"}[language]
    window.present()
    qtbot.waitExposed(window)
    cut = [
        b.text()
        for b in window.findChildren(QPushButton)
        if b.isVisible() and b.width() < b.sizeHint().width()
    ]
    assert cut == []


def test_what_was_set_before_shows_as_it_was(
    connection: apsw.Connection, tmp_path: Path, made: Made
) -> None:
    # Kept where earlier versions kept them: nothing to carry over.
    settings = Settings(connection)
    settings.set(MAGNET, False)
    settings.set(SHORTCUTS, {"new-note": "Ctrl+Alt+M"})
    places = Places(home=tmp_path, config=tmp_path / "config", appdata=tmp_path)
    autostart = Autostart(places, "linux", ["/x"])
    autostart.enable()
    window = made(
        SettingsWindow(
            NoteManager(NoteRepository(connection), settings=settings),
            Translations(),
            autostart=autostart,
            shortcuts=GlobalShortcuts(settings, System()),
        )
    )

    assert not window.magnet_box.isChecked()
    assert window.autostart_box.isChecked()
    rows = window.shortcut_rows
    assert rows is not None
    assert rows.edits["new-note"].keySequence().toString() == "Ctrl+Alt+M"


def test_the_guides_link_opens_the_shortcuts_page(
    qtbot: QtBot, connection: apsw.Connection
) -> None:
    shortcuts = GlobalShortcuts(Settings(connection), System())
    window = StickleWindow(
        NoteManager(NoteRepository(connection)), Translations(), lambda: None, shortcuts=shortcuts
    )
    try:
        window.guide.open()
        assert window.guide.change.isVisible()
        assert "Settings" in window.guide.change.text()

        window.guide.change.linkActivated.emit("settings")

        assert not window.guide.isVisible()
        assert window.settings.isVisible()
        assert window.settings.tabs.currentWidget() is window.settings.shortcuts_page
        qtbot.waitUntil(lambda: QApplication.activeWindow() is window.settings)
    finally:
        window.settings.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
