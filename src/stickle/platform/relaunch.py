"""This program started again for a short job of its own (reaching the
credential store, fetching the version file): the libraries the job needs are
loaded in that process and leave memory with it, where a Python process
never gives memory back."""

import subprocess
import sys
from pathlib import Path

# What the program does when started with one of these, and only that (__main__).
CREDENTIAL_HELPER = "--credential-helper"  # stickle.platform.credential_helper
FETCH_FEED = "--fetch-feed"  # stickle.platform.feed


def own_command(*args: str) -> list[str]:
    """The built program itself (inside the AppImage's mount, which is open
    already: a tenth of the time of the AppImage), or Python running the source."""
    if "__compiled__" in globals():  # a Nuitka build: argv[0] is the program
        return [str(Path(sys.argv[0]).resolve()), *args]
    return [sys.executable, "-m", "stickle", *args]


def no_window() -> int:
    """Creation flags that keep a console window from showing (Python from
    source has one; the built program has none)."""
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW
    return 0
