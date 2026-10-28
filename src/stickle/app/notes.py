"""The open note windows, and keeping them in step with the stored notes.

A new note is stored once it has text (more than spaces and line breaks): an
untouched note that is closed leaves nothing behind. Text is saved a second
after typing stops, at least every five seconds while typing goes on, when it
loses focus, when the note is hidden or deleted, and before the app quits, the session ends or the
computer sleeps. A character still being composed by an input method is
saved once it is committed; hiding, quitting and sleeping ask the input
method to commit it first. If saving fails, the text stays in the window, a
mark in its title bar says so, and saving is tried again. Hiding keeps the note for later;
deleting puts it into the trash, from where it can be restored. A note whose
text was all erased goes for good instead, when hidden or deleted: there is
nothing in it to restore, and neither the hidden list nor the trash fills up
with empty notes. Setting every note aside only takes the windows out of sight
for a while: nothing stored changes.

Where each note is, and its size, is kept once moving or resizing stops, and
before it is hidden or the app quits (see stickle.core.layout); a stored note
opens where it was, and notes are put back when monitors come or go.

A secret note's text is stored encrypted only (stickle.crypto.secret_notes).
It opens with the secret notes' password, one note at a time: the key is held
by the window it opened, and dropped (its bytes overwritten) when the note
locks again, by itself (stickle.app.secret_notes) or when asked. Its text is
saved before it locks; if that fails it stays open, as closing it would lose
the text. A locked one shows no text, and is never taken for an empty note.

Without a repository (measurement mode, some tests) the windows are simply
not stored.
"""

import logging
import random
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, cast, override

import apsw
from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QMimeData,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    QTimer,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QScreen
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

from stickle.app.category_dialog import CreateCategory, NewCategoryDialog, next_dot_color
from stickle.app.color_dialog import ColorDialog
from stickle.app.labels import mark_name
from stickle.app.note_window import NoteWindow
from stickle.app.placement import MonitorWatch, can_place_windows, monitors, qrect, rect
from stickle.app.secret_dialogs import (
    OpenSecretDialog,
    SecretPasswords,
    SecretTitleDialog,
    TurnOnDialog,
)
from stickle.app.secret_notes import Relock
from stickle.core.colors import PALETTE
from stickle.core.labels import DEFAULT_ICON, Category, Mark
from stickle.core.layout import MAIN, Place, Rect, fit, monitor_at, remember, restore
from stickle.core.note import DEFAULT_COLOR, Note
from stickle.core.paste import pasted_text
from stickle.core.snap import resized, snapped, together
from stickle.crypto.keyfile import KeyFileError, WrongPasswordError
from stickle.crypto.secret_notes import SecretNoteError, open_text, seal_text
from stickle.data.colors import MAX_OWN_COLORS, ColorRepository, OwnColor, OwnColorsFullError
from stickle.data.labels import LabelRepository
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteIsSecretError, NoteNotSecretError, NoteRepository
from stickle.data.settings import (
    DEFAULT_NOTE_COLOR,
    LAST_USED_COLOR,
    LOCK_ON_LEAVE,
    MAGNET,
    RANDOM_COLOR,
    RELOCK_MINUTES,
    Settings,
)
from stickle.platform.linux.x11 import activate

if TYPE_CHECKING:
    from stickle.app.reminders import ReminderKeeper

# New notes cascade from the top-left of the screen so they never land exactly on top of each other.
CASCADE_ORIGIN = 80
CASCADE_STEP = 32
CASCADE_LENGTH = 10
HIDDEN_LISTED = 15
FIT_AT_MOST = 0.6  # of the screen's height, for a stored note shown the first time
IDLE_MS = 1000  # save this long after typing stops
MAX_MS = 5000  # and at least this often while typing goes on
RETRY_FIRST_MS = 1000
RETRY_MAX_MS = 30_000

log = logging.getLogger(__name__)

# The view of the notes with no category (not an id: no category is stored with it).
NO_CATEGORY = "*none*"
# The view of the secret notes, which no category's view shows.
SECRET_NOTES = "*secret*"
# A note lined up where it was dropped slides there this quickly.
SNAP_SLIDE_MS = 100


def clipboard_text() -> str:
    """The clipboard's text, as a note can hold it; empty if it holds none.

    Formatted text comes as Markdown, as when pasted into a note (see
    stickle.core.paste), and NUL, which a note cannot hold, is left out.
    """
    # None when nothing was ever copied, whatever the type hints say.
    copied = cast(QMimeData | None, QGuiApplication.clipboard().mimeData())
    if copied is None:
        return ""
    text = pasted_text(
        copied.html() if copied.hasHtml() else None, copied.text() if copied.hasText() else ""
    )
    return text if text.strip() else ""


def _single_shot(parent: QObject, interval: int, action: Callable[[], object]) -> QTimer:
    timer = QTimer(parent)
    timer.setSingleShot(True)
    timer.setInterval(interval)
    timer.timeout.connect(action)
    return timer


class AutoSave(QObject):
    """When to save one note. No timer runs while nothing is waiting to be saved."""

    def __init__(
        self, save: Callable[[], bool], idle_ms: int, max_ms: int, parent: QObject
    ) -> None:
        super().__init__(parent)
        self._save = save
        self._idle = _single_shot(self, idle_ms, self.save_now)
        self._max = _single_shot(self, max_ms, self.save_now)
        self._retry = _single_shot(self, RETRY_FIRST_MS, self.save_now)
        self._backoff = RETRY_FIRST_MS

    def changed(self) -> None:
        if not self._max.isActive():
            self._max.start()
        self._idle.start()

    def save_now(self) -> bool:
        self.stop()
        if self._save():
            self._backoff = RETRY_FIRST_MS
            return True
        self._retry.start(self._backoff)
        self._backoff = min(self._backoff * 2, RETRY_MAX_MS)
        return False

    def stop(self) -> None:
        for timer in (self._idle, self._max, self._retry):
            timer.stop()

    @property
    def waiting(self) -> bool:
        return any(timer.isActive() for timer in (self._idle, self._max, self._retry))


def _ask_for_category(parent: QWidget, create: CreateCategory, color: str) -> NewCategoryDialog:
    dialog = NewCategoryDialog(parent, create, color)
    dialog.exec()
    return dialog


# Asks, and returns once answered: the dialog's created is the new category, if any.
new_category_dialog: Callable[[QWidget, CreateCategory, str], NewCategoryDialog] = (
    _ask_for_category  # replaced in tests
)


def _ask(dialog: QDialog) -> bool:
    return dialog.exec() == QDialog.DialogCode.Accepted


# Shows a secret note's dialog (its password, its title) and waits: whether accepted.
ask_secret: Callable[[QDialog], bool] = _ask  # replaced in tests


def _confirm_ordinary(parent: QWidget) -> bool:
    answer = QMessageBox.question(
        parent,
        "Stickle",
        QCoreApplication.translate(
            "NoteManager",
            "Make it an ordinary note? It will no longer lock, and its text can be"
            " found by searching again.",
        ),
    )
    return answer == QMessageBox.StandardButton.Yes


confirm_ordinary: Callable[[QWidget], bool] = _confirm_ordinary  # replaced in tests

# Shows the colour window and waits: whether a colour was chosen.
ask_color: Callable[[ColorDialog], bool] = _ask  # replaced in tests


def _screen_of(window: QWidget) -> QScreen:
    """The screen a window is on (where its middle is), shown or not."""
    return QGuiApplication.screenAt(window.geometry().center()) or window.screen()


def _wipe(key: bytearray) -> None:
    """Overwrite a key no longer needed, so its bytes do not linger in memory."""
    key[:] = bytes(len(key))


class NoteManager(QObject):
    # Qt's own "quit on last window closed" ignores tool windows, which notes are.
    last_note_closed = Signal()
    # Hidden or deleted notes changed: menus listing them should refresh.
    changed = Signal()
    # A new note was stored for the first time.
    note_created = Signal()
    # A note's text was stored: lists showing titles may want to refresh.
    note_saved = Signal()
    # Every note was put out of sight for a while, or brought back.
    set_aside_changed = Signal()
    # The desktop shows another category's notes, or every note again.
    view_changed = Signal()
    # Notes went out of sight for a full-screen program, or came back.
    covered_changed = Signal()
    # Lining up like a magnet was switched on or off.
    magnet_changed = Signal()
    # The user's own colours were added, removed or put in another order.
    own_colors_changed = Signal()
    # A note asked for the keys at a glance (F1, Ctrl+/, its menu).
    guide_requested = Signal()
    # A note's window opened, about to show (for what dresses it: reminders).
    window_opened = Signal(object)

    def __init__(
        self,
        repository: NoteRepository | None = None,
        idle_ms: int = IDLE_MS,
        max_ms: int = MAX_MS,
        settings: Settings | None = None,
        layouts: LayoutRepository | None = None,
        labels: LabelRepository | None = None,
        colors: ColorRepository | None = None,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._settings = settings
        self._layouts = layouts
        self._labels = labels
        self._colors = colors
        self._random = random.Random()
        self._monitor_watch = MonitorWatch(self)
        self._monitor_watch.changed.connect(self.place_all)
        self._idle_ms = idle_ms
        self._max_ms = max_ms
        self._windows: list[NoteWindow] = []
        self._autosaves: dict[NoteWindow, AutoSave] = {}
        self._set_aside: list[NoteWindow] = []
        self._view: str | None = None  # see view
        self._out_of_view: list[NoteWindow] = []
        # Out of sight on a screen a full-screen program covers (see cover).
        self._covered: list[NoteWindow] = []
        self._covered_screens: set[QScreen] = set()
        # Where a note with no place of its own was before the app moved it
        # onto a smaller screen: back there once it fits again (see place_all).
        self._before_fit: dict[NoteWindow, Rect] = {}
        self._find_among: list[NoteWindow] | None = None  # while finding on the desktop
        # Moving with Shift: the notes that come along, and where each started.
        self._groups: dict[NoteWindow, list[tuple[NoteWindow, QPoint]]] = {}
        self._created = 0
        self._quitting = False
        # Secret notes: none without (measurement mode, tests). The key of each
        # open one, and its text as last stored (saved again only once changed).
        self.secrets: SecretPasswords | None = None
        self._keys: dict[NoteWindow, bytearray] = {}
        self._stored_secret: dict[NoteWindow, str] = {}
        self._relock = Relock(lambda: self.relock_minutes, lambda: self.lock_on_leave, self)
        self._relock.lock_due.connect(self.lock_secret)
        self._relock.all_due.connect(self.lock_secret_notes)
        # Notes' reminders: none without (measurement mode, tests).
        self.reminders: ReminderKeeper | None = None

    @property
    def windows(self) -> tuple[NoteWindow, ...]:
        return tuple(self._windows)

    def watch_quit(self, app: QObject) -> None:
        """Save everything when the app is asked to quit, before Qt closes the windows.

        Qt closes every window on quit; without this, each note would take that
        as a request to hide, and none would come back on the next start.
        """
        app.installEventFilter(self)

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Quit:
            self.prepare_to_quit()
        return super().eventFilter(watched, event)

    def prepare_to_quit(self) -> None:
        self._quitting = True
        for window in self._windows:
            self.flush(window, closing=True)
        for window in self._windows:
            window.allow_close()

    # Opening

    def open_stored(self) -> None:
        """Open every note that is neither hidden nor deleted.

        With no notes at all, an empty note is opened to start with. Hidden notes
        alone open nothing: the app shows the Stickle window, which lists them.
        None of the notes asks for the keyboard (see give_keyboard): under X11
        a request coming after another window's is refused, and GNOME then
        tells that the note "is ready".
        """
        if self._repository is None:
            self.new_note()
            return
        visible = self._repository.visible()
        for note in visible:
            self._open(note, take_keyboard=False)
        if not visible and not self._repository.all():
            self.new_note()
        log.info("opened %d notes", len(visible))

    def give_keyboard(self) -> None:
        """The keyboard for the note opened last, the one in front."""
        if self._windows:
            window = self._windows[-1]
            window.activateWindow()
            window.setFocus()

    def open_brought_in(self, note_ids: list[str]) -> None:
        """Show the notes just brought in from another app; hidden ones stay hidden."""
        if self._repository is None:
            return
        for note_id in note_ids:
            note = self._repository.get(note_id)
            if note is not None and not note.hidden:
                self._open(note)
        self.refresh_labels()  # categories may have been made for them; says changed

    def new_note(self) -> NoteWindow:
        """A new note in the colour last chosen (from the tray or the Stickle window)."""
        return self._open(None)

    def quick_note(self) -> NoteWindow:
        """A note to type in at once (the new note shortcut), in front with the keyboard.

        A new note still untouched is used again rather than another one made, so
        pressing the shortcut again does not pile up empty notes.
        """
        window = (
            next(
                (w for w in reversed(self._windows) if w.note_id is None and not w.text),
                None,
            )
            or self.new_note()
        )
        self._take_back(window)
        window.edit()
        window.bring_to_front()
        return window

    def note_from_clipboard(self) -> NoteWindow | None:
        """A new note holding the clipboard's text, stored at once; None if there is none."""
        text = clipboard_text()
        if not text:
            return None
        window = self._open(None, text)
        self.save(window)
        window.bring_to_front()
        return window

    def new_note_from(self, window: NoteWindow) -> NoteWindow:
        """A new note asked for from a note (Ctrl+N): in that note's colour."""
        return self._open(None, color=window.color)

    def open_unstored(self, text: str) -> NoteWindow:
        """A note with text that is not stored until it changes (measurement mode)."""
        return self._open(None, text)

    def _open(
        self,
        note: Note | None,
        text: str = "",
        color: str | None = None,
        take_keyboard: bool = True,
        in_sight: bool = True,
    ) -> NoteWindow:
        if note is not None:
            window = NoteWindow(
                note.id,
                note.body,
                note.color,
                note.always_on_top,
                opacity=note.opacity,
                locked=note.locked,
                secret_title=(note.secret_title or "") if note.secret else None,
                can_be_secret=self.secrets is not None,
            )
        else:
            window = NoteWindow(
                None,
                text,
                color or self._new_note_color(),
                can_be_secret=self.secrets is not None,
            )
        window.new_note_requested.connect(lambda: self.new_note_from(window))
        window.secret_requested.connect(lambda: self.make_secret(window))
        window.open_secret_requested.connect(lambda: self.open_secret(window))
        window.lock_secret_requested.connect(lambda: self.lock_secret(window))
        window.secret_title_requested.connect(lambda: self.change_secret_title(window))
        window.ordinary_requested.connect(lambda: self.make_ordinary(window))

        def color_requested(color: str) -> None:
            self.set_color(window, color)

        window.color_requested.connect(color_requested)

        def collapse_requested(collapsed: bool) -> None:
            self.set_collapsed(window, collapsed)

        window.collapse_requested.connect(collapse_requested)

        def on_top_requested(on_top: bool) -> None:
            self.set_always_on_top(window, on_top)

        window.on_top_requested.connect(on_top_requested)

        def opacity_requested(opacity: float) -> None:
            self.set_opacity(window, opacity)

        window.opacity_requested.connect(opacity_requested)

        def lock_requested(locked: bool) -> None:
            self.set_locked(window, locked)

        window.lock_requested.connect(lock_requested)

        window.label_choices = self.label_choices
        window.own_colors = self.own_colors
        window.add_color_requested.connect(lambda: self.add_color(window))

        def color_step_requested(step: int) -> None:
            self.step_color(window, step)

        window.color_step_requested.connect(color_step_requested)

        def category_requested(category_id: str | None) -> None:
            self.set_category(window, category_id)

        window.category_requested.connect(category_requested)
        window.new_category_requested.connect(lambda: self.new_category(window))

        def mark_requested(mark_id: str, on: bool) -> None:
            self.set_mark(window, mark_id, on)

        window.mark_requested.connect(mark_requested)
        window.guide_requested.connect(self.guide_requested)
        if note is not None:
            self._show_labels(window, note.label, note.marks)
        elif self._view not in (None, NO_CATEGORY, SECRET_NOTES):
            # Made while one category is in view: of it, so that it stays in sight.
            self._show_labels(window, self._view, ())

        def switch_requested(step: int) -> None:
            self.switch_note(window, step)

        window.switch_requested.connect(switch_requested)
        window.hide_requested.connect(lambda: self.hide(window))
        window.delete_requested.connect(lambda: self.delete(window))
        autosave = AutoSave(lambda: self.save(window), self._idle_ms, self._max_ms, window)
        self._autosaves[window] = autosave
        window.text_changed.connect(autosave.changed)
        window.editing_finished.connect(autosave.save_now)
        window.retry_requested.connect(autosave.save_now)
        window.closed.connect(lambda: self._forget(window))
        window.geometry_settled.connect(lambda: self._settled(window))

        def drag_started(group: bool) -> None:
            self._drag_started(window, group)

        window.drag_started.connect(drag_started)

        def dragged_by(delta: QPoint) -> None:
            self._drag_group(window, delta)

        window.dragged_by.connect(dragged_by)
        self._windows.append(window)

        if not self._restore_place(window):
            if note is not None and not self._places(window):
                # Stored but never placed (the note of the first start): shown whole.
                screen = QGuiApplication.primaryScreen()
                window.fit_to_text(int(screen.availableGeometry().height() * FIT_AT_MOST))
            window.place(QRect(self._free_cascade_spot(window), window.size()))
        if note is not None and note.collapsed:
            window.set_collapsed(True)
            window.mark_placed()

        self.window_opened.emit(window)
        if not in_sight:
            return window
        window.show()
        if take_keyboard:
            window.activateWindow()
            window.setFocus()
        return window

    def _forget(self, window: NoteWindow) -> None:
        self._forget_key(window)
        self._before_fit.pop(window, None)
        self._windows.remove(window)
        self._autosaves.pop(window).stop()
        self._take_back(window)
        if not self._windows and not self._quitting:
            self.last_note_closed.emit()

    # Saving

    def save(self, window: NoteWindow) -> bool:
        """Store the window's text; False (and the window says so) if that failed."""
        if self._repository is None:
            return True
        if window.secret:
            return self._save_secret(window)
        text = window.text
        try:
            if window.note_id is None:
                if text.strip():
                    self._store_new(window)
            else:
                self._repository.update_body(window.note_id, text)
        except (apsw.Error, OSError, NoteIsSecretError) as error:
            # Secret where the window took it for ordinary: marked, never raised
            # (raised, it stopped the other notes being saved on quitting).
            log.error("could not save a note: %s", type(error).__name__)
            window.set_unsaved(True)
            return False
        window.set_unsaved(False)
        if window.note_id is not None:
            if self.reminders is not None:
                self.reminders.text_saved(window.note_id, text)
            self.note_saved.emit()
        return True

    def _store_new(self, window: NoteWindow) -> str:
        """Store a note for the first time, as it is now; its id."""
        assert self._repository is not None
        note_id = self._repository.create(window.text, window.color).id
        window.note_id = note_id
        self.note_created.emit()
        self.save_layout(window, force=True)
        if window.collapsed:
            self._repository.set_collapsed(note_id, True)
        if not window.always_on_top:
            self._repository.set_always_on_top(note_id, False)
        if window.opacity != 1.0:
            self._repository.set_opacity(note_id, window.opacity)
        if window.locked:
            self._repository.set_locked(note_id, True)
        if window.category is not None:
            self._repository.set_category(note_id, window.category.id)
        for mark in window.marks:
            self._repository.set_mark(note_id, mark.id, True)
        return note_id

    def _save_secret(self, window: NoteWindow) -> bool:
        """An open secret note's text, encrypted; a locked one holds none to save."""
        assert self._repository is not None
        key = self._keys.get(window)
        if window.secret_locked or key is None or window.note_id is None:
            return True
        text = window.text
        if text != self._stored_secret.get(window):
            try:
                sealed = seal_text(bytes(key), window.note_id, text)
                self._repository.update_secret_body(window.note_id, sealed)
            except (apsw.Error, OSError, NoteNotSecretError) as error:
                log.error("could not save a secret note: %s", type(error).__name__)
                window.set_unsaved(True)
                return False
            self._stored_secret[window] = text
            if self.reminders is not None:
                # Only the times are kept, for it to ring while covered.
                self.reminders.text_saved(window.note_id, text)
            self.note_saved.emit()
        window.set_unsaved(False)
        return True

    def flush(self, window: NoteWindow, closing: bool = False) -> bool:
        """Save now, with the character being composed (see finish_composition),
        and where the note is if it was just moved."""
        window.finish_composition(closing)
        self.save_layout(window)
        return self._autosaves[window].save_now()

    # Where notes are

    def _places(self, window: NoteWindow) -> dict[str, Place]:
        if self._layouts is None or window.note_id is None:
            return {}
        return self._layouts.places(window.note_id)

    def _restore_place(self, window: NoteWindow) -> bool:
        """Put a stored note where it was; False if nothing is remembered."""
        places = self._places(window)
        if not can_place_windows():
            # Only the size can be kept: the window system decides where windows go.
            if MAIN in places:
                window.resize(places[MAIN].width, places[MAIN].height)
            return False
        placement = restore(places, monitors())
        if placement is None:
            return False
        window.place(qrect(placement.window), placement.own_monitor)
        return True

    def save_layout(self, window: NoteWindow, force: bool = False) -> None:
        """Remember where the note is, if the user moved or resized it (or force)."""
        if window.moved_by_user:
            self._before_fit.pop(window, None)  # where the user put it, from now on
        if self._layouts is None or window.note_id is None:
            return
        if not force and not window.moved_by_user:
            return
        # The system moves notes off a monitor that went away; that is not the
        # user's doing, and taken for it, it replaced the note's own place there.
        if not force and self._monitor_watch.changing:
            return
        places = self._places(window)
        if can_place_windows():
            window_rect = rect(window.expanded_geometry())  # folded or not, the full size
            places = remember(places, window_rect, monitors(), window.own_monitor)
        elif MAIN in places:
            size = {"width": window.width(), "height": window.height()}
            places = {slot: replace(place, **size) for slot, place in places.items()}
        else:
            return
        try:
            self._layouts.save(window.note_id, places)
        except apsw.Error as error:
            log.error("could not store where a note is: %s", type(error).__name__)
            return
        window.mark_placed()

    # Lining up where a note is dropped, and notes side by side moving together

    def _drag_started(self, window: NoteWindow, group: bool) -> None:
        """Dragged by its title bar; with Shift, the notes touching it (and those
        touching them) come along. Locked notes stay where they are."""
        self._groups.pop(window, None)
        if not group:
            return
        free = {
            other: rect(other.geometry())
            for other in self._windows
            if other.isVisible() and (other is window or not other.locked)
        }
        self._groups[window] = [
            (other, other.pos()) for other in together(window, free) if other is not window
        ]

    def _drag_group(self, window: NoteWindow, distance: QPoint) -> None:
        for other, start in self._groups.get(window, []):
            other.move(start + distance)

    def _settled(self, window: NoteWindow) -> None:
        """Moved or resized, then left alone: remembered. Just dropped after a drag,
        it is lined up first with the notes and the edge of the screen near it,
        unless Alt was held (or the window system places windows itself)."""
        if window.resizing:
            window.resizing = False
            self._line_up_size(window)
            return
        if not window.dragging:
            self.save_layout(window)
            return
        window.dragging = False
        moving = [window, *(other for other, _start in self._groups.pop(window, []))]
        delta = QPoint()
        if self.magnet and not window.alt_at_drop and can_place_windows():
            others = [
                rect(other.geometry())
                for other in self._windows
                if other.isVisible() and other not in moving
            ]
            areas = [monitor.available for monitor in monitors()]
            now = rect(window.geometry())
            target = snapped(now, others, areas)
            delta = QPoint(target.x - now.x, target.y - now.y)
        if delta.isNull():
            for moved in moving:
                self.save_layout(moved)
            return
        for moved in moving:
            slide = QPropertyAnimation(moved, b"pos", self)
            slide.setDuration(SNAP_SLIDE_MS)
            slide.setEndValue(moved.pos() + delta)
            slide.finished.connect(lambda moved=moved: self.save_layout(moved))
            slide.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def _line_up_size(self, window: NoteWindow) -> None:
        """Just let go after resizing by its corner: its right and bottom edges go
        to the edges near them (a folded note keeps its height), unless Alt was held."""
        target = now = rect(window.geometry())
        if self.magnet and not window.alt_at_drop and can_place_windows():
            others = [
                rect(other.geometry())
                for other in self._windows
                if other.isVisible() and other is not window
            ]
            areas = [monitor.available for monitor in monitors()]
            target = resized(now, others, areas, height_too=not window.collapsed)
        if (target.width, target.height) == (now.width, now.height):
            self.save_layout(window)
            return
        grow = QPropertyAnimation(window, b"size", self)
        grow.setDuration(SNAP_SLIDE_MS)
        grow.setEndValue(QSize(target.width, target.height))
        grow.finished.connect(lambda: self.save_layout(window))
        grow.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    # Lining up like a magnet, on or off (one setting for moving and resizing)

    @property
    def magnet(self) -> bool:
        return self._settings.get(MAGNET) if self._settings is not None else True

    def set_magnet(self, on: bool) -> None:
        if self._settings is None:
            return
        try:
            self._settings.set(MAGNET, on)
        except apsw.Error as error:
            log.error("could not keep the magnet setting: %s", type(error).__name__)
            return
        self.magnet_changed.emit()

    def place_all(self) -> None:
        """Monitors were connected, removed or changed: put every note where it belongs."""
        if not can_place_windows():
            return
        current = monitors()
        for window in self._windows:
            if self._restore_place(window):
                continue
            # Not remembered yet: at least keep it on a monitor, and back where
            # it was once the screen is large again (a game that changed the
            # screen's resolution for a while, a projector).
            where = rect(window.expanded_geometry())
            before = self._before_fit.pop(window, None)
            if before is not None:
                _, monitor = monitor_at(before, current)
                if fit(before, monitor.available) == before:
                    window.place(qrect(before), window.own_monitor)
                    continue
            _, monitor = monitor_at(where, current)
            fitted = fit(where, monitor.available)
            if fitted != where or before is not None:
                self._before_fit[window] = before or where
            window.place(qrect(fitted), window.own_monitor)
        log.info("monitors changed: %d notes on %d monitors", len(self._windows), len(current))

    def save_all(self) -> None:
        """Save every note that stays open (logout, sleep)."""
        for window in self._windows:
            self.flush(window)

    # Staying on top

    def set_always_on_top(self, window: NoteWindow, on_top: bool) -> None:
        """Keep a note above other windows or not. If that cannot be stored,
        the note stays as it was (the pin shows so)."""
        if self._repository is not None and window.note_id is not None:
            try:
                self._repository.set_always_on_top(window.note_id, on_top)
            except apsw.Error as error:
                log.error("could not store a note staying on top: %s", type(error).__name__)
                window.set_always_on_top(window.always_on_top)
                return
        window.set_always_on_top(on_top)
        window.mark_placed()  # the window system may have nudged it: not the user's move

    def raise_all(self) -> None:
        """Bring every open note in front of other windows (notes not on top get covered)."""
        self.bring_back()
        if self._covered:  # asked for: over the full-screen program too
            self._covered.clear()
            self.covered_changed.emit()
        for window in self._windows:
            window.show()
            window.raise_()
        if self._windows:
            self._windows[-1].activateWindow()

    # Every note out of sight for a while (a screen shared, a presentation)

    @property
    def set_aside(self) -> bool:
        return bool(self._set_aside)

    def set_all_aside(self) -> None:
        """Take every note on screen out of sight, saved first. Nothing stored
        changes: they are not hidden notes, and the next start shows them.
        Those out of sight for a full-screen program go too: they stay aside
        once it ends."""
        shown = [window for window in self._windows if window.isVisible()]
        covered, self._covered = self._covered, []
        if not shown and not covered:
            return
        for window in shown:
            # A note that could not be saved keeps its text in the window, which
            # goes on trying; out of sight is not closed.
            self.flush(window, closing=True)
            window.hide()
        self._set_aside.extend(shown + covered)
        log.info("notes set aside: %d", len(shown) + len(covered))
        if covered:
            self.covered_changed.emit()
        self.set_aside_changed.emit()

    def bring_back(self) -> None:
        """Show again the notes set aside, where they were."""
        windows, self._set_aside = self._set_aside, []
        if not windows:
            return
        for window in windows:
            window.show()
        log.info("notes brought back: %d", len(windows))
        self.set_aside_changed.emit()

    def _take_back(self, window: NoteWindow) -> None:
        """One note is no longer set aside, out of view or covered (shown on its
        own, or closed)."""
        if window in self._out_of_view:
            self._out_of_view.remove(window)
        if window in self._covered:
            self._covered.remove(window)
            self.covered_changed.emit()
        if window in self._set_aside:
            self._set_aside.remove(window)
            if not self._set_aside:
                self.set_aside_changed.emit()

    # Out of the way of full-screen programs

    @property
    def covered(self) -> int:
        """How many notes are out of sight for a full-screen program."""
        return len(self._covered)

    def cover(self, screens: set[QScreen]) -> None:
        """The screens full-screen programs cover now: the notes shown on one
        newly covered go out of sight, saved first, and those of a screen no
        longer covered come back, unless set aside or out of view meanwhile.

        Nothing is stored, as when set aside. A note the user brings up while
        a screen is covered (a new one, one from the list) stays in sight.
        """
        newly = screens - self._covered_screens
        self._covered_screens = set(screens)
        back = [w for w in self._covered if _screen_of(w) not in screens]
        for window in back:
            self._covered.remove(window)
            self._back_in_sight(window)
        gone = [w for w in self._windows if w.isVisible() and _screen_of(w) in newly]
        for window in gone:
            self.flush(window, closing=True)
            window.hide()
        self._covered.extend(gone)
        if back or gone:
            log.info("notes covered by full screen: %d", len(self._covered))
            self.covered_changed.emit()

    def _back_in_sight(self, window: NoteWindow) -> None:
        """A note out of sight for a while comes back, unless it is meant to
        stay out of sight with the others (set aside, out of view)."""
        if not self._in_view(window):
            self._out_of_view.append(window)
        elif self._set_aside:
            self._set_aside.append(window)
        else:
            window.show()

    # On the desktop, the notes of one category only

    @property
    def view(self) -> str | None:
        """The category whose notes alone are on the desktop, NO_CATEGORY for the
        notes with none, SECRET_NOTES for the secret notes, or None for every note."""
        return self._view

    def _in_view(self, window: NoteWindow) -> bool:
        if self._view is None:
            return True
        if self._view == SECRET_NOTES or window.secret:
            # Kept apart: not in the view of the category they keep for later.
            return self._view == SECRET_NOTES and window.secret
        category = window.category.id if window.category is not None else None
        return category is None if self._view == NO_CATEGORY else category == self._view

    def set_view(self, view: str | None) -> None:
        """Show on the desktop only the notes of a category (see view).

        Nothing is stored: a note out of view is taken out of sight as when set
        aside, saved first, and the next start shows every note. Notes set aside
        stay so, and come back in this view. A note given another category while
        in view stays until the view changes, rather than vanish under the hand.
        """
        # The window the view was chosen in (the Stickle window, as a rule).
        chosen_in = QApplication.activeWindow()
        known = {c.id for c in self.label_choices()[0]}
        if self.has_secret_notes():
            known.add(SECRET_NOTES)
        if view is not None and view != NO_CATEGORY and view not in known:
            view = None  # deleted meanwhile
        back, self._out_of_view = self._out_of_view, []
        self._view = view
        for window in back:
            if self._set_aside:
                self._set_aside.append(window)  # back when the rest come back
            else:
                window.show()
        for window in self._windows:
            if self._in_view(window):
                continue
            if window in self._set_aside:
                self._set_aside.remove(window)
            elif window.isVisible():
                self.flush(window, closing=True)
                window.hide()
            else:
                continue
            self._out_of_view.append(window)
        log.info("view: %s, %d notes out of view",
                 "all" if view is None else "one category", len(self._out_of_view))  # fmt: skip
        self.view_changed.emit()
        self.set_aside_changed.emit()
        if chosen_in is not None and chosen_in.isVisible():
            # A window manager gives the keyboard to a note left in sight when
            # others go: back to where the user was choosing.
            chosen_in.activateWindow()
            if QGuiApplication.platformName() == "xcb":
                activate(int(chosen_in.winId()))

    # Finding notes on the desktop by their text

    @property
    def finding(self) -> bool:
        return self._find_among is not None

    def begin_find(self) -> None:
        """Start finding among the notes on the desktop now (in the view, if one),
        saved first so that what was just typed is found too."""
        if self._find_among is not None:
            return
        self._find_among = [w for w in self._windows if w.isVisible()]
        for window in self._find_among:
            self.flush(window)

    def find(self, term: str) -> list[NoteWindow]:
        """Leave in sight only the notes found for term (all of them for none), as
        aside: nothing stored. The notes found, most recently changed first."""
        among = self._find_among or []
        term = term.strip()
        found_ids = self.matching(term) if term else None
        order = {note.id: i for i, note in enumerate(self.listed_notes())}
        found: list[NoteWindow] = []
        for window in among:
            hit = found_ids is None or window.note_id in found_ids
            window.set_found(False)
            if hit:
                found.append(window)
                window.show()
            else:
                window.hide()
        found.sort(key=lambda w: order.get(w.note_id or "", -1))
        return found

    def found_elsewhere(self, term: str) -> tuple[int, int]:
        """How many hidden notes and notes in the trash term is also found in."""
        term = term.strip()
        if not term:
            return 0, 0
        ids = self.matching(term)
        hidden = sum(note.id in ids for note in self.hidden_notes())
        trashed = sum(note.id in ids for note in self.trash_notes())
        return hidden, trashed

    def end_find(self, chosen: NoteWindow | None = None) -> None:
        """Every note found among back in sight, where it was; chosen to the front
        with the keyboard."""
        among, self._find_among = self._find_among or [], None
        for window in among:
            window.set_found(False)
            if window in self._windows:
                window.show()
        if chosen is not None and chosen in self._windows:
            chosen.bring_to_front()

    def view_choices(self) -> list[tuple[str | None, str]]:
        """(view, name) for each view there is: every note, the notes with no
        category, each category in its order, then the secret notes if any."""
        return [
            (None, self.tr("All notes")),
            (NO_CATEGORY, self.tr("Notes with no category")),
            *((c.id, c.name) for c in self.label_choices()[0]),
            *([(SECRET_NOTES, self.tr("Secret notes"))] if self.has_secret_notes() else []),
        ]

    def view_name(self) -> str:
        """The name of the view on the desktop."""
        return dict(self.view_choices()).get(self._view, "")

    def next_view(self) -> None:
        """Every note, then each category in its order, the secret notes if any,
        then every note again."""
        order: list[str | None] = [None, *(c.id for c in self.label_choices()[0])]
        if self.has_secret_notes():
            order.append(SECRET_NOTES)
        current = order.index(self._view) if self._view in order else 0
        self.set_view(order[(current + 1) % len(order)])

    def switch_set_aside(self) -> None:
        """The shortcut's way: set every note aside, or bring them back if they are."""
        if self._set_aside:
            self.bring_back()
        else:
            self.set_all_aside()

    # Folding

    def set_collapsed(self, window: NoteWindow, collapsed: bool) -> None:
        """Fold a note to its title bar, or unfold it (kept on screen)."""
        if collapsed:
            # Like hiding: the text goes out of sight, and some input methods
            # drop a character still being composed.
            self.flush(window, closing=True)
        window.set_collapsed(collapsed)
        if not collapsed and can_place_windows():
            where = rect(window.geometry())
            _, monitor = monitor_at(where, monitors())
            fitted = fit(where, monitor.available)
            if fitted != where:
                window.place(qrect(fitted), window.own_monitor)
        window.mark_placed()  # folding is not moving: the remembered place stays
        if self._repository is None or window.note_id is None:
            return
        try:
            self._repository.set_collapsed(window.note_id, collapsed)
        except apsw.Error as error:
            log.error("could not store a note being folded: %s", type(error).__name__)

    # From note to note with the keyboard (notes are not in Alt+Tab)

    def switch_note(self, window: NoteWindow, step: int) -> None:
        """Bring forward the next note on screen after window (step 1) or before it
        (-1), in the order they were opened, round from the last to the first."""
        shown = [other for other in self._windows if other.isVisible()]
        if window not in shown or len(shown) < 2:
            return
        shown[(shown.index(window) + step) % len(shown)].bring_to_front()

    # Locked where it is, as it is

    def set_locked(self, window: NoteWindow, locked: bool) -> None:
        """Keep a note from being moved, resized or edited, or allow it again.

        Locking while editing saves first, the character being composed too.
        If storing fails, the note stays as it was.
        """
        if locked:
            self.flush(window, closing=True)
        if self._repository is not None and window.note_id is not None:
            try:
                self._repository.set_locked(window.note_id, locked)
            except apsw.Error as error:
                log.error("could not store a note being locked: %s", type(error).__name__)
                window.set_locked(window.locked)  # the menu shows what is kept
                return
        window.set_locked(locked)
        self.changed.emit()  # the list of notes marks locked ones

    # Category and marks

    def label_choices(self) -> tuple[list[Category], list[Mark]]:
        """The categories and marks a note can have, in their order."""
        if self._labels is None:
            return [], []
        try:
            return self._labels.categories(), self._labels.marks()
        except apsw.Error as error:
            log.error("could not read the categories: %s", type(error).__name__)
            return [], []

    def _show_labels(
        self, window: NoteWindow, category_id: str | None, marks: Iterable[str]
    ) -> None:
        categories, known = self.label_choices()
        category = next((c for c in categories if c.id == category_id), None)
        if category is None and category_id is not None and self._labels is not None:
            category = self._labels.category(category_id)  # deleted since: still the note's
        wanted = set(marks)
        window.set_labels(category, [mark for mark in known if mark.id in wanted])

    def set_category(self, window: NoteWindow, category_id: str | None) -> None:
        """What the note is about. A note not stored yet takes it when first stored;
        if storing fails, the note keeps the one it had."""
        if self._repository is not None and window.note_id is not None:
            try:
                self._repository.set_category(window.note_id, category_id)
            except (apsw.Error, KeyError) as error:
                log.error("could not store a note's category: %s", type(error).__name__)
                return
        self._show_labels(window, category_id, (mark.id for mark in window.marks))
        self.changed.emit()

    def set_mark(self, window: NoteWindow, mark_id: str, on: bool) -> None:
        """Put a mark on the note or take it off; as set_category otherwise."""
        marks = {mark.id for mark in window.marks}
        if self._repository is not None and window.note_id is not None:
            try:
                marks = set(self._repository.set_mark(window.note_id, mark_id, on).marks)
            except (apsw.Error, KeyError) as error:
                log.error("could not store a note's mark: %s", type(error).__name__)
                return
        elif on:
            marks.add(mark_id)
        else:
            marks.discard(mark_id)
        category_id = window.category.id if window.category is not None else None
        self._show_labels(window, category_id, marks)
        self.changed.emit()

    def refresh_labels(self) -> None:
        """Categories changed (renamed, recoloured, reordered, deleted): every open
        note shows its own as stored now."""
        for window in self._windows:
            note = (
                self._repository.get(window.note_id)
                if self._repository is not None and window.note_id is not None
                else None
            )
            if note is not None:
                self._show_labels(window, note.label, note.marks)
            elif window.category is not None or window.marks:
                category_id = window.category.id if window.category is not None else None
                self._show_labels(window, category_id, (mark.id for mark in window.marks))
        self.changed.emit()

    def create_category(self, name: str, color: str, icon: str = DEFAULT_ICON) -> Category:
        """Raises CategoryNameError for a name empty or taken."""
        if self._labels is None:
            raise RuntimeError("no notes database")
        category = self._labels.create_category(name, color, icon)
        self.changed.emit()
        return category

    def rename_category(self, category_id: str, name: str) -> None:
        """Raises CategoryNameError for a name empty or taken."""
        if self._labels is None:
            return
        self._labels.rename_category(category_id, name)
        self.refresh_labels()

    def set_category_color(self, category_id: str, color: str) -> None:
        if self._labels is None:
            return
        try:
            self._labels.set_category_color(category_id, color)
        except apsw.Error as error:
            log.error("could not store a category's colour: %s", type(error).__name__)
            return
        self.refresh_labels()

    def set_category_icon(self, category_id: str, icon: str) -> None:
        if self._labels is None:
            return
        try:
            self._labels.set_category_icon(category_id, icon)
        except apsw.Error as error:
            log.error("could not store a category's icon: %s", type(error).__name__)
            return
        self.refresh_labels()

    def move_category(self, category_id: str, step: int) -> None:
        if self._labels is None:
            return
        try:
            self._labels.move_category(category_id, step)
        except apsw.Error as error:
            log.error("could not store the order of categories: %s", type(error).__name__)
            return
        self.changed.emit()

    def notes_in_category(self, category_id: str) -> list[Note]:
        """The notes not deleted that have the category, shown or hidden."""
        return [note for note in self.listed_notes() if note.label == category_id]

    def remove_category(self, category_id: str, with_notes: bool) -> None:
        """Delete a category: its notes lose it, or (with_notes, as the user chose)
        go into the trash together, open ones saved first."""
        if self._repository is None:
            return
        windows = [
            window
            for note in self.notes_in_category(category_id)
            if (window := self.window_for(note.id)) is not None
        ]
        if with_notes:
            for window in windows:
                self.flush(window, closing=True)
        try:
            removed = self._repository.remove_category(category_id, with_notes)
        except (apsw.Error, KeyError) as error:
            log.error("could not delete a category: %s", type(error).__name__)
            return
        log.info("a category was deleted, %d notes %s", len(removed),
                 "into the trash" if with_notes else "kept")  # fmt: skip
        if with_notes:
            for window in windows:
                self._autosaves[window].stop()
                window.release()
        self.refresh_labels()
        if self._view == category_id:
            self.set_view(None)

    def create_mark(self, name: str, icon: str) -> Mark:
        """Raises MarkNameError for a name empty or taken (as shown, translated)."""
        if self._labels is None:
            raise RuntimeError("no notes database")
        mark = self._labels.create_mark(name, icon, mark_name)
        self.changed.emit()
        return mark

    def rename_mark(self, mark_id: str, name: str) -> None:
        """An empty name gives a built-in mark its own back. Raises MarkNameError."""
        if self._labels is None:
            return
        self._labels.rename_mark(mark_id, name, mark_name)
        self.refresh_labels()

    def set_mark_icon(self, mark_id: str, icon: str) -> None:
        if self._labels is None:
            return
        try:
            self._labels.set_mark_icon(mark_id, icon)
        except apsw.Error as error:
            log.error("could not store a mark's icon: %s", type(error).__name__)
            return
        self.refresh_labels()

    def move_mark(self, mark_id: str, step: int) -> None:
        if self._labels is None:
            return
        try:
            self._labels.move_mark(mark_id, step)
        except apsw.Error as error:
            log.error("could not store the order of marks: %s", type(error).__name__)
            return
        self.refresh_labels()

    def notes_with_mark(self, mark_id: str) -> list[Note]:
        """The notes not deleted that have the mark, shown or hidden."""
        return [note for note in self.listed_notes() if mark_id in note.marks]

    def remove_mark(self, mark_id: str) -> None:
        """Delete a mark: the notes that have it lose it, and stay as they are."""
        if self._repository is None:
            return
        try:
            removed = self._repository.remove_mark(mark_id)
        except (apsw.Error, KeyError) as error:
            log.error("could not delete a mark: %s", type(error).__name__)
            return
        log.info("a mark was deleted, %d notes kept", len(removed))
        self.refresh_labels()

    def new_category(self, window: NoteWindow) -> None:
        """Ask for a new category's name and colour, and give it to the note."""
        if self._labels is None:
            return
        dialog = new_category_dialog(
            window, self.create_category, next_dot_color(len(self.label_choices()[0]))
        )
        if dialog.created is not None:
            self.set_category(window, dialog.created.id)

    # See-through while not in use

    def set_opacity(self, window: NoteWindow, opacity: float) -> None:
        """How see-through the note is while another window is in use. A note not
        stored yet takes it when first stored; if storing fails it stays as it was."""
        if self._repository is not None and window.note_id is not None:
            try:
                self._repository.set_opacity(window.note_id, opacity)
            except apsw.Error as error:
                log.error("could not store a note's opacity: %s", type(error).__name__)
                window.set_opacity(window.opacity)  # the menu shows what is kept
                return
        window.set_opacity(opacity)

    # Colour

    def _free_cascade_spot(self, window: NoteWindow) -> QPoint:
        """The next step of the cascade where no note already sits.

        The cascade starts over with each run of the app, so its next step can
        be where a note made in an earlier run still is.
        """
        area = QGuiApplication.primaryScreen().availableGeometry()
        taken = {
            other.pos() for other in self._windows if other is not window and other.isVisible()
        }
        spots: list[QPoint] = []
        for _ in range(CASCADE_LENGTH):
            offset = CASCADE_ORIGIN + CASCADE_STEP * (self._created % CASCADE_LENGTH)
            self._created += 1
            spots.append(area.topLeft() + QPoint(offset, offset))
            if spots[-1] not in taken:
                return spots[-1]
        return spots[0]  # every step taken: the cascade goes on over them

    def _new_note_color(self) -> str:
        """The colour last chosen; or, at random, any but the one used just before."""
        if self._settings is None:
            return DEFAULT_COLOR
        if self._settings.get(RANDOM_COLOR):
            last = self._settings.get(LAST_USED_COLOR)
            order = self.color_order()
            color = self._random.choice([c for c in order if c != last] or order)
        else:
            color = self._settings.get(DEFAULT_NOTE_COLOR)
        self._used(color)
        return color

    def _used(self, color: str) -> None:
        if self._settings is None:
            return
        try:
            self._settings.set(LAST_USED_COLOR, color)
        except apsw.Error as error:
            log.error("could not keep the colour used: %s", type(error).__name__)

    def set_color(self, window: NoteWindow, color: str) -> None:
        """Give the note a colour (a palette key, or one of the user's own), which
        new notes then take too.

        A note that is not stored yet takes the colour when it is first stored.
        If storing fails the note keeps its colour, as nothing changed.
        """
        try:
            if self._repository is not None and window.note_id is not None:
                self._repository.set_color(window.note_id, color)
            if self._settings is not None:
                self._settings.set(DEFAULT_NOTE_COLOR, color)
                self._settings.set(LAST_USED_COLOR, color)
        except apsw.Error as error:
            log.error("could not store a note colour: %s", type(error).__name__)
            window.set_color(window.color)  # the menu shows the colour kept
            return
        window.set_color(color)

    # The user's own colours, and going through the colours by keyboard

    def own_colors(self) -> list[str]:
        """The colours the user added, in their order."""
        return [color.hex for color in self._colors.colors()] if self._colors else []

    def color_order(self) -> list[str]:
        """The palette, then the user's own colours."""
        return [*PALETTE, *self.own_colors()]

    def step_color(self, window: NoteWindow, step: int) -> None:
        """The next colour (step 1) or the one before (-1), round from the end
        to the start; the new colour's name said to a screen reader."""
        order = self.color_order()
        # One of the user's own no longer among them: from either end.
        start = order.index(window.color) if window.color in order else (-1 if step > 0 else 0)
        color = order[(start + step) % len(order)]
        self.set_color(window, color)
        if window.color == color:
            window.announce(window.color_label(color))

    def add_color(self, window: NoteWindow) -> None:
        """A colour chosen in the colour window: given to the note and added to
        the user's own (if there is room, or said so)."""
        dialog = ColorDialog(window.color, window)
        if not ask_color(dialog):
            return
        color = dialog.chosen()
        self.set_color(window, color)
        if self._colors is None:
            return
        try:
            self._colors.add(color)
        except OwnColorsFullError:
            QMessageBox.information(
                window,
                "Stickle",
                self.tr(
                    "My colours hold %1 at most. The note has the colour; to keep it among"
                    " them, remove one in the settings."
                ).replace("%1", str(MAX_OWN_COLORS)),
            )
            return
        except apsw.Error as error:
            log.error("could not keep a colour: %s", type(error).__name__)
            return
        self.own_colors_changed.emit()

    def add_own_color(self, color: str) -> bool:
        """From the settings; False if there is no room."""
        if self._colors is None:
            return False
        try:
            self._colors.add(color)
        except OwnColorsFullError:
            return False
        self.own_colors_changed.emit()
        return True

    def own_color_entries(self) -> list[OwnColor]:
        return self._colors.colors() if self._colors else []

    def remove_own_color(self, color_id: str) -> None:
        if self._colors is not None:
            self._colors.remove(color_id)
            self.own_colors_changed.emit()

    def move_own_color(self, color_id: str, step: int) -> None:
        if self._colors is not None:
            self._colors.move(color_id, step)
            self.own_colors_changed.emit()

    @property
    def random_color(self) -> bool:
        """Whether new notes take a colour at random."""
        return self._settings.get(RANDOM_COLOR) if self._settings is not None else False

    def set_random_color(self, on: bool) -> None:
        if self._settings is None:
            return
        try:
            self._settings.set(RANDOM_COLOR, on)
        except apsw.Error as error:
            log.error("could not keep the random colour setting: %s", type(error).__name__)

    # Secret notes

    def secret_key_here(self, parent: QWidget, typed: str | None, count: int) -> bytes | None:
        """The secret notes' key here, for count secret notes brought in: opened
        with typed if it is their password here too, else asked for (or the
        secret notes turned on, the first time). None if not given."""
        passwords = self.secrets
        if passwords is None:
            return None
        if typed and passwords.turned_on():
            try:
                return passwords.open(typed)
            except WrongPasswordError, KeyFileError, OSError:
                pass  # another password here: asked for
        dialog = (
            OpenSecretDialog(passwords, parent=parent, purpose="import", count=count)
            if passwords.turned_on() or self.has_secret_notes(in_trash=True)
            else TurnOnDialog(passwords, parent)
        )
        return dialog.take_key() if ask_secret(dialog) else None

    def make_secret(self, window: NoteWindow) -> None:
        """Ask for the secret notes' password (setting them up the first time)
        and a title, then keep the note's text encrypted only. It stays open."""
        passwords = self.secrets
        if self._repository is None or passwords is None or window.secret:
            return
        if not self.flush(window, closing=True):
            return
        # With secret notes stored but their key file out of reach, never turned
        # on anew: that new key would replace the one a backup could bring back.
        dialog = (
            OpenSecretDialog(passwords, parent=window, purpose="make")
            if passwords.turned_on() or self.has_secret_notes(in_trash=True)
            else TurnOnDialog(passwords, window)
        )
        opened = dialog.take_key() if ask_secret(dialog) else None
        if opened is None:
            return
        key = bytearray(opened)
        titled = SecretTitleDialog(parent=window, making=True)
        if not ask_secret(titled):
            _wipe(key)
            return
        title, text = titled.chosen(), window.text
        note_id = window.note_id
        try:
            note_id = note_id or self._store_new(window)
            self._repository.make_secret(note_id, title, seal_text(bytes(key), note_id, text))
        except (apsw.Error, OSError) as error:
            # Failed only after it was stored secret: the window follows the note,
            # or its later edits would be refused.
            if not self._stored_secret_note(note_id):
                log.error("could not make a note secret: %s", type(error).__name__)
                _wipe(key)
                QMessageBox.warning(
                    window, "Stickle", self.tr("The note could not be made secret.")
                )
                return
            log.warning("made a note secret, then: %s", type(error).__name__)
            assert note_id is not None
        self._keys[window] = key
        self._stored_secret[window] = text
        window.set_secret_title(title)
        self._relock.opened(window)
        self.changed.emit()

    def _stored_secret_note(self, note_id: str | None) -> bool:
        if self._repository is None or note_id is None:
            return False
        try:
            note = self._repository.get(note_id)
        except apsw.Error, OSError:
            return False
        return note is not None and note.secret

    def open_secret(self, window: NoteWindow) -> None:
        """Open a locked secret note with the secret notes' password: this one only."""
        passwords = self.secrets
        if (
            self._repository is None
            or passwords is None
            or not window.secret_locked
            or window.note_id is None
        ):
            return
        dialog = OpenSecretDialog(passwords, window.secret_name, window)
        opened = dialog.take_key() if ask_secret(dialog) else None
        if opened is None:
            return
        key = bytearray(opened)
        if window not in self._windows or not window.secret_locked:
            _wipe(key)  # closed meanwhile (or opened already): nothing to hold it for
            return
        try:
            note = self._repository.get(window.note_id)
            if note is None or note.sealed is None:
                raise SecretNoteError("not a secret note")
            text = open_text(bytes(key), note.id, note.sealed)
        except (SecretNoteError, apsw.Error) as error:
            log.error("could not open a secret note: %s", type(error).__name__)
            _wipe(key)
            QMessageBox.warning(
                window,
                "Stickle",
                self.tr("This secret note could not be opened. What is stored may be damaged."),
            )
            return
        self._keys[window] = key
        self._stored_secret[window] = text
        window.show_secret_open(text)
        self._relock.opened(window)

    def lock_secret(self, window: NoteWindow) -> bool:
        """Lock an open secret note, its text saved first (the character being
        composed too). False, and left open, if saving failed: locking would lose it."""
        if not window.secret or window.secret_locked:
            return True
        if not self.flush(window, closing=True):
            self._relock.used(window)  # tried again in a while
            return False
        self._forget_key(window)
        window.show_secret_locked(window.secret_title or "")
        return True

    def lock_secret_notes(self) -> None:
        """Every open secret note (the computer locked, asleep, its screen saver on)."""
        opened = self._relock.open_windows
        if opened:
            log.info("locking open secret notes: %d", len(opened))
        for window in opened:
            self.lock_secret(window)

    def _forget_key(self, window: NoteWindow) -> None:
        key = self._keys.pop(window, None)
        if key is not None:
            _wipe(key)
        self._stored_secret.pop(window, None)
        self._relock.closed(window)

    def change_secret_title(self, window: NoteWindow) -> None:
        if self._repository is None or window.secret_locked or window.note_id is None:
            return
        if window.secret_title is None:
            return
        dialog = SecretTitleDialog(window.secret_title, window)
        if not ask_secret(dialog) or window not in self._windows or not window.secret:
            return
        # Kept even if the note locked meanwhile: a title needs no key.
        title = dialog.chosen()
        try:
            self._repository.set_secret_title(window.note_id, title)
        except apsw.Error as error:
            log.error("could not keep a secret note's title: %s", type(error).__name__)
            QMessageBox.warning(window, "Stickle", self.tr("The title could not be kept."))
            return
        window.set_secret_title(title)
        self.changed.emit()

    def make_ordinary(self, window: NoteWindow) -> None:
        """An open secret note an ordinary one again, once the user says so."""
        if (
            self._repository is None
            or not window.secret
            or window.secret_locked
            or window.note_id is None
        ):
            return
        if not confirm_ordinary(window):
            return
        # Locked by itself while asked, its text is no longer in the window: what
        # it holds then must not become the note's text.
        if window not in self._windows or window.secret_locked:
            return
        if not self.flush(window, closing=True):
            return
        try:
            self._repository.make_ordinary(window.note_id, window.text)
        except apsw.Error as error:
            log.error("could not make a note ordinary: %s", type(error).__name__)
            QMessageBox.warning(window, "Stickle", self.tr("The note could not be made ordinary."))
            return
        self._forget_key(window)
        window.set_secret_title(None)
        self.changed.emit()

    @property
    def relock_minutes(self) -> int:
        """How long an open secret note is left alone before it locks again."""
        if self._settings is None:
            return RELOCK_MINUTES.default
        return self._settings.get(RELOCK_MINUTES)

    def set_relock_minutes(self, minutes: int) -> None:
        if self._settings is not None:
            try:
                self._settings.set(RELOCK_MINUTES, minutes)
            except apsw.Error as error:
                log.error("could not keep the relock time: %s", type(error).__name__)
        self._relock.restart_all()

    @property
    def lock_on_leave(self) -> bool:
        """Whether an open secret note locks as soon as another window is used."""
        if self._settings is None:
            return LOCK_ON_LEAVE.default
        return self._settings.get(LOCK_ON_LEAVE)

    def set_lock_on_leave(self, on: bool) -> None:
        if self._settings is not None:
            try:
                self._settings.set(LOCK_ON_LEAVE, on)
            except apsw.Error as error:
                log.error("could not keep locking on leaving: %s", type(error).__name__)

    # Hiding, deleting, bringing back

    def hide(self, window: NoteWindow) -> None:
        if self._quitting:
            window.release()
            return
        # A note that could not be saved stays open: closing it would lose the text.
        if not self.flush(window, closing=True):
            return
        if self._repository is not None and window.note_id is not None:
            try:
                # A secret note's text is not in its window: never taken for empty.
                # Nor is one with a reminder to ring: it is kept for that.
                if window.secret or window.text.strip() or self._has_reminder(window.note_id):
                    self._repository.set_hidden(window.note_id, True)
                else:
                    self._repository.discard(window.note_id)
            except apsw.Error as error:
                log.error("could not hide a note: %s", type(error).__name__)
                return
            self.changed.emit()
        self._autosaves[window].stop()
        window.release()

    def _has_reminder(self, note_id: str) -> bool:
        return self.reminders is not None and self.reminders.has(note_id)

    def stored_id(self, window: NoteWindow) -> str | None:
        """The note's id, stored first as it is now if it was not yet (even with
        no text: something is to be kept for it). None if it cannot be stored."""
        if self._repository is None:
            return None
        if window.note_id is None:
            try:
                self._store_new(window)
            except (apsw.Error, OSError) as error:
                log.error("could not store a note: %s", type(error).__name__)
                return None
        return window.note_id

    def note(self, note_id: str) -> Note | None:
        """The note as stored."""
        return self._repository.get(note_id) if self._repository else None

    def _delete(self, note_id: str, text: str, secret: bool) -> None:
        """Into the trash; with no text, for good (see NoteRepository.discard).
        A secret note always goes into the trash: its text is not in text."""
        assert self._repository is not None
        if secret or text.strip():
            self._repository.delete(note_id)
        else:
            self._repository.discard(note_id)

    def delete(self, window: NoteWindow) -> None:
        if not self.flush(window, closing=True):
            return
        if self._repository is not None and window.note_id is not None:
            try:
                self._delete(window.note_id, window.text, window.secret)
            except apsw.Error as error:
                log.error("could not delete a note: %s", type(error).__name__)
                return
            self.changed.emit()
        self._autosaves[window].stop()
        window.release()

    def waiting_to_save(self) -> bool:
        return any(autosave.waiting for autosave in self._autosaves.values())

    def hidden_notes(self) -> list[Note]:
        """Most recently hidden first."""
        return self._repository.hidden() if self._repository else []

    def last_deleted(self) -> Note | None:
        return self._repository.last_deleted() if self._repository else None

    def show_hidden(self, note_id: str, take_keyboard: bool = True, in_sight: bool = True) -> None:
        """A hidden note no longer hidden, and open (in_sight: shown too)."""
        if self._repository is None:
            return
        note = self._repository.set_hidden(note_id, False)
        self._open(note, take_keyboard=take_keyboard, in_sight=in_sight)
        self.changed.emit()

    def show_all_hidden(self) -> None:
        for note in reversed(self.hidden_notes()):
            self.show_hidden(note.id)

    # By note, for the list of notes

    def listed_notes(self) -> list[Note]:
        """Every note not deleted, shown or hidden; most recently changed first."""
        return self._repository.live() if self._repository else []

    def has_secret_notes(self, in_trash: bool = False) -> bool:
        """Whether a note shown or hidden (or, with in_trash, deleted) is secret."""
        notes = self.listed_notes() + (self.trash_notes() if in_trash else [])
        return any(note.secret for note in notes)

    def secret_key_fits(self, key: bytes) -> bool:
        """Whether key opens a stored secret note (True with none to try): a key
        is kept again for something only once it does, never one that opens
        nothing written over the copies of the real one."""
        if self._repository is None:
            return True
        try:
            stored = next((n for n in self._repository.all() if n.sealed is not None), None)
            if stored is None or stored.sealed is None:
                return True
            open_text(key, stored.id, stored.sealed)
        except (SecretNoteError, apsw.Error) as error:
            log.error("a secret notes' key that opens none of them: %s", type(error).__name__)
            return False
        return True

    def matching(self, term: str) -> set[str]:
        """Ids of stored notes whose text contains term (as last saved)."""
        return self._repository.matching(term) if self._repository else set()

    def window_for(self, note_id: str) -> NoteWindow | None:
        return next((w for w in self._windows if w.note_id == note_id), None)

    def open_note(self, note_id: str) -> None:
        """Bring a note to the front with the keyboard, showing it again if it was hidden."""
        window = self.window_for(note_id)
        if window is None:
            self.show_hidden(note_id)
            window = self.window_for(note_id)
            if window is None:
                return
        self._take_back(window)  # asked for by name: only this one comes back
        window.bring_to_front()

    def come_forward(self, note_id: str) -> NoteWindow | None:
        """A note its reminder asks for: in sight and in front, shown again if it
        was hidden, set aside or out of view, and unfolded, but leaving the
        keyboard where it is. None if it is not among the notes (in the trash).

        On a screen a full-screen program covers, it stays out of sight until
        that ends (the system's notification tells meanwhile)."""
        window = self.window_for(note_id)
        if window is None:
            note = self._repository.get(note_id) if self._repository else None
            if note is None or note.deleted:
                return None
            self.show_hidden(note_id, take_keyboard=False, in_sight=False)
            window = self.window_for(note_id)
            if window is None:
                return None
        if window.collapsed:
            self.set_collapsed(window, False)
        if not window.isVisible() and _screen_of(window) in self._covered_screens:
            if window not in self._covered:
                self._take_back(window)
                self._covered.append(window)
                self.covered_changed.emit()
            return window
        self._take_back(window)
        window.show()
        window.raise_()
        return window

    def hide_note(self, note_id: str) -> None:
        window = self.window_for(note_id)
        if window is not None:
            self.hide(window)

    def delete_note(self, note_id: str) -> None:
        window = self.window_for(note_id)
        if window is not None:
            self.delete(window)
            return
        if self._repository is None:
            return
        try:
            note = self._repository.get(note_id)
            if note is not None:
                self._delete(note_id, note.body, note.secret)
        except apsw.Error as error:
            log.error("could not delete a note: %s", type(error).__name__)
            return
        self.changed.emit()

    def restore_last_deleted(self) -> None:
        """The note deleted last, with any deleted together with it (a category's)."""
        note = self.last_deleted()
        if note is None or self._repository is None:
            return
        try:
            together = self._repository.deleted_with(note.id)
        except apsw.Error as error:
            log.error("could not read the trash: %s", type(error).__name__)
            return
        for other in together:
            self.restore_note(other.id)

    # The trash

    def trash_notes(self) -> list[Note]:
        """Deleted notes, most recently deleted first."""
        return self._repository.deleted() if self._repository else []

    def restore_note(self, note_id: str) -> None:
        """Take a note out of the trash, onto the screen."""
        if self._repository is None:
            return
        try:
            self._repository.restore(note_id)
            # Brought back to be seen, even if it was hidden when deleted.
            note = self._repository.set_hidden(note_id, False)
        except apsw.Error as error:
            log.error("could not restore a note: %s", type(error).__name__)
            return
        self._open(note).bring_to_front()
        self.changed.emit()

    def purge_note(self, note_id: str) -> None:
        """Empty one note from the trash for good (the user confirmed it)."""
        if self._repository is None:
            return
        try:
            self._repository.purge(note_id)
        except apsw.Error as error:
            log.error("could not empty a note from the trash: %s", type(error).__name__)
            return
        log.info("a note was emptied from the trash")
        self.changed.emit()

    def empty_trash(self) -> None:
        """Empty the whole trash for good (the user confirmed it)."""
        if self._repository is None:
            return
        try:
            emptied = self._repository.empty_trash()
        except apsw.Error as error:
            log.error("could not empty the trash: %s", type(error).__name__)
            return
        log.info("trash emptied: %d notes", emptied)
        self.changed.emit()

    def empty_old_trash(self) -> None:
        """At start: empty notes a year in the trash, forget old deletion records,
        and clear away old text a note made secret may have left behind."""
        if self._repository is None:
            return
        try:
            self._repository.forget_old_text_left()
        except (apsw.Error, OSError) as error:
            log.warning("old text not cleared away yet: %s", type(error).__name__)
        try:
            emptied = self._repository.purge_expired()
        except apsw.Error as error:
            log.error("could not empty old notes from the trash: %s", type(error).__name__)
            return
        if emptied:
            log.info("emptied %d notes kept a year in the trash", emptied)
