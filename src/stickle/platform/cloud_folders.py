"""The folders that cloud programs on this computer keep in step with the cloud.

Read only, to tell the user when Stickle's own data folder is inside one: the
program may upload or replace the database or a key file while Stickle is
writing it, which can damage the notes or leave copies of them beside it.
Stickle syncs by itself, never through these folders.

Where each program keeps its folder:
- OneDrive: the OneDrive, OneDriveConsumer and OneDriveCommercial variables it
  sets on Windows; on Linux the onedrive client's sync_dir (~/OneDrive unless set).
- Dropbox: the paths in its info.json (%APPDATA% or %LOCALAPPDATA% on Windows,
  ~/.dropbox elsewhere).
- Google Drive: on Windows a drive of its own labelled "Google Drive", or the
  "My Drive" folder it mirrors into.
- iCloud Drive: %USERPROFILE%\\iCloudDrive on Windows.
- On macOS, every provider's folder under ~/Library/CloudStorage, and iCloud
  Drive's under ~/Library/Mobile Documents.
"""

import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import cast

ONEDRIVE = "OneDrive"
DROPBOX = "Dropbox"
GOOGLE_DRIVE = "Google Drive"
ICLOUD = "iCloud Drive"
GOOGLE_DRIVE_LABEL = "Google Drive"  # the drive Google Drive for desktop streams into


@dataclass(frozen=True)
class CloudFolder:
    name: str  # as the user knows the program
    path: Path


def _dropbox(info: Path) -> list[CloudFolder]:
    """The folders of the accounts in Dropbox's info.json, if it is there."""
    try:
        accounts: object = json.loads(info.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return []
    if not isinstance(accounts, dict):
        return []
    found: list[CloudFolder] = []
    for account in cast(dict[str, object], accounts).values():  # "personal", "business"
        if not isinstance(account, dict):
            continue
        path = cast(dict[str, object], account).get("path")
        if isinstance(path, str) and path:
            found.append(CloudFolder(DROPBOX, Path(path)))
    return found


SYNC_DIR = re.compile(r"""^\s*sync_dir\s*=\s*["']?([^"'\n]+?)["']?\s*$""", re.MULTILINE)


def _onedrive_linux(home: Path) -> list[CloudFolder]:
    """The onedrive client's folder, where it is set up (its configuration folder exists)."""
    config = home / ".config" / "onedrive"
    if not config.is_dir():
        return []
    try:
        match = SYNC_DIR.search((config / "config").read_text(encoding="utf-8"))
    except OSError:
        match = None
    folder = match.group(1).strip() if match else "~/OneDrive"
    if folder.startswith("~"):
        folder = str(home) + folder[1:]
    return [CloudFolder(ONEDRIVE, Path(folder))]


def _macos(home: Path) -> list[CloudFolder]:
    found: list[CloudFolder] = []
    storage = home / "Library" / "CloudStorage"
    names = ((ONEDRIVE, "OneDrive"), (DROPBOX, "Dropbox"), (GOOGLE_DRIVE, "GoogleDrive"))
    try:
        entries = sorted(storage.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        for name, start in names:
            if entry.name.startswith(start):
                found.append(CloudFolder(name, entry))
    icloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    if icloud.is_dir():
        found.append(CloudFolder(ICLOUD, icloud))
    return found


def cloud_folders(
    environ: Mapping[str, str] = os.environ,
    home: Path | None = None,
    system: str = sys.platform,
) -> list[CloudFolder]:
    """The cloud programs' folders on this computer, as far as they can be told."""
    home = home if home is not None else Path.home()
    if system == "win32":
        found = [
            CloudFolder(ONEDRIVE, Path(environ[name]))
            for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
            if environ.get(name)
        ]
        for base in ("APPDATA", "LOCALAPPDATA"):
            if environ.get(base):
                found += _dropbox(Path(environ[base]) / "Dropbox" / "info.json")
        profile = Path(environ["USERPROFILE"]) if environ.get("USERPROFILE") else home
        local = environ.get("LOCALAPPDATA")
        mirrored = profile / "My Drive"
        if local and (Path(local) / "Google" / "DriveFS").is_dir() and mirrored.is_dir():
            found.append(CloudFolder(GOOGLE_DRIVE, mirrored))
        if (profile / "iCloudDrive").is_dir():
            found.append(CloudFolder(ICLOUD, profile / "iCloudDrive"))
        return found
    found = _dropbox(home / ".dropbox" / "info.json")
    if system == "darwin":
        return found + _macos(home)
    return found + _onedrive_linux(home)


def _key(path: PurePath, system: str) -> str:
    """For comparing: Windows and macOS do not tell letter case apart in names."""
    text = os.path.normpath(str(path))
    return text.casefold() if system in ("win32", "darwin") else text


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _volume_label(folder: Path) -> str | None:
    """The label of the Windows drive the folder is on, None if it cannot be read."""
    if sys.platform != "win32":
        return None
    import ctypes

    root = folder.anchor
    if not root:
        return None
    label = ctypes.create_unicode_buffer(261)
    if not ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), label, len(label), None, None, None, None, 0
    ):
        return None
    return label.value


def folder_holding(
    folder: Path,
    clouds: Iterable[CloudFolder],
    system: str = sys.platform,
    volume_label: Callable[[Path], str | None] = _volume_label,
) -> CloudFolder | None:
    """The cloud folder that folder is in (or is), None if it is in none."""
    inside = _resolved(folder)
    key = _key(inside, system)
    for cloud in clouds:
        top = _key(_resolved(cloud.path), system)
        if key == top or key.startswith(top.rstrip("\\/") + os.sep):
            return cloud
    if system == "win32" and volume_label(inside) == GOOGLE_DRIVE_LABEL:
        return CloudFolder(GOOGLE_DRIVE, Path(inside.anchor))
    return None
