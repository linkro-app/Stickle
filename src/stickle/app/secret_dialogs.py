"""The windows of secret notes: turning them on, opening one, a new password.

Secret notes have one password for all of them; forgotten, the recovery key
opens them and a new one is set. Turning them on the first time sets the
password alone, with the date the recovery key was made (someone else may
have made it) and a way to make a new one; a recovery key from before 0.6 is
typed once first, or one is made where there is none. Each secret note opens
on its own, with the password.
"""

import contextlib
import itertools
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Literal, override

import apsw
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

from stickle.app.error_text import error_style
from stickle.app.master_password import MasterPasswords, weakness_reason
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
from stickle.crypto.secret_notes import (
    MIN_PASSWORD_LENGTH,
    NotKeptForRecoveryKeyError,
    SecretNoteError,
)
from stickle.unlock import password_length

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Matching:
    """This computer's secret notes' key, and the others' kept beside it until
    their passwords are matched (stickle.crypto.secret_notes)."""

    target: Callable[[], str | None]  # the others' key's number; None: nothing to match
    number_here: Callable[[], str | None]  # this computer's key's
    sealed_here: Callable[[], int]  # notes holding a text sealed with this computer's key
    keeps_old_keys: Callable[[], bool]  # whether this computer's key carries keys of before
    # This computer's key, nothing written: by its password (WrongPasswordError),
    # by the recovery key it is sealed for (as SecretPasswords.open_with_recovery_key).
    open_here: Callable[[str], bytes]
    open_here_with_recovery_key: Callable[[str], bytes]
    # Whether this computer's key is sealed for a recovery key B no longer has:
    # None if not, else whether it is the one from before sync was turned on.
    aside: Callable[[], bool | None]
    open_other: Callable[[str, str], bytes]  # their number and password: their key
    open_other_with_recovery_key: Callable[[str, str], bytes]  # their number, the recovery key
    # Matched: this computer's key (None: nothing is sealed with it), their number
    # and key; how many notes were sealed again.
    match: Callable[[bytes | None, str, bytes], int]
    not_matched: Callable[[bytes], object]  # this computer's key opened, kept as it is
    # Their key this computer's in its key file, this computer's kept among the old.
    keep_matched: Callable[[bytes | None, str, bytes], object] = lambda _own, _number, _key: None
    # Keys other computers used apart kept beside: by number, the name of the
    # one that used it (None: not known).
    apart: Callable[[], Mapping[str, str | None]] = lambda: {}
    # This computer's key, the number of one used apart and that key: the notes
    # sealed with it sealed again with this computer's; how many.
    adopt: Callable[[bytes, str, bytes], int] = lambda _own, _number, _key: 0
    # That key among the old ones in the key file, no longer kept beside.
    keep_adopted: Callable[[bytes, str, bytes], object] = lambda _own, _number, _key: None


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
    # Whether they still open with a recovery key this computer no longer has
    # (until opened with their password, then sealed for the one it has): None
    # if not, else whether it is the one from before sync was turned on (False:
    # the one before a new one made on another computer was taken).
    aside: Callable[[], bool | None] = lambda: None
    matching: Matching | None = None  # on a computer that syncs, once it has one
    # The keys they were sealed with before (matched since), opened with theirs of now.
    old_keys: Callable[[bytes], Mapping[str, bytes]] = lambda _key: {}


@dataclass(frozen=True)
class Opened:
    """A secret notes' key opened: this computer's (number None), or the others'
    while the passwords are not matched (its number)."""

    key: bytes
    number: str | None = None


# The windows that open them to do something with them match the passwords
# first; going back to a backup and bringing notes in use this computer's key.
MATCHED: frozenset[str] = frozenset({"open", "make", "change", "recovery"})


def _password_field() -> QLineEdit:
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    return field


def _error_label() -> QLabel:
    label = QLabel()
    label.setWordWrap(True)
    label.setStyleSheet(error_style(label))
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
        self.message.setStyleSheet(error_style(self.message))
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
        self.type_button.clicked.connect(self.check_recovery_key)
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
        self.type_button.setText(self.tr("&Check my existing recovery key…"))
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
                "Stickle doesn't know yet whether you kept a recovery key. If you forget"
                " the password, only the recovery key sets a new one: check the one you"
                " have, or make a new one and keep it away from this computer, then turn"
                " them on."
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
        self.type_button.setVisible(state in ("old", "not_kept"))
        self.make_button.setVisible(recovery is not None)
        self.make_button.setText(
            self.tr("&Make a recovery key…")
            if state == "none"
            else self.tr("&Make a new recovery key…")
        )
        self.ok_button.setEnabled(state == "ready")
        self.ok_button.setAccessibleDescription(line if state != "ready" else "")

    def check_recovery_key(self) -> None:
        """The recovery key the user has, typed: one from before 0.6 opens secret
        notes too from now on, and one not known to be kept counts as kept. A
        wrong one changes nothing."""
        recovery = self._passwords.recovery
        if recovery is None:
            return
        take = recovery.upgrade if self.recovery_state() == "old" else recovery.confirm
        dialog: EnterRecoveryKeyDialog

        def submit(typed: str) -> str | None:
            try:
                take(typed)
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


type Purpose = Literal["open", "make", "change", "recovery", "restore", "import"]


class OpenSecretDialog(_PasswordWindow):
    """The secret notes' password: to open one (titled title), to make a note
    one of them, before setting a new one, before making a new recovery key, or
    to go back to a backup holding the earlier text of count of them (accepted
    without it, they stay as they are now). Forgotten, the recovery key opens
    them and a new password is set (recovered). key holds the key once accepted
    with it, key_number which one it is (None: this computer's).

    With the others' key kept beside this computer's (Matching), the password
    is tried on both: both open, the passwords are matched at once; one only,
    the other is asked for (MatchSecretDialog). Later, the one that opened is
    held, this computer's or theirs (only to open or make a note: changing the
    password or making a new recovery key needs this computer's).

    Opening a note sealed with a key another computer used apart (sealed_with,
    kept beside), that key is asked for next, its notes then sealed again with
    this computer's; or, the password typed being that key's only, the note
    opens with it (key_number its number)."""

    def __init__(
        self,
        passwords: SecretPasswords,
        title: str = "",
        parent: QWidget | None = None,
        purpose: Purpose = "open",
        count: int = 0,
        sealed_with: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._passwords = passwords
        self._sealed_with = sealed_with
        self._title = title
        self._count = count
        self.purpose: Purpose = purpose
        self.recovered = False
        self.key_number: str | None = None
        self.password = _password_field()
        self.password_label = QLabel()
        self.password_label.setBuddy(self.password)
        self.form.addRow(self.password_label, self.password)
        self.forgot_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.ActionRole)
        self.forgot_button.clicked.connect(self.use_recovery_key)
        self.forgot_button.setVisible(passwords.recovery is not None)
        self.without_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.ActionRole)
        self.without_button.clicked.connect(self.go_on_without)
        self.without_button.setVisible(purpose == "restore")
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
            "restore": self.tr("Restore from a backup"),
            "import": self.tr("Bring in secret notes"),
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
            "restore": self.tr(
                "This backup holds the earlier text of %Ln note(s) made secret since. Type"
                " the secret notes' password to bring that text back, still secret."
                " Without it, those notes stay as they are now.",
                "",
                self._count,
            ),
            "import": self.tr(
                "Type the secret notes' password of this computer: the %Ln secret note(s)"
                " brought in are locked with it.",
                "",
                self._count,
            ),
        }
        self.intro.setText(intros[self.purpose])
        self.password_label.setText(self.tr("Secret notes' &password:"))
        self.password.setAccessibleName(self.tr("Secret notes' password"))
        self.forgot_button.setText(self.tr("I &forgot the password"))
        self.without_button.setText(self.tr("Restore &without that text"))
        oks = {"open": self.tr("Show"), "restore": self.tr("&Restore")}
        self.ok_button.setText(oks.get(self.purpose, self.tr("Next")))

    def go_on_without(self) -> None:
        """Gone back to without their password: those notes stay as they are now."""
        self.key = None
        QDialog.accept(self)

    def _matching(self) -> tuple[Matching, str] | None:
        """The others' key to match with, if there is one and this window matches."""
        matching = self._passwords.matching
        if matching is None or self.purpose not in MATCHED:
            return None
        number = matching.target()
        return None if number is None else (matching, number)

    def _apart(self) -> tuple[Matching, str, str | None] | None:
        """The key used apart the note being opened is sealed with, if it is kept
        beside: with the name of the computer that used it."""
        matching = self._passwords.matching
        number = self._sealed_with
        if matching is None or number is None or self.purpose != "open":
            return None
        apart = matching.apart()
        return None if number not in apart else (matching, number, apart[number])

    def _move_apart(self, typed: str | None, own: bytes) -> None:
        """The notes sealed with the key used apart sealed again with this
        computer's (own): by the password typed, if it is that key's too, else
        by the one (or the recovery key) asked for. Later, they stay as they are."""
        apart = self._apart()
        if apart is None:
            return
        matching, number, by = apart
        key = None
        if typed:
            with contextlib.suppress(WrongPasswordError, KeyFileError):
                key = matching.open_other(number, typed)
        if key is None:
            dialog = MatchSecretDialog(
                "apart",
                0,
                partial(matching.open_other, number),
                partial(matching.open_other_with_recovery_key, number),
                None,
                self,
                by,
            )
            key = dialog.take_key() if ask_match(dialog) else None
        if key is None:
            return
        try:
            matching.adopt(own, number, key)
        except (KeyFileError, OSError, RuntimeError, SecretNoteError, apsw.Error) as error:
            log.error("secret notes of a key used apart not moved: %s", type(error).__name__)
            QMessageBox.warning(
                self,
                "Stickle",
                QCoreApplication.translate(
                    "MatchSecretDialog", "The secret notes' passwords could not be matched (%1)."
                ).replace("%1", type(error).__name__),
            )

    def _take(self, opened: Opened) -> bool:
        """Whether this window can do what it is for with the key opened."""
        if opened.number is not None and self.purpose not in ("open", "make"):
            return False  # their key alone: this computer's is the one changed
        self.key, self.key_number = opened.key, opened.number
        return True

    def use_recovery_key(self) -> None:
        """Forgotten: the recovery key opens them, and a new password is set
        before anything else (the old one no longer opens them)."""
        opened: list[bytearray] = []
        both: list[tuple[bytes | None, bytes | None]] = []
        matching = self._matching()
        dialog: EnterRecoveryKeyDialog

        def open_both(typed: str) -> None:
            assert matching is not None
            with_it, number = matching
            here = there = None
            refused: Exception | None = None
            try:
                here = with_it.open_here_with_recovery_key(typed)
            except (WrongRecoveryKeyError, NotKeptForRecoveryKeyError) as error:
                refused = error
            try:
                there = with_it.open_other_with_recovery_key(number, typed)
            except (WrongRecoveryKeyError, NotKeptForRecoveryKeyError) as error:
                refused = refused or error
            if here is None and there is None:
                assert refused is not None
                raise refused
            both.append((here, there))

        def submit(typed: str) -> str | None:
            try:
                if matching is None:
                    opened.append(bytearray(self._passwords.open_with_recovery_key(typed)))
                else:
                    open_both(typed)
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
        ask_for_the_old_one(dialog, self._passwords.aside())
        if not ask_recovery_key(dialog):
            return
        if matching is not None:
            if not both:
                return
            here, there = both.pop()
            settled = settle(self, self._passwords, *matching, here, there, recovered=True)
            if settled is None or not self._take(settled):
                return
            self.recovered = True
            if settled.number is None:
                self._move_apart(None, settled.key)
            QDialog.accept(self)
            return
        if not opened:
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
        self._move_apart(None, self.key)
        QDialog.accept(self)

    def _open_both(self, typed: str, matching: Matching, number: str) -> Opened | None:
        """This computer's key and theirs, by the password typed (WrongPasswordError
        if it is neither's): what the passwords came to (None: nothing to hold)."""
        here = there = None
        with contextlib.suppress(WrongPasswordError):
            here = matching.open_here(typed)
        with contextlib.suppress(WrongPasswordError):
            there = matching.open_other(number, typed)
        if here is None and there is None:
            raise WrongPasswordError("neither's password")
        QGuiApplication.restoreOverrideCursor()
        try:
            return settle(self, self._passwords, matching, number, here, there)
        finally:
            QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

    @override
    def accept(self) -> None:
        self.working()
        matching, apart = self._matching(), self._apart()
        typed = self.password.text()
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        settled: Opened | None = None
        try:
            try:
                if matching is None:
                    settled = Opened(self._passwords.open(typed))
                else:
                    settled = self._open_both(typed, *matching)
            except WrongPasswordError:
                if apart is None:
                    raise
                # That key's password alone: the note opens with it, nothing moved.
                settled = Opened(apart[0].open_other(apart[1], typed), apart[1])
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
        if settled is None or not self._take(settled):
            self.reject()
            return
        if settled.number is None:
            self._move_apart(typed, settled.key)
        super().accept()


class ExportedSecretsDialog(_PasswordWindow):
    """The secret notes of an export, locked as they went out: their key of that
    day, opened with their password then or (exported since 0.6) the recovery key
    then. key holds it once accepted; accepted without it (left_out), they are
    left out, to come in another time. typed: the password that opened it, to be
    tried on the secret notes here before asking for theirs."""

    def __init__(
        self,
        count: int,
        with_password: Callable[[str], Sequence[bytes]],
        with_recovery_key: Callable[[str], Sequence[bytes]] | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._count = count
        self.keys: tuple[bytes, ...] = ()  # every key opened, the first in key
        self._with_password = with_password
        self._with_recovery_key = with_recovery_key
        self.left_out = False
        self.typed: str | None = None
        self.password = _password_field()
        self.password_label = QLabel()
        self.password_label.setBuddy(self.password)
        self.form.addRow(self.password_label, self.password)
        self.recovery_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.ActionRole)
        self.recovery_button.clicked.connect(self.use_recovery_key)
        self.recovery_button.setVisible(with_recovery_key is not None)
        self.without_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.ActionRole)
        self.without_button.clicked.connect(self.go_on_without)
        self.finish_layout()
        self.retranslate()
        self.password.setFocus()

    @override
    def retranslate(self) -> None:
        super().retranslate()
        self.setWindowTitle(self.tr("Bring in secret notes"))
        self.intro.setText(
            self.tr(
                "%Ln secret note(s) went out locked. Type the secret notes' password of the"
                " day they were exported, or use the recovery key of that day.",
                "",
                self._count,
            )
        )
        self.password_label.setText(self.tr("Their &password then:"))
        self.password.setAccessibleName(self.tr("The secret notes' password of that day"))
        self.recovery_button.setText(self.tr("Use the &recovery key"))
        self.without_button.setText(self.tr("Bring in &without them"))
        self.ok_button.setText(self.tr("Next"))

    def go_on_without(self) -> None:
        """Without them: the other notes come in, these another time."""
        self.key = None
        self.left_out = True
        QDialog.accept(self)

    def use_recovery_key(self) -> None:
        if self._with_recovery_key is None:
            return
        opened = self._with_recovery_key
        found: list[tuple[bytes, ...]] = []
        dialog: EnterRecoveryKeyDialog

        def submit(typed: str) -> str | None:
            try:
                found.append(tuple(opened(typed)))
            except RecoveryKeyTypoError:
                return dialog.typo()
            except WrongRecoveryKeyError:
                return dialog.wrong_key()
            except NotKeptForRecoveryKeyError:
                return self.tr("This recovery key does not open those secret notes.")
            except (KeyFileError, OSError, RuntimeError, MemoryError) as error:
                log.error("exported secret notes not opened: %s", type(error).__name__)
                return self.tr("The secret notes' key could not be used (%1).").replace(
                    "%1", type(error).__name__
                )
            return None

        dialog = EnterRecoveryKeyDialog(submit, self)
        if not ask_recovery_key(dialog) or not found:
            return
        self.keys = found.pop()
        self.key = self.keys[0]
        QDialog.accept(self)

    @override
    def accept(self) -> None:
        typed = self.password.text()
        self.working()
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            keys = tuple(self._with_password(typed))
        except WrongPasswordError:
            self.password.clear()
            self.show_error(self.tr("The password is not correct."), self.password)
            return
        except (KeyFileError, OSError, RuntimeError, MemoryError) as error:
            log.error("exported secret notes not opened: %s", type(error).__name__)
            self.show_error(
                self.tr("The secret notes' key could not be used (%1).").replace(
                    "%1", type(error).__name__
                ),
                self.password,
            )
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.keys, self.typed = keys, typed
        self.key = keys[0]
        super().accept()

    def take_keys(self) -> tuple[bytes, ...]:
        """The keys opened, handed over once."""
        keys, self.keys, self.key = self.keys, (), None
        return keys


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

    def __init__(
        self, title: str = "", parent: QWidget | None = None, making: bool = False
    ) -> None:
        super().__init__(parent)
        self.title = QLineEdit(title)
        self.title_label = QLabel()
        self.title_label.setBuddy(self.title)
        self.form.addRow(self.title_label, self.title)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.layout_parts.addWidget(self.note)
        # Made secret now: what Stickle cannot reach keeps the text as it is.
        self.copies = QLabel()
        self.copies.setWordWrap(True)
        self.copies.setVisible(making)
        self.layout_parts.addWidget(self.copies)
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
        self.copies.setText(
            self.tr(
                "Copies Stickle cannot change still hold the text as it is now: files"
                " exported before, folders a cloud service syncs, and your computer's own"
                " backups (such as File History)."
            )
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
        self,
        count: int,
        open_notes: Callable[[str], Sequence[bytes]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._count = count
        self._open = open_notes
        self.keys: tuple[bytes, ...] = ()
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
            self.tr("How should the %Ln secret note(s) be exported?", "", self._count)
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
            keys = tuple(self._open(self.password.text()))
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
        self.keys = keys
        super().accept()

    def take_keys(self) -> tuple[bytes, ...]:
        """The keys opened, handed over once."""
        keys, self.keys = self.keys, ()
        return keys


type MatchKind = Literal["here", "there", "apart"]


class MatchSecretDialog(_PasswordWindow):
    """One password opened, the other asked for, to match them: here, this
    computer's opened, the others' is asked for; there, theirs opened, this
    computer's (count: the notes sealed with it); apart, a note sealed with a
    key the computer named by used apart, that key's. Switched, key holds the
    key the password (or, recovered, the recovery key) opened; Later, none."""

    def __init__(
        self,
        kind: MatchKind,
        count: int,
        with_password: Callable[[str], bytes],
        with_recovery_key: Callable[[str], bytes],
        aside: bool | None = None,
        parent: QWidget | None = None,
        by: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.kind: MatchKind = kind
        self._by = by
        self._count = count
        self._with_password = with_password
        self._with_recovery_key = with_recovery_key
        self._aside = aside  # there: which recovery key this computer's key is sealed for
        self.recovered = False
        self.password = _password_field()
        self.password_label = QLabel()
        self.password_label.setBuddy(self.password)
        # Above its field, across the window, wrapped if need be: a computer's
        # name may be long.
        self.password_label.setWordWrap(True)
        self.form.addRow(self.password_label)
        self.form.addRow(self.password)
        self.forgot_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.ActionRole)
        self.forgot_button.clicked.connect(self.use_recovery_key)
        self.finish_layout()
        self.retranslate()
        self.password.setFocus()

    @override
    def retranslate(self) -> None:
        super().retranslate()
        self.setWindowTitle(self.tr("Match secret note passwords"))
        if self.kind == "apart":
            by = self._by or self.tr("another computer")
            intro = self.tr(
                "This secret note is locked with the password %1 used for its own secret"
                " notes. Enter it, and every secret note locked with it switches to your"
                " current password and opens everywhere."
            ).replace("%1", by)
            label = self.tr("The &password %1 used for its secret notes:").replace("%1", by)
            name = self.tr("The password %1 used for its secret notes").replace("%1", by)
        elif self.kind == "there":
            intro = self.tr(
                "%Ln secret note(s) on this computer are locked with a different password, so"
                " they don't go to your other computers. Enter it, and they'll switch to your"
                " current password and open everywhere.",
                "",
                self._count,
            )
            label = self.tr("The &password this computer used for them:")
            name = self.tr("The password this computer used for them")
        else:
            intro = (
                self.tr(
                    "Your other computers' secret notes are locked with their password. Enter"
                    " it, and the %Ln secret note(s) here will switch to it and open everywhere."
                    " This computer will use that password from now on.",
                    "",
                    self._count,
                )
                if self._count
                else self.tr(
                    "Your other computers' secret notes are locked with their password. Enter"
                    " it, and this computer will use that password from now on."
                )
            )
            label = self.tr("Secret notes' &password of the computers that already sync:")
            name = self.tr("Secret notes' password of the computers that already sync")
        self.intro.setText(intro)
        self.password_label.setText(label)
        self.password.setAccessibleName(name)
        self.forgot_button.setText(self.tr("I &forgot"))
        self.ok_button.setText(self.tr("&Switch"))
        self.cancel_button.setText(self.tr("&Later"))

    def use_recovery_key(self) -> None:
        """Forgotten: the recovery key it is sealed for opens it."""
        found: list[bytes] = []
        dialog: EnterRecoveryKeyDialog

        def submit(typed: str) -> str | None:
            try:
                found.append(self._with_recovery_key(typed))
            except RecoveryKeyTypoError:
                return dialog.typo()
            except WrongRecoveryKeyError:
                return dialog.wrong_key()
            except NotKeptForRecoveryKeyError:
                return self.tr("This recovery key does not open those secret notes.")
            except (KeyFileError, OSError, RuntimeError, MemoryError) as error:
                log.error("secret notes not opened to match: %s", type(error).__name__)
                return self.tr("The secret notes' key could not be used (%1).").replace(
                    "%1", type(error).__name__
                )
            return None

        dialog = EnterRecoveryKeyDialog(submit, self)
        if self.kind == "there":
            ask_for_the_old_one(dialog, self._aside)
        if not ask_recovery_key(dialog) or not found:
            return
        self.key = found.pop()
        self.recovered = True
        QDialog.accept(self)

    @override
    def accept(self) -> None:
        self.working()
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            key = self._with_password(self.password.text())
        except WrongPasswordError:
            self.password.clear()
            self.show_error(self.tr("The password is not correct."), self.password)
            return
        except (KeyFileError, OSError, RuntimeError, MemoryError) as error:
            log.error("secret notes not opened to match: %s", type(error).__name__)
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


def ask_for_the_old_one(dialog: EnterRecoveryKeyDialog, aside: bool | None) -> None:
    """The recovery key window says which, for a key sealed for an old one."""
    if aside is True:
        dialog.ask_for_the_one_before_sync()
    elif aside is False:
        dialog.ask_for_the_one_before_a_new_one()


def settle(
    parent: QWidget,
    passwords: SecretPasswords,
    matching: Matching,
    number: str,
    here: bytes | None,
    there: bytes | None,
    recovered: bool = False,
) -> Opened | None:
    """What a password (or, recovered, the recovery key) opened comes to, this
    computer's key (here) and the others' numbered number (there): both, the
    passwords are matched; one, the other is asked for, and they are matched
    once it is given — or theirs is taken at once when nothing is sealed with
    this computer's. Recovered, a new password is set for the key held then,
    if it is this computer's (None if it was not: nothing to hold yet)."""
    count = matching.sealed_here()
    held: Opened
    try:
        if here is not None and there is not None:
            matching.match(here, number, there)
            held = Opened(there)
        elif there is not None:
            if count == 0 and not matching.keeps_old_keys():
                matching.match(None, number, there)
                held = Opened(there)
            else:
                dialog = MatchSecretDialog(
                    "there",
                    count,
                    matching.open_here,
                    matching.open_here_with_recovery_key,
                    matching.aside(),
                    parent,
                )
                mine = dialog.take_key() if ask_match(dialog) else None
                if mine is not None:
                    matching.match(mine, number, there)
                    held = Opened(there)
                else:
                    held = Opened(there, number)
        else:
            assert here is not None
            dialog = MatchSecretDialog(
                "here",
                count,
                partial(matching.open_other, number),
                partial(matching.open_other_with_recovery_key, number),
                None,
                parent,
            )
            theirs = dialog.take_key() if ask_match(dialog) else None
            if theirs is not None:
                recovered = recovered or dialog.recovered
                matching.match(here, number, theirs)
                held = Opened(theirs)
            else:
                matching.not_matched(here)
                held = Opened(here)
    except (KeyFileError, OSError, RuntimeError, SecretNoteError, apsw.Error) as error:
        log.error("secret notes' passwords not matched: %s", type(error).__name__)
        QMessageBox.warning(
            parent,
            "Stickle",
            QCoreApplication.translate(
                "MatchSecretDialog", "The secret notes' passwords could not be matched (%1)."
            ).replace("%1", type(error).__name__),
        )
        held = Opened(here) if here is not None else Opened(there or b"", number)
    if (
        recovered
        and held.number is None
        and not ask_new_password(NewSecretPasswordDialog(passwords, held.key, parent, reset=True))
    ):
        return None  # opened only once a new password is set
    return held


def _exec(dialog: QDialog) -> bool:
    return dialog.exec() == QDialog.DialogCode.Accepted


ask_new_password: Callable[[NewSecretPasswordDialog], bool] = _exec  # replaced in tests
ask_match: Callable[[MatchSecretDialog], bool] = _exec  # replaced in tests
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
