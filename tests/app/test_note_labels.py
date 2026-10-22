"""A note's category and marks: chosen from its menu, shown in its title bar."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QHelpEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QToolTip
from pytestqt.qtbot import QtBot

from stickle.app import notes as notes_module
from stickle.app.category_dialog import CreateCategory, NewCategoryDialog
from stickle.app.i18n import Translations
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.palette import rgb
from stickle.core.colors import ICON_CONTRAST, PALETTE, contrast
from stickle.data.labels import LabelRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store

KEY = secrets.token_bytes(32)


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

    def note(self, text: str) -> NoteWindow:
        window = self.manager.new_note()
        window.editor.insertPlainText(text)
        self.manager.save(window)
        return window

    def close(self) -> None:
        for window in self.manager.windows:
            window.release()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def app(qtbot: QtBot, connection: apsw.Connection) -> Iterator[App]:
    app = App(connection)
    yield app
    app.close()


def open_menus(window: NoteWindow) -> None:
    """What opening the note menu does: the category and mark menus are filled."""
    window.menu.aboutToShow.emit()


def choose(menu: QMenu, text: str) -> None:
    for action in menu.actions():
        if action.text() == text:
            action.trigger()
            return
    raise AssertionError(f"{text!r} not in {[a.text() for a in menu.actions()]}")


def texts(menu: QMenu) -> list[str]:
    return [action.text() for action in menu.actions() if not action.isSeparator()]


def test_a_category_is_made_from_the_note_menu_and_shown(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = app.note("Plan")

    def answer(parent: NoteWindow, create: CreateCategory, color: str) -> NewCategoryDialog:
        dialog = NewCategoryDialog(parent, create, color)
        dialog.name.setText("회사")
        dialog.color.setCurrentIndex(dialog.color.findData("blue"))
        dialog.accept()
        return dialog

    monkeypatch.setattr(notes_module, "new_category_dialog", answer)
    open_menus(window)
    choose(window.category_menu, "New category…")

    stored = app.notes.get(window.note_id or "")
    assert stored is not None and stored.label == app.labels.categories()[0].id
    assert window.title_bar.category_tag.isVisibleTo(window)
    assert window.title_bar.category_tag.accessibleName() == "Category: 회사, stays on top"
    open_menus(app.note("Other"))
    assert texts(app.manager.windows[-1].category_menu) == ["None", "회사", "New category…"]


def test_cancelling_the_new_category_changes_nothing(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = app.note("Plan")

    def cancel(parent: NoteWindow, create: CreateCategory, color: str) -> NewCategoryDialog:
        dialog = NewCategoryDialog(parent, create, color)
        dialog.name.setText("회사")
        dialog.reject()
        return dialog

    monkeypatch.setattr(notes_module, "new_category_dialog", cancel)
    window.new_category_requested.emit()
    assert app.labels.categories() == []
    assert window.title_bar.category_tag.category is None


def test_a_category_is_chosen_and_taken_off(app: App) -> None:
    work = app.labels.create_category("Work", "blue")
    window = app.note("Plan")

    open_menus(window)
    choose(window.category_menu, "Work")
    assert window.category == work
    open_menus(window)
    assert [a.text() for a in window.category_menu.actions() if a.isChecked()] == ["Work"]
    choose(window.category_menu, "None")

    stored = app.notes.get(window.note_id or "")
    assert stored is not None and stored.label is None
    assert window.title_bar.category_tag.category is None


def test_marks_are_put_on_from_the_menu_in_their_order(app: App) -> None:
    window = app.note("Plan")
    open_menus(window)
    assert texts(window.marks_menu) == ["To do", "Urgent", "Important", "Waiting"]

    choose(window.marks_menu, "Urgent")
    open_menus(window)
    choose(window.marks_menu, "To do")

    stored = app.notes.get(window.note_id or "")
    assert stored is not None and stored.marks == {"urgent", "todo"}
    assert [mark.id for mark in window.marks] == ["todo", "urgent"]
    assert window.title_bar.marks_badge.accessibleName() == "Marks: To do, Urgent"

    open_menus(window)
    choose(window.marks_menu, "Urgent")  # checked: takes it off
    assert [mark.id for mark in window.marks] == ["todo"]


def test_a_note_opens_with_its_category_and_marks(
    qtbot: QtBot, connection: apsw.Connection
) -> None:
    first = App(connection)
    work = first.labels.create_category("Work", "green")
    window = first.note("Plan")
    first.manager.set_category(window, work.id)
    first.manager.set_mark(window, "important", True)
    first.close()

    again = App(connection)
    again.manager.open_stored()
    reopened = again.manager.windows[0]
    assert reopened.category == work
    assert [mark.id for mark in reopened.marks] == ["important"]
    again.close()


def test_a_new_note_takes_them_when_first_stored(app: App) -> None:
    work = app.labels.create_category("Work", "blue")
    window = app.manager.new_note()
    app.manager.set_category(window, work.id)
    app.manager.set_mark(window, "todo", True)
    assert window.note_id is None and window.category == work

    window.editor.insertPlainText("Plan")
    app.manager.save(window)

    stored = app.notes.get(window.note_id or "")
    assert stored is not None and stored.label == work.id and stored.marks == {"todo"}


def test_a_locked_note_takes_them(app: App) -> None:
    window = app.note("Plan")
    app.manager.set_locked(window, True)
    open_menus(window)
    choose(window.marks_menu, "Waiting")
    assert [mark.id for mark in window.marks] == ["waiting"]


def test_the_colour_and_the_category_do_not_touch(app: App) -> None:
    work = app.labels.create_category("Work", "blue")
    window = app.note("Plan")
    app.manager.set_category(window, work.id)
    app.manager.set_color(window, "pink")
    app.manager.set_category(window, None)

    stored = app.notes.get(window.note_id or "")
    assert stored is not None and stored.color == "pink" and stored.label is None
    assert stored.body == "Plan"


def test_the_category_shows_as_its_icon_however_long_its_name(app: App) -> None:
    window = app.note("Plan")
    tag = window.title_bar.category_tag
    plain = tag.sizeHint()
    app.manager.set_category(
        window, app.labels.create_category("아주 긴 이름의 카테고리 하나", "blue").id
    )
    assert tag.sizeHint() == plain  # an icon as tall as the letters, not the name
    assert tag.sizeHint().width() < tag.fontMetrics().horizontalAdvance("아주 긴")


def test_the_left_icon_says_what_the_note_is_about_and_whether_it_stays_on_top(
    app: App,
) -> None:
    window = app.note("Plan")
    tag = window.title_bar.category_tag
    assert tag.isVisibleTo(window)  # the app's own icon, for no category
    assert tag.accessibleName() == "No category, stays on top"
    window.set_always_on_top(False)
    assert tag.accessibleName() == "No category"
    assert not tag.on_top

    home = app.labels.create_category("Home", "mint", "house")
    app.manager.set_category(window, home.id)
    assert tag.accessibleName() == "Category: Home"
    window.set_always_on_top(True)
    assert tag.on_top


def test_the_left_icon_only_shows_the_state(app: App) -> None:
    """Changed from the menu and the pin; a press there moves the note instead."""
    window = app.note("Plan")
    tag = window.title_bar.category_tag
    assert tag.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert tag.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_resting_on_the_left_icon_tells_what_it_shows(app: App, qtbot: QtBot) -> None:
    window = app.note("Plan")
    window.show()
    qtbot.waitExposed(window)
    bar = window.title_bar
    point = bar.category_tag.geometry().center()
    # The icon lets the mouse through, so Qt asks the bar for the tooltip there.
    assert bar.childAt(point) is None

    def rest_at(point: QPoint) -> None:
        QToolTip.hideText()
        qtbot.waitUntil(lambda: QToolTip.text() == "")  # gone, not only fading
        QApplication.sendEvent(bar, QHelpEvent(QEvent.Type.ToolTip, point, bar.mapToGlobal(point)))

    rest_at(point)
    qtbot.waitUntil(QToolTip.isVisible)
    assert QToolTip.text() == "No category, stays on top"

    rest_at(QPoint(bar.width() // 2, bar.height() // 2))  # elsewhere on the bar
    assert QToolTip.text() == ""  # nothing to tell there


def test_a_category_icon_shows_on_every_note_colour(app: App) -> None:
    """Its colour deepened (or lightened) until it stands out as an icon should."""
    window = app.note("Plan")
    tag = window.title_bar.category_tag
    app.manager.set_category(window, app.labels.create_category("Home", "yellow").id)
    for color in PALETTE:
        app.manager.set_color(window, color)
        assert contrast(rgb(tag.icon_color()), rgb(tag.background)) >= ICON_CONTRAST, color


def test_marks_that_do_not_fit_are_counted(app: App) -> None:
    window = app.note("Plan")
    for mark in ("todo", "urgent", "important"):
        app.manager.set_mark(window, mark, True)
    badge = window.title_bar.marks_badge

    badge.resize(badge.sizeHint())
    assert badge.shown() == (3, 0)
    badge.resize(badge.minimumSizeHint())
    assert badge.shown() == (1, 2)


def test_the_new_category_dialog_refuses_a_name_taken(
    qtbot: QtBot, connection: apsw.Connection
) -> None:
    labels = LabelRepository(connection)
    labels.create_category("Work", "blue")
    dialog = NewCategoryDialog(None, labels.create_category, "green")
    qtbot.addWidget(dialog)

    assert not dialog.ok_button.isEnabled()  # no name yet
    dialog.name.setText("work")
    dialog.accept()

    assert dialog.created is None and dialog.problem.isVisibleTo(dialog)
    dialog.name.setText("Home")
    dialog.accept()
    assert dialog.created is not None and dialog.created.color == "green"


def test_a_new_category_gets_the_icon_chosen_from_the_grid(
    qtbot: QtBot, connection: apsw.Connection
) -> None:
    labels = LabelRepository(connection)
    dialog = NewCategoryDialog(None, labels.create_category, "green")
    qtbot.addWidget(dialog)
    assert dialog.icon.chosen() == "tag"  # until another is chosen

    dialog.name.setText("Home")
    dialog.icon.setFocus()
    dialog.icon.choose("house")
    QTest.keyClick(dialog.icon, Qt.Key.Key_Right)  # the arrows move in the grid
    chosen = dialog.icon.chosen()
    assert chosen not in ("tag", "house")
    dialog.accept()
    assert dialog.created is not None and dialog.created.icon == chosen


def test_every_icon_in_the_grid_is_named(qtbot: QtBot, connection: apsw.Connection) -> None:
    dialog = NewCategoryDialog(None, LabelRepository(connection).create_category, "green")
    qtbot.addWidget(dialog)
    grid = dialog.icon
    names = {grid.item(row).toolTip() for row in range(grid.count())}
    assert len(names) == grid.count()  # every one its own name
    assert grid.item(0).data(Qt.ItemDataRole.AccessibleTextRole) == "Tag"


def test_built_in_marks_follow_the_language(app: App, translations: Translations) -> None:
    window = app.note("Plan")
    app.manager.set_mark(window, "urgent", True)

    translations.apply("ko")

    assert window.title_bar.marks_badge.accessibleName() == "표시: 긴급"
    open_menus(window)
    assert texts(window.marks_menu) == ["할 일", "긴급", "중요", "대기"]
    assert texts(window.category_menu) == ["없음", "새 카테고리…"]
