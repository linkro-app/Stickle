"""Korean and Chinese lunar calendars: months that start on a new moon, with a
leap month some years, worked out from each country's own time (Korea's new
moons fall a day later than China's in some months).

Korean dates come from the Korea Astronomy and Space Science Institute's data,
as carried by korean-lunar-calendar: 1900 to 2050, as far as KASI publishes.
Chinese dates are worked out by lunar_python, astronomically, and checked
against the Hong Kong Observatory's tables for 1901-2100; where a new moon
falls within seconds of midnight the two can differ, and the Observatory's
day is taken.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache
from typing import Any

import korean_lunar_calendar  # pyright: ignore[reportMissingTypeStubs]
import lunar_python  # pyright: ignore[reportMissingTypeStubs]

from stickle.core.calendars.base import CalendarDate

_KoreanLunarCalendar: Any = korean_lunar_calendar.KoreanLunarCalendar
_LunarYear: Any = lunar_python.LunarYear

# date.toordinal() of the day whose Julian Day Number is JDN: JDN - this.
_JDN_OFFSET = 1721425


@dataclass(frozen=True)
class Month:
    month: int
    leap: bool
    first: date
    length: int


class MonthTable:
    """A lunisolar calendar given as the months of each year: where each starts
    and how many days it has."""

    id = ""
    early = False
    first_year = 0
    last_year = 0

    def year(self, year: int) -> list[Month]:
        raise NotImplementedError

    def _month(self, year: int, month: int, leap: bool) -> Month:
        for found in self.year(year):
            if (found.month, found.leap) == (month, leap):
                return found
        raise ValueError(f"no month {month}{'L' if leap else ''} in {year}")

    def months(self, year: int) -> list[tuple[int, bool]]:
        return [(m.month, m.leap) for m in self.year(year)]

    def days_in_month(self, year: int, month: int, leap: bool) -> int:
        return self._month(year, month, leap).length

    def to_gregorian(self, day: CalendarDate) -> date:
        return self._month(day.year, day.month, day.leap).first + timedelta(days=day.day - 1)

    def from_gregorian(self, day: date) -> CalendarDate:
        for year in (day.year, day.year - 1):
            if not self.first_year <= year <= self.last_year:
                continue
            for month in self.year(year):
                offset = (day - month.first).days
                if 0 <= offset < month.length:
                    return CalendarDate(year, month.month, offset + 1, month.leap)
        raise ValueError(f"{day} is beyond the years worked out")


def _with_lengths(starts: list[tuple[int, bool, date]], following: date | None) -> list[Month]:
    """Months from their starts and the start of the next year's first month
    (None: unknown, and the last month is left out)."""
    months: list[Month] = []
    ends = [first for _, _, first in starts[1:]] + [following]
    for (month, leap, first), end in zip(starts, ends, strict=True):
        if end is not None:
            months.append(Month(month, leap, first, (end - first).days))
    return months


class KoreanLunar(MonthTable):
    id = "korean_lunar"
    first_year = 1900
    last_year = 2050  # KASI publishes no further (its last months in 2050 only)

    def year(self, year: int) -> list[Month]:
        return _korean_year(year)


def _korean_start(year: int, month: int, leap: bool) -> date | None:
    calendar = _KoreanLunarCalendar()
    if not calendar.setLunarDate(year, month, 1, leap):
        return None
    return date(int(calendar.solarYear), int(calendar.solarMonth), int(calendar.solarDay))


@cache
def _korean_year(year: int) -> list[Month]:
    starts: list[tuple[int, bool, date]] = []
    for month in range(1, 13):
        for leap in (False, True):
            first = _korean_start(year, month, leap)
            if first is not None:
                starts.append((month, leap, first))
    return _with_lengths(starts, _korean_start(year + 1, 1, False))


class ChineseLunar(MonthTable):
    id = "chinese_lunar"
    first_year = 1901
    last_year = 2100

    def year(self, year: int) -> list[Month]:
        return _chinese_year(year)


# Where the Hong Kong Observatory's tables start a month on another day than
# lunar_python works out: a new moon seconds from midnight (2057-09-28 23:59:57
# in China's time by Meeus' method, which the Observatory follows).
_HKO_STARTS: dict[tuple[int, int, bool], date] = {
    (2057, 9, False): date(2057, 9, 28),
}


def _chinese_starts(year: int) -> list[tuple[int, bool, date]]:
    starts: list[tuple[int, bool, date]] = []
    for month in _LunarYear.fromYear(year).getMonths():
        if int(month.getYear()) != year:
            continue
        number = int(month.getMonth())
        first = date.fromordinal(int(month.getFirstJulianDay()) - _JDN_OFFSET)
        key = (
            abs(number),
            number < 0,
        )
        starts.append((key[0], key[1], _HKO_STARTS.get((year, *key), first)))
    return starts


@cache
def _chinese_year(year: int) -> list[Month]:
    following = _chinese_starts(year + 1)
    return _with_lengths(_chinese_starts(year), following[0][2] if following else None)
