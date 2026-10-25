"""Secret notes in their windows: made, opened and locked again, their text only
ever stored encrypted, and a locked one never taken for an empty note."""

import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog
from pytestqt.qtbot import QtBot
from test_autosave import compose
from test_startup_dialogs import FAST, Backend, store_of

from stickle.app import application, notes, secret_dialogs, secret_notes, settings_window
from stickle.app.i18n import Translations
from stickle.app.master_password import MasterPasswordDialog
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.protection_changes import ProtectionChanges
from stickle.app.secret_dialogs import (
    NewSecretPasswordDialog,
    OpenSecretDialog,
    SecretTitleDialog,
    TurnOnDialog,
)
from stickle.app.settings_window import SettingsWindow
from stickle.core.note import Note
from stickle.crypto.secret_notes import open_text
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.search import search
from stickle.data.settings import Settings
from stickle.unlock import Unlock

KEY = secrets.token_bytes(32)
SECRET = "secret notes 1"
MASTER = "correct horse battery staple"
OTHER = "another phrase to remember well"
TEXT = "은행 비밀번호 힌트 zqxjkv-plorbit"

type Answer = Callable[[QDialog], bool]


class Answers:
    """What the user does in each window asked, in turn."""

    def __init__(self) -> None:
        self.queue: list[Answer] = []
        self.asked: list[QDialog] = []

    def __call__(self, dialog: QDialog) -> bool:
        self.asked.append(dialog)
        return self.queue.pop(0)(dialog)


def accepted(dialog: QDialog) -> bool:
    """Its button pressed: whether the window took it (it stays open if not)."""
    dialog.accept()
    return dialog.result() == QDialog.DialogCode.Accepted.value


def turn_on(password: str = SECRET, master: str = MASTER) -> Answer:
    def answer(dialog: QDialog) -> bool:
        assert isinstance(dialog, TurnOnDialog)
        dialog.password.setText(password)
        dialog.confirm.setText(password)
        dialog.master.setText(master)
        dialog.master_confirm.setText(master)
        dialog.understood.setChecked(True)
        return accepted(dialog)

    return answer


def password(typed: str = SECRET) -> Answer:
    def answer(dialog: QDialog) -> bool:
        assert isinstance(dialog, OpenSecretDialog)
        dialog.password.setText(typed)
        return accepted(dialog)

    return answer


def title(typed: str) -> Answer:
    def answer(dialog: QDialog) -> bool:
        assert isinstance(dialog, SecretTitleDialog)
        dialog.title.setText(typed)
        return accepted(dialog)

    return answer


def cancel(dialog: QDialog) -> bool:
    dialog.reject()
    return False


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def unlock(tmp_path: Path) -> Unlock:
    (tmp_path / "data").mkdir()
    unlock = Unlock(tmp_path / "data", store_of(Backend()), FAST, master_strength=FAST)
    unlock.opened_key = KEY
    unlock.make_recovery_key(KEY)
    return unlock


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> Answers:
    answers = Answers()
    monkeypatch.setattr(notes, "ask_secret", answers)
    return answers


@pytest.fixture
def manager(
    qtbot: QtBot, connection: apsw.Connection, unlock: Unlock, answers: Answers
) -> Iterator[NoteManager]:
    manager = NoteManager(NoteRepository(connection), idle_ms=50, settings=Settings(connection))
    manager.secrets = application.secret_passwords(unlock, application.master_passwords(unlock))
    yield manager
    for window in manager.windows:
        window.release()


def stored(connection: apsw.Connection, window: NoteWindow) -> Note:
    assert window.note_id is not None
    note = NoteRepository(connection).get(window.note_id)
    assert note is not None
    return note


def opened_text(connection: apsw.Connection, unlock: Unlock, window: NoteWindow) -> str:
    note = stored(connection, window)
    assert note.sealed is not None and note.body == ""
    return open_text(unlock.open_secret_notes(SECRET), note.id, note.sealed)


def secret_note(
    qtbot: QtBot, manager: NoteManager, answers: Answers, text: str = TEXT, first: bool = True
) -> NoteWindow:
    window = manager.new_note()
    window.editor.insertPlainText(text)
    qtbot.waitExposed(window)
    answers.queue += [turn_on() if first else password(), title("은행")]
    window.secret_action.trigger()
    assert window.secret and not window.secret_locked
    return window


def type_into(window: NoteWindow, text: str) -> None:
    window.editor.moveCursor(QTextCursor.MoveOperation.End)
    window.editor.insertPlainText(text)


# Making one


def test_made_secret_its_text_is_stored_encrypted_only(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
    unlock: Unlock,
) -> None:
    window = secret_note(qtbot, manager, answers)

    assert unlock.has_secret_notes and unlock.has_master_password
    note = stored(connection, window)
    assert note.secret_title == "은행"
    assert opened_text(connection, unlock, window) == TEXT
    assert search(connection, "plorbit") == []
    assert window.text == TEXT  # still open
    assert window.title_bar.title.isVisibleTo(window)
    assert window.title_bar.title.text() == "은행"
    assert not window.secret_action.isVisible()
    assert window.lock_secret_action.isVisible() and window.ordinary_action.isVisible()


def test_the_second_one_asks_only_their_password(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
    unlock: Unlock,
) -> None:
    secret_note(qtbot, manager, answers)

    second = secret_note(qtbot, manager, answers, "두 번째", first=False)

    assert isinstance(answers.asked[-2], OpenSecretDialog)
    assert answers.asked[-2].purpose == "make"
    assert opened_text(connection, unlock, second) == "두 번째"


def test_cancelled_nothing_changes(
    qtbot: QtBot, manager: NoteManager, answers: Answers, connection: apsw.Connection
) -> None:
    window = manager.new_note()
    window.editor.insertPlainText(TEXT)
    answers.queue += [turn_on(), cancel]  # the title asked for, then not given

    window.secret_action.trigger()

    assert not window.secret
    assert stored(connection, window).body == TEXT
    assert manager._keys == {}  # pyright: ignore[reportPrivateUsage]


def test_a_new_note_not_yet_stored_can_be_made_secret(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
    unlock: Unlock,
) -> None:
    window = manager.new_note()  # nothing typed: not stored yet
    answers.queue += [turn_on(), title("")]

    window.secret_action.trigger()

    assert window.note_id is not None and window.secret
    assert opened_text(connection, unlock, window) == ""
    assert window.secret_name == "Secret note"  # none typed


def test_secret_notes_are_not_offered_without_them(
    qtbot: QtBot, connection: apsw.Connection
) -> None:
    manager = NoteManager(NoteRepository(connection))  # measuring: no keys
    window = manager.new_note()
    try:
        assert not window.secret_action.isVisible()
    finally:
        window.release()


# Saving and locking


def test_typing_in_it_saves_encrypted(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
    unlock: Unlock,
) -> None:
    window = secret_note(qtbot, manager, answers)

    type_into(window, " 추가")

    qtbot.waitUntil(lambda: opened_text(connection, unlock, window) == TEXT + " 추가")
    assert stored(connection, window).body == ""
    assert search(connection, "추가") == []


def test_locking_saves_first_then_holds_nothing_of_it(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
    unlock: Unlock,
) -> None:
    window = secret_note(qtbot, manager, answers)
    type_into(window, " 마지막")  # not saved yet
    compose(window, "글")  # nor this, still being composed
    key = manager._keys[window]  # pyright: ignore[reportPrivateUsage]

    window.lock_secret_action.trigger()

    assert opened_text(connection, unlock, window) == TEXT + " 마지막글"
    assert window.secret_locked
    assert window.text == "" and window.view.toPlainText() == ""
    assert not window.editor.document().isUndoAvailable()  # nothing to bring back
    assert key == bytearray(len(key))  # its bytes overwritten
    assert window not in manager._keys  # pyright: ignore[reportPrivateUsage]
    assert window.stack.currentWidget() is window.locked_face
    assert window.open_secret_action.isVisible()
    assert not window.lock_secret_action.isVisible()


def test_locked_it_cannot_be_edited_until_opened(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
) -> None:
    window = secret_note(qtbot, manager, answers)
    manager.lock_secret(window)

    window.edit()

    assert window.stack.currentWidget() is window.locked_face
    assert window.locked_face.accessibleName() == "Secret note, locked: 은행"


def test_opened_with_the_password_its_text_comes_back(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
) -> None:
    window = secret_note(qtbot, manager, answers)
    manager.lock_secret(window)
    wrong_first: list[str] = []

    def wrong_then_right(dialog: QDialog) -> bool:
        assert isinstance(dialog, OpenSecretDialog)
        dialog.password.setText("not it at all")
        if accepted(dialog):
            return True
        wrong_first.append(dialog.message.text())
        dialog.password.setText(SECRET)
        return accepted(dialog)

    answers.queue.append(wrong_then_right)
    QTest.keyClick(window.locked_face, Qt.Key.Key_Return)

    assert wrong_first == ["The password is not correct."]
    assert not window.secret_locked
    assert window.text == TEXT


def test_a_double_click_asks_to_open_it(
    qtbot: QtBot, manager: NoteManager, answers: Answers
) -> None:
    window = secret_note(qtbot, manager, answers)
    manager.lock_secret(window)
    answers.queue.append(password())

    QTest.mouseDClick(
        window.locked_face, Qt.MouseButton.LeftButton, pos=window.locked_face.rect().center()
    )

    assert window.text == TEXT


def test_each_note_opens_on_its_own(qtbot: QtBot, manager: NoteManager, answers: Answers) -> None:
    first = secret_note(qtbot, manager, answers)
    second = secret_note(qtbot, manager, answers, "두 번째", first=False)
    manager.lock_secret_notes()
    assert first.secret_locked and second.secret_locked

    answers.queue.append(password())
    first.open_secret_action.trigger()

    assert not first.secret_locked and first.text == TEXT
    assert second.secret_locked and second.text == ""


def test_a_note_that_cannot_be_saved_stays_open(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = secret_note(qtbot, manager, answers)
    repository = manager._repository  # pyright: ignore[reportPrivateUsage]
    assert repository is not None

    def full(note_id: str, sealed: bytes) -> Note:
        raise apsw.FullError("database or disk is full")

    monkeypatch.setattr(repository, "update_secret_body", full)
    type_into(window, " 저장 안 됨")

    assert not manager.lock_secret(window)
    assert not window.secret_locked  # locking would lose the text
    assert window.text == TEXT + " 저장 안 됨" and window.unsaved


# Locking by itself


def test_left_alone_it_locks_by_itself(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secret_notes, "MINUTE_MS", 100)  # a "minute" of 0.1 s
    window = secret_note(qtbot, manager, answers)  # locks after 5 of them

    qtbot.wait(300)
    type_into(window, "!")  # in use: its time starts again
    qtbot.wait(300)
    assert not window.secret_locked

    qtbot.waitUntil(lambda: window.secret_locked, timeout=2000)


def test_the_time_is_a_setting(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secret_notes, "MINUTE_MS", 100)
    assert manager.relock_minutes == 5
    window = secret_note(qtbot, manager, answers)

    manager.set_relock_minutes(1)

    assert NoteManager(settings=Settings(connection)).relock_minutes == 1  # kept
    qtbot.waitUntil(lambda: window.secret_locked, timeout=400)


def test_the_computer_locked_locks_every_open_one(
    qtbot: QtBot, manager: NoteManager, answers: Answers
) -> None:
    first = secret_note(qtbot, manager, answers)
    second = secret_note(qtbot, manager, answers, "두 번째", first=False)

    manager.lock_secret_notes()

    assert first.secret_locked and second.secret_locked


def test_the_screen_saver_locks_them(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = secret_note(qtbot, manager, answers)
    monkeypatch.setattr(secret_notes, "screen_saver_running", lambda: True)

    manager._relock._check_screen_saver()  # pyright: ignore[reportPrivateUsage]

    assert window.secret_locked


def test_with_it_chosen_using_another_window_locks_it(
    qtbot: QtBot, manager: NoteManager, answers: Answers
) -> None:
    window = secret_note(qtbot, manager, answers)
    relock = manager._relock  # pyright: ignore[reportPrivateUsage]

    relock._left(window)  # pyright: ignore[reportPrivateUsage]
    assert not window.secret_locked  # as long as it is not chosen: off at first

    manager.set_lock_on_leave(True)
    relock._left(window)  # pyright: ignore[reportPrivateUsage]
    assert window.secret_locked


def test_its_own_dialog_is_not_another_window(
    qtbot: QtBot, manager: NoteManager, answers: Answers
) -> None:
    window = secret_note(qtbot, manager, answers)
    own = SecretTitleDialog(parent=window)

    assert secret_notes._belongs_to(own, window)  # pyright: ignore[reportPrivateUsage]
    assert not secret_notes._belongs_to(SecretTitleDialog(), window)  # pyright: ignore[reportPrivateUsage]


# Never taken for an empty note


def test_hidden_or_deleted_while_locked_it_is_kept(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
) -> None:
    hidden = secret_note(qtbot, manager, answers)
    deleted = secret_note(qtbot, manager, answers, "두 번째", first=False)
    hidden_id, deleted_id = hidden.note_id, deleted.note_id
    manager.lock_secret_notes()

    hidden.title_bar.close_button.click()
    manager.delete(deleted)

    repository = NoteRepository(connection)
    assert [note.id for note in repository.hidden()] == [hidden_id]
    assert [note.id for note in repository.deleted()] == [deleted_id]


def test_an_open_secret_note_emptied_is_kept_too(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
) -> None:
    window = secret_note(qtbot, manager, answers)
    window.editor.clear()
    note_id = window.note_id

    manager.delete(window)

    assert [note.id for note in NoteRepository(connection).deleted()] == [note_id]


def test_deleted_from_the_list_while_closed_it_goes_to_the_trash(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
) -> None:
    window = secret_note(qtbot, manager, answers)
    note_id = window.note_id
    assert note_id is not None
    window.title_bar.close_button.click()

    manager.delete_note(note_id)

    assert [note.id for note in NoteRepository(connection).deleted()] == [note_id]


def test_stored_it_opens_locked_by_its_title(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
) -> None:
    window = secret_note(qtbot, manager, answers)
    window.release()

    manager.open_stored()

    (reopened,) = manager.windows
    assert reopened.secret_locked and reopened.text == ""
    assert reopened.title_bar.title.text() == "은행"


# Back to an ordinary note, and its title


def test_made_ordinary_it_is_found_again(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = secret_note(qtbot, manager, answers)
    asked: list[bool] = []

    def confirm(_parent: object) -> bool:
        asked.append(True)
        return True

    monkeypatch.setattr(notes, "confirm_ordinary", confirm)

    window.ordinary_action.trigger()

    assert asked and not window.secret
    note = stored(connection, window)
    assert note.body == TEXT and not note.secret
    assert search(connection, "plorbit") == [note.id]
    assert window not in manager._keys  # pyright: ignore[reportPrivateUsage]
    assert window.secret_action.isVisible()


def test_its_title_can_be_changed_while_open(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
) -> None:
    window = secret_note(qtbot, manager, answers)
    answers.queue.append(title("통장"))

    window.secret_title_action.trigger()

    assert stored(connection, window).secret_title == "통장"
    assert window.title_bar.title.text() == "통장"


# Their password


def test_forgotten_the_master_password_opens_it_and_a_new_one_is_set(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    unlock: Unlock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = secret_note(qtbot, manager, answers)
    manager.lock_secret(window)

    def new_password(dialog: NewSecretPasswordDialog) -> bool:
        dialog.password.setText("a new one 22")
        dialog.confirm.setText("a new one 22")
        return accepted(dialog)

    monkeypatch.setattr(secret_dialogs, "ask_new_password", new_password)

    def with_master(dialog: QDialog) -> bool:
        assert isinstance(dialog, OpenSecretDialog)
        dialog.forgot_button.click()
        dialog.password.setText(MASTER)
        return accepted(dialog)

    answers.queue.append(with_master)
    window.open_secret_action.trigger()

    assert window.text == TEXT
    assert unlock.opens_secret_notes("a new one 22")
    assert not unlock.opens_secret_notes(SECRET)


@pytest.mark.parametrize(
    ("typed", "master", "why"),
    [
        ("short", MASTER, "Use at least 8 characters."),
        (MASTER, MASTER, "Choose a master password other than the secret notes' password."),
        (SECRET, "password", "Use at least 12 characters, or 4 words or more."),
    ],
)
def test_a_weak_or_repeated_password_is_refused(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    unlock: Unlock,
    typed: str,
    master: str,
    why: str,
) -> None:
    window = manager.new_note()
    window.editor.insertPlainText(TEXT)
    refused: list[str] = []

    def refuse(dialog: QDialog) -> bool:
        if not turn_on(typed, master)(dialog):
            assert isinstance(dialog, TurnOnDialog)
            refused.append(dialog.message.text())
        return False

    answers.queue.append(refuse)
    window.secret_action.trigger()

    assert refused == [why]
    assert not window.secret and not unlock.has_secret_notes


def test_their_password_cannot_be_the_master_password(
    qtbot: QtBot, manager: NoteManager, answers: Answers, unlock: Unlock
) -> None:
    unlock.set_master_password(KEY, MASTER)  # set before, in the settings
    window = manager.new_note()
    refused: list[str] = []

    def refuse(dialog: QDialog) -> bool:
        assert isinstance(dialog, TurnOnDialog) and dialog.has_master
        if not turn_on(MASTER, MASTER)(dialog):
            refused.append(dialog.message.text())
        return False

    answers.queue.append(refuse)
    window.secret_action.trigger()

    assert refused == ["Choose a password other than the master password."]


def test_nothing_of_its_text_is_logged(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    window = secret_note(qtbot, manager, answers)
    type_into(window, " 더")
    manager.lock_secret(window)
    answers.queue.append(password())
    window.open_secret_action.trigger()

    for secret in ("plorbit", "은행", SECRET, MASTER):
        assert secret not in caplog.text


def test_the_relock_window_hears_a_click_in_the_note(
    qtbot: QtBot, manager: NoteManager, answers: Answers, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secret_notes, "MINUTE_MS", 100)
    window = secret_note(qtbot, manager, answers)
    viewport = (window.editor if window.editing else window.view).viewport()

    qtbot.wait(300)
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    qtbot.wait(300)

    assert not window.secret_locked


# In the settings


@pytest.fixture
def settings(qtbot: QtBot, manager: NoteManager, unlock: Unlock) -> SettingsWindow:
    window = SettingsWindow(manager, Translations(), master=application.master_passwords(unlock))
    qtbot.addWidget(window)
    return window


def test_the_settings_offer_a_new_password_once_they_are_on(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    settings: SettingsWindow,
    unlock: Unlock,
    connection: apsw.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.present(settings.protection)
    assert not settings.secret_password_button.isVisible()
    assert settings.relock_box.isVisible() and settings.relock_box.value() == 5
    assert not settings.leave_box.isChecked()
    window = secret_note(qtbot, manager, answers)
    changes = ProtectionChanges(Settings(connection))
    manager.secrets = application.secret_passwords(
        unlock, application.master_passwords(unlock), changes
    )
    settings.refresh()
    assert settings.secret_password_button.isVisible()
    assert "Secret notes' password: set" in settings.protection_dates.text()

    def change(dialog: QDialog) -> bool:
        if isinstance(dialog, OpenSecretDialog):
            assert dialog.purpose == "change"
            dialog.password.setText(SECRET)
        else:
            assert isinstance(dialog, NewSecretPasswordDialog)
            dialog.password.setText("a new one 22")
            dialog.confirm.setText("a new one 22")
        return accepted(dialog)

    monkeypatch.setattr(settings_window, "show_secret_dialog", change)
    settings.secret_password_button.click()

    assert unlock.opens_secret_notes("a new one 22")
    changes.due()
    assert [c.what for c in changes.to_tell()] == ["secret_password_changed"]
    assert not window.secret_locked  # what is open stays open


def test_the_relock_time_and_leaving_are_kept(
    qtbot: QtBot, manager: NoteManager, settings: SettingsWindow
) -> None:
    settings.relock_box.setValue(12)
    settings.leave_box.click()

    assert manager.relock_minutes == 12 and manager.lock_on_leave
    settings.refresh()
    assert settings.relock_box.value() == 12 and settings.leave_box.isChecked()


def test_forgotten_the_master_password_is_set_again_with_their_password(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    unlock: Unlock,
) -> None:
    secret_note(qtbot, manager, answers)
    key = unlock.open_secret_notes(SECRET)
    master = application.master_passwords(unlock)
    assert master is not None
    dialog = MasterPasswordDialog(master)
    qtbot.addWidget(dialog)

    dialog.forgot()  # with secret notes: their password, not the recovery key
    assert dialog.via_secret and dialog.current_label.text() == "&Secret notes' password:"
    dialog.current.setText("not their password")
    dialog.new.setText(OTHER)
    dialog.confirm.setText(OTHER)
    dialog.accept()
    assert dialog.message.text() == "The secret notes' password is not correct."
    dialog.current.setText(SECRET)
    dialog.new.setText(SECRET)
    dialog.confirm.setText(SECRET)
    dialog.accept()
    assert dialog.message.text() == (
        "Choose a master password other than the secret notes' password."
    )
    dialog.new.setText(OTHER)
    dialog.confirm.setText(OTHER)
    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted.value
    assert unlock.open_secret_notes_with_master(OTHER) == key  # opens them as before
    assert not unlock.is_master_password(MASTER)


def test_a_master_password_like_their_password_is_refused(
    qtbot: QtBot, manager: NoteManager, answers: Answers, unlock: Unlock
) -> None:
    secret_note(qtbot, manager, answers, first=True)
    unlock.change_secret_password(unlock.open_secret_notes(SECRET), OTHER)
    master = application.master_passwords(unlock)
    assert master is not None
    dialog = MasterPasswordDialog(master)
    qtbot.addWidget(dialog)
    dialog.current.setText(MASTER)
    dialog.new.setText(OTHER)
    dialog.confirm.setText(OTHER)

    dialog.accept()

    assert dialog.message.text() == (
        "Choose a master password other than the secret notes' password."
    )


# Locked by itself while one of its windows asks


def test_locked_while_asked_to_make_it_ordinary_its_text_stays(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
    unlock: Unlock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = secret_note(qtbot, manager, answers)

    def locked_meanwhile(_parent: object) -> bool:
        manager.lock_secret_notes()  # left alone too long, or the computer locked
        return True

    monkeypatch.setattr(notes, "confirm_ordinary", locked_meanwhile)
    window.ordinary_action.trigger()

    assert window.secret and window.secret_locked
    assert opened_text(connection, unlock, window) == TEXT  # not the empty window's text


def test_locked_while_its_title_is_changed_it_stays_locked(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
) -> None:
    window = secret_note(qtbot, manager, answers)

    def locked_meanwhile(dialog: QDialog) -> bool:
        manager.lock_secret_notes()
        return title("통장")(dialog)

    answers.queue.append(locked_meanwhile)
    window.secret_title_action.trigger()

    assert window.secret_locked and window.stack.currentWidget() is window.locked_face
    assert window.open_secret_action.isVisible()
    assert stored(connection, window).secret_title == "통장"


def test_enter_after_ticking_i_understand_turns_them_on(qtbot: QtBot, manager: NoteManager) -> None:
    # Some desktops (GNOME) have Enter tick a check box too: ticked with Space,
    # it was unticked again instead of turning them on.
    assert manager.secrets is not None
    dialog = TurnOnDialog(manager.secrets)
    qtbot.addWidget(dialog)
    for field in (dialog.password, dialog.confirm):
        field.setText(SECRET)
    for field in (dialog.master, dialog.master_confirm):
        field.setText(MASTER)
    dialog.show()
    qtbot.waitExposed(dialog)
    dialog.understood.setFocus()

    QTest.keyClick(dialog.understood, Qt.Key.Key_Space)
    QTest.keyClick(dialog.understood, Qt.Key.Key_Return)

    assert dialog.understood.isChecked()
    qtbot.waitUntil(lambda: dialog.result() == QDialog.DialogCode.Accepted.value)
    assert dialog.key is not None
