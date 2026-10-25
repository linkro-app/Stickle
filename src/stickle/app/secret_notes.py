"""When open secret notes lock again by themselves.

Each one on its own once left alone for a while (no typing, clicking or
scrolling in it), all of them as the computer is locked, goes to sleep or
shows its screen saver, and, if chosen, a note as soon as another window is
used. A window of the note's own (its password or title dialog) is not
another window. Locking itself is the owner's (it saves first): this only
says when.
"""

from collections.abc import Callable
from typing import override

from PySide6.QtCore import QEvent, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget

from stickle.app.note_window import NoteWindow
from stickle.platform.session import screen_saver_running

MINUTE_MS = 60_000
SCREEN_SAVER_CHECK_MS = 5_000
# Typing, clicking and scrolling in the note: it is in use.
USE = (
    QEvent.Type.KeyPress,
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.Wheel,
    QEvent.Type.InputMethod,
)


class Relock(QObject):
    lock_due = Signal(object)  # the NoteWindow to lock now
    all_due = Signal()  # every open one

    def __init__(
        self,
        minutes: Callable[[], int],
        on_leave: Callable[[], bool],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._minutes = minutes
        self._on_leave = on_leave
        self._timers: dict[NoteWindow, QTimer] = {}
        self._watched: dict[QObject, NoteWindow] = {}
        # Windows' screen saver is not announced: looked for while a note is open.
        self._screen_saver = QTimer(self)
        self._screen_saver.setInterval(SCREEN_SAVER_CHECK_MS)
        self._screen_saver.timeout.connect(self._check_screen_saver)

    @property
    def open_windows(self) -> tuple[NoteWindow, ...]:
        return tuple(self._timers)

    def opened(self, window: NoteWindow) -> None:
        """window was opened with the password: its time runs from now."""
        if window not in self._timers:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self.lock_due.emit(window))
            self._timers[window] = timer
            for part in self._parts(window):
                self._watched[part] = window
                part.installEventFilter(self)
            window.text_changed.connect(self._used_by_signal)
        self.used(window)
        self._screen_saver.start()

    def closed(self, window: NoteWindow) -> None:
        """window was locked, or is gone."""
        timer = self._timers.pop(window, None)
        if timer is None:
            return
        timer.stop()
        timer.deleteLater()
        for part in self._parts(window):
            self._watched.pop(part, None)
            part.removeEventFilter(self)
        window.text_changed.disconnect(self._used_by_signal)
        if not self._timers:
            self._screen_saver.stop()

    def used(self, window: NoteWindow) -> None:
        timer = self._timers.get(window)
        if timer is not None:
            timer.start(self._minutes() * MINUTE_MS)

    def restart_all(self) -> None:
        """The time to wait was changed: it runs again from now, at the new length."""
        for window in self._timers:
            self.used(window)

    @staticmethod
    def _parts(window: NoteWindow) -> list[QObject]:
        # Where typing, clicks and the wheel arrive; the window itself to hear it
        # stop being the active one.
        return [
            window,
            window.editor,
            window.editor.viewport(),
            window.view,
            window.view.viewport(),
            window.title_bar,
        ]

    def _used_by_signal(self) -> None:
        window = self.sender()
        if isinstance(window, NoteWindow):
            self.used(window)

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        window = self._watched.get(watched)
        if window is not None:
            if event.type() in USE:
                self.used(window)
            elif (
                watched is window
                and event.type() == QEvent.Type.WindowDeactivate
                and self._on_leave()
            ):
                # Once the window that took over is known.
                QTimer.singleShot(0, self, lambda: self._left(window))
        return super().eventFilter(watched, event)

    def _left(self, window: NoteWindow) -> None:
        if not self._on_leave() or window not in self._timers or window.isActiveWindow():
            return
        active = QApplication.activeWindow()
        if QApplication.activePopupWidget() is not None or (
            active is not None and _belongs_to(active, window)
        ):
            return  # its own menu or dialog
        self.lock_due.emit(window)

    def _check_screen_saver(self) -> None:
        if screen_saver_running():
            self.all_due.emit()


def _belongs_to(widget: QWidget, window: NoteWindow) -> bool:
    """Whether widget is a window opened from window (a dialog of its own)."""
    parent = widget.parentWidget()
    while parent is not None:
        if parent is window:
            return True
        parent = parent.parentWidget()
    return False
