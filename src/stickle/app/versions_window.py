"""A note's earlier versions (stickle.data.versions): picked to read, gone
back to, or deleted.

The list reads each one's time, where it came from and its first line, never
its whole text: that is read for the one picked only. A secret note's are
encrypted: while it is covered the list has their times alone, and its text
is shown with the secret notes' password first (as the note's own "Show its
text…"). Going back is left to the note's owner, which saves what the note
held first; this window only says which one.
"""

from collections.abc import Callable
from typing import cast, override

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from stickle.app.in_use import OnTopWhileInUse
from stickle.app.note_view import NoteView
from stickle.app.times import local_time
from stickle.core.colors import note_colors
from stickle.core.markdown import note_title
from stickle.crypto.secret_notes import SecretNoteError
from stickle.data.versions import EarlierVersion, VersionRepository

VERSION_ID = Qt.ItemDataRole.UserRole
ROW_TITLE = 60  # characters of a version's first line in its row


def _confirm(parent: QWidget, question: str, go: str) -> bool:
    box = QMessageBox(QMessageBox.Icon.Question, "Stickle", question, parent=parent)
    yes = box.addButton(go, QMessageBox.ButtonRole.DestructiveRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    return box.clickedButton() is yes


# Asks before deleting (parent, the question, its button): whether to go on.
confirm: Callable[[QWidget, str, str], bool] = _confirm  # replaced in tests


class EarlierVersionsDialog(QDialog):
    """chosen: the version to go back to, once accepted."""

    def __init__(
        self,
        versions: VersionRepository,
        note_id: str,
        title: str,
        color: str,
        *,
        can_restore: bool = True,
        opener: Callable[[bytes], str] | None = None,
        open_secret: Callable[[], Callable[[bytes], str] | None] | None = None,
        device_name: Callable[[str], str] = str,
        parent: QWidget | None = None,
    ) -> None:
        """opener: a secret note's earlier texts opened (None while it is covered,
        or for an ordinary note); open_secret: asks for the password and gives
        the opener (None when it cannot be asked for here, as in the trash)."""
        super().__init__(parent)
        self._versions = versions
        self._note_id = note_id
        self._title = title
        self._can_restore = can_restore
        self._opener = opener
        self._open_secret = open_secret
        self._device_name = device_name
        self.chosen: int | None = None
        self._on_top = OnTopWhileInUse(self)

        self.list = QListWidget(self)
        self.list.currentItemChanged.connect(self._current_changed)
        self.list.installEventFilter(self)
        self.view = NoteView(self)
        self.view.colors = note_colors(color)
        self.covered = QLabel(self)
        self.covered.setWordWrap(True)
        self.covered.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.show_button = QPushButton(self)
        self.show_button.clicked.connect(self._show_secret)
        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.view, 1)
        right_layout.addWidget(self.covered, 1)
        right_layout.addWidget(self.show_button, 0, Qt.AlignmentFlag.AlignCenter)
        split = QSplitter(self)
        split.addWidget(self.list)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setChildrenCollapsible(False)

        self.note = QLabel(self)
        self.note.setWordWrap(True)
        self.restore_button = QPushButton(self)
        self.restore_button.clicked.connect(self.restore_chosen)
        self.delete_button = QPushButton(self)
        self.delete_button.clicked.connect(self.delete_chosen)
        self.delete_all_button = QPushButton(self)
        self.delete_all_button.clicked.connect(self.delete_all)
        self.close_button = QPushButton(self)
        self.close_button.clicked.connect(self.reject)
        self.close_button.setDefault(True)  # Enter never goes back by itself
        buttons = QHBoxLayout()
        buttons.addWidget(self.restore_button)
        buttons.addWidget(self.delete_button)
        buttons.addWidget(self.delete_all_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(split, 1)
        layout.addWidget(self.note)
        layout.addLayout(buttons)
        self.setMinimumSize(620, 400)
        self.retranslate()

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Earlier versions — %1").replace("%1", self._title))
        self.list.setAccessibleName(self.tr("Earlier versions"))
        self.view.setAccessibleName(self.tr("The version chosen"))
        self.covered.setText(
            self.tr("This secret note's text is covered. Show it to read its earlier versions.")
        )
        self.show_button.setText(self.tr("Show its text…"))
        self.note.setText(
            self.tr("Earlier versions stay on this computer only, for 30 days (up to 50 per note).")
        )
        self.restore_button.setText(self.tr("&Restore this version"))
        self.delete_button.setText(self.tr("&Delete this version…"))
        self.delete_all_button.setText(self.tr("Delete &all versions…"))
        self.close_button.setText(self.tr("Close"))
        self.refresh()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.list
            and isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Delete
        ):
            self.delete_chosen()
            return True
        return super().eventFilter(watched, event)

    def _current_changed(
        self, _current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        self._picked()

    # What it says

    def _from(self, version: EarlierVersion) -> str:
        if version.restored:
            return self.tr("Before restoring")
        if version.came_from is None:
            return self.tr("Changed here")
        return self.tr("From %1").replace("%1", self._device_name(version.came_from))

    def _first_line(self, version: EarlierVersion) -> str | None:
        """Its first line: a secret note's opened for it, while it is shown."""
        if not version.sealed:
            return version.first_line
        text = self._text(version.id)
        return None if text is None else note_title(text)

    def _text(self, version_id: int) -> str | None:
        """Its whole text, opened if it is a secret note's (None if it cannot be)."""
        stored = self._versions.text(self._note_id, version_id)
        if not isinstance(stored, bytes):
            return stored
        if self._opener is None:
            return None
        try:
            return self._opener(stored)
        except SecretNoteError:
            return None

    def _row(self, version: EarlierVersion) -> str:
        parts = [local_time(version.at), self._from(version)]
        line = self._first_line(version)
        if line:
            parts.append(line if len(line) <= ROW_TITLE else line[: ROW_TITLE - 1] + "…")
        return " · ".join(parts)

    def refresh(self) -> None:
        picked = self.picked
        # Filled quietly: the one picked is read once, at the end.
        self.list.blockSignals(True)
        try:
            self.list.clear()
            for version in self._versions.listed(self._note_id):
                item = QListWidgetItem(self._row(version))
                item.setData(VERSION_ID, version.id)
                self.list.addItem(item)
                if version.id == picked:
                    self.list.setCurrentItem(item)
            if self.picked is None and self.list.count():
                self.list.setCurrentRow(0)
        finally:
            self.list.blockSignals(False)
        self._picked()

    @property
    def picked(self) -> int | None:
        # None when there is none, whatever the type hints say.
        item = cast(QListWidgetItem | None, self.list.currentItem())
        return None if item is None else int(item.data(VERSION_ID))

    def _picked(self) -> None:
        version_id = self.picked
        text = None if version_id is None else self._text(version_id)
        covered = version_id is not None and text is None
        self.view.setVisible(not covered)
        self.view.show_markdown(text or "")
        self.covered.setVisible(covered)
        self.show_button.setVisible(covered and self._open_secret is not None)
        self.restore_button.setEnabled(self._can_restore and text is not None)
        self.delete_button.setEnabled(version_id is not None)
        self.delete_all_button.setEnabled(self.list.count() > 0)

    # What it does

    def _show_secret(self) -> None:
        if self._open_secret is None:
            return
        opener = self._open_secret()
        if opener is not None:
            self._opener = opener
            self.refresh()

    def restore_chosen(self) -> None:
        if self.restore_button.isEnabled() and self.picked is not None:
            self.chosen = self.picked
            self.accept()

    def delete_chosen(self) -> None:
        version_id = self.picked
        if version_id is None:
            return
        question = self.tr("Delete this version? This cannot be undone.")
        if confirm(self, question, self.tr("Delete")):
            self._versions.delete(self._note_id, version_id)
            self._after_deleting()

    def delete_all(self) -> None:
        count = self.list.count()
        if not count:
            return
        question = self.tr(
            "Delete all %n earlier versions of this note? This cannot be undone.", "", count
        )
        if confirm(self, question, self.tr("Delete all")):
            self._versions.delete_all(self._note_id)
            self._after_deleting()

    def _after_deleting(self) -> None:
        self.refresh()
        if not self.list.count():
            self.reject()  # nothing left to show
