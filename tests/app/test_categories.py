"""Categories in the Stickle window: narrowing the list, and managing them."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from stickle.app import category_dialog, label_manager
from stickle.app.i18n import Translations
from stickle.app.label_manager import CategoryPage, LabelManager, Removal
from stickle.app.labels import dot_color
from stickle.app.note_list import ANY, NONE
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.stickle_window import StickleWindow
from stickle.core.labels import Category
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
        self.window = StickleWindow(self.manager, Translations(), lambda: None)
        self.work = self.labels.create_category("회사", "blue")
        self.home = self.labels.create_category("집", "green")

    def note(self, text: str, category: Category | None = None, *marks: str) -> NoteWindow:
        window = self.manager.new_note()
        window.editor.insertPlainText(text)
        self.manager.save(window)
        if category is not None:
            self.manager.set_category(window, category.id)
        for mark in marks:
            self.manager.set_mark(window, mark, True)
        return window

    def rows(self) -> list[str]:
        rows = self.window.note_list.list
        return [rows.item(row).text() for row in range(rows.count())]

    def titles(self) -> list[str]:
        return sorted(row.split(" · ")[0] for row in self.rows())

    def choose(self, box_name: str, key: str) -> None:
        box = getattr(self.window.note_list, box_name)
        box.setCurrentIndex(box.findData(key))

    def close(self) -> None:
        for window in self.manager.windows:
            window.release()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def app(qtbot: QtBot, connection: apsw.Connection) -> Iterator[App]:
    app = App(connection)
    app.note("회의 준비", app.work, "urgent")
    app.note("보고서", app.work)
    app.note("장보기", app.home)
    app.note("아무거나")
    app.note("또 하나")
    yield app
    app.close()


# Narrowing the list


def test_the_list_narrows_to_a_category_a_mark_or_none(app: App) -> None:
    app.choose("category_box", app.work.id)
    assert app.titles() == ["보고서", "회의 준비"]
    app.choose("mark_box", "urgent")
    assert app.titles() == ["회의 준비"]
    app.choose("mark_box", ANY)
    app.choose("category_box", NONE)
    assert app.titles() == ["또 하나", "아무거나"]


def test_ctrl_and_a_digit_choose_a_category_in_their_order(app: App) -> None:
    note_list = app.window.note_list
    note_list.choose_category(2)
    assert note_list.category_box.currentData() == app.home.id
    note_list.choose_category(5)  # there is no fifth: nothing changes
    assert note_list.category_box.currentData() == app.home.id
    note_list.choose_category(0)
    assert note_list.category_box.currentData() == ANY
    assert len(app.rows()) == 5


def test_search_narrows_within_the_category(app: App) -> None:
    app.choose("category_box", app.work.id)
    app.window.note_list.search_box.setText("보고")
    app.window.note_list.refresh()
    assert app.titles() == ["보고서"]


def test_each_row_names_its_category_and_marks(app: App) -> None:
    assert "회의 준비 · 회사 · Urgent" in app.rows()


# Managing


@pytest.fixture
def manager_window(app: App) -> Iterator[CategoryPage]:
    window = LabelManager(app.manager)
    yield window.categories
    window.deleteLater()


def select(window: CategoryPage, category: Category) -> None:
    for row in range(window.list.count()):
        if window.list.item(row).text() == category.name:
            window.list.setCurrentRow(row)
            return
    raise AssertionError(category.name)


def names(window: CategoryPage) -> list[str]:
    return [window.list.item(row).text() for row in range(window.list.count())]


def test_renaming_changes_the_tag_of_open_notes(app: App, manager_window: CategoryPage) -> None:
    select(manager_window, app.work)
    manager_window.list.currentItem().setText("업무")

    assert names(manager_window) == ["업무", "집"]
    tagged = [
        w for w in app.manager.windows if w.category is not None and w.category.id == app.work.id
    ]
    assert len(tagged) == 2
    assert all(
        w.title_bar.category_tag.accessibleName() == "Category: 업무, stays on top" for w in tagged
    )


def test_a_name_taken_is_refused_and_the_old_one_stays(
    app: App, manager_window: CategoryPage
) -> None:
    select(manager_window, app.work)
    manager_window.list.currentItem().setText("집")

    assert names(manager_window) == ["회사", "집"]
    assert manager_window.problem.isVisibleTo(manager_window)


def test_colour_and_order_change(app: App, manager_window: CategoryPage) -> None:
    select(manager_window, app.home)
    manager_window.move_selected(-1)
    assert names(manager_window) == ["집", "회사"]
    assert app.window.note_list.category_box.itemData(2) == app.home.id  # the list follows
    manager_window._recolor("coral")  # pyright: ignore[reportPrivateUsage]
    assert app.labels.categories()[0].color == "coral"


def test_another_icon_shows_at_once_on_its_notes(
    app: App, manager_window: CategoryPage, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[str, str, bool]] = []

    def choose(_parent: QWidget, current: str, color: QColor, for_mark: bool) -> str:
        asked.append((current, color.name(), for_mark))
        return "briefcase"

    monkeypatch.setattr(category_dialog, "choose_icon", choose)
    select(manager_window, app.work)
    manager_window.icon_button.click()

    assert asked == [("tag", dot_color(app.work.color).name(), False)]
    assert app.labels.categories()[0].icon == "briefcase"
    tagged = [w for w in app.manager.windows if w.category and w.category.id == app.work.id]
    assert tagged and all(w.title_bar.category_tag.category.icon == "briefcase" for w in tagged)  # pyright: ignore[reportOptionalMemberAccess]


def test_the_stickle_windows_lists_show_each_category_with_its_icon(app: App) -> None:
    boxes = (app.window.note_list.category_box, app.window.view_box)
    for box in boxes:
        work = box.findData(app.work.id)
        assert work > 0 and not box.itemIcon(work).isNull()
        assert box.itemIcon(0).isNull()  # every note: no icon of its own
    marks = app.window.note_list.mark_box
    assert not marks.itemIcon(marks.findData("urgent")).isNull()


def test_a_cancelled_icon_changes_nothing(
    app: App, manager_window: CategoryPage, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cancel(_parent: QWidget, _current: str, _color: QColor, _for_mark: bool) -> None:
        return None

    monkeypatch.setattr(category_dialog, "choose_icon", cancel)
    select(manager_window, app.work)
    manager_window.icon_button.click()
    assert app.labels.categories()[0].icon == "tag"


class Answers:
    def __init__(self, how: Removal, sure: bool) -> None:
        self.how: Removal = how
        self.sure = sure
        self.asked: list[str] = []

    def ask_how(self, _parent: QWidget, question: str, _only: str, _notes: str) -> Removal:
        self.asked.append(question)
        return self.how

    def confirm(self, _parent: QWidget, question: str) -> bool:
        self.asked.append(question)
        return self.sure


@pytest.fixture
def answer(monkeypatch: pytest.MonkeyPatch) -> Iterator[Answers]:
    answers = Answers(None, False)
    monkeypatch.setattr(label_manager, "ask_how", answers.ask_how)
    monkeypatch.setattr(label_manager, "confirm", answers.confirm)
    yield answers


def test_deleting_the_category_only_says_it_cannot_be_undone(
    app: App, manager_window: CategoryPage, answer: Answers
) -> None:
    answer.how = "category"
    select(manager_window, app.work)
    manager_window.delete()

    assert "This cannot be undone" in answer.asked[0] and "2 note" in answer.asked[0]
    assert len(answer.asked) == 1  # nothing more asked
    assert [c.id for c in app.labels.categories()] == [app.home.id]
    assert len(app.notes.live()) == 5 and not app.notes.deleted()
    assert all(w.category is None or w.category.id != app.work.id for w in app.manager.windows)


def test_deleting_with_the_notes_asks_again_with_the_count(
    app: App, manager_window: CategoryPage, answer: Answers
) -> None:
    hidden = next(w for w in app.manager.windows if w.text == "보고서")
    app.manager.hide(hidden)
    answer.how, answer.sure = "notes", True
    select(manager_window, app.work)
    manager_window.delete()

    assert "a year" in answer.asked[1] and "1 hidden, 0 locked" in answer.asked[1]
    assert sorted(n.body for n in app.notes.deleted()) == ["보고서", "회의 준비"]
    assert sorted(w.text for w in app.manager.windows) == ["또 하나", "아무거나", "장보기"]

    app.manager.restore_last_deleted()  # both come back, with their category

    assert not app.notes.deleted()
    assert [c.id for c in app.labels.categories()] == [app.home.id, app.work.id]
    back = [w for w in app.manager.windows if w.text in ("보고서", "회의 준비")]
    assert len(back) == 2 and all(w.category == app.labels.category(app.work.id) for w in back)


def test_cancelling_the_second_question_changes_nothing(
    app: App, manager_window: CategoryPage, answer: Answers
) -> None:
    answer.how, answer.sure = "notes", False
    select(manager_window, app.work)
    manager_window.delete()

    assert len(app.labels.categories()) == 2 and not app.notes.deleted()


def test_a_category_no_note_has_is_asked_about_once(
    app: App, manager_window: CategoryPage, answer: Answers
) -> None:
    empty = app.labels.create_category("빈 것", "sky")
    manager_window.refresh()
    answer.sure = True
    select(manager_window, empty)
    manager_window.delete()

    assert len(answer.asked) == 1 and "No note has it" in answer.asked[0]
    assert empty.id not in [c.id for c in app.labels.categories()]


def test_the_manager_is_reached_from_the_stickle_window(app: App) -> None:
    app.window.labels_button.click()
    assert app.window.label_manager is not None
    assert names(app.window.label_manager.categories) == ["회사", "집"]
    app.window.label_manager.close()
