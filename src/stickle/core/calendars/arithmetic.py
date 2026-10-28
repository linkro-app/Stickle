"""Calendars worked out by their own rules, as published: nothing to carry."""

from datetime import date

from stickle.core.calendars.base import CalendarDate


def _gregorian_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


class Julian:
    """The Julian calendar, as Orthodox churches keep it: a leap year every
    fourth year, without exception."""

    id = "julian"
    early = False
    first_year = 2
    last_year = 9998

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


class Ethiopian:
    """The Ethiopian calendar (Amete Mihret): twelve months of 30 days and
    Pagume of 5, or 6 in the year before a Julian leap year (Calendrical
    Calculations, Reingold and Dershowitz)."""

    id = "ethiopian"
    early = False
    first_year = 1
    last_year = 9000

    def months(self, year: int) -> list[tuple[int, bool]]:
        return [(month, False) for month in range(1, 14)]

    def days_in_month(self, year: int, month: int, leap: bool) -> int:
        if month < 13:
            return 30
        return 6 if year % 4 == 3 else 5

    def _fixed(self, year: int, month: int, day: int) -> int:
        return _ETHIOPIAN_EPOCH - 1 + 365 * (year - 1) + year // 4 + 30 * (month - 1) + day

    def to_gregorian(self, day: CalendarDate) -> date:
        return date.fromordinal(self._fixed(day.year, day.month, day.day))

    def from_gregorian(self, day: date) -> CalendarDate:
        fixed = day.toordinal()
        year = (4 * (fixed - _ETHIOPIAN_EPOCH) + 1463) // 1461
        month = (fixed - self._fixed(year, 1, 1)) // 30 + 1
        return CalendarDate(year, month, fixed + 1 - self._fixed(year, month, 1))


# Its first day: 29 August, 8 CE, in the Julian calendar (date.toordinal() counts
# days as Calendrical Calculations' fixed dates do).
_ETHIOPIAN_EPOCH = Julian().to_gregorian(CalendarDate(8, 8, 29)).toordinal()


class IndianNational:
    """India's national calendar (Saka era), as the Positional Astronomy Centre
    publishes it: Chaitra 1 is 22 March, or 21 March in a Gregorian leap year;
    Chaitra has 30 days (31 in a leap year), the next five months 31, the rest 30."""

    id = "indian_national"
    early = False
    first_year = 1879  # 1957, when it was adopted
    last_year = 9000

    def _start(self, year: int) -> date:
        gregorian = year + 78
        return date(gregorian, 3, 21 if _gregorian_leap(gregorian) else 22)

    def months(self, year: int) -> list[tuple[int, bool]]:
        return [(month, False) for month in range(1, 13)]

    def days_in_month(self, year: int, month: int, leap: bool) -> int:
        if month == 1:
            return 31 if _gregorian_leap(year + 78) else 30
        return 31 if month <= 6 else 30

    def to_gregorian(self, day: CalendarDate) -> date:
        before = sum(self.days_in_month(day.year, m, False) for m in range(1, day.month))
        return date.fromordinal(self._start(day.year).toordinal() + before + day.day - 1)

    def from_gregorian(self, day: date) -> CalendarDate:
        year = day.year - 78 if day >= self._start(day.year - 78) else day.year - 79
        offset = (day - self._start(year)).days
        for month in range(1, 13):
            length = self.days_in_month(year, month, False)
            if offset < length:
                return CalendarDate(year, month, offset + 1)
            offset -= length
        raise ValueError(day)


class Bangladeshi:
    """Bangladesh's Bengali calendar as revised in 2019 (Bangla Academy):
    Boishakh 1 is 14 April; the first six months have 31 days, the next four 30,
    Falgun 29 (30 when its February has 29 days) and Chaitra 30."""

    id = "bangladeshi"
    early = False
    first_year = 1427  # 2020-21: the first year wholly under the revised rule
    last_year = 9000

    def _start(self, year: int) -> date:
        return date(year + 593, 4, 14)

    def months(self, year: int) -> list[tuple[int, bool]]:
        return [(month, False) for month in range(1, 13)]

    def days_in_month(self, year: int, month: int, leap: bool) -> int:
        if month <= 6:
            return 31
        if month == 11:
            return 30 if _gregorian_leap(year + 594) else 29
        return 30

    def to_gregorian(self, day: CalendarDate) -> date:
        before = sum(self.days_in_month(day.year, m, False) for m in range(1, day.month))
        return date.fromordinal(self._start(day.year).toordinal() + before + day.day - 1)

    def from_gregorian(self, day: date) -> CalendarDate:
        year = day.year - 593 if day >= self._start(day.year - 593) else day.year - 594
        offset = (day - self._start(year)).days
        for month in range(1, 13):
            length = self.days_in_month(year, month, False)
            if offset < length:
                return CalendarDate(year, month, offset + 1)
            offset -= length
        raise ValueError(day)
