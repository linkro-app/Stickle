"""When a round of sync is due. It keeps no clock and runs nothing: whoever runs
the rounds says what happened and when (in seconds of a clock that only goes
on), and asks when the next one is due.

- What changed here goes five seconds after the last change; a note too big to
  send again and again (its file over 64 KB), when it is left or a minute after.
- What changed elsewhere is looked for every few minutes (set by the user), when
  asked, when the computer wakes, and when the app comes back to the front, if
  the last look is at least half a minute old.
- A round that failed for now is tried again after as long as it says; until
  then nothing else starts one but the user asking.
- A round that stopped until the user does something: nothing starts another,
  but the user asking.
- A full look (stickle.sync.engine) is due first of all, when asked, and a day
  after the last one that went through.
"""

import math

EDIT_WAIT = 5.0  # seconds
BIG_EDIT_WAIT = 60.0
EVERY = 5 * 60.0
FOCUS_AT_LEAST = 30.0
DAY = 24 * 60 * 60.0
START_WAIT = 2.0  # after the app starts, for it not to be held up by the first round


class Schedule:
    def __init__(
        self,
        now: float,
        every: float = EVERY,
        stopped: bool = False,
        start_wait: float = START_WAIT,
    ) -> None:
        """stopped: it was left stopped until the user does something."""
        self._every = every
        self._stopped = stopped
        self._edited_at: float | None = None  # when what changed here is due
        self._big_edited_at: float | None = None  # the same, for a big note's
        self._look_at = now + start_wait  # when what changed elsewhere is due
        self._soon_at: float | None = None  # wanted at once (woke, came to the front)
        self._not_before = -math.inf  # the last round failed for now: wait for this
        self._failing = False
        self._asked = False
        self._looked = now  # when a round last went through (or it started)
        self._full_due = True  # the first round's, a failed one's, or one asked for
        self._last_full = now
        self._running_full = False

    @property
    def waiting_to_put(self) -> bool:
        """Whether something changed here since the last round began."""
        return self._edited_at is not None or self._big_edited_at is not None

    @property
    def held(self) -> bool:
        """Whether rounds are not going through for now: stopped, or failing."""
        return self._stopped or self._failing

    def edited(self, now: float, big: bool = False) -> None:
        """Something changed here; big: a big note's text."""
        if big:
            self._big_edited_at = now + BIG_EDIT_WAIT
        else:
            self._edited_at = now + EDIT_WAIT

    def left(self, now: float) -> None:
        """A note was left: if a big one is waiting, now is its time."""
        if self._big_edited_at is not None:
            self._big_edited_at = now

    def hurry(self, now: float) -> None:
        """What is waiting to be put goes now: the app may not go on for long."""
        if self._edited_at is not None:
            self._edited_at = now
        if self._big_edited_at is not None:
            self._big_edited_at = now

    def focused(self, now: float) -> None:
        """The app came back to the front."""
        if self._edited_at is not None:
            self._edited_at = now
        if now - self._looked >= FOCUS_AT_LEAST:
            self._soon_at = now

    def woke(self, now: float) -> None:
        """The computer woke from sleep."""
        self._soon_at = now

    def asked(self, now: float) -> None:
        """The user asked for a round now: whatever held it back, and a full look."""
        self._asked = True
        self._not_before = now
        self._full_due = True

    def set_every(self, every: float) -> None:
        """Rounds this far apart from now on (from the end of the last one)."""
        self._every = every
        self._look_at = min(self._look_at, self._looked + every)

    def next_at(self) -> float | None:
        """When a round is due; None: not until something happens."""
        if self._asked:
            return self._not_before
        if self._stopped:
            return None
        due = min(
            at
            for at in (self._edited_at, self._big_edited_at, self._soon_at, self._look_at)
            if at is not None
        )
        return max(due, self._not_before)

    def begin(self, now: float) -> bool:
        """A round begins: whether it is to be a full look."""
        self._asked = False
        self._edited_at = self._big_edited_at = self._soon_at = None
        self._running_full = self._full_due or now - self._last_full >= DAY
        return self._running_full

    def ended(self, now: float, wait: float | None = None, stopped: bool = False) -> None:
        """The round ended. wait: it failed for now, to be tried again after that
        many seconds; stopped: it stopped until the user does something."""
        self._stopped = stopped
        self._failing = wait is not None
        if wait is not None:
            self._look_at = now + wait
            if not self._asked:  # asked for while it ran: that still goes first
                self._not_before = self._look_at
            return
        self._not_before = -math.inf
        self._look_at = now + self._every
        if stopped:
            return
        self._looked = now
        if self._running_full:
            self._full_due = False
            self._last_full = now
