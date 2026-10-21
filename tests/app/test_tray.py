import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMenu, QSystemTrayIcon
from pytestqt.qtbot import QtBot

from stickle.app.i18n import Translations
from stickle.app.tray import MenuWatch, Tray


def test_menu_actions_call_their_handlers(translations: Translations) -> None:
    calls: list[str] = []
    tray = Tray(lambda: calls.append("new"), lambda: calls.append("quit"), translations)

    tray.new_note_action.trigger()
    tray.quit_action.trigger()

    assert calls == ["new", "quit"]


def test_tray_has_an_icon_and_a_menu(qtbot: QtBot, translations: Translations) -> None:
    tray = Tray(lambda: None, lambda: None, translations)

    assert not tray.icon().isNull()
    assert tray.contextMenu() is not None


def test_language_menu_switches_and_marks_the_choice(translations: Translations) -> None:
    tray = Tray(lambda: None, lambda: None, translations)

    tray.language_actions["ko"].trigger()

    assert translations.language == "ko"
    assert tray.language_actions["ko"].isChecked()
    assert not tray.language_actions["en"].isChecked()


# Clicks on the icon: a double click makes a new note, a single click nothing.


def made(
    reasons: list[QSystemTrayIcon.ActivationReason],
    qtbot: QtBot,
    translations: Translations,
    pause: int = 0,
) -> int:
    notes: list[None] = []
    tray = Tray(lambda: notes.append(None), lambda: None, translations)
    for reason in reasons:
        tray.activated.emit(reason)
        if pause:
            QTest.qWait(pause)
    return len(notes)


Reason = QSystemTrayIcon.ActivationReason


def test_a_double_click_makes_a_note(qtbot: QtBot, translations: Translations) -> None:
    assert made([Reason.DoubleClick], qtbot, translations) == 1


def test_a_single_click_does_nothing(qtbot: QtBot, translations: Translations) -> None:
    assert made([Reason.Trigger], qtbot, translations) == 0


def test_the_clicks_windows_adds_to_a_double_click_make_no_more(
    qtbot: QtBot, translations: Translations
) -> None:
    assert made([Reason.Trigger, Reason.DoubleClick, Reason.Trigger], qtbot, translations) == 1
    assert made([Reason.Trigger, Reason.Trigger, Reason.DoubleClick], qtbot, translations) == 1


def test_two_quick_single_clicks_count_as_a_double_click(
    qtbot: QtBot, translations: Translations
) -> None:
    # StatusNotifierItem trays (KDE) send no double click.
    assert made([Reason.Trigger, Reason.Trigger], qtbot, translations) == 1


def test_two_slow_single_clicks_do_nothing(qtbot: QtBot, translations: Translations) -> None:
    pause = QGuiApplication.styleHints().mouseDoubleClickInterval() + 100

    assert made([Reason.Trigger, Reason.Trigger], qtbot, translations, pause) == 0


# Keys in the menu: one letter picks an item while the menu is open.


def keyed(menu: QMenu) -> list[QAction]:
    """The menu's items a letter should pick: not the lists filled with names."""
    return [a for a in menu.actions() if not a.isSeparator()]


@pytest.mark.parametrize("language", ["en", "ko"])
def test_every_item_has_a_key_of_its_own(translations: Translations, language: str) -> None:
    translations.apply(language)
    tray = Tray(lambda: None, lambda: None, translations)
    tray.new_version_action.setText(tray.tr("&Get Stickle %1…"))  # shown with a release

    for menu in (tray.contextMenu(), tray.data_menu):
        texts = [a.text() for a in keyed(menu)]
        keys = [
            t.replace("&&", "")[t.replace("&&", "").index("&") + 1].lower()
            for t in texts
            if "&" in t
        ]
        assert len(keys) == len(texts), [t for t in texts if "&" not in t]
        assert len(set(keys)) == len(keys), sorted(keys)


def test_a_letter_picks_its_item(qtbot: QtBot, translations: Translations) -> None:
    calls: list[str] = []
    tray = Tray(lambda: calls.append("new"), lambda: None, translations)
    menu = tray.contextMenu()
    menu.popup(menu.pos())
    qtbot.waitUntil(menu.isVisible)

    QTest.keyClick(menu, Qt.Key.Key_N)

    assert calls == ["new"]


# The menu, once another program's window comes to the front


class Front:
    """Stands in for the window system: which window is in front, and whose."""

    def __init__(self, window: int) -> None:
        self.window = window
        self.ours = {1}

    def __call__(self) -> int:
        return self.window

    def another_program(self, window: int) -> bool:
        return window not in self.ours


def open_watched(qtbot: QtBot, front: Front) -> QMenu:
    menu = QMenu()
    menu.addAction("Item")
    MenuWatch(menu, front, front.another_program)
    menu.popup(QPoint(100, 100))
    qtbot.waitUntil(menu.isVisible)
    qtbot.wait(MenuWatch.INTERVAL_MS + 100)  # what was in front as it opened, noted
    return menu


def test_another_program_coming_up_closes_the_menu(qtbot: QtBot) -> None:
    front = Front(50)  # the taskbar, the menu not let in front
    menu = open_watched(qtbot, front)

    front.window = 70  # a program that came up by itself
    qtbot.waitUntil(lambda: not menu.isVisible(), timeout=1000)


def test_the_menu_stays_while_stickle_or_the_same_window_is_in_front(qtbot: QtBot) -> None:
    front = Front(50)
    menu = open_watched(qtbot, front)

    front.window = 1  # one of Stickle's own (its menu, a submenu)
    qtbot.wait(MenuWatch.INTERVAL_MS * 2)
    front.window = 50  # back to what was in front as it opened
    qtbot.wait(MenuWatch.INTERVAL_MS * 2)
    front.window = 0  # none, while the front one changes
    qtbot.wait(MenuWatch.INTERVAL_MS * 2)
    assert menu.isVisible()
    menu.hide()
