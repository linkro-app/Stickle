"""Which window is in front, and whether it is another program's."""

import ctypes
import os
import sys
from ctypes import wintypes

assert sys.platform == "win32"  # also tells the type checker the rest is Windows only

_user32 = ctypes.windll.user32
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD


def foreground_window() -> int:
    """The window in front (0 when none, as while the front one changes)."""
    return _user32.GetForegroundWindow() or 0


def is_another_program(window: int) -> bool:
    process = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(window, ctypes.byref(process))
    return process.value not in (0, os.getpid())
