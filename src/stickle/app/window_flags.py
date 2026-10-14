"""Changing whether a window stays above others, while it is on screen."""

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget


def stays_on_top(widget: QWidget) -> bool:
    return bool(widget.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)


def set_stays_on_top(widget: QWidget, on_top: bool) -> None:
    """Keep a window above others, or not, without it blinking."""
    if on_top == stays_on_top(widget):
        return
    if not widget.testAttribute(Qt.WidgetAttribute.WA_WState_Created):  # never shown yet
        widget.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on_top)
        return
    # Changed on the window as it is: setWindowFlag would destroy and recreate
    # it, which hides it for a moment.
    flags = widget.windowFlags()
    if on_top:
        flags |= Qt.WindowType.WindowStaysOnTopHint
    else:
        # Not with ~: Python's inverse of a Qt flag leaves out the bits it holds
        # for aliases, the close button hint among them. Windows makes the window
        # again from these flags once it is closed, and it came back with no
        # working close button.
        flags = Qt.WindowType(flags.value & ~Qt.WindowType.WindowStaysOnTopHint.value)
    widget.overrideWindowFlags(flags)
    # sys.platform first, spelled out: type checkers on other systems then skip
    # the Windows-only module.
    if sys.platform == "win32" and _native_windows():
        # Only the stacking order: Qt would restyle the window, and a see-through
        # one lost what was drawn in it (see platform/windows/topmost.py).
        from stickle.platform.windows.topmost import set_topmost

        set_topmost(int(widget.winId()), on_top)
    else:
        widget.windowHandle().setFlags(flags)


def _native_windows() -> bool:
    """Real Windows windows (not, say, the off-screen ones of tests)."""
    return QGuiApplication.platformName() == "windows"


def keep_stays_on_top(widget: QWidget) -> None:
    """After a window is shown again: as it is meant to be, whatever Qt restored."""
    if (
        sys.platform == "win32"
        and _native_windows()
        and widget.testAttribute(Qt.WidgetAttribute.WA_WState_Created)
    ):
        from stickle.platform.windows.topmost import set_topmost

        # Qt shows a window with the flags it last applied itself, which the
        # stacking-order change above leaves as they were.
        set_topmost(int(widget.winId()), stays_on_top(widget))
