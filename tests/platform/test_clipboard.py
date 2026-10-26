"""A secret copied is marked for clipboard histories to leave out."""

import sys

import pytest
from PySide6.QtGui import QGuiApplication
from pytestqt.qtbot import QtBot

from stickle.app.recovery_key_dialog import RecoveryKeyDialog
from stickle.crypto.recovery import generate
from stickle.platform.clipboard import (
    KDE_HINT,
    WINDOWS_FORMATS,
    copy_secret,
    secret_text,
    take_back_secret,
)

KEY = "K7QM-2HXP-9RTV-4WJD-8FNB-3YCS-6GKA-M2Q0"


def test_on_windows_out_of_the_history_and_the_cloud_clipboard() -> None:
    data = secret_text(KEY, "win32")

    assert data.text() == KEY
    for name in WINDOWS_FORMATS:
        assert data.hasFormat(name)
        assert bytes(data.data(name).data()) == bytes(4)  # "no", as a DWORD
    assert not data.hasFormat(KDE_HINT)


def test_on_linux_a_password_managers_hint() -> None:
    data = secret_text(KEY, "linux")

    assert data.text() == KEY
    assert bytes(data.data(KDE_HINT).data()) == b"secret"
    assert not any(data.hasFormat(name) for name in WINDOWS_FORMATS)


@pytest.mark.parametrize("platform", ["darwin", "freebsd14"])
def test_elsewhere_the_text_alone(platform: str) -> None:
    data = secret_text(KEY, platform)

    assert data.text() == KEY
    assert data.formats() == ["text/plain"]


def test_the_recovery_key_is_copied_so(qtbot: QtBot) -> None:
    recovery_key = generate()
    dialog = RecoveryKeyDialog(recovery_key)
    qtbot.addWidget(dialog)

    dialog.panel.copy_button.click()

    copied = QGuiApplication.clipboard().mimeData()
    assert copied is not None and copied.text() == recovery_key
    marks = WINDOWS_FORMATS if sys.platform == "win32" else (KDE_HINT,)
    if sys.platform == "win32" or sys.platform.startswith("linux"):
        assert all(copied.hasFormat(name) for name in marks)


def test_when_stickle_ends_a_secret_still_copied_is_taken_back(qtbot: QtBot) -> None:
    copy_secret(KEY)
    assert QGuiApplication.clipboard().text() == KEY

    take_back_secret()

    assert QGuiApplication.clipboard().text() == ""


def test_anything_copied_since_stays(qtbot: QtBot) -> None:
    copy_secret(KEY)
    QGuiApplication.clipboard().setText("a note's line")  # copied afterwards

    take_back_secret()

    assert QGuiApplication.clipboard().text() == "a note's line"
