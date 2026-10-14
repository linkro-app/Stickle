"""Where "Support Stickle" goes, and the app's own web pages.

Every copy of Stickle shows the same "Support Stickle" in the same places;
only where it leads differs. The copy from the Microsoft Store lets the user
choose an amount and pays through the Store's own dialog (see
support_dialog); any other copy opens the website's support page in the
browser. Stickle keeps nothing about it and never asks whether anyone paid
before, so the app cannot tell who supported it.
"""

import sys

from PySide6.QtCore import QLocale, QUrl

from stickle.app import note_window
from stickle.app.support_dialog import SupportDialog, store_purchaser

SITE = "https://stickle.linkro.co"
SOURCE = "https://github.com/linkro-app/Stickle"
PRIVACY = "https://github.com/linkro-app/Stickle/blob/main/PRIVACY.md"
LICENSE = "https://www.gnu.org/licenses/gpl-3.0.html"
SITE_LANGUAGES = ("en", "ko")


def in_store_package() -> bool:
    """True for the copy installed from the Microsoft Store."""
    if sys.platform == "win32":
        from stickle.platform.windows.package import is_packaged

        return is_packaged()
    return False


def site_language() -> str:
    """The website's language closest to the app's: Korean, or else English."""
    language = QLocale().name().split("_")[0]
    return language if language in SITE_LANGUAGES else SITE_LANGUAGES[0]


def site_page(page: str = "") -> str:
    return f"{SITE}/{site_language()}" + (f"/{page}" if page else "")


def support_url() -> QUrl:
    return QUrl(site_page("support"))


def open_support_website() -> bool:
    return note_window.open_url(support_url())


_dialog: list[SupportDialog] = []  # one, kept while the app runs


def open_support() -> bool:
    """The Store copy chooses an amount for the Store; any other opens the website."""
    if not in_store_package():
        return open_support_website()
    if not _dialog:
        _dialog.append(SupportDialog(store_purchaser(), open_support_website))
    _dialog[0].present()
    return True
