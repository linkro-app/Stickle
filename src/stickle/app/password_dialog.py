"""Ask for the password that protects the notes (only where no credential store exists)."""

from collections.abc import Callable
from typing import override

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from stickle.app.typed import forget_typed
from stickle.unlock import MIN_PASSWORD_LENGTH, password_length

# Returns an error to show, or None when the password was accepted.
type Submit = Callable[[str], str | None]


class PasswordDialog(QDialog):
    """Enter the password, or create one: at first start, or after the recovery key
    opened the notes (after_recovery). While unlocking, "I forgot the password"
    closes it with forgot set, where a recovery key exists. With master, it asks
    for the master password instead, to open the notes when the key is lost."""

    def __init__(
        self,
        create: bool,
        submit: Submit,
        parent: QWidget | None = None,
        can_recover: bool = False,
        after_recovery: bool = False,
        portable: bool = False,
        master: bool = False,
    ) -> None:
        super().__init__(parent)
        self.creating = create
        self.master = master and not create
        self.after_recovery = after_recovery
        self.portable = portable  # notes in a stickle-data folder, carried between computers
        self.forgot = False
        self._submit = submit

        self.intro = QLabel()
        self.intro.setWordWrap(True)
        self.input_note = QLabel()
        self.input_note.setWordWrap(True)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_label = QLabel()
        self.password_label.setBuddy(self.password)
        self.confirm_label = QLabel()
        self.confirm_label.setBuddy(self.confirm)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #b3261e;")
        self.error.hide()
        # Our own buttons: Qt renames standard ones to its "OK"/"Cancel" on a language change.
        self.buttons = QDialogButtonBox()
        self.ok_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.RejectRole)
        self.ok_button.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.forgot_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.ActionRole)
        self.forgot_button.setVisible(can_recover and not create)
        self.forgot_button.clicked.connect(self._forgot)

        form = QFormLayout()
        form.addRow(self.password_label, self.password)
        if create:
            form.addRow(self.confirm_label, self.confirm)
        else:
            self.confirm.hide()
            self.confirm_label.hide()
            self.input_note.hide()
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro)
        layout.addLayout(form)
        layout.addWidget(self.input_note)
        layout.addWidget(self.error)
        layout.addWidget(self.buttons)
        self.setMinimumWidth(420)
        self.retranslate()
        self.password.setFocus()

    def _forgot(self) -> None:
        self.forgot = True
        self.reject()

    def retranslate(self) -> None:
        self.forgot_button.setText(self.tr("I &forgot the password"))
        if self.after_recovery:
            self.setWindowTitle(self.tr("Choose a new password"))
            self.intro.setText(
                self.tr(
                    "The recovery key opened your notes. Choose a new password to lock "
                    "them with from now on."
                )
            )
            self.ok_button.setText(self.tr("Set password"))
        elif self.creating:
            self.setWindowTitle(self.tr("Protect your notes with a password"))
            if self.portable:
                intro = self.tr(
                    "These notes are kept in the stickle-data folder next to Stickle, to go"
                    " with it from computer to computer. They are locked with a password,"
                    " which you will enter each time Stickle starts."
                )
            else:
                intro = self.tr(
                    "This computer has no keychain where Stickle can keep the key to your"
                    " notes, so they are locked with a password instead. You will enter it"
                    " each time Stickle starts."
                )
            self.intro.setText(intro)
            self.ok_button.setText(self.tr("Create password"))
        elif self.master:
            self.setWindowTitle(self.tr("Open with the spare password"))
            self.intro.setText(self.tr("Enter the spare password you set in Stickle's settings."))
            self.ok_button.setText(self.tr("Open notes"))
        else:
            self.setWindowTitle(self.tr("Unlock your notes"))
            self.intro.setText(self.tr("Enter the password that protects your notes."))
            self.ok_button.setText(self.tr("Unlock"))
        self.cancel_button.setText(self.tr("Back") if self.master else self.tr("Quit"))
        self.password_label.setText(
            self.tr("&Spare password:") if self.master else self.tr("&Password:")
        )
        self.confirm_label.setText(self.tr("&Confirm password:"))
        self.password.setAccessibleName(
            self.tr("Spare password") if self.master else self.tr("Password")
        )
        self.confirm.setAccessibleName(self.tr("Confirm password"))
        self.input_note.setText(
            self.tr("Input methods, such as a Korean keyboard, are turned off in password fields.")
        )

    @override
    def done(self, result: int, /) -> None:
        super().done(result)
        forget_typed(self)

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def wrong_password(self) -> str:
        if self.master:
            return self.tr("The spare password is not correct.")
        return self.tr("The password is not correct.")

    def key_file_failed(self, reason: str) -> str:
        return self.tr("The key file could not be used (%1).").replace("%1", reason)

    def show_error(self, message: str) -> None:
        self.error.setText(message)
        self.error.show()
        # Screen readers announce the field again, now with the reason.
        self.password.setAccessibleDescription(message)
        self.password.setFocus()
        self.password.selectAll()

    @override
    def accept(self) -> None:
        password = self.password.text()
        if self.creating:
            if password_length(password) < MIN_PASSWORD_LENGTH:
                self.show_error(self.tr("Use at least %n characters.", "", MIN_PASSWORD_LENGTH))
                return
            if password != self.confirm.text():
                self.confirm.clear()
                self.show_error(self.tr("The two passwords are not the same."))
                return
        # Deriving the key takes a moment on purpose (it slows down guessing).
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            message = self._submit(password)
        finally:
            QGuiApplication.restoreOverrideCursor()
        if message is not None:
            if not self.creating:
                self.password.clear()
            self.show_error(message)
            return
        super().accept()
