"""Waiting on a cloud's word that another device changed something, on a thread
of its own, to bring the next round forward (Dropbox's long poll — a feed,
stickle.sync.targets.base).

It writes nothing and touches no database. All it does is tell that another
device changed something (told), for a round to look soon; and whether it is
waiting as it should (working), for rounds to look for others' changes only
now and then while it is (SAFETY_NET) and every few minutes while it is not
(rounds_every). A device's own file is written when it put something (or once
a day), so a feed of devices/ tells of every change once, not of each file.

The cursor is taken as it starts, before the first round looks, so nothing
changed in between goes untold; it moves on past each change seen. A cursor
too old to follow is taken anew, and a round looks, for what changed meanwhile
is not known. When in doubt (what changed cannot be read), a round looks.

A failure is tried again after a pause: half a minute, then twice as long each
time, up to five minutes (or as long as the cloud asks). It never holds up a
round. Turned down for good (signed out, the app turned away), it stops: the
rounds say so.

Nothing here logs but a failure nobody foresaw, by its kind alone.
"""

import logging
import threading
from collections.abc import Callable

from stickle.sync.schedule import EVERY
from stickle.sync.targets.base import (
    CursorExpiredError,
    Feed,
    NoAccessError,
    SignInNeededError,
    TargetError,
    TooBusyError,
)

log = logging.getLogger(__name__)

THREAD_NAME = "sync-watch"
SAFETY_NET = 60 * 60.0  # seconds between rounds while the watcher waits as it should
FIRST_PAUSE = 30.0  # seconds after a failure, then twice as long each time
LONGEST_PAUSE = 5 * 60.0
TIDY_UP = 0.5  # seconds a watcher told to stop has to do so


def rounds_every(working: bool) -> float:
    """Seconds between rounds, as the watcher waits as it should or not."""
    return SAFETY_NET if working else EVERY


class Watcher:
    def __init__(
        self,
        feed: Feed,
        told: Callable[[], object],
        working: Callable[[bool], object],
        *,
        first_pause: float = FIRST_PAUSE,
        longest_pause: float = LONGEST_PAUSE,
    ) -> None:
        """told: another device changed something; working: whether the
        watcher waits as it should, told when that changes. Both on its thread."""
        self._feed = feed
        self._told = told
        self._working = working
        self._first_pause = first_pause
        self._longest_pause = longest_pause
        self._lock = threading.Lock()
        self._ending = False
        self._again = False
        self._woken = threading.Event()  # cuts a pause short
        self._said: bool | None = None  # what working was last told
        self._thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def again(self) -> None:
        """The wait under way cut, and another begun at once: the computer woke,
        or the network changed, and it may be waiting on a connection gone."""
        with self._lock:
            self._again = True
        self._feed.cut()
        self._woken.set()

    def stop(self, seconds: float = TIDY_UP) -> bool:
        """No more requests from now on: the wait under way is cut, and one
        under way otherwise runs its course. Whether the thread has ended,
        waited for that many seconds at most."""
        with self._lock:
            self._ending = True
        self._feed.cut()
        self._woken.set()
        if self._thread.ident is not None:
            self._thread.join(seconds)
        return not self._thread.is_alive()

    def _over(self) -> bool:
        with self._lock:
            return self._ending

    def _taken_again(self) -> bool:
        with self._lock:
            again, self._again = self._again, False
            return again

    def _run(self) -> None:
        try:
            self._watch()
        except Exception as error:  # whatever it was, the rounds go on without it
            log.error("sync: watching for changes broke: %s", type(error).__name__)
            self._say(False)
        finally:
            self._feed.close()

    def _watch(self) -> None:
        cursor: str | None = None
        taken_anew = False  # a round to look once there is a cursor again
        pause = self._first_pause
        while not self._over():
            try:
                if cursor is None:
                    cursor = self._feed.latest()
                    if taken_anew:
                        taken_anew = False
                        self._tell()
                waited = self._feed.wait(cursor)
                if self._over():
                    return
                self._say(True)
                pause = self._first_pause
                if waited.changes:
                    try:
                        others, cursor = self._feed.since(cursor)
                    except CursorExpiredError:
                        raise
                    except TargetError:
                        self._tell()  # what changed is not known: a round looks
                        raise
                    if others:
                        self._tell()
                if waited.backoff:
                    self._pause(waited.backoff)
            except CursorExpiredError:
                cursor, taken_anew = None, True
            except SignInNeededError, NoAccessError:
                self._say(False)
                return
            except TargetError as error:
                if self._over():
                    return
                if self._taken_again():
                    self._woken.clear()  # set for the pause it did not cut short
                    continue
                self._say(False)
                asked = error.retry_after if isinstance(error, TooBusyError) else None
                self._pause(pause if asked is None else max(asked, pause))
                pause = min(pause * 2, self._longest_pause)

    def _pause(self, seconds: float) -> None:
        """Cut short by again() too: its cut then ends the next wait at once,
        taken for what it is."""
        self._woken.wait(seconds)
        self._woken.clear()

    def _tell(self) -> None:
        try:
            self._told()
        except Exception as error:
            log.error("sync: telling of a change failed: %s", type(error).__name__)

    def _say(self, working: bool) -> None:
        if self._said is working:
            return
        self._said = working
        try:
            self._working(working)
        except Exception as error:
            log.error("sync: telling how watching goes failed: %s", type(error).__name__)
