"""Hybrid logical clocks: when a field was changed, in an order every device
agrees on, even when their clocks do not.

A time is the clock's milliseconds and a counter, written as text of one
length ("1762483200000-0000"), so that the order of the text is the order of
the times. A field changed is given the time of the change or, if that is not
later, the time just after the one the field had: a change is always later
than the value it replaced, however far behind the clock is. That is all the
order a field needs, so a time from another device stays on its field and
does not move this device's clock: one device's wrong clock does not spread
to the others, and a clock set right goes back with it. Two fields changed at
the same time are told apart by the device that changed them
(stickle.sync.contents).
"""

import re
import time
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


END = Hlc(LARGEST_MILLIS, LARGEST_COUNTER)


def wall_millis() -> int:
    """This computer's clock, as milliseconds since 1970 in UTC."""
    return time.time_ns() // 1_000_000


def after(millis: int, before: Hlc | None) -> Hlc:
    """The time of a change made at millis to a field whose value had the time
    before (None: never changed): millis if that is later, else just after
    before. The last time of all, which only a broken file has, stays the last."""
    made = Hlc(min(max(millis, 0), LARGEST_MILLIS))
    if before is None or made > before:
        return made
    return before if before == END else before.following()
