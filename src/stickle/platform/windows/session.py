"""Notice when the Windows session is unlocked.

Windows tells a window registered with WTSRegisterSessionNotification of
changes to the session: WM_WTSSESSION_CHANGE, with WTS_SESSION_UNLOCK once the
user unlocks the computer. A native event filter sees it for that window.
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
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0

_register = ctypes.windll.wtsapi32.WTSRegisterSessionNotification
_register.argtypes = [wintypes.HWND, wintypes.DWORD]
_register.restype = wintypes.BOOL


def is_unlock(message_address: int) -> bool:
    message = MSG.from_address(message_address)
    return message.message == WM_WTSSESSION_CHANGE and message.wParam == WTS_SESSION_UNLOCK


def register(window: int) -> bool:
    """Ask Windows to tell window of this session's changes; False if it refused."""
    return bool(_register(window, NOTIFY_FOR_THIS_SESSION))


class UnlockFilter(QAbstractNativeEventFilter):
    def __init__(self, on_unlock: Callable[[], object]) -> None:
        super().__init__()
        self._on_unlock = on_unlock

    @override
    def nativeEventFilter(
        self, eventType: QByteArray | bytes | bytearray | memoryview, message: int
    ) -> object:
        name = eventType.data() if isinstance(eventType, QByteArray) else bytes(eventType)
        if name == b"windows_generic_MSG" and is_unlock(int(message)):
            self._on_unlock()
        return False, 0
