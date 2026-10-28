"""The programs over which notes stay in sight, full screen or not.

Offered: those that sent notes away since Stickle started (known only while it
runs, never kept). Added ones are kept, by name alone, on this computer.
"""

from typing import override

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from stickle.app.full_screen import FullScreenWatch


class FullScreenExceptions(QDialog):
    def __init__(self, watch: FullScreenWatch, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._watch = watch
        self.found_label = QLabel(self)
        self.found_label.setWordWrap(True)
        self.found = QListWidget(self)
        self.found_label.setBuddy(self.found)
        self.none_found = QLabel(self)
        self.none_found.setWordWrap(True)
        self.except_button = QPushButton(self)
        self.except_button.clicked.connect(self.except_chosen)
        self.found.itemActivated.connect(self.except_chosen)
        self.found.currentRowChanged.connect(self._enable)
        self.kept_label = QLabel(self)
        self.kept_label.setWordWrap(True)
        self.kept = QListWidget(self)
        self.kept_label.setBuddy(self.kept)
        self.remove_button = QPushButton(self)
        self.remove_button.clicked.connect(self.remove_chosen)
        self.kept.currentRowChanged.connect(self._enable)
        self.note = QLabel(self)
        self.note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        for button in (self.except_button, self.remove_button):
            button.setAutoDefault(False)

        layout = QVBoxLayout(self)
        layout.addWidget(self.found_label)
        layout.addWidget(self.found)
        layout.addWidget(self.none_found)
        row = QHBoxLayout()
        row.addWidget(self.except_button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addSpacing(8)
        layout.addWidget(self.kept_label)
        layout.addWidget(self.kept)
        row = QHBoxLayout()
        row.addWidget(self.remove_button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(self.note)
        layout.addWidget(buttons)
        self.setMinimumWidth(420)
        watch.app_found.connect(self.refresh)
        self.retranslate()

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Full-screen exceptions"))
        self.found_label.setText(self.tr("Programs that hid notes since Stickle &started:"))
        self.found.setAccessibleName(self.tr("Programs that hid notes"))
        self.none_found.setText(
            self.tr(
                "None yet. Once a program shown full screen has hidden notes, it is listed"
                " here until Stickle quits."
            )
        )
        self.except_button.setText(self.tr("&Keep notes in sight over it"))
        self.kept_label.setText(
            self.tr("Notes stay in sight over these programs, &full screen too:")
        )
        self.kept.setAccessibleName(self.tr("Programs notes stay in sight over"))
        self.remove_button.setText(self.tr("&Remove"))
        self.note.setText(
            self.tr(
                "Only the names of the programs you add are kept, on this computer."
                " The programs that hid notes are not kept."
            )
        )
        self.refresh()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def refresh(self) -> None:
        exceptions = self._watch.exceptions
        excepted = {name.casefold() for name in exceptions}
        found = [app for app in self._watch.apps if app.casefold() not in excepted]
        for box, names in ((self.found, found), (self.kept, exceptions)):
            row = box.currentRow()
            box.clear()
            box.addItems(names)
            if names:
                box.setCurrentRow(min(max(row, 0), len(names) - 1))
        self.found.setVisible(bool(found))
        self.none_found.setVisible(not found)
        self._enable()

    def _enable(self) -> None:
        self.except_button.setEnabled(self.found.currentRow() >= 0)
        self.remove_button.setEnabled(self.kept.currentRow() >= 0)

    def except_chosen(self) -> None:
        if self.found.currentRow() >= 0:
            self._watch.except_app(self.found.currentItem().text())
            self.refresh()

    def remove_chosen(self) -> None:
        if self.kept.currentRow() >= 0:
            self._watch.unexcept_app(self.kept.currentItem().text())
            self.refresh()
