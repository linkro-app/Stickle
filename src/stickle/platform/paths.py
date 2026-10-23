"""Where the app keeps its data on each operating system.

Portable: a folder named stickle-data next to the program (stickle.exe, or
the AppImage file) holds everything instead, so Stickle can be carried on a
USB stick. Making that folder is how it is asked for.
"""

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PORTABLE_FOLDER = "stickle-data"


@dataclass(frozen=True)
class DataPlace:
    folder: Path
    portable: bool = False  # next to the program, carried from computer to computer


def program_folder(env: Mapping[str, str] | None = None) -> Path | None:
    """The folder the program was started from; None when run from source."""
    env = os.environ if env is None else env
    if appimage := env.get("APPIMAGE"):
        # The AppImage file's own folder, not the one its files are served from.
        return Path(appimage).parent
    if "__compiled__" in globals():  # a Nuitka build: argv[0] is the program
        return Path(sys.argv[0]).resolve().parent
    return None


def own_files_folder() -> Path | None:
    """The folder the program's own files are in: next to stickle.exe, or inside
    the AppImage or the app bundle; None when run from source."""
    if "__compiled__" in globals():  # a Nuitka build: argv[0] is the program
        return Path(sys.argv[0]).resolve().parent
    return None


def data_place(
    env: Mapping[str, str] | None = None,
    platform: str = sys.platform,
    home: Path | None = None,
    program: Path | None = None,
) -> DataPlace:
    """Where the notes are: STICKLE_DATA_DIR (tests, development), else a
    stickle-data folder next to the program (portable), else the usual place.
    program: the program's folder (by default, found as program_folder does)."""
    env = os.environ if env is None else env
    if override := env.get("STICKLE_DATA_DIR"):
        return DataPlace(Path(override))
    program = program_folder(env) if program is None else program
    if program is not None and (program / PORTABLE_FOLDER).is_dir():
        return DataPlace(program / PORTABLE_FOLDER, portable=True)
    return DataPlace(data_dir(env, platform, home))


def data_dir(
    env: Mapping[str, str] | None = None,
    platform: str = sys.platform,
    home: Path | None = None,
) -> Path:
    """The per-user data folder (not created here). STICKLE_DATA_DIR replaces it."""
    env = os.environ if env is None else env
    if override := env.get("STICKLE_DATA_DIR"):
        return Path(override)
    home = Path.home() if home is None else home
    if platform == "win32":
        base = env.get("APPDATA")
        return (Path(base) if base else home / "AppData" / "Roaming") / "Stickle"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Stickle"
    base = env.get("XDG_DATA_HOME")
    return (Path(base) if base else home / ".local" / "share") / "stickle"


def ensure_private_dir(path: Path) -> list[str]:
    """Create the folder readable only by this user; return warnings, if any.

    Linux and macOS home folders can be readable by other local users, so the
    folder is set to 700. On Windows the per-user profile already grants access
    only to the user, SYSTEM and administrators, and the folder inherits that;
    rewriting it would fight backup and roaming tools, so it is only checked.
    """
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        from stickle.platform.windows.permissions import other_readers

        readers = other_readers(path)
        return [f"other accounts can read the data folder: {', '.join(readers)}"] if readers else []
    try:
        path.chmod(0o700)
    except OSError as error:  # a USB stick's FAT file system has no such permissions
        return [f"the data folder's permissions cannot be set: {type(error).__name__}"]
    return []


def writable(folder: Path) -> bool:
    """Whether files can be made in folder (a read-only stick, a protected folder)."""
    probe = folder / ".stickle-write-test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        return False
    return True
