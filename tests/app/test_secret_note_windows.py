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
from PySide6.QtWidgets import QDialog, QMessageBox, QWidget
from pytestqt.qtbot import QtBot
from test_autosave import compose
from test_startup_dialogs import FAST, Backend, store_of

from stickle.app import (
    application,
    data_menu,
    file_dialogs,
    master_password,
    notes,
    secret_dialogs,
    secret_notes,
    settings_window,
)
from stickle.app.i18n import Translations
from stickle.app.master_password import MasterPasswordDialog
from stickle.app.note_list import ANY, NoteList
from stickle.app.note_list import SECRET as SECRET_FILTER
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NO_CATEGORY, SECRET_NOTES, NoteManager
from stickle.app.protection_changes import ProtectionChanges
from stickle.app.recovery_key_dialog import EnterRecoveryKeyDialog, RecoveryKeyDialog
from stickle.app.secret_dialogs import (
    NewSecretPasswordDialog,
    OpenSecretDialog,
    SecretExportDialog,
    SecretTitleDialog,
    TurnOnDialog,
)
from stickle.app.settings_window import SettingsWindow
from stickle.app.times import local_time
from stickle.app.titles import shown_title
from stickle.app.tray import menu_title
from stickle.core.note import Note
from stickle.crypto.key_files import B, C
from stickle.crypto.keyfile import encode_slot, wrap_with_password
from stickle.crypto.recovery import (
    OLD_SLOT,
    WrongRecoveryKeyError,
    canonical,
    generate,
    read_recovery,
)
from stickle.crypto.sealed import SealedSlot
from stickle.crypto.secret_notes import keep_for_recovery_key, open_text, seal_text
from stickle.data.export import ExportResult
from stickle.data.labels import LabelRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.search import search
from stickle.data.settings import RECOVERY_KEY_KEPT, Settings
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


def turn_on(password: str = SECRET) -> Answer:
    def answer(dialog: QDialog) -> bool:
        assert isinstance(dialog, TurnOnDialog)
        dialog.password.setText(password)
        dialog.confirm.setText(password)
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
    manager = NoteManager(
        NoteRepository(connection),
        idle_ms=50,
        settings=Settings(connection),
        labels=LabelRepository(connection),
    )
    Settings(connection).set(RECOVERY_KEY_KEPT, True)  # its recovery key, kept at the first start
    manager.secrets = application.secret_passwords(
        unlock,
        application.master_passwords(unlock),
        recovery=application.recovery_keys(unlock, Settings(connection)),
        fits=manager.secret_key_fits,
    )
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

    assert unlock.has_secret_notes and not unlock.has_master_password  # not asked for
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


def test_their_key_file_gone_while_running_the_next_one_still_asks_their_password(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
    unlock: Unlock,
) -> None:
    # Taken away by a clean-up tool: once, turning them on again put a new key
    # over the other copies, and the notes made before opened nowhere.
    first = secret_note(qtbot, manager, answers)
    unlock.files.path(C).unlink()

    second = secret_note(qtbot, manager, answers, "두 번째", first=False)

    assert isinstance(answers.asked[-2], OpenSecretDialog)
    assert opened_text(connection, unlock, first) == TEXT
    assert opened_text(connection, unlock, second) == "두 번째"


def test_with_every_copy_of_their_key_gone_they_are_not_turned_on_anew(
    qtbot: QtBot, manager: NoteManager, answers: Answers, unlock: Unlock
) -> None:
    # A key made anew would follow into the database and, written over the copy
    # there, leave nothing a backup could bring back.
    secret_note(qtbot, manager, answers)
    unlock.files.path(C).unlink()
    unlock.files.second_path(C).unlink()
    other = manager.new_note()
    other.editor.insertPlainText("다른 메모")
    answers.queue.append(cancel)

    other.secret_action.trigger()

    assert isinstance(answers.asked[-1], OpenSecretDialog)
    assert not unlock.files.path(C).exists()


def test_its_old_text_not_cleared_away_it_is_made_secret_all_the_same(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    connection: apsw.Connection,
    unlock: Unlock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Once, a failure clearing the search index after the change said the note
    # was not made secret, while it was: its later edits were then refused.
    def fails(_self: NoteRepository) -> None:
        raise apsw.FullError("disk full")

    monkeypatch.setattr(NoteRepository, "forget_old_text", fails)
    warned: list[str] = []

    def warning(_parent: QWidget, _title: str, text: str) -> None:
        warned.append(text)

    monkeypatch.setattr(notes.QMessageBox, "warning", warning)

    window = secret_note(qtbot, manager, answers)
    type_into(window, " 더")

    assert manager.save(window) and not window.unsaved
    assert opened_text(connection, unlock, window) == TEXT + " 더"
    assert warned == []


def test_a_window_out_of_step_with_its_note_is_marked_and_the_others_still_saved(
    qtbot: QtBot, manager: NoteManager, connection: apsw.Connection
) -> None:
    # A note made secret while its window still took it for an ordinary one:
    # its text refused, which once stopped the others being saved on quitting.
    out_of_step, other = manager.new_note(), manager.new_note()
    out_of_step.editor.insertPlainText("하나")
    other.editor.insertPlainText("둘")
    manager.save_all()
    assert out_of_step.note_id is not None
    sealed = seal_text(KEY, out_of_step.note_id, "하나")
    NoteRepository(connection).make_secret(out_of_step.note_id, "t", sealed)
    type_into(out_of_step, " 더")
    type_into(other, " 더")

    manager.prepare_to_quit()

    assert out_of_step.unsaved
    assert stored(connection, other).body == "둘 더"


def test_a_key_file_that_cannot_be_read_counts_as_there(
    unlock: Unlock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(_name: str) -> bytes | None:
        raise PermissionError

    monkeypatch.setattr(unlock.files, "raw", unreadable)

    assert unlock.has_secret_notes


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
    assert window.locked_face.accessibleName() == "Secret note, covered: 은행"


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
    other = QWidget()  # another app's window, in front now
    qtbot.addWidget(other)
    other.show()
    other.activateWindow()
    qtbot.waitUntil(lambda: not window.isActiveWindow())

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


# Their password, and the recovery key when it is forgotten


def new_password(typed: str) -> Callable[[NewSecretPasswordDialog], bool]:
    def answer(dialog: NewSecretPasswordDialog) -> bool:
        dialog.password.setText(typed)
        dialog.confirm.setText(typed)
        return accepted(dialog)

    return answer


def recovery_key_typed(*typed: str) -> Callable[[EnterRecoveryKeyDialog], bool]:
    """Each in turn, until one is taken; given up (Back) if none is."""

    def answer(dialog: EnterRecoveryKeyDialog) -> bool:
        for key in typed:
            dialog.key.setText(key)
            if accepted(dialog):
                return True
        dialog.reject()
        return False

    return answer


def with_recovery_key(manager: NoteManager, unlock: Unlock, connection: apsw.Connection) -> None:
    """Their windows told of changes, as Stickle has them."""
    manager.secrets = application.secret_passwords(
        unlock,
        application.master_passwords(unlock),
        ProtectionChanges(Settings(connection)),
        application.recovery_keys(unlock, Settings(connection)),
        manager.secret_key_fits,
    )


def always(_window: QWidget) -> bool:
    return True


def yes(*_args: object) -> QMessageBox.StandardButton:
    return QMessageBox.StandardButton.Yes


def forgot(dialog: QDialog) -> bool:
    assert isinstance(dialog, OpenSecretDialog)
    dialog.forgot_button.click()
    return dialog.result() == QDialog.DialogCode.Accepted.value


def test_forgotten_the_recovery_key_opens_it_and_a_new_one_is_set(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    unlock: Unlock,
    connection: apsw.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery_key = unlock.make_recovery_key(KEY)
    with_recovery_key(manager, unlock, connection)
    window = secret_note(qtbot, manager, answers)
    manager.lock_secret(window)
    typed: list[str] = []

    def seen(dialog: EnterRecoveryKeyDialog) -> bool:
        typed.append(dialog.windowTitle())
        return recovery_key_typed("0000-0000", generate(), recovery_key)(dialog)

    monkeypatch.setattr(secret_dialogs, "ask_recovery_key", seen)
    monkeypatch.setattr(secret_dialogs, "ask_new_password", new_password("a new one 22"))
    answers.queue.append(forgot)
    window.open_secret_action.trigger()

    assert window.text == TEXT
    assert unlock.opens_secret_notes("a new one 22")
    assert not unlock.opens_secret_notes(SECRET)  # the old one no longer
    assert unlock.open_secret_notes_with_recovery_key(recovery_key) is not None
    changes = ProtectionChanges(Settings(connection))
    changes.due()
    assert [c.what for c in changes.to_tell()] == ["secret_password_reset"]


def test_a_wrong_recovery_key_or_none_set_changes_nothing(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    unlock: Unlock,
    connection: apsw.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery_key = unlock.make_recovery_key(KEY)
    with_recovery_key(manager, unlock, connection)
    window = secret_note(qtbot, manager, answers)
    manager.lock_secret(window)
    shown: list[str] = []

    def wrong_then_back(dialog: EnterRecoveryKeyDialog) -> bool:
        for key in ("0000", generate()):
            dialog.key.setText(key)
            dialog.accept()
            shown.append(dialog.error.text())
        dialog.reject()
        return False

    monkeypatch.setattr(secret_dialogs, "ask_recovery_key", wrong_then_back)
    answers.queue.append(forgot)
    window.open_secret_action.trigger()
    assert window.secret_locked
    assert shown[1] == "This recovery key does not open these notes."
    assert "mistyped" in shown[0]

    # The right one, but no new password set: still locked, nothing changed.
    monkeypatch.setattr(secret_dialogs, "ask_recovery_key", recovery_key_typed(recovery_key))
    monkeypatch.setattr(secret_dialogs, "ask_new_password", cancel)
    answers.queue.append(forgot)
    window.open_secret_action.trigger()

    assert window.secret_locked
    assert unlock.opens_secret_notes(SECRET)


def test_a_recovery_key_whose_key_opens_none_of_them_is_refused(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    unlock: Unlock,
    connection: apsw.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A key that opens no secret note, sealed for the recovery key somehow (a
    # key file from elsewhere): never taken, nor kept for their password.
    recovery_key = unlock.make_recovery_key(KEY)
    with_recovery_key(manager, unlock, connection)
    window = secret_note(qtbot, manager, answers)
    manager.lock_secret(window)
    pair = read_recovery(unlock.files)
    assert isinstance(pair, SealedSlot)
    keep_for_recovery_key(unlock.files, secrets.token_bytes(32), pair.public)
    shown: list[str] = []

    def typed(dialog: EnterRecoveryKeyDialog) -> bool:
        dialog.key.setText(recovery_key)
        dialog.accept()
        shown.append(dialog.error.text())
        dialog.reject()
        return False

    monkeypatch.setattr(secret_dialogs, "ask_recovery_key", typed)
    answers.queue.append(forgot)
    window.open_secret_action.trigger()

    assert shown == ["This recovery key does not open the secret notes."]
    assert window.secret_locked and unlock.opens_secret_notes(SECRET)


def test_opened_with_their_password_they_are_kept_for_the_recovery_key_again(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    unlock: Unlock,
    connection: apsw.Connection,
) -> None:
    # A recovery key made without their key (Stickle stopped in between, or an
    # earlier build): the next time their password opens them, it opens them too.
    with_recovery_key(manager, unlock, connection)
    window = secret_note(qtbot, manager, answers)
    manager.lock_secret(window)
    recovery_key = unlock.make_recovery_key(KEY)
    assert not unlock.secret_notes_kept_for_recovery_key

    answers.queue.append(password())
    window.open_secret_action.trigger()

    assert unlock.secret_notes_kept_for_recovery_key
    assert unlock.open_secret_notes_with_recovery_key(recovery_key) == unlock.open_secret_notes(
        SECRET
    )


def test_turning_on_asks_one_password_and_says_when_the_recovery_key_was_made(
    qtbot: QtBot, manager: NoteManager, unlock: Unlock, connection: apsw.Connection
) -> None:
    Settings(connection).set(RECOVERY_KEY_KEPT, True)
    assert manager.secrets is not None
    dialog = TurnOnDialog(manager.secrets)
    qtbot.addWidget(dialog)

    made = unlock.recovery_key_made_at
    assert made is not None
    assert local_time(made) in dialog.recovery_line.text()
    assert "do not keep it on this computer" in dialog.recovery_line.text()
    assert dialog.ok_button.isEnabled() and not dialog.type_button.isVisibleTo(dialog)
    assert dialog.make_button.text() == "&Make a new recovery key…"
    for field in (dialog.password, dialog.confirm):
        field.setText(SECRET)
    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted.value
    assert unlock.has_secret_notes and not unlock.has_master_password


def test_not_turned_on_until_a_recovery_key_is_kept(
    qtbot: QtBot,
    manager: NoteManager,
    unlock: Unlock,
    connection: apsw.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Skipped at the first start ("Later"): the user may not have it at all, and
    # a forgotten password would lock the secret notes away for good.
    Settings(connection).set(RECOVERY_KEY_KEPT, False)
    assert manager.secrets is not None
    dialog = TurnOnDialog(manager.secrets)
    qtbot.addWidget(dialog)
    assert "You have not kept a recovery key yet." in dialog.recovery_line.text()
    assert not dialog.ok_button.isEnabled()
    for field in (dialog.password, dialog.confirm):
        field.setText(SECRET)
    dialog.accept()  # Enter, with the button off
    assert dialog.key is None and not unlock.has_secret_notes
    shown: list[str] = []

    def later(window: RecoveryKeyDialog) -> bool:
        shown.append(window.panel.recovery_key)
        return False

    monkeypatch.setattr(secret_dialogs, "confirm_new_recovery_key", always)
    monkeypatch.setattr(secret_dialogs, "show_recovery_key", later)
    dialog.make_button.click()
    assert not dialog.ok_button.isEnabled()  # made, but "Later" again

    def keep(window: RecoveryKeyDialog) -> bool:
        shown.append(window.panel.recovery_key)
        return True

    monkeypatch.setattr(secret_dialogs, "show_recovery_key", keep)
    dialog.make_button.click()

    assert dialog.ok_button.isEnabled()
    assert "You have not kept" not in dialog.recovery_line.text()
    assert Settings(connection).get(RECOVERY_KEY_KEPT)
    for field in (dialog.password, dialog.confirm):
        field.setText(SECRET)
    dialog.accept()
    assert dialog.key is not None
    assert unlock.open_secret_notes_with_recovery_key(shown[-1]) == dialog.key


def test_an_old_recovery_key_is_typed_once_before_turning_on(
    qtbot: QtBot,
    manager: NoteManager,
    unlock: Unlock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = generate()
    wrapped = wrap_with_password(KEY, canonical(old), 2, 8192, slot_name=OLD_SLOT)
    unlock.files.save(B, {OLD_SLOT: encode_slot(wrapped)})
    assert manager.secrets is not None
    dialog = TurnOnDialog(manager.secrets)
    qtbot.addWidget(dialog)

    assert "earlier Stickle" in dialog.recovery_line.text()
    assert not dialog.ok_button.isEnabled() and dialog.type_button.isVisibleTo(dialog)
    monkeypatch.setattr(secret_dialogs, "ask_recovery_key", recovery_key_typed(generate(), old))
    dialog.type_button.click()

    assert dialog.ok_button.isEnabled() and not dialog.type_button.isVisibleTo(dialog)
    for field in (dialog.password, dialog.confirm):
        field.setText(SECRET)
    dialog.accept()
    assert dialog.key is not None
    assert unlock.open_secret_notes_with_recovery_key(old) == dialog.key
    assert unlock.open_with_recovery_key(old) == KEY


def test_without_a_recovery_key_one_is_made_before_turning_on(
    qtbot: QtBot,
    manager: NoteManager,
    unlock: Unlock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unlock.files.path(B).unlink()
    unlock.files.second_path(B).unlink()
    assert manager.secrets is not None
    dialog = TurnOnDialog(manager.secrets)
    qtbot.addWidget(dialog)
    assert not dialog.ok_button.isEnabled()
    assert dialog.make_button.text() == "&Make a recovery key…"
    shown: list[str] = []

    def keep(window: RecoveryKeyDialog) -> bool:
        shown.append(window.panel.recovery_key)
        return True

    monkeypatch.setattr(secret_dialogs, "show_recovery_key", keep)
    dialog.make_button.click()  # none to replace: not asked first

    assert dialog.ok_button.isEnabled()
    for field in (dialog.password, dialog.confirm):
        field.setText(SECRET)
    dialog.accept()
    assert dialog.key is not None
    assert unlock.open_secret_notes_with_recovery_key(shown[0]) == dialog.key


def test_a_short_password_is_refused(
    qtbot: QtBot, manager: NoteManager, answers: Answers, unlock: Unlock
) -> None:
    window = manager.new_note()
    window.editor.insertPlainText(TEXT)
    refused: list[str] = []

    def refuse(dialog: QDialog) -> bool:
        if not turn_on("short")(dialog):
            assert isinstance(dialog, TurnOnDialog)
            refused.append(dialog.message.text())
        return False

    answers.queue.append(refuse)
    window.secret_action.trigger()

    assert refused == ["Use at least 8 characters."]
    assert not window.secret and not unlock.has_secret_notes


@pytest.mark.parametrize(
    ("typed", "reason"),
    [
        ("password1", "That is one of the most common passwords. Choose another."),
        ("1q2w3e4r", "That is one of the most common passwords. Choose another."),
        ("abababab", "Repeated characters or a run of the keyboard are easy to guess."),
    ],
)
def test_a_guessable_password_is_refused_with_why(
    qtbot: QtBot, manager: NoteManager, answers: Answers, unlock: Unlock, typed: str, reason: str
) -> None:
    window = manager.new_note()
    refused: list[str] = []
    rules: list[str] = []

    def refuse(dialog: QDialog) -> bool:
        assert isinstance(dialog, TurnOnDialog)
        rules.append(dialog.rules.text())
        if not turn_on(typed)(dialog):
            refused.append(dialog.message.text())
        return False

    answers.queue.append(refuse)
    window.secret_action.trigger()

    assert rules == ["At least 8 characters, and not a common password."]
    assert len(refused) == 1 and refused[0].startswith(reason)
    assert not window.secret and not unlock.has_secret_notes


def test_their_password_cannot_be_the_master_password(
    qtbot: QtBot, manager: NoteManager, answers: Answers, unlock: Unlock
) -> None:
    unlock.set_master_password(KEY, MASTER)  # set before, in the settings
    window = manager.new_note()
    refused: list[str] = []

    def refuse(dialog: QDialog) -> bool:
        assert isinstance(dialog, TurnOnDialog)
        if not turn_on(MASTER)(dialog):
            refused.append(dialog.message.text())
        return False

    answers.queue.append(refuse)
    window.secret_action.trigger()

    assert refused == ["Choose a password other than the spare password."]


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
def settings(
    qtbot: QtBot, manager: NoteManager, unlock: Unlock, connection: apsw.Connection
) -> SettingsWindow:
    window = SettingsWindow(
        manager,
        Translations(),
        recovery=application.recovery_keys(
            unlock, Settings(connection), fits=manager.secret_key_fits
        ),
        master=application.master_passwords(unlock),
    )
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
    with_recovery_key(manager, unlock, connection)
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
    changes = ProtectionChanges(Settings(connection))
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


def test_a_new_recovery_key_asks_their_password_and_opens_them_too(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    settings: SettingsWindow,
    unlock: Unlock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = unlock.make_recovery_key(KEY)
    secret_note(qtbot, manager, answers)
    key = unlock.open_secret_notes(SECRET)
    shown: list[str] = []
    asked: list[str] = []

    def their_password(dialog: QDialog) -> bool:
        assert isinstance(dialog, OpenSecretDialog)
        asked.append(dialog.purpose)
        dialog.password.setText(SECRET)
        return accepted(dialog)

    def keep(window: RecoveryKeyDialog) -> bool:
        shown.append(window.panel.recovery_key)
        return True

    monkeypatch.setattr(settings_window.QMessageBox, "question", yes)
    monkeypatch.setattr(settings_window, "show_secret_dialog", their_password)
    monkeypatch.setattr(settings_window, "show_recovery_key", keep)
    settings.recovery_button.click()

    assert asked == ["recovery"]
    assert unlock.open_secret_notes_with_recovery_key(shown[0]) == key
    assert unlock.open_with_recovery_key(shown[0]) == KEY
    with pytest.raises(WrongRecoveryKeyError):
        unlock.open_secret_notes_with_recovery_key(first)


def test_without_their_password_no_new_recovery_key_is_made(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    settings: SettingsWindow,
    unlock: Unlock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Made anyway, the one their key is sealed for would stop working, and the
    # new one would not open them.
    first = unlock.make_recovery_key(KEY)
    secret_note(qtbot, manager, answers)
    monkeypatch.setattr(settings_window.QMessageBox, "question", yes)
    monkeypatch.setattr(settings_window, "show_secret_dialog", cancel)
    monkeypatch.setattr(settings_window, "show_recovery_key", always)

    settings.recovery_button.click()

    assert unlock.open_secret_notes_with_recovery_key(first) == unlock.open_secret_notes(SECRET)


def test_forgotten_the_master_password_the_recovery_key_sets_it_and_they_stay(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    unlock: Unlock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Taken off the secret notes, the master password is set again the way it
    # always is, and nothing of theirs changes.
    recovery_key = unlock.make_recovery_key(KEY)
    unlock.set_master_password(KEY, MASTER)
    secret_note(qtbot, manager, answers)
    key = unlock.open_secret_notes(SECRET)
    master = application.master_passwords(unlock)
    assert master is not None
    dialog = MasterPasswordDialog(master)
    qtbot.addWidget(dialog)
    monkeypatch.setattr(master_password, "ask_recovery_key", recovery_key_typed(recovery_key))

    dialog.forgot()
    dialog.new.setText(OTHER)
    dialog.confirm.setText(OTHER)
    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted.value
    assert unlock.is_master_password(OTHER)
    assert unlock.open_secret_notes(SECRET) == key
    assert unlock.open_secret_notes_with_recovery_key(recovery_key) == key


def test_a_master_password_like_their_password_is_refused(
    qtbot: QtBot, manager: NoteManager, answers: Answers, unlock: Unlock
) -> None:
    unlock.set_master_password(KEY, MASTER)
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
        "Choose a spare password other than the secret notes' password."
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


def test_enter_in_the_password_turns_them_on(qtbot: QtBot, manager: NoteManager) -> None:
    assert manager.secrets is not None
    dialog = TurnOnDialog(manager.secrets)
    qtbot.addWidget(dialog)
    for field in (dialog.password, dialog.confirm):
        field.setText(SECRET)
    dialog.show()
    qtbot.waitExposed(dialog)
    dialog.confirm.setFocus()

    QTest.keyClick(dialog.confirm, Qt.Key.Key_Return)

    qtbot.waitUntil(lambda: dialog.result() == QDialog.DialogCode.Accepted.value)
    assert dialog.key is not None


# How it looks, and where it is listed: by its title only


def listed_texts(note_list: NoteList) -> list[str]:
    return [note_list.list.item(row).text() for row in range(note_list.list.count())]


def test_it_shows_a_keyhole_and_not_its_category_or_marks(
    qtbot: QtBot, manager: NoteManager, answers: Answers
) -> None:
    work = manager.create_category("회사", "blue")
    urgent = manager.create_mark("급함", "star")
    window = manager.new_note()
    window.editor.insertPlainText(TEXT)
    manager.save(window)
    manager.set_category(window, work.id)
    manager.set_mark(window, urgent.id, True)
    qtbot.waitExposed(window)
    tag, badge = window.title_bar.category_tag, window.title_bar.marks_badge
    assert badge.isVisible()
    answers.queue += [turn_on(), title("은행")]

    window.secret_action.trigger()

    assert tag.secret == "open"
    assert tag.accessibleName() == "Secret note, shown: it is covered again by itself, stays on top"
    assert not badge.isVisible()
    assert not window.category_menu.menuAction().isVisible()
    manager.lock_secret(window)
    assert tag.secret == "locked"
    assert tag.accessibleName() == "Secret note, covered, stays on top"
    assert window.category is not None and window.category.id == work.id  # kept for later


def test_lists_and_menus_show_its_title_only(
    qtbot: QtBot, manager: NoteManager, answers: Answers, connection: apsw.Connection
) -> None:
    window = secret_note(qtbot, manager, answers)
    note = stored(connection, window)
    note_list = NoteList(manager)
    qtbot.addWidget(note_list)

    note_list.refresh()

    assert any(text.startswith("은행 · secret note") for text in listed_texts(note_list))
    assert not any("plorbit" in text for text in listed_texts(note_list))
    assert menu_title(note) == "은행"
    assert shown_title(note) == "은행"


def test_it_is_found_by_its_title_never_its_text(
    qtbot: QtBot, manager: NoteManager, answers: Answers, connection: apsw.Connection
) -> None:
    window = secret_note(qtbot, manager, answers)

    assert manager.matching("은행") == {window.note_id}
    assert manager.matching("plorbit") == set()  # open, still not searched


def test_category_views_leave_it_out_and_the_secret_view_gathers_them(
    qtbot: QtBot, manager: NoteManager, answers: Answers
) -> None:
    work = manager.create_category("회사", "blue")
    ordinary = manager.new_note()
    ordinary.editor.insertPlainText("회의")
    manager.save(ordinary)
    manager.set_category(ordinary, work.id)
    assert [view for view, _ in manager.view_choices()] == [None, NO_CATEGORY, work.id]
    window = secret_note(qtbot, manager, answers)
    manager.set_category(window, work.id)  # kept, while secret, for later

    assert manager.view_choices()[-1] == (SECRET_NOTES, "Secret notes")
    manager.set_view(work.id)
    assert ordinary.isVisible() and not window.isVisible()
    manager.next_view()
    assert manager.view == SECRET_NOTES
    assert window.isVisible() and not ordinary.isVisible()
    made = manager.new_note()  # while the secret notes are in view: ordinary, no category
    assert made.category is None and made.isVisible() and not made.secret
    manager.next_view()
    assert manager.view is None and ordinary.isVisible() and window.isVisible()


def test_made_secret_while_its_category_is_in_view_it_stays_until_the_view_ends(
    qtbot: QtBot, manager: NoteManager, answers: Answers
) -> None:
    work = manager.create_category("회사", "blue")
    window = manager.new_note()
    window.editor.insertPlainText(TEXT)
    manager.save(window)
    manager.set_category(window, work.id)
    manager.set_view(work.id)
    answers.queue += [turn_on(), title("은행")]

    window.secret_action.trigger()

    assert window.secret and window.isVisible()
    manager.set_view(work.id)  # the view chosen again: now left out
    assert not window.isVisible()


def test_the_list_gathers_them_apart_from_categories_and_marks(
    qtbot: QtBot, manager: NoteManager, answers: Answers
) -> None:
    work = manager.create_category("회사", "blue")
    window = secret_note(qtbot, manager, answers)
    manager.set_category(window, work.id)
    plain = manager.new_note()
    plain.editor.insertPlainText("회의")
    manager.save(plain)
    manager.set_category(plain, work.id)
    note_list = NoteList(manager)
    qtbot.addWidget(note_list)
    note_list.refresh()
    box = note_list.category_box

    last = box.count() - 1
    assert box.itemData(last) == SECRET_FILTER and box.itemText(last) == "Secret notes"
    box.setCurrentIndex(box.findData(work.id))
    assert [text.split(" · ")[0] for text in listed_texts(note_list)] == ["회의"]
    box.setCurrentIndex(box.findData(SECRET_FILTER))
    assert [text.split(" · ")[0] for text in listed_texts(note_list)] == ["은행"]
    box.setCurrentIndex(box.findData(ANY))
    assert len(listed_texts(note_list)) == 2


def test_without_secret_notes_there_is_no_secret_view(manager: NoteManager) -> None:
    assert SECRET_NOTES not in [view for view, _ in manager.view_choices()]
    manager.set_view(SECRET_NOTES)
    assert manager.view is None


# Exported with the rest: encrypted as they are, or in the clear


exporting_told: list[str] = []  # what the last export told the user


@pytest.fixture
def exporting(
    qtbot: QtBot,
    manager: NoteManager,
    connection: apsw.Connection,
    unlock: Unlock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], ExportResult | None]:
    out = tmp_path / "out"
    out.mkdir()

    def existing_folder(_parent: QWidget, _title: str) -> Path:
        return out

    def tell_exported(_parent: QWidget, _title: str, text: str, _folder: Path) -> None:
        exporting_told.append(text)

    def tell_failed(_parent: QWidget, _title: str, text: str) -> None:
        exporting_told.append(text)

    monkeypatch.setattr(file_dialogs, "existing_folder", existing_folder)
    monkeypatch.setattr(data_menu, "tell_exported", tell_exported)
    monkeypatch.setattr(data_menu, "tell_failed", tell_failed)
    parent = QWidget()
    qtbot.addWidget(parent)
    access = application.data_access(
        manager, connection, tmp_path / "notes.db", KEY, lambda: None, unlock.secret_key_copies
    )
    exporting_told.clear()
    return lambda: data_menu.export_everything(parent, access)


def export_answer(monkeypatch: pytest.MonkeyPatch, answer: Callable[[QDialog], bool]) -> None:
    asked: list[QDialog] = []

    def ask(dialog: QDialog) -> bool:
        asked.append(dialog)
        return answer(dialog)

    monkeypatch.setattr(data_menu, "ask_secret_export", ask)


def test_exported_left_encrypted_as_chosen_first(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    exporting: Callable[[], ExportResult | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = secret_note(qtbot, manager, answers)
    seen: list[str] = []

    def keep_encrypted(dialog: QDialog) -> bool:
        assert isinstance(dialog, SecretExportDialog) and dialog.encrypted.isChecked()
        seen.append(dialog.intro.text())
        return accepted(dialog)

    export_answer(monkeypatch, keep_encrypted)
    result = exporting()

    assert seen == ["How should the 1 secret note(s) be exported?"]
    assert result is not None and result.still_encrypted == 1
    written = "".join(p.read_text(encoding="utf-8") for p in result.folder.rglob("*.*"))
    assert "plorbit" not in written and "Encrypted secret note" in written
    assert "secret_notes_key" in written  # their key, locked, goes along
    assert "1 secret note(s) stayed encrypted" in exporting_told[0]
    assert window.text == TEXT  # still open, as it was


def test_exported_in_the_clear_once_their_password_is_typed(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    exporting: Callable[[], ExportResult | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_note(qtbot, manager, answers)
    refused: list[str] = []

    def in_the_clear(dialog: QDialog) -> bool:
        assert isinstance(dialog, SecretExportDialog)
        dialog.plain.setChecked(True)
        assert dialog.password.isEnabled()
        dialog.password.setText("not their password")
        if not accepted(dialog):
            refused.append(dialog.message.text())
        dialog.password.setText(SECRET)
        return accepted(dialog)

    export_answer(monkeypatch, in_the_clear)
    result = exporting()

    assert refused == ["The password is not correct."]
    assert result is not None and result.still_encrypted == 0
    texts = [p.read_text(encoding="utf-8") for p in result.folder.glob("*.md")]
    assert TEXT in texts
    assert "stayed encrypted" not in exporting_told[0]


def test_cancelled_nothing_is_exported_nor_a_folder_asked(
    qtbot: QtBot,
    manager: NoteManager,
    answers: Answers,
    exporting: Callable[[], ExportResult | None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_note(qtbot, manager, answers)
    export_answer(monkeypatch, cancel)

    assert exporting() is None
    assert list((tmp_path / "out").iterdir()) == []


def test_without_secret_notes_nothing_is_asked(
    qtbot: QtBot,
    manager: NoteManager,
    exporting: Callable[[], ExportResult | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = manager.new_note()
    window.editor.insertPlainText("장보기")

    def never(_dialog: QDialog) -> bool:
        raise AssertionError("asked")

    export_answer(monkeypatch, never)
    result = exporting()

    assert result is not None and result.notes == 1 and result.still_encrypted == 0


def test_enter_on_a_choice_goes_on_to_next(qtbot: QtBot) -> None:
    # Some desktops (GNOME) have Enter press the focused button instead.
    dialog = SecretExportDialog(2, lambda _password: b"")
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    dialog.encrypted.setFocus()

    QTest.keyClick(dialog.encrypted, Qt.Key.Key_Return)

    assert dialog.result() == QDialog.DialogCode.Accepted.value and dialog.key is None
