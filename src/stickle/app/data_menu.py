"""The Data menu: backups, exporting everything, and (later) bringing notes in.

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
from stickle.data.export import ExportResult

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataAccess:
    backups: BackupAccess
    export: Callable[[Path], ExportResult]  # into a new folder inside the one given


class DataMenu(QObject):
    """The menu's words (a QObject, so that its plurals are found for translation)."""

    def export_title(self) -> str:
        return self.tr("Export all notes")

    def open_folder(self) -> str:
        return self.tr("&Open the folder")

    def export_failed(self) -> str:
        return self.tr("The notes could not be exported. Check that the folder can be written to.")

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
    box.addButton(QMessageBox.StandardButton.Ok)
    box.exec()
    if box.clickedButton() is open_button:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


def _tell_failed(parent: QWidget, title: str, text: str) -> None:
    QMessageBox.warning(parent, title, text)


# Replaced in tests.
tell_exported: Callable[[QWidget, str, str, Path], None] = _tell_exported
tell_failed: Callable[[QWidget, str, str], None] = _tell_failed


def export_everything(parent: QWidget, access: DataAccess) -> ExportResult | None:
    """Ask for a folder and export into it; None if cancelled or it failed (said)."""
    words = DataMenu()
    title = words.export_title()
    folder = file_dialogs.existing_folder(parent, title)
    if folder is None:
        return None
    try:
        result = access.export(folder)
    except (OSError, apsw.Error) as error:
        log.error("could not export the notes: %s", type(error).__name__)
        tell_failed(parent, title, words.export_failed())
        return None
    log.info("exported %d notes and %d from the trash", result.notes, result.deleted)
    tell_exported(parent, title, words.exported(result.notes, result.folder), result.folder)
    return result
