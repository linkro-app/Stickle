"""Windows shown full screen by other programs: a slide show, a video, a game.

Windows says when a program in front is full screen (or a presentation is on),
and which window that is has to be worked out: one covering its whole monitor.
X11 windows say so themselves, with _NET_WM_STATE_FULLSCREEN. Under Wayland an
application cannot see other programs' windows at all, nor tell whether any
is full screen, so there is nothing to find; macOS gives a full-screen program
a space of its own, which other windows do not come into.

New windows are only looked for while one in front is full screen; one found
is then followed for as long as it stays so, the user working on another
monitor meanwhile (the video goes on beside them).
"""

import logging
import os
import sys
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FullScreen:
    """A window shown full screen, and the monitor it covers."""

    window: int  # the system's id for it, to know it again
    app: str  # Windows: its program's file name ("vlc.exe"); X11: its WM_CLASS class
    monitor: tuple[int, int, int, int]  # x, y, width, height, in the system's pixels
    monitor_name: str = ""  # Windows: the display's device name, which Qt gives its screen


class FullScreenSource(Protocol):
    def find(self, known: Collection[int]) -> list[FullScreen]:
        """The windows full screen now: new ones while one in front is, and
        those known already (by their id) for as long as they stay so."""
        ...


def full_screen_source(
    platform_name: str, env: Mapping[str, str] = os.environ
) -> FullScreenSource | None:
    """What finds full-screen windows here, or None where nothing can.

    platform_name: Qt's ("windows", "xcb", "wayland", ...). Under a Wayland
    session the app runs through XWayland, which shows it only the windows of
    other X11 programs: most are Wayland ones, so it is not tried at all.
    """
    try:
        if sys.platform == "win32" and platform_name == "windows":
            from stickle.platform.windows.full_screen import WindowsFullScreen

            return WindowsFullScreen()
        if sys.platform == "linux" and platform_name == "xcb" and not _wayland_session(env):
            from stickle.platform.linux.x11 import X11FullScreen

            return X11FullScreen.connect()
    except OSError as error:
        log.warning("cannot follow full-screen windows: %s", type(error).__name__)
    return None


def _wayland_session(env: Mapping[str, str]) -> bool:
    return env.get("XDG_SESSION_TYPE") == "wayland" or bool(env.get("WAYLAND_DISPLAY"))
