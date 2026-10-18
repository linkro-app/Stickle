"""Closing for an update and coming back after it (Windows Restart Manager)."""

import ctypes
import sys
from ctypes import wintypes

import pytest

from stickle.platform.windows.restart_manager import (
    ENDSESSION_CLOSEAPP,
    WM_ENDSESSION,
    WM_QUERYENDSESSION,
    CloseForUpdate,
    asked_to_close,
    register_restart,
)


def test_only_a_close_for_an_update_counts() -> None:
    assert asked_to_close(WM_ENDSESSION, 1, ENDSESSION_CLOSEAPP)
    assert not asked_to_close(WM_ENDSESSION, 1, 0)  # a logout: Qt's own handling
    assert not asked_to_close(WM_ENDSESSION, 0, ENDSESSION_CLOSEAPP)  # cancelled
    assert not asked_to_close(WM_QUERYENDSESSION, 1, ENDSESSION_CLOSEAPP)  # only asked


@pytest.mark.skipif(sys.platform != "win32", reason="Windows messages")
def test_the_filter_saves_and_quits_when_asked() -> None:
    closed: list[bool] = []
    window_filter = CloseForUpdate(lambda: closed.append(True))
    message = wintypes.MSG()
    message.message = WM_ENDSESSION
    message.wParam = 1
    message.lParam = ENDSESSION_CLOSEAPP
    address = ctypes.addressof(message)

    assert window_filter.nativeEventFilter(b"windows_generic_MSG", address) == (False, 0)
    assert closed == [True]

    message.lParam = 0
    window_filter.nativeEventFilter(b"windows_generic_MSG", address)
    assert closed == [True]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_takes_the_restart_request() -> None:
    assert register_restart("--autostart")


@pytest.mark.skipif(sys.platform == "win32", reason="elsewhere")
def test_elsewhere_there_is_nothing_to_register() -> None:
    assert not register_restart("--autostart")
