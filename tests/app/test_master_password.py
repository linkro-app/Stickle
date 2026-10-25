"""Setting, changing and checking the master password, and opening notes with it."""

from collections.abc import Callable
from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialog
from pytestqt.qtbot import QtBot
from test_startup_dialogs import FAST, PASSWORD, Backend, Recovery, never, store_of, type_and_accept

from stickle.app import first_run, master_password, settings_window
from stickle.app.first_run import FirstRunDialog
from stickle.app.i18n import Translations
from stickle.app.master_password import (
    CheckMasterPasswordDialog,
    MasterPasswordDialog,
    MasterPasswords,
)
from stickle.app.notes import NoteManager
from stickle.app.password_dialog import PasswordDialog
from stickle.app.recovery_dialog import Choice, RecoveryDialog
from stickle.app.recovery_key_dialog import EnterRecoveryKeyDialog
from stickle.app.settings_window import SettingsWindow
from stickle.app.startup import open_notes
from stickle.crypto.keyfile import WrongPasswordError
from stickle.crypto.recovery import RecoveryKeyTypoError
from stickle.data.notes import NoteRepository
from stickle.unlock import Unlock

GOOD = "correct horse battery staple"
OTHER = "another phrase to remember well"


class Master:
    """The master password, kept in memory."""

    def __init__(self, current: str | None = None, notes_password: str = PASSWORD) -> None:
        self.current = current
        self.notes_password = notes_password
        self.recovery_key = "K7QM-2HXP-9RTV-4WJD-8FNB-3YCS-6GKA-M2Q0"

    def change(self, current: str, new: str) -> None:
        if current != self.current:
            raise WrongPasswordError
        self.current = new

    def set(self, new: str) -> None:
        self.current = new

    def recovery_key_opens(self, typed: str) -> bool:
        if len(typed) < 10:
            raise RecoveryKeyTypoError
        return typed == self.recovery_key

    def passwords(self) -> MasterPasswords:
        return MasterPasswords(
            exists=lambda: self.current is not None,
            set=self.set,
            change=self.change,
            check=lambda typed: typed == self.current,
            opens_notes=lambda typed: typed == self.notes_password,
            recovery_key_opens=self.recovery_key_opens,
        )


def fill(
    dialog: MasterPasswordDialog, new: str, confirm: str | None = None, current: str = ""
) -> None:
    dialog.current.setText(current)
    dialog.new.setText(new)
    dialog.confirm.setText(new if confirm is None else confirm)
    dialog.accept()


@pytest.mark.parametrize(
    ("new", "confirm", "said"),
    [
        ("short", None, "12 characters"),
        ("password1234", None, "most common"),
        ("aaaaaaaaaaaa", None, "Repeated"),
        (GOOD, "something else entirely", "not the same"),
        (PASSWORD + " and more", None, None),  # longer than the notes' password: fine
    ],
)
def test_a_password_easy_to_guess_is_refused_with_the_reason(
    qtbot: QtBot, new: str, confirm: str | None, said: str | None
) -> None:
    master = Master()
    dialog = MasterPasswordDialog(master.passwords())
    qtbot.addWidget(dialog)

    fill(dialog, new, confirm)

    if said is None:
        assert master.current == new
        assert dialog.result() == QDialog.DialogCode.Accepted
    else:
        assert master.current is None
        assert said in dialog.message.text()
        assert dialog.new.accessibleDescription() == dialog.message.text()


def test_the_password_that_opens_the_notes_is_refused(qtbot: QtBot) -> None:
    master = Master(notes_password=GOOD)
    dialog = MasterPasswordDialog(master.passwords())
    qtbot.addWidget(dialog)

    fill(dialog, GOOD)

    assert master.current is None
    assert "opens your notes" in dialog.message.text()


def test_changing_it_asks_for_the_current_one(qtbot: QtBot) -> None:
    master = Master(current=GOOD)
    dialog = MasterPasswordDialog(master.passwords())
    qtbot.addWidget(dialog)
    assert dialog.changing and dialog.current.isVisibleTo(dialog)

    fill(dialog, OTHER, current="not it at all, sorry")
    assert master.current == GOOD
    assert "not correct" in dialog.message.text()

    fill(dialog, OTHER, current=GOOD)
    assert master.current == OTHER


def test_forgotten_it_is_set_anew_after_the_recovery_key(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    master = Master(current=GOOD)
    asked: list[str] = []

    def enter_key(dialog: EnterRecoveryKeyDialog) -> bool:
        dialog.key.setText("short")
        dialog.accept()  # mistyped: said so, the window stays
        asked.append(dialog.error.text())
        dialog.key.setText(master.recovery_key)
        dialog.accept()
        return dialog.result() == QDialog.DialogCode.Accepted

    monkeypatch.setattr(master_password, "ask_recovery_key", enter_key)
    dialog = MasterPasswordDialog(master.passwords())
    qtbot.addWidget(dialog)

    dialog.forgot()
    assert asked and asked[0]
    assert not dialog.changing and not dialog.current.isVisibleTo(dialog)
    fill(dialog, OTHER)

    assert master.current == OTHER


def test_checking_it_says_whether_it_is_and_changes_nothing(qtbot: QtBot) -> None:
    master = Master(current=GOOD)
    dialog = CheckMasterPasswordDialog(master.passwords())
    qtbot.addWidget(dialog)

    dialog.password.setText("not it")
    dialog.check()
    assert dialog.answer.text() == "That is not the master password."
    dialog.password.setText(GOOD)
    dialog.check()
    assert dialog.answer.text() == "That is the master password."
    assert master.current == GOOD


def test_the_settings_offer_to_set_it_then_to_change_and_check_it(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    master = Master()
    shown: list[QDialog] = []

    def show(dialog: QDialog) -> object:
        shown.append(dialog)
        if isinstance(dialog, MasterPasswordDialog):
            fill(dialog, GOOD)
        return None

    monkeypatch.setattr(settings_window, "show_master_dialog", show)
    window = SettingsWindow(NoteManager(), Translations(), master=master.passwords())
    qtbot.addWidget(window)
    window.present(window.protection)
    assert window.tabs.isTabVisible(window.tabs.indexOf(window.protection))
    assert window.master_button.text() == "Set a &master password…"
    assert not window.check_master_button.isVisibleTo(window)

    window.master_button.click()

    assert master.current == GOOD
    assert window.master_button.text() == "Change the &master password…"
    assert window.check_master_button.isVisibleTo(window)
    window.check_master_button.click()
    assert isinstance(shown[-1], CheckMasterPasswordDialog)


def test_the_first_start_offers_it_below_the_recovery_key(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    master = Master()

    def set_it(shown: MasterPasswordDialog) -> None:
        fill(shown, GOOD)

    monkeypatch.setattr(first_run, "show_master_dialog", set_it)
    dialog = FirstRunDialog(
        "K7QM-2HXP-9RTV-4WJD-8FNB-3YCS-6GKA-M2Q0", False, False, master=master.passwords()
    )
    qtbot.addWidget(dialog)
    dialog.next_button.click()
    assert dialog.master_note.isVisibleTo(dialog)
    assert "somewhere other than this computer" in dialog.master_note.text()

    dialog.master_button.click()

    assert master.current == GOOD
    assert not dialog.master_button.isEnabled()
    assert dialog.later_button.isVisibleTo(dialog)


def test_notes_locked_with_a_password_have_no_later_for_the_recovery_key(qtbot: QtBot) -> None:
    dialog = FirstRunDialog(
        "K7QM-2HXP-9RTV-4WJD-8FNB-3YCS-6GKA-M2Q0", False, False, later_allowed=False
    )
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.next_button.click()

    assert not dialog.later_button.isVisibleTo(dialog)
    dialog.reject()  # Esc, or the close button
    assert dialog.isVisible()
    dialog.recovery.kept.setChecked(True)
    dialog.done_button.click()
    assert dialog.choices().recovery_key_kept


# Opening the notes with it when their key is lost


def notes_with_master(folder: Path, backend: Backend) -> bytes:
    unlock = Unlock(folder, store_of(backend), FAST, master_strength=FAST)
    connection = open_notes(unlock, never, never)
    assert connection is not None and unlock.opened_key is not None
    NoteRepository(connection).create("마스터 암호로 여는 메모")
    connection.close()
    unlock.make_recovery_key(unlock.opened_key)
    unlock.set_master_password(unlock.opened_key, GOOD)
    return unlock.opened_key


def enter(password: str) -> Callable[[PasswordDialog], bool]:
    """Answers the master password's window; any other is not expected."""

    def ask(dialog: PasswordDialog) -> bool:
        if dialog.master:
            type_and_accept(dialog, password)
            return dialog.result() == QDialog.DialogCode.Accepted
        return False

    return ask


def test_a_lost_key_comes_back_with_the_master_password(qtbot: QtBot, tmp_path: Path) -> None:
    backend = Backend()
    key = notes_with_master(tmp_path, backend)
    backend.items.clear()  # the key lost from the credential store
    offered: list[bool] = []

    def recovery(dialog: RecoveryDialog) -> Choice:
        offered.append(dialog.master_button.isVisibleTo(dialog))
        return Choice.MASTER

    unlock = Unlock(tmp_path, store_of(backend), FAST, master_strength=FAST)
    connection = open_notes(unlock, enter(GOOD), recovery, never)

    assert offered == [True]
    assert connection is not None and unlock.opened_key == key
    assert [n.body for n in NoteRepository(connection).all()] == ["마스터 암호로 여는 메모"]
    connection.close()
    assert backend.items  # put back in the store, as after the recovery key


def test_from_the_recovery_key_window_the_master_password_instead(
    qtbot: QtBot, tmp_path: Path
) -> None:
    backend = Backend()
    key = notes_with_master(tmp_path, backend)
    backend.items.clear()

    def instead(dialog: EnterRecoveryKeyDialog) -> bool:
        assert dialog.master_button.isVisibleTo(dialog)
        dialog.master_button.click()
        return False

    unlock = Unlock(tmp_path, store_of(backend), FAST, master_strength=FAST)
    connection = open_notes(unlock, enter(GOOD), Recovery(Choice.RECOVER), instead)

    assert connection is not None and unlock.opened_key == key
    connection.close()
