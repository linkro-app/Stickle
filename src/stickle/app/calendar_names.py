"""Other calendars as the user reads them: their names, their months, a day
picked in one, and why a reminder rings on another day than the one picked.

Lunar and Julian months go by their numbers ("8/15", in Korean "8월 15일");
the other calendars' by the names their users know ("Tishrei 1").
"""

import re
from datetime import date, timedelta

from PySide6.QtCore import QT_TRANSLATE_NOOP, QT_TRANSLATE_NOOP3, QCoreApplication

from stickle.core.calendars import CALENDARS, GREGORIAN, CalendarDate, Change, resolve
from stickle.core.reminders import Rule

_CALENDAR_NAMES = {
    GREGORIAN: QT_TRANSLATE_NOOP("Calendars", "Gregorian"),
    "korean_lunar": QT_TRANSLATE_NOOP("Calendars", "Lunar"),
    "chinese_lunar": QT_TRANSLATE_NOOP("Calendars", "Lunar (China)"),
    "hebrew": QT_TRANSLATE_NOOP("Calendars", "Hebrew"),
    "islamic_umm_al_qura": QT_TRANSLATE_NOOP("Calendars", "Islamic (Umm al-Qura)"),
    "persian": QT_TRANSLATE_NOOP("Calendars", "Persian"),
    "ethiopian": QT_TRANSLATE_NOOP("Calendars", "Ethiopian"),
    "julian": QT_TRANSLATE_NOOP("Calendars", "Julian"),
    "indian_national": QT_TRANSLATE_NOOP("Calendars", "Indian national"),
    "bangladeshi": QT_TRANSLATE_NOOP("Calendars", "Bengali (Bangladesh)"),
}

# Months by name, the first month first; Hebrew ones by pyluach's numbers (from
# Nisan), its Adar also as Adar I and II (see stickle.core.calendars.sources).
_MONTH_NAMES: dict[str, list[object]] = {
    "hebrew": [
        QT_TRANSLATE_NOOP("Calendars", "Nisan"),
        QT_TRANSLATE_NOOP("Calendars", "Iyar"),
        QT_TRANSLATE_NOOP("Calendars", "Sivan"),
        QT_TRANSLATE_NOOP("Calendars", "Tammuz"),
        QT_TRANSLATE_NOOP("Calendars", "Av"),
        QT_TRANSLATE_NOOP("Calendars", "Elul"),
        QT_TRANSLATE_NOOP("Calendars", "Tishrei"),
        QT_TRANSLATE_NOOP("Calendars", "Cheshvan"),
        QT_TRANSLATE_NOOP("Calendars", "Kislev"),
        QT_TRANSLATE_NOOP("Calendars", "Tevet"),
        QT_TRANSLATE_NOOP("Calendars", "Shevat"),
        QT_TRANSLATE_NOOP("Calendars", "Adar"),
    ],
    "islamic_umm_al_qura": [
        QT_TRANSLATE_NOOP("Calendars", "Muharram"),
        QT_TRANSLATE_NOOP("Calendars", "Safar"),
        QT_TRANSLATE_NOOP("Calendars", "Rabi al-Awwal"),
        QT_TRANSLATE_NOOP("Calendars", "Rabi al-Thani"),
        QT_TRANSLATE_NOOP("Calendars", "Jumada al-Ula"),
        QT_TRANSLATE_NOOP("Calendars", "Jumada al-Akhirah"),
        QT_TRANSLATE_NOOP("Calendars", "Rajab"),
        QT_TRANSLATE_NOOP("Calendars", "Shaban"),
        QT_TRANSLATE_NOOP("Calendars", "Ramadan"),
        QT_TRANSLATE_NOOP("Calendars", "Shawwal"),
        QT_TRANSLATE_NOOP("Calendars", "Dhu al-Qadah"),
        QT_TRANSLATE_NOOP("Calendars", "Dhu al-Hijjah"),
    ],
    "persian": [
        QT_TRANSLATE_NOOP("Calendars", "Farvardin"),
        QT_TRANSLATE_NOOP("Calendars", "Ordibehesht"),
        QT_TRANSLATE_NOOP("Calendars", "Khordad"),
        QT_TRANSLATE_NOOP3("Calendars", "Tir", "a Persian month"),
        QT_TRANSLATE_NOOP("Calendars", "Mordad"),
        QT_TRANSLATE_NOOP("Calendars", "Shahrivar"),
        QT_TRANSLATE_NOOP("Calendars", "Mehr"),
        QT_TRANSLATE_NOOP("Calendars", "Aban"),
        QT_TRANSLATE_NOOP("Calendars", "Azar"),
        QT_TRANSLATE_NOOP("Calendars", "Dey"),
        QT_TRANSLATE_NOOP("Calendars", "Bahman"),
        QT_TRANSLATE_NOOP("Calendars", "Esfand"),
    ],
    "ethiopian": [
        QT_TRANSLATE_NOOP("Calendars", "Meskerem"),
        QT_TRANSLATE_NOOP("Calendars", "Tikimt"),
        QT_TRANSLATE_NOOP("Calendars", "Hidar"),
        QT_TRANSLATE_NOOP("Calendars", "Tahsas"),
        QT_TRANSLATE_NOOP3("Calendars", "Tir", "an Ethiopian month"),
        QT_TRANSLATE_NOOP("Calendars", "Yekatit"),
        QT_TRANSLATE_NOOP("Calendars", "Megabit"),
        QT_TRANSLATE_NOOP("Calendars", "Miyazya"),
        QT_TRANSLATE_NOOP("Calendars", "Ginbot"),
        QT_TRANSLATE_NOOP("Calendars", "Sene"),
        QT_TRANSLATE_NOOP("Calendars", "Hamle"),
        QT_TRANSLATE_NOOP("Calendars", "Nehase"),
        QT_TRANSLATE_NOOP("Calendars", "Pagume"),
    ],
    "indian_national": [
        QT_TRANSLATE_NOOP("Calendars", "Chaitra"),
        QT_TRANSLATE_NOOP("Calendars", "Vaisakha"),
        QT_TRANSLATE_NOOP("Calendars", "Jyaishtha"),
        QT_TRANSLATE_NOOP("Calendars", "Asadha"),
        QT_TRANSLATE_NOOP("Calendars", "Shravana"),
        QT_TRANSLATE_NOOP("Calendars", "Bhadra"),
        QT_TRANSLATE_NOOP("Calendars", "Ashvin"),
        QT_TRANSLATE_NOOP("Calendars", "Kartika"),
        QT_TRANSLATE_NOOP("Calendars", "Agrahayana"),
        QT_TRANSLATE_NOOP("Calendars", "Pausha"),
        QT_TRANSLATE_NOOP("Calendars", "Magha"),
        QT_TRANSLATE_NOOP("Calendars", "Phalguna"),
    ],
    "bangladeshi": [
        QT_TRANSLATE_NOOP("Calendars", "Boishakh"),
        QT_TRANSLATE_NOOP("Calendars", "Joishtho"),
        QT_TRANSLATE_NOOP("Calendars", "Asharh"),
        QT_TRANSLATE_NOOP("Calendars", "Shrabon"),
        QT_TRANSLATE_NOOP("Calendars", "Bhadro"),
        QT_TRANSLATE_NOOP("Calendars", "Ashwin"),
        QT_TRANSLATE_NOOP("Calendars", "Kartik"),
        QT_TRANSLATE_NOOP("Calendars", "Ogrohayon"),
        QT_TRANSLATE_NOOP("Calendars", "Poush"),
        QT_TRANSLATE_NOOP("Calendars", "Magh"),
        QT_TRANSLATE_NOOP("Calendars", "Falgun"),
        QT_TRANSLATE_NOOP("Calendars", "Chaitro"),
    ],
}
_ADAR_I = QT_TRANSLATE_NOOP("Calendars", "Adar I")
_ADAR_II = QT_TRANSLATE_NOOP("Calendars", "Adar II")
# The same name for two months: told apart by a comment, translated with it.
_COMMENTS = {("persian", 4): "a Persian month", ("ethiopian", 5): "an Ethiopian month"}


def _tr(text: object, comment: str | None = None) -> str:
    """Translated in this module's context (text: a source string, or one of
    the tables above)."""
    return QCoreApplication.translate("Calendars", str(text), comment)


def calendar_name(calendar: str) -> str:
    return _tr(_CALENDAR_NAMES.get(calendar, _CALENDAR_NAMES[GREGORIAN]))


def named_months(calendar: str) -> bool:
    """Whether the calendar's months go by name rather than number."""
    return calendar in _MONTH_NAMES


def month_text(month: int, leap: bool) -> str:
    """A month by its number, "leap" before a leap month's."""
    if leap:
        return _tr(
            QT_TRANSLATE_NOOP3("Calendars", "leap %1", "a leap month"), "a leap month"
        ).replace("%1", str(month))
    return str(month)


def month_name(calendar: str, year: int, month: int, leap: bool) -> str:
    """A month of the calendar as its users call it: by name, or by number."""
    names = _MONTH_NAMES.get(calendar)
    if names is None:
        return month_text(month, leap)
    if calendar == "hebrew" and month == 12:
        if leap:
            return _tr(_ADAR_II)
        if (12, True) in CALENDARS[calendar].months(year):
            return _tr(_ADAR_I)
    return _tr(names[month - 1], _COMMENTS.get((calendar, month)))


def _fill(pattern: str, parts: list[str]) -> str:
    # Values hold no "%": replaced one after another, they cannot be mistaken.
    for number, part in enumerate(parts, start=1):
        pattern = pattern.replace(f"%{number}", part)
    return pattern


def picked_text(calendar: str, picked: CalendarDate, with_year: bool = False) -> str:
    """A day picked in another calendar: "Lunar 8/15" (in Korean "음력 8월 15일"),
    "Hebrew Tishrei 1" ("히브리력 티슈리 1일")."""
    name = calendar_name(calendar)
    month = month_name(calendar, picked.year, picked.month, picked.leap)
    day, year = str(picked.day), str(picked.year)
    if named_months(calendar):
        if with_year:
            pattern = _tr(
                QT_TRANSLATE_NOOP3("Calendars", "%1 %2 %3, %4", "calendar, month name, day, year"),
                "calendar, month name, day, year",
            )
            return _fill(pattern, [name, month, day, year])
        return _fill(
            _tr(
                QT_TRANSLATE_NOOP3("Calendars", "%1 %2 %3", "calendar, month name, day"),
                "calendar, month name, day",
            ),
            [name, month, day],
        )
    if with_year:
        pattern = _tr(
            QT_TRANSLATE_NOOP3("Calendars", "%1 %2/%3/%4", "calendar, year, month, day"),
            "calendar, year, month, day",
        )
        return _fill(pattern, [name, year, month, day])
    return _fill(
        _tr(
            QT_TRANSLATE_NOOP3("Calendars", "%1 %2/%3", "calendar, month, day"),
            "calendar, month, day",
        ),
        [name, month, day],
    )


def change_text(calendar: str, change: Change) -> str:
    """Why a reminder rings on another day than the one picked, in a line."""
    match change:
        case Change.NO_LEAP_MONTH:
            return _tr(
                QT_TRANSLATE_NOOP(
                    "Calendars", "That year has no such leap month: it rings in the ordinary month."
                )
            )
        case Change.LAST_DAY:
            return _tr(
                QT_TRANSLATE_NOOP("Calendars", "That month is shorter: it rings on its last day.")
            )
        case Change.DAY_BEFORE:
            return _tr(
                QT_TRANSLATE_NOOP(
                    "Calendars", "%1 days can come a day early: it rings a day before."
                )
            ).replace("%1", calendar_name(calendar))


def is_other(calendar: str) -> bool:
    return calendar != GREGORIAN


# Reading a day written in brackets back (only in this language: it was written
# for people to read, in the language of the time).


def _month_pattern(calendar: str) -> tuple[str, dict[str, tuple[int, bool]]]:
    """A pattern for the calendar's months, and what each name stands for."""
    if not named_months(calendar):
        leap = re.escape(
            _tr(QT_TRANSLATE_NOOP3("Calendars", "leap %1", "a leap month"), "a leap month")
        ).replace("%1", "")
        return f"(?P<leap>{leap})?(?P<month>[0-9]{{1,2}})", {}
    names: dict[str, tuple[int, bool]] = {}
    for month in range(1, len(_MONTH_NAMES[calendar]) + 1):
        names[month_name(calendar, 1, month, False)] = (month, False)
    if calendar == "hebrew":
        names[_tr(_ADAR_I)] = (12, False)
        names[_tr(_ADAR_II)] = (12, True)
    alternatives = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return f"(?P<name>{alternatives})", names


def _read_note(calendar: str, note: str) -> tuple[int, int, bool] | None:
    """The month, day and leap month written in brackets for the calendar."""
    month, names = _month_pattern(calendar)
    day = "(?P<day>[0-9]{1,2})"
    if named_months(calendar):
        pattern = re.escape(
            _tr(
                QT_TRANSLATE_NOOP3("Calendars", "%1 %2 %3", "calendar, month name, day"),
                "calendar, month name, day",
            )
        )
    else:
        pattern = re.escape(
            _tr(
                QT_TRANSLATE_NOOP3("Calendars", "%1 %2/%3", "calendar, month, day"),
                "calendar, month, day",
            )
        )
    pattern = pattern.replace("%1", re.escape(calendar_name(calendar)), 1)
    pattern = pattern.replace("%2", month, 1).replace("%3", day, 1)
    found = re.match(pattern, note)
    if found is None:
        return None
    if names:
        number, leap = names[found["name"]]
        return number, int(found["day"]), leap
    return int(found["month"]), int(found["day"]), found["leap"] is not None


def disagrees(note: str, rings: date) -> bool:
    """Whether a day written in brackets (in this language) is not the day the
    reminder rings on, as one side was changed by hand. Unread notes agree."""
    # The longest names first: "Lunar (China)" before "Lunar".
    for calendar_id in sorted(CALENDARS, key=lambda c: len(calendar_name(c)), reverse=True):
        written = _read_note(calendar_id, note)
        if written is None:
            continue
        calendar = CALENDARS[calendar_id]
        day = rings + timedelta(days=1) if calendar.early else rings
        try:
            picked = calendar.from_gregorian(day)
        except ValueError:
            return False  # beyond the years it is worked out for: unread
        if (written[0], written[1], written[2]) == (picked.month, picked.day, picked.leap):
            return False
        # Taken on the earlier side: the last day of a shorter month, the ordinary
        # month of a leap month the year lacks.
        resolved = resolve(calendar, CalendarDate(picked.year, written[0], written[1], written[2]))
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
        note = _tr(QT_TRANSLATE_NOOP("Calendars", "%1, a day before")).replace("%1", note)
    return note
