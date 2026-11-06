"""Hybrid logical clocks: when a field was changed, in an order every device
agrees on, even when their clocks do not.

A time is the wall clock's milliseconds and a counter, written as text of one
length ("1762483200000-0000"), so that the order of the text is the order of
the times. A device's clock never goes back: each time it makes is after the
last one it made or saw, however far the wall clock is behind. A time seen
from another device moves the clock past it. Two fields changed at the same
time are told apart by the device that changed them (stickle.sync.contents).
The device keeps its last time, so the clock does not go back when the app
starts again either.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

MILLIS_DIGITS = 13  # milliseconds until the year 2286
COUNTER_DIGITS = 4
LARGEST_MILLIS = 10**MILLIS_DIGITS - 1
LARGEST_COUNTER = 10**COUNTER_DIGITS - 1
_TEXT = re.compile(r"[0-9]{13}-[0-9]{4}")


@dataclass(frozen=True, order=True)
class Hlc:
    millis: int
    counter: int = 0

    def __post_init__(self) -> None:
        if not (0 <= self.millis <= LARGEST_MILLIS and 0 <= self.counter <= LARGEST_COUNTER):
            raise ValueError("not a time of the clock")

    @property
    def text(self) -> str:
        return f"{self.millis:013d}-{self.counter:04d}"

    @classmethod
    def parse(cls, text: str) -> Hlc:
        if not _TEXT.fullmatch(text):
            raise ValueError("not a time of the clock")
        return cls(int(text[:MILLIS_DIGITS]), int(text[MILLIS_DIGITS + 1 :]))

    def following(self) -> Hlc:
        """The time just after this one; OverflowError after the last time of all
        (seen only from a device whose clock is wrong past the year 2286)."""
        if self.counter < LARGEST_COUNTER:
            return Hlc(self.millis, self.counter + 1)
        if self.millis == LARGEST_MILLIS:
            raise OverflowError("no time after the last one")
        return Hlc(self.millis + 1)


def wall_millis() -> int:
    return time.time_ns() // 1_000_000


class Clock:
    def __init__(self, wall: Callable[[], int] = wall_millis, last: Hlc | None = None) -> None:
        self._wall = wall
        self._last = last

    @property
    def last(self) -> Hlc | None:
        """The latest time made or seen: to keep, and give the clock again."""
        return self._last

    def now(self) -> Hlc:
        """A time for a change made now, after every one before it."""
        physical = min(max(self._wall(), 0), LARGEST_MILLIS)
        last = self._last
        made = Hlc(physical) if last is None or physical > last.millis else last.following()
        self._last = made
        return made

    def saw(self, other: Hlc) -> None:
        """A time from another device: the next one made here comes after it."""
        if self._last is None or other > self._last:
            self._last = other
