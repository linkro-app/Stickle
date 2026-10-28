"""Notes out of the way of programs shown full screen: a slide show, a video, a game."""

import secrets
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import cast

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QRect
from PySide6.QtGui import QGuiApplication, QScreen
from pytestqt.qtbot import QtBot

from stickle.app import notes as notes_module
from stickle.app.full_screen import FullScreenWatch, covered_screens
from stickle.app.full_screen_dialog import FullScreenExceptions
from stickle.app.i18n import Translations
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.settings_window import SettingsWindow
from stickle.app.tray import Tray
from stickle.core.labels import Category
from stickle.data.labels import LabelRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import FULL_SCREEN_EXCEPTIONS, HIDE_FOR_FULL_SCREEN, Settings
from stickle.platform.full_screen import FullScreen, full_screen_source

KEY = secrets.token_bytes(32)


class Source:
    """What the system says is full screen, as a test sets it."""

    def __init__(self) -> None:
        self.shown: list[FullScreen] = []
        self.asked: list[list[int]] = []

    def find(self, known: Collection[int]) -> list[FullScreen]:
        self.asked.append(list(known))
        return list(self.shown)


def screen() -> QScreen:
    return QGuiApplication.primaryScreen()


def filling(app: str = "vlc.exe", window: int = 7) -> FullScreen:
    """A window over the whole (only) screen, in the system's pixels."""
    area, scale = screen().geometry(), screen().devicePixelRatio()
    return FullScreen(
        window, app, (area.x(), area.y(), int(area.width() * scale), int(area.height() * scale))
    )


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


class App:
    def __init__(self, connection: apsw.Connection) -> None:
        self.notes = NoteRepository(connection)
        self.labels = LabelRepository(connection)
        self.manager = NoteManager(self.notes, idle_ms=10, max_ms=50, labels=self.labels)
        self.work = self.labels.create_category("회사", "blue")

    def note(self, text: str, category: Category | None = None) -> NoteWindow:
        window = self.manager.new_note()
        window.editor.insertPlainText(text)
        self.manager.save(window)
        if category is not None:
            self.manager.set_category(window, category.id)
        return window

    def shown(self) -> list[str]:
        return sorted(w.text for w in self.manager.windows if w.isVisible())

    def close(self) -> None:
        for window in self.manager.windows:
            window.release()


@pytest.fixture
def app(qtbot: QtBot, connection: apsw.Connection) -> Iterator[App]:
    app = App(connection)
    app.note("회의", app.work)
    app.note("장보기")
    yield app
    app.close()


ALL = ["장보기", "회의"]


# What is full screen, and which screens it covers


def test_the_screen_a_window_fills_is_covered_until_it_ends(qtbot: QtBot) -> None:
    source = Source()
    watch = FullScreenWatch(source)
    told: list[set[QScreen]] = []
    watch.covered_changed.connect(lambda: told.append(set(watch.screens)))
    watch.start()
    assert watch.available and told == []

    source.shown = [filling()]
    watch.check()
    assert watch.screens == {screen()}
    assert watch.apps == ["vlc.exe"]
    watch.check()
    assert source.asked[-1] == [7]  # followed by its id, whatever is in front

    source.shown = []
    watch.check()
    assert told == [{screen()}, set()]
    assert watch.apps == ["vlc.exe"]  # remembered while Stickle runs


class Display:
    """A screen as Windows names it, at 150 %: Qt scales its size, not where it is."""

    def name(self) -> str:
        return "\\\\.\\DISPLAY2"

    def geometry(self) -> QRect:
        return QRect(1920, 0, 1280, 720)  # 1920 by 1080 pixels

    def devicePixelRatio(self) -> float:  # noqa: N802 - as Qt names it
        return 1.5


def test_a_screen_is_known_by_its_name_or_by_where_it_is() -> None:
    display = cast(QScreen, Display())
    elsewhere = (0, 0, 1920, 1080)
    assert covered_screens([FullScreen(1, "a.exe", elsewhere, "\\\\.\\DISPLAY2")], [display]) == {
        display
    }
    assert covered_screens([FullScreen(1, "a.exe", elsewhere)], [display]) == set()
    assert covered_screens([FullScreen(1, "a.exe", (1920, 0, 1920, 1080))], [display]) == {display}
    assert covered_screens([filling()], [screen()]) == {screen()}


def test_an_exception_covers_nothing_and_is_kept(qtbot: QtBot, connection: apsw.Connection) -> None:
    settings = Settings(connection)
    source = Source()
    source.shown = [filling("VLC.exe")]
    watch = FullScreenWatch(source, settings)
    watch.except_app("vlc.exe")  # file names on Windows: whatever the case
    watch.start()

    assert watch.screens == set() and watch.apps == []
    assert settings.get(FULL_SCREEN_EXCEPTIONS) == ["vlc.exe"]
    watch.unexcept_app("vlc.exe")
    assert watch.screens == {screen()}
    assert FullScreenWatch(source, settings).exceptions == []


def test_turned_off_it_covers_nothing_and_stays_off(
    qtbot: QtBot, connection: apsw.Connection
) -> None:
    settings = Settings(connection)
    source = Source()
    source.shown = [filling()]
    watch = FullScreenWatch(source, settings)
    watch.start()
    assert watch.screens == {screen()}

    watch.switch(False)
    assert watch.screens == set()
    assert settings.get(HIDE_FOR_FULL_SCREEN) is False
    again = FullScreenWatch(source, settings)
    asked = len(source.asked)
    again.start()
    assert not again.on and len(source.asked) == asked  # not even asked


def test_where_nothing_can_tell_nothing_is_done(qtbot: QtBot) -> None:
    watch = FullScreenWatch(None)
    watch.start()
    assert not watch.available and watch.screens == set()


def test_under_wayland_nothing_is_looked_for() -> None:
    # XWayland would show only the other X11 programs' windows.
    assert full_screen_source("xcb", {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}) is None
    assert full_screen_source("xcb", {"WAYLAND_DISPLAY": "wayland-0"}) is None
    assert full_screen_source("wayland", {}) is None
    assert full_screen_source("offscreen", {}) is None


# The notes on a covered screen


def test_notes_go_out_of_sight_and_come_back_where_they_were(app: App) -> None:
    places = {w.text: w.pos() for w in app.manager.windows}

    app.manager.cover({screen()})
    assert app.shown() == [] and app.manager.covered == 2
    assert not any(note.hidden for note in app.notes.all())  # nothing stored changes

    app.manager.cover(set())
    assert app.shown() == ALL and app.manager.covered == 0
    assert {w.text: w.pos() for w in app.manager.windows} == places


def test_text_typed_is_saved_before_they_go(app: App) -> None:
    window = app.manager.windows[0]
    window.editor.insertPlainText(" 3시")  # not saved yet

    app.manager.cover({screen()})

    stored = app.notes.get(window.note_id or "")
    assert stored is not None and stored.body == window.text


def test_only_the_notes_of_a_covered_screen_go(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    left, right = cast(QScreen, object()), cast(QScreen, object())

    def screen_of(window: NoteWindow) -> QScreen:
        return left if window.text == "회의" else right

    monkeypatch.setattr(notes_module, "_screen_of", screen_of)

    app.manager.cover({left})
    assert app.shown() == ["장보기"]
    app.manager.cover({left, right})
    assert app.shown() == []
    app.manager.cover({right})
    assert app.shown() == ["회의"]


def test_a_note_brought_up_meanwhile_stays_in_sight(app: App) -> None:
    app.manager.cover({screen()})
    new = app.manager.new_note()
    app.manager.cover({screen()})  # still covered: nothing more goes

    assert new.isVisible()
    app.manager.cover(set())
    assert app.shown() == ["", *ALL]


def test_hidden_for_now_meanwhile_they_stay_aside_once_it_ends(app: App) -> None:
    app.manager.cover({screen()})
    app.manager.switch_set_aside()
    assert app.manager.set_aside and app.manager.covered == 0

    app.manager.cover(set())
    assert app.shown() == []
    app.manager.switch_set_aside()
    assert app.shown() == ALL


def test_set_aside_before_they_come_back_as_they_were(app: App) -> None:
    app.manager.switch_set_aside()
    app.manager.cover({screen()})
    app.manager.cover(set())
    assert app.shown() == []
    app.manager.switch_set_aside()
    assert app.shown() == ALL


def test_one_category_on_the_desktop_meanwhile(app: App) -> None:
    app.manager.cover({screen()})
    app.manager.set_view(app.work.id)
    assert app.shown() == []

    app.manager.cover(set())
    assert app.shown() == ["회의"]
    app.manager.set_view(None)
    assert app.shown() == ALL


def test_a_reminder_meanwhile_leaves_its_note_out_of_sight(app: App) -> None:
    # The system's notification tells it; the note comes once the screen is free.
    shopping = next(w for w in app.manager.windows if w.text == "장보기")
    app.manager.hide(shopping)
    meeting = next(w for w in app.manager.windows if w.text == "회의")
    hidden_id = shopping.note_id or ""
    app.manager.cover({screen()})

    assert app.manager.come_forward(meeting.note_id or "") is meeting
    rung = app.manager.come_forward(hidden_id)
    assert rung is not None and not rung.isVisible() and not meeting.isVisible()
    assert app.manager.covered == 2

    app.manager.cover(set())
    assert app.shown() == ALL
    stored = app.notes.get(hidden_id)
    assert stored is not None and not stored.hidden


def test_bringing_every_note_to_front_brings_them_over_it(app: App) -> None:
    app.manager.cover({screen()})
    app.manager.raise_all()
    assert app.shown() == ALL and app.manager.covered == 0


def test_a_note_deleted_meanwhile_is_no_longer_counted(app: App) -> None:
    app.manager.cover({screen()})
    window = app.manager.windows[0]
    app.manager.delete_note(window.note_id or "")
    assert app.manager.covered == 1


def test_the_tray_says_how_many_are_out_of_sight(app: App) -> None:
    tray = Tray(lambda: None, lambda: None, Translations(), app.manager)
    app.manager.cover({screen()})
    assert "hidden for a full-screen program" in tray.toolTip()
    app.manager.cover(set())
    assert tray.toolTip() == "Stickle"


# The settings


def test_the_settings_turn_it_off_and_list_the_exceptions(
    qtbot: QtBot, app: App, connection: apsw.Connection
) -> None:
    source = Source()
    source.shown = [filling("vlc.exe")]
    watch = FullScreenWatch(source, Settings(connection))
    watch.start()
    window = SettingsWindow(app.manager, Translations(), full_screen=watch)
    qtbot.addWidget(window)
    page = window.notes_page
    assert window.full_screen_box.isVisibleTo(page) and window.full_screen_box.isChecked()
    assert not window.no_full_screen.isVisibleTo(page)

    dialog = FullScreenExceptions(watch, window)
    qtbot.addWidget(dialog)
    assert dialog.found.count() == 1 and dialog.kept.count() == 0
    dialog.except_button.click()
    assert watch.exceptions == ["vlc.exe"] and dialog.found.count() == 0
    assert dialog.none_found.isVisibleTo(dialog) and dialog.kept.count() == 1
    dialog.remove_button.click()
    assert watch.exceptions == [] and dialog.found.count() == 1

    window.full_screen_box.click()
    assert not watch.on and watch.screens == set()


def test_under_wayland_the_settings_say_why_there_is_nothing(qtbot: QtBot, app: App) -> None:
    window = SettingsWindow(app.manager, Translations(), full_screen=FullScreenWatch(None))
    qtbot.addWidget(window)
    page = window.notes_page
    assert window.no_full_screen.isVisibleTo(page)
    assert not window.full_screen_box.isVisibleTo(page)
    assert not window.exceptions_button.isVisibleTo(page)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
