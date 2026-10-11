"""The open note windows, and keeping them in step with the stored notes.

A new note is stored once it has text: an untouched note that is closed
leaves nothing behind. Text is saved a second after typing stops, at least
every five seconds while typing goes on, when it loses focus, when the note
is hidden or deleted, and before the app quits, the session ends or the
computer sleeps. A character still being composed by an input method is
saved once it is committed; hiding, quitting and sleeping ask the input
method to commit it first. If saving fails, the text stays in the window, a
mark in its title bar says so, and saving is tried again. Hiding keeps the note for later;
hiding a note whose text was all erased deletes it instead (it can still be
restored), so the hidden list never fills up with empty notes. Deleting only
marks the note. Setting every note aside only takes the windows out of sight
for a while: nothing stored changes.

Where each note is, and its size, is kept once moving or resizing stops, and
before it is hidden or the app quits (see stickle.core.layout); a stored note
opens where it was, and notes are put back when monitors come or go.

Without a repository (measurement mode, some tests) the windows are simply
not stored.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import override

import apsw
from PySide6.QtCore import QEvent, QObject, QPoint, QPropertyAnimation, QRect, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

from stickle.app.category_dialog import CreateCategory, NewCategoryDialog, next_dot_color
from stickle.app.labels import mark_name
from stickle.app.note_window import NoteWindow
from stickle.app.placement import MonitorWatch, can_place_windows, monitors, qrect, rect
from stickle.core.labels import DEFAULT_CATEGORY_ICON, Category, Mark
from stickle.core.layout import MAIN, Place, fit, monitor_at, remember, restore
from stickle.core.note import DEFAULT_COLOR, Note
from stickle.core.snap import snapped, together
from stickle.data.labels import LabelRepository
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.settings import DEFAULT_NOTE_COLOR, Settings
from stickle.platform.linux.x11 import activate

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
# A note lined up where it was dropped slides there this quickly.
SNAP_SLIDE_MS = 100


def clipboard_text() -> str:
    """The clipboard's plain text, as a note can hold it; empty if it holds none.

    NUL cannot be stored in a note (see the schema), so it is left out.
    """
    text = QGuiApplication.clipboard().text().replace("\x00", "")
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
    # A note asked for the keys at a glance (F1, Ctrl+/, its menu).
    guide_requested = Signal()

    def __init__(
        self,
        repository: NoteRepository | None = None,
        idle_ms: int = IDLE_MS,
        max_ms: int = MAX_MS,
        settings: Settings | None = None,
        layouts: LayoutRepository | None = None,
        labels: LabelRepository | None = None,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._settings = settings
        self._layouts = layouts
        self._labels = labels
        self._monitor_watch = MonitorWatch(self)
        self._monitor_watch.changed.connect(self.place_all)
        self._idle_ms = idle_ms
        self._max_ms = max_ms
        self._windows: list[NoteWindow] = []
        self._autosaves: dict[NoteWindow, AutoSave] = {}
        self._set_aside: list[NoteWindow] = []
        self._view: str | None = None  # see view
        self._out_of_view: list[NoteWindow] = []
        self._find_among: list[NoteWindow] | None = None  # while finding on the desktop
        # Moving with Shift: the notes that come along, and where each started.
        self._groups: dict[NoteWindow, list[tuple[NoteWindow, QPoint]]] = {}
        self._created = 0
        self._quitting = False

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
        """
        if self._repository is None:
            self.new_note()
            return
        visible = self._repository.visible()
        for note in visible:
            self._open(note)
        if not visible and not self._repository.all():
            self.new_note()
        log.info("opened %d notes", len(visible))

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

    def _open(self, note: Note | None, text: str = "", color: str | None = None) -> NoteWindow:
        if note is not None:
            window = NoteWindow(
                note.id,
                note.body,
                note.color,
                note.always_on_top,
                opacity=note.opacity,
                locked=note.locked,
            )
        else:
            window = NoteWindow(None, text, color or self._new_note_color())
        window.new_note_requested.connect(lambda: self.new_note_from(window))

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
        elif self._view is not None and self._view != NO_CATEGORY:
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

        window.show()
        window.activateWindow()
        window.setFocus()
        return window

    def _forget(self, window: NoteWindow) -> None:
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
        text = window.text
        try:
            if window.note_id is None:
                if text:
                    window.note_id = self._repository.create(text, window.color).id
                    self.note_created.emit()
                    self.save_layout(window, force=True)
                    if window.collapsed:
                        self._repository.set_collapsed(window.note_id, True)
                    if not window.always_on_top:
                        self._repository.set_always_on_top(window.note_id, False)
                    if window.opacity != 1.0:
                        self._repository.set_opacity(window.note_id, window.opacity)
                    if window.locked:
                        self._repository.set_locked(window.note_id, True)
                    if window.category is not None:
                        self._repository.set_category(window.note_id, window.category.id)
                    for mark in window.marks:
                        self._repository.set_mark(window.note_id, mark.id, True)
            else:
                self._repository.update_body(window.note_id, text)
        except (apsw.Error, OSError) as error:
            log.error("could not save a note: %s", type(error).__name__)
            window.set_unsaved(True)
            return False
        window.set_unsaved(False)
        if window.note_id is not None:
            self.note_saved.emit()
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
        if not window.dragging:
            self.save_layout(window)
            return
        window.dragging = False
        moving = [window, *(other for other, _start in self._groups.pop(window, []))]
        delta = QPoint()
        if not window.alt_at_drop and can_place_windows():
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

    def place_all(self) -> None:
        """Monitors were connected, removed or changed: put every note where it belongs."""
        if not can_place_windows():
            return
        current = monitors()
        for window in self._windows:
            if self._restore_place(window):
                continue
            # Not remembered yet: at least keep it on a monitor.
            where = rect(window.expanded_geometry())
            _, monitor = monitor_at(where, current)
            window.place(qrect(fit(where, monitor.available)), window.own_monitor)
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
        changes: they are not hidden notes, and the next start shows them."""
        shown = [window for window in self._windows if window.isVisible()]
        if not shown:
            return
        for window in shown:
            # A note that could not be saved keeps its text in the window, which
            # goes on trying; out of sight is not closed.
            self.flush(window, closing=True)
            window.hide()
        self._set_aside.extend(shown)
        log.info("notes set aside: %d", len(shown))
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
        """One note is no longer set aside or out of view (shown on its own, or closed)."""
        if window in self._out_of_view:
            self._out_of_view.remove(window)
        if window in self._set_aside:
            self._set_aside.remove(window)
            if not self._set_aside:
                self.set_aside_changed.emit()

    # On the desktop, the notes of one category only

    @property
    def view(self) -> str | None:
        """The category whose notes alone are on the desktop, NO_CATEGORY for the
        notes with none, or None for every note."""
        return self._view

    def _in_view(self, window: NoteWindow) -> bool:
        if self._view is None:
            return True
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
        category, then each category in its order."""
        return [
            (None, self.tr("All notes")),
            (NO_CATEGORY, self.tr("Notes with no category")),
            *((c.id, c.name) for c in self.label_choices()[0]),
        ]

    def view_name(self) -> str:
        """The name of the view on the desktop."""
        return dict(self.view_choices()).get(self._view, "")

    def next_view(self) -> None:
        """Every note, then each category in its order, then every note again."""
        order: list[str | None] = [None, *(c.id for c in self.label_choices()[0])]
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

    def create_category(self, name: str, color: str, icon: str = DEFAULT_CATEGORY_ICON) -> Category:
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
        return self._settings.get(DEFAULT_NOTE_COLOR) if self._settings else DEFAULT_COLOR

    def set_color(self, window: NoteWindow, color: str) -> None:
        """Give the note a palette colour, which new notes then take too.

        A note that is not stored yet takes the colour when it is first stored.
        If storing fails the note keeps its colour, as nothing changed.
        """
        try:
            if self._repository is not None and window.note_id is not None:
                self._repository.set_color(window.note_id, color)
            if self._settings is not None:
                self._settings.set(DEFAULT_NOTE_COLOR, color)
        except apsw.Error as error:
            log.error("could not store a note colour: %s", type(error).__name__)
            window.set_color(window.color)  # the menu shows the colour kept
            return
        window.set_color(color)

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
                if window.text.strip():
                    self._repository.set_hidden(window.note_id, True)
                else:
                    self._repository.delete(window.note_id)
            except apsw.Error as error:
                log.error("could not hide a note: %s", type(error).__name__)
                return
            self.changed.emit()
        self._autosaves[window].stop()
        window.release()

    def delete(self, window: NoteWindow) -> None:
        if not self.flush(window, closing=True):
            return
        if self._repository is not None and window.note_id is not None:
            try:
                self._repository.delete(window.note_id)
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

    def show_hidden(self, note_id: str) -> None:
        if self._repository is None:
            return
        note = self._repository.set_hidden(note_id, False)
        self._open(note)
        self.changed.emit()

    def show_all_hidden(self) -> None:
        for note in reversed(self.hidden_notes()):
            self.show_hidden(note.id)

    # By note, for the list of notes

    def listed_notes(self) -> list[Note]:
        """Every note not deleted, shown or hidden; most recently changed first."""
        return self._repository.live() if self._repository else []

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
            self._repository.delete(note_id)
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
        """At start: empty notes a year in the trash, forget old deletion records."""
        if self._repository is None:
            return
        try:
            emptied = self._repository.purge_expired()
        except apsw.Error as error:
            log.error("could not empty old notes from the trash: %s", type(error).__name__)
            return
        if emptied:
            log.info("emptied %d notes kept a year in the trash", emptied)
