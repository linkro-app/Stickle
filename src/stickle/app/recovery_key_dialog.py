"""The recovery key on screen: typing it in to get back in, and showing a new one."""

from collections.abc import Callable
from pathlib import Path
from typing import override

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from stickle.app.file_dialogs import save_file_name
from stickle.app.sizing import grow_to_fit

# Returns an error to show, or None when the key was accepted.
type Submit = Callable[[str], str | None]


class EnterRecoveryKeyDialog(QDialog):
    """Asks for the recovery key; submit decides whether it opens the notes.
    Where a master password was set, it can be used instead: the window then
    closes with use_master set."""

    def __init__(
        self, submit: Submit, parent: QWidget | None = None, can_use_master: bool = False
    ) -> None:
        super().__init__(parent)
        self._submit = submit
        self.use_master = False
        self.intro = QLabel()
        self.intro.setWordWrap(True)
        self.label = QLabel()
        self.key = QLineEdit()
        self.key.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.label.setBuddy(self.key)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #b3261e;")
        self.error.hide()
        self.buttons = QDialogButtonBox()
        self.ok_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.RejectRole)
        self.ok_button.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.master_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.ActionRole)
        self.master_button.setVisible(can_use_master)
        self.master_button.clicked.connect(self._use_master)
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro)
        layout.addWidget(self.label)
        layout.addWidget(self.key)
        layout.addWidget(self.error)
        layout.addWidget(self.buttons)
        self.setMinimumWidth(460)
        self.retranslate()
        self.key.setFocus()

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Open with the recovery key"))
        self.intro.setText(
            self.tr(
                "Type the recovery key you were given when you started using Stickle. "
                "Letter case, spaces and dashes do not matter."
            )
        )
        self.label.setText(self.tr("&Recovery key:"))
        self.key.setAccessibleName(self.tr("Recovery key"))
        self.ok_button.setText(self.tr("Open notes"))
        self.cancel_button.setText(self.tr("Back"))
        self.master_button.setText(self.tr("Use the &master password instead"))

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def _use_master(self) -> None:
        self.use_master = True
        self.reject()

    def typo(self) -> str:
        return self.tr(
            "This is not a recovery key as written: a character may be missing, extra or "
            "mistyped. Please check it again."
        )

    def wrong_key(self) -> str:
        return self.tr("This recovery key does not open these notes.")

    def no_recovery_key(self) -> str:
        return self.tr("No recovery key was made for these notes.")

    def show_error(self, message: str) -> None:
        self.error.setText(message)
        self.error.show()
        grow_to_fit(self)
        self.key.setAccessibleDescription(message)
        self.key.setFocus()
        self.key.selectAll()

    @override
    def accept(self) -> None:
        # Unwrapping takes a moment on purpose (it slows down guessing).
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            message = self._submit(self.key.text())
        finally:
            QGuiApplication.restoreOverrideCursor()
        if message is not None:
            self.show_error(message)
            return
        super().accept()


class KeptBox(QCheckBox):
    """ "I have kept it": Space ticks it, Enter goes on to the window's Done.

    Some desktops (GNOME) have Enter tick a check box too: ticked with its key
    and then Enter, it would be unticked again instead of finishing.
    """

    ENTER_KEYS = (Qt.Key.Key_Return, Qt.Key.Key_Enter)

    @override
    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() in self.ENTER_KEYS:
            e.ignore()  # on to the window, whose default button is Done once ticked
            return
        super().keyPressEvent(e)

    @override
    def keyReleaseEvent(self, e: QKeyEvent) -> None:
        if e.key() in self.ENTER_KEYS:
            e.ignore()
            return
        super().keyReleaseEvent(e)


class RecoveryKeyPanel(QWidget):
    """A new recovery key, with ways to keep it; used in its own window and at first start."""

    def __init__(self, recovery_key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.recovery_key = recovery_key
        self.intro = QLabel()
        self.intro.setWordWrap(True)
        self.key = QLabel(recovery_key)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSizeF(font.pointSizeF() * 1.3)
        font.setWeight(QFont.Weight.Bold)
        self.key.setFont(font)
        self.key.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.key.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.copy_button = QPushButton()
        self.copy_button.clicked.connect(self._copy)
        self.save_button = QPushButton()
        self.save_button.clicked.connect(self._save)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.kept = KeptBox()

        actions = QHBoxLayout()
        actions.addWidget(self.copy_button)
        actions.addWidget(self.save_button)
        actions.addStretch()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.intro)
        layout.addWidget(self.key)
        layout.addLayout(actions)
        layout.addWidget(self.status)
        layout.addWidget(self.kept)
        self.retranslate()

    def retranslate(self) -> None:
        self.intro.setText(
            self.tr(
                "If the key that opens your notes is ever lost (a new computer, a reset "
                "keychain, a forgotten password), this recovery key opens them again. "
                "Stickle does not keep it: write it down or save it somewhere safe, away "
                "from this computer. Anyone with it can read your notes."
            )
        )
        self.key.setAccessibleName(self.tr("Recovery key"))
        self.copy_button.setText(self.tr("&Copy"))
        self.save_button.setText(self.tr("&Save to a file…"))
        self.kept.setText(self.tr("I have &kept the recovery key somewhere safe"))

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def _copy(self) -> None:
        clipboard = QGuiApplication.clipboard()
        clipboard.setText(self.recovery_key)
        self.status.setText(self.tr("Copied. Paste it somewhere safe, then clear the clipboard."))

    def _save(self) -> None:
        name = save_file_name(
            self,
            self.tr("Save the recovery key"),
            Path.home() / "Stickle recovery key.txt",
            self.tr("Text files (*.txt)"),
        )
        if name is None:
            return
        text = self.tr("Stickle recovery key: %1").replace("%1", self.recovery_key)
        try:
            Path(name).write_text(text + "\n", encoding="utf-8")
        except OSError:
            self.status.setText(self.tr("The file could not be saved."))
            return
        self.status.setText(self.tr("Saved. Keep the file away from this computer."))


class RecoveryKeyDialog(QDialog):
    """Shows a new recovery key: Done once it is kept, or Later."""

    def __init__(self, recovery_key: str, parent: QWidget | None = None, reason: str = "") -> None:
        super().__init__(parent)
        # Why it is shown now, when Stickle offers it rather than the user asking.
        self.reason = QLabel(reason)
        self.reason.setWordWrap(True)
        self.reason.setVisible(bool(reason))
        self.panel = RecoveryKeyPanel(recovery_key, self)
        self.buttons = QDialogButtonBox()
        self.done_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.AcceptRole)
        self.later_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.RejectRole)
        self.done_button.setEnabled(False)
        self.panel.kept.toggled.connect(self.done_button.setEnabled)
        # Kept: Enter is Done, wherever the desktop puts Done and Later.
        self.panel.kept.toggled.connect(self.done_button.setDefault)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.reason)
        layout.addWidget(self.panel)
        layout.addWidget(self.buttons)
        self.setMinimumWidth(480)
        self.retranslate()

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Your recovery key"))
        self.done_button.setText(self.tr("Done"))
        self.later_button.setText(self.tr("Later"))

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)
