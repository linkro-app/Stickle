"""Whether this Stickle is kept up to date by what installed it.

The Microsoft Store and Flatpak update their apps themselves, so those
copies never ask the website (and offer nothing about updates).
"""

import os
import sys
from collections.abc import Mapping


def updated_by_its_store(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    if env.get("FLATPAK_ID"):
        return True
    if sys.platform == "win32":
        from stickle.platform.windows.package import is_packaged

        return is_packaged()
    return False
