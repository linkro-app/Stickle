"""Days and times as the user reads them: one of another year with its year."""

from datetime import UTC, datetime, time

from PySide6.QtCore import QDate, QDateTime, QTime
from pytestqt.qtbot import QtBot

from stickle.app.note_view import reminder_text
from stickle.app.reminder_dialog import when_text
from stickle.app.times import day_text, local_time, moment_text
from stickle.core.text_reminders import codes, with_code

THIS_YEAR = QDate.currentDate().year()


def test_a_day_of_another_year_says_its_year(qtbot: QtBot) -> None:
    for year in (THIS_YEAR - 1, THIS_YEAR + 1):
        assert str(year) in day_text(QDate(year, 3, 4))
        assert str(year) in moment_text(QDateTime(QDate(year, 3, 4), QTime(9, 30)))
        assert str(year) in moment_text(QDateTime(QDate(year, 3, 4), QTime(9, 30)), weekday=True)
        assert str(year) in local_time(f"{year}-03-04T12:00:00.000Z")
        assert str(year) in when_text(datetime(year, 3, 4, 12, 0, tzinfo=UTC))
        [code] = codes(with_code("보고서", datetime(year, 3, 4, 9, 30)))
        assert str(year) in reminder_text(code, time(9, 0))


def test_a_day_of_this_year_does_not(qtbot: QtBot) -> None:
    assert str(THIS_YEAR) not in day_text(QDate(THIS_YEAR, 3, 4))
    assert str(THIS_YEAR) not in moment_text(QDateTime(QDate(THIS_YEAR, 3, 4), QTime(9, 30)))
    assert str(THIS_YEAR) not in when_text(datetime(THIS_YEAR, 6, 4, 12, 0, tzinfo=UTC))
    [code] = codes(with_code("보고서", datetime(THIS_YEAR, 3, 4, 9, 30)))
    assert str(THIS_YEAR) not in reminder_text(code, time(9, 0))
