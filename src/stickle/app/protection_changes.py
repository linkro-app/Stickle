"""Changes to what protects the notes, told to their user afterwards.

Someone at a computer left unlocked could set a master password or make a new
recovery key of their own. So each change made in the settings is noted and
told, not at once (whoever is at the screen then made it), but at the next
start of Stickle or once the computer is unlocked: a line at the top of
Stickle Manager until the user says it is fine, or, with the window closed,
one notification of the system. It is never told again after that, and never
on a schedule (no reminders). Changes made at the first start or when the
recovery key is offered once more are the user's own setting up: not noted.
"""

import logging
from dataclasses import dataclass

import apsw

from stickle.core.clock import Clock, utc_now
from stickle.data.settings import CHANGE_KINDS, PROTECTION_CHANGES, Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Change:
    what: str  # one of CHANGE_KINDS
    at: str  # when (UTC)


class ProtectionChanges:
    def __init__(self, settings: Settings, clock: Clock = utc_now) -> None:
        self._settings = settings
        self._clock = clock

    def _entries(self) -> list[dict[str, object]]:
        return [dict(entry) for entry in self._settings.get(PROTECTION_CHANGES)]

    def _keep(self, entries: list[dict[str, object]]) -> None:
        try:
            self._settings.set(PROTECTION_CHANGES, entries)
        except apsw.Error as error:  # not told, but nothing else is in the way
            log.warning("could not note a protection change: %s", type(error).__name__)

    def note(self, what: str) -> None:
        """A change just made: told later, not now."""
        assert what in CHANGE_KINDS
        self._keep([*self._entries(), {"what": what, "at": self._clock(), "told": False}])

    def due(self) -> bool:
        """Changes are to be told from now on (a start, an unlock): True if any
        were not before."""
        entries = self._entries()
        if all(entry["told"] for entry in entries):
            return False
        self._keep([{**entry, "told": True} for entry in entries])
        return True

    def to_tell(self) -> list[Change]:
        """The changes being told, the latest of each kind, oldest first."""
        latest: dict[str, Change] = {}
        for entry in self._entries():
            if entry["told"]:
                what, at = str(entry["what"]), str(entry["at"])
                latest[what] = Change(what, at)
        return sorted(latest.values(), key=lambda change: change.at)

    def seen(self) -> None:
        """The user said they are fine: those told are no longer kept."""
        self._keep([entry for entry in self._entries() if not entry["told"]])
