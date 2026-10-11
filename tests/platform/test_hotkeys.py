"""Shortcuts registered with the system: the key combinations, and each system's own codes."""

import ctypes
import sys

import pytest
from pytestqt.qtbot import QtBot

from stickle.platform.hotkeys import Combo, x11_session
from stickle.platform.linux.hotkeys import CONTROL, MOD1, MOD4, SHIFT, XK_F1, keysym, modifier_mask


@pytest.mark.parametrize(
    ("text", "combo"),
    [
        ("Ctrl+Alt+N", Combo("N", ctrl=True, alt=True)),
        ("Ctrl+Shift+F7", Combo("F7", ctrl=True, shift=True)),
        ("Meta+5", Combo("5", meta=True)),
        ("Alt+n", Combo("N", alt=True)),
    ],
)
def test_combinations_are_read_as_qt_writes_them(text: str, combo: Combo) -> None:
    assert Combo.parse(text) == combo
    assert Combo.parse(combo.text) == combo


@pytest.mark.parametrize(
    "text", ["", "Ctrl+Ctrl+N", "Hyper+N", "Ctrl+Alt+Space", "Ctrl+Alt+F13", "Ctrl+Alt+ㅜ"]
)
def test_anything_else_is_not_a_combination(text: str) -> None:
    assert Combo.parse(text) is None


def test_only_combinations_held_with_ctrl_alt_or_the_windows_key_are_usable() -> None:
    assert Combo("N", ctrl=True).usable
    assert Combo("N", meta=True).usable
    assert not Combo("N").usable
    assert not Combo("N", shift=True).usable  # typing a capital N would set it off


@pytest.mark.parametrize(
    ("environment", "x11"),
    [
        ({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}, True),
        ({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}, False),
        ({"DISPLAY": ":0"}, True),
        ({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}, False),
        ({}, False),
    ],
)
def test_only_an_x11_session_is_grabbed_from(environment: dict[str, str], x11: bool) -> None:
    assert x11_session(environment) is x11


def test_x11_keys_and_modifiers() -> None:
    assert keysym("N") == ord("n")  # the key's own, unshifted symbol
    assert keysym("5") == ord("5")
    assert keysym("F12") == XK_F1 + 11
    assert modifier_mask(Combo("N", ctrl=True, alt=True)) == CONTROL | MOD1
    assert modifier_mask(Combo("N", shift=True, meta=True)) == SHIFT | MOD4


@pytest.mark.skipif(sys.platform != "win32", reason="Windows")
def test_windows_keys_and_modifiers() -> None:
    assert sys.platform == "win32"  # type checking on other systems passes over the rest
    from stickle.platform.windows.hotkeys import (
        MOD_ALT,
        MOD_CONTROL,
        MOD_NOREPEAT,
        MOD_WIN,
        VK_F1,
        modifier_flags,
        virtual_key,
    )

    assert virtual_key("N") == ord("N")
    assert virtual_key("7") == ord("7")
    assert virtual_key("F3") == VK_F1 + 2
    assert modifier_flags(Combo("N", ctrl=True, alt=True)) == MOD_NOREPEAT | MOD_CONTROL | MOD_ALT
    assert modifier_flags(Combo("N", meta=True)) == MOD_NOREPEAT | MOD_WIN


@pytest.mark.skipif(sys.platform != "win32", reason="Windows")
def test_a_windows_shortcut_is_registered_heard_and_let_go(qtbot: QtBot) -> None:
    assert sys.platform == "win32"  # type checking on other systems passes over the rest
    from stickle.platform.windows.hotkeys import WM_HOTKEY, WindowsHotkeys

    heard: list[int] = []
    hotkeys = WindowsHotkeys(heard.append)
    # A combination no one uses, so the test does not depend on the machine.
    rare = Combo("F11", ctrl=True, alt=True, shift=True, meta=True)
    assert hotkeys.register(7, rare)
    assert not WindowsHotkeys(heard.append).register(8, rare)  # taken: by us, here

    # As Windows posts it when the keys are pressed.
    thread = ctypes.windll.kernel32.GetCurrentThreadId()
    ctypes.windll.user32.PostThreadMessageW(thread, WM_HOTKEY, 7, 0)
    qtbot.waitUntil(lambda: heard == [7], timeout=2000)

    hotkeys.unregister(7)
    again = WindowsHotkeys(heard.append)
    assert again.register(9, rare)  # free again
    again.unregister(9)
