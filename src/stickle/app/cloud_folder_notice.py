"""Stickle's data folder inside a cloud program's folder, told once for that place.

The program (OneDrive, Dropbox...) may upload or replace the database or a key
file while Stickle is writing it, which can damage the notes or leave copies
beside them. Nothing is stopped: a line at the top of Stickle Manager says so
until the user says OK, and, with that window closed, one notification of the
system does. The place is kept as a hash, never its path; in another cloud
folder, or another place in one, it is told again.
"""

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import override

import apsw
from PySide6.QtCore import QEvent, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from stickle.data.settings import CLOUD_FOLDER_SEEN, Settings
from stickle.platform.cloud_folders import CloudFolder

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InCloudFolder:
    cloud: str  # the program, by the name its user knows
    portable: bool  # next to Stickle: the folder Stickle is in is what to move
    to_move: Path  # what "Open the folder" opens
    place: str  # the cloud and the data folder, as a hash


def in_cloud_folder(
    data: Path, portable: bool, held_by: CloudFolder | None
) -> InCloudFolder | None:
    """What to tell of the data folder, None if no cloud folder holds it."""
    if held_by is None:
        return None
    try:
        where = data.resolve()
    except OSError:
        where = data.absolute()
    place = hashlib.sha256(f"{held_by.name}\n{os.path.normcase(where)}".encode()).hexdigest()
    return InCloudFolder(held_by.name, portable, data.parent if portable else data, place)


class CloudFolderLine(QWidget):
    """The line at the top of Stickle Manager, with "Open the folder" and OK."""

    def __init__(self, settings: Settings | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.found: InCloudFolder | None = None
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.open_button = QPushButton(self)
        self.open_button.clicked.connect(self._open_folder)
        self.ok_button = QPushButton(self)
        self.ok_button.clicked.connect(self._seen)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.ok_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)
        layout.addLayout(buttons)
        self.hide()
        self.retranslate()

    def tell(self, found: InCloudFolder) -> bool:
        """Shown, unless the user said OK to that place before: True if shown."""
        if self._settings is not None and self._settings.get(CLOUD_FOLDER_SEEN) == found.place:
            return False
        self.found = found
        self.retranslate()
        self.show()
        return True

    def text(self) -> str:
        found = self.found
        if found is None:
            return ""
        said = self.tr(
            "Stickle's data folder is inside your %1 folder. If the %1 app uploads or"
            " replaces a file while Stickle is saving it, your notes can be damaged."
        )
        if found.portable:
            move = self.tr(
                "Quit Stickle, then move the whole folder Stickle is in out of your %1 folder."
            )
        else:
            move = self.tr("Move the data folder out of your %1 folder.")
        return " ".join((said, move)).replace("%1", found.cloud)

    def retranslate(self) -> None:
        text = self.text()
        self.label.setText(text)
        self.open_button.setText(self.tr("Open the &folder"))
        self.ok_button.setText(self.tr("OK"))
        self.ok_button.setAccessibleDescription(text)

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def _open_folder(self) -> None:
        if self.found is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.found.to_move)))

    def _seen(self) -> None:
        if self.found is not None and self._settings is not None:
            try:
                self._settings.set(CLOUD_FOLDER_SEEN, self.found.place)
            except apsw.Error as error:  # shown again at the next start, nothing worse
                log.warning("could not keep the cloud folder line seen: %s", type(error).__name__)
        self.hide()
