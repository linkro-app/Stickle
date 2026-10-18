"""Closing for an update, and coming back after it (Windows Restart Manager).

When the Microsoft Store (or an installer) updates Stickle while it runs,
Windows asks it to close with WM_QUERYENDSESSION and WM_ENDSESSION carrying
ENDSESSION_CLOSEAPP, which is not the end of the session: Qt keeps running
then, and the update waited for the user to retry and to start Stickle
again. Stickle now saves and quits when asked, and registers to be started
again by Windows once the update is in, only then (not after a crash, a hang
or a restart of the computer, which starting at login covers).
"""

import ctypes
import sys
from collections.abc import Callable
from typing import override

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
ENDSESSION_CLOSEAPP = 0x00000001
RESTART_NO_CRASH = 1
RESTART_NO_HANG = 2
RESTART_NO_REBOOT = 8
RESTART_FLAGS = RESTART_NO_CRASH | RESTART_NO_HANG | RESTART_NO_REBOOT


def asked_to_close(message: int, wparam: int, lparam: int) -> bool:
    """Whether a window message asks Stickle to close for an update (not a logout)."""
    return message == WM_ENDSESSION and wparam != 0 and bool(lparam & ENDSESSION_CLOSEAPP)


def register_restart(arguments: str) -> bool:
    """Ask Windows to start Stickle again, with arguments, after an update closed it."""
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    register = kernel32.RegisterApplicationRestart
    register.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    register.restype = ctypes.c_long
    return register(arguments, RESTART_FLAGS) == 0  # S_OK


class CloseForUpdate(QAbstractNativeEventFilter):
    """Calls on_close (which saves and quits) when Windows asks to close for an update."""

    def __init__(self, on_close: Callable[[], object]) -> None:
        super().__init__()
        self._on_close = on_close

    @override
    def nativeEventFilter(
        self, eventType: QByteArray | bytes | bytearray | memoryview, message: int
    ) -> object:
        name = eventType.data() if isinstance(eventType, QByteArray) else bytes(eventType)
        if sys.platform != "win32" or name != b"windows_generic_MSG":
            return False, 0
        from ctypes import wintypes

        msg = wintypes.MSG.from_address(int(message))
        if asked_to_close(msg.message, msg.wParam, msg.lParam):
            self._on_close()
        return False, 0
