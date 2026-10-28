"""Bringing notes over from another sticky-note app, after a look at what comes.

The window lists the apps whose notes are on this computer (or reads a file
or folder the user picks), then shows what would come: how many notes, the
colours they take, and the formatting that does not come along. Nothing is
brought in before the user says so, and the other app's files are only read.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import override

from PySide6.QtCore import QDate, QEvent, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from stickle.app.file_dialogs import open_file_name
from stickle.app.in_use import OnTopWhileInUse
from stickle.app.palette import swatch_icon
from stickle.core.markdown import note_title
from stickle.importers.base import Found, ImportResult, SourceError
from stickle.importers.bring_in import BroughtIn
from stickle.importers.sources import Source

log = logging.getLogger(__name__)
PREVIEW_TITLE = 60


@dataclass(frozen=True)
class ImportAccess:
    """What the window can do, without it knowing the database."""

    found: Callable[[], list[tuple[Source, Found]]]
    recognise: Callable[[Path], tuple[Source, Found] | None]
    new_notes: Callable[[Source, ImportResult], int]  # not brought in before
    # And show them; the line, if given, ends each note (where it came from).
    bring_in: Callable[[Source, ImportResult, str | None], BroughtIn]


choose_file: Callable[[QWidget, str], Path | None] = open_file_name  # replaced in tests


@dataclass
class _Read:
    source: Source
    found: Found
    result: ImportResult | None  # None: could not be read
    new: int = 0


class ImportDialog(QDialog):
    def __init__(self, access: ImportAccess, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._access = access
        # Called up from the tray, it is kept above pinned notes on its own;
        # from the Stickle window, that window keeps it there.
        self._on_top = OnTopWhileInUse(self)
        self._read: list[_Read] = []
        self.intro = QLabel(self)
        self.intro.setWordWrap(True)
        self.sources = QListWidget(self)
        self.sources.currentRowChanged.connect(self._source_chosen)
        self.none_found = QLabel(self)
        self.none_found.setWordWrap(True)
        self.choose_button = QPushButton(self)
        self.choose_button.clicked.connect(self.choose)
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        self.preview = QListWidget(self)
        # Off unless asked: it adds to the notes' own text. Where each note came
        # from is recorded either way.
        self.source_box = QCheckBox(self)
        self.outcome = QLabel(self)
        self.outcome.setWordWrap(True)
        self.outcome.hide()
        self.bring_button = QPushButton(self)
        self.bring_button.clicked.connect(self.bring_in)
        self.close_button = QPushButton(self)
        self.close_button.clicked.connect(self.close)

        choose = QHBoxLayout()
        choose.addWidget(self.none_found, 1)
        choose.addWidget(self.choose_button)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.bring_button)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro)
        layout.addWidget(self.sources)
        layout.addLayout(choose)
        layout.addWidget(self.summary)
        layout.addWidget(self.preview, 1)
        layout.addWidget(self.source_box)
        layout.addWidget(self.outcome)
        layout.addLayout(buttons)
        self.setMinimumSize(460, 480)
        self.retranslate()

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Bring notes in"))
        self.intro.setText(
            self.tr(
                "Bring notes over from another sticky-note app. Its own files are only "
                "read, never changed, so it goes on working as before."
            )
        )
        self.sources.setAccessibleName(self.tr("Apps with notes on this computer"))
        self.none_found.setText(self.tr("No other app's notes were found on this computer."))
        self.choose_button.setText(self.tr("Choose a &file…"))
        self.preview.setAccessibleName(self.tr("Notes that would come"))
        self.source_box.setText(self.tr("&Add where each note came from, at its end"))
        self.source_box.setToolTip(
            self.tr("A highlighted line such as “%1”.").replace("%1", self.source_line("xpad"))
        )
        self.close_button.setText(self.tr("Close"))
        self._refresh_rows()
        self._show_chosen()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def _load(self, source: Source, found: Found) -> _Read:
        try:
            result = source.read(found.path)
        except (SourceError, OSError) as error:
            log.warning("could not read %s's notes: %s", source.name, type(error).__name__)
            return _Read(source, found, None)
        return _Read(source, found, result, self._access.new_notes(source, result))

    def refresh(self) -> None:
        """Look again for other apps' notes, and what is new in them."""
        self._read = [self._load(source, found) for source, found in self._access.found()]
        self._refresh_rows()
        if self._read:
            self.sources.setCurrentRow(0)
        self._show_chosen()

    def _row_text(self, read: _Read) -> str:
        if read.result is None:
            return self.tr("%1 — its notes could not be read").replace("%1", read.source.title)
        if read.new == 0 and read.result.notes:
            return self.tr("%1 — every note brought in already").replace("%1", read.source.title)
        return self.tr("%1 — %n new note(s)", "", read.new).replace("%1", read.source.title)

    def _refresh_rows(self) -> None:
        row = self.sources.currentRow()
        self.sources.blockSignals(True)
        self.sources.clear()
        for read in self._read:
            self.sources.addItem(self._row_text(read))
        self.sources.setCurrentRow(row if 0 <= row < len(self._read) else -1)
        self.sources.blockSignals(False)
        self.sources.setVisible(bool(self._read))
        self.none_found.setVisible(not self._read)

    def _chosen(self) -> _Read | None:
        row = self.sources.currentRow()
        return self._read[row] if 0 <= row < len(self._read) else None

    def _source_chosen(self, _row: int) -> None:
        self.outcome.hide()
        self._show_chosen()

    def _show_chosen(self) -> None:
        self.preview.clear()
        read = self._chosen()
        if read is None or read.result is None:
            self.summary.setText("")
            self.summary.setVisible(read is not None)
            if read is not None:
                self.summary.setText(self.tr("The notes could not be read. Nothing was changed."))
            self.bring_button.setText(self.tr("&Bring in"))
            self.bring_button.setEnabled(False)
            return
        result = read.result
        lines = [
            self.tr(
                "%n note(s) not brought in before. Each takes the nearest of Stickle's "
                "colours, and keeps its place and category where it had them.",
                "",
                read.new,
            )
        ]
        if result.dropped:
            lines.append(
                self.tr("Left behind: %1.").replace(
                    "%1", ", ".join(self._dropped_name(what) for what in result.dropped)
                )
            )
        if result.unreadable:
            lines.append(self.tr("%n note(s) could not be read.", "", result.unreadable))
        self.summary.setText("\n".join(lines))
        self.summary.show()
        for note in result.notes:
            title = note_title(note.body) or self.tr("(empty note)")
            item = QListWidgetItem(swatch_icon(note.color), title[:PREVIEW_TITLE])
            item.setData(Qt.ItemDataRole.AccessibleTextRole, title)
            self.preview.addItem(item)
        self.bring_button.setText(self.tr("&Bring in %n note(s)", "", read.new))
        self.bring_button.setEnabled(read.new > 0)

    def _dropped_name(self, what: str) -> str:
        names = {
            "underline": self.tr("underline"),
            "strikethrough": self.tr("strikethrough"),
            "fonts": self.tr("fonts"),
            "text colours": self.tr("text colours"),
            "pictures": self.tr("pictures"),
        }
        return names.get(what, what)

    def source_line(self, title: str) -> str:
        """Where a note came from, as written at its end: the app and today's date."""
        today = QDate.currentDate().toString(Qt.DateFormat.ISODate)
        return self.tr("Brought in from %1 · %2").replace("%1", title).replace("%2", today)

    def choose(self) -> None:
        """Read notes from a file or folder the user picks (copied from elsewhere)."""
        path = choose_file(self, self.tr("Bring notes in"))
        if path is None:
            return
        recognised = self._access.recognise(path)
        if recognised is None:
            self.outcome.setText(self.tr("Stickle does not know that file's notes."))
            self.outcome.show()
            return
        self._read.append(self._load(*recognised))
        self._refresh_rows()
        self.sources.setCurrentRow(len(self._read) - 1)

    def bring_in(self) -> None:
        read = self._chosen()
        if read is None or read.result is None or read.new == 0:
            return
        try:
            line = self.source_line(read.source.title) if self.source_box.isChecked() else None
            brought = self._access.bring_in(read.source, read.result, line)
        except Exception as error:  # nothing was brought in: it is all or nothing
            log.error("could not bring notes in: %s", type(error).__name__)
            self.outcome.setText(self.tr("The notes could not be brought in. Nothing was changed."))
            self.outcome.show()
            return
        log.info("brought in %d notes from %s", len(brought.note_ids), read.source.name)
        read.new = self._access.new_notes(read.source, read.result)
        self._refresh_rows()
        self._show_chosen()
        self.outcome.setText(
            self.tr(
                "%n note(s) brought in. They are on the desktop; hidden ones are listed "
                "in Stickle Manager.",
                "",
                len(brought.note_ids),
            )
        )
        self.outcome.show()
