"""Folding a note to its title bar, and unfolding it."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QGuiApplication, QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from stickle.app.note_window import TITLE_BAR_HEIGHT, NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.palette import qcolor
from stickle.core.layout import MAIN
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store

KEY = secrets.token_bytes(32)


class App:
    def __init__(self, connection: apsw.Connection) -> None:
        self.notes = NoteRepository(connection)
        self.layouts = LayoutRepository(connection)
        self.manager = NoteManager(self.notes, layouts=self.layouts)

    def close(self) -> None:
        for window in self.manager.windows:
            window.release()


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def app(qtbot: QtBot, connection: apsw.Connection) -> Iterator[App]:
    app = App(connection)
    yield app
    app.close()


def stored_note(app: App, text: str = "# **장보기**\n- [ ] 우유") -> NoteWindow:
    window = app.manager.new_note()
    window.editor.insertPlainText(text)
    app.manager.save(window)
    window.setGeometry(QRect(window.pos(), QSize(280, 260)))
    app.manager.save_layout(window, force=True)
    return window


def double_click_title_bar(window: NoteWindow) -> None:
    bar = window.title_bar
    point = QPoint(bar.width() // 3, bar.height() // 2)
    QTest.mouseDClick(bar, Qt.MouseButton.LeftButton, pos=point)
    # Let go as a person does: QTest leaves the button held after a double-click,
    # and every later mouse move in the tests would drag.
    QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=point)


def test_double_clicking_the_title_bar_folds_and_unfolds(app: App) -> None:
    window = stored_note(app)
    before = window.geometry()

    double_click_title_bar(window)

    assert window.collapsed
    assert window.height() == TITLE_BAR_HEIGHT
    assert window.pos() == before.topLeft()  # the top edge stays
    assert window.title_bar.title.isVisible()
    assert window.title_bar.title.text() == "장보기"
    assert not window.stack.isVisible()

    double_click_title_bar(window)

    assert not window.collapsed
    assert window.geometry() == before
    assert not window.title_bar.title.isVisible()


def test_folding_leaves_the_text_alone(app: App) -> None:
    window = stored_note(app)
    changes: list[None] = []
    window.text_changed.connect(lambda: changes.append(None))

    window.collapse_action.trigger()
    window.collapse_action.trigger()

    assert window.text == "# **장보기**\n- [ ] 우유"
    assert changes == []


def test_a_folded_note_opens_folded_and_unfolds_to_its_height(
    app: App, connection: apsw.Connection
) -> None:
    window = stored_note(app)
    window.collapse_action.trigger()
    app.close()

    again = App(connection)
    again.manager.open_stored()
    (restored,) = again.manager.windows

    assert restored.collapsed
    assert restored.height() == TITLE_BAR_HEIGHT
    restored.collapse_action.trigger()
    assert restored.size() == QSize(280, 260)
    again.close()


def test_the_place_keeps_the_unfolded_height(qtbot: QtBot, app: App) -> None:
    window = stored_note(app)
    window.collapse_action.trigger()

    window.move(window.pos() + QPoint(40, 30))  # moved while folded
    app.manager.save_layout(window)

    place = app.layouts.places(window.note_id or "")[MAIN]
    assert place.height == 260


def test_folding_keeps_the_character_being_composed(app: App) -> None:
    window = stored_note(app, "메모 ")
    window.edit()
    QApplication.sendEvent(window.editor, QInputMethodEvent("한", []))  # still composing
    assert window.composing

    window.collapse_action.trigger()

    assert window.text == "메모 한"
    note = app.notes.get(window.note_id or "")
    assert note is not None and note.body == "메모 한"


def test_unfolding_near_the_bottom_stays_on_screen(app: App) -> None:
    window = stored_note(app)
    area = QGuiApplication.primaryScreen().availableGeometry()
    window.collapse_action.trigger()
    window.move(window.x(), area.bottom() - TITLE_BAR_HEIGHT)

    window.collapse_action.trigger()

    assert area.contains(window.geometry())


def test_a_folded_note_stays_folded_when_monitors_change(app: App) -> None:
    window = stored_note(app)
    window.collapse_action.trigger()

    app.manager.place_all()

    assert window.collapsed
    assert window.height() == TITLE_BAR_HEIGHT


def test_enter_unfolds_a_folded_note(qtbot: QtBot, app: App) -> None:
    window = stored_note(app)
    window.collapse_action.trigger()

    QTest.keyClick(window.title_bar, Qt.Key.Key_Return)

    assert not window.collapsed


def test_the_title_is_written_in_the_title_bar_colour(app: App) -> None:
    window = stored_note(app)

    window.collapse_action.trigger()

    colour = window.title_bar.title.palette().color(window.title_bar.title.foregroundRole())
    assert colour == qcolor(window.colors.title_text)


def test_a_note_without_text_is_called_empty(app: App) -> None:
    window = app.manager.new_note()

    window.collapse_action.trigger()

    assert window.title_bar.title.text() == "Empty note"


def test_a_long_title_is_shortened_to_fit(app: App) -> None:
    window = stored_note(app, "아주 긴 제목 " * 30)

    window.collapse_action.trigger()

    text = window.title_bar.title.text()
    assert text.endswith("…")
    metrics = window.title_bar.title.fontMetrics()
    assert metrics.horizontalAdvance(text) <= window.title_bar.title.width()


def test_the_menu_says_what_folding_will_do(app: App) -> None:
    window = stored_note(app)
    assert window.collapse_action.text() == "Collapse note"

    window.collapse_action.trigger()

    assert window.collapse_action.text() == "Expand note"


def test_a_new_note_folded_before_it_has_text_is_stored_folded(app: App) -> None:
    window = app.manager.new_note()
    window.collapse_action.trigger()

    window.editor.insertPlainText("나중에 쓴 글")
    app.manager.save(window)

    note = app.notes.get(window.note_id or "")
    assert note is not None and note.collapsed
