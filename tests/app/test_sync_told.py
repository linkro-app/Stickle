"""What sync did that the user should know of, told once: stopped for the server
turning the password down, for access taken away, for a newer Stickle's file
format; uploads stopped with the cloud full; notes put in the trash after a
long time away; a note made secret on another computer while it was edited
here, what was written here let go of (one note at a time). Each at the top
of Stickle Manager until OK, the system told once with that window closed,
and each leading where it is dealt with. Sync here is a stand-in: its rounds
are what each test says they were."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import QCoreApplication, QDate, QDateTime, QEvent, QObject, QTime, QUrl, Signal
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot
from sync_screens import FakeSyncing
from test_stickle_window import move_to

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
from stickle.app.sync_status import SyncStatus, cloud_name
from stickle.app.syncing import Ended, Syncing
from stickle.app.tray import Tray
from stickle.app.turn_on_sync import TurnOnSync
from stickle.app.update_check import UpdateCheck
from stickle.app.window_flags import stays_on_top
from stickle.core.note import SealedText
from stickle.core.updates import Release
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import (
    GOING_BACK_FAILED,
    SYNC_GONE_BACK,
    SYNC_LET_GO,
    SYNC_SECRETS_RESEALED,
    SYNC_STOP_SEEN,
    SYNC_STOP_TOLD,
    SYNC_TRASHED,
    Settings,
)
from stickle.data.sync_connection import ONEDRIVE, WEBDAV, Login
from stickle.platform.updates import STORE
from stickle.sync.engine import LetGo, Report
from stickle.sync.outcome import Outcome, Stopped

KEY = bytes([4]) * 32
LOGIN = Login("vault", WEBDAV, "http://nas.local/dav", "u", "p", {1: bytes(32)})
ONEDRIVE_LOGIN = Login("vault", ONEDRIVE, "", "someone@example.com", "a token", {1: bytes(32)})
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
    unmatched_changed = Signal(object)
    round_shown = Signal(object)
    moving = Signal(str, int, int)

    def __init__(self, stopped: Stopped | None = None, login: Login = LOGIN) -> None:
        super().__init__()
        self.status = SyncStatus(
            login.kind,
            stopped,
            lambda _among: 0,
            lambda: QDateTime(QDate.currentDate(), QTime(14, 5)),
        )
        self.round_shown.connect(self.status.round_ended)
        self.login = login
        self.cloud_name = cloud_name(login.kind)

    def kept_login(self) -> Login:
        return self.login

    def ended(
        self,
        outcome: Outcome,
        trashed: int = 0,
        taken_in: bool = False,
        let_go: list[LetGo] | None = None,
    ) -> None:
        report = Report(trashed=[f"note {n}" for n in range(trashed)], let_go=let_go or [])
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
    return SyncSetup(lambda: None, keys, lambda: 0, lambda: {}, unused, lambda _left: None)


def manager_of(
    qtbot: QtBot,
    settings: Settings,
    updates: UpdateCheck | None = None,
    notes: NoteManager | None = None,
) -> Manager:
    translations = Translations()
    notes = NoteManager() if notes is None else notes
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


@pytest.mark.parametrize("way", ["button", "notification", "status line", "tray"])
def test_signed_out_of_onedrive_leads_to_signing_in_anew(
    qtbot: QtBot, settings: Settings, way: str
) -> None:
    at = manager_of(qtbot, settings)
    sync = at.follow(FakeSync(login=ONEDRIVE_LOGIN))
    sync.ended(Outcome(Stopped.SIGN_IN))
    assert at.line == (
        "Stickle was signed out of OneDrive, so sync stopped. Sign in again to carry on."
        " Your notes are still on this computer."
    )
    assert at.window.sync_stop_line.action_button.text() == "Sign &in again…"
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
    assert window.pages.current() is window.sign_in_page
    assert window.message.text() == ""  # nothing sent, nothing turned down yet
    assert not at.window.settings.isVisible()


def test_the_window_it_leads_to_stays_above_only_until_the_user_moves_on(
    qtbot: QtBot, settings: Settings
) -> None:
    # Above every other window until it was closed, it covered the page the
    # user went to in the browser.
    at = manager_of(qtbot, settings)
    sync = at.follow(FakeSync())
    sync.ended(Outcome(Stopped.SIGN_IN))
    at.window.open_standing()
    window = password_window()
    assert window is not None and stays_on_top(window)
    move_to(qtbot, window)
    assert stays_on_top(window)

    move_to(qtbot, None)  # to another app
    assert not stays_on_top(window)


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
    gone, sealed = at.window.sync_gone_back_line, at.window.sync_secrets_line
    matched, let_go = at.window.sync_matched_line, at.window.sync_let_go_line
    not_back = at.window.not_gone_back_line
    assert not_back.action_button.nextInFocusChain() is not_back.ok_button
    assert not_back.ok_button.nextInFocusChain() is gone.action_button
    assert gone.action_button.nextInFocusChain() is gone.ok_button
    assert gone.ok_button.nextInFocusChain() is sealed.ok_button
    assert sealed.ok_button.nextInFocusChain() is matched.ok_button
    assert matched.ok_button.nextInFocusChain() is let_go.action_button
    assert let_go.action_button.nextInFocusChain() is let_go.ok_button
    assert let_go.ok_button.nextInFocusChain() is stop.action_button
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
    panel = SyncPanel(cast(Syncing, syncing), lambda _left: None)
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
    panel = SyncPanel(
        cast(Syncing, FakeSyncing().round_was(Outcome(Stopped.NEWER))), lambda _left: None
    )
    qtbot.addWidget(panel)
    assert panel.review_button.isHidden()


# Going back to a backup on every computer that syncs


def gone_back(settings: Settings) -> None:
    settings.set(
        SYNC_GONE_BACK,
        {"to": "2026-11-14T03:00:00.000Z", "before": "notes-20261115T010203000Z-before-restore.db"},
    )


def test_going_back_is_told_in_the_run_after_with_the_way_back(
    qtbot: QtBot, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone_back(settings)
    at = manager_of(qtbot, settings)
    undone: list[str] = []
    monkeypatch.setattr(at.window, "undo_going_back", undone.append)
    line = at.window.sync_gone_back_line

    qtbot.waitUntil(lambda: not line.isHidden())

    assert line.label.text().startswith("Every computer that syncs went back to the backup from ")
    assert "2026-11-14" in line.label.text()
    assert line.action_button.text() == "&Undo"
    assert [each for each in at.told if "went back to the backup" in each]
    assert settings.get(SYNC_GONE_BACK) is None  # this run only
    line.action_button.click()
    assert undone == ["notes-20261115T010203000Z-before-restore.db"]
    assert not line.isHidden()  # until OK
    line.ok_button.click()
    assert line.isHidden()


def test_a_run_after_that_says_nothing_of_it(qtbot: QtBot, settings: Settings) -> None:
    gone_back(settings)
    manager_of(qtbot, settings)
    qtbot.wait(10)
    again = manager_of(qtbot, settings)
    qtbot.wait(10)
    assert again.window.sync_gone_back_line.isHidden()
    assert not again.told


def test_with_stickle_manager_open_the_system_is_not_told_of_it(
    qtbot: QtBot, settings: Settings
) -> None:
    gone_back(settings)
    at = manager_of(qtbot, settings)
    at.window.show()
    qtbot.waitUntil(lambda: not at.window.sync_gone_back_line.isHidden())
    assert not at.told


def test_going_back_is_told_in_korean(
    qtbot: QtBot, settings: Settings, translations: Translations
) -> None:
    translations.apply("ko")
    gone_back(settings)
    at = manager_of(qtbot, settings)
    line = at.window.sync_gone_back_line

    qtbot.waitUntil(lambda: not line.isHidden())

    assert line.label.text().startswith("동기화하는 모든 PC를 ")
    assert line.label.text().endswith(" 백업으로 되돌렸습니다.")
    assert line.action_button.text() == "되돌리기 전으로(&U)"


# The secret notes sealed for the recovery key of the computers this one syncs with


def test_the_secret_notes_sealed_again_are_told_until_ok_and_once_to_the_system(
    qtbot: QtBot, settings: Settings
) -> None:
    at = manager_of(qtbot, settings)
    line = at.window.sync_secrets_line
    assert line.isHidden()

    at.display.secrets_resealed()

    assert line.label.text() == (
        "This computer's secret notes now open with the same recovery key as your other"
        " computers too. You can throw the old one away now."
    )
    assert line.action_button.isHidden()  # OK alone
    assert at.told == [line.label.text()]
    again = manager_of(qtbot, settings)  # the next start: still there, the system not told
    again.display.follow(None)  # sync off, even
    assert not again.window.sync_secrets_line.isHidden()
    assert not again.told
    again.window.sync_secrets_line.ok_button.click()
    assert again.window.sync_secrets_line.isHidden()
    assert settings.get(SYNC_SECRETS_RESEALED) is False


def test_the_secret_notes_sealed_again_are_told_in_korean(
    qtbot: QtBot, settings: Settings, translations: Translations
) -> None:
    translations.apply("ko")
    at = manager_of(qtbot, settings)
    at.window.show()

    at.display.secrets_resealed()

    assert at.window.sync_secrets_line.label.text() == (
        "이제 이 PC의 비밀 메모도 다른 PC와 같은 복구 키로 엽니다. 옛 복구 키는 버려도 됩니다."
    )
    assert not at.told  # the window open: the system is not told


# A note made secret on another computer while it was edited here


@pytest.fixture
def with_notes(tmp_path: Path) -> Iterator[tuple[Settings, NoteRepository]]:
    db = open_store(tmp_path / "with-notes.db", KEY)
    yield Settings(db), NoteRepository(db)
    db.close()


def secret_one(notes: NoteRepository, title: str) -> str:
    """A note made secret elsewhere, as sync takes it in."""
    note = notes.create("장보기")
    sealed = SealedText(b"sealed bytes", note.id, "5bd21933-4927-4c74-93bd-c3a05759f1d9")
    notes.take_in(note.id, sealed, "laptop", {"secret_title": title}, {}, {})
    return note.id


def test_a_note_made_secret_elsewhere_while_edited_here_is_told_until_ok_one_at_a_time(
    qtbot: QtBot, with_notes: tuple[Settings, NoteRepository], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, notes = with_notes
    first, second = secret_one(notes, "통장"), secret_one(notes, "일기")
    manager = NoteManager(notes)
    at = manager_of(qtbot, settings, notes=manager)
    shown: list[str] = []
    monkeypatch.setattr(manager, "show_versions_of", shown.append)
    sync = at.follow(FakeSync())
    line = at.window.sync_let_go_line
    assert line.isHidden()

    sync.ended(Outcome(), let_go=[LetGo(first, "노트북")])
    said = (
        "노트북 made “통장” a secret note, so what you changed here wasn't put into it."
        " It stays in Earlier versions on this computer for 30 days."
    )
    assert not line.isHidden() and line.label.text() == said
    assert line.action_button.text() == "Show &earlier versions…"
    assert at.told == [said]
    sync.ended(Outcome(), let_go=[LetGo(second, None)])
    assert line.label.text() == said and len(at.told) == 1  # the first, till OK

    line.action_button.click()
    assert shown == [first] and not line.isHidden()  # it stays till OK
    again = manager_of(qtbot, settings, notes=NoteManager(notes))  # at the next start
    again.follow(FakeSync())
    assert again.window.sync_let_go_line.label.text() == said
    again.window.sync_let_go_line.ok_button.click()
    assert again.window.sync_let_go_line.label.text().startswith(
        "another computer made “일기” a secret note"
    )
    again.window.sync_let_go_line.ok_button.click()
    assert again.window.sync_let_go_line.isHidden() and settings.get(SYNC_LET_GO) == []


def test_one_gone_since_is_not_told(
    qtbot: QtBot, with_notes: tuple[Settings, NoteRepository]
) -> None:
    settings, notes = with_notes
    note_id = secret_one(notes, "통장")
    settings.set(SYNC_LET_GO, [{"note": note_id, "by": "노트북"}, {"note": "gone", "by": None}])
    notes.delete(note_id)

    at = manager_of(qtbot, settings, notes=NoteManager(notes))
    at.follow(FakeSync())

    assert at.window.sync_let_go_line.isHidden() and settings.get(SYNC_LET_GO) == []


def test_a_note_made_secret_elsewhere_is_told_in_korean(
    qtbot: QtBot, with_notes: tuple[Settings, NoteRepository], translations: Translations
) -> None:
    translations.apply("ko")
    settings, notes = with_notes
    note_id = secret_one(notes, "통장")
    at = manager_of(qtbot, settings, notes=NoteManager(notes))

    at.follow(FakeSync()).ended(Outcome(), let_go=[LetGo(note_id, None)])

    line = at.window.sync_let_go_line
    quoted = "\N{LEFT SINGLE QUOTATION MARK}%s\N{RIGHT SINGLE QUOTATION MARK}"
    assert line.label.text() == (
        f"다른 PC에서 {quoted % '통장'} 메모를 비밀 메모로 바꿔서, 이 PC에서 고친 글은 그"
        f" 메모에 넣지 않았습니다. 고친 글은 이 PC의 {quoted % '이전 버전'}에 30일 동안"
        " 남습니다."
    )
    assert line.action_button.text() == "이전 버전 보기(&E)…"


# Going back to a backup that could not be put in place


def test_a_going_back_that_failed_is_told_in_the_run_after_with_a_way_to_try_again(
    qtbot: QtBot, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.set(GOING_BACK_FAILED, True)
    at = manager_of(qtbot, settings)
    asked: list[bool] = []
    monkeypatch.setattr(at.window, "restore_backup", lambda: asked.append(True))
    line = at.window.not_gone_back_line

    qtbot.waitUntil(lambda: not line.isHidden())

    assert line.label.text() == "Couldn't go back to the backup. Your notes are as they were."
    assert line.action_button.text() == "Try aga&in…"
    assert at.told == [line.label.text()]
    assert not settings.get(GOING_BACK_FAILED)  # this run only
    line.action_button.click()
    assert asked == [True]
    assert not line.isHidden()  # until OK
    line.ok_button.click()
    assert line.isHidden()


def test_a_going_back_that_failed_is_not_told_again(qtbot: QtBot, settings: Settings) -> None:
    settings.set(GOING_BACK_FAILED, True)
    manager_of(qtbot, settings)
    qtbot.wait(10)
    again = manager_of(qtbot, settings)
    qtbot.wait(10)
    assert again.window.not_gone_back_line.isHidden()
    assert not again.told


def test_a_going_back_that_failed_is_told_in_korean(
    qtbot: QtBot, settings: Settings, translations: Translations
) -> None:
    translations.apply("ko")
    settings.set(GOING_BACK_FAILED, True)
    at = manager_of(qtbot, settings)
    line = at.window.not_gone_back_line

    qtbot.waitUntil(lambda: not line.isHidden())

    assert line.label.text() == "백업으로 되돌리지 못했습니다. 메모는 그대로입니다."
    assert line.action_button.text() == "다시 해 보기(&I)…"
