"""The Stickle window: the tray's actions in a window, and when it opens."""

import secrets
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QHideEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QDialog,
    QLabel,
    QPushButton,
    QSystemTrayIcon,
    QWidget,
)
from pytestqt.qtbot import QtBot

from stickle.app import file_dialogs, in_use
from stickle.app.application import connect_stickle_window, open_at_start
from stickle.app.i18n import Translations
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.settings_window import SettingsWindow
from stickle.app.stickle_window import ElidedButton, StickleWindow
from stickle.app.tray import Tray
from stickle.app.window_flags import set_stays_on_top, stays_on_top
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.platform.autostart import Autostart, Places

KEY = secrets.token_bytes(32)


class App:
    def __init__(self, connection: apsw.Connection, tray: bool, folder: Path) -> None:
        self.notes = NoteRepository(connection)
        self.manager = NoteManager(self.notes)
        # No translation files here: a language chosen in a test installs nothing
        # that outlives it (the built English plurals turned "2 note(s)" into
        # "2 notes" in the tests after). The translations fixture switches languages.
        self.translations = Translations(folder / "no translations")
        self.quits: list[None] = []
        self.window = StickleWindow(self.manager, self.translations, self.quit)
        self.tray = Tray(self.manager.new_note, lambda: None, self.translations, self.manager)
        connect_stickle_window(self.window, self.manager, self.tray if tray else None, self.quit)

    def quit(self) -> None:
        self.quits.append(None)

    def close(self) -> None:
        for note in self.manager.windows:
            note.release()
        # Gone before the database closes: a later language switch reaches every window.
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def app(qtbot: QtBot, connection: apsw.Connection, tmp_path: Path) -> Iterator[App]:
    app = App(connection, tray=True, folder=tmp_path)
    yield app
    app.close()


@pytest.fixture
def trayless(qtbot: QtBot, connection: apsw.Connection, tmp_path: Path) -> Iterator[App]:
    app = App(connection, tray=False, folder=tmp_path)
    yield app
    app.close()


def hidden_note(app: App, text: str) -> str:
    note = app.notes.create(text)
    app.notes.set_hidden(note.id, True)
    return note.id


def listed(window: StickleWindow) -> list[str]:
    rows = window.note_list.list
    return [rows.item(row).text() for row in range(rows.count())]


def open_note(app: App, text: str) -> NoteWindow:
    window = app.manager.new_note()
    window.editor.insertPlainText(text)
    app.manager.save(window)
    return window


def test_hidden_notes_are_listed_by_title_and_come_back(app: App) -> None:
    hidden_note(app, "# **장보기**\n우유")
    note_id = hidden_note(app, "- [ ] 전화하기")
    app.window.open()

    # The most recently changed first, each marked hidden.
    assert listed(app.window) == ["전화하기 · hidden", "장보기 · hidden"]
    rows = app.window.note_list.list
    rows.itemActivated.emit(rows.item(0))

    assert [w.note_id for w in app.manager.windows] == [note_id]
    assert listed(app.window) == ["전화하기", "장보기 · hidden"]


def test_with_no_notes_the_list_says_so(app: App) -> None:
    app.window.open()

    assert listed(app.window) == ["No notes here"]
    assert not app.window.show_all_button.isEnabled()


def test_the_buttons_do_what_the_tray_does(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    hidden_note(app, "하나")
    hidden_note(app, "둘")
    app.window.open()

    app.window.show_all_button.click()
    assert sorted(w.text for w in app.manager.windows) == ["둘", "하나"]

    app.manager.delete(app.manager.windows[0])
    assert app.window.restore_button.isEnabled()
    app.window.restore_button.click()
    assert len(app.manager.windows) == 2

    app.window.new_note_button.click()
    assert len(app.manager.windows) == 3

    raised: list[NoteWindow] = []

    def record_raise(self: NoteWindow) -> None:
        raised.append(self)

    monkeypatch.setattr(NoteWindow, "raise_", record_raise)
    app.window.raise_button.click()
    assert set(raised) == set(app.manager.windows)


def test_the_quit_button_quits(app: App) -> None:
    app.window.quit_button.click()

    assert app.quits == [None]


def test_the_language_can_be_chosen_in_its_settings(app: App) -> None:
    box = app.window.settings.language_box
    box.activated.emit(box.findData("en"))

    assert app.translations.language == "en"
    assert box.itemText(0) == "System language"
    app.translations.apply("en")  # not the system's: on a Korean system it stayed Korean


def test_everything_has_a_name_and_a_key(app: App) -> None:
    window = app.window
    assert window.note_list.list.accessibleName()
    assert window.note_list.filter_box.accessibleName()
    assert window.note_list.label.buddy() is window.note_list.list
    assert "&" in window.note_list.label.text()
    assert window.settings_button.text() == "&Settings…"


# The settings


def test_the_button_and_ctrl_comma_open_the_settings_once(qtbot: QtBot, app: App) -> None:
    app.window.open()
    qtbot.waitExposed(app.window)

    app.window.settings_button.click()
    assert app.window.settings.isVisible()
    assert app.window.settings.parentWidget() is app.window  # one of its windows
    app.window.settings.close()

    app.window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is app.window)
    QTest.keyClick(app.window.note_list.list, Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
    assert app.window.settings.isVisible()
    app.window.settings_button.click()  # again: the same window, brought forward

    assert len(app.window.findChildren(SettingsWindow)) == 1


def test_from_the_stickle_window_the_settings_stay_above_pinned_notes_with_it(
    qtbot: QtBot, app: App
) -> None:
    note = open_note(app, "핀 메모")
    app.window.open()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is app.window)

    app.window.settings_button.click()
    move_to(qtbot, app.window.settings)

    assert stays_on_top(app.window)
    assert not stays_on_top(app.window.settings)  # kept above it by the system
    move_to(qtbot, note)
    assert not stays_on_top(app.window)


def test_shown_from_it_the_settings_put_it_back_on_top(
    qtbot: QtBot, app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Telling Windows the settings are not on top took the Stickle window, which
    # they belong to, out of the top too: both went below the pinned notes.
    kept: list[QWidget] = []
    monkeypatch.setattr(in_use, "keep_stays_on_top", kept.append)
    app.window.open()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is app.window)
    kept.clear()

    app.window.settings_button.click()
    qtbot.waitUntil(lambda: app.window.settings in kept)

    assert kept[kept.index(app.window.settings) + 1 :] == [app.window]
    assert stays_on_top(app.window)


def test_from_the_tray_the_settings_stay_above_pinned_notes_until_left(
    qtbot: QtBot, app: App
) -> None:
    # The Stickle window closed: nothing else keeps them above the notes.
    note = open_note(app, "핀 메모")
    app.tray.settings_action.trigger()
    settings = app.window.settings

    assert settings.isVisible() and not app.window.isVisible()
    assert stays_on_top(settings)
    move_to(qtbot, settings)
    assert stays_on_top(settings)

    move_to(qtbot, note)
    assert not stays_on_top(settings)
    settings.close()


def test_the_tray_has_the_settings_where_its_switches_were(app: App) -> None:
    texts = [a.text() for a in app.tray.contextMenu().actions() if not a.isSeparator()]

    assert "&Settings…" in texts
    for gone in ("&Language", "&Start Stickle when I log in", "Line notes up like a &magnet"):
        assert gone not in texts


def mnemonics(texts: list[str]) -> list[str]:
    """The letter after each "&" (an "&&" is a plain ampersand)."""
    keys: list[str] = []
    for text in texts:
        cleaned = text.replace("&&", "")
        if "&" in cleaned:
            keys.append(cleaned[cleaned.index("&") + 1].lower())
    return keys


@pytest.mark.parametrize("language", ["en", "ko"])
def test_no_two_things_share_a_key(app: App, translations: Translations, language: str) -> None:
    # Alt and a letter two things share only moves between them: Alt+D reached
    # "On the desktop" instead of the Data menu.
    translations.apply(language)
    # In this window only: the windows it opens (About Stickle) have keys of their own.
    texts = [w.text() for w in app.window.findChildren(QAbstractButton) if w.window() is app.window]
    texts += [
        w.text()
        for w in app.window.findChildren(QLabel)
        if cast(QWidget | None, w.buddy()) is not None and w.window() is app.window
    ]
    keys = mnemonics(texts)

    assert len(set(keys)) == len(keys), sorted(keys)


@pytest.mark.parametrize("language", ["en", "ko"])
def test_every_button_is_wide_enough_for_its_text(
    app: App, qtbot: QtBot, translations: Translations, language: str
) -> None:
    # "Manage categories and marks…" was cut to "age categories and ma" in English.
    translations.apply(language)
    window = app.window
    window.show()
    for width in (window.width(), 200):  # as it opens, and made as narrow as it goes
        window.resize(width, window.height())
        qtbot.wait(50)
        cut = [
            b.text()
            for b in window.findChildren(QPushButton)
            if b.isVisible()
            and not isinstance(b, ElidedButton)  # ends with "…" instead
            and b.width() < b.sizeHint().width()
        ]
        assert cut == [], (width, window.width())


def test_a_long_title_just_deleted_does_not_widen_the_window(app: App, qtbot: QtBot) -> None:
    window = app.window
    window.show()
    width = window.width()
    note = open_note(app, "A title far longer than any window would show " * 4)
    app.manager.delete(note)
    window.refresh()
    qtbot.wait(50)

    button = window.restore_button
    assert window.width() == width
    assert button.text().endswith("…")
    assert button.fontMetrics().horizontalAdvance(button.text()) <= button.width()
    assert "A title far longer" in button.toolTip() == button.accessibleName()


# When it opens


def test_starting_with_only_hidden_notes_opens_it(app: App) -> None:
    hidden_note(app, "숨긴 메모")

    open_at_start(app.manager, app.window, tray_available=True)

    assert app.manager.windows == ()
    assert app.window.isVisible()
    assert not app.window.notice_shown


def test_starting_with_notes_to_show_does_not(app: App) -> None:
    app.notes.create("보이는 메모")

    open_at_start(app.manager, app.window, tray_available=True)

    assert len(app.manager.windows) == 1
    assert not app.window.isVisible()


def test_with_a_tray_hiding_the_last_note_leaves_it_closed(app: App) -> None:
    note = open_note(app, "마지막")

    app.manager.hide(note)

    assert not app.window.isVisible()
    assert app.quits == []


def test_a_double_click_on_the_tray_icon_makes_a_note_not_the_window(app: App) -> None:
    app.tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
    assert not app.window.isVisible()  # a single click does nothing

    app.tray.activated.emit(QSystemTrayIcon.ActivationReason.DoubleClick)

    assert len(app.manager.windows) == 1
    assert not app.window.isVisible()


def test_the_tray_menu_numbers_the_first_nine_hidden_notes(app: App) -> None:
    for number in range(1, 12):
        hidden_note(app, f"메모 {number}")
    app.tray.refresh_notes()

    texts = [a.text() for a in app.tray.hidden_menu.actions() if not a.isSeparator()]

    assert [t.split(" ", 1)[0] for t in texts[:9]] == [f"&{n}" for n in range(1, 10)]
    assert not texts[9].startswith("&")  # the tenth on, by mouse or arrow keys


def test_the_tray_menu_opens_it_too(app: App) -> None:
    # Where the tray only shows its menu (GNOME), this is the way to the window.
    actions = app.tray.contextMenu().actions()
    assert actions[0] is app.tray.open_window_action
    assert app.tray.open_window_action.text()

    app.tray.open_window_action.trigger()

    assert app.window.isVisible()


def move_to(qtbot: QtBot, window: QWidget | None) -> None:
    """The keyboard goes to window, or to another app, as the system tells it."""
    if window is None:
        with warnings.catch_warnings():  # no other way to it off screen
            warnings.simplefilter("ignore", DeprecationWarning)
            QApplication.setActiveWindow(None)  # pyright: ignore[reportArgumentType]
        app = QApplication.instance()
        assert isinstance(app, QApplication)
        app.focusWindowChanged.emit(None)
    else:
        window.activateWindow()
        qtbot.waitUntil(lambda: QApplication.activeWindow() is window)
    QTest.qWait(20)  # what it does once the keyboard has landed


def test_it_stays_above_other_windows_only_until_it_is_used(qtbot: QtBot, app: App) -> None:
    # Opened by a second start, the window manager may not let it take the
    # keyboard; above the others, it is still seen.
    app.window.open()
    assert stays_on_top(app.window)

    # Being made active is not enough: that can be reported before it is in front.
    QCoreApplication.sendEvent(app.window, QEvent(QEvent.Type.WindowActivate))
    assert stays_on_top(app.window)
    move_to(qtbot, open_note(app, "다른 창"))
    assert not stays_on_top(app.window)

    app.window.open()
    set_stays_on_top(app.window, True)
    app.window.hide()
    assert not stays_on_top(app.window)
    assert app.window.windowType() == Qt.WindowType.Window


def test_it_keeps_its_close_button_once_no_longer_on_top(qtbot: QtBot, app: App) -> None:
    # Windows makes the window again after it is closed, from these flags: one
    # without the close button hint came back with its X greyed out.
    app.window.open()
    on_top = app.window.windowFlags()
    move_to(qtbot, None)

    assert not stays_on_top(app.window)
    assert app.window.windowFlags() & Qt.WindowType.WindowCloseButtonHint
    # Only the on-top hint went.
    assert app.window.windowFlags() | Qt.WindowType.WindowStaysOnTopHint == on_top


def test_it_stays_above_when_said_to_be_hidden_while_on_screen(app: App) -> None:
    # Ubuntu sent a hide event as the window came up, the window still on screen.
    app.window.open()

    QCoreApplication.sendEvent(app.window, QHideEvent())

    assert app.window.isVisible()
    assert stays_on_top(app.window)


def test_the_windows_it_opens_stay_above_pinned_notes_with_it(qtbot: QtBot, app: App) -> None:
    # They are kept above it by the system only while it is above the notes:
    # a pinned note covered the question of whether to go back to a backup.
    note = open_note(app, "핀 메모")
    app.window.open()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is app.window)
    dialog = QDialog(app.window)
    dialog.show()
    move_to(qtbot, dialog)
    QCoreApplication.sendEvent(app.window, QEvent(QEvent.Type.WindowDeactivate))
    QTest.qWait(20)
    assert stays_on_top(app.window)

    question = QDialog(dialog)  # asked from that window, waiting for an answer
    question.setModal(True)
    question.show()
    move_to(qtbot, question)
    assert stays_on_top(app.window)

    question.close()
    move_to(qtbot, note)
    assert not stays_on_top(app.window)


def test_it_stays_above_pinned_notes_while_the_systems_file_dialog_is_open(
    qtbot: QtBot, app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    # That dialog is no Qt window: to Qt, the keyboard has left for another app.
    app.window.open()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is app.window)
    held: list[bool] = []

    def system_dialog(*_args: object) -> None:
        move_to(qtbot, None)
        held.append(stays_on_top(app.window))

    monkeypatch.setattr(file_dialogs, "from_the_system", system_dialog)
    file_dialogs.existing_folder(app.window, "폴더")

    assert held == [True]
    move_to(qtbot, None)  # closed: leaving now lets go
    assert not stays_on_top(app.window)


def test_about_stickle_is_one_of_its_windows(qtbot: QtBot, app: App) -> None:
    # Not one of them, it made the Stickle window let go of the top on Windows,
    # which put that window back above every other, About too: About was hidden.
    app.window.open()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is app.window)

    app.window.about_button.click()
    move_to(qtbot, app.window.about)

    assert app.window.about.parentWidget() is app.window
    assert stays_on_top(app.window)


def test_it_lets_go_when_the_user_leaves_from_a_window_it_opened(qtbot: QtBot, app: App) -> None:
    app.window.open()
    dialog = QDialog(app.window)
    dialog.show()
    move_to(qtbot, dialog)
    assert stays_on_top(app.window)

    move_to(qtbot, None)  # to another app: the Stickle window itself is not told

    assert not stays_on_top(app.window)


def test_without_a_tray_hiding_the_last_note_opens_it_with_a_notice(trayless: App) -> None:
    note = open_note(trayless, "마지막")

    trayless.manager.hide(note)

    assert trayless.window.isVisible()
    assert trayless.window.notice_shown
    assert listed(trayless.window) == ["마지막 · hidden"]
    assert trayless.quits == []
    # Says why it opened, in the window and its title, with the two ways on.
    assert trayless.window.notice_heading.text()
    assert trayless.window.windowTitle() == "All notes are hidden - Stickle Manager"
    assert trayless.window.notice_show_button.isVisible()
    assert trayless.window.notice_quit_button.isVisible()


@pytest.mark.parametrize("shown_before", ["open", "closed"])
def test_the_notice_is_shown_whole_in_a_window_that_was_open_before(
    qtbot: QtBot, trayless: App, translations: Translations, shown_before: str
) -> None:
    # Opened before at the size it needs without the notice, the window kept
    # that size and the notice's last line was hidden (Fedora).
    translations.apply("ko")
    window = trayless.window
    note = open_note(trayless, "마지막")
    window.open()
    qtbot.waitExposed(window)
    window.resize(window.minimumSizeHint())
    if shown_before == "closed":
        window.hide()

    trayless.manager.hide(note)
    qtbot.waitExposed(window)
    qtbot.wait(50)  # the layout is redone from the event loop

    text = window.notice_text
    assert text.isVisible()
    assert text.height() >= text.heightForWidth(text.width())
    assert window.notice_buttons.geometry().bottom() <= window.height()


def test_the_notice_buttons_bring_the_notes_back_or_quit(trayless: App) -> None:
    trayless.manager.hide(open_note(trayless, "마지막"))

    trayless.window.notice_show_button.click()

    assert len(trayless.manager.windows) == 1
    assert not trayless.window.notice_shown
    assert trayless.window.windowTitle() == "Stickle Manager"

    trayless.manager.hide(trayless.manager.windows[0])
    trayless.window.notice_quit_button.click()

    assert trayless.quits == [None]


def test_without_a_tray_closing_it_then_quits(trayless: App) -> None:
    trayless.manager.hide(open_note(trayless, "마지막"))

    trayless.window.close()

    assert trayless.quits == [None]


def test_without_a_tray_a_note_brought_back_keeps_stickle_running(trayless: App) -> None:
    trayless.manager.hide(open_note(trayless, "마지막"))

    rows = trayless.window.note_list.list
    rows.itemActivated.emit(rows.item(0))
    assert not trayless.window.notice_shown
    trayless.window.close()

    assert trayless.quits == []
    assert len(trayless.manager.windows) == 1


def press_escape(widget: QWidget) -> None:
    QTest.keyClick(widget, Qt.Key.Key_Escape)


def test_escape_closes_it(qtbot: QtBot, app: App) -> None:
    app.window.open()
    qtbot.waitExposed(app.window)

    press_escape(app.window.note_list.list)

    assert not app.window.isVisible()


def test_escape_clears_a_search_before_closing_it(qtbot: QtBot, app: App) -> None:
    app.window.open()
    qtbot.waitExposed(app.window)
    search = app.window.note_list.search_box
    search.setFocus()
    search.setText("장보기")

    press_escape(search)
    assert search.text() == ""
    assert app.window.isVisible()

    press_escape(search)
    assert not app.window.isVisible()


def test_escape_with_ctrl_does_not_close_it(qtbot: QtBot, app: App) -> None:
    app.window.open()
    qtbot.waitExposed(app.window)

    rows = app.window.note_list.list
    QTest.keyClick(rows, Qt.Key.Key_Escape, Qt.KeyboardModifier.ControlModifier)

    assert app.window.isVisible()


def test_without_a_tray_escape_brings_back_the_notes_set_aside(qtbot: QtBot, trayless: App) -> None:
    note = open_note(trayless, "치워 둔 메모")
    note.show()
    trayless.manager.set_all_aside()
    trayless.window.open()
    qtbot.waitExposed(trayless.window)

    press_escape(trayless.window.note_list.list)

    assert not trayless.window.isVisible()
    assert not trayless.manager.set_aside
    assert note.isVisible()
    assert trayless.quits == []


# Starting at login


class FakeAutostart(Autostart):
    """The real rules, with the files in a test folder."""

    def __init__(self, folder: Path, fail: bool = False) -> None:
        super().__init__(
            Places(home=folder, config=folder / "config", appdata=folder), "linux", ["/x"]
        )
        self.fail = fail

    def enable(self) -> None:
        if self.fail:
            raise PermissionError("read-only")
        super().enable()


def settings_with(qtbot: QtBot, autostart: Autostart) -> SettingsWindow:
    window = SettingsWindow(NoteManager(), Translations(), autostart=autostart)
    qtbot.addWidget(window)
    window.present()
    return window


def test_starting_at_login_is_switched_in_the_settings(qtbot: QtBot, tmp_path: Path) -> None:
    autostart = FakeAutostart(tmp_path)
    window = settings_with(qtbot, autostart)
    box = window.autostart_box
    assert not box.isChecked()
    assert box.text() == "&Start Stickle when I log in"

    box.click()
    assert autostart.enabled and box.isChecked()

    box.click()
    assert not autostart.enabled and not box.isChecked()


def test_the_open_settings_show_a_change_made_elsewhere_when_used(
    qtbot: QtBot, tmp_path: Path
) -> None:
    # Turned off in the system's settings, or on from the app list question,
    # while the window was open: it showed the old state until opened again.
    autostart = FakeAutostart(tmp_path)
    autostart.enable()
    window = settings_with(qtbot, autostart)
    assert window.autostart_box.isChecked()

    autostart.path.unlink()  # as when turned off in the system's own settings
    QCoreApplication.sendEvent(window, QEvent(QEvent.Type.WindowActivate))

    assert not window.autostart_box.isChecked()


def test_a_file_that_cannot_be_written_leaves_it_off(qtbot: QtBot, tmp_path: Path) -> None:
    autostart = FakeAutostart(tmp_path, fail=True)
    window = settings_with(qtbot, autostart)

    window.autostart_box.click()

    assert not autostart.enabled
    assert not window.autostart_box.isChecked()


def test_without_the_setting_the_items_are_hidden(app: App) -> None:
    assert app.window.settings.autostart_box.isHidden()
    assert app.window.settings.login_note.isHidden()


def test_started_at_login_with_only_hidden_notes_stays_in_the_tray(app: App) -> None:
    hidden_note(app, "숨긴 메모")

    open_at_start(app.manager, app.window, tray_available=True, at_login=True)

    assert not app.window.isVisible()


def test_started_at_login_without_a_tray_still_shows_the_way_back(trayless: App) -> None:
    hidden_note(trayless, "숨긴 메모")

    open_at_start(trayless.manager, trayless.window, tray_available=False, at_login=True)

    assert trayless.window.isVisible()
    assert trayless.window.notice_shown
