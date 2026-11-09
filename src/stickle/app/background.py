"""Work that takes a while, done off the app's thread, its outcome brought back to it."""

import gc
import threading
from collections.abc import Callable

from PySide6.QtCore import QCoreApplication, QObject, Signal

from stickle.sync.runner import THREAD_NAME as SYNC_THREAD

THREAD_NAME = "stickle-work"


def _work_goes_on_elsewhere(mine: threading.Thread) -> bool:
    return any(
        thread is not mine and thread.name in (THREAD_NAME, SYNC_THREAD)
        for thread in threading.enumerate()
    )


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
        if not _work_goes_on_elsewhere(self._thread):
            gc.enable()
        self.deleteLater()
        self._then(outcome)
