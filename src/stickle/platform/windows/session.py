"""Notice when the Windows session is locked and unlocked.

Windows tells a window registered with WTSRegisterSessionNotification of
changes to the session: WM_WTSSESSION_CHANGE, with WTS_SESSION_LOCK when the
computer is locked and WTS_SESSION_UNLOCK once the user unlocks it. A native
event filter sees it for that window. The screen saver is not among them:
SystemParametersInfo says whether it is running.
"""

import ctypes
import sys
from collections.abc import Callable
from ctypes import wintypes
from typing import override

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray

assert sys.platform == "win32"  # also tells the type checker the rest is Windows only

from stickle.platform.windows.power import MSG  # noqa: E402  (Windows only, as this)

WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0
SPI_GETSCREENSAVERRUNNING = 0x0072

_register = ctypes.windll.wtsapi32.WTSRegisterSessionNotification
_register.argtypes = [wintypes.HWND, wintypes.DWORD]
_register.restype = wintypes.BOOL
_parameters = ctypes.windll.user32.SystemParametersInfoW
_parameters.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
_parameters.restype = wintypes.BOOL


def session_change(message_address: int) -> int | None:
    """The change (WTS_SESSION_LOCK, WTS_SESSION_UNLOCK…) the message tells of, if it does."""
    message = MSG.from_address(message_address)
    return int(message.wParam) if message.message == WM_WTSSESSION_CHANGE else None


def register(window: int) -> bool:
    """Ask Windows to tell window of this session's changes; False if it refused."""
    return bool(_register(window, NOTIFY_FOR_THIS_SESSION))


def screen_saver_running() -> bool:
    running = wintypes.BOOL()
    if not _parameters(SPI_GETSCREENSAVERRUNNING, 0, ctypes.byref(running), 0):
        raise ctypes.WinError()
    return bool(running.value)


class SessionFilter(QAbstractNativeEventFilter):
    def __init__(self, on_unlock: Callable[[], object], on_lock: Callable[[], object]) -> None:
        super().__init__()
        self._on_unlock = on_unlock
        self._on_lock = on_lock

    @override
    def nativeEventFilter(
        self, eventType: QByteArray | bytes | bytearray | memoryview, message: int
    ) -> object:
        name = eventType.data() if isinstance(eventType, QByteArray) else bytes(eventType)
        if name == b"windows_generic_MSG":
            change = session_change(int(message))
            if change == WTS_SESSION_UNLOCK:
                self._on_unlock()
            elif change == WTS_SESSION_LOCK:
                self._on_lock()
        return False, 0
