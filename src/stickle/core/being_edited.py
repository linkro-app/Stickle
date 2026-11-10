"""The notes being edited on screen, for sync to keep its hands off what is
being typed: the app says when an edit of a note begins and ends, and sync's
thread asks while it takes a note in.

Both go through one lock. A note's edit begins with the note marked, and then
its text read again, to be shown if one came in just before: taking a note in
happens with the lock held, so it is either over before the mark (its text
read then) or sees the mark. Neither falls between the other's steps.
"""

import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager


class Editing:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._notes: set[str] = set()

    def begin(self, note_id: str) -> None:
        """The note is being edited from now on (waiting, if a note is being
        taken in, until that is over)."""
        with self._lock:
            self._notes.add(note_id)

    def end(self, note_id: str) -> None:
        with self._lock:
            self._notes.discard(note_id)

    @contextmanager
    def taking(self) -> Generator[Callable[[str], bool]]:
        """While a note is taken in: whether a note is being edited, no edit
        beginning or ending meanwhile."""
        with self._lock:
            yield self._notes.__contains__


@contextmanager
def nobody_editing() -> Generator[Callable[[str], bool]]:
    """As Editing.taking, where no note is ever edited (tests, a round with no app)."""
    yield lambda _note_id: False
