"""Sync stopped for something a window asks about: that window, what each of its
buttons does and what comes of it, the line at the top of Stickle Manager that
tells of it once, the notification of the system, and the ways to the window.
Sync here is a stand-in: its rounds are what each test says they were."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import QCoreApplication, QDate, QDateTime, QEvent, QObject, Qt, QTime, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from stickle.app.i18n import Translations
from stickle.app.notes import NoteManager
from stickle.app.stickle_window import StickleWindow
from stickle.app.sync_display import SyncDisplay
from stickle.app.sync_setup import SyncSetup
from stickle.app.sync_status import SyncStatus
from stickle.app.sync_stopped import SyncStopped
from stickle.app.syncing import BigChange, Ended, Syncing
from stickle.app.tray import Tray
from stickle.app.turn_on_sync import TurnOnSync
from stickle.data.schema import open_store
from stickle.data.settings import SYNC_STOP_SEEN, SYNC_STOP_TOLD, Settings
from stickle.data.sync_connection import WEBDAV, Login
from stickle.sync.engine import Report
from stickle.sync.outcome import Outcome, Stopped

KEY = bytes([3]) * 32
LOGIN = Login("vault", WEBDAV, "http://nas.local/dav", "u", "p", {1: bytes(32)})
MS = 5_000


@pytest.fixture(autouse=True)
def windows_deleted() -> Iterator[None]:
    yield
    for window in QApplication.topLevelWidgets():
        if isinstance(window, (SyncStopped, TurnOnSync)):
            window.done(0)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


class FakeSync(QObject):
    """Sync as the window and the lines see it."""

    unsent_changed = Signal(object)
    round_shown = Signal(object)
    moving = Signal(str, int, int)

    def __init__(self, stopped: Stopped | None, change: BigChange | None = None) -> None:
        super().__init__()
        self.status = SyncStatus(
            "WebDAV", stopped, lambda: 0, lambda: QDateTime(QDate.currentDate(), QTime(14, 5))
        )
        self.round_shown.connect(self.status.round_ended)
        self.cloud_name = "WebDAV"
        self.change = change or BigChange([], [], [])
        self.done: list[str] = []
        self.login = LOGIN

    def big_change(self) -> BigChange:
        return self.change

    def take_big_change(self) -> None:
        self.done.append("take")

    def leaving(self) -> None:
        self.done.append("leaving")

    def leave(self) -> None:
        self.done.append("leave")

    def kept_login(self) -> Login:
        return self.login

    def ended(self, outcome: Outcome, backup_failed: bool = False) -> None:
        self.round_shown.emit(Ended(Report(), outcome, None, backup_failed))


@dataclass
class Asked:
    window: SyncStopped
    sync: FakeSync
    again: list[str] = field(default_factory=list[str])
    disconnected: list[str] = field(default_factory=list[str])


def asking(qtbot: QtBot, why: Stopped, change: BigChange | None = None) -> Asked:
    sync = FakeSync(why, change)
    asked = Asked(cast(SyncStopped, None), sync)
    window = SyncStopped(
        cast(Syncing, sync),
        lambda: asked.again.append("again"),
        lambda: asked.disconnected.append("gone"),
    )
    asked.window = window  # deletes itself once closed: not the test's to close
    window.show()
    return asked


BIG = BigChange(["첫 메모", "", None], ["영구히 갈 메모"], [])


def lines_of(asked: Asked, which: str) -> list[str]:
    folded = getattr(asked.window, which)
    return [folded.lines.item(row).text() for row in range(folded.lines.count())]


# What it says


def test_a_big_change_is_counted_and_unfolds_into_first_lines(qtbot: QtBot) -> None:
    asked = asking(qtbot, Stopped.BIG_CHANGE, BIG)
    window = asked.window
    assert window.windowTitle() == "Sync stopped"
    assert "removed or changed many notes" in window.text.text()
    assert window.trash.toggle.text() == "3 note(s) would go to the trash"
    assert window.gone.toggle.text() == "1 note(s) would be deleted for good"
    assert window.copies.isHidden()  # none to count
    assert lines_of(asked, "trash") == ["첫 메모", "Empty note", "Secret note"]
    italic = [window.trash.lines.item(row).font().italic() for row in range(3)]
    assert italic == [False, True, True]  # what stands for a line, not one of the note's
    assert window.trash.lines.isHidden()
    window.trash.toggle.click()
    assert not window.trash.lines.isHidden()
    assert window.trash.lines.accessibleName() == window.trash.toggle.text()
    assert "A backup is made first" in window.after.text()
    assert "won't upload either" in window.until.text()
    assert window.first_button.text() == "&Back up and bring in"
    assert window.second_button.text() == "&Not now"


def test_enter_does_not_bring_a_big_change_in(qtbot: QtBot) -> None:
    asked = asking(qtbot, Stopped.BIG_CHANGE, BIG)
    window = asked.window
    assert window.focusWidget() is window.trash.toggle
    assert window.second_button.isDefault() and not window.first_button.isDefault()
    QTest.keyClick(window.trash.toggle, Qt.Key.Key_Return)  # not now
    assert asked.sync.done == []
    assert not window.isVisible()


@pytest.mark.parametrize(
    ("why", "first", "said"),
    [
        (Stopped.VAULT_GONE, "&Make it again from this computer's notes…", "is gone from WebDAV"),
        (Stopped.VAULT_CHANGED, "&Sync with the new folder…", "in WebDAV was made anew"),
        (Stopped.CERTIFICATE, "&View the new certificate…", "certificate has changed"),
    ],
)
def test_the_folder_or_the_certificate_is_asked_about(
    qtbot: QtBot, why: Stopped, first: str, said: str
) -> None:
    asked = asking(qtbot, why)
    window = asked.window
    assert said in window.text.text()
    assert window.after.text()
    assert window.trash.isHidden() and window.until.isHidden()
    assert window.first_button.text() == first
    assert window.second_button.text() == "&Disconnect"
    assert window.focusWidget() is window.first_button
    assert window.first_button.isDefault()


@pytest.mark.parametrize("why", [Stopped.VAULT_GONE, Stopped.VAULT_CHANGED, Stopped.CERTIFICATE])
def test_going_on_is_done_by_the_window_that_turns_sync_on(qtbot: QtBot, why: Stopped) -> None:
    asked = asking(qtbot, why)
    QTest.keyClick(asked.window.first_button, Qt.Key.Key_Return)  # one more window: on Enter
    assert asked.again == ["again"]
    assert not asked.window.isVisible()
    assert asked.sync.done == []


# What comes of it


def test_not_now_leaves_it_stopped_and_changes_nothing(qtbot: QtBot) -> None:
    asked = asking(qtbot, Stopped.BIG_CHANGE, BIG)
    asked.window.second_button.click()
    assert not asked.window.isVisible()
    assert asked.sync.done == []


def test_backing_up_and_bringing_in_waits_for_the_round_and_closes(qtbot: QtBot) -> None:
    asked = asking(qtbot, Stopped.BIG_CHANGE, BIG)
    window = asked.window
    window.first_button.click()
    assert asked.sync.done == ["take"]
    assert not window.first_button.isEnabled() and not window.second_button.isEnabled()
    assert window.message.text() == "Backing up, then bringing it in…"
    asked.sync.status.moved("getting", 1, 30)  # how far it has got: still waiting
    assert window.isVisible()
    asked.sync.ended(Outcome())
    assert not window.isVisible()


def test_a_backup_that_cannot_be_made_is_said_and_it_stays(qtbot: QtBot) -> None:
    asked = asking(qtbot, Stopped.BIG_CHANGE, BIG)
    window = asked.window
    window.first_button.click()
    asked.sync.ended(Outcome(Stopped.BIG_CHANGE), backup_failed=True)
    assert window.isVisible()
    assert "Couldn't make the backup" in window.message.text()
    assert window.first_button.isEnabled()
    assert "Couldn't make the backup" in window.first_button.accessibleDescription()


def test_more_changed_meanwhile_shows_what_it_stopped_for_now(qtbot: QtBot) -> None:
    asked = asking(qtbot, Stopped.BIG_CHANGE, BIG)
    window = asked.window
    window.first_button.click()
    asked.sync.change = BigChange([f"메모 {n}" for n in range(12)], [], [])
    asked.sync.ended(Outcome(Stopped.BIG_CHANGE))
    assert window.isVisible()
    assert window.trash.toggle.text() == "12 note(s) would go to the trash"
    assert window.gone.isHidden()
    assert "stopped again" in window.message.text()
    assert window.first_button.isEnabled()


def test_a_round_that_failed_for_now_closes_it_and_sync_carries_on_by_itself(
    qtbot: QtBot,
) -> None:
    asked = asking(qtbot, Stopped.BIG_CHANGE, BIG)
    asked.window.first_button.click()
    asked.sync.ended(Outcome(wait=15.0, failures=1, offline=True))
    assert not asked.window.isVisible()


def test_sync_going_on_meanwhile_closes_it(qtbot: QtBot) -> None:
    asked = asking(qtbot, Stopped.VAULT_GONE)
    asked.sync.ended(Outcome())
    assert not asked.window.isVisible()


def test_stopped_for_something_else_it_asks_about_that(qtbot: QtBot) -> None:
    asked = asking(qtbot, Stopped.CERTIFICATE)
    asked.sync.ended(Outcome(Stopped.VAULT_GONE))
    assert asked.window.isVisible()
    assert asked.window.why is Stopped.VAULT_GONE
    assert "is gone from" in asked.window.text.text()


def test_disconnecting_leaves_and_forgets_the_connection(qtbot: QtBot) -> None:
    asked = asking(qtbot, Stopped.VAULT_GONE)
    window = asked.window
    finished: list[int] = []
    window.finished.connect(finished.append)
    window.second_button.click()
    assert window.message.text() == "Disconnecting…"
    assert not window.second_button.isEnabled()
    QTest.keyClick(window, Qt.Key.Key_Escape)  # not while it leaves
    assert finished == []
    qtbot.waitUntil(lambda: asked.disconnected == ["gone"], timeout=MS)
    assert asked.sync.done == ["leaving", "leave"]
    assert finished == [1]


# Told once, and the ways to the window


@dataclass
class Manager:
    window: StickleWindow
    display: SyncDisplay
    settings: Settings
    told: list[str]
    tray: Tray
    clicks: list[Callable[[], object]]


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    db = open_store(tmp_path / "notes.db", KEY)
    yield Settings(db)
    db.close()


def setup() -> SyncSetup:
    def unused(*_args: object) -> Syncing:
        raise AssertionError("not here")

    return SyncSetup(
        lambda: None,
        cast(object, None),  # pyright: ignore[reportArgumentType]
        lambda: 0,
        lambda: {},
        unused,
        lambda: None,
    )


def manager_of(qtbot: QtBot, settings: Settings) -> Manager:
    translations = Translations()
    notes = NoteManager()
    tray = Tray(lambda: None, lambda: None, translations, notes)
    window = StickleWindow(notes, translations, lambda: None)
    qtbot.addWidget(window)
    told: list[str] = []
    clicks: list[Callable[[], object]] = []

    def notify(text: str, clicked: Callable[[], object]) -> None:
        told.append(text)
        clicks.append(clicked)

    display = SyncDisplay(tray, window, notes, translations, None, settings, setup(), notify)
    return Manager(window, display, settings, told, tray, clicks)


def stopped_window() -> SyncStopped | None:
    found = [w for w in QApplication.topLevelWidgets() if isinstance(w, SyncStopped)]
    shown = [w for w in found if w.isVisible()]
    return shown[0] if shown else None


def test_a_stop_is_told_at_the_top_and_once_to_the_system(qtbot: QtBot, settings: Settings) -> None:
    at = manager_of(qtbot, settings)
    sync = FakeSync(None)
    at.display.follow(cast(Syncing, sync))
    line = at.window.sync_stop_line
    assert line.isHidden() and at.told == []
    sync.ended(Outcome(Stopped.VAULT_GONE))
    said = "The Stickle folder is gone from WebDAV, so sync stopped."
    assert not line.isHidden() and line.label.text() == said
    assert at.told == [said]
    assert settings.get(SYNC_STOP_TOLD) == "vault gone"
    sync.status.moved("getting", 1, 40)
    sync.ended(Outcome(Stopped.VAULT_GONE))  # stopped again: not told again
    assert at.told == [said]
    line.review_button.click()
    window = stopped_window()
    assert window is not None and window.why is Stopped.VAULT_GONE
    window.reject()


def test_with_stickle_manager_open_the_system_is_not_told(qtbot: QtBot, settings: Settings) -> None:
    at = manager_of(qtbot, settings)
    at.window.show()
    sync = FakeSync(None)
    at.display.follow(cast(Syncing, sync))
    sync.ended(Outcome(Stopped.BIG_CHANGE))
    assert at.told == []
    assert not at.window.sync_stop_line.isHidden()


def test_ok_hides_the_line_until_the_next_stop(qtbot: QtBot, settings: Settings) -> None:
    at = manager_of(qtbot, settings)
    sync = FakeSync(None)
    at.display.follow(cast(Syncing, sync))
    sync.ended(Outcome(Stopped.CERTIFICATE))
    line = at.window.sync_stop_line
    line.ok_button.click()
    assert line.isHidden()
    assert settings.get(SYNC_STOP_SEEN) == "certificate"
    sync.ended(Outcome(Stopped.CERTIFICATE))
    assert line.isHidden()
    sync.ended(Outcome())  # through: the stop is over
    assert settings.get(SYNC_STOP_SEEN) is None and settings.get(SYNC_STOP_TOLD) is None
    sync.ended(Outcome(Stopped.CERTIFICATE))
    assert not line.isHidden()
    assert len(at.told) == 2


def test_at_the_next_start_the_line_stays_until_ok_and_the_system_is_not_told_again(
    qtbot: QtBot, settings: Settings
) -> None:
    first = manager_of(qtbot, settings)
    sync = FakeSync(None)
    first.display.follow(cast(Syncing, sync))
    sync.ended(Outcome(Stopped.VAULT_CHANGED))
    assert len(first.told) == 1
    again = manager_of(qtbot, settings)
    again.display.follow(cast(Syncing, FakeSync(Stopped.VAULT_CHANGED)))  # as kept
    assert not again.window.sync_stop_line.isHidden()
    assert again.told == []


def test_a_round_that_failed_for_now_forgets_nothing_told(qtbot: QtBot, settings: Settings) -> None:
    at = manager_of(qtbot, settings)
    sync = FakeSync(None)
    at.display.follow(cast(Syncing, sync))
    sync.ended(Outcome(Stopped.VAULT_GONE))
    sync.ended(Outcome(wait=15.0, failures=1, offline=True))
    assert at.window.sync_stop_line.isHidden()  # not stopped for now
    assert settings.get(SYNC_STOP_TOLD) == "vault gone"
    sync.ended(Outcome(Stopped.VAULT_GONE))
    assert len(at.told) == 1


def test_a_stop_with_nothing_to_ask_is_not_told_here(qtbot: QtBot, settings: Settings) -> None:
    at = manager_of(qtbot, settings)
    sync = FakeSync(None)
    at.display.follow(cast(Syncing, sync))
    sync.ended(Outcome(Stopped.SIGN_IN))
    assert at.window.sync_stop_line.isHidden() and at.told == []


def test_the_notification_opens_the_window(qtbot: QtBot, settings: Settings) -> None:
    at = manager_of(qtbot, settings)
    sync = FakeSync(None)
    at.display.follow(cast(Syncing, sync))
    sync.ended(Outcome(Stopped.BIG_CHANGE))
    (clicked,) = at.clicks
    clicked()
    window = stopped_window()
    assert window is not None and window.why is Stopped.BIG_CHANGE
    window.reject()


@pytest.mark.parametrize("way", ["status line", "tray", "connections page"])
def test_the_lines_saying_how_sync_stands_lead_to_the_window(
    qtbot: QtBot, settings: Settings, way: str
) -> None:
    at = manager_of(qtbot, settings)
    at.tray.sync_action.triggered.connect(lambda: at.window.open_standing())
    sync = FakeSync(Stopped.VAULT_GONE)
    at.display.follow(cast(Syncing, sync))
    match way:
        case "status line":
            at.window.sync_line.pressed.emit()
        case "tray":
            at.tray.sync_action.trigger()
        case _:
            at.window.settings.sync_review_asked.emit()
    window = stopped_window()
    assert window is not None
    assert not at.window.settings.isVisible()
    # Asked for again, the one open comes forward.
    at.window.open_standing()
    assert stopped_window() is window
    window.reject()


def test_otherwise_they_lead_to_the_connection_settings(qtbot: QtBot, settings: Settings) -> None:
    at = manager_of(qtbot, settings)
    at.display.follow(cast(Syncing, FakeSync(Stopped.SIGN_IN)))
    at.window.sync_line.pressed.emit()
    assert stopped_window() is None
    assert at.window.settings.isVisible()
    assert at.window.settings.tabs.currentWidget() is at.window.settings.connections_page
    at.window.settings.close()


def test_sync_turned_off_or_replaced_closes_the_window_that_asked(
    qtbot: QtBot, settings: Settings
) -> None:
    at = manager_of(qtbot, settings)
    at.display.follow(cast(Syncing, FakeSync(Stopped.VAULT_GONE)))
    at.display.open_stopped()
    assert stopped_window() is not None
    at.display.follow(cast(Syncing, FakeSync(None)))
    assert stopped_window() is None
    assert settings.get(SYNC_STOP_TOLD) is None


def test_the_line_is_reached_by_the_keyboard_after_the_lines_before_it(
    qtbot: QtBot, settings: Settings
) -> None:
    at = manager_of(qtbot, settings)
    sync = FakeSync(None)
    at.display.follow(cast(Syncing, sync))
    sync.ended(Outcome(Stopped.BIG_CHANGE))
    line = at.window.sync_stop_line
    following: QWidget | None = at.window.cloud_folder_line.ok_button.nextInFocusChain()
    assert following is line.review_button
    assert line.review_button.nextInFocusChain() is line.ok_button
    assert line.ok_button.accessibleDescription() == line.label.text()
