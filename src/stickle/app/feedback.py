"""Reporting a problem or an idea: a GitHub issue drafted in the browser.

Nothing is sent by Stickle. It opens the project's new-issue page with a
draft the user reads, changes and submits (or not) themselves. The draft
says how this copy runs, which is what reports most often lack: the version,
how it was installed, the system and desktop, the input method and the
screens. It holds no note text, no paths and nothing that tells the user
apart; the log file is only named, for the user to attach if they like.
"""

import os
import platform
import sys
from collections.abc import Mapping
from urllib.parse import urlencode

from PySide6.QtCore import QLocale, QUrl, qVersion
from PySide6.QtGui import QGuiApplication

from stickle import __version__
from stickle.app import note_window
from stickle.app.support import SOURCE
from stickle.logs import redact

ISSUE_URL = f"{SOURCE}/issues/new"


def how_installed(portable: bool, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    if portable:
        return "portable"
    if env.get("FLATPAK_ID"):
        return "Flatpak"
    if env.get("APPIMAGE"):
        return "AppImage"
    if sys.platform == "win32":
        from stickle.platform.windows.package import is_packaged

        if is_packaged():
            return "Microsoft Store"
    if "__compiled__" in globals():
        return "zip" if sys.platform == "win32" else "build"
    return "source"


def log_place(portable: bool) -> str:
    """Where the log is, written without the user's own folder names."""
    if portable:
        return "stickle-data/logs/stickle.log, next to the program"
    if sys.platform == "win32":
        return r"%APPDATA%\Stickle\logs\stickle.log"
    if sys.platform == "darwin":
        return "~/Library/Application Support/Stickle/logs/stickle.log"
    return "~/.local/share/stickle/logs/stickle.log"


def this_copy(portable: bool, env: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    lines = [
        f"Stickle {__version__} ({how_installed(portable, env)})",
        f"System: {platform.system()} {platform.release()} {platform.machine()}",
        f"Qt {qVersion()} ({QGuiApplication.platformName()}), Python {platform.python_version()}",
        f"Language: {QLocale().name()}",
    ]
    if sys.platform.startswith("linux"):
        desktop = env.get("XDG_CURRENT_DESKTOP", "?")
        session = env.get("XDG_SESSION_TYPE", "?")
        lines.append(f"Desktop: {desktop} ({session})")
        im = env.get("QT_IM_MODULE") or env.get("XMODIFIERS", "").removeprefix("@im=") or "?"
        lines.append(f"Input method: {im}")
    scales = sorted({f"{screen.devicePixelRatio():g}" for screen in QGuiApplication.screens()})
    lines.append(f"Screens: {len(QGuiApplication.screens())}, scale {', '.join(scales)}")
    return [redact(line) for line in lines]


def draft(portable: bool, env: Mapping[str, str] | None = None) -> str:
    """The issue's text: headings to fill in, then how this copy runs."""
    about = "\n".join(this_copy(portable, env))
    return (
        "**What happened, or what would you like?**\n\n\n\n"
        "**Steps, if it is a problem**\n\n1. \n2. \n\n"
        "**What did you expect?**\n\n\n\n"
        "---\n"
        f"If you can, attach the log file ({log_place(portable)}): it holds no note text "
        "or folder paths.\n\n"
        f"```\n{about}\n```\n"
    )


def issue_url(portable: bool, env: Mapping[str, str] | None = None) -> QUrl:
    return QUrl(f"{ISSUE_URL}?{urlencode({'body': draft(portable, env)})}")


def open_feedback(portable: bool) -> bool:
    return note_window.open_url(issue_url(portable))
