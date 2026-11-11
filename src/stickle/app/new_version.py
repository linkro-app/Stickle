"""Where getting a newer Stickle leads, by what installed this copy: the
Store's page of Stickle for the Store's copy; nowhere where the software
center keeps it up to date (Flatpak); else the release the daily check found,
or the website, which links to every download.

Only places fixed here or checked by the update check are opened."""

from __future__ import annotations

from typing import TYPE_CHECKING

from stickle.app.support import site_page
from stickle.platform.updates import STORE, kept_by

if TYPE_CHECKING:
    from stickle.app.update_check import UpdateCheck

STORE_PAGE = "ms-windows-store://pdp/?productid=9MZ6BHN3JKFR"


def new_version_page(updates: UpdateCheck | None) -> str | None:
    """None: the software center updates it, and nothing here leads there."""
    kept = kept_by()
    if kept == STORE:
        return STORE_PAGE
    if kept is not None:
        return None
    release = None if updates is None else updates.release
    return site_page() if release is None else release.page
