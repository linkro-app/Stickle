"""Days picked in other calendars: worked out to Gregorian days, on the earlier
side when the day does not exist or may come early, repeating as that
calendar does."""

from datetime import date, timedelta

from hypothesis import given
from hypothesis import strategies as st

from stickle.core.calendars import (
    CalendarDate,
    Change,
    Julian,
    occurrences,
    resolve,
)

JULIAN = Julian()


class TinyLunar:
    """A made-up lunisolar calendar: months of 29 and 30 days from 2000-01-01,
    with a leap 3rd month in even years."""

    id = "tiny"
    early = False

    def months(self, year: int) -> list[tuple[int, bool]]:
        ordinary = [(month, False) for month in range(1, 13)]
        return [*ordinary[:3], (3, True), *ordinary[3:]] if year % 2 == 0 else ordinary

    def days_in_month(self, year: int, month: int, leap: bool) -> int:
        return 29 if leap or month % 2 == 0 else 30

    def _start(self, year: int) -> date:
        day = date(2000, 1, 1)
        for y in range(2000, year):
            day += timedelta(days=sum(self.days_in_month(y, m, lp) for m, lp in self.months(y)))
        return day

    def to_gregorian(self, day: CalendarDate) -> date:
        start = self._start(day.year)
        for month, leap in self.months(day.year):
            if (month, leap) == (day.month, day.leap):
                return start + timedelta(days=day.day - 1)
            start += timedelta(days=self.days_in_month(day.year, month, leap))
        raise ValueError(day)

    def from_gregorian(self, day: date) -> CalendarDate:
        raise NotImplementedError


class Early(Julian):
    early = True


def test_stored_with_its_leap_month() -> None:
    for picked in (CalendarDate(2026, 8, 15), CalendarDate(2025, 6, 1, leap=True)):
        assert CalendarDate.parse(picked.stored()) == picked
    assert CalendarDate(2025, 6, 1, leap=True).stored() == "2025-06L-01"


def test_julian_days_as_orthodox_churches_keep_them() -> None:
    # Christmas on the Julian 25 December: 7 January since 1900 (until 2100).
    assert JULIAN.to_gregorian(CalendarDate(2026, 12, 25)) == date(2027, 1, 7)
    # The Gregorian reform: the day after Julian 4 October 1582 was 15 October.
    assert JULIAN.to_gregorian(CalendarDate(1582, 10, 5)) == date(1582, 10, 15)
    # 2100 is a leap year there, not here: the difference grows to 14 days.
    assert JULIAN.days_in_month(2100, 2, False) == 29
    assert JULIAN.to_gregorian(CalendarDate(2100, 3, 1)) == date(2100, 3, 15)


@given(st.dates(date(1, 3, 1), date(9999, 12, 31)))
def test_julian_there_and_back(day: date) -> None:
    assert JULIAN.to_gregorian(JULIAN.from_gregorian(day)) == day


def test_a_day_the_month_lacks_is_its_last_day() -> None:
    resolved = resolve(JULIAN, CalendarDate(2027, 2, 29))
    assert resolved is not None
    assert resolved.day == JULIAN.to_gregorian(CalendarDate(2027, 2, 28))
    assert resolved.changes == {Change.LAST_DAY}


def test_a_leap_month_the_year_lacks_is_the_ordinary_one() -> None:
    tiny = TinyLunar()
    resolved = resolve(tiny, CalendarDate(2001, 3, 10, leap=True))
    assert resolved is not None
    assert resolved.day == tiny.to_gregorian(CalendarDate(2001, 3, 10))
    assert resolved.changes == {Change.NO_LEAP_MONTH}
    exists = resolve(tiny, CalendarDate(2002, 3, 10, leap=True))
    assert exists is not None and exists.changes == frozenset()


def test_a_calendar_whose_days_may_come_early_rings_a_day_before() -> None:
    resolved = resolve(Early(), CalendarDate(2026, 12, 25))
    assert resolved is not None
    assert resolved.day == date(2027, 1, 6)
    assert resolved.changes == {Change.DAY_BEFORE}


def test_every_year_and_every_month_of_the_calendar() -> None:
    yearly = occurrences(JULIAN, CalendarDate(2026, 12, 25), "yearly", date(2026, 1, 1))
    assert [next(yearly).day for _ in range(3)] == [
        date(2027, 1, 7),
        date(2028, 1, 7),
        date(2029, 1, 7),
    ]
    tiny = TinyLunar()
    monthly = occurrences(tiny, CalendarDate(2002, 2, 30), "monthly", date(2000, 1, 1))
    picked = [next(monthly) for _ in range(4)]
    # The 2nd month (29 days), the 3rd, the leap 3rd (29 days), the 4th.
    assert [r.day for r in picked] == [
        tiny.to_gregorian(CalendarDate(2002, 2, 29)),
        tiny.to_gregorian(CalendarDate(2002, 3, 30)),
        tiny.to_gregorian(CalendarDate(2002, 3, 29, leap=True)),
        tiny.to_gregorian(CalendarDate(2002, 4, 29)),
    ]
    assert picked[0].changes == {Change.LAST_DAY}


def test_once_it_rings_on_its_day_only() -> None:
    once = list(occurrences(JULIAN, CalendarDate(2026, 12, 25), "none", date(2027, 1, 8)))
    assert once == []
