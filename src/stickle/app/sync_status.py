"""How sync stands, in words: and in the one line said wherever it is shown."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QDate, QDateTime, QElapsedTimer, QLocale, QObject, Signal

from stickle.app.times import moment_text
from stickle.data.settings import CLOUD_FULL
from stickle.platform.updates import SOFTWARE_CENTER, kept_by
from stickle.sync.engine import GETTING
from stickle.sync.outcome import Stopped

if TYPE_CHECKING:
    from stickle.app.syncing import Ended

WEBDAV_NAME = "WebDAV"  # the same in every language
MANY = 20  # notes on the move beyond which the line says how far it has got
MOVING_EVERY_MS = 500  # how far it has got is said at most this often
# Stopped for these, a window asks what to do (stickle.app.sync_stopped).
ASKING = frozenset(
    {Stopped.BIG_CHANGE, Stopped.VAULT_GONE, Stopped.VAULT_CHANGED, Stopped.CERTIFICATE}
)


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

    def told(self, why: Stopped, cloud: str) -> str:
        """The sentence telling once that sync stopped, and why."""
        match why:
            case Stopped.SIGN_IN:
                said = self.tr(
                    "The server didn't accept the password, so sync stopped. If you changed"
                    " it on the server, enter the new one."
                )
            case Stopped.NO_ACCESS:
                said = self.tr(
                    "This account can no longer write to the Stickle folder on the server, so"
                    " sync stopped. Your notes are still on this computer. Fix its permissions"
                    " on the server, then choose Sync now under Settings, Connections."
                )
            case Stopped.NEWER:
                said = self.tr(
                    "A newer Stickle on another computer changed the file format, so sync"
                    " waits until you update. Your notes are still on this computer."
                )
            case Stopped.BIG_CHANGE:
                said = self.tr("Another computer changed many notes at once, so sync stopped.")
            case Stopped.VAULT_GONE:
                said = self.tr("The Stickle folder is gone from %1, so sync stopped.")
            case Stopped.VAULT_CHANGED:
                said = self.tr("The Stickle folder in %1 was made anew, so sync stopped.")
            case Stopped.CERTIFICATE:
                said = self.tr("The server's certificate changed, so sync stopped.")
        return said.replace("%1", cloud)

    def update_elsewhere(self) -> str:
        """Said after that, where the software center keeps Stickle up to date."""
        return self.tr("Update it in your software center.")

    def full_told(self, cloud: str) -> str:
        return self.tr(
            "%1 is full, so uploads stopped. Changes from your other computers still come"
            " in. Free up space and it carries on."
        ).replace("%1", cloud)

    def way_on(self, why: Stopped) -> str:
        """The button beside what says that sync stopped, and why: where it leads."""
        match why:
            case Stopped.SIGN_IN:
                return self.tr("&Enter the password…")
            case Stopped.NO_ACCESS:
                return self.tr("&Open connection settings")
            case Stopped.NEWER:  # G is for getting changes, on the Connections page
                return self.tr("Get the ne&w version…")
            case _:
                return self.tr("&Review…")

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

    @property
    def stopped(self) -> Stopped | None:
        """Why sync stopped until the user does something, if it did."""
        return self._stopped

    @property
    def asks(self) -> str | None:
        """Why sync stopped, if a window of its own asks what to do (Stopped's value)."""
        return self._stopped.value if self._stopped in ASKING else None

    @property
    def asks_password(self) -> bool:
        """Whether it stopped for the server turning down the password."""
        return self._stopped is Stopped.SIGN_IN

    @property
    def told(self) -> str | None:
        """What is told once: why sync stopped (Stopped's value), or uploads did
        (CLOUD_FULL); None: neither."""
        if self._stopped is not None:
            return self._stopped.value
        return CLOUD_FULL if self._full else None

    def notice(self) -> str:
        """The sentence telling once of what told says; empty if nothing."""
        words = self._words
        if self._stopped is None:
            return words.full_told(self._cloud) if self._full else ""
        said = words.told(self._stopped, self._cloud)
        if self._stopped is Stopped.NEWER and kept_by() == SOFTWARE_CENTER:
            said = f"{said} {words.update_elsewhere()}"
        return said

    def way_on(self) -> str:
        """The button beside that sentence; empty if there is none (OK alone)."""
        if self._stopped is None:
            return ""
        if self._stopped is Stopped.NEWER and kept_by() == SOFTWARE_CENTER:
            return ""  # updated where Stickle cannot lead
        return self._words.way_on(self._stopped)

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
