"""Reminders gone by unrung: one line each in their window, dealt with there or
from their note, the window shown once as Stickle starts or the computer wakes
and reached from the tray and Stickle Manager while any is left."""

import secrets
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot
from test_accessibility import unnamed

from stickle.app.application import connect_missed
from stickle.app.i18n import Translations
from stickle.app.missed_reminders import MissedDialog
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.reminders import ReminderKeeper
from stickle.app.stickle_window import StickleWindow
from stickle.app.tray import Tray
from stickle.app.window_flags import stays_on_top
from stickle.core.reminders import Repeat, Rule, time_zone
from stickle.core.text_reminders import MARK
from stickle.data.notes import NoteRepository
from stickle.data.reminders import ReminderRepository
from stickle.data.schema import open_store
from stickle.data.settings import Settings

KEY = secrets.token_bytes(32)
SEOUL = "Asia/Seoul"
HERE = time_zone(SEOUL)
WEDNESDAY_3PM = "2026-11-04T06:00:00.000Z"  # 15:00 in Seoul


class Clock:
    def __init__(self) -> None:
        self.now = "2026-11-02T03:00:00.000Z"  # Monday, noon in Seoul

    def __call__(self) -> str:
        return self.now


class App:
    def __init__(self, connection: apsw.Connection) -> None:
        self.clock = Clock()
        self.notes = NoteRepository(connection, self.clock)
        self.settings = Settings(connection, self.clock)
        self.reminders = ReminderRepository(connection, self.clock, "this-device")
        self.manager = NoteManager(self.notes, idle_ms=10, max_ms=50, settings=self.settings)
        self.told: list[tuple[str, str]] = []
        self.keeper = ReminderKeeper(
            self.reminders,
            self.manager,
            self.settings,
            notify=lambda title, text: self.told.append((title, text)),
            clock=self.clock,
            zone=SEOUL,
        )

    def note(self, text: str, at: time | None = None, repeat: Repeat = "none") -> str:
        """A note, with a reminder of its own on Monday at at."""
        window = self.manager.new_note()
        window.editor.insertPlainText(text)
        self.manager.save(window)
        assert window.note_id is not None
        if at is not None:
            self.reminders.set(
                window.note_id, Rule(date(2026, 11, 2), at, SEOUL, repeat), here=HERE
            )
        return window.note_id

    def window(self, note_id: str) -> NoteWindow:
        window = self.manager.window_for(note_id)
        assert window is not None
        return window

    def at(self, stamp: str) -> None:
        self.clock.now = stamp
        self.keeper.check()

    def close(self) -> None:
        for window in self.manager.windows:
            window.release()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def app(qtbot: QtBot, connection: apsw.Connection) -> Iterator[App]:
    app = App(connection)
    yield app
    app.close()


@pytest.fixture
def dialog(app: App) -> Iterator[MissedDialog]:
    dialog = MissedDialog(app.keeper)
    yield dialog
    dialog.deleteLater()


def gone_by(app: App) -> tuple[str, str]:
    """A note whose reminder was for Monday 13:00, and one every day at 14:00,
    gone by as Stickle was not running until Wednesday 15:00."""
    once = app.note("회의 준비", time(13, 0))
    daily = app.note("물 마시기", time(14, 0), "daily")
    app.at(WEDNESDAY_3PM)
    return once, daily


def test_each_gone_by_is_one_line_saying_how_long_ago_or_how_many_times(
    app: App, dialog: MissedDialog
) -> None:
    once, daily = gone_by(app)
    assert len(app.keeper.missed) == 2
    assert app.told == []

    dialog.present()

    assert [row.missed.note_id for row in dialog.rows] == [once, daily]
    first, every_day = dialog.rows
    assert first.open_button.text() == "회의 준비"
    assert first.open_button.description().startswith("2 day")  # 50 hours
    assert every_day.open_button.text() == "물 마시기"
    # Monday, Tuesday and Wednesday at 14:00: one line.
    assert every_day.open_button.description().startswith("3 times since ")
    assert "회의 준비" in first.open_button.accessibleName()
    assert unnamed(dialog) == []


def test_how_long_ago_in_minutes_hours_or_days(app: App, dialog: MissedDialog) -> None:
    once = app.note("회의 준비", time(13, 0))
    for stamp, said in (
        ("2026-11-02T04:03:00.000Z", "3 minute"),
        ("2026-11-03T04:00:00.000Z", "24 hour"),
        ("2026-11-05T04:00:00.000Z", "3 day"),
    ):
        app.at(stamp)
        dialog.refresh()
        [row] = dialog.rows
        assert row.missed.note_id == once
        assert row.open_button.description().startswith(said), stamp


def test_a_long_title_is_cut_short_and_said_whole(app: App, dialog: MissedDialog) -> None:
    title = "R&D " + "가" * 80
    app.note(title, time(13, 0))
    app.at(WEDNESDAY_3PM)
    dialog.refresh()

    [row] = dialog.rows
    assert row.open_button.text().startswith("R&&D 가")
    assert row.open_button.text().endswith("…")
    assert len(row.open_button.text()) < 60
    assert title in row.open_button.accessibleName()


def test_choosing_a_line_brings_its_note_with_its_strip(app: App, dialog: MissedDialog) -> None:
    once, daily = gone_by(app)
    app.manager.hide(app.window(once))
    dialog.present()
    assert stays_on_top(dialog)  # shown by itself: above the notes

    dialog.rows[0].open_button.click()

    assert not stays_on_top(dialog)  # below the note it brings forward

    window = app.window(once)
    assert window.isVisible()
    assert window.ringing
    assert app.told == []  # no notification for one gone by
    assert [row.missed.note_id for row in dialog.rows] == [daily]
    window.reminder_bar.done_button.click()
    assert not app.keeper.has(once)


def test_done_and_remind_me_again_in_the_list(app: App, dialog: MissedDialog) -> None:
    once, daily = gone_by(app)
    dialog.present()

    dialog.rows[1].done_button.click()  # every day: on to Thursday

    reminder = app.reminders.of_note(daily)
    assert reminder is not None
    assert reminder.rings_at() == datetime(2026, 11, 5, 5, 0, tzinfo=UTC)
    [row] = dialog.rows
    assert row.missed.note_id == once

    row.again_button.click()  # the usual wait: 10 minutes

    assert app.keeper.missed == set()
    assert not dialog.isVisible()  # none left
    app.at("2026-11-04T06:10:01.000Z")
    assert app.window(once).ringing
    assert len(app.told) == 1


def test_all_done(app: App, dialog: MissedDialog) -> None:
    once, daily = gone_by(app)
    dialog.present()

    dialog.all_button.click()

    assert app.keeper.missed == set()
    assert not dialog.isVisible()
    assert not app.keeper.has(once)
    assert app.keeper.has(daily)


def test_enter_presses_the_button_with_the_keyboard(app: App, dialog: MissedDialog) -> None:
    once, daily = gone_by(app)
    dialog.present()

    QTest.keyClick(dialog.rows[1].done_button, Qt.Key.Key_Return)
    assert [row.missed.note_id for row in dialog.rows] == [once]
    QTest.keyClick(dialog.all_button, Qt.Key.Key_Return)

    assert app.keeper.missed == set()
    assert app.keeper.has(daily)


def test_up_and_down_go_from_line_to_line(app: App, dialog: MissedDialog) -> None:
    gone_by(app)
    dialog.present()
    first, second = dialog.rows
    first.done_button.setFocus()

    QTest.keyClick(first.done_button, Qt.Key.Key_Down)
    assert dialog.focusWidget() is second.done_button
    QTest.keyClick(second.done_button, Qt.Key.Key_Down)  # the last: stays
    assert dialog.focusWidget() is second.done_button
    QTest.keyClick(second.done_button, Qt.Key.Key_Up)
    assert dialog.focusWidget() is first.done_button


def test_told_once_at_start_and_after_sleep_not_for_a_time_written_past(app: App) -> None:
    told: list[int] = []
    app.keeper.missed_found.connect(lambda: told.append(len(app.keeper.missed)))
    app.note("회의 준비", time(13, 0))
    app.clock.now = WEDNESDAY_3PM

    app.keeper.start()
    assert told == [1]
    app.at("2026-11-04T06:00:30.000Z")
    assert told == [1]

    # A time already past, written in a note: listed, not told.
    app.note(f"어제 할 일 {MARK}2026-11-03 10:00")
    assert len(app.keeper.missed) == 2
    assert told == [1]

    # One for 15:30, then the computer sleeps until 16:00.
    call = app.note("전화하기")
    app.reminders.set(call, Rule(date(2026, 11, 4), time(15, 30), SEOUL), here=HERE)
    app.at("2026-11-04T07:00:00.000Z")
    assert told == [1, 3]


def test_the_tray_and_stickle_manager_say_how_many_are_left(app: App) -> None:
    tray = Tray(lambda: None, lambda: None, Translations(), app.manager)
    window = StickleWindow(app.manager, Translations(), lambda: None)
    dialog = connect_missed(app.keeper, window, tray)
    try:
        assert not tray.missed_action.isVisible()
        assert window.missed_label.isHidden()

        gone_by(app)

        assert tray.missed_action.isVisible()
        assert "2" in tray.missed_action.text()
        assert not window.missed_label.isHidden()
        assert "2" in window.missed_label.text()
        window.missed_button.click()
        assert dialog.isVisible()

        dialog.all_button.click()

        assert not tray.missed_action.isVisible()
        assert window.missed_label.isHidden()
    finally:
        dialog.deleteLater()
        window.deleteLater()
        tray.deleteLater()
