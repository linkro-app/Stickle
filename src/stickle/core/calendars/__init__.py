"""Other calendars a reminder can be set in. A day picked there rings on the
Gregorian day it falls on, worked out each time it is needed from the
calendar's rules or the data the app carries (stickle.core.calendars.base).

Only calendars tested against published, official data are offered: their
tests are in tests/core/test_calendars.py.
"""

from stickle.core.calendars.arithmetic import Bangladeshi, Ethiopian, IndianNational, Julian
from stickle.core.calendars.base import (
    GREGORIAN,
    Calendar,
    CalendarDate,
    Change,
    Resolved,
    occurrences,
    resolve,
)
from stickle.core.calendars.lunisolar import ChineseLunar, KoreanLunar
from stickle.core.calendars.sources import Hebrew, IslamicUmmAlQura, Persian

CALENDARS: dict[str, Calendar] = {
    calendar.id: calendar
    for calendar in (
        KoreanLunar(),
        ChineseLunar(),
        Hebrew(),
        IslamicUmmAlQura(),
        Persian(),
        Ethiopian(),
        Julian(),
        IndianNational(),
        Bangladeshi(),
    )
}

__all__ = [
    "CALENDARS",
    "GREGORIAN",
    "Bangladeshi",
    "Calendar",
    "CalendarDate",
    "Change",
    "ChineseLunar",
    "Ethiopian",
    "Hebrew",
    "IndianNational",
    "IslamicUmmAlQura",
    "Julian",
    "KoreanLunar",
    "Persian",
    "Resolved",
    "occurrences",
    "resolve",
]
