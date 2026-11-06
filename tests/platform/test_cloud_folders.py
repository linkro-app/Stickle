"""Telling where the cloud programs keep their folders, and whether Stickle's
data folder is inside one of them."""

import json
import sys
from pathlib import Path

import pytest

from stickle.platform.cloud_folders import (
    DROPBOX,
    GOOGLE_DRIVE,
    ICLOUD,
    ONEDRIVE,
    CloudFolder,
    cloud_folders,
    folder_holding,
)


def dropbox_info(at: Path, **accounts: str) -> None:
    at.mkdir(parents=True, exist_ok=True)
    (at / "info.json").write_text(
        json.dumps({kind: {"path": path, "host": 1} for kind, path in accounts.items()}),
        encoding="utf-8",
    )


def test_on_windows_onedrive_dropbox_google_drive_and_icloud(tmp_path: Path) -> None:
    profile = tmp_path / "me"
    roaming, local = profile / "AppData" / "Roaming", profile / "AppData" / "Local"
    dropbox_info(roaming / "Dropbox", personal=str(profile / "Dropbox"))
    dropbox_info(local / "Dropbox", business=str(profile / "Dropbox (Work)"))
    (local / "Google" / "DriveFS").mkdir(parents=True)
    (profile / "My Drive").mkdir()
    (profile / "iCloudDrive").mkdir()
    environ = {
        "OneDrive": str(profile / "OneDrive"),
        "OneDriveCommercial": str(profile / "OneDrive - Work"),
        "APPDATA": str(roaming),
        "LOCALAPPDATA": str(local),
        "USERPROFILE": str(profile),
    }
    assert cloud_folders(environ, home=profile, system="win32") == [
        CloudFolder(ONEDRIVE, profile / "OneDrive"),
        CloudFolder(ONEDRIVE, profile / "OneDrive - Work"),
        CloudFolder(DROPBOX, profile / "Dropbox"),
        CloudFolder(DROPBOX, profile / "Dropbox (Work)"),
        CloudFolder(GOOGLE_DRIVE, profile / "My Drive"),
        CloudFolder(ICLOUD, profile / "iCloudDrive"),
    ]


def test_on_windows_without_them_there_are_none(tmp_path: Path) -> None:
    # A folder named "My Drive" is Google Drive's only where Google Drive is set up.
    (tmp_path / "My Drive").mkdir()
    environ = {"USERPROFILE": str(tmp_path), "LOCALAPPDATA": str(tmp_path / "Local")}
    assert cloud_folders(environ, home=tmp_path, system="win32") == []


def test_a_damaged_dropbox_info_is_passed_over(tmp_path: Path) -> None:
    (tmp_path / ".dropbox").mkdir()
    (tmp_path / ".dropbox" / "info.json").write_text("{not json", encoding="utf-8")
    assert cloud_folders({}, home=tmp_path, system="linux") == []
    (tmp_path / ".dropbox" / "info.json").write_text('["a list"]', encoding="utf-8")
    assert cloud_folders({}, home=tmp_path, system="linux") == []


def test_on_linux_dropbox_and_the_onedrive_client(tmp_path: Path) -> None:
    dropbox_info(tmp_path / ".dropbox", personal=str(tmp_path / "Dropbox"))
    (tmp_path / ".config" / "onedrive").mkdir(parents=True)
    assert cloud_folders({}, home=tmp_path, system="linux") == [
        CloudFolder(DROPBOX, tmp_path / "Dropbox"),
        CloudFolder(ONEDRIVE, tmp_path / "OneDrive"),  # the client's own default
    ]
    (tmp_path / ".config" / "onedrive" / "config").write_text(
        '# set up by hand\nsync_dir = "~/Clouds/OneDrive"\n', encoding="utf-8"
    )
    assert cloud_folders({}, home=tmp_path, system="linux")[-1] == CloudFolder(
        ONEDRIVE, tmp_path / "Clouds" / "OneDrive"
    )


def test_on_macos_every_provider_under_cloud_storage_and_icloud_drive(tmp_path: Path) -> None:
    storage = tmp_path / "Library" / "CloudStorage"
    for name in ("Dropbox", "GoogleDrive-me@example.com", "OneDrive-Personal", "Other"):
        (storage / name).mkdir(parents=True)
    icloud = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    icloud.mkdir(parents=True)
    assert cloud_folders({}, home=tmp_path, system="darwin") == [
        CloudFolder(DROPBOX, storage / "Dropbox"),
        CloudFolder(GOOGLE_DRIVE, storage / "GoogleDrive-me@example.com"),
        CloudFolder(ONEDRIVE, storage / "OneDrive-Personal"),
        CloudFolder(ICLOUD, icloud),
    ]


def test_a_folder_inside_one_is_held_by_it_and_one_beside_it_is_not(tmp_path: Path) -> None:
    onedrive = CloudFolder(ONEDRIVE, tmp_path / "OneDrive")
    inside = tmp_path / "OneDrive" / "Desktop" / "Stickle" / "stickle-data"
    inside.mkdir(parents=True)
    beside = tmp_path / "OneDrive2" / "stickle-data"  # a name that only starts the same
    beside.mkdir(parents=True)
    assert folder_holding(inside, [onedrive], system="linux") == onedrive
    assert folder_holding(tmp_path / "OneDrive", [onedrive], system="linux") == onedrive
    assert folder_holding(beside, [onedrive], system="linux") is None
    assert folder_holding(tmp_path, [onedrive], system="linux") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows names letter case alike")
def test_on_windows_letter_case_does_not_matter(tmp_path: Path) -> None:
    onedrive = CloudFolder(ONEDRIVE, tmp_path / "OneDrive")
    (tmp_path / "OneDrive" / "stickle-data").mkdir(parents=True)
    upper = Path(str(tmp_path / "ONEDRIVE" / "stickle-data"))
    assert folder_holding(upper, [onedrive], system="win32") == onedrive


def test_on_windows_a_drive_labelled_google_drive_is_held(tmp_path: Path) -> None:
    folder = tmp_path / "stickle-data"
    held = folder_holding(folder, [], system="win32", volume_label=lambda _: "Google Drive")
    assert held is not None and held.name == GOOGLE_DRIVE
    assert folder_holding(folder, [], system="win32", volume_label=lambda _: "Data") is None
    assert folder_holding(folder, [], system="win32", volume_label=lambda _: None) is None
