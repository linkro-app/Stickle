"""Finding notes on the desktop by their text."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from stickle.app.application import answer_request
from stickle.app.i18n import Translations
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.stickle_window import StickleWindow
from stickle.data.labels import LabelRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.platform.instance import FIND

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
        self.finder = self.window.finder

    def note(self, text: str) -> NoteWindow:
        window = self.manager.new_note()
        window.editor.insertPlainText(text)
        self.manager.save(window)
        return window

    def shown(self) -> list[str]:
        return sorted(w.text for w in self.manager.windows if w.isVisible())

    def type(self, text: str) -> None:
        self.finder.box.setText(text)
        self.finder.search()

    def close(self) -> None:
        self.finder.hide()
        for window in self.manager.windows:
            window.release()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


ALL = ["아무거나", "장보기 우유", "회의 준비", "회의록을 정리"]


@pytest.fixture
def app(qtbot: QtBot, connection: apsw.Connection) -> Iterator[App]:
    app = App(connection)
    for text in ("회의록을 정리", "장보기 우유", "회의 준비", "아무거나"):
        app.note(text)
    yield app
    app.close()


def test_typing_leaves_only_the_notes_found(app: App) -> None:
    places = {w.text: w.pos() for w in app.manager.windows}
    app.finder.open()
    assert app.shown() == ALL  # nothing typed yet

    app.type("회의")
    assert app.shown() == ["회의 준비", "회의록을 정리"]
    assert app.finder.count.text() == "2 note(s)"

    QTest.keyClick(app.finder.box, Qt.Key.Key_Escape)
    assert app.shown() == ALL
    assert {w.text: w.pos() for w in app.manager.windows} == places
    assert all(not note.hidden for note in app.notes.live())  # nothing stored


def test_down_chooses_the_next_and_enter_goes_to_it(app: App) -> None:
    app.finder.open()
    app.type("회의")
    first, second = app.finder.found
    assert first.found and not second.found

    QTest.keyClick(app.finder.box, Qt.Key.Key_Down)
    assert second.found and not first.found
    QTest.keyClick(app.finder.box, Qt.Key.Key_Up)
    QTest.keyClick(app.finder.box, Qt.Key.Key_Up)  # round to the last
    assert second.found

    QTest.keyClick(app.finder.box, Qt.Key.Key_Return)
    assert not app.finder.isVisible()
    assert app.shown() == ALL
    assert not second.found


def test_nothing_found_says_so(app: App) -> None:
    app.finder.open()
    app.type("없는말")
    assert app.shown() == []
    assert app.finder.count.text() == "No note on the desktop matches."
    app.finder.end()
    assert app.shown() == ALL


def test_text_just_typed_is_found(app: App) -> None:
    window = app.manager.new_note()
    window.editor.insertPlainText("방금 친 메모")  # not saved yet
    app.finder.open()
    app.type("방금")
    assert app.shown() == ["방금 친 메모"]
    app.finder.end()


def test_hidden_and_deleted_notes_found_are_counted_and_listed(app: App) -> None:
    hidden = next(w for w in app.manager.windows if w.text == "회의 준비")
    app.manager.hide(hidden)
    app.manager.delete(app.note("지난 회의"))
    app.finder.open()
    app.type("회의")
    assert app.finder.elsewhere.isVisibleTo(app.finder)
    assert "1 hidden" in app.finder.elsewhere.text()
    assert "1 in the trash" in app.finder.elsewhere.text()

    QTest.keyClick(app.finder.box, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert not app.finder.isVisible() and app.window.isVisible()
    listed = app.window.note_list
    assert listed.search_box.text() == "회의"
    assert listed.list.count() == 2  # the hidden one too
    assert listed.trash_row.isVisible()  # and the one in the trash, a button away
    assert listed.trash_found.text() == "1 note(s) in the trash match too."


def test_the_stickle_window_lists_every_category_and_mark_for_what_was_found(app: App) -> None:
    work = app.labels.create_category("회사", "blue")
    urgent = app.labels.marks()[0]
    app.window.open()
    listed = app.window.note_list
    listed.category_box.setCurrentIndex(listed.category_box.findData(work.id))
    listed.mark_box.setCurrentIndex(listed.mark_box.findData(urgent.id))
    app.window.hide()
    app.manager.hide(next(w for w in app.manager.windows if w.text == "회의 준비"))

    app.finder.open()
    app.type("회의")
    QTest.keyClick(app.finder.box, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert listed.category_box.currentIndex() == 0  # all categories
    assert listed.mark_box.currentIndex() == 0  # all marks
    assert listed.list.count() == 2  # as many as were found, in no category, unmarked


def test_only_the_view_is_searched(app: App) -> None:
    work = app.labels.create_category("회사", "blue")
    meeting = next(w for w in app.manager.windows if w.text == "회의 준비")
    app.manager.set_category(meeting, work.id)
    app.manager.set_view(work.id)

    app.finder.open()
    app.type("회의")
    assert app.shown() == ["회의 준비"]
    app.finder.end()
    assert app.shown() == ["회의 준비"]  # back to the view, not every note


def test_the_command_line_opens_it(app: App) -> None:
    answer_request(FIND, app.manager, app.window)
    assert app.finder.isVisible()
    app.finder.end()


def test_the_box_keeps_its_width(app: App) -> None:
    app.finder.open()
    width = app.finder.width()
    app.type("회의")
    app.type("없는말")
    assert app.finder.width() == width >= 400
    app.finder.end()
