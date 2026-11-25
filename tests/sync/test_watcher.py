"""Waiting on a cloud's word of a change (stickle.sync.watcher), on a feed the
test plays: what it tells, how it pauses after a failure, and how it stops.
The real feed, on Dropbox in the test, is in test_dropbox_round."""

import threading
import time
from collections import deque
from collections.abc import Callable, Iterator

import pytest

from stickle.sync.schedule import EVERY
from stickle.sync.targets.base import (
    CursorExpiredError,
    NoAccessError,
    SignInNeededError,
    TooBusyError,
    UnavailableError,
    Waited,
)
from stickle.sync.watcher import SAFETY_NET, Watcher, rounds_every

SOON = 5.0  # seconds a test waits for what should come at once, at most

type Step = Waited | str | tuple[bool, str] | Exception


class Feed:
    """A feed whose every answer the test gives, in turn; a wait with none to
    give waits for one, or to be cut."""

    def __init__(self) -> None:
        self.calls: list[str] = []  # what was asked, and what was told, in order
        self.times: list[float] = []  # when each wait began
        self.closed = False
        self._answers: dict[str, deque[Step]] = {
            "latest": deque(),
            "wait": deque(),
            "since": deque(),
        }
        self._told = threading.Condition()
        self._cut = False

    def give(self, call: str, *answers: Step) -> None:
        with self._told:
            self._answers[call].extend(answers)
            self._told.notify_all()

    def note(self, what: str) -> None:
        with self._told:
            self.calls.append(what)
            self._told.notify_all()

    def until(self, done: Callable[[], bool], seconds: float = SOON) -> bool:
        with self._told:
            return self._told.wait_for(done, seconds)

    def asked(self, call: str) -> int:
        return sum(1 for each in self.calls if each.startswith(call))

    def _next(self, call: str, *, cuttable: bool = False) -> Step:
        with self._told:
            while True:
                if cuttable and self._cut:
                    self._cut = False
                    raise UnavailableError()
                if self._answers[call]:
                    step = self._answers[call].popleft()
                    if isinstance(step, Exception):
                        raise step
                    return step
                self._told.wait()

    def latest(self) -> str:
        self.note("latest")
        step = self._next("latest")
        assert isinstance(step, str)
        return step

    def wait(self, cursor: str) -> Waited:
        self.times.append(time.monotonic())
        self.note(f"wait {cursor}")
        step = self._next("wait", cuttable=True)
        assert isinstance(step, Waited)
        return step

    def since(self, cursor: str) -> tuple[bool, str]:
        self.note(f"since {cursor}")
        step = self._next("since")
        assert isinstance(step, tuple)
        return step

    def cut(self) -> None:
        with self._told:
            self._cut = True
            self._told.notify_all()

    def close(self) -> None:
        self.closed = True


class Watching:
    """A watcher on the test's feed, and what it told."""

    def __init__(self, feed: Feed, first_pause: float = 0.05, longest_pause: float = 0.2) -> None:
        self.feed = feed
        self.working: list[bool] = []
        self.watcher = Watcher(
            feed,
            lambda: feed.note("told"),
            self.said,
            first_pause=first_pause,
            longest_pause=longest_pause,
        )

    def said(self, working: bool) -> None:
        self.working.append(working)
        self.feed.note(f"working {working}")


@pytest.fixture
def feed() -> Feed:
    return Feed()


@pytest.fixture
def watching(feed: Feed) -> Iterator[Watching]:
    made = Watching(feed)
    yield made
    assert made.watcher.stop(SOON)


def waits(feed: Feed, count: int) -> bool:
    return feed.until(lambda: feed.asked("wait") >= count)


def test_a_change_another_device_made_is_told(feed: Feed, watching: Watching) -> None:
    feed.give("latest", "c1")
    feed.give("wait", Waited(True, None))
    feed.give("since", (True, "c2"))

    watching.watcher.start()

    assert waits(feed, 2)
    assert feed.calls == [
        "latest",
        "told",
        "wait c1",
        "working True",
        "since c1",
        "told",
        "wait c2",
    ]


def test_a_round_looks_once_the_first_cursor_is_taken(feed: Feed, watching: Watching) -> None:
    feed.give("latest", "c1")

    watching.watcher.start()

    # The rounds may have looked before it was there: what changed then is a round's to find.
    assert waits(feed, 1)
    assert feed.calls == ["latest", "told", "wait c1"]


def test_its_own_change_is_not_told(feed: Feed, watching: Watching) -> None:
    feed.give("latest", "c1")
    feed.give("wait", Waited(True, None))
    feed.give("since", (False, "c2"))

    watching.watcher.start()

    assert waits(feed, 2)
    assert feed.calls.count("told") == 1 and feed.calls[-1] == "wait c2"  # the cursor's


def test_nothing_changed_tells_nothing(feed: Feed, watching: Watching) -> None:
    feed.give("latest", "c1")
    feed.give("wait", Waited(False, None), Waited(False, None))

    watching.watcher.start()

    assert waits(feed, 3)
    assert feed.calls == ["latest", "told", "wait c1", "working True", "wait c1", "wait c1"]


def test_a_failure_pauses_twice_as_long_each_time_up_to_the_longest(
    feed: Feed, watching: Watching
) -> None:
    feed.give("latest", "c1")
    feed.give("wait", *[UnavailableError() for _ in range(4)], Waited(False, None))
    feed.give("wait", UnavailableError())

    watching.watcher.start()

    assert waits(feed, 7)
    gaps = [after - before for before, after in zip(feed.times, feed.times[1:], strict=False)]
    for gap, pause in zip(gaps, [0.05, 0.1, 0.2, 0.2, 0.0, 0.05], strict=True):
        assert gap >= pause * 0.9, gaps
    assert gaps[3] < 0.2 + 2.0
    assert watching.working == [False, True, False]  # told only when it changes


def test_failing_from_the_first_cursor_on(feed: Feed, watching: Watching) -> None:
    feed.give("latest", UnavailableError(), "c1")
    feed.give("wait", Waited(False, None))

    watching.watcher.start()

    assert waits(feed, 2)
    assert feed.calls[:5] == ["latest", "working False", "latest", "told", "wait c1"]


def test_too_busy_waits_as_long_as_asked(feed: Feed, watching: Watching) -> None:
    feed.give("latest", "c1")
    feed.give("wait", TooBusyError(0.4), Waited(False, None))

    watching.watcher.start()

    assert waits(feed, 3)
    assert feed.times[1] - feed.times[0] >= 0.4 * 0.9


def test_the_pause_dropbox_asks_for_is_kept(feed: Feed, watching: Watching) -> None:
    feed.give("latest", "c1")
    feed.give("wait", Waited(False, 0.4), Waited(False, None))

    watching.watcher.start()

    assert waits(feed, 3)
    assert feed.times[1] - feed.times[0] >= 0.4 * 0.9
    assert watching.working == [True]


def test_a_cursor_too_old_is_taken_anew_and_a_round_looks(feed: Feed, watching: Watching) -> None:
    feed.give("latest", "c1", "c9")
    feed.give("wait", CursorExpiredError())

    watching.watcher.start()

    assert waits(feed, 2)
    # Told once the new cursor is there: what changes after the round's look is not missed.
    assert feed.calls == ["latest", "told", "wait c1", "latest", "told", "wait c9"]


def test_what_changed_unread_is_told_in_doubt(feed: Feed, watching: Watching) -> None:
    feed.give("latest", "c1")
    feed.give("wait", Waited(True, None), Waited(True, None))
    feed.give("since", UnavailableError(), (False, "c2"))

    watching.watcher.start()

    assert waits(feed, 3)
    assert feed.calls[:7] == [
        "latest",
        "told",
        "wait c1",
        "working True",
        "since c1",
        "told",
        "working False",
    ]
    assert feed.calls[7:] == ["wait c1", "working True", "since c1", "wait c2"]


@pytest.mark.parametrize("call", ["latest", "since"])
@pytest.mark.parametrize("error", [SignInNeededError, NoAccessError])
def test_turned_down_for_good_it_stops(
    feed: Feed, watching: Watching, call: str, error: type[Exception]
) -> None:
    if call == "latest":
        feed.give("latest", error())
    else:
        feed.give("latest", "c1")
        feed.give("wait", Waited(True, None))
        feed.give("since", error())

    watching.watcher.start()

    assert feed.until(lambda: not watching.watcher.running)
    assert watching.working[-1] is False and feed.closed
    assert feed.asked("wait") <= 1


def test_stopping_cuts_the_wait_and_asks_nothing_more(feed: Feed, watching: Watching) -> None:
    feed.give("latest", "c1")
    watching.watcher.start()
    assert waits(feed, 1)

    began = time.monotonic()
    assert watching.watcher.stop(SOON)

    assert time.monotonic() - began < 1
    assert feed.calls == ["latest", "told", "wait c1"] and feed.closed


def test_stopping_during_a_pause_ends_it(feed: Feed) -> None:
    watching = Watching(feed, first_pause=30.0)
    feed.give("latest", "c1")
    feed.give("wait", UnavailableError())
    watching.watcher.start()
    assert feed.until(lambda: "working False" in feed.calls)

    began = time.monotonic()
    assert watching.watcher.stop(SOON)

    assert time.monotonic() - began < 1
    assert feed.asked("wait") == 1


def test_stopping_one_never_started_is_nothing(feed: Feed) -> None:
    assert Watching(feed).watcher.stop()
    assert feed.calls == []


def test_again_cuts_the_wait_and_waits_anew_at_once(feed: Feed, watching: Watching) -> None:
    feed.give("latest", "c1")
    watching.watcher.start()
    assert waits(feed, 1)

    watching.watcher.again()

    assert waits(feed, 2)
    assert feed.calls == ["latest", "told", "wait c1", "wait c1"]  # no pause, nothing told
    assert feed.times[1] - feed.times[0] < 1


def test_again_during_a_pause_waits_at_once(feed: Feed) -> None:
    watching = Watching(feed, first_pause=30.0)
    feed.give("latest", "c1")
    feed.give("wait", UnavailableError())
    watching.watcher.start()
    assert feed.until(lambda: "working False" in feed.calls)

    watching.watcher.again()

    assert waits(feed, 2)
    feed.give("wait", Waited(False, None))
    assert feed.until(lambda: "working True" in feed.calls)
    assert watching.watcher.stop(SOON)


def test_a_telling_that_fails_does_not_stop_it(feed: Feed) -> None:
    def failing() -> None:
        raise RuntimeError("no")

    watcher = Watcher(feed, failing, lambda _working: None, first_pause=0.05)
    feed.give("latest", "c1")
    feed.give("wait", Waited(True, None))
    feed.give("since", (True, "c2"))

    watcher.start()

    assert waits(feed, 2)
    assert watcher.stop(SOON)


def test_something_nobody_foresaw_ends_it_and_says_so(
    feed: Feed, watching: Watching, caplog: pytest.LogCaptureFixture
) -> None:
    feed.give("latest", "c1")
    feed.give("wait", Waited(True, None))
    feed.give("since", RuntimeError("a path, say"))

    watching.watcher.start()

    assert feed.until(lambda: not watching.watcher.running)
    assert watching.working == [True, False] and feed.closed
    assert [each.getMessage() for each in caplog.records] == [
        "sync: watching for changes broke: RuntimeError"
    ]


def test_rounds_look_now_and_then_while_it_works() -> None:
    assert rounds_every(True) == SAFETY_NET == 60 * 60
    assert rounds_every(False) == EVERY
