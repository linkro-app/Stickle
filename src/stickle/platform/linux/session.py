"""Notice when a Linux desktop is locked and unlocked, through its screen saver.

The screen saver says on the session bus when it starts and stops
(ActiveChanged): GNOME as org.gnome.ScreenSaver, Cinnamon (Linux Mint), MATE
and Xfce under their own names, KDE and others as
org.freedesktop.ScreenSaver. Its start is taken as the lock and its end as the
unlock, the screen being locked while it runs on most desktops. Without
either, nothing is watched: what changed is still told at the next start.
"""

from collections.abc import Callable

from PySide6.QtCore import SLOT, QObject, Slot
from PySide6.QtDBus import QDBusConnection

SCREEN_SAVERS = (
    ("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver"),
    ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver", "org.freedesktop.ScreenSaver"),
    ("org.freedesktop.ScreenSaver", "/ScreenSaver", "org.freedesktop.ScreenSaver"),
    ("org.cinnamon.ScreenSaver", "/org/cinnamon/ScreenSaver", "org.cinnamon.ScreenSaver"),
    ("org.mate.ScreenSaver", "/org/mate/ScreenSaver", "org.mate.ScreenSaver"),
    ("org.xfce.ScreenSaver", "/org/xfce/ScreenSaver", "org.xfce.ScreenSaver"),
)


class ScreenSaverWatcher(QObject):
    def __init__(self, on_unlock: Callable[[], object], on_lock: Callable[[], object]) -> None:
        super().__init__()
        self._on_unlock = on_unlock
        self._on_lock = on_lock
        bus = QDBusConnection.sessionBus()
        self.connected = bus.isConnected() and any(
            [
                bus.connect(
                    service,
                    path,
                    interface,
                    "ActiveChanged",
                    self,
                    # The text form SLOT() returns; the type hints say bytes, but PySide6
                    # rejects bytes here at run time.
                    SLOT("active_changed(bool)"),  # pyright: ignore[reportArgumentType]
                )
                for service, path, interface in SCREEN_SAVERS
            ]
        )

    @Slot(bool)
    def active_changed(self, active: bool) -> None:
        if active:
            self._on_lock()
        else:
            self._on_unlock()
