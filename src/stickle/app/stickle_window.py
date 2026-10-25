"""The Stickle window: the list of notes, and what the tray menu offers.

It opens when Stickle starts with only hidden notes, when it is started
again while already running, and, where there is no tray, when the last
note is hidden: then it says so, and closing it ends Stickle. The settings
are in a window of their own, opened from here and the tray.
"""

import itertools
import logging
from collections.abc import Callable
from pathlib import Path
from typing import override

import apsw
from PySide6.QtCore import QEvent, QRect, QRectF, Qt, QUrl, Signal
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
    QResizeEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from stickle.app import note_window
from stickle.app.about_dialog import AboutDialog
from stickle.app.backups import RestoreDialog
from stickle.app.data_menu import DataAccess, export_everything
from stickle.app.finder import DesktopFinder
from stickle.app.i18n import Translations
from stickle.app.import_dialog import ImportDialog
from stickle.app.in_use import OnTopWhileInUse
from stickle.app.label_manager import LabelManager
from stickle.app.labels import category_item_icon
from stickle.app.master_password import MasterPasswords
from stickle.app.note_list import NoteList
from stickle.app.notes import NoteManager, clipboard_text
from stickle.app.protection_changes import Change, ProtectionChanges
from stickle.app.settings_window import RecoveryKeys, SettingsWindow
from stickle.app.shortcut_guide import KEYS as GUIDE_KEYS
from stickle.app.shortcut_guide import ShortcutGuide
from stickle.app.shortcuts import GlobalShortcuts
from stickle.app.sizing import grow_to_fit
from stickle.app.times import local_time
from stickle.app.update_check import UpdateCheck
from stickle.app.window_flags import set_stays_on_top
from stickle.core.markdown import note_title
from stickle.data.settings import LIST_WINDOW_SIZE, Settings
from stickle.platform.autostart import Autostart, may_be_blocked
from stickle.platform.linux.appimage import AppMenuEntry
from stickle.platform.linux.x11 import activate

DEFAULT_SIZE = (460, 700)
NOTICE_PADDING = 10  # inside the notice's frame
NOTICE_BORDER = 2
SETTINGS_KEYS = "Ctrl+,"  # ⌘, on macOS, where Qt reads Ctrl as Command
log = logging.getLogger(__name__)


class ElidedButton(QPushButton):
    """A button whose text may hold a note's title, of any length: it takes the
    window's width rather than setting it, and ends its text with "…" to fit."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def full_text(self) -> str:
        return self._full

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self.setAccessibleName(text)  # read whole, whatever is shown
        self._fit()

    @override
    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        room = self.width() - 2 * self.style().pixelMetric(QStyle.PixelMetric.PM_ButtonMargin) - 8
        self.setText(
            self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, max(room, 0))
        )


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
        master: MasterPasswords | None = None,
        changes: ProtectionChanges | None = None,
    ) -> None:
        super().__init__()
        self._notes = notes
        self._translations = translations
        self._settings = settings
        # Above the others while in use, from when open() puts it there.
        self._on_top = OnTopWhileInUse(self)
        # No minimum width of its own: its layout's keeps every button as wide as
        # its text (a fixed 320 let "Manage categories and marks…" be cut).
        size = settings.get(LIST_WINDOW_SIZE) if settings else None
        self.resize(*(size or DEFAULT_SIZE))

        if login_may_be_blocked is None:
            login_may_be_blocked = may_be_blocked()
        # One of its own windows, above it and the pinned notes while in use.
        self.settings = SettingsWindow(
            notes,
            translations,
            self,
            autostart=autostart,
            app_list=app_list,
            recovery=recovery,
            login_may_be_blocked=login_may_be_blocked,
            shortcuts=shortcuts,
            portable=portable_folder is not None,
            updates=updates,
            master=master,
        )
        self.settings_button = QPushButton(self)
        self.settings_button.clicked.connect(lambda: self.open_settings())
        settings_shortcut = QShortcut(QKeySequence(SETTINGS_KEYS), self)
        settings_shortcut.activated.connect(lambda: self.open_settings())
        # While a newer Stickle is out (not where a store keeps it up to date),
        # a line saying so at the top.
        self._updates = updates
        self.new_version_label = QLabel(self)
        self.new_version_label.setWordWrap(True)
        self.new_version_button = QPushButton(self)
        self.new_version_button.clicked.connect(self._get_new_version)
        if updates is not None:
            updates.found.connect(self._update_news)
            updates.switched.connect(self._update_news)
        # A change to what protects the notes, told once it is due (the next start,
        # an unlock) until the user says it is fine: someone else may have made it.
        self._changes = changes
        self.changes_label = QLabel(self)
        self.changes_label.setWordWrap(True)
        self.changes_again_button = QPushButton(self)
        self.changes_again_button.clicked.connect(
            lambda: self.open_settings(self.settings.protection)
        )
        self.changes_seen_button = QPushButton(self)
        self.changes_seen_button.clicked.connect(self._changes_seen)

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
        self.restore_button = ElidedButton(self)
        self.restore_button.clicked.connect(notes.restore_last_deleted)
        self.labels_button = QPushButton(self)
        self.labels_button.clicked.connect(self.manage_labels)
        self.label_manager: LabelManager | None = None
        # The keys at a glance, for the whole app: from here, a note or the tray.
        self.guide = ShortcutGuide(shortcuts)
        self.guide_button = QPushButton(self)
        self.guide_button.clicked.connect(self.guide.toggle)
        notes.guide_requested.connect(self.guide.toggle)
        self.guide.settings_requested.connect(self.open_shortcut_settings)
        # One of its own windows, above it and the pinned notes while in use: on its
        # own, the Stickle window letting go of the top was put back above it.
        self.about = AboutDialog(self, portable=portable_folder is not None)
        self.about_button = QPushButton(self)
        self.about_button.clicked.connect(self.about.present)
        # Backups (and later exporting and importing), from a menu on one button.
        self._data = data
        self.restore_dialog: RestoreDialog | None = None
        self.data_button = QPushButton(self)
        self.data_menu = QMenu(self.data_button)
        self.import_dialog: ImportDialog | None = None
        self.import_action = self.data_menu.addAction("")
        self.import_action.triggered.connect(self.bring_in)
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

        self.quit_button = QPushButton(self)
        self.quit_button.clicked.connect(on_quit)

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
        layout = QVBoxLayout(self)
        for part in self._notice_parts:
            layout.addWidget(part)
        self._after_notice = QSpacerItem(0, 0)  # room below the frame, while shown
        layout.addItem(self._after_notice)
        changes_buttons = QHBoxLayout()
        changes_buttons.addStretch(1)
        changes_buttons.addWidget(self.changes_again_button)
        changes_buttons.addWidget(self.changes_seen_button)
        layout.addWidget(self.changes_label)
        layout.addLayout(changes_buttons)
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
        bottom = QHBoxLayout()
        bottom.addWidget(self.settings_button)
        bottom.addStretch(1)
        bottom.addWidget(self.quit_button)
        layout.addLayout(bottom)

        self._set_tab_order()
        notes.changed.connect(self.refresh)
        translations.changed.connect(self.retranslate)
        self.retranslate()

    def search_for(self, term: str) -> None:
        """Open, listing every note with term, hidden ones too, whatever category
        or mark was chosen there; those in the trash are counted below the list,
        a button away."""
        self.open()
        note_list = self.note_list
        note_list.filter_box.setCurrentIndex(0)  # all notes
        note_list.category_box.setCurrentIndex(0)  # all categories
        note_list.mark_box.setCurrentIndex(0)  # all marks
        note_list.search_box.setText(term)
        note_list.refresh()
        note_list.search_box.setFocus()

    def bring_in(self) -> None:
        """The window for bringing notes over from another app, one at a time."""
        if self._data is None:
            return
        if self.import_dialog is None:
            self.import_dialog = ImportDialog(self._data.importing, self)
        self.import_dialog.refresh()
        self.import_dialog.show()
        self.import_dialog.raise_()
        self.import_dialog.activateWindow()

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
            self.changes_again_button,
            self.changes_seen_button,
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
            self.settings_button,
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
        self.data_button.setText(self.tr("Da&ta"))
        self.import_action.setText(self.tr("&Bring notes in from another app…"))
        self.export_action.setText(self.tr("&Export all notes…"))
        self.restore_backup_action.setText(self.tr("&Restore from a backup…"))
        self.guide_button.setText(self.tr("Keyboard shortcuts"))
        self.guide_button.setToolTip(self.tr("F1", "the key that opens the shortcuts"))
        self.about_button.setText(self.tr("About Stickle"))
        self.settings_button.setText(self.tr("&Settings…"))
        self.settings_button.setToolTip(self.tr("Ctrl+,", "the keys that open the settings"))
        self.quit_button.setText(self.tr("Quit Stickle"))
        self.new_version_button.setText(self.tr("&Get it…"))
        self.refresh_new_version()
        self.changes_again_button.setText(self.tr("Set &again…"))
        self.changes_seen_button.setText(self.tr("OK, it was me"))
        self.refresh_changes()
        self.refresh()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def open_settings(self, page: QWidget | None = None) -> None:
        """The settings, on the page last shown unless told which."""
        self.settings.present(page)

    def open_shortcut_settings(self) -> None:
        self.open_settings(self.settings.shortcuts_page)

    def refresh(self) -> None:
        """Show the notes and the note just deleted as they are now."""
        self.note_list.refresh()
        self.show_all_button.setEnabled(bool(self._notes.hidden_notes()))
        self.refresh_view()  # categories may have been made, renamed or deleted

        deleted = self._notes.last_deleted()
        self.restore_button.setEnabled(deleted is not None)
        if deleted is None:
            self.restore_button.set_full_text(self.tr("Restore the note just deleted"))
        else:
            title = note_title(deleted.body) or self.tr("(empty note)")
            self.restore_button.set_full_text(
                self.tr("Restore the note just deleted: %1").replace("%1", title)
            )
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

    def change_text(self, change: Change) -> str:
        when = local_time(change.at)
        texts = {
            "recovery_key": self.tr("On %1 a new recovery key was made on this computer."),
            "master_password_set": self.tr("On %1 a master password was set on this computer."),
            "master_password_changed": self.tr(
                "On %1 the master password was changed on this computer."
            ),
            "secret_password_changed": self.tr(
                "On %1 the secret notes' password was changed on this computer."
            ),
        }
        return texts[change.what].replace("%1", when)

    def changes_told(self) -> str:
        """What is being told, with what to do if it was not the user; "" if nothing."""
        changes = self._changes.to_tell() if self._changes is not None else []
        if not changes:
            return ""
        lines = [self.change_text(change) for change in changes]
        return " ".join([*lines, self.tr("If it was not you, set it again in the settings.")])

    def refresh_changes(self) -> None:
        """At the top for as long as there is a change being told (no pop-up)."""
        text = self.changes_told()
        for part in (self.changes_label, self.changes_again_button, self.changes_seen_button):
            part.setVisible(bool(text))
        self.changes_label.setText(text)
        self.changes_seen_button.setAccessibleDescription(text)

    def _changes_seen(self) -> None:
        if self._changes is not None:
            self._changes.seen()
        self.refresh_changes()

    def _get_new_version(self) -> None:
        if self._updates is not None and self._updates.release is not None:
            note_window.open_url(QUrl(self._updates.release.page))

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
            self.setWindowTitle(self.tr("All notes are hidden - Stickle Manager"))
        else:
            self.setWindowTitle(self.tr("Stickle Manager"))

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
            # Some desktops tell only the application in front that the clipboard changed.
            self.refresh_clipboard()
        return super().event(event)

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
