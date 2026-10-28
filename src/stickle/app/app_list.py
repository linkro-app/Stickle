"""Offering to put the AppImage in the application list, once, and switching it."""

import logging

from PySide6.QtCore import QBuffer, QByteArray, QCoreApplication, QIODevice
from PySide6.QtWidgets import QMessageBox, QWidget

from stickle.data.settings import APP_MENU_ASKED, Settings
from stickle.platform.linux.appimage import AppMenuEntry

log = logging.getLogger(__name__)
ICON_PIXELS = 256


def icon_png() -> bytes:
    """The app icon as a PNG, for the application list."""
    from stickle.app.app_icon import make_icon

    pixmap = make_icon().pixmap(ICON_PIXELS, ICON_PIXELS)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    return bytes(data.data())


def switch_app_list(entry: AppMenuEntry, on: bool) -> bool:
    """Add Stickle to the application list or take it out; False (logged) if that failed."""
    try:
        if on:
            entry.add()
        else:
            entry.remove()
    except OSError as error:
        log.error("could not change the application list: %s", type(error).__name__)
        return False
    return True


def offer_app_list(entry: AppMenuEntry, settings: Settings, parent: QWidget | None = None) -> None:
    """Ask once, the first time this AppImage runs, whether to add it to the list."""
    if settings.get(APP_MENU_ASKED) or entry.added:
        return
    answer = QMessageBox.question(
        parent,
        "Stickle",
        QCoreApplication.translate(
            "AppList",
            "Add Stickle to your list of applications, so you can start it like any "
            "other program? You can change this later in the settings.",
        ),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes,
    )
    # Whatever the answer, it is not asked again.
    settings.set(APP_MENU_ASKED, True)
    if answer == QMessageBox.StandardButton.Yes:
        switch_app_list(entry, True)
