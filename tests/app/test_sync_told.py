"""What sync did that the user should know of, told once: stopped for the server
turning the password down, for access taken away, for a newer Stickle's file
format; uploads stopped with the cloud full; notes put in the trash after a
long time away. Each at the top of Stickle Manager until OK, the system told
once with that window closed, and each leading where it is dealt with. Sync
here is a stand-in: its rounds are what each test says they were."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import QCoreApplication, QDate, QDateTime, QEvent, QObject, QTime, QUrl, Signal
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot
from sync_screens import FakeSyncing

from stickle.app import new_version, note_window
from stickle.app.i18n import Translations
from stickle.app.note_list import TRASH
from stickle.app.notes import NoteManager
from stickle.app.recovery_key_dialog import RecoveryKeys
from stickle.app.stickle_window import StickleWindow
from stickle.app.support import site_page
from stickle.app.sync_display import SyncDisplay
from stickle.app.sync_panel import SyncPanel
from stickle.app.sync_setup import SyncSetup
from stickle.app.sync_status import SyncStatus
from stickle.app.syncing import Ended, Syncing
from stickle.app.tray import Tray
from stickle.app.turn_on_sync import TurnOnSync
from stickle.app.update_check import UpdateCheck
from stickle.core.updates import Release
from stickle.data.schema import open_store
from stickle.data.settings import SYNC_STOP_SEEN, SYNC_STOP_TOLD, SYNC_TRASHED, Settings
from stickle.data.sync_connection import WEBDAV, Login
from stickle.platform.updates import STORE
from stickle.sync.engine import Report
from stickle.sync.outcome import Outcome, Stopped

KEY = bytes([4]) * 32
LOGIN = Login("vault", WEBDAV, "http://nas.local/dav", "u", "p", {1: bytes(32)})
RELEASE = Release("0.8.0", "https://github.com/linkro-app/Stickle/releases/tag/v0.8.0")
FULL = Outcome(full=True)
SIGN_IN_TOLD = (
    "The server didn't accept the password, so sync stopped. If you changed it on the"
    " server, enter the new one."
)


@pytest.fixture(autouse=True)
def windows_deleted() -> Iterator[None]:
    yield
    for window in QApplication.topLevelWidgets():
        if isinstance(window, TurnOnSync):
            window.done(0)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What was opened outside Stickle (nothing is)."""
    urls: list[str] = []

    def open_url(url: QUrl) -> bool:
        urls.append(url.toString())
        return True

    monkeypatch.setattr(note_window, "open_url", open_url)
    return urls


class FakeSync(QObject):
    """Sync as the lines and the windows they lead to see it."""

    unsent_changed = Signal(object)
    round_shown = Signal(object)
    moving = Signal(str, int, int)
    cloud_name = "WebDAV"

    def __init__(self, stopped: Stopped | None = None) -> None:
        super().__init__()
        self.status = SyncStatus(
            "WebDAV", stopped, lambda: 0, lambda: QDateTime(QDate.currentDate(), QTime(14, 5))
        )
        self.round_shown.connect(self.status.round_ended)
        self.login = LOGIN

    def kept_login(self) -> Login:
        return self.login

    def ended(self, outcome: Outcome, trashed: int = 0, taken_in: bool = False) -> None:
        report = Report(trashed=[f"note {n}" for n in range(trashed)])
        self.round_shown.emit(Ended(report, outcome, None, False, taken_in))


@dataclass
class Manager:
    window: StickleWindow
    display: SyncDisplay
    tray: Tray
    told: list[str]  # what the system was told
    clicks: list[Callable[[], object]]  # what a click on each opens

    def follow(self, sync: FakeSync) -> FakeSync:
        self.display.follow(cast(Syncing, sync))
        return sync

    @property
    def line(self) -> str:
        """What the line telling of a stop says; empty while it is not shown."""
        stop = self.window.sync_stop_line
        return "" if stop.isHidden() else stop.label.text()


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    db = open_store(tmp_path / "notes.db", KEY)
    yield Settings(db)
    db.close()


def setup() -> SyncSetup:
    def unused(*_args: object) -> Syncing:
        raise AssertionError("not here")

    keys = RecoveryKeys(
        exists=lambda: True,
        make=lambda: "",
        kept=lambda: None,
        was_kept=lambda: True,
        is_old=lambda: False,
    )
    return SyncSetup(lambda: None, keys, lambda: 0, lambda: {}, unused, lambda: None)


def manager_of(qtbot: QtBot, settings: Settings, updates: UpdateCheck | None = None) -> Manager:
    translations = Translations()
    notes = NoteManager()
    tray = Tray(lambda: None, lambda: None, translations, notes, updates)
    window = StickleWindow(notes, translations, lambda: None, updates=updates)
    qtbot.addWidget(window)
    tray.sync_action.triggered.connect(lambda: window.open_standing())
    told: list[str] = []
    clicks: list[Callable[[], object]] = []

    def notify(text: str, clicked: Callable[[], object]) -> None:
        told.append(text)
        clicks.append(clicked)

    display = SyncDisplay(tray, window, notes, translations, None, settings, setup(), notify)
    return Manager(window, display, tray, told, clicks)


def password_window() -> TurnOnSync | None:
    shown = [w for w in QApplication.topLevelWidgets() if isinstance(w, TurnOnSync)]
    return next((w for w in shown if w.isVisible()), None)


# What each says


@pytest.mark.parametrize(
    ("outcome", "said", "way_on"),
    [
        (Outcome(Stopped.SIGN_IN), SIGN_IN_TOLD, "&Enter the password…"),
        (
            Outcome(Stopped.NO_ACCESS),
            "This account can no longer write to the Stickle folder on the server, so sync"
            " stopped. Your notes are still on this computer. Fix its permissions on the"
            " server, then choose Sync now under Settings, Connections.",
            "&Open connection settings",
        ),
        (
            Outcome(Stopped.NEWER),
            "A newer Stickle on another computer changed the file format, so sync waits"
            " until you update. Your notes are still on this computer.",
            "Get the ne&w version…",
        ),
        (
            FULL,
            "WebDAV is full, so uploads stopped. Changes from your other computers still"
            " come in. Free up space and it carries on.",
            "",
        ),
    ],
)
def test_each_is_told_at_the_top_with_its_way_on_and_once_to_the_system(
    qtbot: QtBot, settings: Settings, outcome: Outcome, said: str, way_on: str
) -> None:
    at = manager_of(qtbot, settings)
    sync = at.follow(FakeSync())
    sync.ended(outcome)
    line = at.window.sync_stop_line
    assert at.line == said
    assert line.action_button.isHidden() is (not way_on)
    assert not way_on or line.action_button.text() == way_on
    assert line.ok_button.accessibleDescription() == said
    assert at.told == [said]
    assert settings.get(SYNC_STOP_TOLD) == (
        "full" if outcome.stopped is None else outcome.stopped.value
    )
    sync.ended(outcome)  # the same again: not told again
    assert at.told == [said]


def test_at_the_next_start_it_stays_until_ok_and_the_system_is_not_told_again(
    qtbot: QtBot, settings: Settings
) -> None:
    first = manager_of(qtbot, settings)
    first.follow(FakeSync()).ended(Outcome(Stopped.SIGN_IN))
    assert first.told == [SIGN_IN_TOLD]
    again = manager_of(qtbot, settings)
    again.follow(FakeSync(Stopped.SIGN_IN))  # as kept
    assert again.line == SIGN_IN_TOLD and again.told == []
    again.window.sync_stop_line.ok_button.click()
    assert again.line == "" and settings.get(SYNC_STOP_SEEN) == "sign in"
    once_more = manager_of(qtbot, settings)
    once_more.follow(FakeSync(Stopped.SIGN_IN))
    assert once_more.line == "" and once_more.told == []


def test_a_full_cloud_is_told_once_until_it_had_room_again(
    qtbot: QtBot, settings: Settings
) -> None:
    at = manager_of(qtbot, settings)
    sync = at.follow(FakeSync())
    sync.ended(FULL)
    assert len(at.told) == 1
    sync.ended(Outcome(wait=15.0, failures=1, offline=True))
    assert at.line == ""  # not known for now
    sync.ended(FULL)
    assert at.line and len(at.told) == 1
    # Not known at a start before a round: what was told is kept for it.
    again = manager_of(qtbot, settings)
    sync = again.follow(FakeSync())
    assert settings.get(SYNC_STOP_TOLD) == "full"
    sync.ended(FULL)
    assert again.line and again.told == []
    again.window.sync_stop_line.ok_button.click()
    sync.ended(FULL)
    assert again.line == ""
    sync.ended(Outcome())  # room again
    assert settings.get(SYNC_STOP_TOLD) is None and settings.get(SYNC_STOP_SEEN) is None
    sync.ended(FULL)
    assert again.line and len(again.told) == 1


def test_a_stop_after_a_full_cloud_is_told_and_then_the_full_cloud_again(
    qtbot: QtBot, settings: Settings
) -> None:
    at = manager_of(qtbot, settings)
    sync = at.follow(FakeSync())
    sync.ended(FULL)
    at.window.sync_stop_line.ok_button.click()
    sync.ended(Outcome(Stopped.NO_ACCESS))
    assert at.line.startswith("This account can no longer write")
    assert len(at.told) == 2
    sync.ended(FULL)  # through, and full: a stop no longer, and told as it is
    assert at.line.startswith("WebDAV is full")
    assert len(at.told) == 3


def test_with_stickle_manager_open_the_system_is_not_told(qtbot: QtBot, settings: Settings) -> None:
    at = manager_of(qtbot, settings)
    at.window.show()
    at.follow(FakeSync()).ended(Outcome(Stopped.NEWER))
    assert at.told == [] and at.line


# Where each leads


@pytest.mark.parametrize("way", ["button", "notification", "status line", "tray"])
def test_a_password_turned_down_leads_to_typing_it_anew(
    qtbot: QtBot, settings: Settings, way: str
) -> None:
    at = manager_of(qtbot, settings)
    sync = at.follow(FakeSync())
    sync.ended(Outcome(Stopped.SIGN_IN))
    match way:
        case "button":
            at.window.sync_stop_line.action_button.click()
        case "notification":
            at.clicks[0]()
        case "status line":
            at.window.sync_line.pressed.emit()
        case _:
            at.tray.sync_action.trigger()
    window = password_window()
    assert window is not None
    assert window.pages.current() is window.server_page
    assert (window.address.text(), window.user.text()) == (LOGIN.address, LOGIN.user)
    assert window.password.text() == ""
    assert "didn't accept" in window.message.text()
    assert not at.window.settings.isVisible()
    assert at.line == SIGN_IN_TOLD  # until OK, whatever else is done
    at.window.open_standing()  # asked for again: the one open
    assert password_window() is window


@pytest.mark.parametrize("way", ["button", "notification", "status line"])
def test_access_taken_away_leads_to_the_connection_settings(
    qtbot: QtBot, settings: Settings, way: str
) -> None:
    at = manager_of(qtbot, settings)
    at.follow(FakeSync()).ended(Outcome(Stopped.NO_ACCESS))
    match way:
        case "button":
            at.window.sync_stop_line.action_button.click()
        case "notification":
            at.clicks[0]()
        case _:
            at.window.sync_line.pressed.emit()
    assert at.window.settings.isVisible()
    assert at.window.settings.tabs.currentWidget() is at.window.settings.connections_page
    assert password_window() is None
    at.window.settings.close()


def test_a_full_cloud_leads_to_the_connection_settings(qtbot: QtBot, settings: Settings) -> None:
    at = manager_of(qtbot, settings)
    at.follow(FakeSync()).ended(FULL)
    at.clicks[0]()
    assert at.window.settings.tabs.currentWidget() is at.window.settings.connections_page
    at.window.settings.close()


@pytest.mark.parametrize("way", ["button", "notification", "status line", "tray"])
def test_a_newer_format_leads_to_the_new_version(
    qtbot: QtBot, settings: Settings, opened: list[str], way: str
) -> None:
    at = manager_of(qtbot, settings)
    at.follow(FakeSync()).ended(Outcome(Stopped.NEWER))
    match way:
        case "button":
            at.window.sync_stop_line.action_button.click()
        case "notification":
            at.clicks[0]()
        case "status line":
            at.window.sync_line.pressed.emit()
        case _:
            at.tray.sync_action.trigger()
    assert opened == [site_page()]  # no release known: the website, with every download
    assert not at.window.settings.isVisible()


def test_the_new_version_is_the_release_the_update_check_found(
    qtbot: QtBot, settings: Settings, opened: list[str]
) -> None:
    updates = UpdateCheck(settings)
    updates.release = RELEASE
    at = manager_of(qtbot, settings, updates)
    at.follow(FakeSync()).ended(Outcome(Stopped.NEWER))
    at.window.sync_stop_line.action_button.click()
    assert opened == [RELEASE.page]


def test_the_store_copy_gets_it_from_the_store(
    qtbot: QtBot, settings: Settings, opened: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(new_version, "kept_by", lambda: STORE)
    at = manager_of(qtbot, settings)
    at.follow(FakeSync()).ended(Outcome(Stopped.NEWER))
    at.window.sync_stop_line.action_button.click()
    assert opened == ["ms-windows-store://pdp/?productid=9MZ6BHN3JKFR"]


def test_where_the_software_center_updates_it_that_is_said_and_nothing_opened(
    qtbot: QtBot, settings: Settings, opened: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLATPAK_ID", "co.linkro.stickle")
    at = manager_of(qtbot, settings)
    at.follow(FakeSync()).ended(Outcome(Stopped.NEWER))
    assert at.line.endswith("on this computer. Update it in your software center.")
    assert at.told == [at.line]
    assert at.window.sync_stop_line.action_button.isHidden()
    at.window.sync_line.pressed.emit()
    assert opened == []
    assert at.window.settings.tabs.currentWidget() is at.window.settings.connections_page
    at.window.settings.close()


def test_while_sync_waits_for_an_update_the_line_that_one_is_out_waits_too(
    qtbot: QtBot, settings: Settings
) -> None:
    updates = UpdateCheck(settings)
    updates.release = RELEASE
    at = manager_of(qtbot, settings, updates)
    at.window.refresh_new_version()
    assert not at.window.new_version_label.isHidden()
    sync = at.follow(FakeSync())
    sync.ended(Outcome(Stopped.NEWER))
    assert at.window.new_version_label.isHidden() and at.window.new_version_button.isHidden()
    at.window.sync_stop_line.ok_button.click()
    assert not at.window.new_version_label.isHidden()  # the line of before, as it was
    sync.ended(Outcome())
    assert not at.window.new_version_label.isHidden()


# Notes put in the trash, deleted elsewhere while this computer was away


def test_notes_put_in_the_trash_are_told_until_ok_and_counted_on(
    qtbot: QtBot, settings: Settings
) -> None:
    at = manager_of(qtbot, settings)
    sync = at.follow(FakeSync())
    line = at.window.sync_trash_line
    assert line.isHidden()
    sync.ended(Outcome(), trashed=3)
    said = (
        "While this computer wasn't syncing, 3 note(s) were deleted on your other"
        " computers, so they're in the trash here too."
    )
    assert not line.isHidden() and line.label.text() == said
    assert line.action_button.text() == "S&how the trash"
    assert at.told == [said]
    sync.ended(Outcome(), trashed=2)
    assert "5 note(s)" in line.label.text()
    assert len(at.told) == 1  # once, the first time
    again = manager_of(qtbot, settings)  # at the next start
    again.follow(FakeSync())
    assert "5 note(s)" in again.window.sync_trash_line.label.text()
    again.window.sync_trash_line.ok_button.click()
    assert again.window.sync_trash_line.isHidden() and settings.get(SYNC_TRASHED) == 0
    sync = again.follow(FakeSync())
    sync.ended(Outcome(), trashed=1)
    assert len(again.told) == 1 and "1 note(s)" in again.told[0]


def test_none_is_told_of_the_big_change_the_user_let_in(qtbot: QtBot, settings: Settings) -> None:
    at = manager_of(qtbot, settings)
    at.follow(FakeSync(Stopped.BIG_CHANGE)).ended(Outcome(), trashed=12, taken_in=True)
    assert at.window.sync_trash_line.isHidden()
    assert not [each for each in at.told if "in the trash here too" in each]
    assert settings.get(SYNC_TRASHED) == 0


def test_the_line_shows_the_trash_whatever_was_chosen_and_stays_until_ok(
    qtbot: QtBot, settings: Settings
) -> None:
    at = manager_of(qtbot, settings)
    at.follow(FakeSync()).ended(Outcome(), trashed=2)
    note_list = at.window.note_list
    note_list.search_box.setText("장보기")
    at.window.sync_trash_line.action_button.click()
    assert at.window.isVisible()
    assert note_list.filter_box.currentData() == TRASH
    assert note_list.search_box.text() == ""
    assert note_list.category_box.currentIndex() == 0 and note_list.mark_box.currentIndex() == 0
    assert not at.window.sync_trash_line.isHidden()
    at.window.close()


def test_it_is_said_with_sync_off_too_until_ok(qtbot: QtBot, settings: Settings) -> None:
    settings.set(SYNC_TRASHED, 4)
    at = manager_of(qtbot, settings)
    at.display.follow(None)
    assert "4 note(s)" in at.window.sync_trash_line.label.text()


def test_the_lines_are_reached_by_the_keyboard_in_their_order(
    qtbot: QtBot, settings: Settings
) -> None:
    at = manager_of(qtbot, settings)
    at.follow(FakeSync()).ended(Outcome(Stopped.SIGN_IN), trashed=1)
    stop, trash = at.window.sync_stop_line, at.window.sync_trash_line
    assert stop.action_button.nextInFocusChain() is stop.ok_button
    assert stop.ok_button.nextInFocusChain() is trash.action_button
    assert trash.action_button.nextInFocusChain() is trash.ok_button


# The Connections page


@pytest.mark.parametrize(
    ("outcome", "button"),
    [
        (Outcome(Stopped.SIGN_IN), "&Enter the password…"),
        (Outcome(Stopped.NEWER), "Get the ne&w version…"),
        (Outcome(Stopped.BIG_CHANGE), "&Review…"),
        (Outcome(Stopped.NO_ACCESS), ""),  # Sync now, beside it, once it is fixed
        (FULL, ""),
        (Outcome(), ""),
    ],
)
def test_the_connections_page_has_the_way_on_beside_how_sync_stands(
    qtbot: QtBot, outcome: Outcome, button: str
) -> None:
    syncing = FakeSyncing().round_was(outcome)
    panel = SyncPanel(cast(Syncing, syncing), lambda: None)
    qtbot.addWidget(panel)
    assert panel.review_button.isHidden() is (not button)
    if button:
        assert panel.review_button.text() == button
        asked: list[str] = []
        panel.review.connect(lambda: asked.append("asked"))
        panel.review_button.click()
        assert asked == ["asked"]


def test_where_the_software_center_updates_it_the_page_has_no_button_for_it(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLATPAK_ID", "co.linkro.stickle")
    panel = SyncPanel(cast(Syncing, FakeSyncing().round_was(Outcome(Stopped.NEWER))), lambda: None)
    qtbot.addWidget(panel)
    assert panel.review_button.isHidden()
