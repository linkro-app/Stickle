"""System tray icon and its menu."""

import logging
import sys
from collections.abc import Callable

from PySide6.QtCore import QElapsedTimer, QObject, QTimer, QUrl
from PySide6.QtGui import QActionGroup, QGuiApplication
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from stickle.app import note_window
from stickle.app.app_icon import make_icon
from stickle.app.i18n import Translations
from stickle.app.notes import HIDDEN_LISTED, NoteManager, clipboard_text
from stickle.app.support import open_support
from stickle.app.update_check import UpdateCheck
from stickle.core.markdown import note_title
from stickle.core.note import Note
from stickle.platform.autostart import Autostart

log = logging.getLogger(__name__)
TITLE_LENGTH = 40


def switch_autostart(autostart: Autostart, on: bool) -> bool:
    """Turn starting at login on or off; False (and logged) if the file could not be changed."""
    try:
        if on:
            autostart.enable()
        else:
            autostart.disable()
    except OSError as error:
        log.error("could not change start at login: %s", type(error).__name__)
        return False
    return True


def menu_title(note: Note) -> str:
    """The note's title, shortened, with & kept literal (Qt reads it as a shortcut mark)."""
    title = note_title(note.body)
    if len(title) > TITLE_LENGTH:
        title = title[: TITLE_LENGTH - 1] + "…"
    return title.replace("&", "&&")


class MenuWatch(QObject):
    """Closes a menu when another program's window comes to the front while it is
    open. Windows leaves the tray menu on screen when a program comes up by itself
    (a click elsewhere closes it): the window in front as it opened (the menu, or
    the taskbar if it was not let in front) is noted, and any other program's
    window in front after that closes it."""

    INTERVAL_MS = 200

    def __init__(
        self, menu: QMenu, front: Callable[[], int], another_program: Callable[[int], bool]
    ) -> None:
        super().__init__(menu)
        self._menu = menu
        self._front = front
        self._another_program = another_program
        self._at_open: int | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self._look)
        menu.aboutToShow.connect(self._start)
        menu.aboutToHide.connect(self._timer.stop)

    def _start(self) -> None:
        self._at_open = None
        self._timer.start()

    def _look(self) -> None:
        if not self._menu.isVisible():
            self._timer.stop()
            return
        window = self._front()
        if self._at_open is None:
            self._at_open = window
        elif window and window != self._at_open and self._another_program(window):
            self._menu.hide()


class Tray(QSystemTrayIcon):
    def __init__(
        self,
        on_new_note: Callable[[], object],
        on_quit: Callable[[], object],
        translations: Translations,
        notes: NoteManager | None = None,
        updates: UpdateCheck | None = None,
    ) -> None:
        super().__init__(make_icon())
        self._faded = False
        self.setToolTip("Stickle")
        self._notes = notes
        self._updates = updates

        # QSystemTrayIcon does not own its menu, so keep a reference.
        self._menu = QMenu()
        # Some desktops (GNOME's AppIndicator) open this menu on any click, so the
        # Stickle window must be reachable from it, not only from a click on the icon.
        self.open_window_action = self._menu.addAction("")
        # Shown while a newer Stickle is out, for as long as that holds.
        self.new_version_action = self._menu.addAction("")
        self.new_version_action.setVisible(False)
        self.new_version_action.triggered.connect(self._get_new_version)
        self._menu.addSeparator()
        self.new_note_action = self._menu.addAction("")
        self.new_note_action.triggered.connect(on_new_note)
        self.clipboard_action = self._menu.addAction("")
        self.clipboard_action.setVisible(notes is not None)
        self.hidden_menu = self._menu.addMenu("")
        self.restore_action = self._menu.addAction("")
        self.restore_action.triggered.connect(self._restore)
        self.raise_action = self._menu.addAction("")
        self.raise_action.triggered.connect(self._raise_all)
        self.set_aside_action = self._menu.addAction("")
        self.set_aside_action.setVisible(notes is not None)
        # On the desktop, one category's notes only: filled as the menu opens.
        self.view_menu = self._menu.addMenu("")
        self.view_menu.menuAction().setVisible(notes is not None)
        self.view_menu.aboutToShow.connect(self._fill_views)
        if notes is not None:
            self.clipboard_action.triggered.connect(notes.note_from_clipboard)
            self.set_aside_action.triggered.connect(notes.switch_set_aside)
            notes.set_aside_changed.connect(self.retranslate)
            notes.view_changed.connect(self.retranslate)
        self._menu.aboutToShow.connect(self._refresh_clipboard)
        if sys.platform == "win32":
            from stickle.platform.windows.foreground import foreground_window, is_another_program

            self._menu_watch = MenuWatch(self._menu, foreground_window, is_another_program)
        self._menu.addSeparator()

        # Finding notes on the desktop, and the keys at a glance: shown by the
        # Stickle window's finder and guide (see the app).
        self.find_action = self._menu.addAction("")
        self.find_action.setVisible(notes is not None)
        self.guide_action = self._menu.addAction("")
        # Backups (and later exporting and importing): shown by the Stickle window.
        self.data_menu = self._menu.addMenu("")
        self.data_menu.menuAction().setVisible(False)
        self.import_action = self.data_menu.addAction("")
        self.export_action = self.data_menu.addAction("")
        self.data_menu.addSeparator()
        self.restore_backup_action = self.data_menu.addAction("")
        # Language, starting at login and the like: in the settings window,
        # shown by the Stickle window.
        self.settings_action = self._menu.addAction("")
        if updates is not None:
            updates.found.connect(self._update_news)
            updates.switched.connect(self._update_news)  # off: no longer said

        self._menu.addSeparator()
        # One of the two places that mention support (the other is About Stickle).
        # A draft issue in the browser (see the app: About Stickle drafts it).
        self.feedback_action = self._menu.addAction("")
        self.support_action = self._menu.addAction("")
        self.support_action.triggered.connect(open_support)
        self.about_action = self._menu.addAction("")
        self._menu.addSeparator()
        self.quit_action = self._menu.addAction("")
        self.quit_action.triggered.connect(on_quit)
        self.setContextMenu(self._menu)

        # A double click on the icon makes a new note; a single click does nothing,
        # the menu (the right button) holding the rest. StatusNotifierItem trays
        # (KDE) tell of two single clicks instead of a double click.
        self._on_new_note = on_new_note
        self._last_click = QElapsedTimer()
        self._last_note = QElapsedTimer()
        self.activated.connect(self._clicked)

        # Not a widget, so no LanguageChange event: follow the translations instead.
        translations.changed.connect(self.retranslate)
        if notes is not None:
            notes.changed.connect(self.refresh_notes)
        self.retranslate()

    def _clicked(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        interval = QGuiApplication.styleHints().mouseDoubleClickInterval()
        # Windows tells of the clicks of a double click too, after or before it:
        # one note for the lot.
        if self._last_note.isValid() and not self._last_note.hasExpired(interval):
            return
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick or (
            reason == QSystemTrayIcon.ActivationReason.Trigger
            and self._last_click.isValid()
            and not self._last_click.hasExpired(interval)
        ):
            self._last_click.invalidate()
            self._last_note.start()
            self._on_new_note()
        elif reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._last_click.start()

    def refresh_notes(self) -> None:
        """Rebuild the lists of hidden and deleted notes (they change only through the app)."""
        self.hidden_menu.clear()
        hidden = self._notes.hidden_notes() if self._notes else []
        for number, note in enumerate(hidden[:HIDDEN_LISTED], start=1):
            title = menu_title(note) or self.tr("(empty note)")
            # The first nine chosen by their number while the menu is open.
            action = self.hidden_menu.addAction(f"&{number} {title}" if number < 10 else title)
            action.triggered.connect(lambda _=False, note_id=note.id: self._show(note_id))
        if hidden:
            self.hidden_menu.addSeparator()
        show_all = self.hidden_menu.addAction(self.tr("Show &all hidden notes"))
        show_all.triggered.connect(self._show_all)
        self.hidden_menu.setEnabled(bool(hidden))

        deleted = self._notes.last_deleted() if self._notes else None
        self.restore_action.setEnabled(deleted is not None)
        if deleted is None:
            self.restore_action.setText(self.tr("&Restore the note just deleted"))
        else:
            title = menu_title(deleted) or self.tr("(empty note)")
            self.restore_action.setText(
                self.tr("&Restore the note just deleted: %1").replace("%1", title)
            )

    def _show(self, note_id: str) -> None:
        if self._notes:
            self._notes.show_hidden(note_id)

    def _show_all(self) -> None:
        if self._notes:
            self._notes.show_all_hidden()

    def _restore(self) -> None:
        if self._notes:
            self._notes.restore_last_deleted()

    def _update_news(self, _news: object) -> None:
        self.refresh_new_version()

    def refresh_new_version(self) -> None:
        release = self._updates.release if self._updates is not None else None
        self.new_version_action.setVisible(release is not None)
        if release is not None:
            self.new_version_action.setText(
                self.tr("&Get Stickle %1…").replace("%1", release.version)
            )

    def _get_new_version(self) -> None:
        if self._updates is not None and self._updates.release is not None:
            note_window.open_url(QUrl(self._updates.release.page))

    def _refresh_clipboard(self) -> None:
        self.clipboard_action.setEnabled(bool(clipboard_text()))

    def _fill_views(self) -> None:
        self.view_menu.clear()
        if self._notes is None:
            return
        notes = self._notes
        group = QActionGroup(self.view_menu)
        for view, name in notes.view_choices():
            action = self.view_menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(view == notes.view)
            action.triggered.connect(lambda _=False, view=view: notes.set_view(view))
            group.addAction(action)

    def _raise_all(self) -> None:
        if self._notes:
            self._notes.raise_all()

    def retranslate(self) -> None:
        self.open_window_action.setText(self.tr("&Open Stickle Manager"))
        self.new_note_action.setText(self.tr("&New note"))
        self.hidden_menu.setTitle(self.tr("&Hidden notes"))
        self.raise_action.setText(self.tr("&Bring all notes to front"))
        self.clipboard_action.setText(self.tr("New note from &clipboard"))
        if self._notes is not None and self._notes.set_aside:
            self.set_aside_action.setText(self.tr("Sho&w the notes again"))
        else:
            self.set_aside_action.setText(self.tr("Hide all notes for no&w"))
        self.view_menu.setTitle(self.tr("Show on the &desktop"))
        self.guide_action.setText(self.tr("&Keyboard shortcuts"))
        self.find_action.setText(self.tr("&Find on the desktop"))
        self.data_menu.setTitle(self.tr("Da&ta"))
        self.import_action.setText(self.tr("&Bring notes in from another app…"))
        self.export_action.setText(self.tr("&Export all notes…"))
        self.restore_backup_action.setText(self.tr("&Restore from a backup…"))
        set_aside = self._notes is not None and self._notes.set_aside
        # Faded while the notes are out of sight, so the icon says why none show;
        # set only on a change, as some desktops redraw the whole tray for it.
        if set_aside != self._faded:
            self._faded = set_aside
            self.setIcon(make_icon(faded=set_aside))
        if set_aside:
            self.setToolTip(self.tr("Stickle: notes hidden for now"))
        elif self._notes is not None and self._notes.view is not None:
            # Always in sight, so that notes out of view are not thought lost.
            self.setToolTip(
                self.tr("Stickle: only “%1” on the desktop").replace("%1", self._notes.view_name())
            )
        else:
            self.setToolTip("Stickle")
        self.refresh_notes()
        self.settings_action.setText(self.tr("&Settings…"))
        self.quit_action.setText(self.tr("&Quit Stickle"))
        self.feedback_action.setText(self.tr("Report a problem or &idea…"))
        self.support_action.setText(self.tr("Su&pport Stickle…"))
        self.about_action.setText(self.tr("Abo&ut Stickle"))
        self.refresh_new_version()
