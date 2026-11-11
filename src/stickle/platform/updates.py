"""Whether this Stickle is kept up to date by what installed it.

The Microsoft Store and Flatpak update their apps themselves, so those
copies never ask the website (and offer nothing about updates).
"""

import os
import sys
from collections.abc import Mapping

STORE = "store"  # the Microsoft Store
SOFTWARE_CENTER = "software center"  # Flatpak's: wherever the user installed it from


def kept_by(env: Mapping[str, str] | None = None) -> str | None:
    """What keeps this Stickle up to date, if not its user: STORE or SOFTWARE_CENTER."""
    env = os.environ if env is None else env
    if env.get("FLATPAK_ID"):
        return SOFTWARE_CENTER
    if sys.platform == "win32":
        from stickle.platform.windows.package import is_packaged

        if is_packaged():
            return STORE
    return None


def updated_by_its_store(env: Mapping[str, str] | None = None) -> bool:
    return kept_by(env) is not None
