"""Other calendars as the user reads them: their names, a day picked in one,
and why a reminder rings on another day than the one picked."""

import re
from datetime import date, timedelta

from PySide6.QtCore import QCoreApplication

from stickle.core.calendars import CALENDARS, GREGORIAN, CalendarDate, Change, resolve
from stickle.core.reminders import Rule


def calendar_name(calendar: str) -> str:
    match calendar:
        case "julian":
            return QCoreApplication.translate("Calendars", "Julian")
        case _:
            return QCoreApplication.translate("Calendars", "Gregorian")


def month_text(month: int, leap: bool) -> str:
    """A month of another calendar by its number, "leap" before a leap month's."""
    if leap:
        return QCoreApplication.translate("Calendars", "leap %1", "a leap month").replace(
            "%1", str(month)
        )
    return str(month)


def picked_text(calendar: str, picked: CalendarDate, with_year: bool = False) -> str:
    """A day picked in another calendar: "Julian 12/25" (in Korean "율리우스력 12월 25일")."""
    month = month_text(picked.month, picked.leap)
    if with_year:
        pattern = QCoreApplication.translate(
            "Calendars", "%1 %2/%3/%4", "calendar, year, month, day"
        )
        parts = [calendar_name(calendar), str(picked.year), month, str(picked.day)]
    else:
        pattern = QCoreApplication.translate("Calendars", "%1 %2/%3", "calendar, month, day")
        parts = [calendar_name(calendar), month, str(picked.day)]
    # Values hold no "%": replaced one after another, they cannot be mistaken.
    for number, part in enumerate(parts, start=1):
        pattern = pattern.replace(f"%{number}", part)
    return pattern


def change_text(calendar: str, change: Change) -> str:
    """Why a reminder rings on another day than the one picked, in a line."""
    match change:
        case Change.NO_LEAP_MONTH:
            return QCoreApplication.translate(
                "Calendars", "That year has no such leap month: it rings in the ordinary month."
            )
        case Change.LAST_DAY:
            return QCoreApplication.translate(
                "Calendars", "That month is shorter: it rings on its last day."
            )
        case Change.DAY_BEFORE:
            return QCoreApplication.translate(
                "Calendars", "%1 days can come a day early: it rings a day before."
            ).replace("%1", calendar_name(calendar))


def is_other(calendar: str) -> bool:
    return calendar != GREGORIAN


def _note_pattern(calendar: str) -> re.Pattern[str]:
    """picked_text without the year, as a pattern for the month and day."""
    leap = re.escape(QCoreApplication.translate("Calendars", "leap %1", "a leap month"))
    month = "(?P<leap>" + leap.replace("%1", "") + ")?(?P<month>[0-9]{1,2})"
    pattern = re.escape(QCoreApplication.translate("Calendars", "%1 %2/%3", "calendar, month, day"))
    pattern = pattern.replace("%1", re.escape(calendar_name(calendar)), 1)
    pattern = pattern.replace("%2", month, 1).replace("%3", "(?P<day>[0-9]{1,2})", 1)
    return re.compile(pattern)


def disagrees(note: str, rings: date) -> bool:
    """Whether a day written in brackets (in this language) is not the day the
    reminder rings on, as one side was changed by hand. Unread notes agree."""
    for calendar_id, calendar in CALENDARS.items():
        found = _note_pattern(calendar_id).match(note)
        if found is None:
            continue
        day = rings + timedelta(days=1) if calendar.early else rings
        picked = calendar.from_gregorian(day)
        written = (int(found["month"]), int(found["day"]), found["leap"] is not None)
        if written == (picked.month, picked.day, picked.leap):
            return False
        # Taken on the earlier side: the last day of a shorter month, the ordinary
        # month of a leap month the year lacks.
        resolved = resolve(calendar, CalendarDate(picked.year, *written[:2], written[2]))
        return resolved is None or resolved.day != rings
    return False


def written_note(rule: Rule) -> str:
    """What a reminder picked in another calendar says in brackets when written in
    a note: the day picked there, and "a day before" if it rings then."""
    if rule.picked is None:
        return ""
    note = picked_text(rule.calendar, rule.picked)
    resolved = resolve(CALENDARS[rule.calendar], rule.picked)
    if resolved is not None and Change.DAY_BEFORE in resolved.changes:
        note = QCoreApplication.translate("Calendars", "%1, a day before").replace("%1", note)
    return note
