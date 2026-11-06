"""Stickle's data folder inside a cloud program's folder: told once for that place,
at the top of Stickle Manager until the user says OK, and with that window closed
in one notification of the system. Nothing is stopped."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from pytestqt.qtbot import QtBot

from stickle.app import cloud_folder_notice
from stickle.app.application import tell_cloud_folder
from stickle.app.cloud_folder_notice import CloudFolderLine, InCloudFolder, in_cloud_folder
from stickle.app.i18n import Translations
from stickle.app.notes import NoteManager
from stickle.app.stickle_window import StickleWindow
from stickle.app.tray import Tray
from stickle.data.schema import open_store
from stickle.data.settings import CLOUD_FOLDER_SEEN, CLOUD_FOLDER_TOLD, Settings
from stickle.platform.cloud_folders import DROPBOX, ONEDRIVE, CloudFolder

KEY = secrets.token_bytes(32)


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield Settings(connection)
    connection.close()


@pytest.fixture
def onedrive(tmp_path: Path) -> CloudFolder:
    (tmp_path / "OneDrive" / "Desktop" / "Stickle" / "stickle-data").mkdir(parents=True)
    return CloudFolder(ONEDRIVE, tmp_path / "OneDrive")


def portable_in(cloud: CloudFolder) -> InCloudFolder:
    found = in_cloud_folder(cloud.path / "Desktop" / "Stickle" / "stickle-data", True, cloud)
    assert found is not None
    return found


def test_what_to_move_is_the_folder_stickle_is_in_when_portable(onedrive: CloudFolder) -> None:
    data = onedrive.path / "Desktop" / "Stickle" / "stickle-data"
    assert in_cloud_folder(data, True, None) is None  # in no cloud folder
    portable = in_cloud_folder(data, True, onedrive)
    chosen = in_cloud_folder(data, False, onedrive)  # STICKLE_DATA_DIR set there
    assert portable is not None and chosen is not None
    assert portable.to_move == data.parent and chosen.to_move == data
    assert portable.place == chosen.place  # the place, whatever kind of start
    # Kept as a hash: no path in the settings.
    assert len(portable.place) == 64 and "stickle-data" not in portable.place
    elsewhere = in_cloud_folder(onedrive.path, True, onedrive)
    in_dropbox = in_cloud_folder(data, True, CloudFolder(DROPBOX, onedrive.path))
    assert elsewhere is not None and in_dropbox is not None
    assert len({portable.place, elsewhere.place, in_dropbox.place}) == 3


def test_the_line_names_the_program_and_says_what_to_move(
    qtbot: QtBot, settings: Settings, onedrive: CloudFolder
) -> None:
    line = CloudFolderLine(settings)
    qtbot.addWidget(line)
    assert not line.isVisibleTo(line.parentWidget() or line) and line.text() == ""

    assert line.tell(portable_in(onedrive))
    text = line.label.text()
    assert "inside your OneDrive folder" in text and "If the OneDrive app uploads" in text
    assert "move the whole folder Stickle is in out of your OneDrive folder" in text
    found = in_cloud_folder(onedrive.path / "notes", False, onedrive)
    assert found is not None and line.tell(found)
    assert "Move the data folder out of your OneDrive folder." in line.label.text()
    assert line.open_button.text() == "Open the &folder" and line.ok_button.text() == "OK"


def test_open_the_folder_opens_the_one_to_move(
    qtbot: QtBot, settings: Settings, onedrive: CloudFolder, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []

    def open_url(url: QUrl) -> bool:
        opened.append(url.toLocalFile())
        return True

    monkeypatch.setattr(cloud_folder_notice.QDesktopServices, "openUrl", open_url)
    line = CloudFolderLine(settings)
    qtbot.addWidget(line)
    found = portable_in(onedrive)
    line.tell(found)
    line.open_button.click()
    assert [Path(p) for p in opened] == [found.to_move]
    assert QUrl.fromLocalFile(str(found.to_move)).isLocalFile()


def test_ok_puts_it_away_for_that_place_only(
    qtbot: QtBot, settings: Settings, onedrive: CloudFolder
) -> None:
    line = CloudFolderLine(settings)
    qtbot.addWidget(line)
    found = portable_in(onedrive)
    line.tell(found)
    line.ok_button.click()
    assert line.isHidden() and settings.get(CLOUD_FOLDER_SEEN) == found.place

    again = CloudFolderLine(settings)  # the next start
    qtbot.addWidget(again)
    assert not again.tell(found) and again.isHidden()
    moved = in_cloud_folder(onedrive.path, True, onedrive)  # another place in it
    assert moved is not None and again.tell(moved)


def window_with(settings: Settings) -> StickleWindow:
    return StickleWindow(NoteManager(), Translations(), lambda: None, settings=settings)


def close(window: StickleWindow) -> None:
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("open_window", [True, False])
def test_at_start_it_is_at_the_top_and_with_the_window_closed_the_system_tells_it_once(
    qtbot: QtBot,
    settings: Settings,
    onedrive: CloudFolder,
    monkeypatch: pytest.MonkeyPatch,
    open_window: bool,
) -> None:
    shown: list[str] = []
    window = window_with(settings)
    tray = Tray(lambda: None, lambda: None, Translations())

    def show_message(_title: str, text: str) -> None:
        shown.append(text)

    monkeypatch.setattr(tray, "showMessage", show_message)
    found = portable_in(onedrive)
    try:
        if open_window:
            window.open()
        tell_cloud_folder(found, window, tray, settings)
        assert window.cloud_folder_line.isVisibleTo(window)
        assert settings.get(CLOUD_FOLDER_TOLD) == found.place
        tell_cloud_folder(found, window, tray, settings)  # the next start: no more notifications

        assert len(shown) == (0 if open_window else 1)
        assert all("OneDrive" in text for text in shown)
        window.cloud_folder_line.ok_button.click()
        tell_cloud_folder(found, window, tray, settings)
        assert not window.cloud_folder_line.isVisibleTo(window)
    finally:
        tray.hide()
        close(window)
