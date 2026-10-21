"""The list of notes in the Stickle window."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from stickle.app import note_list
from stickle.app.i18n import Translations
from stickle.app.note_list import ALL, HIDDEN, REFRESH_DELAY_MS, SEARCH_DELAY_MS, SHOWN, TRASH
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.stickle_window import DEFAULT_SIZE, StickleWindow
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import LIST_WINDOW_SIZE, Settings

KEY = secrets.token_bytes(32)


class App:
    def __init__(self, connection: apsw.Connection) -> None:
        self.notes = NoteRepository(connection)
        self.settings = Settings(connection)
        self.manager = NoteManager(self.notes, idle_ms=10, max_ms=50)
        self.window = StickleWindow(
            self.manager, Translations(), lambda: None, settings=self.settings
        )

    def rows(self) -> list[str]:
        rows = self.window.note_list.list
        return [rows.item(row).text() for row in range(rows.count())]

    def note(self, text: str) -> NoteWindow:
        window = self.manager.new_note()
        window.editor.insertPlainText(text)
        self.manager.save(window)
        return window

    def select(self, text: str) -> None:
        rows = self.window.note_list.list
        for row in range(rows.count()):
            if rows.item(row).text().startswith(text):
                rows.setCurrentRow(row)
                return
        raise AssertionError(f"{text!r} not listed in {self.rows()}")

    def show_only(self, which: str) -> None:
        box = self.window.note_list.filter_box
        box.setCurrentIndex(box.findData(which))

    def close(self) -> None:
        for note in self.manager.windows:
            note.release()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


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


def test_every_note_is_listed_latest_change_first_hidden_ones_marked(app: App) -> None:
    first = app.note("첫 메모")
    app.note("# 둘째 메모\n본문")
    app.manager.hide(first)
    app.window.open()

    assert app.rows() == ["첫 메모 · hidden", "둘째 메모"]


def test_notes_on_screen_or_hidden_can_be_shown_apart(app: App) -> None:
    app.manager.hide(app.note("숨긴 것"))
    app.note("보이는 것")
    app.window.open()

    app.show_only(SHOWN)
    assert app.rows() == ["보이는 것"]
    app.show_only(HIDDEN)
    assert app.rows() == ["숨긴 것 · hidden"]
    app.show_only(ALL)
    assert len(app.rows()) == 2


def test_enter_brings_a_note_forward_and_shows_a_hidden_one(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    fronted: list[str | None] = []

    def record(self: NoteWindow) -> None:
        fronted.append(self.note_id)

    monkeypatch.setattr(NoteWindow, "bring_to_front", record)
    shown = app.note("보이는 것")
    app.manager.hide(app.note("숨긴 것"))
    app.window.open()

    app.select("보이는 것")
    QTest.keyClick(app.window.note_list.list, Qt.Key.Key_Return)
    assert fronted == [shown.note_id]

    app.select("숨긴 것")
    QTest.keyClick(app.window.note_list.list, Qt.Key.Key_Return)
    assert len(app.manager.windows) == 2
    assert "숨긴 것" in app.rows()  # no longer marked hidden


def test_delete_key_deletes_the_selected_note_without_asking(app: App) -> None:
    keep = app.note("남길 것")
    app.note("지울 것")
    app.manager.hide(app.note("숨겨서 지울 것"))
    app.window.open()

    app.select("지울 것")
    QTest.keyClick(app.window.note_list.list, Qt.Key.Key_Delete)
    app.select("숨겨서 지울 것")
    QTest.keyClick(app.window.note_list.list, Qt.Key.Key_Delete)

    assert app.rows() == ["남길 것"]
    assert [w.note_id for w in app.manager.windows] == [keep.note_id]
    deleted = app.manager.last_deleted()
    assert deleted is not None and deleted.body == "숨겨서 지울 것"  # can be brought back


def test_the_context_menu_hides_shows_and_deletes(app: App) -> None:
    app.note("메모")
    app.window.open()
    rows = app.window.note_list.list

    def act(label: str) -> None:
        menu = app.window.note_list.menu_for(rows.currentItem())
        action = next(a for a in menu.actions() if a.text() == label)
        action.trigger()
        menu.close()

    app.select("메모")
    act("Hide")
    assert app.rows() == ["메모 · hidden"] and not app.manager.windows
    app.select("메모")
    act("Show")
    assert app.rows() == ["메모"] and len(app.manager.windows) == 1
    app.select("메모")
    act("Delete")
    assert app.rows() == ["No notes here"]


def test_the_list_follows_notes_made_and_changed_while_it_is_open(app: App, qtbot: QtBot) -> None:
    app.window.open()
    note = app.note("처음 제목")
    qtbot.wait(REFRESH_DELAY_MS + 100)
    assert app.rows() == ["처음 제목"]

    note.editor.selectAll()
    note.editor.insertPlainText("고친 제목")
    app.manager.save(note)
    qtbot.wait(REFRESH_DELAY_MS + 100)
    assert app.rows() == ["고친 제목"]


def test_the_list_has_the_keyboard_when_the_window_opens(app: App, qtbot: QtBot) -> None:
    app.note("메모")
    app.window.open()
    qtbot.waitExposed(app.window)

    assert app.window.note_list.list.hasFocus() or app.window.focusWidget() is (
        app.window.note_list.list
    )
    assert app.window.note_list.list.currentRow() == 0


def test_the_window_size_is_kept_on_this_computer(app: App, qtbot: QtBot) -> None:
    assert (app.window.width(), app.window.height()) == DEFAULT_SIZE
    app.window.open()
    qtbot.waitExposed(app.window)
    width = app.window.minimumSizeHint().width() + 60  # wider than its buttons need
    app.window.resize(width, 700)
    app.window.hide()

    assert app.settings.get(LIST_WINDOW_SIZE) == [width, 700]
    again = StickleWindow(app.manager, Translations(), lambda: None, settings=app.settings)
    assert (again.width(), again.height()) == (width, 700)
    again.deleteLater()


# The trash


class Answers:
    """Stands in for the confirmation box: records each question, answers as set."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, yes: bool) -> None:
        self.yes = yes
        self.asked: list[str] = []
        monkeypatch.setattr(note_list, "confirm", self.answer)

    def answer(self, _parent: object, question: str) -> bool:
        self.asked.append(question)
        return self.yes


def deleted(app: App, *texts: str) -> None:
    for text in texts:
        app.manager.delete(app.note(text))


def stay_put(_window: NoteWindow) -> None:
    """In place of bringing a note to the front, which tests need not see."""


def test_the_trash_lists_deleted_notes_latest_first_with_their_day(app: App) -> None:
    app.note("남긴 것")
    deleted(app, "먼저 지운 것", "나중에 지운 것")
    app.window.open()
    app.show_only(TRASH)

    rows = app.rows()
    assert [row.split(" · ")[0] for row in rows] == ["나중에 지운 것", "먼저 지운 것"]
    assert all(" · " in row for row in rows)  # with the day it was deleted
    assert app.window.note_list.empty_button.isVisible()


def test_enter_in_the_trash_brings_a_note_back_on_screen(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(NoteWindow, "bring_to_front", stay_put)
    note = app.note("숨긴 채 지운 것")
    app.manager.hide(note)
    app.manager.delete_note(str(app.notes.hidden()[0].id))
    app.window.open()
    app.show_only(TRASH)

    app.select("숨긴 채 지운 것")
    QTest.keyClick(app.window.note_list.list, Qt.Key.Key_Return)

    assert app.rows() == ["The trash is empty"]
    assert [w.text for w in app.manager.windows] == ["숨긴 채 지운 것"]


def test_delete_in_the_trash_asks_and_empties_only_on_yes(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    deleted(app, "지울 것")
    app.window.open()
    app.show_only(TRASH)

    answers = Answers(monkeypatch, yes=False)
    app.select("지울 것")
    QTest.keyClick(app.window.note_list.list, Qt.Key.Key_Delete)
    assert len(answers.asked) == 1 and "cannot be undone" in answers.asked[0]
    assert len(app.manager.trash_notes()) == 1

    answers.yes = True
    app.select("지울 것")
    QTest.keyClick(app.window.note_list.list, Qt.Key.Key_Delete)
    assert app.manager.trash_notes() == []
    assert app.rows() == ["The trash is empty"]
    assert len(app.notes.deletion_records()) == 1


def test_emptying_the_whole_trash_asks_first(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    kept = app.note("남긴 것")
    deleted(app, "하나", "둘")
    app.window.open()
    app.show_only(TRASH)

    answers = Answers(monkeypatch, yes=True)
    app.window.note_list.empty_button.click()

    assert len(answers.asked) == 1
    assert app.manager.trash_notes() == []
    assert [n.id for n in app.notes.all()] == [kept.note_id]
    assert not app.window.note_list.empty_button.isEnabled()


def test_an_emptied_note_does_not_come_back_anywhere(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    deleted(app, "회의록")
    Answers(monkeypatch, yes=True)
    app.window.open()
    app.show_only(TRASH)
    app.select("회의록")
    QTest.keyClick(app.window.note_list.list, Qt.Key.Key_Delete)

    assert app.manager.last_deleted() is None
    assert not app.window.restore_button.isEnabled()
    app.show_only(ALL)
    assert app.rows() == ["No notes here"]


def test_the_trash_menu_restores_and_empties(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(NoteWindow, "bring_to_front", stay_put)
    deleted(app, "메모")
    Answers(monkeypatch, yes=True)
    app.window.open()
    app.show_only(TRASH)
    rows = app.window.note_list.list

    def act(label: str) -> None:
        menu = app.window.note_list.menu_for(rows.currentItem())
        next(a for a in menu.actions() if a.text() == label).trigger()
        menu.close()

    app.select("메모")
    act("Restore")
    assert len(app.manager.windows) == 1
    app.manager.delete(app.manager.windows[0])
    app.show_only(TRASH)
    app.select("메모")
    act("Empty from the trash…")
    assert app.manager.trash_notes() == []


def test_the_empty_button_shows_only_in_the_trash(app: App) -> None:
    app.window.open()
    assert not app.window.note_list.empty_button.isVisible()
    app.show_only(TRASH)
    assert app.window.note_list.empty_button.isVisible()
    assert not app.window.note_list.empty_button.isEnabled()  # nothing to empty


# Searching


def typed(app: App, qtbot: QtBot, text: str) -> None:
    """Type into the search box, then let the list catch up.

    Inserted as an input method commits text: QTest cannot click Korean
    letters as keys (it brings the test process down on Windows). Typing
    Korean through a real input method is checked on the test machines.
    """
    box = app.window.note_list.search_box
    box.clear()
    box.insert(text)
    qtbot.wait(SEARCH_DELAY_MS + 100)


def titles(app: App) -> list[str]:
    return [row.split(" · ")[0] for row in app.rows()]


def test_typing_narrows_the_list_to_notes_containing_the_text(app: App, qtbot: QtBot) -> None:
    app.note("회의록을 정리")
    app.note("장보기")
    app.note("Weekly Meeting")
    app.window.open()

    typed(app, qtbot, "회의")  # two letters, a particle attached in the note
    assert titles(app) == ["회의록을 정리"]
    typed(app, qtbot, "회의록")
    assert titles(app) == ["회의록을 정리"]
    typed(app, qtbot, "meeting")  # English ignores case
    assert titles(app) == ["Weekly Meeting"]
    typed(app, qtbot, "%")  # a symbol is only itself
    assert app.rows() == ["No notes match"]

    app.window.note_list.search_box.clear()
    qtbot.wait(SEARCH_DELAY_MS + 100)
    assert len(app.rows()) == 3


def test_search_stays_within_the_chosen_notes(app: App, qtbot: QtBot) -> None:
    app.note("회의 보이는 것")
    app.manager.hide(app.note("회의 숨긴 것"))
    deleted(app, "회의 지운 것", "비운 회의")
    app.notes.purge(app.notes.deleted()[0].id)  # 비운 회의, the latest deleted
    app.window.open()

    typed(app, qtbot, "회의")
    assert sorted(titles(app)) == ["회의 보이는 것", "회의 숨긴 것"]
    app.show_only(HIDDEN)
    assert titles(app) == ["회의 숨긴 것"]
    app.show_only(TRASH)
    assert titles(app) == ["회의 지운 것"]
    assert app.window.note_list.empty_button.isEnabled()


def test_emptying_the_trash_while_searching_still_empties_all_of_it(
    app: App, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    deleted(app, "회의", "장보기")
    app.window.open()
    app.show_only(TRASH)
    typed(app, qtbot, "없는 말")
    assert app.window.note_list.empty_button.isEnabled()  # the trash is not empty

    Answers(monkeypatch, yes=True)
    app.window.note_list.empty_button.click()
    assert app.manager.trash_notes() == []


def test_the_results_follow_a_note_as_it_is_changed(app: App, qtbot: QtBot) -> None:
    note = app.note("회의 준비")
    app.window.open()
    typed(app, qtbot, "회의")
    assert titles(app) == ["회의 준비"]

    note.editor.selectAll()
    note.editor.insertPlainText("장보기")
    app.manager.save(note)
    qtbot.wait(REFRESH_DELAY_MS + 100)
    assert app.rows() == ["No notes match"]


def test_the_search_is_reached_and_left_with_the_keyboard(
    app: App, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    fronted: list[str | None] = []

    def record(self: NoteWindow) -> None:
        fronted.append(self.note_id)

    monkeypatch.setattr(NoteWindow, "bring_to_front", record)
    app.note("장보기")
    wanted = app.note("회의 준비")
    app.note("회의록")
    app.window.open()
    qtbot.waitActive(app.window)  # shortcuts reach only the active window
    listed = app.window.note_list
    box = listed.search_box

    # Through the window, as a key press arrives, so the shortcut map sees it.
    QTest.keySequence(app.window.windowHandle(), QKeySequence(QKeySequence.StandardKey.Find))
    assert app.window.focusWidget() is box
    box.insert("준비")
    QTest.keyClick(box, Qt.Key.Key_Down)  # before the pause: the list catches up at once
    assert app.window.focusWidget() is listed.list
    assert titles(app) == ["회의 준비"] and listed.list.currentRow() == 0
    QTest.keyClick(listed.list, Qt.Key.Key_Return)
    assert fronted == [wanted.note_id]

    box.setFocus()
    QTest.keyClick(box, Qt.Key.Key_Escape)
    assert box.text() == ""
    assert len(app.rows()) == 3


def test_the_search_box_is_named_and_translated(app: App, translations: Translations) -> None:
    box = app.window.note_list.search_box
    assert box.accessibleName() == "Search notes"
    assert box.placeholderText() == "Search notes"

    translations.apply("ko")
    assert box.accessibleName() == "메모 검색"
    assert box.placeholderText() == "메모 검색"
    assert "목록" in box.accessibleDescription()
    box.setText("없는 말")
    app.window.note_list.refresh()
    assert app.rows() == ["찾는 메모가 없습니다"]
