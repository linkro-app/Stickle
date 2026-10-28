"""Other calendars as the user reads them (in English here: no translation is
loaded in the tests)."""

from datetime import date, time

from pytestqt.qtbot import QtBot

from stickle.app.calendar_names import disagrees, month_name, picked_text, written_note
from stickle.core.calendars import CalendarDate, Hebrew, resolve
from stickle.core.reminders import Rule


def test_months_by_number_or_by_name(qtbot: QtBot) -> None:
    assert picked_text("korean_lunar", CalendarDate(2026, 8, 15)) == "Lunar 8/15"
    assert picked_text("korean_lunar", CalendarDate(2025, 6, 1, leap=True)) == "Lunar leap 6/1"
    assert picked_text("hebrew", CalendarDate(5787, 7, 1)) == "Hebrew Tishrei 1"
    assert picked_text("persian", CalendarDate(1406, 1, 1), with_year=True) == (
        "Persian Farvardin 1, 1406"
    )


def test_adar_in_a_leap_year_is_adar_i_or_ii(qtbot: QtBot) -> None:
    assert month_name("hebrew", 5786, 12, False) == "Adar"  # an ordinary year
    assert month_name("hebrew", 5784, 12, False) == "Adar I"
    assert month_name("hebrew", 5784, 12, True) == "Adar II"
    # Purim (Adar 14) of 5784 fell in Adar II; Adar I came first.
    hebrew = Hebrew()
    assert hebrew.to_gregorian(CalendarDate(5784, 12, 14, leap=True)) == date(2024, 3, 24)
    early = resolve(hebrew, CalendarDate(5784, 12, 14))
    assert early is not None and early.day == date(2024, 2, 23)


def test_written_in_a_note_with_a_day_before_where_it_rings_then(qtbot: QtBot) -> None:
    ramadan = CalendarDate(1448, 9, 1)
    rule = Rule(date(2027, 2, 7), time(9, 0), None, calendar="islamic_umm_al_qura", picked=ramadan)
    note = written_note(rule)
    assert note == "Islamic (Umm al-Qura) Ramadan 1, a day before"
    assert not disagrees(note, date(2027, 2, 7))
    assert disagrees(note, date(2027, 2, 8))


def test_lunar_in_china_is_not_taken_for_korean(qtbot: QtBot) -> None:
    # New Year 2027: 6 February in China, 7 February in Korea.
    assert not disagrees("Lunar (China) 1/1", date(2027, 2, 6))
    assert not disagrees("Lunar 1/1", date(2027, 2, 7))
    assert disagrees("Lunar 1/1", date(2027, 2, 6))
