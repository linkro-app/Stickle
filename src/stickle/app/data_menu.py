"""The Data menu: bringing notes in, exporting everything, and backups.

The Stickle window holds the menu on a button, and the tray shows the same
entries in a submenu. What the entries do is handed in as DataAccess, so
neither knows the database or its key.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import apsw
from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QWidget

from stickle.app import file_dialogs
from stickle.app.backups import BackupAccess
from stickle.app.import_dialog import ImportAccess
from stickle.app.secret_dialogs import SecretExportDialog
from stickle.crypto.secret_notes import SecretNoteError
from stickle.data.export import ExportResult, SecretExport

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataAccess:
    backups: BackupAccess
    # Into a new folder inside the one given, the secret notes as said.
    export: Callable[[Path, SecretExport], ExportResult]
    importing: ImportAccess
    secret_notes: Callable[[], int] = lambda: 0  # how many there are, in the trash too
    # Their password: their key (WrongPasswordError), to export them in the clear.
    open_secret_notes: Callable[[str], bytes] | None = None
    # Their key file as it is, to go along with them left encrypted.
    secret_key_copies: Callable[[], dict[str, object] | None] = lambda: None


class DataMenu(QObject):
    """The menu's words (a QObject, so that its plurals are found for translation)."""

    def export_title(self) -> str:
        return self.tr("Export all notes")

    def open_folder(self) -> str:
        return self.tr("&Open the folder")

    def export_failed(self) -> str:
        return self.tr("The notes could not be exported. Check that the folder can be written to.")

    def secret_not_opened(self) -> str:
        return self.tr(
            "A secret note could not be opened with their key, so nothing was exported."
            " Export them encrypted instead."
        )

    def locked_line(self) -> str:
        """A secret note's Markdown file, left encrypted, below its title."""
        return self.tr("Encrypted secret note")

    def stayed_encrypted(self, notes: int) -> str:
        return self.tr(
            "%n secret note(s) stayed encrypted with the secret notes' password: their"
            " titles can be read, and their text opens with that password.",
            "",
            notes,
        )

    def exported(self, notes: int, folder: Path) -> str:
        return self.tr(
            "%n note(s) exported to %1, each as a Markdown file, with everything in "
            "stickle-export.json. Notes in the trash are in its trash folder.\n\n"
            "These files are not encrypted: keep them somewhere safe.",
            "",
            notes,
        ).replace("%1", str(folder))


def _tell_exported(parent: QWidget, title: str, text: str, folder: Path) -> None:
    box = QMessageBox(QMessageBox.Icon.Information, title, text, parent=parent)
    open_button = box.addButton(DataMenu().open_folder(), QMessageBox.ButtonRole.ActionRole)
    ok = box.addButton(QMessageBox.StandardButton.Ok)
    # With a second button Qt finds no button for Esc by itself, and the box stayed.
    box.setEscapeButton(ok)
    box.exec()
    if box.clickedButton() is open_button:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


def _tell_failed(parent: QWidget, title: str, text: str) -> None:
    QMessageBox.warning(parent, title, text)


# Replaced in tests.
tell_exported: Callable[[QWidget, str, str, Path], None] = _tell_exported
tell_failed: Callable[[QWidget, str, str], None] = _tell_failed


def _ask(dialog: SecretExportDialog) -> bool:
    return dialog.exec() == SecretExportDialog.DialogCode.Accepted


ask_secret_export: Callable[[SecretExportDialog], bool] = _ask  # replaced in tests


def export_everything(parent: QWidget, access: DataAccess) -> ExportResult | None:
    """Ask how secret notes go out (if there are any) and for a folder, and export
    into it; None if cancelled or it failed (said)."""
    words = DataMenu()
    title = words.export_title()
    secret = SecretExport(locked_line=words.locked_line(), key_copies=access.secret_key_copies())
    count = access.secret_notes()
    if count and access.open_secret_notes is not None:
        dialog = SecretExportDialog(count, access.open_secret_notes, parent)
        if not ask_secret_export(dialog):
            return None
        key = dialog.take_key()
        if key is not None:
            secret = SecretExport(key=key)
    folder = file_dialogs.existing_folder(parent, title)
    if folder is None:
        return None
    try:
        result = access.export(folder, secret)
    except SecretNoteError:
        log.error("could not open a secret note to export it")
        tell_failed(parent, title, words.secret_not_opened())
        return None
    except (OSError, apsw.Error) as error:
        log.error("could not export the notes: %s", type(error).__name__)
        tell_failed(parent, title, words.export_failed())
        return None
    log.info(
        "exported %d notes and %d from the trash, %d left encrypted",
        result.notes,
        result.deleted,
        result.still_encrypted,
    )
    text = words.exported(result.notes, result.folder)
    if result.still_encrypted:
        text += "\n\n" + words.stayed_encrypted(result.still_encrypted)
    tell_exported(parent, title, text, result.folder)
    return result
