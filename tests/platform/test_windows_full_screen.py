"""Full-screen windows of other programs on Windows, found with the shell quiet:
it tells only of the main monitor, so a program full screen on another one is
seen by the window in front alone."""

import ctypes
import sys
from collections.abc import Iterator

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows' own windows")

if sys.platform == "win32":
    from ctypes import wintypes

    WS_POPUP = 0x80000000
    WS_VISIBLE = 0x10000000
    SM_CXSCREEN, SM_CYSCREEN = 0, 1

    _user32 = ctypes.WinDLL("user32")
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]  # fmt: skip
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.GetSystemMetrics.restype = ctypes.c_int

    def screen() -> tuple[int, int]:
        """The main monitor's size, in this process's pixels."""
        return int(_user32.GetSystemMetrics(SM_CXSCREEN)), int(
            _user32.GetSystemMetrics(SM_CYSCREEN)
        )

    def popup(made: list[int], width: int, height: int) -> int:
        """A window without a frame at the main monitor's top left, of this process."""
        window = int(
            _user32.CreateWindowExW(
                0, "Static", "", WS_POPUP | WS_VISIBLE, 0, 0, width, height, None, None, None, None
            )
            or 0
        )
        assert window
        made.append(window)
        return window

    def destroy(window: int) -> None:
        _user32.DestroyWindow(window)


@pytest.fixture
def made() -> Iterator[list[int]]:
    """The windows a test makes, gone after it."""
    windows: list[int] = []
    yield windows
    if sys.platform == "win32":
        for window in windows:
            destroy(window)


def test_a_window_in_front_covering_its_monitor_is_found_with_the_shell_quiet(
    made: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "win32":  # also tells the type checker this is Windows
        return
    from stickle.platform.windows import full_screen

    width, height = screen()
    filling = popup(made, width, height)
    monkeypatch.setattr(full_screen, "full_screen_in_front", lambda: False)
    monkeypatch.setattr(full_screen, "_front_window", lambda: filling)
    source = full_screen.WindowsFullScreen()
    source._own = -1  # pyright: ignore[reportPrivateUsage]  # this process's window counts here

    found = source.find([])

    assert [(each.window, each.monitor) for each in found] == [(filling, (0, 0, width, height))]


def test_a_window_in_front_covering_less_is_not(
    made: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "win32":
        return
    from stickle.platform.windows import full_screen

    width, height = screen()
    half = popup(made, width // 2, height // 2)
    monkeypatch.setattr(full_screen, "full_screen_in_front", lambda: False)
    monkeypatch.setattr(full_screen, "_front_window", lambda: half)
    source = full_screen.WindowsFullScreen()
    source._own = -1  # pyright: ignore[reportPrivateUsage]

    assert source.find([]) == []


def test_with_the_shell_quiet_only_the_window_in_front_and_those_known_are_looked_at(
    made: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "win32":
        return
    from stickle.platform.windows import full_screen

    width, height = screen()
    behind, known = popup(made, width, height), popup(made, width, height)
    monkeypatch.setattr(full_screen, "full_screen_in_front", lambda: False)
    monkeypatch.setattr(full_screen, "_front_window", lambda: 0)
    source = full_screen.WindowsFullScreen()
    source._own = -1  # pyright: ignore[reportPrivateUsage]

    found = [each.window for each in source.find([known])]

    assert found == [known]
    assert behind not in found
