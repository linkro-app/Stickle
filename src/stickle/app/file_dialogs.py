"""Asking which file to open, where to save one, or which folder to use, in the
system's own dialog.

Not through Qt's QFileDialog where it can be helped: each time one closes, it
records the last folder and the folders visited in the system's settings store
(the registry on Windows), which Stickle does not write to. Windows: the
system's dialog asked for directly (stickle.platform.windows.file_dialogs).
Linux: the desktop's, through its portal (stickle.platform.linux.portal_files);
where there is none, Qt's own dialog, which keeps those in a file of Qt's, and
never the GTK one Qt may pick (GTK keeps its own in GSettings). macOS: Qt's for
now.
"""

import logging
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFileDialog, QWidget

log = logging.getLogger(__name__)

type Kind = Literal["open", "save", "folder"]
type Answer = Path | Literal["unavailable"] | None

# The windows a system dialog is open for just now, the latest last.
_asking_for: list[QWidget] = []


def asking_for() -> QWidget | None:
    """The window a system dialog is open for, if one is: modal to it, though
    it has no Qt window of its own."""
    return _asking_for[-1] if _asking_for else None


def file_types(filters: str) -> list[tuple[str, str]]:
    """Qt's "Text files (*.txt);;Notes (*.md *.json)" as the system's
    ("Text files (*.txt)", "*.txt") pairs."""
    types: list[tuple[str, str]] = []
    for entry in (part.strip() for part in filters.split(";;")):
        if not entry:
            continue
        found = re.search(r"\(([^()]*)\)\s*$", entry)
        patterns = (found.group(1) if found else entry).split()
        types.append((entry, ";".join(patterns)))
    return types


def default_extension(filters: str) -> str:
    """The extension a name typed without one gets: the first type's ("txt")."""
    types = file_types(filters)
    found = re.fullmatch(r"\*\.(\w+)", types[0][1].split(";")[0]) if types else None
    return found.group(1) if found else ""


def _from_the_system(
    parent: QWidget, kind: Kind, title: str, suggested: Path | None, filters: str
) -> Answer:
    window = parent.window()
    if sys.platform == "win32":
        from stickle.platform.windows.file_dialogs import choose as choose_on_windows

        handle = int(window.winId()) if window.isVisible() else 0
        try:
            return choose_on_windows(
                kind, title, handle, suggested, file_types(filters), default_extension(filters)
            )
        except OSError as error:
            log.warning("the system's file dialog could not be shown: %s", error)
            return None  # not Qt's instead: that one writes to the registry
    if not sys.platform.startswith("linux"):
        return "unavailable"
    from stickle.platform.linux.portal_files import PortalUnavailableError, choose

    x11 = QGuiApplication.platformName() == "xcb"
    handle = f"x11:{int(window.winId()):x}" if x11 and window.isVisible() else ""
    try:
        return choose(kind, title, handle, suggested)
    except PortalUnavailableError as error:
        log.info("the desktop's file dialog is not available: %s", error)
        return "unavailable"


# The system's dialog: a chosen path, None if cancelled, or "unavailable".
from_the_system: Callable[[QWidget, Kind, str, Path | None, str], Answer] = (
    _from_the_system  # replaced in tests
)


def _ask(
    parent: QWidget, kind: Kind, title: str, suggested: Path | None, filters: str
) -> Path | None:
    _asking_for.append(parent)
    try:
        chosen = from_the_system(parent, kind, title, suggested, filters)
        if chosen == "unavailable":
            return _from_qt(parent, kind, title, suggested, filters)
        return chosen
    finally:
        _asking_for.pop()


def _from_qt(
    parent: QWidget, kind: Kind, title: str, suggested: Path | None, filters: str
) -> Path | None:
    own = QFileDialog.Option.DontUseNativeDialog
    options = own if sys.platform.startswith("linux") else QFileDialog.Option(0)
    start = str(suggested) if suggested is not None else ""
    if kind == "save":
        name, _ = QFileDialog.getSaveFileName(parent, title, start, filters, options=options)
    elif kind == "open":
        name, _ = QFileDialog.getOpenFileName(parent, title, start, filters, options=options)
    else:
        name = QFileDialog.getExistingDirectory(
            parent, title, start, options=options | QFileDialog.Option.ShowDirsOnly
        )
    return Path(name) if name else None


def save_file_name(parent: QWidget, title: str, suggested: Path, filters: str) -> Path | None:
    """Where to save a file, suggested as its folder and name; None if cancelled."""
    return _ask(parent, "save", title, suggested, filters)


def open_file_name(parent: QWidget, title: str) -> Path | None:
    """A file to read, any kind; None if cancelled."""
    return _ask(parent, "open", title, None, "")


def existing_folder(parent: QWidget, title: str) -> Path | None:
    """A folder to use; None if cancelled."""
    return _ask(parent, "folder", title, None, "")
