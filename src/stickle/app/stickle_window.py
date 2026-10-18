"""The Stickle window: the list of notes, and what the tray menu offers.

It opens when Stickle starts with only hidden notes, when it is started
again while already running, and, where there is no tray, when the last
note is hidden: then it says so, and closing it ends Stickle.
"""

import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import override

import apsw
from PySide6.QtCore import QEvent, QRect, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QFont,
    QGuiApplication,
    QHideEvent,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QPalette,
    QPen,
    QShortcut,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)

from stickle.app import note_window
from stickle.app.about_dialog import AboutDialog
from stickle.app.app_list import switch_app_list
from stickle.app.backups import RestoreDialog
from stickle.app.data_menu import DataAccess, export_everything
from stickle.app.finder import DesktopFinder
from stickle.app.first_run import LoginNote
from stickle.app.i18n import LANGUAGES, Translations
from stickle.app.label_manager import LabelManager
from stickle.app.labels import category_item_icon
from stickle.app.note_list import NoteList
from stickle.app.notes import NoteManager, clipboard_text
from stickle.app.recovery_key_dialog import RecoveryKeyDialog
from stickle.app.shortcut_guide import KEYS as GUIDE_KEYS
from stickle.app.shortcut_guide import ShortcutGuide
from stickle.app.shortcut_rows import ShortcutRows
from stickle.app.shortcuts import GlobalShortcuts
from stickle.app.sizing import grow_to_fit
from stickle.app.tray import switch_autostart
from stickle.app.update_check import UpdateCheck
from stickle.app.window_flags import keep_stays_on_top, set_stays_on_top, stays_on_top
from stickle.core.markdown import note_title
from stickle.data.settings import LIST_WINDOW_SIZE, Settings
from stickle.platform.autostart import Autostart, may_be_blocked
from stickle.platform.linux.appimage import AppMenuEntry
from stickle.platform.linux.x11 import activate

DEFAULT_SIZE = (460, 700)
NOTICE_PADDING = 10  # inside the notice's frame
NOTICE_BORDER = 2
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryKeys:
    """Making the recovery key, without the window knowing the database key."""

    exists: Callable[[], bool]
    make: Callable[[], str]  # a new one, the old one no longer working
    kept: Callable[[], None]  # the user said the new one is kept somewhere safe


def _exec(dialog: RecoveryKeyDialog) -> bool:
    return dialog.exec() == RecoveryKeyDialog.DialogCode.Accepted


show_recovery_key: Callable[[RecoveryKeyDialog], bool] = _exec  # replaced in tests


class StickleWindow(QWidget):
    closed = Signal()  # the user closed the window

    def __init__(
        self,
        notes: NoteManager,
        translations: Translations,
        on_quit: Callable[[], object],
        autostart: Autostart | None = None,
        app_list: AppMenuEntry | None = None,
        recovery: RecoveryKeys | None = None,
        settings: Settings | None = None,
        login_may_be_blocked: bool | None = None,
        shortcuts: GlobalShortcuts | None = None,
        portable_folder: Path | None = None,
        data: DataAccess | None = None,
        updates: UpdateCheck | None = None,
    ) -> None:
        super().__init__()
        self._notes = notes
        self._translations = translations
        self._autostart = autostart
        self._app_list = app_list
        self._recovery = recovery
        self._settings = settings
        self.setMinimumWidth(320)
        size = settings.get(LIST_WINDOW_SIZE) if settings else None
        self.resize(*(size or DEFAULT_SIZE))

        self.autostart_box = QCheckBox(self)
        self.autostart_box.setVisible(autostart is not None)
        self.autostart_box.clicked.connect(self._switch_autostart)
        if login_may_be_blocked is None:
            login_may_be_blocked = may_be_blocked()
        self.login_note = LoginNote(self)
        self.login_note.setVisible(autostart is not None and login_may_be_blocked)
        self.app_list_box = QCheckBox(self)
        self.app_list_box.setVisible(app_list is not None)
        self.app_list_box.clicked.connect(self._switch_app_list)
        # The daily check for a newer Stickle (not where a store keeps it up to date),
        # and, while one is out, a line saying so at the top.
        self._updates = updates
        self.update_box = QCheckBox(self)
        self.update_box.setVisible(updates is not None)
        self.new_version_label = QLabel(self)
        self.new_version_label.setWordWrap(True)
        self.new_version_button = QPushButton(self)
        self.new_version_button.clicked.connect(self._get_new_version)
        if updates is not None:
            self.update_box.clicked.connect(updates.set_enabled)
            updates.found.connect(self._update_news)
            updates.switched.connect(self._update_news)
        self.recovery_button = QPushButton(self)
        self.recovery_button.setVisible(recovery is not None)
        self.recovery_button.clicked.connect(self.new_recovery_key)

        # Shown only when there is no tray and every note was hidden: the window
        # then opened by itself, so it says why at the top, where it is seen, and
        # offers the two ways on (a single line was passed over in testing). Its
        # parts sit in the window's own layout, framed by paintEvent: inside a
        # box with a layout of its own, the wrapped text's last line was cut off.
        self.notice_heading = QLabel(self)
        heading_font = QFont(self.notice_heading.font())
        heading_font.setBold(True)
        heading_font.setPointSizeF(heading_font.pointSizeF() * 1.2)
        self.notice_heading.setFont(heading_font)
        self.notice_heading.setContentsMargins(NOTICE_PADDING, NOTICE_PADDING, NOTICE_PADDING, 0)
        self.notice_text = QLabel(self)
        self.notice_text.setWordWrap(True)
        self.notice_text.setContentsMargins(NOTICE_PADDING, 0, NOTICE_PADDING, 0)
        self.notice_buttons = QWidget(self)
        self.notice_show_button = QPushButton(self.notice_buttons)
        self.notice_show_button.clicked.connect(notes.show_all_hidden)
        self.notice_quit_button = QPushButton(self.notice_buttons)
        self.notice_quit_button.clicked.connect(on_quit)
        notice_buttons = QHBoxLayout(self.notice_buttons)
        notice_buttons.setContentsMargins(NOTICE_PADDING, 0, NOTICE_PADDING, NOTICE_PADDING)
        notice_buttons.addWidget(self.notice_show_button)
        notice_buttons.addWidget(self.notice_quit_button)
        self._notice_parts: tuple[QWidget, ...] = (
            self.notice_heading,
            self.notice_text,
            self.notice_buttons,
        )
        for part in self._notice_parts:
            part.hide()

        self.new_note_button = QPushButton(self)
        self.new_note_button.clicked.connect(notes.new_note)
        self.raise_button = QPushButton(self)
        self.raise_button.clicked.connect(notes.raise_all)
        self.clipboard_button = QPushButton(self)
        self.clipboard_button.clicked.connect(notes.note_from_clipboard)
        self.set_aside_button = QPushButton(self)
        self.set_aside_button.clicked.connect(notes.switch_set_aside)
        # Said for as long as it holds, where the way back is (no pop-up).
        self.set_aside_label = QLabel(self)
        self.set_aside_label.setWordWrap(True)
        notes.set_aside_changed.connect(self.refresh_set_aside)
        # On the desktop, one category's notes only; said for as long as it holds.
        self.view_label = QLabel(self)
        self.view_box = QComboBox(self)
        self.view_label.setBuddy(self.view_box)
        self.view_box.activated.connect(self._choose_view)
        self.all_notes_button = QPushButton(self)
        self.all_notes_button.clicked.connect(lambda: notes.set_view(None))
        self.view_status = QLabel(self)
        self.view_status.setWordWrap(True)
        notes.view_changed.connect(self.refresh_view)
        # Portable: where the notes are, said for as long as it lasts.
        self._portable_folder = portable_folder
        self.portable_label = QLabel(self)
        self.portable_label.setWordWrap(True)
        self.portable_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.portable_label.setVisible(portable_folder is not None)
        QGuiApplication.clipboard().dataChanged.connect(self.refresh_clipboard)

        self.note_list = NoteList(notes, self)
        self.show_all_button = QPushButton(self)
        self.show_all_button.clicked.connect(notes.show_all_hidden)
        self.restore_button = QPushButton(self)
        self.restore_button.clicked.connect(notes.restore_last_deleted)
        self.labels_button = QPushButton(self)
        self.labels_button.clicked.connect(self.manage_labels)
        self.label_manager: LabelManager | None = None
        # The keys at a glance, for the whole app: from here, a note or the tray.
        self.guide = ShortcutGuide(shortcuts)
        self.guide_button = QPushButton(self)
        self.guide_button.clicked.connect(self.guide.toggle)
        notes.guide_requested.connect(self.guide.toggle)
        self.about = AboutDialog(
            checks_updates=updates is not None, portable=portable_folder is not None
        )
        self.about_button = QPushButton(self)
        self.about_button.clicked.connect(self.about.present)
        # Backups (and later exporting and importing), from a menu on one button.
        self._data = data
        self.restore_dialog: RestoreDialog | None = None
        self.data_button = QPushButton(self)
        self.data_menu = QMenu(self.data_button)
        self.export_action = self.data_menu.addAction("")
        self.export_action.triggered.connect(self.export_all)
        self.data_menu.addSeparator()
        self.restore_backup_action = self.data_menu.addAction("")
        self.restore_backup_action.triggered.connect(self.restore_backup)
        self.data_button.setMenu(self.data_menu)
        self.data_button.setVisible(data is not None)
        # Finding notes on the desktop; what it also finds hidden or deleted is here.
        self.finder = DesktopFinder(notes, self.search_for)
        for keys in GUIDE_KEYS:
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.activated.connect(self.guide.toggle)

        self.language_label = QLabel(self)
        self.language_box = QComboBox(self)
        self.language_label.setBuddy(self.language_box)
        for code, native_name in LANGUAGES:
            self.language_box.addItem(native_name, code)
        self.language_box.activated.connect(self._choose_language)
        self.quit_button = QPushButton(self)
        self.quit_button.clicked.connect(on_quit)
        self.shortcut_rows = (
            ShortcutRows(shortcuts, self, portable=portable_folder is not None)
            if shortcuts is not None
            else None
        )

        buttons = QHBoxLayout()
        buttons.addWidget(self.new_note_button)
        buttons.addWidget(self.raise_button)
        more_buttons = QHBoxLayout()
        more_buttons.addWidget(self.clipboard_button)
        more_buttons.addWidget(self.set_aside_button)
        view = QHBoxLayout()
        view.addWidget(self.view_label)
        view.addWidget(self.view_box, 1)
        view.addWidget(self.all_notes_button)
        language = QHBoxLayout()
        language.addWidget(self.language_label)
        language.addWidget(self.language_box, 1)
        layout = QVBoxLayout(self)
        for part in self._notice_parts:
            layout.addWidget(part)
        self._after_notice = QSpacerItem(0, 0)  # room below the frame, while shown
        layout.addItem(self._after_notice)
        new_version = QHBoxLayout()
        new_version.addWidget(self.new_version_label, 1)
        new_version.addWidget(self.new_version_button)
        layout.addLayout(new_version)
        layout.addWidget(self.portable_label)
        layout.addWidget(self.set_aside_label)
        layout.addWidget(self.view_status)
        layout.addLayout(buttons)
        layout.addLayout(more_buttons)
        layout.addLayout(view)
        layout.addWidget(self.note_list, 1)
        layout.addWidget(self.show_all_button)
        layout.addWidget(self.restore_button)
        tools = QHBoxLayout()
        tools.addWidget(self.labels_button, 1)
        tools.addWidget(self.data_button)
        tools.addWidget(self.guide_button)
        tools.addWidget(self.about_button)
        layout.addLayout(tools)
        layout.addLayout(language)
        layout.addWidget(self.autostart_box)
        layout.addWidget(self.login_note)
        layout.addWidget(self.app_list_box)
        layout.addWidget(self.update_box)
        layout.addWidget(self.recovery_button)
        if self.shortcut_rows is not None:
            layout.addWidget(self.shortcut_rows)
        layout.addWidget(self.quit_button)

        self._set_tab_order()
        notes.changed.connect(self.refresh)
        translations.changed.connect(self.retranslate)
        self.retranslate()

    def search_for(self, term: str) -> None:
        """Open, listing every note (hidden ones and the trash's too) with term."""
        self.open()
        note_list = self.note_list
        note_list.filter_box.setCurrentIndex(0)  # all notes
        note_list.search_box.setText(term)
        note_list.refresh()
        note_list.search_box.setFocus()

    def export_all(self) -> None:
        """Every note as Markdown and everything as JSON, into a folder the user picks."""
        if self._data is not None:
            export_everything(self, self._data)

    def restore_backup(self) -> None:
        """The window for going back to a backup, one at a time, its list fresh."""
        if self._data is None:
            return
        if self.restore_dialog is None:
            self.restore_dialog = RestoreDialog(self._data.backups, self)
        self.restore_dialog.refresh()
        self.restore_dialog.show()
        self.restore_dialog.raise_()
        self.restore_dialog.activateWindow()

    def manage_labels(self) -> None:
        """The window for categories and marks, one at a time, brought forward if open."""
        if self.label_manager is None:
            self.label_manager = LabelManager(self._notes, self)
        self.label_manager.show()
        self.label_manager.raise_()
        self.label_manager.activateWindow()

    def _set_tab_order(self) -> None:
        """Tab goes as the window reads, top to bottom, not in the order its parts
        were made (hidden and disabled ones are passed over by Qt)."""
        order: list[QWidget] = [
            self.notice_show_button,
            self.notice_quit_button,
            self.new_version_button,
            self.new_note_button,
            self.raise_button,
            self.clipboard_button,
            self.set_aside_button,
            self.view_box,
            self.all_notes_button,
            *self.note_list.tab_order(),
            self.show_all_button,
            self.restore_button,
            self.labels_button,
            self.data_button,
            self.guide_button,
            self.about_button,
            self.language_box,
            self.autostart_box,
            self.app_list_box,
            self.update_box,
            self.recovery_button,
            *(self.shortcut_rows.tab_order() if self.shortcut_rows is not None else []),
            self.quit_button,
        ]
        for before, after in itertools.pairwise(order):
            QWidget.setTabOrder(before, after)

    def retranslate(self) -> None:
        self._set_title()
        self.notice_heading.setText(self.tr("All notes are hidden"))
        self.notice_text.setText(
            self.tr(
                "Closing this window quits Stickle. The hidden notes are listed below, "
                "and here again the next time you start it."
            )
        )
        self.notice_show_button.setText(self.tr("Show all hidden notes"))
        self.notice_quit_button.setText(self.tr("Quit Stickle"))
        self.new_note_button.setText(self.tr("New note"))
        self.raise_button.setText(self.tr("Bring all notes to front"))
        self.clipboard_button.setText(self.tr("New note from clipboard"))
        if self._portable_folder is not None:
            self.portable_label.setText(
                self.tr("Portable: the notes are in %1, opened with their password.").replace(
                    "%1", str(self._portable_folder)
                )
            )
        self.set_aside_label.setText(
            self.tr("All notes are out of sight for now. They come back as they were.")
        )
        self.refresh_set_aside()
        self.view_label.setText(self.tr("On the &desktop:"))
        self.all_notes_button.setText(self.tr("Show all"))
        self.all_notes_button.setAccessibleName(self.tr("Show all notes on the desktop"))
        self.refresh_view()
        self.note_list.retranslate()
        self.show_all_button.setText(self.tr("Show all hidden notes"))
        self.labels_button.setText(self.tr("Manage &categories and marks…"))
        self.data_button.setText(self.tr("&Data"))
        self.export_action.setText(self.tr("Export all notes…"))
        self.restore_backup_action.setText(self.tr("Restore from a backup…"))
        self.guide_button.setText(self.tr("Keyboard shortcuts"))
        self.guide_button.setToolTip(self.tr("F1", "the key that opens the shortcuts"))
        self.about_button.setText(self.tr("About Stickle"))
        self.language_label.setText(self.tr("&Language"))
        self.language_box.setAccessibleName(self.tr("Language"))
        self.language_box.setItemText(0, self.tr("System language"))
        self.quit_button.setText(self.tr("Quit Stickle"))
        self.autostart_box.setText(self.tr("&Start Stickle when I log in"))
        self.app_list_box.setText(self.tr("Show Stickle in the &app list"))
        self.update_box.setText(self.tr("Check for a new &version daily"))
        self.new_version_button.setText(self.tr("&Get it…"))
        self.refresh_new_version()
        self.recovery_button.setText(self.tr("Make a new &recovery key…"))
        self.refresh()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def refresh(self) -> None:
        """Show the notes, the note just deleted and the language as they are now."""
        self.note_list.refresh()
        self.show_all_button.setEnabled(bool(self._notes.hidden_notes()))
        self.refresh_view()  # categories may have been made, renamed or deleted

        deleted = self._notes.last_deleted()
        self.restore_button.setEnabled(deleted is not None)
        if deleted is None:
            self.restore_button.setText(self.tr("Restore the note just deleted"))
        else:
            title = note_title(deleted.body) or self.tr("(empty note)")
            self.restore_button.setText(
                self.tr("Restore the note just deleted: %1").replace("%1", title)
            )
        self.language_box.setCurrentIndex(
            max(0, self.language_box.findData(self._translations.language))
        )
        self.refresh_switches()
        self.refresh_clipboard()

    def refresh_view(self) -> None:
        """The views there are, the one on the desktop chosen and said."""
        self.view_box.clear()
        categories = {c.id: c for c in self._notes.label_choices()[0]}
        background = self.view_box.palette().base().color()
        for view, name in self._notes.view_choices():
            category = categories.get(view) if view is not None else None
            icon = category_item_icon(category, background) if category else QIcon()
            self.view_box.addItem(icon, name, view)
        self.view_box.setCurrentIndex(max(0, self.view_box.findData(self._notes.view)))
        one = self._notes.view is not None
        self.all_notes_button.setVisible(one)
        self.view_status.setVisible(one)
        if one:
            self.view_status.setText(
                self.tr(
                    "Only “%1” is on the desktop. The other notes come back with Show all."
                ).replace("%1", self._notes.view_name())
            )

    def _choose_view(self, index: int) -> None:
        view = self.view_box.itemData(index)
        self._notes.set_view(view if isinstance(view, str) else None)

    def refresh_set_aside(self) -> None:
        aside = self._notes.set_aside
        self.set_aside_label.setVisible(aside)
        if aside:
            self.set_aside_button.setText(self.tr("Show the notes again"))
        else:
            self.set_aside_button.setText(self.tr("Hide all notes for now"))

    def refresh_clipboard(self) -> None:
        self.clipboard_button.setEnabled(bool(clipboard_text()))

    def refresh_switches(self) -> None:
        """Show what the files say: they may have changed outside Stickle, or elsewhere in it."""
        if self._autostart is not None:
            self.autostart_box.setChecked(self._autostart.enabled)
        if self._app_list is not None:
            self.app_list_box.setChecked(self._app_list.added)
        if self._updates is not None:
            self.update_box.setChecked(self._updates.enabled)

    def _update_news(self, _news: object) -> None:
        self.refresh_new_version()

    def refresh_new_version(self) -> None:
        """Said at the top for as long as a newer Stickle is out (no pop-up)."""
        release = self._updates.release if self._updates is not None else None
        self.new_version_label.setVisible(release is not None)
        self.new_version_button.setVisible(release is not None)
        if release is not None:
            text = self.tr("Stickle %1 is out.").replace("%1", release.version)
            self.new_version_label.setText(text)
            self.new_version_button.setAccessibleDescription(text)
        self.refresh_switches()

    def _get_new_version(self) -> None:
        if self._updates is not None and self._updates.release is not None:
            note_window.open_url(QUrl(self._updates.release.page))

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

    def _switch_app_list(self, on: bool) -> None:
        if self._app_list is not None:
            switch_app_list(self._app_list, on)
            self.app_list_box.setChecked(self._app_list.added)

    def _switch_autostart(self, on: bool) -> None:
        if self._autostart is not None:
            switch_autostart(self._autostart, on)
            self.autostart_box.setChecked(self._autostart.enabled)

    @property
    def notice_shown(self) -> bool:
        return self.notice_text.isVisibleTo(self)

    def show_notice(self, shown: bool) -> None:
        """The "all notes are hidden" notice, in the window and its title."""
        for part in self._notice_parts:
            part.setVisible(shown)
        self._after_notice.changeSize(0, NOTICE_PADDING if shown else 0)
        layout = self.layout()
        if layout is not None:
            layout.invalidate()
        self._set_title()
        self.update()
        if shown:
            grow_to_fit(self)  # opened before at its smaller size: the text was cut off

    @override
    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if not self.notice_shown:
            return
        frame = QRect()
        for part in self._notice_parts:
            frame = frame.united(part.geometry())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self.palette().color(QPalette.ColorRole.Highlight), NOTICE_BORDER)
        painter.setPen(pen)
        half = NOTICE_BORDER / 2
        painter.drawRoundedRect(QRectF(frame).adjusted(half, half, -half, -half), 6, 6)

    def _set_title(self) -> None:
        if self.notice_shown:
            self.setWindowTitle(self.tr("All notes are hidden - Stickle"))
        else:
            self.setWindowTitle("Stickle")

    def open(self, notice: bool = False) -> None:
        """Show the window in front, with the "all notes are hidden" notice or not."""
        self.show_notice(notice)
        self.refresh()
        # Window managers may refuse to hand over the keyboard to a window
        # asked for by another program (a second start): kept above the others
        # until the user moves on from it, it is at least seen.
        set_stays_on_top(self, True)
        self.showNormal()
        if notice:
            grow_to_fit(self)  # shown again at the size it had before the notice
        self.raise_()
        self.activateWindow()
        if QGuiApplication.platformName() == "xcb":
            # Asked for as a taskbar would; the application's own request above
            # may be refused.
            activate(int(self.winId()))
        self.note_list.list.setFocus()  # arrow keys and Enter work at once

    @override
    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowActivate:
            self.refresh_switches()  # as the tray does each time its menu opens
            # Some desktops tell only the application in front that the clipboard changed.
            self.refresh_clipboard()
        # Not on activation: that can be reported before the window is in front.
        if (
            event.type() == QEvent.Type.WindowDeactivate or event.type() == QEvent.Type.Hide
        ) and stays_on_top(self):
            set_stays_on_top(self, False)
        return super().event(event)

    def _choose_language(self, index: int) -> None:
        code = self.language_box.itemData(index)
        self._translations.apply(code if isinstance(code, str) else None)

    @override
    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not event.spontaneous():
            # Once shown: Qt may have put back an on-top state since changed.
            QTimer.singleShot(0, self, lambda: keep_stays_on_top(self))

    @override
    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        if self._settings is not None and not self.isMinimized():
            size = [self.width(), self.height()]
            if size != list(DEFAULT_SIZE) or self._settings.get(LIST_WINDOW_SIZE):
                try:
                    self._settings.set(LIST_WINDOW_SIZE, size)
                except apsw.Error as error:  # a size not kept is not worth more than a log line
                    log.warning("could not keep the window size: %s", type(error).__name__)

    @override
    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Esc closes it, as the close button does. It only gets here when nothing
        # inside took it first: a search being cleared, a list being folded, or
        # a shortcut field recording the key.
        if event.key() == Qt.Key.Key_Escape and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            self.close()
            return
        super().keyPressEvent(event)

    @override
    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
        self.closed.emit()
