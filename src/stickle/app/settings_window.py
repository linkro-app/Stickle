"""Stickle's settings: what is set once and left, each kind on a page of its own.

What is done now and then (a new note, hiding them all, a backup) stays in
the menus. A change takes effect and is kept as it is made: there is nothing
to confirm, and Esc closes the window. A page with nothing on it yet is not
shown.
"""

import itertools
import logging
from collections.abc import Callable
from datetime import time
from typing import override

from PySide6.QtCore import QEvent, QLocale, QObject, QSize, Qt, QTime
from PySide6.QtGui import QFont, QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from stickle.app.app_list import switch_app_list
from stickle.app.color_dialog import ColorDialog
from stickle.app.first_run import LoginNote
from stickle.app.i18n import LANGUAGES, Translations
from stickle.app.in_use import OnTopWhileInUse
from stickle.app.master_password import (
    CheckMasterPasswordDialog,
    MasterPasswordDialog,
    MasterPasswords,
)
from stickle.app.notes import NoteManager
from stickle.app.palette import SWATCH_SIZE, own_color_name, swatch_icon
from stickle.app.recovery_key_dialog import RecoveryKeyDialog, RecoveryKeys
from stickle.app.secret_dialogs import NewSecretPasswordDialog, OpenSecretDialog
from stickle.app.shortcut_rows import ShortcutRows
from stickle.app.shortcuts import GlobalShortcuts
from stickle.app.times import local_time
from stickle.app.tray import switch_autostart
from stickle.app.update_check import UpdateCheck
from stickle.app.window_flags import set_stays_on_top, stays_on_top
from stickle.core.updates import FEED_HOST
from stickle.crypto.keyfile import KeyFileError
from stickle.data.colors import MAX_OWN_COLORS
from stickle.data.settings import SNOOZE_CHOICES
from stickle.platform.autostart import Autostart
from stickle.platform.linux.appimage import AppMenuEntry
from stickle.platform.linux.x11 import activate

log = logging.getLogger(__name__)


def _exec(dialog: RecoveryKeyDialog) -> bool:
    return dialog.exec() == RecoveryKeyDialog.DialogCode.Accepted


show_recovery_key: Callable[[RecoveryKeyDialog], bool] = _exec  # replaced in tests


def _exec_master(dialog: QDialog) -> object:
    return dialog.exec()


show_master_dialog: Callable[[QDialog], object] = _exec_master  # replaced in tests


def _exec_secret(dialog: QDialog) -> bool:
    return dialog.exec() == QDialog.DialogCode.Accepted


show_secret_dialog: Callable[[QDialog], bool] = _exec_secret  # replaced in tests
show_color_dialog: Callable[[QDialog], bool] = _exec_secret  # replaced in tests
OWN_LIST_HEIGHT = 140


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
        # New notes' colour at random, and the colours the user added.
        self.random_box = QCheckBox(self.notes_page)
        self.random_box.clicked.connect(notes.set_random_color)
        notes_layout.addWidget(self.random_box)
        self.random_note = _hint(self.notes_page)
        notes_layout.addWidget(self.random_note)
        self.own_label = QLabel(self.notes_page)
        self.own_list = QListWidget(self.notes_page)
        self.own_list.setIconSize(QSize(SWATCH_SIZE, SWATCH_SIZE))
        self.own_list.setMaximumHeight(OWN_LIST_HEIGHT)
        self.own_list.installEventFilter(self)  # Alt+Up and Alt+Down move a colour
        self.own_list.currentRowChanged.connect(self._own_chosen)
        self.own_label.setBuddy(self.own_list)
        self.add_own_button = QPushButton(self.notes_page)
        self.add_own_button.clicked.connect(self.add_own_color)
        self.remove_own_button = QPushButton(self.notes_page)
        self.remove_own_button.clicked.connect(self.remove_own_color)
        own_buttons = QHBoxLayout()
        own_buttons.addWidget(self.add_own_button)
        own_buttons.addWidget(self.remove_own_button)
        own_buttons.addStretch(1)
        self.own_note = _hint(self.notes_page)
        notes_layout.addWidget(self.own_label)
        notes_layout.addWidget(self.own_list)
        notes_layout.addLayout(own_buttons)
        notes_layout.addWidget(self.own_note)
        notes.own_colors_changed.connect(self._refresh_own_colors)
        notes_layout.addStretch(1)

        # Reminders: the morning, and how long "remind me again" waits.
        self.reminders_page, reminders_layout = _page(self.tabs)
        self.morning_label = QLabel(self.reminders_page)
        self.morning_edit = QTimeEdit(self.reminders_page)
        self.morning_edit.setDisplayFormat(QLocale().timeFormat(QLocale.FormatType.ShortFormat))
        self.morning_label.setBuddy(self.morning_edit)
        self.morning_edit.timeChanged.connect(self._set_morning)
        self.morning_note = _hint(self.reminders_page)
        self.snooze_label = QLabel(self.reminders_page)
        self.snooze_box = QComboBox(self.reminders_page)
        for minutes in SNOOZE_CHOICES:
            self.snooze_box.addItem("", minutes)
        self.snooze_label.setBuddy(self.snooze_box)
        self.snooze_box.currentIndexChanged.connect(self._set_snooze)
        for label, field in (
            (self.morning_label, self.morning_edit),
            (self.snooze_label, self.snooze_box),
        ):
            row = QHBoxLayout()
            row.addWidget(label)
            row.addWidget(field)
            row.addStretch(1)
            reminders_layout.addLayout(row)
            if field is self.morning_edit:
                reminders_layout.addWidget(self.morning_note)
        reminders_layout.addStretch(1)

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
        # Secret notes, once there are any: a new password for them.
        self.secret_password_button = QPushButton(self.protection)
        self.secret_password_button.clicked.connect(self.change_secret_password)
        for button in (
            self.recovery_button,
            self.master_button,
            self.check_master_button,
            self.secret_password_button,
        ):
            row = QHBoxLayout()
            row.addWidget(button)
            row.addStretch(1)
            protection.addLayout(row)
        # When an open secret note locks again by itself.
        self.relock_before = QLabel(self.protection)
        self.relock_box = QSpinBox(self.protection)
        self.relock_box.setRange(1, 60)
        self.relock_before.setBuddy(self.relock_box)
        self.relock_after = QLabel(self.protection)
        self.relock_box.valueChanged.connect(self._set_relock_minutes)
        relock = QHBoxLayout()
        relock.addWidget(self.relock_before)
        relock.addWidget(self.relock_box)
        relock.addWidget(self.relock_after)
        relock.addStretch(1)
        protection.addLayout(relock)
        self.leave_box = QCheckBox(self.protection)
        self.leave_box.clicked.connect(notes.set_lock_on_leave)
        protection.addWidget(self.leave_box)
        for part in (self.relock_before, self.relock_box, self.relock_after, self.leave_box):
            part.setVisible(notes.secrets is not None)
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
            self.reminders_page,
            self.protection,
            self.shortcuts_page,
            self.connections_page,
        ):
            self.tabs.addTab(page, "")
        self.tabs.setTabVisible(self.tabs.indexOf(self.reminders_page), notes.reminders is not None)
        self.recovery_button.setVisible(recovery is not None)
        self.tabs.setTabVisible(
            self.tabs.indexOf(self.protection),
            recovery is not None or master is not None or notes.secrets is not None,
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
            self.random_box,
            self.own_list,
            self.add_own_button,
            self.remove_own_button,
            self.morning_edit,
            self.snooze_box,
            self.recovery_button,
            self.master_button,
            self.check_master_button,
            self.secret_password_button,
            self.relock_box,
            self.leave_box,
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
        self.tabs.setTabText(self.tabs.indexOf(self.reminders_page), self.tr("Reminders"))
        self.morning_label.setText(self.tr("&Morning:"))
        morning = self.tr(
            'When "Tomorrow morning" and "Next Monday" ring, and a reminder written'
            " with a day but no time."
        )
        self.morning_note.setText(morning)
        self.morning_edit.setAccessibleDescription(morning)
        self.snooze_label.setText(self.tr('"Remind me &again" waits:'))
        for index, minutes in enumerate(SNOOZE_CHOICES):
            self.snooze_box.setItemText(
                index,
                self.tr("%n hour(s)", "", minutes // 60)
                if minutes % 60 == 0
                else self.tr("%n minute(s)", "", minutes),
            )
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
        self.random_box.setText(self.tr("Give new notes a &random colour"))
        random_note = self.tr(
            "Any of the colours, yours too, but the one used just before. A new note"
            " made from a note (Ctrl+N) takes that note's colour."
        )
        self.random_note.setText(random_note)
        self.random_box.setAccessibleDescription(random_note)
        self.own_label.setText(self.tr("My &colours:"))
        self.own_list.setAccessibleName(self.tr("My colours"))
        self.add_own_button.setText(self.tr("A&dd…"))
        self.remove_own_button.setText(self.tr("Remo&ve"))
        own_note = self.tr(
            "Up to %1, shown under the note menu's colours. Alt+Up and Alt+Down change"
            " their order. Notes of a colour removed keep it."
        ).replace("%1", str(MAX_OWN_COLORS))
        self.own_note.setText(own_note)
        self.own_list.setAccessibleDescription(own_note)
        self.recovery_button.setText(self.tr("Make a new &recovery key…"))
        self.check_master_button.setText(self.tr("Chec&k the spare password…"))
        self.secret_password_button.setText(self.tr("Change the &secret notes' password…"))
        self.relock_before.setText(self.tr("Cover a shown secret note again a&fter"))
        self.relock_after.setText(self.tr("minutes left alone"))
        self.relock_box.setAccessibleName(
            self.tr("Minutes a shown secret note is left alone before it is covered again")
        )
        self.leave_box.setText(self.tr("Also cover it as soon as I &use another window"))
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
        self.random_box.setChecked(self._notes.random_color)
        self._refresh_own_colors()
        reminders = self._notes.reminders
        if reminders is not None:
            # Shown as kept: not a change to keep.
            for field in (self.morning_edit, self.snooze_box):
                field.blockSignals(True)
            morning = reminders.morning
            self.morning_edit.setTime(QTime(morning.hour, morning.minute))
            self.snooze_box.setCurrentIndex(SNOOZE_CHOICES.index(reminders.snooze_minutes))
            for field in (self.morning_edit, self.snooze_box):
                field.blockSignals(False)
        master = self._master is not None and self._master.exists()
        self.master_button.setVisible(self._master is not None)
        self.master_button.setText(
            self.tr("Change the s&pare password…") if master else self.tr("Set a s&pare password…")
        )
        self.check_master_button.setVisible(master)
        secrets = self._notes.secrets
        self.secret_password_button.setVisible(secrets is not None and secrets.turned_on())
        self.relock_box.blockSignals(True)  # shown as kept: not a change to keep
        self.relock_box.setValue(self._notes.relock_minutes)
        self.relock_box.blockSignals(False)
        self.leave_box.setChecked(self._notes.lock_on_leave)
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
        secrets = self._notes.secrets
        secret_key: bytearray | None = None
        if secrets is not None and secrets.turned_on():
            # The new key opens them too: their key, to seal for it, is opened first.
            opened = OpenSecretDialog(secrets, parent=self, purpose="recovery")
            key = opened.take_key() if show_secret_dialog(opened) else None
            if key is None or self._recovery.make_with_secret is None:
                return
            secret_key = bytearray(key)
        try:
            if secret_key is not None and self._recovery.make_with_secret is not None:
                recovery_key = self._recovery.make_with_secret(bytes(secret_key))
            else:
                recovery_key = self._recovery.make()
        except (OSError, KeyFileError) as error:
            log.error("could not make a recovery key: %s", type(error).__name__)
            QMessageBox.warning(self, "Stickle", self.tr("The recovery key could not be saved."))
            return
        finally:
            if secret_key is not None:
                secret_key[:] = bytes(len(secret_key))
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
                parts.append(self.tr("No spare password"))
            elif (set_at := self._master.set_at()) is not None:
                parts.append(self.tr("Spare password: set %1").replace("%1", local_time(set_at)))
        secrets = self._notes.secrets
        if secrets is not None and secrets.turned_on() and (set_at := secrets.set_at()):
            parts.append(
                self.tr("Secret notes' password: set %1").replace("%1", local_time(set_at))
            )
        return " · ".join(parts)

    def set_master_password(self) -> None:
        """Set it, or change it when there is one."""
        if self._master is not None:
            show_master_dialog(MasterPasswordDialog(self._master, self))
            self.refresh()

    def check_master_password(self) -> None:
        if self._master is not None:
            show_master_dialog(CheckMasterPasswordDialog(self._master, self))

    def change_secret_password(self) -> None:
        """Their password now, or the recovery key, then a new one."""
        secrets = self._notes.secrets
        if secrets is None or not secrets.turned_on():
            return
        opened = OpenSecretDialog(secrets, parent=self, purpose="change")
        key = opened.take_key() if show_secret_dialog(opened) else None
        if key is None:
            return
        if not opened.recovered:  # that way, the new one was asked for already
            show_secret_dialog(NewSecretPasswordDialog(secrets, key, self))
        self.refresh()  # its date

    def _refresh_own_colors(self) -> None:
        """The user's own colours as they are now, the one chosen kept chosen."""
        chosen_id = self._chosen_own_id()
        self.own_list.clear()
        for number, color in enumerate(self._notes.own_color_entries(), start=1):
            item = QListWidgetItem(swatch_icon(color.hex), f"{own_color_name(number)}  {color.hex}")
            item.setData(Qt.ItemDataRole.UserRole, color.id)
            self.own_list.addItem(item)
            if color.id == chosen_id:
                self.own_list.setCurrentItem(item)
        full = self.own_list.count() >= MAX_OWN_COLORS
        self.add_own_button.setEnabled(not full)
        self._own_chosen(self.own_list.currentRow())

    def _own_chosen(self, row: int) -> None:
        self.remove_own_button.setEnabled(row >= 0)

    def _chosen_own_id(self) -> str | None:
        row = self.own_list.currentRow()
        value = self.own_list.item(row).data(Qt.ItemDataRole.UserRole) if row >= 0 else None
        return value if isinstance(value, str) else None

    def add_own_color(self) -> None:
        """A colour chosen in the colour window, last among the user's own."""
        dialog = ColorDialog("yellow", self)
        if show_color_dialog(dialog):
            self._notes.add_own_color(dialog.chosen())

    def remove_own_color(self) -> None:
        color_id = self._chosen_own_id()
        if color_id is not None:
            row = self.own_list.currentRow()
            self._notes.remove_own_color(color_id)
            self.own_list.setCurrentRow(min(row, self.own_list.count() - 1))
            self.own_list.setFocus()

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.own_list
            and isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and event.modifiers() == Qt.KeyboardModifier.AltModifier
            and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down)
            and (color_id := self._chosen_own_id()) is not None
        ):
            self._notes.move_own_color(color_id, -1 if event.key() == Qt.Key.Key_Up else 1)
            return True
        return super().eventFilter(watched, event)

    def _set_relock_minutes(self, minutes: int) -> None:
        self._notes.set_relock_minutes(minutes)

    def _set_morning(self, at: QTime) -> None:
        if self._notes.reminders is not None:
            self._notes.reminders.set_morning(time(at.hour(), at.minute()))

    def _set_snooze(self, index: int) -> None:
        if self._notes.reminders is not None and index >= 0:
            self._notes.reminders.set_snooze_minutes(SNOOZE_CHOICES[index])

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
