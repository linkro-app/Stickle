"""The recovery key on screen: typing it in to get back in, and showing a new one."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import override

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QKeyEvent,
    QPainter,
    QPixmap,
)
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
from stickle.app.typed import forget_typed
from stickle.platform.clipboard import copy_secret

# Returns an error to show, or None when the key was accepted.
type Submit = Callable[[str], str | None]


def _no_upgrade(_typed: str) -> None:
    raise NotImplementedError


@dataclass(frozen=True)
class RecoveryKeys:
    """Making the recovery key, without the windows knowing the notes' keys."""

    exists: Callable[[], bool]
    make: Callable[[], str]  # a new one, the old one no longer working
    kept: Callable[[], None]  # the user said the new one is kept somewhere safe
    made_at: Callable[[], str | None] = lambda: None  # when (UTC); unknown before 0.6
    was_kept: Callable[[], bool] = lambda: True  # the user said they kept it (not "Later")
    # With secret notes: a new one their key (opened by the user) is sealed for too.
    make_with_secret: Callable[[bytes], str] | None = None
    # Made before 0.6: nothing can be sealed for it until it is typed once, which
    # upgrade does (RecoveryKeyTypoError, WrongRecoveryKeyError).
    is_old: Callable[[], bool] = lambda: False
    upgrade: Callable[[str], None] = _no_upgrade


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
        self.master_button.setText(self.tr("Use the s&pare password instead"))

    @override
    def done(self, result: int, /) -> None:
        super().done(result)
        forget_typed(self, self.key)

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


QR_MODULE = 4  # pixels for each square of the code
QR_BORDER = 4  # squares of white around it, as readers need


def qr_squares(text: str) -> list[list[bool]]:
    """The QR code of text (error correction M), its white border included:
    True for each dark square."""
    import segno  # only when a recovery key is shown, not at every start

    code = segno.make_qr(text, error="m")
    return [[bool(square) for square in row] for row in code.matrix_iter(border=QR_BORDER)]


def qr_pixmap(text: str) -> QPixmap:
    """Black on white whatever the colours of the desktop: a dark theme's light
    squares on dark were not read by phones."""
    squares = qr_squares(text)
    pixmap = QPixmap(len(squares) * QR_MODULE, len(squares) * QR_MODULE)
    pixmap.fill(QColor("white"))
    painter = QPainter(pixmap)
    for y, row in enumerate(squares):
        for x, dark in enumerate(row):
            if dark:
                painter.fillRect(
                    x * QR_MODULE, y * QR_MODULE, QR_MODULE, QR_MODULE, QColor("black")
                )
    painter.end()
    return pixmap


class RecoveryKeyPanel(QWidget):
    """A new recovery key, with ways to keep it; used in its own window and at first start.
    Beside the characters, the same key as a QR code to keep on a phone: it holds
    the key alone (no address), so scanning it opens nothing."""

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
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if recovery_key:
            self.qr.setPixmap(qr_pixmap(recovery_key))
        self.qr_note = QLabel()
        self.qr_note.setWordWrap(True)
        self.qr_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
        layout.addWidget(self.qr)
        layout.addWidget(self.qr_note)
        layout.addLayout(actions)
        layout.addWidget(self.status)
        layout.addWidget(self.kept)
        self.retranslate()

    def retranslate(self) -> None:
        self.intro.setText(
            self.tr(
                "If the key that opens your notes is ever lost (a new computer, a reset "
                "keychain, a forgotten password), this recovery key opens them again, "
                "secret notes included. Stickle does not keep it: write it down or save it "
                "somewhere safe, away from this computer. Avoid places that may go up by "
                "themselves to the cloud you have set up, such as a photo app or a folder "
                "synced with OneDrive, iCloud, Google Drive or Dropbox. Anyone with it can "
                "read your notes."
            )
        )
        self.key.setAccessibleName(self.tr("Recovery key"))
        self.qr.setAccessibleName(self.tr("The recovery key as a QR code"))
        self.qr_note.setText(
            self.tr(
                "The same key as a QR code: scan it into a password manager on your phone."
                " Do not take a photo of it, as photos may go up by themselves to the cloud"
                " you have set up. It holds only the key: scanning it opens nothing."
            )
        )
        self.copy_button.setText(self.tr("&Copy"))
        self.save_button.setText(self.tr("&Save to a file…"))
        self.kept.setText(self.tr("I have &kept the recovery key somewhere safe"))

    def forget(self) -> None:
        """The key shown no longer kept, once its window is closed."""
        self.recovery_key = ""
        self.key.clear()
        self.qr.clear()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def _copy(self) -> None:
        # Left out of clipboard histories and the cloud clipboard, where the system reads it.
        copy_secret(self.recovery_key)
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
        self.status.setText(
            self.tr(
                "Saved. Move the file off this computer, to a USB stick for instance, and"
                " not into a folder synced with the cloud."
            )
        )


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
    def done(self, result: int, /) -> None:
        super().done(result)
        self.panel.forget()  # the window lives on, kept by the one it belongs to

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)
