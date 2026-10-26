"""The windows of secret notes: turning them on, opening one, a new password.

Secret notes have one password for all of them, and the master password opens
them too; the recovery key never does. Turning them on the first time sets
the password, asks for the master password (set now if there is none, typed
again if there is, with the date it was set: someone else may have set it),
and has the user say they know that losing both locks the notes away for
good. Each secret note opens on its own, with the password.
"""

import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, override

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from stickle.app.master_password import ERROR_STYLE, MasterPasswords, weakness_reason
from stickle.app.recovery_key_dialog import KeptBox
from stickle.app.times import local_time
from stickle.app.typed import forget_typed
from stickle.core.passwords import weakness
from stickle.crypto.keyfile import KeyFileError, WrongPasswordError
from stickle.crypto.secret_notes import MIN_PASSWORD_LENGTH
from stickle.unlock import password_length

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecretPasswords:
    """The secret notes' key, without the windows knowing where it is kept."""

    turned_on: Callable[[], bool]
    turn_on: Callable[[str], bytes]  # their password: the new key
    open: Callable[[str], bytes]  # their password: the key (WrongPasswordError)
    open_with_master: Callable[[str], bytes]  # the master password: the key
    change_password: Callable[[bytes, str], None]  # the key known, a new password
    is_password: Callable[[str], bool]  # whether it is their password
    master: MasterPasswords  # set, checked and told apart from theirs
    set_at: Callable[[], str | None] = lambda: None  # when their password was set (UTC)


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


class _PasswordWindow(QDialog):
    """What the windows here share: fields, a line for what is wrong, buttons
    whose default is set once they are in the window (or GNOME's Cancel was)."""

    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.key: bytes | None = None  # the key opened, if this window opens one
        self.intro = QLabel()
        self.intro.setWordWrap(True)
        self.form = QFormLayout()
        self.message = _error_label()
        self.buttons = QDialogButtonBox()
        self.ok_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.RejectRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.layout_parts = QVBoxLayout(self)
        self.layout_parts.addWidget(self.intro)
        self.layout_parts.addLayout(self.form)

    def finish_layout(self) -> None:
        self.layout_parts.addWidget(self.message)
        self.layout_parts.addWidget(self.buttons)
        self.ok_button.setDefault(True)
        self.setMinimumWidth(460)
        # The buttons in the order the system lays them out (GNOME: Cancel first).
        layout = self.buttons.layout()
        if layout is not None:
            shown = [
                widget
                for index in range(layout.count())
                if (item := layout.itemAt(index)) is not None
                and isinstance(widget := item.widget(), QPushButton)
            ]
            for before, after in itertools.pairwise(shown):
                QWidget.setTabOrder(before, after)

    def show_error(self, message: str, field: QLineEdit) -> None:
        self.message.setStyleSheet(ERROR_STYLE)
        self.message.setText(message)
        self.message.show()
        field.setAccessibleDescription(message)
        field.setFocus()
        field.selectAll()

    def working(self) -> None:
        """Said before the key is derived, which takes a moment on purpose."""
        self.message.setStyleSheet("")
        self.message.setText(self.tr("Checking…"))
        self.message.show()
        QCoreApplication.processEvents()

    def password_refusal(
        self, password: str, confirm: str, passwords: SecretPasswords
    ) -> str | None:
        """Why password cannot be the secret notes' new one, or None."""
        if password_length(password) < MIN_PASSWORD_LENGTH:
            # Not a plural (%n): tr() on a subclass takes the English plural, its %n
            # filled in, for a translation found under the subclass's name.
            return self.tr("Use at least %1 characters.").replace("%1", str(MIN_PASSWORD_LENGTH))
        if password != confirm:
            return self.tr("The two passwords are not the same.")
        if passwords.master.opens_notes(password):
            return self.tr("Choose a password other than the one that opens your notes.")
        if passwords.master.exists() and passwords.master.check(password):
            return self.tr("Choose a password other than the master password.")
        return None

    def take_key(self) -> bytes | None:
        """The key opened, handed over once: the window keeps it no longer."""
        key, self.key = self.key, None
        return key

    @override
    def done(self, result: int, /) -> None:
        super().done(result)
        forget_typed(self)  # the window lives on, kept by the one it belongs to

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def retranslate(self) -> None:
        self.cancel_button.setText(self.tr("Cancel"))


class TurnOnDialog(_PasswordWindow):
    """The first secret note: the password for all of them, the master password,
    and that losing both locks them away. key holds the new key once accepted."""

    def __init__(self, passwords: SecretPasswords, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._passwords = passwords
        self.has_master = passwords.master.exists()
        self.password = _password_field()
        self.password_label = QLabel()
        self.password_label.setBuddy(self.password)
        self.confirm = _password_field()
        self.confirm_label = QLabel()
        self.confirm_label.setBuddy(self.confirm)
        self.form.addRow(self.password_label, self.password)
        self.form.addRow(self.confirm_label, self.confirm)
        self.master_intro = QLabel()
        self.master_intro.setWordWrap(True)
        self.layout_parts.addWidget(self.master_intro)
        self.master_form = QFormLayout()
        self.master = _password_field()
        self.master_label = QLabel()
        self.master_label.setBuddy(self.master)
        self.master_confirm = _password_field()
        self.master_confirm_label = QLabel()
        self.master_confirm_label.setBuddy(self.master_confirm)
        self.master_form.addRow(self.master_label, self.master)
        self.master_form.addRow(self.master_confirm_label, self.master_confirm)
        self.master_form.setRowVisible(1, not self.has_master)
        self.layout_parts.addLayout(self.master_form)
        self.understood = KeptBox()  # Enter goes on to Turn on, as Space ticks it
        self.understood.toggled.connect(
            lambda: self.ok_button.setEnabled(self.understood.isChecked())
        )
        self.layout_parts.addWidget(self.understood)
        self.finish_layout()
        self.ok_button.setEnabled(False)
        self.retranslate()
        self.password.setFocus()

    @override
    def retranslate(self) -> None:
        super().retranslate()
        self.setWindowTitle(self.tr("Turn on secret notes"))
        self.intro.setText(
            self.tr(
                "A secret note stays locked until you open it with the secret notes'"
                " password, one for all of them. Their text is never searched."
            )
        )
        self.password_label.setText(self.tr("Secret notes' &password:"))
        self.confirm_label.setText(self.tr("C&onfirm:"))
        self.password.setAccessibleName(self.tr("Secret notes' password"))
        self.confirm.setAccessibleName(self.tr("Confirm the secret notes' password"))
        if self.has_master:
            set_at = self._passwords.master.set_at()
            when = local_time(set_at) if set_at else self.tr("earlier")
            self.master_intro.setText(
                self.tr(
                    "The master password opens them too. It was set on %1: type it to"
                    " show it is yours. If you do not know it, set it again in the"
                    " settings with the recovery key first."
                ).replace("%1", when)
            )
        else:
            self.master_intro.setText(
                self.tr(
                    "Set a master password too: it opens secret notes if their password"
                    " is forgotten. The recovery key does not open them."
                )
            )
        self.master_label.setText(self.tr("&Master password:"))
        self.master_confirm_label.setText(self.tr("Confirm the m&aster password:"))
        self.master.setAccessibleName(self.tr("Master password"))
        self.master_confirm.setAccessibleName(self.tr("Confirm the master password"))
        self.understood.setText(
            self.tr(
                "I &understand: if I lose both the secret notes' password and the master"
                " password, no one can open my secret notes, not even with the recovery key."
            )
        )
        self.ok_button.setText(self.tr("Turn on"))

    @override
    def accept(self) -> None:
        passwords, master = self._passwords, self.master.text()
        refused = self.password_refusal(self.password.text(), self.confirm.text(), passwords)
        if refused is not None:
            self.confirm.clear()
            self.show_error(refused, self.password)
            return
        if not self.has_master:
            found = weakness(master)
            if found is not None or master != self.master_confirm.text():
                self.master_confirm.clear()
                self.show_error(
                    weakness_reason(found)
                    if found is not None
                    else self.tr("The two master passwords are not the same."),
                    self.master,
                )
                return
            if passwords.master.opens_notes(master):
                self.show_error(
                    self.tr("Choose a master password other than the one that opens your notes."),
                    self.master,
                )
                return
        if master == self.password.text():
            self.show_error(
                self.tr("Choose a master password other than the secret notes' password."),
                self.master,
            )
            return
        self.working()
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            if self.has_master:
                if not passwords.master.check(master):
                    self.master.clear()
                    self.show_error(self.tr("The master password is not correct."), self.master)
                    return
            else:
                passwords.master.set(master)
            self.key = passwords.turn_on(self.password.text())
        except (KeyFileError, OSError, RuntimeError, MemoryError) as error:
            log.error("secret notes not turned on: %s", type(error).__name__)
            self.show_error(
                self.tr("Secret notes could not be turned on (%1).").replace(
                    "%1", type(error).__name__
                ),
                self.password,
            )
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        super().accept()


type Purpose = Literal["open", "make", "change"]


class OpenSecretDialog(_PasswordWindow):
    """The secret notes' password: to open one (titled title), to make a note
    one of them, or before setting a new one. Forgotten, the master password
    opens them and a new password is set (using_master). key holds the key
    once accepted."""

    def __init__(
        self,
        passwords: SecretPasswords,
        title: str = "",
        parent: QWidget | None = None,
        purpose: Purpose = "open",
    ) -> None:
        super().__init__(parent)
        self._passwords = passwords
        self._title = title
        self.purpose: Purpose = purpose
        self.using_master = False
        self.password = _password_field()
        self.password_label = QLabel()
        self.password_label.setBuddy(self.password)
        self.form.addRow(self.password_label, self.password)
        self.forgot_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.ActionRole)
        self.forgot_button.clicked.connect(self.use_master)
        self.forgot_button.setVisible(passwords.master.exists())
        self.finish_layout()
        self.retranslate()
        self.password.setFocus()

    @override
    def retranslate(self) -> None:
        super().retranslate()
        titles = {
            "open": self.tr("Open a secret note"),
            "make": self.tr("Make it a secret note"),
            "change": self.tr("Change the secret notes' password"),
        }
        self.setWindowTitle(titles[self.purpose])
        if self.using_master:
            self.intro.setText(
                self.tr("Type the master password. You will then set a new password for them.")
            )
            self.password_label.setText(self.tr("&Master password:"))
            self.password.setAccessibleName(self.tr("Master password"))
        else:
            intros = {
                "open": self.tr("Type the secret notes' password to open “%1”.").replace(
                    "%1", self._title
                ),
                "make": self.tr("Type the secret notes' password to make this note one of them."),
                "change": self.tr("Type the secret notes' password you have now."),
            }
            self.intro.setText(intros[self.purpose])
            self.password_label.setText(self.tr("Secret notes' &password:"))
            self.password.setAccessibleName(self.tr("Secret notes' password"))
        self.forgot_button.setText(self.tr("I &forgot the password"))
        self.ok_button.setText(self.tr("Open") if self.purpose == "open" else self.tr("Next"))

    def use_master(self) -> None:
        self.using_master = True
        self.forgot_button.hide()
        self.password.clear()
        self.message.hide()
        self.retranslate()
        self.password.setFocus()

    @override
    def accept(self) -> None:
        self.working()
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            if self.using_master:
                key = self._passwords.open_with_master(self.password.text())
            else:
                key = self._passwords.open(self.password.text())
        except WrongPasswordError:
            self.password.clear()
            self.show_error(
                self.tr("The master password is not correct.")
                if self.using_master
                else self.tr("The password is not correct."),
                self.password,
            )
            return
        except (KeyFileError, OSError, RuntimeError, MemoryError) as error:
            log.error("secret note not opened: %s", type(error).__name__)
            self.show_error(
                self.tr("The secret notes' key could not be used (%1).").replace(
                    "%1", type(error).__name__
                ),
                self.password,
            )
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        if self.using_master and not ask_new_password(
            NewSecretPasswordDialog(self._passwords, key, self)
        ):
            return  # opened only once a new password is set
        self.key = key
        super().accept()


class NewSecretPasswordDialog(_PasswordWindow):
    """A new password for the secret notes, their key known (opened)."""

    def __init__(
        self, passwords: SecretPasswords, key: bytes, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._passwords = passwords
        self.held_key = key  # let go of once the window closes
        self.password = _password_field()
        self.password_label = QLabel()
        self.password_label.setBuddy(self.password)
        self.confirm = _password_field()
        self.confirm_label = QLabel()
        self.confirm_label.setBuddy(self.confirm)
        self.form.addRow(self.password_label, self.password)
        self.form.addRow(self.confirm_label, self.confirm)
        self.finish_layout()
        self.retranslate()
        self.password.setFocus()

    @override
    def retranslate(self) -> None:
        super().retranslate()
        self.setWindowTitle(self.tr("New secret notes' password"))
        self.intro.setText(self.tr("Choose a new password for all your secret notes."))
        self.password_label.setText(self.tr("&New password:"))
        self.confirm_label.setText(self.tr("C&onfirm:"))
        self.password.setAccessibleName(self.tr("New secret notes' password"))
        self.confirm.setAccessibleName(self.tr("Confirm the secret notes' password"))
        self.ok_button.setText(self.tr("Set"))

    @override
    def accept(self) -> None:
        refused = self.password_refusal(self.password.text(), self.confirm.text(), self._passwords)
        if refused is not None:
            self.confirm.clear()
            self.show_error(refused, self.password)
            return
        self.working()
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._passwords.change_password(self.held_key, self.password.text())
        except (KeyFileError, OSError) as error:
            log.error("secret notes' password not changed: %s", type(error).__name__)
            self.show_error(
                self.tr("The password could not be kept (%1).").replace("%1", type(error).__name__),
                self.password,
            )
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        super().accept()

    @override
    def done(self, result: int, /) -> None:
        super().done(result)
        self.held_key = b""


class SecretTitleDialog(_PasswordWindow):
    """The title a secret note shows while locked: typed apart from its text."""

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = QLineEdit(title)
        self.title_label = QLabel()
        self.title_label.setBuddy(self.title)
        self.form.addRow(self.title_label, self.title)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.layout_parts.addWidget(self.note)
        self.finish_layout()
        self.retranslate()
        self.title.setFocus()
        self.title.selectAll()

    @override
    def retranslate(self) -> None:
        super().retranslate()
        self.setWindowTitle(self.tr("Secret note's title"))
        self.intro.setText(self.tr("The note is shown by this title while it is locked."))
        self.title_label.setText(self.tr("&Title:"))
        self.title.setAccessibleName(self.tr("Title"))
        self.title.setPlaceholderText(self.tr("Secret note"))
        self.note.setText(
            self.tr("The title shows even while the note is locked. Do not put its content in it.")
        )
        self.ok_button.setText(self.tr("OK"))

    def chosen(self) -> str:
        return self.title.text().strip()


class _Choice(QRadioButton):
    """One of the ways to choose: the arrows move between them, and Enter goes on
    to the window's default button. Some desktops (GNOME) have Enter press the
    button with the keyboard instead, as for KeptBox."""

    @override
    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() in KeptBox.ENTER_KEYS:
            e.ignore()
            return
        super().keyPressEvent(e)

    @override
    def keyReleaseEvent(self, e: QKeyEvent) -> None:
        if e.key() in KeptBox.ENTER_KEYS:
            e.ignore()
            return
        super().keyReleaseEvent(e)


class SecretExportDialog(_PasswordWindow):
    """How the secret notes go out with the rest of the notes: encrypted, as they
    are, or in the clear once their password shows it is the user's. key holds
    their key once accepted that way."""

    def __init__(
        self, count: int, open_notes: Callable[[str], bytes], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._count = count
        self._open = open_notes
        self.encrypted = _Choice()
        self.encrypted.setChecked(True)  # what they are: chosen until the user says otherwise
        self.encrypted_note = QLabel()
        self.encrypted_note.setWordWrap(True)
        self.plain = _Choice()
        for place, widget in enumerate((self.encrypted, self.encrypted_note, self.plain), 1):
            self.layout_parts.insertWidget(place, widget)
        self.password = _password_field()
        self.password_label = QLabel()
        self.password_label.setBuddy(self.password)
        self.form.addRow(self.password_label, self.password)
        self.plain_note = QLabel()
        self.plain_note.setWordWrap(True)
        self.layout_parts.addWidget(self.plain_note)
        self.plain.toggled.connect(self._show_choice)
        self.finish_layout()
        self.retranslate()
        self._show_choice()
        self.encrypted.setFocus()

    def _show_choice(self) -> None:
        plain = self.plain.isChecked()
        for part in (self.password, self.password_label, self.plain_note):
            part.setEnabled(plain)
        if plain:
            self.password.setFocus()

    @override
    def retranslate(self) -> None:
        super().retranslate()
        self.setWindowTitle(self.tr("Export secret notes"))
        self.intro.setText(
            self.tr("How should the %n secret note(s) be exported?", "", self._count)
        )
        self.encrypted.setText(self.tr("&Encrypted, as they are"))
        self.encrypted_note.setText(
            self.tr("Their text stays encrypted: only the titles typed for them can be read.")
        )
        self.plain.setText(self.tr("In the &clear"))
        self.password_label.setText(self.tr("Secret notes' &password:"))
        self.password.setAccessibleName(self.tr("Secret notes' password"))
        self.plain_note.setText(
            self.tr(
                "The exported files hold no password: anyone who has them can read these notes."
            )
        )
        self.ok_button.setText(self.tr("Next"))

    @override
    def accept(self) -> None:
        if self.encrypted.isChecked():
            super().accept()
            return
        self.working()
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            key = self._open(self.password.text())
        except WrongPasswordError:
            self.password.clear()
            self.show_error(self.tr("The password is not correct."), self.password)
            return
        except (KeyFileError, OSError, RuntimeError, MemoryError) as error:
            log.error("secret notes not opened to export: %s", type(error).__name__)
            self.show_error(
                self.tr("The secret notes' key could not be used (%1).").replace(
                    "%1", type(error).__name__
                ),
                self.password,
            )
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.key = key
        super().accept()


def _exec(dialog: QDialog) -> bool:
    return dialog.exec() == QDialog.DialogCode.Accepted


ask_new_password: Callable[[NewSecretPasswordDialog], bool] = _exec  # replaced in tests
