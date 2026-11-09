"""Run a callback just before the computer sleeps, and one when it wakes, where
the system says so.

Windows and Linux (systemd-logind) announce sleep; macOS is not watched yet.
Returns an object to keep alive while watching, or None.
"""

import logging
import sys
from collections.abc import Callable

from PySide6.QtCore import QCoreApplication

log = logging.getLogger(__name__)


def watch_sleep(
    before_sleep: Callable[[], object], after_wake: Callable[[], object] = lambda: None
) -> object | None:
    """Never stops the app: saving after each pause in typing works without it."""
    try:
        return _watch(before_sleep, after_wake)
    except Exception as error:
        log.warning("sleep is not watched: %s: %s", type(error).__name__, error)
        return None


def _watch(before_sleep: Callable[[], object], after_wake: Callable[[], object]) -> object | None:
    if sys.platform == "win32":
        from stickle.platform.windows.power import SleepFilter

        app = QCoreApplication.instance()
        if app is None:
            return None
        sleep_filter = SleepFilter(before_sleep, after_wake)
        app.installNativeEventFilter(sleep_filter)
        return sleep_filter
    if sys.platform.startswith("linux"):
        from stickle.platform.linux.power import SleepWatcher

        watcher = SleepWatcher(before_sleep, after_wake)
        return watcher if watcher.connected else None
    return None
