"""Reporting a problem or an idea: a draft issue, with nothing personal in it."""

import getpass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from PySide6.QtCore import QUrl
from pytestqt.qtbot import QtBot

from stickle import __version__
from stickle.app import note_window
from stickle.app.about_dialog import AboutDialog
from stickle.app.feedback import draft, how_installed, issue_url

LINUX = {
    "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
    "XDG_SESSION_TYPE": "wayland",
    "XMODIFIERS": "@im=fcitx",
}


def test_the_draft_says_how_this_copy_runs(qtbot: QtBot) -> None:
    del qtbot
    text = draft(portable=False, env=LINUX)

    assert f"Stickle {__version__}" in text
    assert "System:" in text and "Screens:" in text
    assert "What happened" in text
    assert "stickle.log" in text


def test_the_draft_holds_nothing_personal(qtbot: QtBot) -> None:
    del qtbot
    text = draft(portable=False, env=LINUX)

    assert str(Path.home()) not in text
    assert getpass.getuser() not in text.replace("Stickle", "")


@pytest.mark.parametrize(
    ("portable", "env", "expected"),
    [
        (True, {"APPIMAGE": "/x/Stickle.AppImage"}, "portable"),
        (False, {"FLATPAK_ID": "co.linkro.stickle"}, "Flatpak"),
        (False, {"APPIMAGE": "/x/Stickle.AppImage"}, "AppImage"),
    ],
)
def test_how_it_was_installed(portable: bool, env: dict[str, str], expected: str) -> None:
    assert how_installed(portable, env) == expected


def test_it_opens_a_new_issue_on_the_projects_github(qtbot: QtBot) -> None:
    del qtbot
    url = urlsplit(issue_url(portable=False, env=LINUX).toString())

    assert (url.scheme, url.netloc, url.path) == (
        "https",
        "github.com",
        "/linkro-app/Stickle/issues/new",
    )
    (body,) = parse_qs(url.query)["body"]
    assert f"Stickle {__version__}" in body


def test_about_drafts_it_in_the_browser(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[QUrl] = []

    def open_url(url: QUrl) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(note_window, "open_url", open_url)
    about = AboutDialog()
    qtbot.addWidget(about)

    about.feedback_button.click()

    assert len(opened) == 1
    assert opened[0].host() == "github.com"
