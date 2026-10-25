"""Run a callback once the computer is unlocked, where the system says so.

Windows and Linux desktops with a screen saver on the session bus announce
it; macOS is not watched yet. Returns an object to keep alive while watching,
or None.
"""

import logging
import sys
from collections.abc import Callable

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)


def watch_unlock(on_unlock: Callable[[], object]) -> object | None:
    """Never in the way: without it, changes are still told at the next start."""
    try:
        return _watch(on_unlock)
    except Exception as error:
        log.warning("unlocking is not watched: %s: %s", type(error).__name__, error)
        return None


def _watch(on_unlock: Callable[[], object]) -> object | None:
    if sys.platform == "win32":
        from stickle.platform.windows.session import UnlockFilter, register

        app = QCoreApplication.instance()
        if app is None:
            return None
        # Windows tells windows: one of its own, never shown.
        window = QWidget()
        unlock_filter = UnlockFilter(on_unlock)
        app.installNativeEventFilter(unlock_filter)
        if not register(int(window.winId())):
            app.removeNativeEventFilter(unlock_filter)
            return None
        return window, unlock_filter
    if sys.platform.startswith("linux"):
        from stickle.platform.linux.session import UnlockWatcher

        watcher = UnlockWatcher(on_unlock)
        return watcher if watcher.connected else None
    return None
