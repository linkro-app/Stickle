"""The daily backup while Stickle runs, and the window for going back to one.

The backup is taken a little after the notes are up (not to slow the start)
and checked again every hour, so a Stickle left running past midnight takes
the next day's too. A backup that fails is logged and tried again within the
hour; nothing pops up (the window lists when the last one was made).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import override

import apsw
from PySide6.QtCore import QDateTime, QEvent, QLocale, QObject, Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from stickle.app.in_use import OnTopWhileInUse
from stickle.app.secret_dialogs import OpenSecretDialog, SecretPasswords
from stickle.core.clock import Clock, utc_now
from stickle.data.backups import Backup, Contents, take_daily

FIRST_DELAY_MS = 10_000
EVERY_MS = 60 * 60 * 1000
log = logging.getLogger(__name__)


class DailyBackups(QObject):
    def __init__(
        self,
        connection: apsw.Connection,
        database: Path,
        clock: Clock = utc_now,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._connection = connection
        self._database = database
        self._clock = clock
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.run_now)

    def start(self) -> None:
        QTimer.singleShot(FIRST_DELAY_MS, self, self.run_now)
        self._timer.start(EVERY_MS)

    def run_now(self) -> Path | None:
        """Take the day's backup if there is none yet; None if not (or it failed)."""
        try:
            taken = take_daily(self._connection, self._database, self._clock)
        except (OSError, apsw.Error) as error:
            log.warning("could not back up the notes: %s", type(error).__name__)
            return None
        if taken is not None:
            log.info("notes backed up")
        return taken


@dataclass(frozen=True)
class BackupAccess:
    """What the window can do with backups, without it knowing the database key."""

    available: Callable[[], list[Backup]]
    contents: Callable[[Path], Contents | None]
    # Back up the notes as they are, put the backup in their place and start
    # again; False (and nothing changed) if it could not. The notes secret now
    # stay secret: with the secret notes' key, their earlier text in the backup
    # comes back sealed; without it (None), they stay as they are now.
    restore: Callable[[Path, bytes | None], bool]
    # How many notes secret now the backup holds the earlier text of, in the clear.
    old_secret_texts: Callable[[Path], int] = lambda _path: 0
    secrets: Callable[[], SecretPasswords | None] = lambda: None


def _confirm(parent: QWidget, title: str, text: str, go: str) -> bool:
    box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=parent)
    yes = box.addButton(go, QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    return box.clickedButton() is yes


confirm: Callable[[QWidget, str, str, str], bool] = _confirm  # replaced in tests


def _ask(dialog: QDialog) -> bool:
    return dialog.exec() == QDialog.DialogCode.Accepted


# Asks for the secret notes' password, when the backup holds notes secret since.
ask_secret: Callable[[QDialog], bool] = _ask  # replaced in tests


def when(moment: datetime) -> str:
    """A backup's time, on this computer's clock, in the interface language."""
    local = QDateTime.fromSecsSinceEpoch(int(moment.timestamp())).toLocalTime()
    return QLocale().toString(local, "yyyy-MM-dd (ddd) HH:mm")


class RestoreDialog(QDialog):
    def __init__(self, access: BackupAccess, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._access = access
        # Called up from the tray, it is kept above pinned notes on its own;
        # from the Stickle window, that window keeps it there.
        self._on_top = OnTopWhileInUse(self)
        self.intro = QLabel(self)
        self.intro.setWordWrap(True)
        self.backups = QListWidget(self)
        self.backups.currentRowChanged.connect(self._row_changed)
        self.backups.itemActivated.connect(self._activated)
        self.empty = QLabel(self)
        self.empty.setWordWrap(True)
        self.problem = QLabel(self)
        self.problem.setWordWrap(True)
        self.problem.hide()
        self.restore_button = QPushButton(self)
        self.restore_button.clicked.connect(self.restore_chosen)
        self.close_button = QPushButton(self)
        self.close_button.clicked.connect(self.close)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.restore_button)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro)
        layout.addWidget(self.backups, 1)
        layout.addWidget(self.empty)
        layout.addWidget(self.problem)
        layout.addLayout(buttons)
        self.setMinimumSize(420, 360)
        self.retranslate()

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Restore from a backup"))
        self.intro.setText(
            self.tr(
                "Stickle backs up your notes once a day and keeps the backups for a week. "
                "Choose the time to go back to."
            )
        )
        self.backups.setAccessibleName(self.tr("Backups"))
        self.empty.setText(
            self.tr("There is no backup yet. The first one is made shortly after Stickle starts.")
        )
        self.restore_button.setText(self.tr("&Restore…"))
        self.close_button.setText(self.tr("Close"))
        self.refresh()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def _row(self, backup: Backup, contents: Contents | None) -> str:
        moment = when(backup.taken)
        if contents is None:
            return self.tr("%1 — cannot be opened").replace("%1", moment)
        if contents.newer:
            return self.tr("%1 — made by a newer Stickle").replace("%1", moment)
        if backup.before_restore:
            return self.tr("%1 — %n note(s), from before going back", "", contents.notes).replace(
                "%1", moment
            )
        return self.tr("%1 — %n note(s)", "", contents.notes).replace("%1", moment)

    def refresh(self) -> None:
        self.backups.clear()
        for backup in self._access.available():
            contents = self._access.contents(backup.path)
            item = QListWidgetItem(self._row(backup, contents))
            item.setData(Qt.ItemDataRole.UserRole, str(backup.path))
            item.setData(Qt.ItemDataRole.UserRole + 1, backup.taken)
            if contents is None or contents.newer:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.backups.addItem(item)
        usable = [
            self.backups.item(row)
            for row in range(self.backups.count())
            if self.backups.item(row).flags() & Qt.ItemFlag.ItemIsEnabled
        ]
        if usable:
            self.backups.setCurrentItem(usable[0])
        self.empty.setVisible(self.backups.count() == 0)
        self._refresh_button()

    def _row_changed(self, _row: int) -> None:
        self._refresh_button()

    def _activated(self, _item: QListWidgetItem) -> None:
        self.restore_chosen()

    def _chosen(self) -> QListWidgetItem | None:
        """The backup chosen, if it is one that can be gone back to."""
        row = self.backups.currentRow()
        if row < 0:
            return None
        item = self.backups.item(row)
        return item if item.flags() & Qt.ItemFlag.ItemIsEnabled else None

    def _refresh_button(self) -> None:
        self.restore_button.setEnabled(self._chosen() is not None)

    def restore_chosen(self) -> None:
        item = self._chosen()
        if item is None:
            return
        moment = when(item.data(Qt.ItemDataRole.UserRole + 1))
        asked = confirm(
            self,
            self.tr("Restore from a backup"),
            self.tr(
                "Go back to your notes as they were at %1?\n\n"
                "Your notes as they are now are backed up first, so you can come back "
                "to them the same way. Stickle then starts again."
            ).replace("%1", moment),
            self.tr("&Restore"),
        )
        if not asked:
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        secret_key: bytes | None = None
        found = self._access.old_secret_texts(path)
        secrets = self._access.secrets()
        if found and secrets is not None:
            dialog = OpenSecretDialog(secrets, parent=self, purpose="restore", count=found)
            if not ask_secret(dialog):
                return
            secret_key = dialog.take_key()  # None: those notes stay as they are now
        if not self._access.restore(path, secret_key):
            self.problem.setText(self.tr("The notes could not be restored. Nothing was changed."))
            self.problem.show()
