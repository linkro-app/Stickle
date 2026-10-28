"""Notes survive restarts; hiding, deleting and bringing back through the tray."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFocusEvent, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from stickle.app.i18n import Translations
from stickle.app.note_window import NoteWindow
from stickle.app.notes import HIDDEN_LISTED, NoteManager
from stickle.app.tray import Tray
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store

KEY = secrets.token_bytes(32)


class Session:
    """One run of the app against a database, which can be restarted."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: apsw.Connection = open_store(path, KEY)
        self.repository = NoteRepository(self.connection)
        self.manager = NoteManager(self.repository)
        self.translations = Translations()
        self.tray = Tray(self.manager.new_note, lambda: None, self.translations, self.manager)
        self.manager.open_stored()

    def quit(self) -> None:
        """What happens on quit: the Quit event reaches the manager, then Qt closes windows."""
        app = QApplication.instance()
        assert app is not None
        self.manager.eventFilter(app, QEvent(QEvent.Type.Quit))
        for window in self.manager.windows:
            window.close()
        self.connection.close()

    def restart(self) -> Session:
        self.quit()
        return Session(self.path)

    def texts(self) -> list[str]:
        return sorted(window.text for window in self.manager.windows)


@pytest.fixture
def session(qtbot: QtBot, tmp_path: Path) -> Iterator[Session]:
    sessions = [Session(tmp_path / "notes.db")]
    yield sessions[0]
    for window in QApplication.topLevelWidgets():
        if isinstance(window, NoteWindow):
            window.release()


def type_into(window: NoteWindow, text: str) -> None:
    # Not QTest.keyClicks: with Hangul it crashes the test process (PySide6 6.11).
    # Real typing through an input method is checked by hand.
    window.editor.moveCursor(QTextCursor.MoveOperation.End)
    window.editor.insertPlainText(text)


def leave(window: NoteWindow) -> None:
    """The text loses focus, as when the user clicks elsewhere."""
    QApplication.sendEvent(window.editor, QFocusEvent(QEvent.Type.FocusOut))


def only_window(session: Session) -> NoteWindow:
    (window,) = session.manager.windows
    return window


# A. Notes come back after a restart, with their text.
def test_notes_come_back_after_a_restart(session: Session) -> None:
    first = only_window(session)  # the one empty note of a first start
    type_into(first, "회의록 내일까지")
    type_into(session.manager.new_note(), "장보기: 우유")

    later = session.restart()

    assert later.texts() == ["장보기: 우유", "회의록 내일까지"]


# B. An untouched new note leaves nothing behind.
def test_an_untouched_note_is_not_stored(session: Session) -> None:
    only_window(session).title_bar.close_button.click()

    assert session.repository.all() == []
    assert session.manager.windows == ()


# C. Hidden notes stay hidden, are listed in the tray, and come back from there.
def test_hidden_note_is_listed_and_comes_back(qtbot: QtBot, session: Session) -> None:
    window = only_window(session)
    type_into(window, "회의록\n둘째 줄")
    QTest.keyClick(window.editor, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: session.manager.windows == ())

    later = session.restart()
    assert later.manager.windows == ()
    (entry,) = [a for a in later.tray.hidden_menu.actions() if a.text() == "&1 회의록"]
    entry.trigger()

    assert later.texts() == ["회의록\n둘째 줄"]
    assert later.repository.hidden() == []


# D. Deleting asks nothing and can be undone, also after a restart.
def test_deleted_note_can_be_restored(session: Session) -> None:
    window = only_window(session)
    type_into(window, "지울 메모")
    window.delete_action.trigger()
    assert session.manager.windows == ()
    assert session.tray.restore_action.isEnabled()

    later = session.restart()
    assert later.manager.windows == ()
    assert "지울 메모" in later.tray.restore_action.text()
    later.tray.restore_action.trigger()

    assert later.texts() == ["지울 메모"]
    assert not later.tray.restore_action.isEnabled()


# E. A note with no text goes for good when closed or deleted: there is nothing in
# it to bring back, so it is neither hidden nor put into the trash.
def test_a_new_note_with_only_spaces_is_not_stored(session: Session) -> None:
    window = only_window(session)
    type_into(window, " \n ")
    leave(window)
    assert session.repository.all() == []

    window.title_bar.close_button.click()

    assert session.repository.all() == []
    assert not session.tray.restore_action.isEnabled()


@pytest.mark.parametrize("how", ["close", "delete"])
def test_a_note_emptied_by_hand_goes_for_good(session: Session, how: str) -> None:
    window = only_window(session)
    type_into(window, "잠깐")
    leave(window)
    stored = window.note_id
    window.editor.clear()
    type_into(window, " \n")  # a space and a line break left

    if how == "close":
        window.title_bar.close_button.click()
    else:
        window.delete_action.trigger()

    assert session.manager.windows == ()
    assert session.repository.all() == []
    assert [record[0] for record in session.repository.deletion_records()] == [stored]
    assert not session.tray.restore_action.isEnabled()
    assert not session.tray.hidden_menu.isEnabled()


def test_the_note_to_restore_stays_the_last_one_with_text(session: Session) -> None:
    window = only_window(session)
    type_into(window, "지운 메모")
    window.delete_action.trigger()
    empty = session.manager.new_note()
    type_into(empty, "곧 지움")
    leave(empty)
    empty.editor.clear()

    empty.title_bar.close_button.click()

    assert "지운 메모" in session.tray.restore_action.text()


def test_an_empty_note_restored_from_the_trash_goes_for_good_when_closed(
    session: Session,
) -> None:
    only_window(session).release()
    note = session.repository.create("옛 메모")
    session.repository.update_body(note.id, "")
    session.repository.delete(note.id)  # as Stickle 0.5 left such a note
    session.manager.restore_last_deleted()

    only_window(session).title_bar.close_button.click()

    assert session.repository.all() == []
    assert not session.tray.restore_action.isEnabled()


def test_deleting_a_hidden_note_with_no_text_removes_it_for_good(session: Session) -> None:
    note = session.repository.create("x")
    session.repository.update_body(note.id, "")
    session.repository.set_hidden(note.id, True)

    session.manager.delete_note(note.id)

    assert session.repository.get(note.id) is None
    assert session.repository.last_deleted() is None


# F. Quitting saves every open note, and does not hide them.
def test_quitting_saves_everything_without_hiding(session: Session) -> None:
    windows = [only_window(session), session.manager.new_note(), session.manager.new_note()]
    for number, window in enumerate(windows):
        type_into(window, f"메모 {number}")
    leave(windows[0])
    type_into(windows[0], " 더")  # typed after the last save

    later = session.restart()

    assert later.texts() == ["메모 0 더", "메모 1", "메모 2"]
    assert later.repository.hidden() == []


def test_leaving_a_note_saves_it(session: Session) -> None:
    window = only_window(session)
    type_into(window, "저장됨")
    leave(window)

    assert [n.body for n in session.repository.all()] == ["저장됨"]


def test_a_close_from_outside_hides_the_note(qtbot: QtBot, session: Session) -> None:
    # Alt+F4 and the like: the note must not vanish without being stored.
    window = only_window(session)
    type_into(window, "밖에서 닫힘")
    window.close()
    qtbot.waitUntil(lambda: session.manager.windows == ())

    assert [n.body for n in session.repository.hidden()] == ["밖에서 닫힘"]


# G. The tray lists the latest hidden notes; the rest are one click away.
def test_tray_lists_the_latest_hidden_notes(qtbot: QtBot, session: Session) -> None:
    only_window(session).release()
    for number in range(20):
        window = session.manager.new_note()
        type_into(window, f"숨긴 메모 {number:02d}")
        window.title_bar.close_button.click()

    actions = [a for a in session.tray.hidden_menu.actions() if not a.isSeparator()]
    titles = [a.text() for a in actions]
    assert len(titles) == HIDDEN_LISTED + 1
    assert titles[0] == "&1 숨긴 메모 19"  # the latest first, chosen by its number
    actions[-1].trigger()  # show all

    assert len(session.manager.windows) == 20
    assert session.repository.hidden() == []


# H. Only hidden notes: nothing new is opened (the Stickle window lists them).
def test_no_empty_note_is_added_while_notes_are_hidden(session: Session) -> None:
    window = only_window(session)
    type_into(window, "숨김")
    window.title_bar.close_button.click()

    assert session.restart().manager.windows == ()


# I. The note menu works from the keyboard.
def test_note_menu_opens_with_f10(qtbot: QtBot, session: Session) -> None:
    window = only_window(session)
    qtbot.waitActive(window)
    type_into(window, "키보드로 지우기")

    QTest.keyClick(window.windowHandle(), Qt.Key.Key_F10)
    qtbot.waitUntil(window.menu.isVisible)
    # Opens with nothing chosen, so Enter alone never deletes; Down picks the first item.
    assert window.menu.activeAction() is None
    assert not window.color_menu.isVisible()
    QTest.keyClick(window.menu, Qt.Key.Key_Down)
    assert window.menu.activeAction() is window.color_menu.menuAction()
    for _ in range(len(window.menu.actions())):
        if window.menu.activeAction() is window.delete_action:
            break
        QTest.keyClick(window.menu, Qt.Key.Key_Down)
    assert window.menu.activeAction() is window.delete_action
    QTest.keyClick(window.menu, Qt.Key.Key_Return)

    qtbot.waitUntil(lambda: session.manager.windows == ())
    assert session.repository.last_deleted() is not None


def test_ampersands_in_titles_stay_literal(session: Session) -> None:
    window = only_window(session)
    type_into(window, "R&D 회의")
    window.title_bar.close_button.click()

    (entry,) = [a for a in session.tray.hidden_menu.actions() if "R" in a.text()]
    assert entry.text() == "&1 R&&D 회의"


# J. No note text in the log.
def test_note_text_is_never_logged(session: Session, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    window = only_window(session)
    type_into(window, "비밀 내용")
    window.title_bar.close_button.click()
    later = session.restart()
    later.tray.hidden_menu.actions()[0].trigger()
    later.restart()

    assert "비밀" not in caplog.text


def test_a_stored_note_never_placed_opens_showing_its_text_whole(
    qtbot: QtBot, tmp_path: Path
) -> None:
    connection = open_store(tmp_path / "notes.db", KEY)
    repository = NoteRepository(connection)
    repository.create("# Welcome\n" + "\n".join(f"- [ ] step {n}" for n in range(20)))
    repository.create("# Long\n" + "\n".join(f"line {n}" for n in range(400)))
    manager = NoteManager(repository)

    manager.open_stored()
    qtbot.wait(50)

    screen = QApplication.primaryScreen().availableGeometry()
    welcome, long = manager.windows
    assert welcome.height() > 240  # more than a new note gets
    assert welcome.view.verticalScrollBar().maximum() == 0
    assert welcome.width() == 260
    assert long.height() <= screen.height() * 0.6
    for window in manager.windows:
        window.release()
    connection.close()


def test_only_the_note_in_front_asks_for_the_keyboard_at_start(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Under X11 a request coming after another window's is refused, and GNOME
    # then tells that the note "is ready": the notes opened ask for none, and
    # the one in front asks once nothing else shown at start wants it.
    for text in ("하나", "둘", "셋"):
        session.repository.create(text)
    asked: list[NoteWindow] = []

    def activate(window: NoteWindow) -> None:
        asked.append(window)

    monkeypatch.setattr(NoteWindow, "activateWindow", activate)

    later = session.restart()
    assert len(later.manager.windows) == 3
    assert asked == []

    later.manager.give_keyboard()
    assert asked == [later.manager.windows[-1]]
