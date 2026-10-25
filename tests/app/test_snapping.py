"""Notes lined up where they are dropped, and moved together with Shift."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QGuiApplication, QMouseEvent
from pytestqt.qtbot import QtBot

from stickle.app import note_window
from stickle.app.i18n import Translations
from stickle.app.note_window import SETTLE_MS, NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.settings_window import SettingsWindow
from stickle.core.snap import GAP
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import MAGNET, Settings

KEY = secrets.token_bytes(32)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


class App:
    def __init__(self, qtbot: QtBot, connection: apsw.Connection) -> None:
        self.qtbot = qtbot
        self.notes = NoteRepository(connection)
        self.layouts = LayoutRepository(connection)
        self.manager = NoteManager(self.notes, idle_ms=10, max_ms=50, layouts=self.layouts)
        area = QGuiApplication.primaryScreen().availableGeometry()
        self.origin = area.topLeft() + QPoint(200, 200)

    def note(self, text: str, x: int, y: int) -> NoteWindow:
        window = self.manager.new_note()
        window.editor.insertPlainText(text)
        self.manager.save(window)
        window.place(QRect(self.origin + QPoint(x, y), window.size()))
        self.qtbot.waitExposed(window)
        return window

    def drop(self, window: NoteWindow, at: QPoint, shift: bool = False, alt: bool = False) -> None:
        """As a drag by the title bar would: started, moved, let go, left alone."""
        window.title_bar.drag_started.emit(shift)
        before = window.pos()
        window.move(self.origin + at)
        if shift:
            # Each step says how far from the start, as the window's own pos() lags under X11.
            distance = window.pos() - before
            window.title_bar.dragged_by.emit(QPoint(distance.x() // 2, distance.y() // 2))
            window.title_bar.dragged_by.emit(distance)
        window.alt_at_drop = alt
        window.geometry_settled.emit()

    def close(self) -> None:
        for window in self.manager.windows:
            window.release()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def app(qtbot: QtBot, connection: apsw.Connection) -> Iterator[App]:
    app = App(qtbot, connection)
    yield app
    app.close()


def test_dropped_beside_a_note_it_lines_up(qtbot: QtBot, app: App) -> None:
    a = app.note("a", 0, 0)
    b = app.note("b", 600, 0)
    app.drop(b, QPoint(a.width() + 10, 5))

    target = a.pos() + QPoint(a.width() + GAP, 0)
    qtbot.waitUntil(lambda: b.pos() == target, timeout=1000)
    qtbot.waitUntil(lambda: not b.moved_by_user, timeout=1000)  # where it went is remembered


def test_held_still_in_the_middle_of_a_drag_it_is_not_dropped_yet(
    qtbot: QtBot, app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = app.note("a", 0, 0)
    b = app.note("b", 600, 0)
    held = [True]
    monkeypatch.setattr(note_window, "_main_button_down", lambda: held[0])
    b.title_bar.drag_started.emit(False)
    b.move(app.origin + QPoint(a.width() + 10, 5))
    qtbot.wait(SETTLE_MS + 300)
    assert b.dragging and b.pos() == app.origin + QPoint(a.width() + 10, 5)

    held[0] = False  # let go
    target = a.pos() + QPoint(a.width() + GAP, 0)
    qtbot.waitUntil(lambda: b.pos() == target, timeout=SETTLE_MS + 1000)


def test_alt_told_of_only_just_after_the_button_is_up_still_counts(
    qtbot: QtBot, app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As under Wayland through XWayland: the desktop keeps the keys while it moves the window.
    a = app.note("a", 0, 0)
    b = app.note("b", 600, 0)
    held, alt = [True], [False]
    monkeypatch.setattr(note_window, "_main_button_down", lambda: held[0])
    monkeypatch.setattr(note_window, "_alt_held", lambda: alt[0])
    b.title_bar.drag_started.emit(False)
    b.move(app.origin + QPoint(a.width() + 10, 5))
    qtbot.wait(100)
    held[0] = False
    qtbot.wait(2 * note_window.DRAG_WATCH_MS)
    alt[0] = True
    qtbot.wait(SETTLE_MS + 300)
    assert b.pos() == app.origin + QPoint(a.width() + 10, 5)


def test_far_from_everything_it_stays(qtbot: QtBot, app: App) -> None:
    app.note("a", 0, 0)
    b = app.note("b", 600, 0)
    app.drop(b, QPoint(700, 300))
    qtbot.wait(200)
    assert b.pos() == app.origin + QPoint(700, 300)


def test_alt_at_the_drop_leaves_it_where_it_is(qtbot: QtBot, app: App) -> None:
    a = app.note("a", 0, 0)
    b = app.note("b", 600, 0)
    app.drop(b, QPoint(a.width() + 10, 5), alt=True)
    qtbot.wait(200)
    assert b.pos() == app.origin + QPoint(a.width() + 10, 5)


def test_shift_moves_the_notes_beside_it_along(qtbot: QtBot, app: App) -> None:
    a = app.note("a", 0, 0)
    b = app.note("b", a.width() + GAP, 0)
    c = app.note("c", 0, a.height() + GAP)
    far = app.note("far", 900, 0)
    far_at = far.pos()
    shift = QPoint(40, 60)
    b_at, c_at = b.pos(), c.pos()

    app.drop(a, shift, shift=True)
    qtbot.wait(200)

    assert b.pos() == b_at + shift and c.pos() == c_at + shift
    assert far.pos() == far_at


def test_a_locked_note_is_not_taken_along(qtbot: QtBot, app: App) -> None:
    a = app.note("a", 0, 0)
    b = app.note("b", a.width() + GAP, 0)
    app.manager.set_locked(b, True)
    b_at = b.pos()
    app.drop(a, QPoint(30, 400), shift=True)
    qtbot.wait(200)
    assert b.pos() == b_at


def test_without_shift_only_the_note_moves(qtbot: QtBot, app: App) -> None:
    a = app.note("a", 0, 0)
    b = app.note("b", a.width() + GAP, 0)
    b_at = b.pos()
    app.drop(a, QPoint(30, 400))
    qtbot.wait(200)
    assert b.pos() == b_at


# Resized by its corner, and the magnet switched off


def let_go_resized(window: NoteWindow, width: int, height: int, alt: bool = False) -> None:
    """As its corner grip would: pressed, the size changed, let go, left alone."""
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(2, 2),
        QPointF(window.size_grip.mapToGlobal(QPoint(2, 2))),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.eventFilter(window.size_grip, press)  # what the note hears of the grip
    window.resize(width, height)
    window.alt_at_drop = alt
    window.geometry_settled.emit()


def test_resized_below_a_note_it_takes_its_width(qtbot: QtBot, app: App) -> None:
    a = app.note("a", 0, 0)
    b = app.note("b", 0, a.height() + GAP)
    assert b.resizing is False
    let_go_resized(b, a.width() - 8, b.height())

    qtbot.waitUntil(lambda: b.width() == a.width(), timeout=1000)
    assert b.pos() == app.origin + QPoint(0, a.height() + GAP)
    assert b.resizing is False


def test_resized_beside_a_note_it_takes_its_height(qtbot: QtBot, app: App) -> None:
    a = app.note("a", 0, 0)
    b = app.note("b", a.width() + GAP, 0)
    let_go_resized(b, b.width(), a.height() + 9)

    qtbot.waitUntil(lambda: b.height() == a.height(), timeout=1000)


def test_with_alt_held_the_size_stays_as_let_go(qtbot: QtBot, app: App) -> None:
    a = app.note("a", 0, 0)
    b = app.note("b", 0, a.height() + GAP)
    let_go_resized(b, a.width() - 8, b.height(), alt=True)
    qtbot.wait(200)
    assert b.width() == a.width() - 8


def test_with_the_magnet_off_nothing_lines_up(
    qtbot: QtBot, connection: apsw.Connection, app: App
) -> None:
    settings = Settings(connection)
    manager = NoteManager(app.notes, idle_ms=10, max_ms=50, layouts=app.layouts, settings=settings)
    switched: list[None] = []
    manager.magnet_changed.connect(lambda: switched.append(None))
    assert manager.magnet is True
    manager.set_magnet(False)
    assert manager.magnet is False and settings.get(MAGNET) is False and switched == [None]

    app.manager = manager
    a = app.note("a", 0, 0)
    b = app.note("b", 600, 0)
    app.drop(b, QPoint(a.width() + 10, 5))
    qtbot.wait(200)
    assert b.pos() == app.origin + QPoint(a.width() + 10, 5)
    c = app.note("c", 0, a.height() + GAP)
    let_go_resized(c, a.width() - 8, c.height())
    qtbot.wait(200)
    assert c.width() == a.width() - 8


def test_the_settings_switch_the_magnet(qtbot: QtBot, connection: apsw.Connection) -> None:
    manager = NoteManager(NoteRepository(connection), settings=Settings(connection))
    window = SettingsWindow(manager, Translations())
    try:
        box = window.magnet_box
        assert box.isChecked()
        assert "Alt" in window.magnet_note.text() == box.accessibleDescription()

        box.click()  # off
        assert manager.magnet is False and Settings(connection).get(MAGNET) is False

        manager.set_magnet(True)  # elsewhere
        assert box.isChecked()  # told at once
    finally:
        # Gone before the database closes: a later language switch reaches every window.
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
