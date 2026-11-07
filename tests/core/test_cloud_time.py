"""Devices go by the cloud's clock: told by the responses' Date, kept through
this computer's clock being set, and the same wherever the devices are."""

from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from stickle.core.cloud_time import SLOWEST, CloudTime, date_millis

HOUR = 3_600_000
DAY = 24 * HOUR
TRUE = 1_762_483_200_123  # what the time really is, at the start of each test


class Computer:
    """A computer's clock, which can be wrong and be set, and its clock that
    only goes on (sleep included)."""

    def __init__(self, wrong_by: int = 0) -> None:
        self.wall = TRUE + wrong_by
        self.since = 50_000

    def passes(self, millis: int) -> None:
        self.wall += millis
        self.since += millis

    def time(self, offset: int = 0) -> CloudTime:
        return CloudTime(lambda: self.since, offset, lambda: self.wall)


def date_of(true: int) -> int:
    """A Date as the cloud writes it: its time, cut down to the second."""
    return true - true % 1000


def test_before_any_response_it_is_this_computers_clock_with_the_kept_difference() -> None:
    computer = Computer(wrong_by=9 * HOUR)

    assert computer.time().now() == computer.wall
    assert computer.time(offset=-9 * HOUR).now() == TRUE


@given(wrong_by=st.integers(-400 * DAY, 400 * DAY), took=st.integers(0, SLOWEST))
def test_one_response_sets_a_wrong_clock_right(wrong_by: int, took: int) -> None:
    computer = Computer(wrong_by)
    time = computer.time()

    computer.passes(took)  # the request there and back
    time.heard(date_of(TRUE + took // 2), took)

    assert abs(time.now() - (TRUE + took)) <= 500 + took // 2 + 1


def test_two_devices_hours_apart_go_by_one_clock() -> None:
    # Seoul set right; New York's time zone set wrong and its hands moved to
    # look right: its clock is five hours off. Time zones themselves play no part.
    seoul, new_york = Computer(), Computer(wrong_by=5 * HOUR)
    seoul_time, new_york_time = seoul.time(), new_york.time()

    seoul_time.heard(date_of(TRUE), 0)
    new_york_time.heard(date_of(TRUE), 0)

    assert abs(seoul_time.now() - new_york_time.now()) <= 1


def test_the_middle_of_the_last_few_leaves_one_odd_response_out() -> None:
    computer = Computer()
    time = computer.time()

    for date in (TRUE, TRUE + 60_000, TRUE):  # one from a server a minute out
        time.heard(date_of(date), 0)

    assert abs(time.now() - TRUE) <= 1000


def test_a_slow_response_is_not_used() -> None:
    computer = Computer(wrong_by=HOUR)
    time = computer.time()

    time.heard(date_of(TRUE), SLOWEST + 1)
    time.heard(date_of(TRUE), -1)

    assert time.now() == computer.wall


def test_set_while_running_the_time_goes_on_as_it_was() -> None:
    computer = Computer(wrong_by=9 * HOUR)
    time = computer.time()
    time.heard(date_of(TRUE), 0)
    before = time.now()

    computer.wall -= 9 * HOUR  # the user sets it right
    computer.passes(10)
    set_right = time.now()
    computer.wall += 3 * HOUR  # and wrong again
    computer.passes(10)

    assert set_right - before == 10
    assert time.now() - before == 20
    assert abs(time.offset + 3 * HOUR) <= 1000  # the cloud's clock less one three hours ahead


def test_asleep_the_time_goes_on_by_the_time_slept() -> None:
    computer = Computer()
    time = computer.time()
    time.heard(date_of(TRUE), 0)
    before = time.now()

    computer.passes(8 * HOUR)  # both clocks go on while it sleeps

    assert time.now() - before == 8 * HOUR


def test_two_clocks_drifting_apart_a_little_is_not_a_clock_set() -> None:
    computer = Computer()
    time = computer.time()
    time.heard(date_of(TRUE), 0)
    offset = time.offset

    computer.passes(DAY)
    computer.wall += 2000  # the system put its clock right by two seconds over the day

    assert time.offset == offset


def test_differences_measured_before_a_clock_set_are_moved_with_it() -> None:
    computer = Computer()
    time = computer.time()
    time.heard(date_of(TRUE), 0)
    time.heard(date_of(TRUE), 0)

    computer.wall += 9 * HOUR  # set wrong
    time.heard(date_of(TRUE), 0)

    assert abs(time.now() - TRUE) <= 1000


def test_a_date_is_the_same_moment_in_any_zone_it_is_written_in() -> None:
    utc = date_millis("Sat, 07 Nov 2026 01:30:00 GMT")

    assert utc == int(datetime(2026, 11, 7, 1, 30, tzinfo=UTC).timestamp() * 1000)
    assert date_millis("Sat, 07 Nov 2026 10:30:00 +0900") == utc
    assert date_millis("Fri, 06 Nov 2026 20:30:00 -0500") == utc
    assert date_millis("Sat, 07 Nov 2026 01:30:00 -0000") == utc


def test_what_is_not_a_date_is_none() -> None:
    for header in (None, "", "yesterday", "Sat, 99 Nov 2026 01:30:00 GMT"):
        assert date_millis(header) is None
