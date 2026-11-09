"""The thread sync's rounds run on, one at a time, when they are due
(stickle.sync.schedule). Everything it is told comes from the app's thread and
is only noted: nothing here waits for the network but the thread itself.

What a round does is given (work): this runs it and keeps going whatever it
raises. Work to do with what sync reads and writes, but outside of a round (a
first look at a vault, say), is given to do() and runs between rounds.
"""

import logging
import threading
from collections import deque
from collections.abc import Callable, Iterator
from typing import Protocol

from stickle.sync.schedule import Schedule
from stickle.sync.targets.base import (
    AsRead,
    Changes,
    Condition,
    Entry,
    Read,
    Target,
    UnavailableError,
)

log = logging.getLogger(__name__)

LONGEST_WAIT = 5 * 60.0  # after a round that broke in a way nobody foresaw
TIDY_UP = 0.2  # seconds a thread told to end has to do so, past what it was given


class Turn(Protocol):
    """How a round ended, as far as the next one is concerned."""

    @property
    def wait(self) -> float | None:
        """Failed for now: seconds to the next try."""
        ...

    @property
    def stopped(self) -> bool:
        """Stopped until the user does something."""
        ...


class Until:
    """A target that makes no more requests once over says so: the app is ending.
    The request under way, if any, runs its course."""

    def __init__(self, target: Target, over: Callable[[], bool]) -> None:
        self._target = target
        self._over = over

    def _check(self) -> None:
        if self._over():
            raise UnavailableError("ending")

    @property
    def keeps_conditions(self) -> bool:
        return self._target.keeps_conditions

    @property
    def unique_tags(self) -> bool:
        return self._target.unique_tags

    def list(self, folder: str) -> Iterator[Entry]:
        self._check()
        return self._target.list(folder)

    def get(self, path: str) -> Read:
        self._check()
        return self._target.get(path)

    def put(self, path: str, data: bytes, condition: Condition) -> str | None:
        self._check()
        return self._target.put(path, data, condition)

    def delete(self, path: str, condition: AsRead) -> None:
        self._check()
        self._target.delete(path, condition)

    def changes(self, cursor: str | None) -> Iterator[Changes] | None:
        self._check()
        return self._target.changes(cursor)

    def wait_for_change(self, cursor: str, seconds: float) -> bool | None:
        self._check()
        return self._target.wait_for_change(cursor, seconds)

    def close(self) -> None:
        close = getattr(self._target, "close", None)
        if callable(close):
            close()


class Runner[T: Turn]:
    def __init__(
        self,
        schedule: Schedule,
        work: Callable[[bool], T],
        ended: Callable[[T], object],
        clock: Callable[[], float],
        closing: Callable[[], object] = lambda: None,
    ) -> None:
        """work: one round (full: a full look); ended: told of it, on the thread;
        clock: seconds that only go on, sleep included; closing: on the thread, as
        it ends."""
        self._schedule = schedule
        self._work = work
        self._ended = ended
        self._clock = clock
        self._closing = closing
        self._told = threading.Condition()
        self._jobs: deque[Callable[[], object]] = deque()
        self._ending = False
        self._last_round = False  # one more, for what is waiting to be put, as it ends
        self._over_at: float | None = None
        self._thread = threading.Thread(target=self._run, name="sync", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def edited(self, big: bool = False) -> None:
        self._tell(lambda now: self._schedule.edited(now, big))

    def left(self) -> None:
        self._tell(self._schedule.left)

    def hurry(self) -> None:
        self._tell(self._schedule.hurry)

    def focused(self) -> None:
        self._tell(self._schedule.focused)

    def woke(self) -> None:
        self._tell(self._schedule.woke)

    def asked(self) -> None:
        self._tell(self._schedule.asked)

    def set_every(self, every: float) -> None:
        self._tell(lambda _now: self._schedule.set_every(every))

    def do(self, job: Callable[[], object]) -> None:
        """job on the thread, before the next round."""
        with self._told:
            self._jobs.append(job)
            self._told.notify_all()

    @property
    def running(self) -> bool:
        """Whether the thread is still at it."""
        return self._thread.is_alive()

    @property
    def over(self) -> bool:
        """Whether the time it was given to end is up: no more requests."""
        return self._over_at is not None and self._clock() >= self._over_at

    def finish(self, seconds: float = 0.0) -> None:
        """Ends the thread. seconds: how long it may go on, for what is waiting
        to be put, if rounds are going through; what is not put by then waits for
        the next run. Returns once it has ended, or at the latest after that."""
        with self._told:
            self._ending = True
            self._last_round = seconds > 0 and (
                self._schedule.waiting_to_put and not self._schedule.held
            )
            self._over_at = self._clock() + seconds
            self._told.notify_all()
        if self._thread.is_alive():
            self._thread.join(seconds + TIDY_UP)

    def _tell(self, what: Callable[[float], object]) -> None:
        with self._told:
            what(self._clock())
            self._told.notify_all()

    def _run(self) -> None:
        try:
            while True:
                job, full = self._next()
                if job is not None:
                    self._do(job)
                elif full is None:
                    return
                else:
                    self._round(full)
        finally:
            try:
                self._closing()
            except Exception as error:
                log.warning("sync did not end cleanly: %s", type(error).__name__)

    def _next(self) -> tuple[Callable[[], object] | None, bool | None]:
        """What to do next, once it is time: a job, or a round (whether full);
        neither: the thread is to end."""
        with self._told:
            while True:
                if self._jobs and not self._ending:
                    return self._jobs.popleft(), None
                now = self._clock()
                if self._ending:
                    if self._last_round:
                        self._last_round = False
                        return None, self._schedule.begin(now)
                    return None, None
                at = self._schedule.next_at()
                if at is not None and at <= now:
                    return None, self._schedule.begin(now)
                self._told.wait(None if at is None else at - now)

    def _do(self, job: Callable[[], object]) -> None:
        try:
            job()
        except Exception as error:
            log.error("sync: work beside the rounds failed: %s", type(error).__name__)

    def _round(self, full: bool) -> None:
        try:
            turn = self._work(full)
        except Exception as error:  # the thread goes on, whatever it was
            log.error("sync: a round broke: %s", type(error).__name__)
            with self._told:
                self._schedule.ended(self._clock(), LONGEST_WAIT)
            return
        with self._told:
            self._schedule.ended(self._clock(), turn.wait, turn.stopped)
        try:
            self._ended(turn)
        except Exception as error:
            log.error("sync: telling of a round failed: %s", type(error).__name__)
