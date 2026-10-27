"""Timestamps are stored as ISO 8601 in UTC, whatever the user's locale."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

type Clock = Callable[[], str]


def _format(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def utc_now() -> str:
    """For example 2026-09-26T03:41:07.123Z (millisecond precision, sortable as text)."""
    return _format(datetime.now(UTC))


def days_before(stamp: str, days: int) -> str:
    """The timestamp that many days before another, in the same form."""
    return _format(moment_of(stamp) - timedelta(days=days))


def moment_of(stamp: str) -> datetime:
    """A timestamp as a moment (in UTC)."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def stamp_of(moment: datetime) -> str:
    """A moment (in any time zone) as a timestamp."""
    return _format(moment.astimezone(UTC))
