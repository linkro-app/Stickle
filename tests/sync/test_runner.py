"""The thread sync's rounds run on."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from fake_cloud import WEBDAV, FakeCloud

from stickle.sync.runner import Runner, Until
from stickle.sync.schedule import Schedule
from stickle.sync.targets.base import Absent, AsRead, UnavailableError


@dataclass
class Turned:
    full: bool = False
    wait: float | None = None
    stopped: bool = False


@dataclass
class Work:
    """Rounds as asked for, each ended as the test says."""

    turns: list[Turned] = field(default_factory=list[Turned])  # those ended
    fail_with: Exception | None = None
    block: threading.Event | None = None  # rounds wait for this
    began: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(0))
    next_turn: Turned = field(default_factory=Turned)

    def __call__(self, full: bool) -> Turned:
        self.began.release()
        if self.block is not None:
            self.block.wait(5)
        if self.fail_with is not None:
            raise self.fail_with
        turn = Turned(full, self.next_turn.wait, self.next_turn.stopped)
        self.turns.append(turn)
        return turn


def runner_of(
    work: Work,
    told: list[Turned] | None = None,
    closing: Callable[[], object] = lambda: None,
    stopped: bool = False,
    start_wait: float = 3600.0,
) -> Runner[Turned]:
    """Rounds at once when asked; none by themselves unless start_wait is short."""
    schedule = Schedule(time.monotonic(), every=3600.0, stopped=stopped, start_wait=start_wait)
    seen = [] if told is None else told
    return Runner(schedule, work, seen.append, time.monotonic, closing)


def began(work: Work, count: int = 1) -> bool:
    return all(work.began.acquire(timeout=5) for _ in range(count))


def test_a_round_comes_when_it_is_due() -> None:
    work, told = Work(), list[Turned]()
    runner = runner_of(work, told, start_wait=0.0)
    runner.start()
    assert began(work)
    runner.finish()
    assert told and told[0].full


def test_nothing_due_nothing_runs() -> None:
    work = Work()
    runner = runner_of(work)
    runner.start()
    assert not work.began.acquire(timeout=0.2)
    runner.finish()
    assert work.turns == []


def test_a_change_wakes_it_up_from_a_long_wait() -> None:
    work = Work()
    runner = runner_of(work)
    runner.start()
    runner.asked()
    assert began(work)
    runner.finish()


def test_a_round_that_breaks_does_not_end_the_thread() -> None:
    work = Work(fail_with=RuntimeError("no"))
    runner = runner_of(work)
    runner.start()
    runner.asked()
    assert began(work)
    work.fail_with = None
    runner.asked()  # asking does not wait out the failure
    assert began(work)
    runner.finish()
    assert len(work.turns) == 1


def test_being_told_of_a_round_failing_does_not_end_the_thread() -> None:
    work = Work()

    def told(_turn: Turned) -> None:
        raise RuntimeError("no")

    runner = Runner(
        Schedule(time.monotonic(), start_wait=3600.0), work, told, time.monotonic, lambda: None
    )
    runner.start()
    runner.asked()
    assert began(work)
    time.sleep(0.05)
    runner.asked()
    assert began(work)
    runner.finish()


def test_ending_closes_on_the_thread_itself() -> None:
    work, closed = Work(), list[str]()
    runner = runner_of(work, closing=lambda: closed.append(threading.current_thread().name))
    runner.start()
    runner.finish()
    assert closed == ["sync"]


def test_ending_with_something_waiting_takes_one_more_round() -> None:
    work = Work()
    runner = runner_of(work)
    runner.start()
    runner.edited()
    runner.finish(5.0)
    assert len(work.turns) == 1


def test_ending_with_nothing_waiting_takes_no_time() -> None:
    work = Work()
    runner = runner_of(work)
    runner.start()
    started = time.monotonic()
    runner.finish(5.0)
    assert time.monotonic() - started < 1.0
    assert work.turns == []


def test_ending_right_away_sends_nothing_more() -> None:
    work = Work()
    runner = runner_of(work)
    runner.start()
    runner.edited()
    runner.finish(0.0)
    assert work.turns == []


def test_ending_does_not_try_what_is_failing() -> None:
    work = Work(next_turn=Turned(wait=120.0))
    runner = runner_of(work)
    runner.start()
    runner.asked()
    assert began(work)
    time.sleep(0.05)
    runner.edited()
    runner.finish(5.0)
    assert len(work.turns) == 1


def test_ending_does_not_try_what_is_stopped() -> None:
    work = Work()
    runner = runner_of(work, stopped=True)
    runner.start()
    runner.edited()
    runner.finish(5.0)
    assert work.turns == []


def test_ending_waits_for_the_round_under_way_for_as_long_as_it_was_given() -> None:
    block = threading.Event()
    work = Work(block=block)
    runner = runner_of(work)
    runner.start()
    runner.asked()
    assert began(work)
    started = time.monotonic()
    runner.finish(0.3)
    waited = time.monotonic() - started
    assert 0.25 < waited < 2.0
    assert runner.over
    block.set()


def test_it_says_when_the_time_to_end_is_up() -> None:
    runner = runner_of(Work())
    assert not runner.over
    runner.start()
    runner.finish()
    assert runner.over


def test_a_job_runs_on_the_thread_before_the_next_round() -> None:
    work, order = Work(), list[str]()

    def job() -> None:
        order.append(threading.current_thread().name)

    runner = runner_of(work)
    runner.do(job)
    runner.start()
    runner.asked()
    assert began(work)
    runner.finish()
    assert order == ["sync"]


def test_a_job_that_fails_does_not_end_the_thread() -> None:
    work = Work()

    def job() -> None:
        raise RuntimeError("no")

    runner = runner_of(work)
    runner.do(job)
    runner.start()
    runner.asked()
    assert began(work)
    runner.finish()


def test_a_full_look_is_passed_on() -> None:
    work = Work()
    runner = runner_of(work)
    runner.start()
    runner.asked()
    assert began(work)
    runner.finish()
    assert work.turns[0].full


def test_a_target_lets_requests_through_until_it_is_time() -> None:
    cloud, over = FakeCloud(WEBDAV), [False]
    target = Until(cloud.target("a"), lambda: over[0])
    assert target.keeps_conditions and not target.unique_tags
    target.put("vault.json", b"{}", Absent())
    assert target.get("vault.json").data == b"{}"
    assert [entry.path for entry in target.list("notes")] == []
    over[0] = True
    with pytest.raises(UnavailableError):
        target.get("vault.json")
    with pytest.raises(UnavailableError):
        target.put("vault.json", b"{}", AsRead(None))
    with pytest.raises(UnavailableError):
        target.delete("vault.json", AsRead(None))
    with pytest.raises(UnavailableError):
        target.list("notes")


def test_a_target_is_closed_if_it_can_be() -> None:
    closed: list[bool] = []
    target = Until(FakeCloud(WEBDAV).target("a"), lambda: False)
    target.close()  # one that cannot: nothing happens
    inner = FakeCloud(WEBDAV).target("a")
    inner.close = lambda: closed.append(True)  # pyright: ignore[reportAttributeAccessIssue]
    Until(inner, lambda: False).close()
    assert closed == [True]
