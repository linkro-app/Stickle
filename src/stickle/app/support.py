"""Where "Support Stickle" goes, and the app's own web pages.

Every copy of Stickle shows the same "Support Stickle" in the same places;
only where it leads differs. The copy from the Microsoft Store opens the
Store's page for a support item, which pays through the Store; any other
copy opens the website's support page in the browser. Stickle only opens
the page: it keeps nothing about it and never asks whether anyone paid, so
the app cannot tell who supported it.
"""

import sys

from PySide6.QtCore import QLocale, QUrl

from stickle.app import note_window

SITE = "https://stickle.linkro.co"
SOURCE = "https://github.com/linkro-app/Stickle"
PRIVACY = "https://github.com/linkro-app/Stickle/blob/main/PRIVACY.md"
LICENSE = "https://www.gnu.org/licenses/gpl-3.0.html"
SITE_LANGUAGES = ("en", "ko")
# The support item in the Microsoft Store (an add-on of the app), once it exists.
STORE_SUPPORT_ID: str | None = None


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
    if STORE_SUPPORT_ID and in_store_package():
        return QUrl(f"ms-windows-store://pdp/?productid={STORE_SUPPORT_ID}")
    return QUrl(site_page("support"))


def open_support() -> bool:
    return note_window.open_url(support_url())
