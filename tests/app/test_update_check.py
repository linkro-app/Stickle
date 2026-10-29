"""The daily check for a newer Stickle, and how a newer one is shown."""

import base64
import io
import json
import secrets
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from PySide6.QtWidgets import QLabel
from pytestqt.qtbot import QtBot

from stickle.app import note_window
from stickle.app.about_dialog import AboutDialog
from stickle.app.i18n import Translations
from stickle.app.notes import NoteManager
from stickle.app.settings_window import SettingsWindow
from stickle.app.stickle_window import StickleWindow
from stickle.app.tray import Tray
from stickle.app.update_check import UpdateCheck
from stickle.core.updates import FEED_URL
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import UPDATE_CHECK, UPDATE_CHECKED_AT, Settings
from stickle.platform import feed
from stickle.platform.feed import fetch_feed, fetch_feed_in_helper

KEY = secrets.token_bytes(32)
PAGE = "https://github.com/linkro-app/Stickle/releases/tag/v0.5.0-preview.1"
NEWER = json.dumps({"format": 1, "latest": "0.5.0", "page": PAGE}).encode()
SAME = json.dumps({"format": 1, "latest": "0.4.0", "page": PAGE}).encode()


class FakeClock:
    def __init__(self, now: str = "2026-10-18T12:00:00.000Z") -> None:
        self.now = now

    def __call__(self) -> str:
        return self.now


class Server:
    """Answers the check, counting how often it was asked."""

    def __init__(self, answer: bytes | None) -> None:
        self.answer = answer
        self.asked = 0

    def __call__(self) -> bytes | None:
        self.asked += 1
        return self.answer


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield Settings(connection)
    connection.close()


def checker(settings: Settings, server: Server, clock: FakeClock) -> UpdateCheck:
    return UpdateCheck(settings, current="0.4.0", clock=clock, fetch=server)


def test_a_newer_version_is_found(qtbot: QtBot, settings: Settings) -> None:
    server = Server(NEWER)
    check = checker(settings, server, FakeClock())

    with qtbot.waitSignal(check.found, timeout=3000):
        assert check.check_if_due()

    assert check.release is not None and check.release.version == "0.5.0"
    assert server.asked == 1


def test_it_asks_once_a_day_also_across_restarts(qtbot: QtBot, settings: Settings) -> None:
    server = Server(SAME)
    clock = FakeClock("2026-10-18T12:00:00.000Z")

    assert checker(settings, server, clock).check_if_due()
    qtbot.waitUntil(lambda: server.asked == 1)
    clock.now = "2026-10-19T11:59:00.000Z"
    assert not checker(settings, server, clock).check_if_due()  # a restart, same day
    clock.now = "2026-10-19T12:00:00.000Z"
    assert checker(settings, server, clock).check_if_due()
    qtbot.waitUntil(lambda: server.asked == 2)


def test_a_failure_waits_for_the_next_day_too(qtbot: QtBot, settings: Settings) -> None:
    server = Server(None)
    clock = FakeClock()
    check = checker(settings, server, clock)

    assert check.check_if_due()
    qtbot.waitUntil(lambda: server.asked == 1)
    qtbot.wait(50)

    assert check.release is None
    assert settings.get(UPDATE_CHECKED_AT) == clock.now
    assert not check.check_if_due()


def test_turned_off_it_never_asks(qtbot: QtBot, settings: Settings) -> None:
    del qtbot
    server = Server(NEWER)
    check = checker(settings, server, FakeClock())

    check.set_enabled(False)

    assert not check.check_if_due()
    assert server.asked == 0
    assert settings.get(UPDATE_CHECK) is False


def test_turning_it_off_stops_showing_the_new_version(qtbot: QtBot, settings: Settings) -> None:
    check = checker(settings, Server(NEWER), FakeClock())
    with qtbot.waitSignal(check.found, timeout=3000):
        check.check_if_due()

    check.set_enabled(False)

    assert check.release is None


def test_the_request_carries_nothing_that_tells_users_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[urllib.request.Request] = []

    class Opener:
        def open(self, request: urllib.request.Request, timeout: float) -> None:
            del timeout
            sent.append(request)
            raise OSError("offline")

    def build_opener(*_handlers: object) -> Opener:
        return Opener()

    monkeypatch.setattr(feed.urllib.request, "build_opener", build_opener)

    assert fetch_feed() is None

    (request,) = sent
    assert request.full_url == FEED_URL
    assert request.get_method() == "GET"
    assert request.data is None
    assert {k.lower(): v for k, v in request.header_items()} == {
        "user-agent": "Stickle",
        "accept": "application/json",
    }


def test_the_tray_and_window_say_a_new_version_is_out(
    qtbot: QtBot, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[QUrl] = []

    def open_url(url: QUrl) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(note_window, "open_url", open_url)
    connection = open_store(tmp_path / "other.db", KEY)
    manager = NoteManager(NoteRepository(connection))
    check = checker(settings, Server(NEWER), FakeClock())
    tray = Tray(lambda: None, lambda: None, Translations(), manager, updates=check)
    window = StickleWindow(manager, Translations(), lambda: None, updates=check)
    settings_window = window.settings
    try:
        assert not tray.new_version_action.isVisible()
        assert window.new_version_label.isHidden()
        assert settings_window.update_box.isChecked()

        with qtbot.waitSignal(check.found, timeout=3000):
            check.check_if_due()

        assert tray.new_version_action.isVisible()
        assert "0.5.0" in tray.new_version_action.text()
        assert not window.new_version_label.isHidden()
        assert "0.5.0" in window.new_version_label.text()
        window.new_version_button.click()
        tray.new_version_action.trigger()
        assert opened == [QUrl(PAGE), QUrl(PAGE)]

        settings_window.update_box.click()  # off
        assert not check.enabled
        assert not tray.new_version_action.isVisible()
        assert window.new_version_label.isHidden()
    finally:
        tray.hide()
        window.deleteLater()
        connection.close()


def test_the_settings_say_what_stickle_connects_to(
    qtbot: QtBot, settings: Settings, tmp_path: Path
) -> None:
    check = checker(settings, Server(NEWER), FakeClock())
    checking = SettingsWindow(NoteManager(), Translations(), updates=check)
    not_checking = SettingsWindow(NoteManager(), Translations())
    qtbot.addWidget(not_checking)
    try:
        assert "stickle.linkro.co" in checking.connections.text()
        assert "nothing" in not_checking.connections.text()
        checking.update_box.click()  # off: the line says so at once
        assert "turned off" in checking.connections.text()
        assert "stickle.linkro.co" not in checking.connections.text()
        checking.update_box.click()
        assert "stickle.linkro.co" in checking.connections.text()
    finally:
        # Gone before the database closes: a later language switch reaches every window.
        checking.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_about_no_longer_lists_the_connections(qtbot: QtBot) -> None:
    about = AboutDialog()
    qtbot.addWidget(about)

    texts = " ".join(label.text() for label in about.findChildren(QLabel))
    assert "connects" not in texts


def test_without_a_check_nothing_about_updates_shows(qtbot: QtBot, tmp_path: Path) -> None:
    del qtbot
    connection: apsw.Connection = open_store(tmp_path / "notes.db", KEY)
    manager = NoteManager(NoteRepository(connection))
    tray = Tray(lambda: None, lambda: None, Translations(), manager)
    window = StickleWindow(manager, Translations(), lambda: None)
    try:
        assert not tray.new_version_action.isVisible()
        assert window.settings.update_box.isHidden()
        assert window.new_version_button.isHidden()
    finally:
        tray.hide()
        window.deleteLater()
        connection.close()


def test_the_file_comes_from_a_helper_process(tmp_path: Path) -> None:
    # Its HTTP and TLS libraries (4 MB) leave memory with that process.
    script = tmp_path / "helper.py"
    script.write_text('import base64, sys\nsys.stdout.write(base64.b64encode(b"{}").decode())\n')

    assert fetch_feed_in_helper([sys.executable, str(script)]) == b"{}"


def test_a_helper_that_could_not_read_the_file_says_why_and_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "helper.py"
    script.write_text('import sys\nsys.stderr.write("URLError\n")\nsys.exit(1)\n')
    asked_here: list[bool] = []
    monkeypatch.setattr(feed, "fetch_feed", lambda: asked_here.append(True))

    assert fetch_feed_in_helper([sys.executable, str(script)]) is None
    assert asked_here == []  # offline is offline here too


def test_a_helper_that_does_not_start_leaves_it_to_this_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feed, "fetch_feed", lambda: b"{}")

    assert fetch_feed_in_helper([str(tmp_path / "no-such-program")]) == b"{}"


def test_the_helper_writes_the_file_or_why_not(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(feed, "_fetch", lambda: b'{"latest": "9.9.9"}')
    assert feed.serve(out, err) == 0
    assert base64.b64decode(out.getvalue()) == b'{"latest": "9.9.9"}'

    def offline() -> bytes:
        raise OSError("offline")

    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(feed, "_fetch", offline)
    assert feed.serve(out, err) == 1
    assert (out.getvalue(), err.getvalue()) == ("", "OSError\n")
