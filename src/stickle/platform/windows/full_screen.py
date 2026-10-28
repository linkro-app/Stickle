"""Full-screen windows of other programs on Windows (see stickle.platform.full_screen).

The shell tells whether a full-screen program (or a presentation) is in front;
the windows it means are those that cover their whole monitor. A maximized
window keeping its title bar covers it too when the taskbar hides itself: that
one is left out.
"""

import ctypes
import os
import sys
from collections.abc import Collection
from ctypes import wintypes
from pathlib import PureWindowsPath

from stickle.platform.full_screen import FullScreen

assert sys.platform == "win32"  # also tells the type checker the rest is Windows only

# What SHQueryUserNotificationState says while a program is full screen in front.
BUSY = 2
RUNNING_D3D_FULL_SCREEN = 3
PRESENTATION_MODE = 4
FULL_SCREEN_STATES = (BUSY, RUNNING_D3D_FULL_SCREEN, PRESENTATION_MODE)

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CAPTION = 0x00C00000
WS_EX_TRANSPARENT = 0x00000020  # clicked through: an overlay drawn over the screen
DWMWA_CLOAKED = 14
MONITOR_DEFAULTTONEAREST = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# The desktop and the taskbars: never a program shown full screen.
SHELL_CLASSES = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd")


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_user32 = ctypes.windll.user32
_user32.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.IsZoomed.argtypes = [wintypes.HWND]
_user32.IsZoomed.restype = wintypes.BOOL
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HMONITOR
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MonitorInfo)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL
_shell32 = ctypes.windll.shell32
_shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
_shell32.SHQueryUserNotificationState.restype = ctypes.c_long
_dwmapi = ctypes.windll.dwmapi
_dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
]  # fmt: skip
_dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
_kernel32 = ctypes.windll.kernel32
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]  # fmt: skip
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def full_screen_in_front() -> bool:
    """Whether the shell says a full-screen program, or a presentation, is in front."""
    state = ctypes.c_int(0)
    if _shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
        return False
    return state.value in FULL_SCREEN_STATES


class WindowsFullScreen:
    def __init__(self) -> None:
        self._own = os.getpid()

    def find(self, known: Collection[int]) -> list[FullScreen]:
        if full_screen_in_front():
            candidates = _top_level_windows()
        elif known:
            candidates = [window for window in known if _user32.IsWindow(window)]
        else:
            return []
        found: list[FullScreen] = []
        for window in candidates:
            shown = self._full_screen(window)
            if shown is not None:
                found.append(shown)
        return found

    def _full_screen(self, window: int) -> FullScreen | None:
        if not _user32.IsWindowVisible(window) or _user32.IsIconic(window):
            return None
        process = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(window, ctypes.byref(process))
        if process.value in (0, self._own):
            return None
        if _user32.GetWindowLongW(window, GWL_EXSTYLE) & WS_EX_TRANSPARENT:
            return None
        if _user32.IsZoomed(window) and (
            _user32.GetWindowLongW(window, GWL_STYLE) & WS_CAPTION == WS_CAPTION
        ):
            return None
        cloaked = wintypes.DWORD(0)  # kept off screen, as other desktops' windows are
        if (
            _dwmapi.DwmGetWindowAttribute(
                window, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
            )
            == 0
            and cloaked.value
        ):
            return None
        name = ctypes.create_unicode_buffer(64)
        _user32.GetClassNameW(window, name, len(name))
        if name.value in SHELL_CLASSES:
            return None
        rect = wintypes.RECT()
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        monitor = _user32.MonitorFromWindow(window, MONITOR_DEFAULTTONEAREST)
        if not _user32.GetWindowRect(window, ctypes.byref(rect)) or not (
            monitor and _user32.GetMonitorInfoW(monitor, ctypes.byref(info))
        ):
            return None
        area = info.rcMonitor
        if not (
            rect.left <= area.left
            and rect.top <= area.top
            and rect.right >= area.right
            and rect.bottom >= area.bottom
        ):
            return None
        return FullScreen(
            window,
            _program(process.value),
            (area.left, area.top, area.right - area.left, area.bottom - area.top),
            info.szDevice,
        )


def _top_level_windows() -> list[int]:
    windows: list[int] = []

    def visit(window: int | None, _: int) -> bool:
        if window:
            windows.append(window)
        return True

    _user32.EnumWindows(_EnumProc(visit), 0)
    return windows


def _program(process: int) -> str:
    """The file name of the program a process runs ("" if Windows will not say)."""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process)
    if not handle:
        return ""
    try:
        path = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(path))
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            return ""
        return PureWindowsPath(path.value).name
    finally:
        _kernel32.CloseHandle(handle)
