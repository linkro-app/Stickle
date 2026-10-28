"""Calendars worked out by libraries carried with the app: Hebrew (pyluach),
Islamic as Saudi Arabia's Umm al-Qura (hijridate) and Persian (persiantools,
the 33-year rule, which gives the official Nowruz until 2119)."""

from datetime import date
from functools import cache
from typing import Any

import hijridate
import persiantools.jdatetime
import pyluach.dates  # pyright: ignore[reportMissingTypeStubs]
import pyluach.hebrewcal  # pyright: ignore[reportMissingTypeStubs]

from stickle.core.calendars.base import CalendarDate

_HebrewDate: Any = pyluach.dates.HebrewDate
_GregorianDate: Any = pyluach.dates.GregorianDate
_HebrewYear: Any = pyluach.hebrewcal.Year
_JalaliDate: Any = persiantools.jdatetime.JalaliDate

# pyluach counts months from Nisan (1) and gives a leap year's Adar II 13: here
# Adar II is the leap month after Adar I (12), so that a day of Adar picked in
# an ordinary year falls in Adar I, the earlier, in a leap year.
_ADAR_II = 13


class Hebrew:
    id = "hebrew"
    early = False
    first_year = 5660  # 1899
    last_year = 6000  # 2240

    def months(self, year: int) -> list[tuple[int, bool]]:
        return _hebrew_months(year)

    def days_in_month(self, year: int, month: int, leap: bool) -> int:
        return _hebrew_length(year, month, leap)

    def to_gregorian(self, day: CalendarDate) -> date:
        month = _ADAR_II if day.leap else day.month
        found: date = _HebrewDate(day.year, month, day.day).to_pydate()
        return found

    def from_gregorian(self, day: date) -> CalendarDate:
        hebrew = _GregorianDate.from_pydate(day).to_heb()
        month = int(hebrew.month)
        if month == _ADAR_II:
            return CalendarDate(int(hebrew.year), 12, int(hebrew.day), leap=True)
        return CalendarDate(int(hebrew.year), month, int(hebrew.day))


@cache
def _hebrew_months(year: int) -> list[tuple[int, bool]]:
    # Its year starts in Tishrei (7).
    leap = bool(_HebrewYear(year).leap)
    autumn = [(month, False) for month in range(7, 13)]
    spring = [(month, False) for month in range(1, 7)]
    return [*autumn, *([(12, True)] if leap else []), *spring]


@cache
def _hebrew_length(year: int, month: int, leap: bool) -> int:
    first = Hebrew().to_gregorian(CalendarDate(year, month, 1, leap))
    months = _hebrew_months(year)
    index = months.index((month, leap))
    if index + 1 < len(months):
        following = CalendarDate(year, *months[index + 1][:1], 1, months[index + 1][1])
    else:
        following = CalendarDate(year + 1, 7, 1)
    return (Hebrew().to_gregorian(following) - first).days


class IslamicUmmAlQura:
    """Saudi Arabia's Umm al-Qura calendar, 1343-1500 AH (1924-2077). Its months
    begin, in practice, on sighting the moon, often a day earlier: a reminder
    rings a day before."""

    id = "islamic_umm_al_qura"
    early = True
    first_year = 1343
    last_year = 1500

    def months(self, year: int) -> list[tuple[int, bool]]:
        return [(month, False) for month in range(1, 13)]

    def days_in_month(self, year: int, month: int, leap: bool) -> int:
        return hijridate.Hijri(year, month, 1).month_length()

    def to_gregorian(self, day: CalendarDate) -> date:
        return hijridate.Hijri(day.year, day.month, day.day).to_gregorian()

    def from_gregorian(self, day: date) -> CalendarDate:
        try:
            hijri = hijridate.Gregorian(day.year, day.month, day.day).to_hijri()
        except OverflowError as error:
            raise ValueError(day) from error
        return CalendarDate(hijri.year, hijri.month, hijri.day)


class Persian:
    """Iran's official Solar Hijri calendar, by the 33-year rule: it gives the
    University of Tehran's Nowruz dates for 1206-1498 (1827-2119)."""

    id = "persian"
    early = False
    first_year = 1300
    last_year = 1498

    def months(self, year: int) -> list[tuple[int, bool]]:
        return [(month, False) for month in range(1, 13)]

    def days_in_month(self, year: int, month: int, leap: bool) -> int:
        return int(_JalaliDate.days_in_month(month, year))

    def to_gregorian(self, day: CalendarDate) -> date:
        found: date = _JalaliDate(day.year, day.month, day.day).to_gregorian()
        return found

    def from_gregorian(self, day: date) -> CalendarDate:
        jalali = _JalaliDate.to_jalali(day)
        return CalendarDate(int(jalali.year), int(jalali.month), int(jalali.day))
