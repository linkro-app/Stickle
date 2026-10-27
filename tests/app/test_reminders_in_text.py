"""Reminders written in a note's text: written with Ctrl+Shift+R, shown as a
day and time in the formatted note, ringing like the note's own, the line's
time standing out."""

import secrets
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path

import apsw
import pytest
from PySide6.QtGui import QTextCursor
from pytestqt.qtbot import QtBot
from test_reminder_ringing import App, answering

from stickle.app import fonts
from stickle.app.note_list import NoteList
from stickle.app.reminder_dialog import ReminderDialog
from stickle.core.reminders import Rule, time_zone
from stickle.crypto.secret_notes import seal_text
from stickle.data.schema import open_store

KEY = secrets.token_bytes(32)
MARK = "⏰"
ONE_PM = "2026-11-02T04:00:01.000Z"  # 13:00:01 in Seoul, the keeper's time zone


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


def written_times(app: App) -> set[datetime | None]:
    return {r.written for r in app.reminders.pending() if r.kind == "text"}


def test_a_reminder_typed_in_the_text_is_kept_and_shown_as_a_day_and_time(app: App) -> None:
    window = app.note(f"- [ ] 보고서 {MARK}2026-11-02 13:00\n- [ ] 점심 {MARK}2026-11-03")

    assert written_times(app) == {datetime(2026, 11, 2, 13, 0), datetime(2026, 11, 3, 9, 0)}
    window.show_formatted()
    shown = window.view.toPlainText()
    assert "2026-11-02" not in shown
    assert shown.count(MARK + " ") == 2


def test_ctrl_shift_r_writes_one_at_the_end_of_the_line_being_edited(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = app.note("- [ ] 보고서\n- [ ] 점심")
    window.edit()
    cursor = window.editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    window.editor.setTextCursor(cursor)
    asked = answering(monkeypatch, lambda dialog: dialog.hour_button.click())

    assert window.text_reminder_action.shortcut().toString() == "Ctrl+Shift+R"
    window.text_reminder_action.trigger()

    assert asked[0].in_text
    assert asked[0].repeat_box.isHidden() and asked[0].hide_box.isHidden()
    assert window.text == f"- [ ] 보고서 {MARK}2026-11-02 13:00\n- [ ] 점심"
    assert window.editing
    app.manager.save(window)
    assert written_times(app) == {datetime(2026, 11, 2, 13, 0)}


def test_written_again_on_a_line_it_takes_the_place_of_the_one_there(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = app.note(f"보고서 {MARK}2026-11-05 10:00")
    window.edit()
    asked = answering(monkeypatch, lambda dialog: dialog.morning_button.click())
    window.text_reminder_action.trigger()

    assert asked[0].rule().start.day == 3  # it opened with the line's time, then changed
    assert window.text == f"보고서 {MARK}2026-11-03 09:00"


def test_offered_in_the_menu_only_while_editing(app: App) -> None:
    window = app.note("보고서")
    window.show_formatted()
    window.menu.aboutToShow.emit()
    assert not window.text_reminder_action.isVisible()
    window.menu.aboutToHide.emit()
    window.edit()
    window.menu.aboutToShow.emit()
    assert window.text_reminder_action.isVisible()


def test_it_rings_and_its_time_stands_out_in_the_note(app: App) -> None:
    window = app.note(f"- [ ] 보고서 {MARK}2026-11-02 13:00\n- [ ] 회의 {MARK}2026-11-02 13:00")
    window.show_formatted()

    app.at(ONE_PM)

    assert window.ringing
    assert len(app.told) == 1  # the same time on two lines: one reminder
    assert window.note_id is not None
    assert app.keeper.look(window.note_id).ringing == {datetime(2026, 11, 2, 13, 0)}
    window.reminder_bar.done_button.click()
    assert not window.ringing
    assert written_times(app) == set()  # dealt with
    assert app.keeper.look(window.note_id).ringing == frozenset()


def test_on_a_checked_line_it_does_not_ring(app: App) -> None:
    app.note(f"- [x] 끝낸 일 {MARK}2026-11-02 13:00")
    app.at(ONE_PM)
    assert app.told == []


def test_taken_out_of_the_text_while_ringing_it_stops(app: App) -> None:
    window = app.note(f"보고서 {MARK}2026-11-02 13:00")
    app.at(ONE_PM)
    assert window.ringing

    window.editor.setPlainText("보고서")
    app.manager.save(window)

    assert not window.ringing


def test_the_note_and_its_text_ringing_together_are_done_together(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = app.note(f"보고서 {MARK}2026-11-02 13:00")
    answering(monkeypatch, lambda dialog: dialog.hour_button.click())  # 13:00 too
    window.reminder_action.trigger()
    app.at(ONE_PM)
    assert window.ringing

    window.reminder_bar.done_button.click()

    assert app.reminders.pending() == []


def test_a_covered_secret_note_rings_by_the_times_saved_while_open(app: App) -> None:
    note = app.notes.create()
    text = f"- [ ] 통장 정리 zqxjkv {MARK}2026-11-02 13:00"
    app.notes.make_secret(note.id, "은행", seal_text(secrets.token_bytes(32), note.id, text))
    app.keeper.text_saved(note.id, text)  # as when it was saved open
    app.manager.open_stored()
    window = app.window(note.id)
    assert window.secret_locked

    app.at(ONE_PM)

    assert window.ringing
    assert app.told[0][0] == "은행"
    assert "zqxjkv" not in repr(app.told)


def test_notes_stored_with_reminders_in_them_are_followed_at_the_start(app: App) -> None:
    app.notes.create(f"가져온 메모 {MARK}2026-11-04 08:00")
    assert app.reminders.pending() == []

    app.keeper.start()

    assert written_times(app) == {datetime(2026, 11, 4, 8, 0)}


def test_a_day_alone_rings_in_the_morning_as_set(app: App) -> None:
    app.note(f"장보기 {MARK}2026-11-03")
    assert written_times(app) == {datetime(2026, 11, 3, 9, 0)}

    app.keeper.set_morning(time(7, 30))

    assert written_times(app) == {datetime(2026, 11, 3, 7, 30)}


def test_the_dialog_for_the_text_has_no_repeat_or_turning_off(app: App) -> None:
    dialog = ReminderDialog(
        "",
        datetime(2026, 11, 2, 3, 0, tzinfo=UTC),
        None,
        time_zone("Asia/Seoul"),
        time(9, 0),
        Rule(date(2026, 11, 5), time(10, 0), None),
        in_text=True,
    )
    assert dialog.off_button.isHidden()
    assert dialog.windowTitle() == "Reminder in this line"
    dialog.deleteLater()


def test_a_stored_note_opens_with_its_reminders_shown_as_days_and_times(app: App) -> None:
    note = app.notes.create(f"보고서 {MARK}2026-11-05 10:00")
    app.manager.open_stored()

    shown = app.window(note.id).view.toPlainText()

    assert f"{MARK}2026" not in shown
    assert MARK + " " in shown


def test_where_the_alarm_clock_would_not_show_it_is_written_in_the_bundled_font(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As on Fedora: the system's emoji font has it in a form Qt draws as nothing.
    monkeypatch.setattr(fonts, "_alarm_clock_family", "Noto Emoji")
    window = app.note(f"보고서 {MARK}2026-11-05 10:00")
    window.show_formatted()

    pieces: list[tuple[str, list[str]]] = []
    block = window.view.document().firstBlock()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            pieces.append((fragment.text(), fragment.charFormat().fontFamilies() or []))
            it += 1
        block = block.next()
    # The alarm clock alone, as a text character, in that font; nothing else in it.
    assert (MARK + fonts.AS_TEXT, ["Noto Emoji"]) in pieces
    assert all(families == [] for text, families in pieces if MARK not in text)

    window.ring("10:00", 10)
    assert "Noto Emoji" in window.reminder_bar.time_label.text()
    assert fonts.AS_TEXT in window.reminder_bar.time_label.text()

    listed = NoteList(app.manager)
    assert MARK not in listed.list.item(0).text()
    assert "Reminder " in listed.list.item(0).text()
    listed.deleteLater()


def test_a_day_picked_in_another_calendar_is_written_first_in_brackets(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    def julian_christmas(dialog: ReminderDialog) -> None:
        dialog.calendar_box.setCurrentIndex(dialog.calendar_box.findData("julian"))
        dialog.year_box.setValue(2026)
        months = [dialog.month_box.itemData(i) for i in range(dialog.month_box.count())]
        dialog.month_box.setCurrentIndex(months.index((12, False)))
        dialog.day_box.setCurrentIndex(dialog.day_box.findData(25))

    window = app.note("성탄 예배")
    window.edit()
    answering(monkeypatch, julian_christmas)
    window.text_reminder_action.trigger()

    # In English here (no translation loaded): "Julian 12/25", in Korean "율리우스력 12월 25일".
    assert window.text == f"성탄 예배 {MARK}[Julian 12/25] 2027-01-07 09:00"
    app.manager.save(window)
    assert written_times(app) == {datetime(2027, 1, 7, 9, 0)}
    window.show_formatted()
    assert "Julian 12/25" in window.view.toPlainText()


def test_a_day_in_brackets_changed_by_hand_is_said_to_disagree(app: App) -> None:
    agrees = app.note(f"성탄 예배 {MARK}[Julian 12/25] 2027-01-07 09:00")
    disagrees = app.note(f"성탄 예배 {MARK}[Julian 12/24] 2027-01-07 09:00")
    unknown = app.note(f"성탄 예배 {MARK}[생일] 2027-01-07 09:00")

    for window in (agrees, disagrees, unknown):
        window.show_formatted()
    assert "disagree" not in agrees.view.toPlainText()
    assert "the days written disagree" in disagrees.view.toPlainText()
    assert "disagree" not in unknown.view.toPlainText()  # not a day it can read
    # The Gregorian day is the one kept.
    assert written_times(app) == {datetime(2027, 1, 7, 9, 0)}
