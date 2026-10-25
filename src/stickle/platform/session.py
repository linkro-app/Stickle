"""Hear when the computer is locked and unlocked, where the system says so.

Windows tells a window of its session's changes; Linux desktops with a screen
saver on the session bus say when it starts (the screen locking too) and
stops. Windows' own screen saver is not announced: screen_saver_running is
asked instead, now and then. macOS is not watched yet.
"""

import logging
import sys
from collections.abc import Callable

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)


def _nothing() -> None:
    pass


def watch_session(
    on_unlock: Callable[[], object], on_lock: Callable[[], object] = _nothing
) -> object | None:
    """An object to keep alive while watching, or None. Never in the way:
    without it, changes are still told at the next start, and secret notes
    still lock once left alone."""
    try:
        return _watch(on_unlock, on_lock)
    except Exception as error:
        log.warning("locking is not watched: %s: %s", type(error).__name__, error)
        return None


def _watch(on_unlock: Callable[[], object], on_lock: Callable[[], object]) -> object | None:
    if sys.platform == "win32":
        from stickle.platform.windows.session import SessionFilter, register

        app = QCoreApplication.instance()
        if app is None:
            return None
        # Windows tells windows: one of its own, never shown.
        window = QWidget()
        session_filter = SessionFilter(on_unlock, on_lock)
        app.installNativeEventFilter(session_filter)
        if not register(int(window.winId())):
            app.removeNativeEventFilter(session_filter)
            return None
        return window, session_filter
    if sys.platform.startswith("linux"):
        from stickle.platform.linux.session import ScreenSaverWatcher

        watcher = ScreenSaverWatcher(on_unlock, on_lock)
        return watcher if watcher.connected else None
    return None


def screen_saver_running() -> bool:
    """Whether Windows' screen saver is on the screen (elsewhere its start is
    announced, so this is always False)."""
    if sys.platform != "win32":
        return False
    try:
        from stickle.platform.windows.session import screen_saver_running as running

        return running()
    except OSError:
        return False
