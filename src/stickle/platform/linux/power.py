"""Notice when Linux is about to sleep, through systemd-logind.

logind sends PrepareForSleep(true) on the system bus just before suspending
and PrepareForSleep(false) after waking. Without logind (or without a system
bus) nothing is watched; saving a second after the last keystroke still
bounds what a sudden power loss could take.
"""

from collections.abc import Callable

from PySide6.QtCore import SLOT, QObject, Slot
from PySide6.QtDBus import QDBusConnection

SERVICE = "org.freedesktop.login1"
PATH = "/org/freedesktop/login1"
INTERFACE = "org.freedesktop.login1.Manager"


class SleepWatcher(QObject):
    def __init__(
        self, before_sleep: Callable[[], object], after_wake: Callable[[], object] = lambda: None
    ) -> None:
        super().__init__()
        self._before_sleep = before_sleep
        self._after_wake = after_wake
        bus = QDBusConnection.systemBus()
        self.connected = bus.isConnected() and bus.connect(
            SERVICE,
            PATH,
            INTERFACE,
            "PrepareForSleep",
            self,
            # The text form SLOT() returns; the type hints say bytes, but PySide6
            # rejects bytes here at run time.
            SLOT("prepare_for_sleep(bool)"),  # pyright: ignore[reportArgumentType]
        )

    @Slot(bool)
    def prepare_for_sleep(self, starting: bool) -> None:
        if starting:
            self._before_sleep()
        else:
            self._after_wake()
