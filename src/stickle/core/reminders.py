"""When a reminder rings: its rule, and the days worked out from it.

A reminder keeps its rule (the day it starts, the time, the time zone it was
set in, how it repeats) and the day it rings next, never a list of days worked
out ahead: once it has rung and been dealt with, the next day is worked out
from the rule. The moment of a day is worked out each time it is needed, with
the time zone data the app carries (not the system's: Windows has none, and
another system's may be older), so new daylight saving rules shipped with a
new version are followed.

When the rule asks for a day a month does not have (the 31st, February 29th),
it is the month's last day; a time the clocks skip or go through twice is the
earlier moment. A reminder that comes late is of no use.
"""

import re
from calendar import monthrange
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from functools import cache
from importlib import resources
from itertools import islice
from typing import Literal, TypeGuard
from zoneinfo import ZoneInfo

type Repeat = Literal["none", "daily", "weekdays", "weekly", "monthly", "yearly"]
REPEATS: tuple[Repeat, ...] = ("none", "daily", "weekdays", "weekly", "monthly", "yearly")
MONDAY, FRIDAY = 0, 4

_ZONE_NAME = re.compile(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*")


def is_repeat(value: object) -> TypeGuard[Repeat]:
    return value in REPEATS


@dataclass(frozen=True)
class Rule:
    start: date
    at: time  # hours and minutes
    zone: str | None  # where it was set, as an IANA name; None: wherever it rings
    repeat: Repeat = "none"
    weekdays: frozenset[int] = frozenset()  # weekly: Monday 0 ... Sunday 6

    def days_of_week(self) -> frozenset[int]:
        """The days a weekly rule rings on: those chosen, or the start's."""
        return self.weekdays or frozenset({self.start.weekday()})


@cache
def _carried_zone(name: str) -> tzinfo | None:
    if not _ZONE_NAME.fullmatch(name):
        return None
    try:
        source = resources.files("tzdata.zoneinfo").joinpath(*name.split("/"))
        with source.open("rb") as file:
            return ZoneInfo.from_file(file, key=name)
    except OSError, ValueError:
        return None


def is_known_zone(name: str) -> bool:
    return _carried_zone(name) is not None


def time_zone(name: str | None, here: tzinfo | None = None) -> tzinfo:
    """A time zone by its IANA name; None, or a name the data does not know
    (one a newer version added), is here: this computer's (UTC if not given)."""
    zone = None if name is None else _carried_zone(name)
    return zone or here or UTC


def _in_month(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, monthrange(year, month)[1]))


def _months(first: date) -> Iterator[tuple[int, int]]:
    year, month = first.year, first.month
    while year <= date.max.year:
        yield year, month
        year, month = (year, month + 1) if month < 12 else (year + 1, 1)


def days(rule: Rule, first: date) -> Iterator[date]:
    """The days the rule rings on, from first on (and never before its start)."""
    first = max(first, rule.start)
    match rule.repeat:
        case "none":
            if first == rule.start:
                yield rule.start
        case "monthly":
            for year, month in _months(first):
                day = _in_month(year, month, rule.start.day)
                if day >= first:
                    yield day
        case "yearly":
            for year in range(first.year, date.max.year + 1):
                day = _in_month(year, rule.start.month, rule.start.day)
                if day >= first:
                    yield day
        case "daily" | "weekdays" | "weekly":
            wanted = {
                "daily": frozenset(range(7)),
                "weekdays": frozenset(range(MONDAY, FRIDAY + 1)),
                "weekly": rule.days_of_week(),
            }[rule.repeat]
            day = first
            while True:
                if day.weekday() in wanted:
                    yield day
                if day == date.max:
                    return
                day += timedelta(days=1)


def moment(rule: Rule, day: date, here: tzinfo | None = None) -> datetime:
    """When the rule rings on day, in UTC: the earlier moment if the clocks skip
    its time or go through it twice."""
    wall = datetime.combine(day, rule.at).replace(tzinfo=time_zone(rule.zone, here))
    return min(wall.replace(fold=fold).astimezone(UTC) for fold in (0, 1))


def first_after(rule: Rule, instant: datetime, here: tzinfo | None = None) -> date | None:
    """The first day the rule rings later than instant; None if it never does again."""
    local = instant.astimezone(time_zone(rule.zone, here)).date()
    start = local - timedelta(days=1) if local > date.min else local
    return next((day for day in days(rule, start) if moment(rule, day, here) > instant), None)


def next_day(rule: Rule, day: date) -> date | None:
    """The day the rule rings after day; None if it does not repeat."""
    if day == date.max:
        return None
    return next(days(rule, day + timedelta(days=1)), None)


def coming(rule: Rule, first: date, count: int) -> list[date]:
    """The next few days the rule rings on, from first on."""
    return list(islice(days(rule, first), count))
