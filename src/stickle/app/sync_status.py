"""How sync stands, in words: and in the one line said wherever it is shown."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QDate, QDateTime, QElapsedTimer, QLocale, QObject, Signal

from stickle.app.times import moment_text
from stickle.sync.engine import GETTING
from stickle.sync.outcome import Stopped

if TYPE_CHECKING:
    from stickle.app.syncing import Ended

WEBDAV_NAME = "WebDAV"  # the same in every language
MANY = 20  # notes on the move beyond which the line says how far it has got
MOVING_EVERY_MS = 500  # how far it has got is said at most this often


class SyncWords(QObject):
    def stopped(self, why: Stopped, cloud: str) -> str:
        """Sync stopped until the user does something, and why."""
        match why:
            case Stopped.SIGN_IN:
                return self.tr("Sync stopped — check your server password")
            case Stopped.NO_ACCESS:
                return self.tr(
                    "Sync stopped — check the Stickle folder's permissions on the server"
                )
            case Stopped.BIG_CHANGE:
                return self.tr("Sync stopped — check a big change from another computer")
            case Stopped.VAULT_GONE:
                return self.tr("Sync stopped — the Stickle folder is gone from %1").replace(
                    "%1", cloud
                )
            case Stopped.VAULT_CHANGED:
                return self.tr("Sync stopped — the Stickle folder was made anew")
            case Stopped.CERTIFICATE:
                return self.tr("Sync stopped — the server's certificate changed")
            case Stopped.NEWER:
                return self.tr("Sync stopped — update Stickle to carry on")

    def full(self, cloud: str) -> str:
        return self.tr("Uploads stopped — your %1 is full").replace("%1", cloud)

    def synced(self, cloud: str, moment: QDateTime) -> str:
        """When it last went through; the day too, if that was not today."""
        when = (
            QLocale().toString(moment.time(), QLocale.FormatType.ShortFormat)
            if moment.date() == QDate.currentDate()
            else moment_text(moment)
        )
        return self.tr("Synced with %1 · %2").replace("%1", cloud).replace("%2", when)

    def moving(self, what: str, cloud: str, done: int, of: int) -> str:
        pattern = (
            self.tr("Downloading from %1 · %2 of %3 notes")
            if what == GETTING
            else self.tr("Uploading to %1 · %2 of %3 notes")
        )
        return (
            pattern.replace("%1", cloud)
            .replace("%2", QLocale().toString(done))
            .replace("%3", QLocale().toString(of))
        )

    def offline(self, to_put: int) -> str:
        return (
            self.tr("Offline · %n note(s) to upload", "", to_put) if to_put else self.tr("Offline")
        )

    def trying_again(self, to_put: int) -> str:
        return self.tr("%n note(s) to upload · trying again shortly", "", to_put)


class SyncStatus(QObject):
    """How sync stands, from what the rounds said, as one line of words."""

    changed = Signal()

    def __init__(
        self,
        cloud: str,
        stopped: Stopped | None,
        to_put: Callable[[], int],
        last_synced: Callable[[], QDateTime | None],
        parent: QObject | None = None,
    ) -> None:
        """stopped: as it was left; to_put: notes changed here and not in the
        cloud yet; last_synced: when a round last went through, if one has."""
        super().__init__(parent)
        self._words = SyncWords(self)
        self._cloud = cloud
        self._stopped = stopped
        self._to_put = to_put
        self._last_synced = last_synced
        self._synced = last_synced()
        self._full = False
        self._offline = False
        self._waiting = 0  # notes a round that failed, or passed over them, left to upload
        self._moving: tuple[str, int, int] | None = None
        self._told_moving = QElapsedTimer()

    @property
    def offline(self) -> bool:
        """Whether the last round had no answer at all."""
        return self._offline

    @property
    def halted(self) -> bool:
        """Whether there is something for the user to do: stopped, or uploads stopped."""
        return self._stopped is not None or self._full

    @property
    def update_carries_on(self) -> bool:
        return self._stopped is Stopped.NEWER

    def round_ended(self, ended: Ended) -> None:
        outcome = ended.outcome
        self._stopped = outcome.stopped
        self._full = outcome.full
        self._offline = outcome.offline
        self._moving = None
        self._told_moving.invalidate()
        self._synced = self._last_synced()
        self._waiting = self._to_put() if ended.wait is not None else len(set(ended.report.left))
        self.changed.emit()

    def moved(self, what: str, done: int, of: int) -> None:
        if of <= MANY:
            return
        self._moving = (what, done, of)
        if self._told_moving.isValid() and not self._told_moving.hasExpired(MOVING_EVERY_MS):
            return
        self._told_moving.start()
        self.changed.emit()

    def text(self) -> str:
        """The line; empty before anything is known of it."""
        words = self._words
        if self._stopped is not None:
            return words.stopped(self._stopped, self._cloud)
        if self._full:
            return words.full(self._cloud)
        if self._moving is not None:
            return words.moving(self._moving[0], self._cloud, *self._moving[1:])
        if self._offline:
            return words.offline(self._waiting)
        if self._waiting:
            return words.trying_again(self._waiting)
        return "" if self._synced is None else words.synced(self._cloud, self._synced)
