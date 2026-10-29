"""Days picked in other calendars: worked out to Gregorian days, on the earlier
side when the day does not exist or may come early, repeating as that
calendar does."""

import csv
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from stickle.core.calendars import (
    CALENDARS,
    Bangladeshi,
    Calendar,
    CalendarDate,
    Change,
    ChineseLunar,
    Ethiopian,
    Hebrew,
    IndianNational,
    IslamicUmmAlQura,
    Julian,
    KoreanLunar,
    Persian,
    occurrences,
    resolve,
)

JULIAN = Julian()


class TinyLunar:
    """A made-up lunisolar calendar: months of 29 and 30 days from 2000-01-01,
    with a leap 3rd month in even years."""

    id = "tiny"
    early = False
    first_year = 2000
    last_year = 2100

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


# Each calendar against the data its country or keepers publish
# (tests/core/calendar_data, each file names its source on its first line).

DATA = Path(__file__).with_name("calendar_data")


def rows(name: str) -> list[dict[str, str]]:
    lines = (DATA / name).read_text(encoding="utf-8").splitlines()
    return list(csv.DictReader(line for line in lines if not line.startswith("#")))


def test_chinese_lunar_months_as_the_hong_kong_observatory_gives_them() -> None:
    chinese = ChineseLunar()
    by_year: dict[int, list[tuple[int, bool]]] = {}
    for row in rows("hko_lunar_months.csv"):
        year, month, leap = int(row["year"]), int(row["month"]), row["leap"] == "1"
        start = date.fromisoformat(row["gregorian"])
        assert chinese.to_gregorian(CalendarDate(year, month, 1, leap)) == start, row
        assert chinese.from_gregorian(start) == CalendarDate(year, month, 1, leap), row
        by_year.setdefault(year, []).append((month, leap))
    for year, months in by_year.items():
        assert chinese.months(year) == months, year
    assert len(by_year) == 200  # 1901-2100


def test_korean_lunar_days_as_kasi_gives_them() -> None:
    korean = KoreanLunar()
    found = rows("kasi_lunar_days.csv")
    assert len(found) > 150
    for row in found:
        expected = CalendarDate(
            int(row["year"]), int(row["month"]), int(row["day"]), row["leap"] == "1"
        )
        assert korean.from_gregorian(date.fromisoformat(row["gregorian"])) == expected, row


def test_korea_and_china_differ_where_their_new_moons_do() -> None:
    # Seollal 2027 is 7 February in Korea, 6 February in China (KASI, HKO).
    assert KoreanLunar().to_gregorian(CalendarDate(2027, 1, 1)) == date(2027, 2, 7)
    assert ChineseLunar().to_gregorian(CalendarDate(2027, 1, 1)) == date(2027, 2, 6)
    # 2012: a leap 3rd month in Korea, a leap 4th in China.
    assert (3, True) in KoreanLunar().months(2012)
    assert (4, True) in ChineseLunar().months(2012)


def test_korean_lunar_ends_where_kasi_does() -> None:
    korean = KoreanLunar()
    assert korean.last_year == 2050
    month, leap = korean.months(2050)[-1]
    days = korean.days_in_month(2050, month, leap)
    assert korean.to_gregorian(CalendarDate(2050, month, days, leap)) <= date(2050, 12, 31)
    yearly = occurrences(korean, CalendarDate(2048, 8, 15), "yearly", date(2048, 1, 1))
    assert [r.day.year for r in yearly] == [2048, 2049, 2050]


def test_umm_al_qura_months_as_van_gent_gives_them() -> None:
    islamic = IslamicUmmAlQura()
    checked = 0
    for row in rows("umm_al_qura_months.csv"):
        year, month = int(row["year"]), int(row["month"])
        if islamic.first_year <= year <= islamic.last_year:
            start = date.fromisoformat(row["gregorian"])
            assert islamic.to_gregorian(CalendarDate(year, month, 1)) == start, row
            checked += 1
    assert checked > 1700
    # Ramadan 1448 begins 8 February 2027: a reminder for its first day rings on the 7th.
    resolved = resolve(islamic, CalendarDate(1448, 9, 1))
    assert resolved is not None
    assert (resolved.day, resolved.changes) == (date(2027, 2, 7), {Change.DAY_BEFORE})


def test_persian_new_year_as_the_university_of_tehran_gives_it() -> None:
    persian = Persian()
    checked = 0
    for row in rows("nowruz.csv"):
        year = int(row["year"])
        if persian.first_year <= year <= persian.last_year:
            nowruz = date.fromisoformat(row["gregorian"])
            assert persian.to_gregorian(CalendarDate(year, 1, 1)) == nowruz, row
            checked += 1
    assert checked == 199  # 1300-1498


HEBCAL_MONTHS = {
    "Nisan": (1, False),
    "Iyyar": (2, False),
    "Sivan": (3, False),
    "Tamuz": (4, False),
    "Av": (5, False),
    "Elul": (6, False),
    "Tishrei": (7, False),
    "Cheshvan": (8, False),
    "Kislev": (9, False),
    "Tevet": (10, False),
    "Sh'vat": (11, False),
    "Adar": (12, False),
    "Adar I": (12, False),
    "Adar II": (12, True),
}


def test_hebrew_days_as_hebcal_gives_them() -> None:
    hebrew = Hebrew()
    found = rows("hebcal_days.csv")
    assert len(found) == 900
    for row in found:
        month, leap = HEBCAL_MONTHS[row["month"].replace(chr(0x2019), "'")]
        expected = CalendarDate(int(row["year"]), month, int(row["day"]), leap)
        assert hebrew.from_gregorian(date.fromisoformat(row["gregorian"])) == expected, row


def test_ethiopian_new_year_on_11_or_12_september() -> None:
    # 12 September before a Gregorian leap year (this holds until 2099).
    ethiopian = Ethiopian()
    for year in range(1901, 2099):
        new_year = ethiopian.from_gregorian(date(year, 9, 20))  # well into its new year
        leap_next = (year + 1) % 4 == 0
        expected = date(year, 9, 12 if leap_next else 11)
        assert ethiopian.to_gregorian(CalendarDate(new_year.year, 1, 1)) == expected, year
    # Ethiopian Christmas (Genna, 29 Tahsas 2019) is 7 January 2027.
    assert ethiopian.to_gregorian(CalendarDate(2019, 4, 29)) == date(2027, 1, 7)


def test_indian_national_calendar_by_its_published_rule() -> None:
    indian = IndianNational()
    for saka in range(1879, 2100):
        year = saka + 78
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        chaitra = date(year, 3, 21 if leap else 22)
        assert indian.to_gregorian(CalendarDate(saka, 1, 1)) == chaitra
        # Vaisakha 1 follows a Chaitra of 30 days, or 31 in a leap year.
        assert indian.to_gregorian(CalendarDate(saka, 2, 1)) == date(year, 4, 21)


def test_bangladeshi_calendar_keeps_its_national_days() -> None:
    # The revision keeps these on the same days every year (Bangla Academy, 2019).
    bangla = Bangladeshi()
    for year in range(2021, 2100):
        assert bangla.from_gregorian(date(year, 4, 14)) == CalendarDate(year - 593, 1, 1)
        assert bangla.from_gregorian(date(year, 2, 14)) == CalendarDate(year - 594, 11, 1)
        assert bangla.from_gregorian(date(year, 2, 21)) == CalendarDate(year - 594, 11, 8)
        assert bangla.from_gregorian(date(year, 3, 26)) == CalendarDate(year - 594, 12, 12)
        assert bangla.from_gregorian(date(year, 12, 16)) == CalendarDate(year - 593, 9, 1)


def _first_day(calendar: Calendar) -> date:
    month, leap = calendar.months(calendar.first_year)[0]
    return calendar.to_gregorian(CalendarDate(calendar.first_year, month, 1, leap))


@pytest.mark.parametrize("calendar", list(CALENDARS.values()), ids=list(CALENDARS))
def test_every_day_there_and_back(calendar: Calendar) -> None:
    """Sixty years of days from 1990 (or its first year), each to the calendar
    and back, within the month it is given."""
    day = max(_first_day(calendar), date(1990, 1, 1))
    end = min(day + timedelta(days=366 * 60), date(2100, 12, 31))
    if calendar.id == "korean_lunar":
        end = min(end, date(2050, 12, 1))
    if calendar.id == "islamic_umm_al_qura":
        end = min(end, date(2077, 11, 1))
    while day <= end:
        picked = calendar.from_gregorian(day)
        assert calendar.to_gregorian(picked) == day, (calendar.id, day)
        assert 1 <= picked.day <= calendar.days_in_month(picked.year, picked.month, picked.leap)
        day += timedelta(days=1)


def test_a_calendars_library_is_loaded_only_once_a_date_is_worked_out_in_it() -> None:
    # lunar_python and pyluach take 3 MB together, which most people never need.
    code = (
        "import sys\n"
        "from stickle.core.calendars import CALENDARS, CalendarDate\n"
        "print(sorted(n for n in ('lunar_python', 'pyluach') if n in sys.modules))\n"
        "CALENDARS['chinese_lunar'].to_gregorian(CalendarDate(2026, 1, 1))\n"
        "print(sorted(n for n in ('lunar_python', 'pyluach') if n in sys.modules))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    ).stdout.splitlines()

    assert out == ["[]", "['lunar_python']"]
