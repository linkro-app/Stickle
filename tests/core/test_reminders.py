"""When reminders ring: the days a rule gives, and the moment of each."""

from calendar import isleap
from datetime import UTC, date, datetime, time, timedelta
from itertools import islice

from hypothesis import given
from hypothesis import strategies as st

from stickle.core.reminders import (
    REPEATS,
    Rule,
    coming,
    days,
    first_after,
    is_known_zone,
    moment,
    next_day,
    time_zone,
)

SEOUL = "Asia/Seoul"
NEW_YORK = "America/New_York"
NINE = time(9, 0)


def test_a_day_a_month_lacks_is_its_last() -> None:
    rule = Rule(date(2026, 1, 31), NINE, SEOUL, "monthly")
    assert coming(rule, rule.start, 4) == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
    ]


def test_february_29th_is_the_28th_in_other_years() -> None:
    rule = Rule(date(2028, 2, 29), NINE, SEOUL, "yearly")
    assert coming(rule, rule.start, 3) == [date(2028, 2, 29), date(2029, 2, 28), date(2030, 2, 28)]


def test_weekdays_skip_the_weekend() -> None:
    saturday = date(2026, 10, 31)
    rule = Rule(saturday, NINE, SEOUL, "weekdays")
    assert coming(rule, saturday, 3) == [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]


def test_weekly_rings_on_the_days_chosen_or_the_starts() -> None:
    monday = date(2026, 11, 2)
    chosen = Rule(monday, NINE, SEOUL, "weekly", frozenset({0, 3}))
    assert coming(chosen, monday, 3) == [date(2026, 11, 2), date(2026, 11, 5), date(2026, 11, 9)]
    plain = Rule(monday, NINE, SEOUL, "weekly")
    assert coming(plain, monday, 2) == [date(2026, 11, 2), date(2026, 11, 9)]


def test_once_is_once() -> None:
    rule = Rule(date(2026, 11, 2), NINE, SEOUL)
    assert coming(rule, date(2026, 1, 1), 5) == [date(2026, 11, 2)]
    assert next_day(rule, date(2026, 11, 2)) is None


def test_the_time_is_the_time_where_it_was_set() -> None:
    rule = Rule(date(2026, 11, 2), NINE, SEOUL)
    assert moment(rule, rule.start) == datetime(2026, 11, 2, 0, 0, tzinfo=UTC)


def test_a_time_the_clocks_skip_or_repeat_is_the_earlier_moment() -> None:
    # New York, 8 March 2026: 2:00 becomes 3:00, so 2:30 never comes; 1:30 EST is
    # the earlier of the two readings.
    skipped = Rule(date(2026, 3, 8), time(2, 30), NEW_YORK)
    assert moment(skipped, skipped.start) == datetime(2026, 3, 8, 6, 30, tzinfo=UTC)
    # 1 November 2026: 2:00 goes back to 1:00, so 1:30 comes twice; the first, EDT.
    twice = Rule(date(2026, 11, 1), time(1, 30), NEW_YORK)
    assert moment(twice, twice.start) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


def test_daylight_saving_keeps_the_time_of_day() -> None:
    rule = Rule(date(2026, 3, 7), NINE, NEW_YORK, "daily")
    before, after = coming(rule, rule.start, 2)
    assert moment(rule, before) == datetime(2026, 3, 7, 14, 0, tzinfo=UTC)
    assert moment(rule, after) == datetime(2026, 3, 8, 13, 0, tzinfo=UTC)


def test_zones_come_from_the_data_the_app_carries() -> None:
    assert is_known_zone(SEOUL)
    for name in ("Mars/Olympus", "../../etc/passwd", "Asia/../Asia/Seoul", ""):
        assert not is_known_zone(name)
    here = time_zone(SEOUL)
    assert time_zone("Mars/Olympus", here) is here
    assert time_zone(None) is UTC


def test_first_after_is_strictly_after() -> None:
    rule = Rule(date(2026, 11, 2), NINE, SEOUL, "daily")
    at_nine = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    assert first_after(rule, at_nine) == date(2026, 11, 4)
    assert first_after(rule, at_nine - timedelta(seconds=1)) == date(2026, 11, 3)
    assert first_after(Rule(date(2026, 11, 2), NINE, SEOUL), at_nine) is None


ZONES = st.sampled_from([SEOUL, NEW_YORK, "Europe/London", "Australia/Lord_Howe", "UTC", None])
RULES = st.builds(
    Rule,
    start=st.dates(date(1990, 1, 1), date(2100, 12, 31)),
    at=st.times().map(lambda at: at.replace(second=0, microsecond=0)),
    zone=ZONES,
    repeat=st.sampled_from(REPEATS),
    weekdays=st.frozensets(st.integers(0, 6)),
)


def _fits(rule: Rule, day: date) -> bool:
    match rule.repeat:
        case "none":
            return day == rule.start
        case "daily":
            return True
        case "weekdays":
            return day.weekday() < 5
        case "weekly":
            return day.weekday() in rule.days_of_week()
        case "monthly":
            last = (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            return day.day == min(rule.start.day, last.day)
        case "yearly":
            return (day.month, day.day) == (rule.start.month, rule.start.day) or (
                (rule.start.month, rule.start.day) == (2, 29)
                and (day.month, day.day) == (2, 28)
                and not isleap(day.year)
            )


@given(RULES, st.dates(date(1990, 1, 1), date(2100, 12, 31)))
def test_days_follow_the_rule_in_order(rule: Rule, first: date) -> None:
    found = list(islice(days(rule, first), 30))
    assert found == sorted(set(found))
    assert all(day >= max(first, rule.start) and _fits(rule, day) for day in found)
    # Nothing the rule gives is skipped between them.
    if len(found) >= 2:
        span = (found[-1] - found[0]).days
        every = [found[0] + timedelta(days=n) for n in range(span + 1)]
        assert [day for day in every if _fits(rule, day)] == found


@given(RULES, st.datetimes(datetime(1990, 1, 1), datetime(2100, 1, 1), timezones=st.just(UTC)))
def test_first_after_is_the_first_later_moment(rule: Rule, instant: datetime) -> None:
    found = first_after(rule, instant)
    # Days two before the instant's ring before it in any time zone.
    for day in days(rule, instant.date() - timedelta(days=2)):
        if found is not None and day >= found:
            break
        if moment(rule, day) > instant:
            raise AssertionError(f"{day} rings after {instant} but {found} was given")
        if day > instant.date() + timedelta(days=2):
            break
    if found is not None:
        assert moment(rule, found) > instant
