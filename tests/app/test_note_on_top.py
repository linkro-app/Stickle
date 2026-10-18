"""Keeping a note above other windows, with the pin in its title bar."""

import ctypes
import secrets
import sys
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPixmap
from pytestqt.qtbot import QtBot

from stickle.app.i18n import Translations
from stickle.app.note_window import ICON_SCALES, ICON_SIZE, NoteWindow, make_pin_icon
from stickle.app.notes import NoteManager
from stickle.app.tray import Tray
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


def stored_note(manager: NoteManager) -> NoteWindow:
    window = manager.new_note()
    window.editor.insertPlainText("핀 시험")
    manager.save(window)
    return window


def on_top_flag(window: NoteWindow) -> bool:
    return bool(window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)


def test_new_notes_stay_on_top_and_show_a_solid_pin(manager: NoteManager) -> None:
    window = manager.new_note()

    assert on_top_flag(window)
    assert window.title_bar.pin_button.isChecked()
    assert window.on_top_action.isChecked()


def _ink_rows(pixmap: QPixmap) -> list[int]:
    image = pixmap.toImage()
    return [
        y
        for y in range(image.height())
        if any(image.pixelColor(x, y).alpha() > 40 for x in range(image.width()))
    ]


@pytest.mark.parametrize("scale", ICON_SCALES)
def test_the_pin_is_no_taller_than_the_buttons_beside_it_need(qtbot: QtBot, scale: float) -> None:
    """Drawn at full height, the pin stood out beside the menu and hide buttons."""
    del qtbot
    size = round(ICON_SIZE * scale)
    for pinned in (True, False):
        pixmap = make_pin_icon(QColor("black"), pinned).pixmap(QSize(ICON_SIZE, ICON_SIZE), scale)
        rows = _ink_rows(pixmap)
        assert rows, "the pin is drawn"
        assert rows[0] > 0 and rows[-1] < size - 1, "it stays inside its box"
        # 80 % and a pixel and a half of smoothing; the old pin took about 93 %.
        assert len(rows) <= 0.8 * size + 1.5


def test_the_pin_turns_it_off_and_on_and_it_is_stored(
    manager: NoteManager, connection: apsw.Connection
) -> None:
    window = stored_note(manager)
    geometry = window.geometry()
    notes = NoteRepository(connection)

    window.title_bar.pin_button.click()

    assert not on_top_flag(window)
    assert window.isVisible()
    assert window.geometry() == geometry
    assert not window.on_top_action.isChecked()
    note = notes.get(window.note_id or "")
    assert note is not None and not note.always_on_top

    window.title_bar.pin_button.click()

    assert on_top_flag(window)
    note = notes.get(window.note_id or "")
    assert note is not None and note.always_on_top


def test_notes_are_tool_windows_outside_x11(manager: NoteManager) -> None:
    # Under X11 they are ordinary windows kept off the taskbar instead.
    window = manager.new_note()

    assert window.windowType() == Qt.WindowType.Tool


def test_the_pin_changes_the_window_without_remaking_it(manager: NoteManager) -> None:
    # Remaking the native window hides and shows it again: the note blinks.
    window = stored_note(manager)
    native = window.windowHandle()
    hidden: list[bool] = []

    def visible_changed(visible: bool) -> None:
        hidden.append(not visible)

    native.visibleChanged.connect(visible_changed)

    window.title_bar.pin_button.click()

    assert window.windowHandle() is native
    assert not on_top_flag(window)
    assert not really_on_top(window)
    assert not any(hidden)

    window.title_bar.pin_button.click()

    assert window.windowHandle() is native
    assert really_on_top(window)
    assert not any(hidden)


def test_a_note_unpinned_stays_so_when_shown_again(qtbot: QtBot, manager: NoteManager) -> None:
    # Qt shows a window with the on-top state it last applied itself.
    window = stored_note(manager)
    window.title_bar.pin_button.click()

    window.hide()
    window.show()
    qtbot.wait(50)

    assert not really_on_top(window)


def really_on_top(window: NoteWindow) -> bool:
    """What the window system has, not only what the widget says."""
    if sys.platform == "win32" and QGuiApplication.platformName() == "windows":
        ws_ex_topmost = 0x0008
        styles = ctypes.windll.user32.GetWindowLongW(int(window.winId()), -20)  # GWL_EXSTYLE
        return bool(styles & ws_ex_topmost)
    return bool(window.windowHandle().flags() & Qt.WindowType.WindowStaysOnTopHint)


def test_the_menu_does_the_same_as_the_pin(manager: NoteManager) -> None:
    window = stored_note(manager)

    window.on_top_action.trigger()

    assert not on_top_flag(window)
    assert not window.title_bar.pin_button.isChecked()


def test_a_note_not_on_top_opens_that_way(qtbot: QtBot, connection: apsw.Connection) -> None:
    notes = NoteRepository(connection)
    note = notes.create("아래에 있는 메모")
    notes.set_always_on_top(note.id, False)

    manager = NoteManager(notes)
    manager.open_stored()
    (window,) = manager.windows

    assert not on_top_flag(window)
    assert not window.title_bar.pin_button.isChecked()
    window.release()


def test_a_new_note_unpinned_before_it_has_text_is_stored_unpinned(
    manager: NoteManager, connection: apsw.Connection
) -> None:
    window = manager.new_note()
    window.title_bar.pin_button.click()

    window.editor.insertPlainText("나중에")
    manager.save(window)

    note = NoteRepository(connection).get(window.note_id or "")
    assert note is not None and not note.always_on_top


def test_unpinning_leaves_the_text_alone(manager: NoteManager) -> None:
    window = stored_note(manager)
    changes: list[None] = []
    window.text_changed.connect(lambda: changes.append(None))

    window.title_bar.pin_button.click()
    window.title_bar.pin_button.click()

    assert window.text == "핀 시험"
    assert changes == []


def test_a_setting_that_cannot_be_stored_leaves_the_pin_as_it_was(
    manager: NoteManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = stored_note(manager)

    def fail(self: NoteRepository, note_id: str, on_top: bool) -> None:
        raise apsw.FullError("database or disk is full")

    monkeypatch.setattr(NoteRepository, "set_always_on_top", fail)
    window.title_bar.pin_button.click()

    assert on_top_flag(window)
    assert window.title_bar.pin_button.isChecked()
    assert window.on_top_action.isChecked()


def test_the_pin_has_a_name_and_says_what_it_does(manager: NoteManager) -> None:
    window = manager.new_note()

    assert window.title_bar.pin_button.accessibleName() == "Always on top"
    assert window.title_bar.pin_button.toolTip() == "Always on top"


def test_bring_all_notes_to_front_raises_every_note(
    manager: NoteManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = stored_note(manager), stored_note(manager)
    first.title_bar.pin_button.click()
    raised: list[NoteWindow] = []

    def record_raise(self: NoteWindow) -> None:
        raised.append(self)

    monkeypatch.setattr(NoteWindow, "raise_", record_raise)
    tray = Tray(lambda: None, lambda: None, Translations(), manager)

    tray.raise_action.trigger()

    assert set(raised) == {first, second}
    assert tray.raise_action.text() == "Bring all notes to front"
