"""Windows that asked for a password or a key, or showed one, keep none of it
once used: they live on, kept by the window they belong to, long after."""

from pathlib import Path

from PySide6.QtWidgets import QDialog, QLineEdit
from pytestqt.qtbot import QtBot
from test_master_password import GOOD, Master, fill
from test_startup_dialogs import FAST, Backend, store_of

from stickle.app import application
from stickle.app.master_password import CheckMasterPasswordDialog, MasterPasswordDialog
from stickle.app.password_dialog import PasswordDialog
from stickle.app.recovery_key_dialog import EnterRecoveryKeyDialog, RecoveryKeyDialog
from stickle.app.secret_dialogs import (
    NewSecretPasswordDialog,
    OpenSecretDialog,
    SecretExportDialog,
    SecretPasswords,
    TurnOnDialog,
)
from stickle.unlock import Unlock

SECRET = "secret notes 1"
MASTER = "correct horse battery staple"
RECOVERY_KEY = "K7QM-2HXP-9RTV-4WJD-8FNB-3YCS-6GKA-M2Q0"


def typed(dialog: QDialog) -> list[str]:
    return [field.text() for field in dialog.findChildren(QLineEdit) if field.text()]


def accepted(dialog: QDialog) -> bool:
    dialog.accept()
    return dialog.result() == QDialog.DialogCode.Accepted.value


def secret_passwords(tmp_path: Path) -> SecretPasswords:
    unlock = Unlock(tmp_path, store_of(Backend()), FAST, master_strength=FAST)
    unlock.opened_key = bytes(32)
    unlock.make_recovery_key(bytes(32))
    passwords = application.secret_passwords(unlock, application.master_passwords(unlock))
    assert passwords is not None
    return passwords


def turned_on(qtbot: QtBot, passwords: SecretPasswords) -> TurnOnDialog:
    dialog = TurnOnDialog(passwords)
    qtbot.addWidget(dialog)
    for field in (dialog.password, dialog.confirm):
        field.setText(SECRET)
    for field in (dialog.master, dialog.master_confirm):
        field.setText(MASTER)
    dialog.understood.setChecked(True)
    assert accepted(dialog)
    return dialog


def test_turning_them_on_keeps_neither_password_and_hands_the_key_over_once(
    qtbot: QtBot, tmp_path: Path
) -> None:
    dialog = turned_on(qtbot, secret_passwords(tmp_path))

    assert typed(dialog) == []
    key = dialog.take_key()
    assert key is not None and len(key) == 32
    assert dialog.take_key() is None and dialog.key is None


def test_opening_keeps_no_password_and_hands_the_key_over_once(
    qtbot: QtBot, tmp_path: Path
) -> None:
    passwords = secret_passwords(tmp_path)
    key = turned_on(qtbot, passwords).take_key()
    dialog = OpenSecretDialog(passwords, "은행")
    qtbot.addWidget(dialog)
    dialog.password.setText("not it at all")
    dialog.accept()
    assert dialog.password.text() == ""  # a wrong one is cleared as it was
    dialog.password.setText(SECRET)

    assert accepted(dialog)

    assert typed(dialog) == []
    assert dialog.take_key() == key and dialog.key is None


def test_a_new_password_window_lets_go_of_the_key_it_was_given(
    qtbot: QtBot, tmp_path: Path
) -> None:
    passwords = secret_passwords(tmp_path)
    key = turned_on(qtbot, passwords).take_key()
    assert key is not None
    dialog = NewSecretPasswordDialog(passwords, key)
    qtbot.addWidget(dialog)
    for field in (dialog.password, dialog.confirm):
        field.setText("a new one 22")

    assert accepted(dialog)

    assert typed(dialog) == [] and not dialog.held_key
    assert passwords.is_password("a new one 22")


def test_the_export_window_keeps_no_password(qtbot: QtBot) -> None:
    dialog = SecretExportDialog(1, lambda _password: bytes(32))
    qtbot.addWidget(dialog)
    dialog.plain.setChecked(True)
    dialog.password.setText(SECRET)

    assert accepted(dialog)

    assert typed(dialog) == []
    assert dialog.take_key() == bytes(32) and dialog.key is None


def test_cancelled_a_window_keeps_what_was_typed_no_longer(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = OpenSecretDialog(secret_passwords(tmp_path), "은행")
    qtbot.addWidget(dialog)
    dialog.password.setText(SECRET)

    dialog.reject()

    assert typed(dialog) == []


def test_the_master_password_windows_keep_nothing_typed(qtbot: QtBot) -> None:
    master = Master(current=GOOD)
    dialog = MasterPasswordDialog(master.passwords())
    qtbot.addWidget(dialog)
    fill(dialog, "another phrase to remember well", current=GOOD)
    assert dialog.result() == QDialog.DialogCode.Accepted.value
    check = CheckMasterPasswordDialog(master.passwords())
    qtbot.addWidget(check)
    check.password.setText("another phrase to remember well")
    check.check()

    check.reject()

    assert typed(dialog) == [] and typed(check) == []


def test_the_recovery_key_typed_is_kept_no_longer(qtbot: QtBot) -> None:
    dialog = EnterRecoveryKeyDialog(lambda _typed: None)
    qtbot.addWidget(dialog)
    dialog.key.setText(RECOVERY_KEY)

    dialog.ok_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted.value
    assert typed(dialog) == []


def test_a_recovery_key_shown_is_kept_no_longer_once_closed(qtbot: QtBot) -> None:
    dialog = RecoveryKeyDialog(RECOVERY_KEY)
    qtbot.addWidget(dialog)
    assert dialog.panel.key.text() == RECOVERY_KEY

    dialog.reject()  # Later, as Done

    assert dialog.panel.recovery_key == "" and dialog.panel.key.text() == ""
    assert dialog.panel.qr.pixmap().isNull()


def test_the_password_at_start_is_kept_no_longer(qtbot: QtBot) -> None:
    dialog = PasswordDialog(False, lambda _typed: None)
    qtbot.addWidget(dialog)
    dialog.password.setText(SECRET)

    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted.value
    assert typed(dialog) == []
