"""Work that takes a while, done off the app's thread, its outcome brought back to it."""

import gc
import threading
from collections.abc import Callable

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal

from stickle.sync.runner import THREAD_NAME as SYNC_THREAD
from stickle.sync.watcher import THREAD_NAME as WATCH_THREAD

THREAD_NAME = "stickle-work"
COLLECT_EVERY_MS = 5 * 60 * 1000  # as sync's (stickle.app.syncing)


def work_goes_on() -> bool:
    """Whether any thread of sync (its rounds, its watching), or of work like
    this, is still at it."""
    names = (THREAD_NAME, SYNC_THREAD, WATCH_THREAD)
    return any(thread.name in names for thread in threading.enumerate())


class _Collector(QObject):
    """While a job goes on, garbage collected now and then on the app's thread
    (one may wait long: a sign-in, for the browser to come back)."""

    def __init__(self) -> None:
        super().__init__(QCoreApplication.instance())
        self.jobs = 0
        self.timer = QTimer(self)
        self.timer.setInterval(COLLECT_EVERY_MS)
        self.timer.timeout.connect(gc.collect)


_collector: _Collector | None = None


def collecting() -> bool:
    """Whether garbage is collected on the app's thread now and then, for jobs."""
    return _collector is not None and _collector.timer.isActive()


def _collecting(jobs: int) -> None:
    """jobs more (or fewer) going on: collected on the app's thread while any is."""
    global _collector
    if _collector is None:
        _collector = _Collector()
    _collector.jobs += jobs
    if _collector.jobs > 0:
        if not _collector.timer.isActive():
            _collector.timer.start()
    else:
        _collector.timer.stop()


class Job(QObject):
    """One piece of work on a thread of its own. then is given what it returned,
    or the exception it raised, on the app's thread.

    Python collects garbage on whichever thread allocates when it is time, and
    garbage may hold windows, which only the app's thread may destroy: while
    the work goes on nothing is collected by itself (stickle.app.syncing)."""

    _ended = Signal(object)

    def __init__(self, work: Callable[[], object], then: Callable[[object], None]) -> None:
        super().__init__(QCoreApplication.instance())  # outlives the window that asked
        self._work = work
        self._then = then
        self._ended.connect(self._end)
        self._thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
        gc.disable()
        _collecting(1)
        self._thread.start()

    def _run(self) -> None:
        outcome: object
        try:
            outcome = self._work()
        except Exception as error:  # the app's thread says what it means
            outcome = error
        self._ended.emit(outcome)

    def drop(self, instead: Callable[[], object]) -> None:
        """The window that asked has gone: instead is called when the work ends."""

        def then(_outcome: object) -> None:
            instead()

        self._then = then

    def _end(self, outcome: object) -> None:
        self._thread.join()  # told of this as the last thing it does
        _collecting(-1)
        if not work_goes_on():
            gc.enable()
        self.deleteLater()
        self._then(outcome)
