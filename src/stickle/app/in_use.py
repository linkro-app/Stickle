"""A window the user called up, kept above the others until they move on from it.

Opened by a second start or from the tray, a window may not be handed the
keyboard by the window manager, and pinned notes stay above every ordinary
window: kept above them until the user moves on from it, it is at least seen.
The windows it opens (its dialogs, the system's file chooser) are kept above it
by the system, so it stays above while the keyboard is in any of them.
"""

from typing import override

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

from stickle.app import file_dialogs
from stickle.app.window_flags import keep_stays_on_top, set_stays_on_top, stays_on_top


class OnTopWhileInUse(QObject):
    """Takes a window out of the top once the user has moved on from it and the
    windows it opened; it is put there with set_stays_on_top as it is called up."""

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._window = window
        window.installEventFilter(self)
        app = QGuiApplication.instance()
        assert isinstance(app, QGuiApplication)
        # From one of its windows to another app: it is not told itself.
        app.focusWindowChanged.connect(self._let_go_soon)

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        window = self._window
        if watched is window:
            # Not on activation: that can be reported before the window is in front.
            # Hidden for real: Ubuntu also said so as the window came up, still on screen.
            if event.type() == QEvent.Type.Hide and not window.isVisible() and stays_on_top(window):
                set_stays_on_top(window, False)
            elif event.type() == QEvent.Type.WindowDeactivate and stays_on_top(window):
                self._let_go_soon()
            elif event.type() == QEvent.Type.Show and not event.spontaneous():
                # Once shown: Qt may have put back an on-top state since changed.
                QTimer.singleShot(0, window, lambda: keep_stays_on_top(window))
        return False

    def _let_go_soon(self) -> None:
        # Once the keyboard has landed, not on the way: in one of its own
        # windows, it stays.
        if stays_on_top(self._window):
            QTimer.singleShot(0, self, self._let_go_unless_in_use)

    def _let_go_unless_in_use(self) -> None:
        """No longer above the others once the user has moved on from it and
        the windows it opened: those stay above pinned notes only with it."""
        if not stays_on_top(self._window):
            return
        # A file chooser of the system has no Qt window, but it is modal to one.
        widget = (
            QApplication.activeModalWidget()
            or file_dialogs.asking_for()
            or QApplication.activeWindow()
        )
        while widget is not None:
            if widget is self._window:
                return
            widget = widget.parentWidget()
        set_stays_on_top(self._window, False)
