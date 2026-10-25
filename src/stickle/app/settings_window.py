"""Stickle's settings: what is set once and left, each kind on a page of its own.

What is done now and then (a new note, hiding them all, a backup) stays in
the menus. A change takes effect and is kept as it is made: there is nothing
to confirm, and Esc closes the window. A page with nothing on it yet is not
shown.
"""

import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from stickle.app.app_list import switch_app_list
from stickle.app.first_run import LoginNote
from stickle.app.i18n import LANGUAGES, Translations
from stickle.app.in_use import OnTopWhileInUse
from stickle.app.master_password import (
    CheckMasterPasswordDialog,
    MasterPasswordDialog,
    MasterPasswords,
)
from stickle.app.notes import NoteManager
from stickle.app.recovery_key_dialog import RecoveryKeyDialog
from stickle.app.shortcut_rows import ShortcutRows
from stickle.app.shortcuts import GlobalShortcuts
from stickle.app.times import local_time
from stickle.app.tray import switch_autostart
from stickle.app.update_check import UpdateCheck
from stickle.app.window_flags import set_stays_on_top, stays_on_top
from stickle.core.updates import FEED_HOST
from stickle.platform.autostart import Autostart
from stickle.platform.linux.appimage import AppMenuEntry
from stickle.platform.linux.x11 import activate

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryKeys:
    """Making the recovery key, without the window knowing the database key."""

    exists: Callable[[], bool]
    make: Callable[[], str]  # a new one, the old one no longer working
    kept: Callable[[], None]  # the user said the new one is kept somewhere safe
    made_at: Callable[[], str | None] = lambda: None  # when (UTC); unknown before 0.6


def _exec(dialog: RecoveryKeyDialog) -> bool:
    return dialog.exec() == RecoveryKeyDialog.DialogCode.Accepted


show_recovery_key: Callable[[RecoveryKeyDialog], bool] = _exec  # replaced in tests


def _exec_master(dialog: QDialog) -> object:
    return dialog.exec()


show_master_dialog: Callable[[QDialog], object] = _exec_master  # replaced in tests


def _page(parent: QWidget) -> tuple[QWidget, QVBoxLayout]:
    page = QWidget(parent)
    layout = QVBoxLayout(page)
    return page, layout


def _hint(parent: QWidget) -> QLabel:
    """A line under a switch saying what it does, smaller, under its text."""
    label = QLabel(parent)
    label.setWordWrap(True)
    label.setIndent(24)  # under the box's text, not its tick
    font = QFont(label.font())
    font.setPointSizeF(font.pointSizeF() * 0.9)
    label.setFont(font)
    return label


class SettingsWindow(QDialog):
    def __init__(
        self,
        notes: NoteManager,
        translations: Translations,
        parent: QWidget | None = None,
        autostart: Autostart | None = None,
        app_list: AppMenuEntry | None = None,
        recovery: RecoveryKeys | None = None,
        login_may_be_blocked: bool = False,
        shortcuts: GlobalShortcuts | None = None,
        portable: bool = False,
        updates: UpdateCheck | None = None,
        master: MasterPasswords | None = None,
    ) -> None:
        super().__init__(parent)
        self._notes = notes
        self._translations = translations
        self._autostart = autostart
        self._app_list = app_list
        self._recovery = recovery
        self._updates = updates
        # Called up from the tray or a note, it is kept above pinned notes on its
        # own; from the Stickle window, that window keeps it there.
        self._on_top = OnTopWhileInUse(self)
        self.tabs = QTabWidget(self)
        # Every tab in sight: the window is made wide enough instead.
        self.tabs.setUsesScrollButtons(False)

        self.general, general = _page(self.tabs)
        self.language_label = QLabel(self.general)
        self.language_box = QComboBox(self.general)
        self.language_label.setBuddy(self.language_box)
        for code, native_name in LANGUAGES:
            self.language_box.addItem(native_name, code)
        self.language_box.activated.connect(self._choose_language)
        language = QHBoxLayout()
        language.addWidget(self.language_label)
        language.addWidget(self.language_box, 1)
        general.addLayout(language)
        self.autostart_box = QCheckBox(self.general)
        self.autostart_box.setVisible(autostart is not None)
        self.autostart_box.clicked.connect(self._switch_autostart)
        general.addWidget(self.autostart_box)
        self.login_note = LoginNote(self.general)
        self.login_note.setVisible(autostart is not None and login_may_be_blocked)
        general.addWidget(self.login_note)
        self.app_list_box = QCheckBox(self.general)
        self.app_list_box.setVisible(app_list is not None)
        self.app_list_box.clicked.connect(self._switch_app_list)
        general.addWidget(self.app_list_box)
        # Not where a store keeps Stickle up to date.
        self.update_box = QCheckBox(self.general)
        self.update_box.setVisible(updates is not None)
        general.addWidget(self.update_box)
        general.addStretch(1)

        # Lining notes up when moved or resized.
        self.notes_page, notes_layout = _page(self.tabs)
        self.magnet_box = QCheckBox(self.notes_page)
        self.magnet_box.clicked.connect(notes.set_magnet)
        notes.magnet_changed.connect(self.refresh)
        self.magnet_note = _hint(self.notes_page)
        notes_layout.addWidget(self.magnet_box)
        notes_layout.addWidget(self.magnet_note)
        notes_layout.addStretch(1)

        self.protection, protection = _page(self.tabs)
        # When each was made or set, always in sight: a change made by someone
        # else at this computer shows here too.
        self.protection_dates = QLabel(self.protection)
        self.protection_dates.setWordWrap(True)
        self.protection_dates.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        protection.addWidget(self.protection_dates)
        self.recovery_button = QPushButton(self.protection)
        self.recovery_button.clicked.connect(self.new_recovery_key)
        # A second way back, which the user remembers: set once, changed, never removed.
        self._master = master
        self.master_button = QPushButton(self.protection)
        self.master_button.clicked.connect(self.set_master_password)
        self.check_master_button = QPushButton(self.protection)
        self.check_master_button.clicked.connect(self.check_master_password)
        for button in (self.recovery_button, self.master_button, self.check_master_button):
            row = QHBoxLayout()
            row.addWidget(button)
            row.addStretch(1)
            protection.addLayout(row)
        protection.addStretch(1)

        self.shortcuts_page, shortcuts_layout = _page(self.tabs)
        self.shortcut_rows = (
            ShortcutRows(shortcuts, self.shortcuts_page, portable=portable)
            if shortcuts is not None
            else None
        )
        if self.shortcut_rows is not None:
            shortcuts_layout.addWidget(self.shortcut_rows)
        shortcuts_layout.addStretch(1)

        # Every address the app itself connects to, said where it can be read.
        self.connections_page, connections = _page(self.tabs)
        self.connections = QLabel(self.connections_page)
        self.connections.setWordWrap(True)
        connections.addWidget(self.connections)
        connections.addStretch(1)

        for page in (
            self.general,
            self.notes_page,
            self.protection,
            self.shortcuts_page,
            self.connections_page,
        ):
            self.tabs.addTab(page, "")
        self.recovery_button.setVisible(recovery is not None)
        self.tabs.setTabVisible(
            self.tabs.indexOf(self.protection), recovery is not None or master is not None
        )
        self.tabs.setTabVisible(self.tabs.indexOf(self.shortcuts_page), shortcuts is not None)

        if updates is not None:
            self.update_box.clicked.connect(updates.set_enabled)
            updates.switched.connect(self.refresh)
        # Enter in a field presses nothing: a dialog's buttons would take it, and
        # the first one makes a new recovery key.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        self.setMinimumWidth(440)
        self._set_tab_order()
        translations.changed.connect(self.refresh)
        self.retranslate()

    def _set_tab_order(self) -> None:
        """The tabs, then the page shown, top to bottom (Qt passes over the
        hidden ones)."""
        order: list[QWidget] = [
            self.tabs.tabBar(),
            self.language_box,
            self.autostart_box,
            self.app_list_box,
            self.update_box,
            self.magnet_box,
            self.recovery_button,
            self.master_button,
            self.check_master_button,
            *(self.shortcut_rows.tab_order() if self.shortcut_rows is not None else []),
        ]
        for before, after in itertools.pairwise(order):
            QWidget.setTabOrder(before, after)

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Stickle Settings"))
        self.tabs.setAccessibleName(self.tr("Settings"))
        self.tabs.tabBar().setAccessibleName(self.tr("Settings"))
        self.tabs.setTabText(self.tabs.indexOf(self.general), self.tr("General"))
        self.tabs.setTabText(self.tabs.indexOf(self.notes_page), self.tr("Notes"))
        self.tabs.setTabText(self.tabs.indexOf(self.protection), self.tr("Protection"))
        self.tabs.setTabText(self.tabs.indexOf(self.shortcuts_page), self.tr("Shortcuts"))
        self.tabs.setTabText(self.tabs.indexOf(self.connections_page), self.tr("Connections"))
        self.language_label.setText(self.tr("&Language"))
        self.language_box.setAccessibleName(self.tr("Language"))
        self.language_box.setItemText(0, self.tr("System language"))
        self.autostart_box.setText(self.tr("&Start Stickle when I log in"))
        self.app_list_box.setText(self.tr("Show Stickle in the &app list"))
        self.update_box.setText(self.tr("Check for a new &version daily"))
        self.magnet_box.setText(self.tr("Line notes up like a &magnet"))
        magnet = self.tr(
            "A note moved or resized near another note or the edge of the screen lines"
            " up with it. Hold Alt as you let go to leave it as it is."
        )
        self.magnet_note.setText(magnet)
        self.magnet_box.setAccessibleDescription(magnet)
        self.recovery_button.setText(self.tr("Make a new &recovery key…"))
        self.check_master_button.setText(self.tr("Chec&k the master password…"))
        self.refresh()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    @override
    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowActivate:
            self.refresh()  # changed outside Stickle meanwhile, say in the system's settings
        return super().event(event)

    def refresh(self) -> None:
        """Show what the files and settings say: they may have changed outside
        Stickle, or elsewhere in it."""
        self.language_box.setCurrentIndex(
            max(0, self.language_box.findData(self._translations.language))
        )
        if self._autostart is not None:
            self.autostart_box.setChecked(self._autostart.enabled)
        if self._app_list is not None:
            self.app_list_box.setChecked(self._app_list.added)
        if self._updates is not None:
            self.update_box.setChecked(self._updates.enabled)
        self.magnet_box.setChecked(self._notes.magnet)
        master = self._master is not None and self._master.exists()
        self.master_button.setVisible(self._master is not None)
        self.master_button.setText(
            self.tr("Change the &master password…")
            if master
            else self.tr("Set a &master password…")
        )
        self.check_master_button.setVisible(master)
        self.protection_dates.setText(self.dates())
        if self._updates is None:
            self.connections.setText(self.tr("Stickle connects to nothing on the internet."))
        elif self._updates.enabled:
            self.connections.setText(
                self.tr(
                    "Stickle connects only to %1, once a day, to see whether a new version "
                    "is out. It sends nothing about you or your notes, and the check can be "
                    "turned off in General."
                ).replace("%1", FEED_HOST)
            )
        else:
            self.connections.setText(
                self.tr(
                    "With the daily check for a new version turned off, Stickle connects "
                    "to nothing on the internet."
                )
            )

    def present(self, page: QWidget | None = None) -> None:
        """Shown, or brought forward if it already is, on the page last shown
        unless told which; above pinned notes until the user moves on from it."""
        if page is not None and self.tabs.isTabVisible(self.tabs.indexOf(page)):
            self.tabs.setCurrentWidget(page)
        self.refresh()
        parent = self.parentWidget()
        if parent is None or not (parent.isVisible() and stays_on_top(parent)):
            set_stays_on_top(self, True)
        self.show()
        self.raise_()
        self.activateWindow()
        if QGuiApplication.platformName() == "xcb":
            # Asked for as a taskbar would; the application's own request above
            # may be refused.
            activate(int(self.winId()))
        self.tabs.tabBar().setFocus()

    def new_recovery_key(self) -> None:
        """Make a new recovery key and show it; the previous one stops working."""
        if self._recovery is None:
            return
        if self._recovery.exists():
            answer = QMessageBox.question(
                self,
                "Stickle",
                self.tr(
                    "Make a new recovery key? The one you have now will no longer open your notes."
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            recovery_key = self._recovery.make()
        except OSError as error:
            log.error("could not make a recovery key: %s", type(error).__name__)
            QMessageBox.warning(self, "Stickle", self.tr("The recovery key could not be saved."))
            return
        if show_recovery_key(RecoveryKeyDialog(recovery_key, self)):
            self._recovery.kept()
        self.refresh()  # its date

    def dates(self) -> str:
        """When the recovery key was made and the master password set, one line."""
        parts: list[str] = []
        if self._recovery is not None:
            if not self._recovery.exists():
                parts.append(self.tr("No recovery key yet"))
            elif (made := self._recovery.made_at()) is None:
                parts.append(self.tr("Recovery key: made with an earlier Stickle"))
            else:
                parts.append(self.tr("Recovery key: made %1").replace("%1", local_time(made)))
        if self._master is not None:
            if not self._master.exists():
                parts.append(self.tr("No master password"))
            elif (set_at := self._master.set_at()) is not None:
                parts.append(self.tr("Master password: set %1").replace("%1", local_time(set_at)))
        return " · ".join(parts)

    def set_master_password(self) -> None:
        """Set it, or change it when there is one."""
        if self._master is not None:
            show_master_dialog(MasterPasswordDialog(self._master, self))
            self.refresh()

    def check_master_password(self) -> None:
        if self._master is not None:
            show_master_dialog(CheckMasterPasswordDialog(self._master, self))

    def _choose_language(self, index: int) -> None:
        code = self.language_box.itemData(index)
        self._translations.apply(code if isinstance(code, str) else None)

    def _switch_app_list(self, on: bool) -> None:
        if self._app_list is not None:
            switch_app_list(self._app_list, on)
            self.app_list_box.setChecked(self._app_list.added)

    def _switch_autostart(self, on: bool) -> None:
        if self._autostart is not None:
            switch_autostart(self._autostart, on)
            self.autostart_box.setChecked(self._autostart.enabled)
