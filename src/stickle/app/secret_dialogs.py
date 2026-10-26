"""The windows of secret notes: turning them on, opening one, a new password.

Secret notes have one password for all of them; forgotten, the recovery key
opens them and a new one is set. Turning them on the first time sets the
password alone, with the date the recovery key was made (someone else may
have made it) and a way to make a new one; a recovery key from before 0.6 is
typed once first, or one is made where there is none. Each secret note opens
on its own, with the password.
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
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from stickle.app.master_password import ERROR_STYLE, MasterPasswords, weakness_reason
from stickle.app.recovery_key_dialog import (
    EnterRecoveryKeyDialog,
    KeptBox,
    RecoveryKeyDialog,
    RecoveryKeys,
)
from stickle.app.times import local_time
from stickle.app.typed import forget_typed
from stickle.core.passwords import guessable
from stickle.crypto.keyfile import KeyFileError, WrongPasswordError
from stickle.crypto.recovery import RecoveryKeyTypoError, WrongRecoveryKeyError
from stickle.crypto.secret_notes import MIN_PASSWORD_LENGTH, NotKeptForRecoveryKeyError
from stickle.unlock import password_length

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecretPasswords:
    """The secret notes' key, without the windows knowing where it is kept."""

    turned_on: Callable[[], bool]
    turn_on: Callable[[str], bytes]  # their password: the new key
    open: Callable[[str], bytes]  # their password: the key (WrongPasswordError)
    # The recovery key typed: the key (RecoveryKeyTypoError, WrongRecoveryKeyError,
    # NotKeptForRecoveryKeyError).
    open_with_recovery_key: Callable[[str], bytes]
    change_password: Callable[[bytes, str], None]  # the key known, a new password
    reset_password: Callable[[bytes, str], None]  # a new one, the recovery key having opened them
    is_password: Callable[[str], bool]  # whether it is their password
    master: MasterPasswords  # told apart from theirs
    recovery: RecoveryKeys | None = None  # what opens them when the password is forgotten
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


def _wipe(key: bytearray) -> None:
    key[:] = bytes(len(key))


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

    def add_rules(self) -> None:
        """What a new password must be, said before one is refused."""
        self.rules = QLabel()
        self.rules.setWordWrap(True)
        self.layout_parts.addWidget(self.rules)

    def rules_text(self) -> str:
        return self.tr("At least %1 characters, and not a common password.").replace(
            "%1", str(MIN_PASSWORD_LENGTH)
        )

    def password_refusal(
        self, password: str, confirm: str, passwords: SecretPasswords
    ) -> str | None:
        """Why password cannot be the secret notes' new one, or None."""
        if password_length(password) < MIN_PASSWORD_LENGTH:
            # Not a plural (%n): tr() on a subclass takes the English plural, its %n
            # filled in, for a translation found under the subclass's name.
            return self.tr("Use at least %1 characters.").replace("%1", str(MIN_PASSWORD_LENGTH))
        found = guessable(password)
        if found is not None:
            return weakness_reason(found)
        if password != confirm:
            return self.tr("The two passwords are not the same.")
        if passwords.master.opens_notes(password):
            return self.tr("Choose a password other than the one that opens your notes.")
        if passwords.master.exists() and passwords.master.check(password):
            return self.tr("Choose a password other than the spare password.")
        return None

    def take_key(self) -> bytes | None:
        """The key opened, handed over once: the window keeps it no longer."""
        key, self.key = self.key, None
        return key

    @override
    def done(self, result: int, /) -> None:
        super().done(result)
        forget_typed(self)
        # Used once: not kept, hidden, by the window it belongs to until that closes.
        self.deleteLater()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def retranslate(self) -> None:
        self.cancel_button.setText(self.tr("Cancel"))


# Turned on only when ready: without a recovery key the user keeps, a forgotten
# password would leave the secret notes closed for good.
type RecoveryState = Literal["ready", "not_kept", "old", "none"]


class TurnOnDialog(_PasswordWindow):
    """The first secret note: the password for all of them, and the recovery key
    that sets a new one if it is forgotten. key holds the new key once accepted."""

    def __init__(self, passwords: SecretPasswords, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._passwords = passwords
        self.password = _password_field()
        self.password_label = QLabel()
        self.password_label.setBuddy(self.password)
        self.confirm = _password_field()
        self.confirm_label = QLabel()
        self.confirm_label.setBuddy(self.confirm)
        self.form.addRow(self.password_label, self.password)
        self.form.addRow(self.confirm_label, self.confirm)
        self.add_rules()
        self.recovery_line = QLabel()
        self.recovery_line.setWordWrap(True)
        self.layout_parts.addWidget(self.recovery_line)
        self.type_button = QPushButton()
        self.type_button.clicked.connect(self.type_recovery_key)
        self.make_button = QPushButton()
        self.make_button.clicked.connect(self.make_recovery_key)
        row = QHBoxLayout()
        row.addWidget(self.type_button)
        row.addWidget(self.make_button)
        row.addStretch()
        self.layout_parts.addLayout(row)
        self.finish_layout()
        self.retranslate()
        self.password.setFocus()

    def recovery_state(self) -> RecoveryState:
        recovery = self._passwords.recovery
        if recovery is None:
            return "ready"  # nothing to say (no recovery keys here: tried, and refused if so)
        if not recovery.exists():
            return "none"
        if recovery.is_old():
            return "old"
        return "ready" if recovery.was_kept() else "not_kept"  # "Later" at the first start

    @override
    def retranslate(self) -> None:
        super().retranslate()
        self.setWindowTitle(self.tr("Turn on secret notes"))
        self.rules.setText(self.rules_text())
        self.intro.setText(
            self.tr(
                "A secret note keeps its text hidden until you open it with the secret"
                " notes' password, one for all of them. Their text is never searched."
            )
        )
        self.password_label.setText(self.tr("Secret notes' &password:"))
        self.confirm_label.setText(self.tr("C&onfirm:"))
        self.password.setAccessibleName(self.tr("Secret notes' password"))
        self.confirm.setAccessibleName(self.tr("Confirm the secret notes' password"))
        self.type_button.setText(self.tr("&Type the recovery key…"))
        self.ok_button.setText(self.tr("Turn on"))
        self.show_recovery_state()

    def show_recovery_state(self) -> None:
        """The line on the recovery key, and what can be done about it now."""
        state = self.recovery_state()
        recovery = self._passwords.recovery
        made_at = recovery.made_at() if recovery is not None else None
        if state == "none":
            line = self.tr(
                "If you forget the password, you set a new one with the recovery key."
                " Make one first: it opens secret notes too, so do not keep it on this"
                " computer."
            )
        elif state == "old":
            line = self.tr(
                "Your recovery key was made with an earlier Stickle and cannot open"
                " secret notes yet. Type it once, or make a new one if you cannot find it."
            )
        elif state == "not_kept":
            line = self.tr(
                "You have not kept a recovery key yet. If you forget the password, only"
                " the recovery key sets a new one: make a new one and keep it, away from"
                " this computer, then turn them on."
            )
        elif made_at is not None:
            line = self.tr(
                "If you forget the password, you set a new one with the recovery key"
                " made on %1. It opens secret notes too: do not keep it on this computer."
            ).replace("%1", local_time(made_at))
        else:
            line = self.tr(
                "If you forget the password, you set a new one with your recovery key."
                " It opens secret notes too: do not keep it on this computer."
            )
        self.recovery_line.setText(line)
        self.recovery_line.setVisible(recovery is not None)
        self.type_button.setVisible(state == "old")
        self.make_button.setVisible(recovery is not None)
        self.make_button.setText(
            self.tr("&Make a recovery key…")
            if state == "none"
            else self.tr("&Make a new recovery key…")
        )
        self.ok_button.setEnabled(state == "ready")
        self.ok_button.setAccessibleDescription(line if state != "ready" else "")

    def type_recovery_key(self) -> None:
        """A recovery key from before 0.6, typed once: it opens secret notes too from now on."""
        recovery = self._passwords.recovery
        if recovery is None:
            return
        dialog: EnterRecoveryKeyDialog

        def submit(typed: str) -> str | None:
            try:
                recovery.upgrade(typed)
            except RecoveryKeyTypoError:
                return dialog.typo()
            except WrongRecoveryKeyError:
                return dialog.wrong_key()
            except (KeyFileError, OSError, RuntimeError, MemoryError) as error:
                log.error("recovery key not kept for secret notes: %s", type(error).__name__)
                return self.tr("The recovery key could not be kept (%1).").replace(
                    "%1", type(error).__name__
                )
            return None

        dialog = EnterRecoveryKeyDialog(submit, self)
        ask_recovery_key(dialog)
        self.show_recovery_state()

    def make_recovery_key(self) -> None:
        """A new recovery key, shown; any earlier one stops working."""
        recovery = self._passwords.recovery
        if recovery is None:
            return
        if recovery.exists() and not confirm_new_recovery_key(self):
            return
        try:
            recovery_key = recovery.make()
        except (KeyFileError, OSError) as error:
            log.error("could not make a recovery key: %s", type(error).__name__)
            self.show_error(self.tr("The recovery key could not be saved."), self.password)
            return
        if show_recovery_key(RecoveryKeyDialog(recovery_key, self)):
            recovery.kept()
        self.show_recovery_state()

    @override
    def accept(self) -> None:
        passwords = self._passwords
        refused = self.password_refusal(self.password.text(), self.confirm.text(), passwords)
        if refused is not None:
            self.confirm.clear()
            self.show_error(refused, self.password)
            return
        if self.recovery_state() != "ready":
            return  # its button is off until there is a recovery key to seal them for
        self.working()
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
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


type Purpose = Literal["open", "make", "change", "recovery"]


class OpenSecretDialog(_PasswordWindow):
    """The secret notes' password: to open one (titled title), to make a note
    one of them, before setting a new one, or before making a new recovery key.
    Forgotten, the recovery key opens them and a new password is set
    (recovered). key holds the key once accepted."""

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
        self.recovered = False
        self.password = _password_field()
        self.password_label = QLabel()
        self.password_label.setBuddy(self.password)
        self.form.addRow(self.password_label, self.password)
        self.forgot_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.ActionRole)
        self.forgot_button.clicked.connect(self.use_recovery_key)
        self.forgot_button.setVisible(passwords.recovery is not None)
        self.finish_layout()
        self.retranslate()
        self.password.setFocus()

    @override
    def retranslate(self) -> None:
        super().retranslate()
        titles = {
            "open": self.tr("Show a secret note"),
            "make": self.tr("Make it a secret note"),
            "change": self.tr("Change the secret notes' password"),
            "recovery": self.tr("Make a new recovery key"),
        }
        self.setWindowTitle(titles[self.purpose])
        intros = {
            "open": self.tr("Type the secret notes' password to show “%1”.").replace(
                "%1", self._title
            ),
            "make": self.tr("Type the secret notes' password to make this note one of them."),
            "change": self.tr("Type the secret notes' password you have now."),
            "recovery": self.tr(
                "The new recovery key opens secret notes too: type their password first."
            ),
        }
        self.intro.setText(intros[self.purpose])
        self.password_label.setText(self.tr("Secret notes' &password:"))
        self.password.setAccessibleName(self.tr("Secret notes' password"))
        self.forgot_button.setText(self.tr("I &forgot the password"))
        self.ok_button.setText(self.tr("Show") if self.purpose == "open" else self.tr("Next"))

    def use_recovery_key(self) -> None:
        """Forgotten: the recovery key opens them, and a new password is set
        before anything else (the old one no longer opens them)."""
        opened: list[bytearray] = []
        dialog: EnterRecoveryKeyDialog

        def submit(typed: str) -> str | None:
            try:
                opened.append(bytearray(self._passwords.open_with_recovery_key(typed)))
            except RecoveryKeyTypoError:
                return dialog.typo()
            except WrongRecoveryKeyError:
                return dialog.wrong_key()
            except NotKeptForRecoveryKeyError:
                return self.tr("This recovery key does not open the secret notes.")
            except (KeyFileError, OSError, RuntimeError, MemoryError) as error:
                log.error("secret notes not opened with the recovery key: %s", type(error).__name__)
                return self.tr("The secret notes' key could not be used (%1).").replace(
                    "%1", type(error).__name__
                )
            return None

        dialog = EnterRecoveryKeyDialog(submit, self)
        if not ask_recovery_key(dialog) or not opened:
            return
        key = opened.pop()
        try:
            if not ask_new_password(
                NewSecretPasswordDialog(self._passwords, bytes(key), self, reset=True)
            ):
                return  # opened only once a new password is set
            self.recovered = True
            self.key = bytes(key)
        finally:
            _wipe(key)
        QDialog.accept(self)

    @override
    def accept(self) -> None:
        self.working()
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            key = self._passwords.open(self.password.text())
        except WrongPasswordError:
            self.password.clear()
            self.show_error(self.tr("The password is not correct."), self.password)
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
        self.key = key
        super().accept()


class NewSecretPasswordDialog(_PasswordWindow):
    """A new password for the secret notes, their key known: opened with the
    password, or (reset) with the recovery key, the old one then forgotten."""

    def __init__(
        self,
        passwords: SecretPasswords,
        key: bytes,
        parent: QWidget | None = None,
        reset: bool = False,
    ) -> None:
        super().__init__(parent)
        self._passwords = passwords
        self.reset = reset
        self.held_key = key  # let go of once the window closes
        self.password = _password_field()
        self.password_label = QLabel()
        self.password_label.setBuddy(self.password)
        self.confirm = _password_field()
        self.confirm_label = QLabel()
        self.confirm_label.setBuddy(self.confirm)
        self.form.addRow(self.password_label, self.password)
        self.form.addRow(self.confirm_label, self.confirm)
        self.add_rules()
        self.finish_layout()
        self.retranslate()
        self.password.setFocus()

    @override
    def retranslate(self) -> None:
        super().retranslate()
        self.setWindowTitle(self.tr("New secret notes' password"))
        self.rules.setText(self.rules_text())
        self.intro.setText(
            self.tr(
                "The recovery key opened your secret notes. Choose a new password for"
                " them: the old one no longer opens them."
            )
            if self.reset
            else self.tr("Choose a new password for all your secret notes.")
        )
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
            if self.reset:
                self._passwords.reset_password(self.held_key, self.password.text())
            else:
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
        self.intro.setText(self.tr("The note is shown by this title while its text is covered."))
        self.title_label.setText(self.tr("&Title:"))
        self.title.setAccessibleName(self.tr("Title"))
        self.title.setPlaceholderText(self.tr("Secret note"))
        self.note.setText(
            self.tr("The title shows even while the text is covered. Do not put the text in it.")
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
            self.tr(
                "Their text stays encrypted with the secret notes' password, and the titles"
                " typed for them can be read. Anyone who has the files and guesses that"
                " password can read the text too."
            )
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
ask_recovery_key: Callable[[EnterRecoveryKeyDialog], bool] = _exec  # replaced in tests
show_recovery_key: Callable[[RecoveryKeyDialog], bool] = _exec  # replaced in tests


def _confirm_new_recovery_key(parent: QWidget) -> bool:
    answer = QMessageBox.question(
        parent,
        "Stickle",
        QCoreApplication.translate(
            "SettingsWindow",
            "Make a new recovery key? The one you have now will no longer open your notes.",
        ),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


confirm_new_recovery_key: Callable[[QWidget], bool] = _confirm_new_recovery_key  # replaced in tests
