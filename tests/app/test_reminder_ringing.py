"""A note's reminder: set from the note (Ctrl+R), shown in its title bar and the
list of notes, ringing at its time with the note in front, then done or snoozed."""

import re
import secrets
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QDate, QEvent, Qt, QTime
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QDialog, QDialogButtonBox, QLabel
from pytestqt.qtbot import QtBot

from stickle.app import reminders as reminders_module
from stickle.app.note_list import NoteList
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.reminder_bar import TOMORROW_MORNING
from stickle.app.reminder_dialog import ReminderDialog
from stickle.app.reminders import ReminderKeeper
from stickle.core.calendars import CalendarDate
from stickle.core.reminders import Rule, time_zone
from stickle.crypto.secret_notes import seal_text
from stickle.data.notes import NoteRepository
from stickle.data.reminders import ReminderRepository
from stickle.data.schema import open_store
from stickle.data.settings import MORNING, SNOOZE_MINUTES, Settings

KEY = secrets.token_bytes(32)
SEOUL = "Asia/Seoul"
HERE = time_zone(SEOUL)
MONDAY_NOON = "2026-11-02T03:00:00.000Z"  # 12:00 in Seoul


class Clock:
    def __init__(self) -> None:
        self.now = MONDAY_NOON

    def __call__(self) -> str:
        return self.now


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


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

    def note(self, text: str) -> NoteWindow:
        window = self.manager.new_note()
        window.editor.insertPlainText(text)
        self.manager.save(window)
        return window

    def window(self, note_id: str) -> NoteWindow:
        window = self.manager.window_for(note_id)
        assert window is not None
        return window

    def at(self, stamp: str) -> None:
        """The clock shows stamp, and the reminders are looked at."""
        self.clock.now = stamp
        self.keeper.check()

    def close(self) -> None:
        for window in self.manager.windows:
            window.release()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def app(qtbot: QtBot, connection: apsw.Connection) -> Iterator[App]:
    app = App(connection)
    yield app
    app.close()


def answering(
    monkeypatch: pytest.MonkeyPatch, fill: Callable[[ReminderDialog], object]
) -> list[ReminderDialog]:
    """The reminder dialog, filled in by fill, then accepted."""
    asked: list[ReminderDialog] = []

    def answer(dialog: QDialog) -> bool:
        assert isinstance(dialog, ReminderDialog)
        asked.append(dialog)
        fill(dialog)
        if not dialog.turned_off:
            dialog.accept()
        return True

    monkeypatch.setattr(reminders_module, "ask_reminder", answer)
    return asked


def at_utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def test_ctrl_r_sets_one_shown_in_the_title_bar_and_the_list(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = app.note("회의 준비")
    asked = answering(monkeypatch, lambda dialog: dialog.morning_button.click())

    assert window.reminder_action.shortcut().toString() == "Ctrl+R"
    window.reminder_action.trigger()

    assert asked[0].note_title.text() == "회의 준비"
    assert window.note_id is not None
    reminder = app.reminders.of_note(window.note_id)
    assert reminder is not None
    assert reminder.rings_at() == at_utc(2026, 11, 3, 0, 0)  # Tuesday, 9:00 in Seoul
    assert window.title_bar.category_tag.reminder is not None
    listed = NoteList(app.manager)
    assert "⏰" in listed.list.item(0).text()
    listed.deleteLater()


def test_the_quick_choices_fill_in_the_day_and_time(app: App) -> None:
    now = at_utc(2026, 11, 2, 3, 0)  # Monday 12:00 in Seoul
    dialog = ReminderDialog("회의 준비", now, SEOUL, HERE, time(9, 0))

    def chosen() -> tuple[date, time]:
        rule = dialog.rule()
        return rule.start, rule.at

    dialog.hour_button.click()
    assert chosen() == (date(2026, 11, 2), time(13, 0))
    dialog.evening_button.click()
    assert chosen() == (date(2026, 11, 2), time(18, 0))
    dialog.morning_button.click()
    assert chosen() == (date(2026, 11, 3), time(9, 0))
    dialog.monday_button.click()
    assert chosen() == (date(2026, 11, 9), time(9, 0))
    dialog.deleteLater()

    evening = ReminderDialog("", at_utc(2026, 11, 2, 10, 0), SEOUL, HERE, time(9, 0))  # 19:00
    assert not evening.evening_button.isEnabled()
    evening.deleteLater()


def test_a_time_gone_by_cannot_be_set_unless_it_repeats(app: App) -> None:
    dialog = ReminderDialog("", at_utc(2026, 11, 2, 3, 0), SEOUL, HERE, time(9, 0))
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    dialog.day_edit.setDate(QDate(2026, 11, 2))
    dialog.time_edit.setTime(QTime(11, 0))
    assert not ok.isEnabled()
    assert not dialog.past_label.isHidden()

    dialog.repeat_box.setCurrentIndex(dialog.repeat_box.findData("daily"))
    assert ok.isEnabled()
    assert dialog.coming_label.text().count("\n") == 4  # the next five times
    dialog.deleteLater()


def test_weekly_shows_the_days_to_choose(app: App) -> None:
    dialog = ReminderDialog("", at_utc(2026, 11, 2, 3, 0), SEOUL, HERE, time(9, 0))
    assert dialog.day_boxes[0].isHidden()
    dialog.repeat_box.setCurrentIndex(dialog.repeat_box.findData("weekly"))
    assert not dialog.day_boxes[0].isHidden()
    assert dialog.day_boxes[1].isChecked()  # tomorrow morning: Tuesday
    dialog.day_boxes[3].setChecked(True)
    assert dialog.rule().weekdays == frozenset({1, 3})
    dialog.deleteLater()


def test_it_rings_at_its_time_in_front_with_one_notification(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = app.note("회의 준비")
    answering(monkeypatch, lambda dialog: dialog.hour_button.click())
    window.reminder_action.trigger()
    note_id = window.note_id
    assert note_id is not None
    app.manager.hide(window)
    assert app.manager.window_for(note_id) is None

    app.at("2026-11-02T03:59:00.000Z")
    assert app.manager.window_for(note_id) is None
    app.at("2026-11-02T04:00:05.000Z")

    shown = app.window(note_id)
    assert shown.isVisible()
    assert shown.ringing
    assert len(app.told) == 1
    assert app.told[0][0] == "회의 준비"
    app.at("2026-11-02T04:00:30.000Z")
    assert len(app.told) == 1


def test_done_moves_one_that_repeats_on_and_ends_one_that_does_not(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    def daily(dialog: ReminderDialog) -> None:
        dialog.hour_button.click()
        dialog.repeat_box.setCurrentIndex(dialog.repeat_box.findData("daily"))

    window = app.note("물 마시기")
    answering(monkeypatch, daily)
    window.reminder_action.trigger()
    app.at("2026-11-02T04:00:01.000Z")
    assert window.ringing

    window.reminder_bar.done_button.click()

    assert not window.ringing
    assert window.note_id is not None
    reminder = app.reminders.of_note(window.note_id)
    assert reminder is not None
    assert reminder.rings_at() == at_utc(2026, 11, 3, 4, 0)

    once = app.note("회의 준비")
    answering(monkeypatch, lambda dialog: dialog.hour_button.click())
    once.reminder_action.trigger()  # 14:00 in Seoul
    app.at("2026-11-02T05:00:02.000Z")
    assert once.ringing
    once.reminder_bar.done_button.click()
    assert once.title_bar.category_tag.reminder is None
    assert once.note_id is not None
    assert not app.keeper.has(once.note_id)


def test_remind_me_again_rings_after_the_wait(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    window = app.note("회의 준비")
    answering(monkeypatch, lambda dialog: dialog.hour_button.click())
    window.reminder_action.trigger()
    app.at("2026-11-02T04:00:01.000Z")

    window.reminder_bar.again_button.click()  # the usual wait: 10 minutes

    assert not window.ringing
    app.at("2026-11-02T04:10:00.000Z")
    assert not window.ringing
    app.at("2026-11-02T04:10:02.000Z")
    assert window.ringing
    assert len(app.told) == 2

    window.reminder_bar.snooze_actions[TOMORROW_MORNING].trigger()
    assert window.note_id is not None
    reminder = app.reminders.of_note(window.note_id)
    assert reminder is not None
    assert reminder.rings_at() == at_utc(2026, 11, 3, 0, 0)


def test_one_gone_by_while_stickle_was_not_running_does_not_ring_late(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = app.note("회의 준비")
    answering(monkeypatch, lambda dialog: dialog.hour_button.click())
    window.reminder_action.trigger()

    app.at("2026-11-02T04:03:00.000Z")  # three minutes late: asleep, or not running

    assert not window.ringing
    assert app.told == []
    assert len(app.keeper.missed) == 1


def test_one_gone_by_and_set_again_rings_at_its_new_time(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = app.note("회의 준비")
    answering(monkeypatch, lambda dialog: dialog.hour_button.click())
    window.reminder_action.trigger()
    app.at("2026-11-02T04:03:00.000Z")
    assert len(app.keeper.missed) == 1

    window.reminder_action.trigger()  # set again, an hour from now: 14:03 in Seoul

    assert app.keeper.missed == set()
    app.at("2026-11-02T05:03:01.000Z")
    assert window.ringing


def test_kept_out_of_sight_until_it_rings(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    def hidden(dialog: ReminderDialog) -> None:
        dialog.hour_button.click()
        dialog.hide_box.setChecked(True)

    window = app.note("월요일 체크리스트")
    note_id = window.note_id
    assert note_id is not None
    answering(monkeypatch, hidden)
    window.reminder_action.trigger()

    assert app.manager.window_for(note_id) is None
    stored = app.notes.get(note_id)
    assert stored is not None and stored.hidden
    app.at("2026-11-02T04:00:01.000Z")
    assert app.window(note_id).ringing


def test_a_secret_notes_notification_says_its_title_only(app: App) -> None:
    note = app.notes.create()
    secret_key = secrets.token_bytes(32)
    app.notes.make_secret(note.id, "은행", seal_text(secret_key, note.id, "통장 비밀번호 zqxjkv"))
    app.reminders.set(note.id, Rule(date(2026, 11, 2), time(13, 0), SEOUL))
    app.manager.open_stored()

    app.at("2026-11-02T04:00:01.000Z")

    assert app.window(note.id).ringing
    assert app.told[0][0] == "은행"
    assert "zqxjkv" not in repr(app.told)


def test_in_the_trash_it_does_not_ring(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    window = app.note("회의 준비")
    answering(monkeypatch, lambda dialog: dialog.hour_button.click())
    window.reminder_action.trigger()
    note_id = window.note_id
    assert note_id is not None
    app.manager.delete(window)

    app.at("2026-11-02T04:00:01.000Z")

    assert app.manager.window_for(note_id) is None
    assert app.told == []


def test_a_note_with_no_text_but_a_reminder_is_kept_when_hidden(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = app.manager.new_note()
    answering(monkeypatch, lambda dialog: dialog.hour_button.click())
    window.reminder_action.trigger()
    note_id = window.note_id
    assert note_id is not None

    app.manager.hide(window)

    stored = app.notes.get(note_id)
    assert stored is not None and stored.hidden


def test_turned_off_it_is_gone(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    window = app.note("회의 준비")
    answering(monkeypatch, lambda dialog: dialog.hour_button.click())
    window.reminder_action.trigger()
    asked = answering(monkeypatch, lambda dialog: dialog.off_button.click())
    window.reminder_action.trigger()

    assert not asked[0].off_button.isHidden()
    assert window.note_id is not None
    assert app.reminders.of_note(window.note_id) is None
    assert window.title_bar.category_tag.reminder is None


def test_the_morning_and_the_wait_are_settings(app: App) -> None:
    app.keeper.set_morning(time(7, 30))
    app.keeper.set_snooze_minutes(30)

    assert app.settings.get(MORNING) == "07:30"
    assert app.settings.get(SNOOZE_MINUTES) == 30
    assert app.keeper.morning == time(7, 30)
    window = app.note("회의 준비")
    window.ring("13:00", app.keeper.snooze_minutes)
    assert window.reminder_bar.snooze_minutes == 30


def test_enter_on_done_deals_with_it(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    window = app.note("회의 준비")
    answering(monkeypatch, lambda dialog: dialog.hour_button.click())
    window.reminder_action.trigger()
    app.at("2026-11-02T04:00:01.000Z")

    QTest.keyClick(window.reminder_bar.done_button, Qt.Key.Key_Return)

    assert not window.ringing
    assert window.note_id is not None
    assert not app.keeper.has(window.note_id)


def test_a_day_picked_in_another_calendar(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    def julian_christmas(dialog: ReminderDialog) -> None:
        dialog.calendar_box.setCurrentIndex(dialog.calendar_box.findData("julian"))
        dialog.year_box.setValue(2026)
        months = [dialog.month_box.itemData(i) for i in range(dialog.month_box.count())]
        dialog.month_box.setCurrentIndex(months.index((12, False)))
        dialog.day_box.setCurrentIndex(dialog.day_box.findData(25))
        dialog.repeat_box.setCurrentIndex(dialog.repeat_box.findData("yearly"))

    window = app.note("성탄 예배")
    asked = answering(monkeypatch, julian_christmas)
    window.reminder_action.trigger()

    assert window.note_id is not None
    reminder = app.reminders.of_note(window.note_id)
    assert reminder is not None
    assert (reminder.rule.calendar, reminder.rule.picked) == ("julian", CalendarDate(2026, 12, 25))
    assert reminder.rings_at() == at_utc(2027, 1, 7, 0, 0)  # 9:00 in Seoul
    assert not asked[0].gregorian_label.isHidden()
    assert asked[0].changes_label.isHidden()
    assert app.keeper.recent_calendars == ["julian"]

    # Opened again, it shows as picked, the calendar first after the Gregorian one.
    answering(monkeypatch, lambda dialog: asked.append(dialog))
    window.reminder_action.trigger()
    again = asked[-1]
    assert again.calendar() == "julian"
    assert again.picked() == CalendarDate(2026, 12, 25)
    assert again.calendar_box.itemData(1) == "julian"


def test_ringing_on_another_day_than_the_one_picked_says_why(app: App) -> None:
    window = app.note("월말 정산")
    assert window.note_id is not None
    picked = CalendarDate(2026, 10, 31)  # Julian: 13 November; monthly
    rule = Rule(date(2026, 11, 13), time(9, 0), SEOUL, "monthly", calendar="julian", picked=picked)
    reminder = app.reminders.set(window.note_id, rule)

    app.at("2026-11-13T00:00:01.000Z")
    assert window.ringing
    assert window.reminder_bar.reason_label.isHidden()  # on the day picked
    window.reminder_bar.done_button.click()
    following = app.reminders.get(reminder.id)
    assert following is not None
    assert following.next_day == date(2026, 12, 13)  # Julian 30 November: no 31st

    app.at("2026-12-13T00:00:01.000Z")
    assert window.ringing
    assert not window.reminder_bar.reason_label.isHidden()
    assert "last day" in window.reminder_bar.reason_label.text()


def test_a_quick_choice_in_a_calendar_whose_days_come_early_rings_that_day(app: App) -> None:
    dialog = ReminderDialog("", at_utc(2026, 11, 2, 3, 0), SEOUL, HERE, time(9, 0))
    dialog.calendar_box.setCurrentIndex(dialog.calendar_box.findData("islamic_umm_al_qura"))
    dialog.morning_button.click()

    rule = dialog.rule()
    assert rule.start == date(2026, 11, 3)  # tomorrow, as chosen
    assert rule.picked == CalendarDate(1448, 5, 24)  # the day after, a day early
    assert "a day early" in dialog.changes_label.text()
    dialog.deleteLater()


def test_repeating_in_a_calendar_whose_data_ends_says_until_when(app: App) -> None:
    dialog = ReminderDialog("", at_utc(2026, 11, 2, 3, 0), SEOUL, HERE, time(9, 0))
    dialog.calendar_box.setCurrentIndex(dialog.calendar_box.findData("korean_lunar"))
    assert dialog.year_box.maximum() == 2050
    dialog.repeat_box.setCurrentIndex(dialog.repeat_box.findData("yearly"))

    assert not dialog.changes_label.isHidden()
    assert "until 2050" in dialog.changes_label.text()
    dialog.deleteLater()


def test_the_dialogs_shortcut_letters_are_its_own(app: App) -> None:
    # On Linux, Qt's OK and Cancel take O and C: a letter shared is ambiguous.
    current = Rule(date(2026, 11, 3), time(9, 0), SEOUL)
    dialog = ReminderDialog("", at_utc(2026, 11, 2, 3, 0), SEOUL, HERE, time(9, 0), current)
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    texts += [button.text() for button in dialog.findChildren(QAbstractButton)]
    letters = [found.lower() for text in texts for found in re.findall(r"&(\w)", text)]
    assert len(letters) == len(set(letters)), letters
    assert not {"o", "c"} & set(letters), letters
    dialog.deleteLater()
