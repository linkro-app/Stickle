"""Setting, changing and checking the master password.

A second way back to the notes, beside the recovery key: one the user
remembers, in case the recovery key cannot be found. It is never removed, only
changed: with the current one, or, forgotten, after the recovery key shows it
is the user's. A password easy to guess is refused, with the reason in one
line and no strength meter (stickle.core.passwords). Deriving its key takes
about a second, said meanwhile.
"""

import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from stickle.app.recovery_key_dialog import EnterRecoveryKeyDialog
from stickle.app.typed import forget_typed
from stickle.core.passwords import Weakness, weakness
from stickle.crypto.keyfile import KeyFileError, WrongPasswordError
from stickle.crypto.recovery import RecoveryKeyTypoError

ERROR_STYLE = "color: #b3261e;"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MasterPasswords:
    """The master password, without the windows knowing the notes' key."""

    exists: Callable[[], bool]
    set: Callable[[str], None]  # a new one: none yet, or one forgotten
    change: Callable[[str, str], None]  # current, new; WrongPasswordError
    check: Callable[[str], bool]  # whether it is the master password
    opens_notes: Callable[[str], bool]  # whether it is the password that opens the notes
    # Whether a recovery key opens these notes; RecoveryKeyTypoError if mistyped.
    recovery_key_opens: Callable[[str], bool]
    set_at: Callable[[], str | None] = lambda: None  # when it was set or changed (UTC)
    # Whether it is the secret notes' password, which it must not be (it opens
    # nothing of theirs, and is typed less often).
    is_secret_password: Callable[[str], bool] = lambda _typed: False


def weakness_reason(found: Weakness) -> str:
    """Why a master password is refused, in a line."""
    reasons = {
        Weakness.SHORT: QCoreApplication.translate(
            "MasterPasswordDialog", "Use at least 12 characters, or 4 words or more."
        ),
        Weakness.COMMON: QCoreApplication.translate(
            "MasterPasswordDialog", "That is one of the most common passwords. Choose another."
        ),
        Weakness.PATTERN: QCoreApplication.translate(
            "MasterPasswordDialog",
            "Repeated characters or a run of the keyboard are easy to guess. Choose another.",
        ),
    }
    return reasons[found]


def _password_field() -> QLineEdit:
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    return field


def _error_label() -> QLabel:
    label = QLabel()
    label.setWordWrap(True)
    label.setStyleSheet(ERROR_STYLE)
    label.hide()
    return label


def _working(label: QLabel, text: str) -> None:
    """Said before the second the key takes, which would otherwise pass unseen."""
    label.setStyleSheet("")
    label.setText(text)
    label.show()
    QCoreApplication.processEvents()


class MasterPasswordDialog(QDialog):
    """Set the master password, or change it: then the current one is asked for
    first, or, forgotten, the recovery key."""

    def __init__(self, passwords: MasterPasswords, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._passwords = passwords
        self.changing = passwords.exists()
        self.intro = QLabel()
        self.intro.setWordWrap(True)
        self.current = _password_field()
        self.current_label = QLabel()
        self.current_label.setBuddy(self.current)
        self.new = _password_field()
        self.new_label = QLabel()
        self.new_label.setBuddy(self.new)
        self.confirm = _password_field()
        self.confirm_label = QLabel()
        self.confirm_label.setBuddy(self.confirm)
        self.rules = QLabel()
        self.rules.setWordWrap(True)
        self.message = _error_label()
        self.buttons = QDialogButtonBox()
        self.ok_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.RejectRole)
        self.forgot_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.ActionRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.forgot_button.clicked.connect(self.forgot)

        self.form = QFormLayout()
        self.form.addRow(self.current_label, self.current)
        self.form.addRow(self.new_label, self.new)
        self.form.addRow(self.confirm_label, self.confirm)
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro)
        layout.addLayout(self.form)
        layout.addWidget(self.rules)
        layout.addWidget(self.message)
        layout.addWidget(self.buttons)
        # Once in this window, or it never learns: Qt then made the first button of
        # the Tab order the default, Cancel where the desktop puts it first (GNOME).
        self.ok_button.setDefault(True)
        self.setMinimumWidth(460)
        self._show_mode()
        self.retranslate()

    def _show_mode(self) -> None:
        self.form.setRowVisible(0, self.changing)
        self.forgot_button.setVisible(self.changing)
        (self.current if self.changing else self.new).setFocus()
        # The fields, then the buttons as the system lays them out (it orders them its way).
        layout = self.buttons.layout()
        assert layout is not None
        buttons = [
            widget
            for index in range(layout.count())
            if (item := layout.itemAt(index)) is not None
            and isinstance(widget := item.widget(), QPushButton)
        ]
        for before, after in itertools.pairwise([self.current, self.new, self.confirm, *buttons]):
            QWidget.setTabOrder(before, after)

    def retranslate(self) -> None:
        self.current_label.setText(self.tr("&Current master password:"))
        self.current.setAccessibleName(self.tr("Current master password"))
        if self.changing:
            self.setWindowTitle(self.tr("Change the master password"))
            self.intro.setText(self.tr("Enter the master password you have now, then the new one."))
            self.new_label.setText(self.tr("&New master password:"))
            self.ok_button.setText(self.tr("Change"))
        else:
            self.setWindowTitle(self.tr("Set a master password"))
            self.intro.setText(
                self.tr(
                    "A master password you remember is a second way back to your notes, in"
                    " case you cannot find the recovery key. Once set it is never removed,"
                    " only changed."
                )
            )
            self.new_label.setText(self.tr("&Master password:"))
            self.ok_button.setText(self.tr("Set"))
        self.confirm_label.setText(self.tr("C&onfirm:"))
        self.new.setAccessibleName(self.tr("New master password"))
        self.confirm.setAccessibleName(self.tr("Confirm the master password"))
        self.rules.setText(
            self.tr(
                "At least 12 characters, or 4 words or more, and not a common password."
                " It cannot be the password that opens your notes."
            )
        )
        self.cancel_button.setText(self.tr("Cancel"))
        self.forgot_button.setText(self.tr("I &forgot it"))

    @override
    def done(self, result: int, /) -> None:
        super().done(result)
        forget_typed(self)

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def refusal(self, password: str) -> str | None:
        """Why password cannot be the new master password, or None."""
        found = weakness(password)
        if found is not None:
            return weakness_reason(found)
        if password != self.confirm.text():
            return self.tr("The two passwords are not the same.")
        if self._passwords.opens_notes(password):
            return self.tr(
                "Choose a master password other than the password that opens your notes."
            )
        if self._passwords.is_secret_password(password):
            return self.tr("Choose a master password other than the secret notes' password.")
        return None

    def show_error(self, message: str, field: QLineEdit) -> None:
        self.message.setStyleSheet(ERROR_STYLE)
        self.message.setText(message)
        self.message.show()
        field.setAccessibleDescription(message)  # read with the field again
        field.setFocus()
        field.selectAll()

    @override
    def accept(self) -> None:
        new = self.new.text()
        refused = self.refusal(new)
        if refused is not None:
            self.confirm.clear()
            self.show_error(refused, self.new)
            return
        _working(self.message, self.tr("Checking…"))
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            if self.changing:
                self._passwords.change(self.current.text(), new)
            else:
                self._passwords.set(new)
        except WrongPasswordError:
            self.current.clear()
            self.show_error(self.tr("The current master password is not correct."), self.current)
            return
        except (KeyFileError, OSError, RuntimeError, MemoryError) as error:
            # RuntimeError: the key could not be derived (too little memory for it).
            log.error("master password not kept: %s", type(error).__name__)
            self.show_error(
                self.tr("The master password could not be kept (%1).").replace(
                    "%1", type(error).__name__
                ),
                self.new,
            )
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        super().accept()

    def forgot(self) -> None:
        """The recovery key instead of the current master password, then a new one."""
        dialog: EnterRecoveryKeyDialog

        def submit(typed: str) -> str | None:
            try:
                opens = self._passwords.recovery_key_opens(typed)
            except RecoveryKeyTypoError:
                return dialog.typo()
            return None if opens else dialog.wrong_key()

        dialog = EnterRecoveryKeyDialog(submit, self)
        if ask_recovery_key(dialog):
            self.changing = False
            self.current.clear()
            self.message.hide()
            self._show_mode()
            self.retranslate()


def _exec(dialog: EnterRecoveryKeyDialog) -> bool:
    return dialog.exec() == QDialog.DialogCode.Accepted


ask_recovery_key: Callable[[EnterRecoveryKeyDialog], bool] = _exec  # replaced in tests


class CheckMasterPasswordDialog(QDialog):
    """Whether the user still remembers it: says so, changes nothing."""

    def __init__(self, passwords: MasterPasswords, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._passwords = passwords
        self.intro = QLabel()
        self.intro.setWordWrap(True)
        self.password = _password_field()
        self.label = QLabel()
        self.label.setBuddy(self.password)
        self.answer = QLabel()
        self.answer.setWordWrap(True)
        self.answer.hide()
        self.buttons = QDialogButtonBox()
        self.check_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.ActionRole)
        self.close_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.RejectRole)
        self.check_button.clicked.connect(self.check)
        self.buttons.rejected.connect(self.reject)
        form = QFormLayout()
        form.addRow(self.label, self.password)
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro)
        layout.addLayout(form)
        layout.addWidget(self.answer)
        layout.addWidget(self.buttons)
        self.check_button.setDefault(True)  # once in this window (see MasterPasswordDialog)
        self.setMinimumWidth(420)
        self.retranslate()
        self.password.setFocus()

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Check the master password"))
        self.intro.setText(
            self.tr(
                "Type the master password to see whether you still remember it. Nothing changes."
            )
        )
        self.label.setText(self.tr("&Master password:"))
        self.password.setAccessibleName(self.tr("Master password"))
        self.check_button.setText(self.tr("Check"))
        self.close_button.setText(self.tr("Close"))

    @override
    def done(self, result: int, /) -> None:
        super().done(result)
        forget_typed(self)

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def check(self) -> None:
        _working(self.answer, self.tr("Checking…"))
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            right = self._passwords.check(self.password.text())
        except (KeyFileError, OSError, RuntimeError, MemoryError) as error:
            log.error("master password not checked: %s", type(error).__name__)
            self.answer.setText(
                self.tr("The master password could not be checked (%1).").replace(
                    "%1", type(error).__name__
                )
            )
            self.answer.setStyleSheet(ERROR_STYLE)
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.answer.setText(
            self.tr("That is the master password.")
            if right
            else self.tr("That is not the master password.")
        )
        self.answer.setStyleSheet("" if right else ERROR_STYLE)
        self.password.setAccessibleDescription(self.answer.text())
        self.password.setFocus()
        self.password.selectAll()
