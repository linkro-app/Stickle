"""Shown when the notes cannot be opened: what happened, and the ways forward.

Nothing here changes the notes. The user can try again, copy the notes out
as Markdown files (when the key is known), open the data folder, or copy a
description of the problem for an issue report; that description contains
no note text, no paths and no keys.
"""

import platform
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Literal, get_args, override

from PySide6.QtCore import QEvent, Qt, QUrl, qVersion
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from stickle import __version__
from stickle.app.file_dialogs import existing_folder
from stickle.data.export import ExportResult
from stickle.data.schema import NotesDiff
from stickle.logs import redact

type Kind = Literal[
    "store_unavailable",
    "key_missing",
    "key_file_unreadable",
    "wrong_key",
    "newer_version",
    "upgrade_failed",
    "open_failed",
]
KINDS: tuple[Kind, ...] = get_args(Kind.__value__)
NO_RETRY: set[Kind] = {"wrong_key", "newer_version"}
# Problems where the key itself is out of reach, which the recovery key replaces.
RECOVERABLE: set[Kind] = {"store_unavailable", "key_missing", "key_file_unreadable"}


@dataclass(frozen=True)
class Problem:
    kind: Kind
    error: str = ""  # type and message of the exception, for the report
    diff: NotesDiff | None = None


class Choice(Enum):
    RETRY = auto()
    QUIT = auto()
    RECOVER = auto()  # open with the recovery key


def diagnostics(problem: Problem) -> str:
    lines = [
        f"Stickle {__version__}",
        f"System: {platform.system()} {platform.release()} {platform.machine()}",
        f"Python {platform.python_version()}, Qt {qVersion()}",
        f"Problem: {problem.kind}",
    ]
    if problem.error:
        lines.append(f"Error: {redact(problem.error)}")
    if problem.diff is not None:
        diff = problem.diff
        lines.append(
            f"Notes: {len(diff.missing)} missing, {len(diff.changed)} changed,"
            f" {len(diff.added)} added"
        )
    return "\n".join(lines)


class RecoveryDialog(QDialog):
    def __init__(
        self,
        problem: Problem,
        data_folder: Path,
        export: Callable[[Path], ExportResult] | None = None,
        parent: QWidget | None = None,
        can_recover: bool = False,
    ) -> None:
        super().__init__(parent)
        self.problem = problem
        self._data_folder = data_folder
        self._export = export
        self.choice = Choice.QUIT

        self.heading = QLabel()
        font = QFont(self.heading.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.2)
        self.heading.setFont(font)
        self.heading.setWordWrap(True)
        self.explanation = QLabel()
        self.explanation.setWordWrap(True)
        self.notes = QListWidget()
        self.notes.setVisible(bool(problem.diff))
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.hide()

        self.retry_button = QPushButton()
        self.retry_button.clicked.connect(self._retry)
        self.retry_button.setVisible(problem.kind not in NO_RETRY)
        self.export_button = QPushButton()
        self.export_button.clicked.connect(self._export_notes)
        self.export_button.setVisible(export is not None)
        # When the key is lost and a recovery key was made, it is the way back in.
        self.recover_button = QPushButton()
        self.recover_button.clicked.connect(self._recover)
        self.recover_button.setVisible(can_recover and problem.kind in RECOVERABLE)
        # Helpers for a problem report, kept small below the real choices.
        self.report_label = QLabel()
        self.copy_button = QPushButton()
        self.copy_button.clicked.connect(self._copy_details)
        self.log_button = QPushButton()
        self.log_button.clicked.connect(self._open_logs)
        for link in (self.copy_button, self.log_button):
            link.setFlat(True)
            link.setCursor(Qt.CursorShape.PointingHandCursor)
            link.setStyleSheet("QPushButton { text-decoration: underline; padding: 0 4px; }")
        self.quit_button = QPushButton()
        self.quit_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addWidget(self.export_button)
        buttons.addStretch()
        buttons.addWidget(self.quit_button)
        buttons.addWidget(self.recover_button)
        buttons.addWidget(self.retry_button)
        report = QHBoxLayout()
        report.addWidget(self.report_label)
        report.addWidget(self.copy_button)
        report.addWidget(self.log_button)
        report.addStretch()
        layout = QVBoxLayout(self)
        layout.addWidget(self.heading)
        layout.addWidget(self.explanation)
        layout.addWidget(self.notes)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        layout.addSpacing(8)
        layout.addLayout(report)
        self.setMinimumWidth(560)
        self.retranslate()
        default = self.retry_button if self.retry_button.isVisibleTo(self) else self.quit_button
        default.setDefault(True)

    def texts(self) -> tuple[str, str]:
        kind = self.problem.kind
        unchanged = self.tr("Your notes have not been changed.")
        if kind == "store_unavailable":
            return self.tr("The system keychain did not respond"), " ".join(
                [
                    self.tr(
                        "Stickle keeps the key to your notes in the system keychain, and it"
                        " could not be opened. It may be locked, or its prompt was closed."
                        " Unlock it, then try again."
                    ),
                    unchanged,
                ]
            )
        if kind == "key_missing":
            return self.tr("The key to your notes was not found"), " ".join(
                [
                    self.tr(
                        "Your notes are here, but the system keychain no longer holds the key"
                        " that opens them. Stickle did not create a new key, because it could"
                        " not open these notes. If you copied them from another computer or"
                        " user account, open them there."
                    ),
                    unchanged,
                ]
            )
        if kind == "key_file_unreadable":
            return self.tr("The key file could not be read"), " ".join(
                [
                    self.tr(
                        "The file that holds the key to your notes is damaged or was"
                        " written by an unknown version of Stickle."
                    ),
                    unchanged,
                ]
            )
        if kind == "wrong_key":
            return self.tr("The key does not open these notes"), " ".join(
                [
                    self.tr(
                        "The notes file does not open with the key Stickle has. It may come"
                        " from another computer or user account, or it may be damaged."
                    ),
                    unchanged,
                ]
            )
        if kind == "newer_version":
            return self.tr("These notes were saved by a newer Stickle"), " ".join(
                [
                    self.tr("Update Stickle to open them."),
                    unchanged,
                    self.tr("Meanwhile you can export them as Markdown files."),
                ]
            )
        if kind == "upgrade_failed":
            parts = [
                self.tr(
                    "Stickle checks each update on a copy of your notes first, and this check"
                    " failed. Your notes stay exactly as they were before the update."
                ),
                self.tr("You can try again, or export your notes as Markdown files."),
            ]
            if self.problem.diff:
                parts.append(self.tr("These are the notes the update would have changed:"))
            return self.tr("Stickle could not update your notes"), " ".join(parts)
        return self.tr("Your notes could not be opened"), " ".join(
            [self.tr("An unexpected error occurred while opening your notes."), unchanged]
        )

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Stickle cannot open your notes"))
        heading, explanation = self.texts()
        self.heading.setText(heading)
        self.explanation.setText(explanation)
        self.notes.clear()
        if diff := self.problem.diff:
            empty = self.tr("(empty note)")
            for template, titles in (
                (self.tr("Missing: %1"), diff.missing),
                (self.tr("Changed: %1"), diff.changed),
                (self.tr("Added: %1"), diff.added),
            ):
                self.notes.addItems([template.replace("%1", t or empty) for t in titles])
        self.notes.setAccessibleName(self.tr("Affected notes"))
        self.retry_button.setText(self.tr("&Try again"))
        self.recover_button.setText(self.tr("Use the &recovery key…"))
        self.export_button.setText(self.tr("&Export notes…"))
        self.report_label.setText(self.tr("For a problem report:"))
        self.log_button.setText(self.tr("Open the log folder"))
        self.log_button.setToolTip(
            self.tr("The log file can be attached to a problem report. It holds no note text.")
        )
        self.copy_button.setText(self.tr("&Copy details"))
        self.copy_button.setToolTip(
            self.tr("For a problem report. Contains no note text, paths or keys.")
        )
        self.quit_button.setText(self.tr("&Quit"))

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def _say(self, message: str) -> None:
        self.status.setText(message)
        self.status.show()

    def _retry(self) -> None:
        self.choice = Choice.RETRY
        self.accept()

    def _recover(self) -> None:
        self.choice = Choice.RECOVER
        self.accept()

    def _export_notes(self) -> None:
        assert self._export is not None
        chosen = existing_folder(self, self.tr("Choose where to put the exported notes"))
        if chosen is None:
            return
        try:
            result = self._export(chosen)
        except Exception as error:
            self._say(
                self.tr("The notes could not be exported (%1).").replace("%1", type(error).__name__)
            )
            return
        self._say(
            self.tr(
                "Exported %n note(s) to the folder “%1”.", "", result.notes + result.deleted
            ).replace("%1", result.folder.name)
        )

    def _open_logs(self) -> None:
        logs = self._data_folder / "logs"
        folder = logs if logs.is_dir() else self._data_folder
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _copy_details(self) -> None:
        clipboard = QGuiApplication.clipboard()
        clipboard.setText(diagnostics(self.problem))
        self._say(self.tr("Copied. The details contain no note text, paths or keys."))

    def run(self) -> Choice:
        self.exec()
        return self.choice
