"""The time every device of a vault goes by: this computer's clock, set right
by how far the cloud's clock is from it.

Every response from the cloud says the cloud's time (Date, in GMT, to the
second). How far that is from this computer's clock is measured from them and
added to it, so that the devices syncing a vault all go by the cloud's clock:
which of two changes was later is decided by one clock, not by each device's
own. It is the order that must agree, not the time that must be exact: a home
server whose clock is wrong still gives every device the same one. No request
is made for it. Time zones play no part: these are milliseconds since 1970 in
UTC, wherever the device is.

The difference is the middle one of the last few measured, each from a
response that came back soon (one slow to come leaves its Date that much
less certain). When this computer's clock is set (by hand, by the system) the
difference is put right at once, so that the time goes on as it was: the move
is told from time passing by a clock that only goes on, sleep included
(stickle.platform.clocks). Before the first response, and with sync off, the
difference is the one given (none, or the last one kept).
"""

import threading
from collections.abc import Callable
from datetime import UTC
from email.utils import parsedate_to_datetime

from stickle.core.hlc import wall_millis

SAMPLES = 5
SLOWEST = 5_000  # ms: a response slower to come than this is not used
# This computer's clock moved this much more or less than time passed: it was
# set. One part in a thousand more for the time between, which two clocks can
# drift apart by.
SET = 1_000


def date_millis(header: str | None) -> int | None:
    """A response's Date header as milliseconds since 1970, None if it is not one."""
    if not header:
        return None
    try:
        moment = parsedate_to_datetime(header)
    except TypeError, ValueError, IndexError:
        return None
    if moment.tzinfo is None:  # "-0000": the standard's GMT all the same
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


class CloudTime:
    def __init__(
        self,
        since_start: Callable[[], int],
        offset: int = 0,
        wall: Callable[[], int] = wall_millis,
    ) -> None:
        """since_start: milliseconds of a clock that only goes on, sleep included;
        offset: the difference kept from before; wall: this computer's clock."""
        self._since_start = since_start
        self._wall = wall
        self._offset = offset
        self._samples: list[int] = []
        self._lock = threading.Lock()
        self._read = (wall(), since_start())

    @property
    def offset(self) -> int:
        """The cloud's clock less this computer's, in milliseconds: to keep."""
        with self._lock:
            self._computer()
            return self._offset

    def now(self) -> int:
        """The time now, by the cloud's clock as far as it is known."""
        with self._lock:
            return self._computer() + self._offset

    def heard(self, date: int, took: int) -> None:
        """A response's Date (as milliseconds) as the response comes, took: the
        milliseconds from sending the request."""
        if not 0 <= took <= SLOWEST:
            return
        with self._lock:
            computer = self._computer()
            # The Date was written while the request was at the cloud, about
            # halfway, and is cut down to its second.
            cloud = date + 500 + took // 2
            self._samples = [*self._samples[-(SAMPLES - 1) :], cloud - computer]
            self._offset = sorted(self._samples)[len(self._samples) // 2]

    def _computer(self) -> int:
        """This computer's clock now; the difference put right if it was set
        since it was last read."""
        wall, since = self._wall(), self._since_start()
        last_wall, last_since = self._read
        passed = since - last_since
        moved = (wall - last_wall) - passed
        if abs(moved) > SET + abs(passed) // 1000:
            self._offset -= moved
            self._samples = [sample - moved for sample in self._samples]
        self._read = (wall, since)
        return wall
