"""Marks in the window for categories and marks."""

import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from stickle.app import category_dialog, label_manager
from stickle.app.i18n import Translations
from stickle.app.label_manager import LabelManager, MarkPage, NewMarkDialog
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.core.labels import Mark
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
        self.dialog = LabelManager(self.manager)
        self.page = self.dialog.marks

    def note(self, text: str, *marks: str) -> NoteWindow:
        window = self.manager.new_note()
        window.editor.insertPlainText(text)
        self.manager.save(window)
        for mark in marks:
            self.manager.set_mark(window, mark, True)
        return window

    def names(self) -> list[str]:
        return [self.page.list.item(row).text() for row in range(self.page.list.count())]

    def select(self, name: str) -> None:
        self.page.list.setCurrentRow(self.names().index(name))

    def rename(self, name: str, to: str) -> None:
        self.select(name)
        self.page.list.currentItem().setText(to)

    def close(self) -> None:
        for window in self.manager.windows:
            window.release()
        self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def app(qtbot: QtBot, connection: apsw.Connection) -> Iterator[App]:
    app = App(connection)
    yield app
    app.close()


def test_the_marks_tab_lists_the_built_in_marks(app: App) -> None:
    assert app.names() == ["To do", "Urgent", "Important", "Waiting"]
    assert app.dialog.tabs.tabText(1) == "Marks"


def test_a_new_mark_is_made_and_offered_in_the_note_menu(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    def answer(parent: QWidget, create: Callable[[str, str], Mark]) -> NewMarkDialog:
        dialog = NewMarkDialog(parent, create)
        dialog.name.setText("전화")
        dialog.icon.choose("user")
        dialog.accept()
        return dialog

    monkeypatch.setattr(label_manager, "new_mark_dialog", answer)
    app.dialog.tabs.setCurrentWidget(app.page)
    app.dialog.show()
    app.page.new_button.setFocus()
    app.page.new_mark()
    window = app.note("Plan")
    window.menu.aboutToShow.emit()

    assert app.names()[-1] == "전화"
    assert app.page.list.currentItem().text() == "전화"
    assert app.dialog.focusWidget() is app.page.list  # on with the keyboard
    assert [a.text() for a in window.marks_menu.actions()][-1] == "전화"
    assert app.labels.marks()[-1].icon == "user"


def test_a_new_mark_needs_a_free_name(qtbot: QtBot, app: App) -> None:
    dialog = NewMarkDialog(None, app.manager.create_mark)
    qtbot.addWidget(dialog)
    assert not dialog.ok_button.isEnabled()
    dialog.name.setText("urgent")  # the built-in mark's name as shown
    dialog.accept()
    assert dialog.created is None and dialog.problem.isVisibleTo(dialog)


def test_renaming_a_built_in_mark_keeps_it_out_of_translation_until_cleared(
    app: App, translations: Translations
) -> None:
    window = app.note("Plan", "urgent")
    app.rename("Urgent", "급함")
    assert window.title_bar.marks_badge.accessibleName() == "Marks: 급함"
    translations.apply("ko")
    assert window.title_bar.marks_badge.accessibleName() == "표시: 급함"

    app.rename("급함", "")
    assert window.title_bar.marks_badge.accessibleName() == "표시: 긴급"
    translations.apply("en")
    assert "Urgent" in app.names()


def test_a_name_taken_is_refused(app: App) -> None:
    app.rename("Urgent", "to do")
    assert "Urgent" in app.names()
    assert app.page.problem.isVisibleTo(app.page)


def test_icon_and_order_follow_on_the_notes(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    offered: list[tuple[str, bool]] = []

    def choose(_parent: QWidget, current: str, _color: QColor, for_mark: bool) -> str:
        offered.append((current, for_mark))
        return "flag"

    monkeypatch.setattr(category_dialog, "choose_icon", choose)
    window = app.note("Plan", "todo", "waiting")
    app.select("Important")
    app.page.icon_button.click()
    assert offered == [("star", True)]  # the built-in mark's own to start with
    assert app.labels.mark("important").icon == "flag"  # pyright: ignore[reportOptionalMemberAccess]
    app.select("Waiting")
    for _ in range(3):
        app.page.move_selected(-1)
    assert app.names()[0] == "Waiting"
    assert [mark.id for mark in window.marks] == ["waiting", "todo"]


class Confirm:
    def __init__(self, sure: bool) -> None:
        self.sure = sure
        self.asked: list[str] = []

    def __call__(self, _parent: QWidget, question: str) -> bool:
        self.asked.append(question)
        return self.sure


def test_deleting_a_mark_says_it_cannot_be_undone_and_keeps_the_notes(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = [app.note("a", "urgent"), app.note("b", "urgent", "todo")]
    confirm = Confirm(False)
    monkeypatch.setattr(label_manager, "confirm", confirm)
    app.select("Urgent")
    app.page.delete()
    assert "2 note" in confirm.asked[0] and "cannot be undone" in confirm.asked[0]
    assert "Urgent" in app.names()  # cancelled

    confirm.sure = True
    app.page.delete()

    assert "Urgent" not in app.names()
    assert [[m.id for m in w.marks] for w in windows] == [[], ["todo"]]
    assert sorted(n.body for n in app.notes.live()) == ["a", "b"]


def test_a_mark_no_note_has_is_asked_about_once(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    confirm = Confirm(True)
    monkeypatch.setattr(label_manager, "confirm", confirm)
    app.select("Waiting")
    app.page.delete()
    assert len(confirm.asked) == 1 and "No note has it" in confirm.asked[0]
    assert "Waiting" not in app.names()


def test_the_page_names_its_controls(app: App) -> None:
    page: MarkPage = app.page
    assert page.list.accessibleName() == "Marks"
    assert page.icon_button.text() == "&Icon…"
