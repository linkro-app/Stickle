"""Other calendars a reminder can be set in. A day picked there rings on the
Gregorian day it falls on, worked out each time it is needed from the
calendar's rules or the data the app carries.

A day that does not exist, or may come a day early, is taken on the earlier
side, as a reminder that comes late is of no use: a leap month that a year
does not have is the ordinary month of that number (the leap month comes
after it), a day a month does not have is the month's last day, and in a
calendar whose days are settled by sighting the moon or by local sunrise
(Islamic, Hindu) the reminder rings a day before the day worked out. Each
such change is told, with its reason.

Repeating every month or every year follows the calendar picked: every lunar
month (leap months too), or the same lunar day each year.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Protocol

GREGORIAN = "gregorian"


@dataclass(frozen=True, order=True)
class CalendarDate:
    """A day in some calendar: its year, month (and whether it is the leap
    month of that number) and day, as that calendar counts them."""

    year: int
    month: int
    day: int
    leap: bool = False

    def stored(self) -> str:
        """As kept in the reminders table: "2026-08-15", a leap month "2026-08L-15"."""
        return f"{self.year:04d}-{self.month:02d}{'L' if self.leap else ''}-{self.day:02d}"

    @classmethod
    def parse(cls, text: str) -> CalendarDate:
        """From stored(); ValueError for anything else."""
        year, month, day = text.split("-")
        leap = month.endswith("L")
        return cls(int(year), int(month.removesuffix("L")), int(day), leap)


class Calendar(Protocol):
    @property
    def id(self) -> str:
        """Kept with a reminder: never changed once released."""
        ...

    @property
    def early(self) -> bool:
        """Whether its days may come a day earlier than worked out (sighting,
        local sunrise): a reminder then rings a day before."""
        ...

    def months(self, year: int) -> list[tuple[int, bool]]:
        """The year's months in order, as (month, leap)."""
        ...

    def days_in_month(self, year: int, month: int, leap: bool) -> int: ...

    def to_gregorian(self, day: CalendarDate) -> date:
        """The Gregorian day of a day that exists."""
        ...

    def from_gregorian(self, day: date) -> CalendarDate: ...


class Change(Enum):
    """Why a reminder rings on another day than the one picked."""

    NO_LEAP_MONTH = "no leap month"  # the year has none: the ordinary month
    LAST_DAY = "last day"  # the month is shorter: its last day
    DAY_BEFORE = "day before"  # the calendar's days may come a day early


@dataclass(frozen=True)
class Resolved:
    day: date  # the Gregorian day it rings on
    changes: frozenset[Change]


def resolve(calendar: Calendar, picked: CalendarDate) -> Resolved | None:
    """The Gregorian day a day picked in the calendar rings on, taken on the
    earlier side; None if its year has no month of that number at all."""
    months = calendar.months(picked.year)
    month, leap = picked.month, picked.leap
    changes: set[Change] = set()
    if (month, leap) not in months:
        if not leap or (month, False) not in months:
            return None
        leap = False
        changes.add(Change.NO_LEAP_MONTH)
    day = picked.day
    length = calendar.days_in_month(picked.year, month, leap)
    if day > length:
        day = length
        changes.add(Change.LAST_DAY)
    rings = calendar.to_gregorian(CalendarDate(picked.year, month, day, leap))
    if calendar.early:
        rings -= timedelta(days=1)
        changes.add(Change.DAY_BEFORE)
    return Resolved(rings, frozenset(changes))


def _following_months(calendar: Calendar, picked: CalendarDate) -> Iterator[CalendarDate]:
    """The same day in each month from the one picked on, leap months too."""
    year = picked.year
    months = calendar.months(year)
    if (picked.month, picked.leap) in months:
        index = months.index((picked.month, picked.leap))
    else:  # a leap month this year lacks: on from its ordinary month
        index = months.index((picked.month, False))
    while True:
        for month, leap in months[index:]:
            yield CalendarDate(year, month, picked.day, leap)
        year += 1
        months = calendar.months(year)
        index = 0


def occurrences(
    calendar: Calendar, picked: CalendarDate, repeat: str, first: date, last: date = date.max
) -> Iterator[Resolved]:
    """The days a reminder picked in the calendar rings on, from first on (and
    up to last): once, every month or every year of that calendar."""
    if repeat == "monthly":
        candidates: Iterator[CalendarDate] = _following_months(calendar, picked)
    elif repeat == "yearly":
        candidates = (
            CalendarDate(year, picked.month, picked.day, picked.leap)
            for year in range(picked.year, 1_000_000)
        )
    else:
        candidates = iter([picked])
    for candidate in candidates:
        resolved = resolve(calendar, candidate)
        if resolved is None:
            continue  # no month of that number that year
        if resolved.day > last:
            return
        if resolved.day >= first:
            yield resolved


# The calendars


class Julian:
    """The Julian calendar, as Orthodox churches keep it: a leap year every
    fourth year, without exception."""

    id = "julian"
    early = False

    def months(self, year: int) -> list[tuple[int, bool]]:
        return [(month, False) for month in range(1, 13)]

    def days_in_month(self, year: int, month: int, leap: bool) -> int:
        if month == 2:
            return 29 if year % 4 == 0 else 28
        return 30 if month in (4, 6, 9, 11) else 31

    def to_gregorian(self, day: CalendarDate) -> date:
        return date.fromordinal(_julian_day(day.year, day.month, day.day) - _ORDINAL_OFFSET)

    def from_gregorian(self, day: date) -> CalendarDate:
        # Fliegel's inverse, for the Julian calendar.
        c = day.toordinal() + _ORDINAL_OFFSET + 32082
        d = (4 * c + 3) // 1461
        e = c - 1461 * d // 4
        m = (5 * e + 2) // 153
        return CalendarDate(d - 4800 + m // 10, m + 3 - 12 * (m // 10), e - (153 * m + 2) // 5 + 1)


# date.toordinal() + this is the Julian Day Number of the day.
_ORDINAL_OFFSET = 1721425


def _julian_day(year: int, month: int, day: int) -> int:
    """The Julian Day Number of a day of the Julian calendar."""
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    return day + (153 * m + 2) // 5 + 365 * y + y // 4 - 32083


CALENDARS: dict[str, Calendar] = {calendar.id: calendar for calendar in (Julian(),)}
