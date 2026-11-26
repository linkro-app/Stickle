"""Sync running alongside the app: rounds on a thread of their own
(stickle.sync.runner), on a connection of their own to the notes database, and
what they bring shown here.

Imported only when this device syncs: a run with sync off does not load it.
What it takes to reach a cloud (TLS, HTTP) is loaded by the thread, when it
first needs it, so a start is not held up by it.

With Dropbox, a watcher on its long poll (stickle.sync.watcher) runs beside the
rounds, on a thread of its own: started once a round has been through (devices/
is there then, with this device's file in it), it brings a round forward when
another device changes something, and has them look only now and then while it
works. It ends with them, as the user quits or leaves, or as sync stops until
the user signs in again.
"""

import gc
import logging
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

import apsw
from PySide6.QtCore import QDateTime, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from stickle.app.background import work_goes_on
from stickle.app.labels import mark_name
from stickle.app.notes import NoteManager
from stickle.app.reminders import ReminderKeeper
from stickle.app.sync_status import SyncStatus, cloud_name
from stickle.core.being_edited import Editing
from stickle.core.clock import Clocks, millis_at
from stickle.core.cloud_time import CloudTime
from stickle.core.labels import Mark
from stickle.core.markdown import note_title
from stickle.crypto.following import kept_pairs
from stickle.crypto.key_files import C, KeyFiles
from stickle.crypto.keyfile import KeyFileError
from stickle.crypto.sealed import SealedSlot
from stickle.crypto.secret_notes import key_number, old_numbers
from stickle.crypto.vault_keys import DataKeys
from stickle.data import sync_connection, sync_held, sync_state
from stickle.data.backups import take
from stickle.data.database import open_database
from stickle.data.notes import NoteRepository
from stickle.data.record_stamps import SETTING
from stickle.data.settings import DEVICE_NAME, SYNC_EVERY, Settings
from stickle.data.sync_connection import DROPBOX, ONEDRIVE, WEBDAV, Login
from stickle.platform.clocks import since_start
from stickle.sync import diagnostics
from stickle.sync.devices import Other, others, this_device
from stickle.sync.engine import (
    FirstLook,
    Place,
    Progress,
    Report,
    first_look,
    sync_vault,
)
from stickle.sync.format import DEVICES, item_path
from stickle.sync.keys import Follow
from stickle.sync.outcome import LONGEST_WAIT, Outcome, Stopped, after
from stickle.sync.runner import Runner, Until
from stickle.sync.schedule import EVERY, START_WAIT, Schedule
from stickle.sync.secret_key import SecretFollow, SecretOther
from stickle.sync.targets.base import (
    AsRead,
    CertificateError,
    NotThereError,
    Target,
    UnavailableError,
)

if TYPE_CHECKING:
    from stickle.sync.targets.dropbox import App as DropboxApp
    from stickle.sync.targets.onedrive import App
    from stickle.sync.watcher import Watcher

log = logging.getLogger(__name__)

QUIT_WAIT = 5.0  # seconds what is waiting to be put may take when the user quits
LEAVING_TIMEOUT = 4.0  # seconds, for each step of a request made on the way out
LOOK_AGAIN_MS = 1_000  # for the last request of a thread told to end
UNSENT_AFTER_MS = 60_000  # a note not in the cloud this long after it changed is shown so
COLLECT_EVERY_MS = 5 * 60 * 1000
WAIT_MS = 30_000  # for the app's write to end: this thread can wait
CACHE_KIB = 512
OFFSET_KEPT_WITHIN = 1_000  # ms: the cloud's clock is not written down for less
BIG_TEXT = 48 * 1024  # bytes: a note's file is a third bigger, so over 64 KB
ROWS_LOOKED_AT = 100  # notes changed at once beyond which they are not weighed
# Tables holding what goes to the cloud (those without a rowid are never told of,
# but each goes with a change to one of these).
SYNCED = frozenset(
    {
        "notes",
        "note_marks",
        "categories",
        "marks",
        "custom_colors",
        "shared_settings",
        "reminders",
        "import_records",
        "deletion_records",
    }
)
# Taking the vault's pairs in the key file B, and its secret notes' key in C, on
# the app's thread (the vault, them).
type TakeKeys = Callable[[str, Sequence[Follow], Sequence[SecretFollow | SecretOther]], object]
# Those looked at again when the app starts: it may have been dealt with since.
# The folder gone (a server's disk not there yet as it started) may be back.
LOOKED_AT_AGAIN = {Stopped.BIG_CHANGE.value, Stopped.NEWER.value, Stopped.VAULT_GONE.value}
# Stopped for these, sync goes on with a new connection (signed in anew), or once
# the user asks: nothing is watched meanwhile.
SIGNED_OUT = frozenset({Stopped.SIGN_IN, Stopped.NO_ACCESS})


def cloud_time(connection: apsw.Connection, clocks: Clocks) -> CloudTime:
    """The cloud's clock as last known, and what this run stamps things with from now on."""
    cloud = CloudTime(since_start, sync_connection.cloud_offset(connection))
    clocks.cloud = cloud.now
    return cloud


def stopped_as_kept(why: str | None) -> Stopped | None:
    try:
        return None if why is None else Stopped(why)
    except ValueError:  # by a later version, and it is not told of here
        return None


def _renewed_unkept(_token: str) -> None:
    """On the way out: the connection is let go of all the same."""


def _heard_nothing(_date: int, _took: int) -> None:
    pass


def _shown_name(mark_id: str, name: str | None) -> str:
    return mark_name(Mark(mark_id, name, "", 0))


class BackupFailedError(Exception):
    """The backup before taking in a big change could not be made: nothing of
    it was taken in, and sync stays stopped for it."""


@dataclass(frozen=True)
class Ended:
    """A round over: what it brought (as far as it got), and what comes of it."""

    report: Report
    outcome: Outcome
    certificate: bytes | None = None  # what a server showed, if that stopped it
    backup_failed: bool = False  # the big change let in, but no backup could be made first
    taken_in: bool = False  # the big change let in, backed up and taken in (the user saw it)
    accepting: bool = False  # a round asked to let the big change in (not one already going)

    @property
    def wait(self) -> float | None:
        return self.outcome.wait

    @property
    def stopped(self) -> bool:
        return self.outcome.stopped is not None

    @property
    def offline(self) -> bool:
        return self.outcome.offline

    @property
    def looks_again(self) -> bool:
        # Undone on the computer it was made on, it may let sync go on by itself.
        return self.outcome.stopped is Stopped.BIG_CHANGE


@dataclass(frozen=True)
class BigChange:
    """What a round stopped for, note by note: the first line of each note here
    (None: a secret note's), of those that would go to the trash, be deleted for
    good, or have a conflict copy made of them."""

    trash: list[str | None]
    gone: list[str | None]
    copies: list[str | None]


@dataclass
class _Rounds:
    """What runs on sync's thread."""

    store: Path
    key: bytes
    login: Login
    cloud: CloudTime
    make_target: Callable[[Callable[[int, int], None], Callable[[], None]], Target] | None = None
    over: Callable[[], bool] = lambda: False
    # Stickle as registered with Microsoft, and with Dropbox (None: as this build has it).
    onedrive: App | None = None
    dropbox: DropboxApp | None = None
    # Told each new refresh token, once it is used from now on (and kept, if the login is).
    renewed_to: Callable[[str], None] | None = None
    # Told after each round that went through, on sync's thread, of the target it
    # reached the cloud with (as made, not as the rounds use it).
    went_through: Callable[[Target], None] | None = None
    _reached: Target | None = None
    _db: apsw.Connection | None = None
    _home: threading.Thread | None = None  # the one the database is open on
    _target: Target | None = None
    failures: int = 0
    accept: bool = False  # the user lets the next round take in what it stopped for
    place: Place | None = None
    progress: Progress | None = None
    editing: Editing | None = None  # the notes being edited on screen
    held: frozenset[str] = frozenset()  # those the last round held: read again
    kept: bool = True  # the login is kept here (not yet, while turning sync on)
    _backed_up: bool = False  # this round, to take in what it stopped for
    _stopped: str | None = None
    _offset: int = 0
    _renewal: tuple[str, str] | None = None  # (old, new) refresh token, to be kept
    _renewing: threading.Lock = field(default_factory=threading.Lock)  # for those two

    def pairs(self) -> Mapping[str, SealedSlot]:
        """The key file B's pairs as they are now (looked at, never written: the
        app's thread writes it)."""
        return kept_pairs(KeyFiles(self.store.parent))

    def secret_key(self) -> Mapping[str, object] | None:
        """The key file C's slots as they are now, None without one (looked at,
        never written)."""
        return KeyFiles(self.store.parent).peek(C)

    def database(self) -> apsw.Connection:
        if self._db is None:
            self._db = open_database(self.store, self.key, wait_ms=WAIT_MS)
            self._home = threading.current_thread()
            self._db.pragma("cache_size", -CACHE_KIB)
            self._stopped = self.login.stopped
            self._offset = self.cloud.offset
        return self._db

    def _no_longer_trusted(self) -> None:
        if self.kept:
            sync_connection.no_longer_trusted(self.database())

    def _renewed(self, token: str) -> None:
        """A new refresh token from the cloud, on sync's thread (or a watcher's):
        used from now on, and kept in place of the one it renews if that is
        still the one kept (not over a connection kept since)."""
        with self._renewing:
            old = self.login
            self.login = replace(old, secret=token)
            if self.kept:
                first = old.secret if self._renewal is None else self._renewal[0]
                self._renewal = (first, token)
        self._keep_renewal()
        if self.renewed_to is not None:
            self.renewed_to(token)

    def _keep_renewal(self) -> None:
        """The refresh token renewed, kept once no transaction is open: a round
        may renew it in the middle of one, which could yet be rolled back. Only
        on the thread the database is open on: on another, as the round ends."""
        with self._renewing:
            renewal = self._renewal
        db = self._db
        if renewal is None or db is None or threading.current_thread() is not self._home:
            return
        if db.in_transaction:
            return
        try:
            sync_connection.renew(db, self.login.vault, *renewal)
        except apsw.Error as error:  # tried again as the round ends
            log.warning("sync: the refresh token is not kept yet: %s", type(error).__name__)
            return
        with self._renewing:
            if self._renewal == renewal:
                self._renewal = None
            elif self._renewal is not None:  # renewed again meanwhile: from the one kept
                self._renewal = (renewal[1], self._renewal[1])

    def _new_target(self) -> Target:
        return self._reach(self.cloud.heard, self._no_longer_trusted, renewed=self._renewed)

    def _reach(
        self,
        heard: Callable[[int, int], None],
        no_longer_trusted: Callable[[], None],
        timeout: float | None = None,
        renewed: Callable[[str], None] = _renewed_unkept,
    ) -> Target:
        login = self.login
        if self.make_target is not None:
            return self.make_target(heard, no_longer_trusted)
        if login.kind in (ONEDRIVE, DROPBOX):
            app: App | DropboxApp | None
            if login.kind == ONEDRIVE:
                from stickle.sync.targets import onedrive

                app, longest = self.onedrive or onedrive.registered(), onedrive.TIMEOUT
            else:
                from stickle.sync.targets import dropbox

                app, longest = self.dropbox or dropbox.registered(), dropbox.TIMEOUT
            if app is None:  # a build without Stickle registered: tried again later
                log.info("sync: this build cannot reach %s", cloud_name(login.kind))
                raise UnavailableError(answered=True)
            return app.target(
                login.secret,
                renewed,
                heard=heard,
                timeout=longest if timeout is None else timeout,
            )
        if login.kind != WEBDAV:
            raise ValueError(login.kind)
        from stickle.sync.targets.webdav import TIMEOUT, WebDav, parse_address

        return WebDav(
            parse_address(login.address),
            login.user,
            login.secret,
            login.certificate,
            heard=heard,
            no_longer_trusted=no_longer_trusted,
            timeout=TIMEOUT if timeout is None else timeout,
        )

    def sign_off(self, device: str) -> None:
        """As sync is left, off the thread of the rounds: this device's file gone
        from the vault, for the others to stop telling of it; then, with
        Dropbox, Stickle signed out of the account (its refresh token, about to
        be forgotten here, revoked). What cannot be done stays as it is. Only
        this device writes that file, so no condition: a server's weak tags
        would never meet one."""
        try:
            reached = self._reach(_heard_nothing, lambda: None, LEAVING_TIMEOUT)
        except Exception as error:  # the app goes on without it
            log.info("sync: this device stays listed: %s", type(error).__name__)
            return
        target = Until(reached, lambda: False)
        try:
            try:
                target.delete(item_path(DEVICES, device), AsRead(None))
            except NotThereError:
                pass
            except Exception as error:
                log.info("sync: this device stays listed: %s", type(error).__name__)
            if self.login.kind == DROPBOX:
                self._sign_out(reached)
        finally:
            target.close()

    @staticmethod
    def _sign_out(reached: Target) -> None:
        from stickle.sync.targets.dropbox import Dropbox

        if not isinstance(reached, Dropbox):
            return
        try:
            reached.revoke()
        except Exception as error:  # forgotten here all the same
            log.info("sync: still signed in to Dropbox: %s", type(error).__name__)

    def place_of(self, db: apsw.Connection) -> Place:
        if self._target is None:
            self._reached = self._new_target()
            self._target = Until(self._reached, self.over)
        login = self.login
        aside = self.store.parent / "quarantine"
        device = Settings(db).device_id()
        self.place = Place(self._target, login.vault, DataKeys(login.keys), device, aside)
        return self.place

    def repository(self, db: apsw.Connection) -> NoteRepository:
        clocks = Clocks()
        clocks.cloud = self.cloud.now
        return NoteRepository(db, clocks.stamp, clocks.millis)

    def _back_up(self) -> None:
        try:
            take(self.database(), self.store)
        except (OSError, apsw.Error) as error:  # a full disk, say
            raise BackupFailedError() from error
        self._backed_up = True

    def accept_next(self) -> None:
        """The next round takes in what it stopped for, after a backup."""
        self.accept = True

    def first_look(self) -> FirstLook | None:
        """What merging with the vault this device joins would make; None if it
        could not be looked at."""
        try:
            db = self.database()
            return first_look(
                db, self.place_of(db), self.cloud.now, self.repository(db), self.progress
            )
        except Exception as error:  # the user is told, and may try again
            log.warning("sync: could not look at the vault: %s", type(error).__name__)
            return None

    def __call__(self, full: bool) -> Ended:
        report = Report()
        error: Exception | None = None
        db: apsw.Connection | None = None
        self._backed_up = False
        accepting = self.accept
        try:
            db = self.database()
            sync_vault(
                db,
                self.place_of(db),
                self.cloud.now,
                self.repository(db),
                shown=_shown_name,
                about=this_device(Settings(db).get(DEVICE_NAME) or None),
                full=full,
                accept=self._back_up if self.accept else None,
                report=report,
                progress=self.progress,
                editing=self.editing,
                again=self.held,
                pairs=self.pairs,
                secret_key=self.secret_key,
            )
        except Exception as failed:  # told apart below, whatever it was
            error = failed
        self._keep_renewal()
        # Read again next round; a round that failed may not have got to them.
        self.held = frozenset(report.held) | (self.held if error is not None else frozenset())
        outcome = self._outcome(error, report)
        self.failures = outcome.failures
        backup_failed = isinstance(error, BackupFailedError)
        if error is None or backup_failed:  # not tried again by itself without a backup
            self.accept = False
        if db is not None:
            self._keep(db, outcome, report)
        if error is None:
            self._went_through()
        return Ended(
            report,
            outcome,
            error.certificate if isinstance(error, CertificateError) else None,
            backup_failed,
            self._backed_up,
            accepting,
        )

    def _went_through(self) -> None:
        if self.went_through is None or self._reached is None or self.over():
            return
        try:
            self.went_through(self._reached)
        except Exception as error:  # the rounds go on all the same
            log.error("sync: after a round: %s", type(error).__name__)

    def _outcome(self, error: Exception | None, report: Report) -> Outcome:
        if isinstance(error, BackupFailedError):
            log.warning("sync: no backup, so the big change was not taken in")
            return Outcome(Stopped.BIG_CHANGE)
        try:
            outcome = after(error, self.failures, report.full)
        except Exception as unknown:
            log.error("sync: a round failed: %s", type(unknown).__name__)
            return Outcome(wait=LONGEST_WAIT, failures=self.failures + 1)
        if outcome.wait is not None and outcome.failures == 1:  # once, not every few minutes
            log.info("sync: a round failed for now: %s", type(error).__name__)
        return outcome

    def _keep(self, db: apsw.Connection, outcome: Outcome, report: Report) -> None:
        """What is to last beyond this run, and the memory a round took given back
        (not once the time to end is up: what it says may have been forgotten)."""
        if self.over():
            return
        try:
            if outcome.wait is None:
                stopped = None if outcome.stopped is None else outcome.stopped.value
                if stopped != self._stopped:
                    sync_connection.set_stopped(db, stopped)
                    self._stopped = stopped
                    log.info("sync: %s", f"stopped, {stopped}" if stopped else "going on")
            offset = self.cloud.offset
            if abs(offset - self._offset) > OFFSET_KEPT_WITHIN:
                sync_connection.keep_cloud_offset(db, offset)
                self._offset = offset
            if report.changed or report.put:
                log.info(
                    "sync: %d notes changed here, %d put", len(report.changed), len(report.put)
                )
                db.pragma("shrink_memory")
        except apsw.Error as error:
            log.warning("sync: could not keep where it got to: %s", type(error).__name__)

    def close(self) -> None:
        if isinstance(self._target, Until):
            self._target.close()
        self._target = self._reached = None
        self._keep_renewal()
        if self._db is not None:
            self._db.close()
            self._db = None


def look_first(
    store: Path,
    key: bytes,
    login: Login,
    progress: Progress | None = None,
    make_target: Callable[[Callable[[int, int], None], Callable[[], None]], Target] | None = None,
    renewed: Callable[[str], None] | None = None,
    onedrive: App | None = None,
    dropbox: DropboxApp | None = None,
) -> FirstLook | None:
    """What merging this device's notes with those of the vault login opens would
    make, before anything of it is kept here: on a connection of its own to the
    notes database and to the cloud, off the app's thread. None if it could not
    be looked at. progress: told as the vault's notes are read. renewed: told a
    new refresh token, to sign in with from then on (nothing here keeps it)."""
    rounds = _Rounds(
        store,
        key,
        login,
        CloudTime(since_start, 0),
        make_target,
        kept=False,
        onedrive=onedrive,
        dropbox=dropbox,
    )
    rounds.progress = progress
    rounds.renewed_to = renewed
    try:
        return rounds.first_look()
    finally:
        rounds.close()


class Syncing(QObject):
    # A round over and what it brought shown (Ended).
    round_shown = Signal(object)
    # A round's way through the notes: what it is doing, how many so far, of how many.
    moving = Signal(str, int, int)
    # The notes that, a minute after they changed, are still not in the cloud (ids).
    unsent_changed = Signal(object)
    # The secret notes that stay here until their password is matched (ids).
    unmatched_changed = Signal(object)
    _round_ended = Signal(object)

    def __init__(
        self,
        connection: apsw.Connection,
        store: Path,
        key: bytes,
        login: Login,
        cloud: CloudTime,
        manager: NoteManager,
        reminders: ReminderKeeper | None = None,
        make_target: Callable[[Callable[[int, int], None], Callable[[], None]], Target]
        | None = None,
        start_wait: float = START_WAIT,
        take_keys: TakeKeys | None = None,
        onedrive: App | None = None,
        dropbox: DropboxApp | None = None,
    ) -> None:
        """store, key: the notes database. make_target: how to reach the cloud,
        if not as login says (given the two things to tell of what it hears).
        start_wait: seconds from now to the first round of its own. take_keys:
        what takes the vault's recovery key or master password here. onedrive,
        dropbox: Stickle as registered there, if not as this build has it."""
        super().__init__()
        self._connection = connection
        self._manager = manager
        self._reminders = reminders
        self._take_keys = take_keys
        self._rounds = _Rounds(
            store, key, login, cloud, make_target, onedrive=onedrive, dropbox=dropbox
        )
        self._rounds.progress = self.moving.emit  # sent from sync's thread: heard on this one
        self._rounds.editing = manager.editing
        self._rounds.went_through = self._watch
        # Dropbox's rounds go by how its watcher goes, not as the user set them.
        every = EVERY if login.kind == DROPBOX else Settings(connection).get(SYNC_EVERY)
        stopped = login.stopped is not None and login.stopped not in LOOKED_AT_AGAIN
        self.runner = Runner(
            Schedule(self._now(), every, stopped, start_wait),
            self._rounds,
            self._round_ended.emit,
            self._now,
            self._rounds.close,
        )
        self._rounds.over = lambda: self.runner.over
        self.cloud_name = cloud_name(login.kind)
        self.status = SyncStatus(
            login.kind,
            stopped_as_kept(login.stopped),
            self.to_put,
            self.last_synced,
            self,
        )
        self.round_shown.connect(self.status.round_ended)
        self.moving.connect(self.status.moved)
        self._leaving_as = ""
        self._finished = False
        self._let_go = False  # of what the app tells it (see _let_go_of_the_app)
        self._replaced = False  # ended for another connection (see end)
        self._unsent: frozenset[str] = frozenset()
        self._unmatched: frozenset[str] = frozenset()
        self._looks_for_unsent = False  # not before a first round has been through
        self._next_unsent = QTimer(self)
        self._next_unsent.setSingleShot(True)
        self._next_unsent.timeout.connect(self._look_for_unsent)
        self._written_rows: set[int] = set()
        self._noticing = False
        self._round_ended.connect(self._show)
        self._collector = QTimer(self)
        self._collector.setInterval(COLLECT_EVERY_MS)
        self._collector.timeout.connect(self._collect)
        self._looking_again = QTimer(self)
        self._looking_again.setSingleShot(True)
        self._looking_again.timeout.connect(self.left)
        self._watching = threading.Lock()  # for the two below
        self._watcher: Watcher | None = None
        self._watched = False  # no watcher from now on

    @staticmethod
    def _now() -> float:
        return since_start() / 1000

    def start(self) -> None:
        """Rounds as they fall due, from now on; changes made here noticed."""
        gc.disable()  # see _collect
        self._collector.start()
        self._connection.set_update_hook(self._written)
        self._manager.left_note.connect(self._left_note)
        self._manager.held_released.connect(self._held_released)
        application = QGuiApplication.instance()
        if isinstance(application, QGuiApplication):
            application.applicationStateChanged.connect(self._state_changed)
        self.runner.start()

    @staticmethod
    def _collect() -> None:
        """Python collects garbage on whichever thread allocates when it is time,
        and garbage may hold windows: Qt objects, which must not be destroyed
        anywhere but on the app's thread. While sync's thread is about, only
        the app's thread collects."""
        gc.collect()

    def _left_note(self) -> None:
        self.runner.left()

    def _held_released(self) -> None:
        self.runner.let_go()

    def _state_changed(self, state: Qt.ApplicationState) -> None:
        if state == Qt.ApplicationState.ApplicationActive:
            self.runner.focused()

    def woke(self) -> None:
        self.runner.woke()
        with self._watching:
            watcher = self._watcher
        if watcher is not None:  # its wait may be on a connection gone
            watcher.again()

    def _watch(self, target: Target) -> None:
        """On sync's thread, once a round with Dropbox has been through: its
        long poll watched from now on (if it is not already), for another
        device's change to bring a round forward, rounds looking only now and
        then while it works."""
        if self.login.kind != DROPBOX:
            return
        from stickle.sync.targets.dropbox import Dropbox
        from stickle.sync.watcher import Watcher, rounds_every

        if not isinstance(target, Dropbox):
            return
        runner = self.runner
        with self._watching:
            if self._watcher is not None or self._watched:
                return
            self._watcher = Watcher(
                target.feed(DEVICES),
                runner.changed_elsewhere,
                lambda working: runner.set_every(rounds_every(working)),
            )
            self._watcher.start()

    def _stop_watching(self) -> None:
        """No more long polls from now on, the one under way cut; rounds look
        every few minutes again."""
        with self._watching:
            self._watched = True
            watcher = self._watcher
        if watcher is not None:
            watcher.stop(0.0)
            self.runner.set_every(EVERY)

    def _still_watching(self) -> bool:
        with self._watching:
            return self._watcher is not None and self._watcher.running

    def hurry(self) -> None:
        """Send what is waiting now: the app may not go on for long (a session ending)."""
        self._notice()
        self.runner.hurry()

    def sync_now(self) -> None:
        self.runner.asked()

    # What the Connections page shows, and sets

    @property
    def login(self) -> Login:
        return self._rounds.login

    def kept_login(self) -> Login:
        """The connection as kept now: a certificate no longer needed is not in it."""
        try:
            return sync_connection.current(self._connection) or self.login
        except (apsw.Error, ValueError, KeyError, TypeError) as error:
            log.warning("sync: the connection kept does not read: %s", type(error).__name__)
            return self.login

    def to_put(self, among: Collection[str] | None = None) -> int:
        """Notes changed here and not in the cloud yet (among those, if given; not
        those that wait for their secret notes' password to be matched: they
        wait for the user)."""
        unmatched = self.unmatched()
        changed = sync_state.changed(self._connection, self.login.vault)
        return len(
            [
                note_id
                for note_id in changed
                if note_id not in unmatched and (among is None or note_id in among)
            ]
        )

    def vault_secret_key(self) -> str | None:
        """The number of the vault's secret notes' key, as a round last saw it."""
        return sync_state.secret_key(self._connection, self.login.vault)

    def unmatched(self) -> frozenset[str]:
        """Secret notes sealed with this computer's key while the vault's is
        another (or not seen yet): they stay here until the passwords are
        matched; and any sealed with a key of before, until sealed again."""
        try:
            slots = self._rounds.secret_key()
        except KeyFileError, OSError:
            slots = None
        number, old = key_number(slots), old_numbers(slots)
        held = frozenset(sync_state.sealed_here(self._connection, old))
        if number is not None and number == self.vault_secret_key():
            # Sealed with this computer's key, they go: not those of a key of before.
            return held - frozenset(sync_state.sealed_here(self._connection))
        return held

    def last_synced(self) -> QDateTime | None:
        """When a round last went through, by this computer's clock; None: none has."""
        at = sync_state.synced_at(self._connection, self.login.vault)
        return None if at is None else QDateTime.fromMSecsSinceEpoch(at - self._rounds.cloud.offset)

    def others(self) -> list[Other]:
        return others(self._connection, self.login.vault, Settings(self._connection).device_id())

    def chosen_name(self) -> str:
        return Settings(self._connection).get(DEVICE_NAME)

    @staticmethod
    def system_name() -> str:
        return this_device().name

    def this_name(self) -> str:
        return self.chosen_name() or self.system_name()

    def rename(self, name: str) -> None:
        """What the other devices call this one; empty: as its system calls it."""
        Settings(self._connection).set(DEVICE_NAME, name)
        self.runner.edited()

    def looks_every(self) -> int:
        return Settings(self._connection).get(SYNC_EVERY)

    def set_looks_every(self, seconds: int) -> None:
        Settings(self._connection).set(SYNC_EVERY, seconds)
        if self.login.kind != DROPBOX:  # its watcher sets it
            self.runner.set_every(seconds)

    # What the user decides (the windows that ask are elsewhere)

    def take_big_change(self) -> None:
        """The big change a round stopped for is taken in, after a backup."""
        self.runner.do(self._rounds.accept_next)
        self.runner.asked()

    def big_change(self) -> BigChange:
        """What the last round stopped for, as kept (nothing if it did not)."""
        lists: dict[str, list[str | None]] = {
            sync_held.TRASH: [],
            sync_held.GONE: [],
            sync_held.COPY: [],
        }
        notes = NoteRepository(self._connection)
        for each in sync_held.every(self._connection, self.login.vault):
            if each.kind not in lists:  # of a later version
                continue
            here = each.copy_of if each.kind == sync_held.COPY else each.note_id
            note = None if here is None else notes.get(here)
            if note is None:
                continue  # gone here since
            lists[each.kind].append(None if note.secret else note_title(note.body))
        return BigChange(lists[sync_held.TRASH], lists[sync_held.GONE], lists[sync_held.COPY])

    def end(self) -> None:
        """Ended at once, on the app's thread, for another connection to take its
        place: what is typed is saved, and whatever is waiting to be put goes
        with the next one (each vault keeps its own state of the notes)."""
        self._replaced = True
        self._let_go_of_the_app()
        self.finish(0.0)
        self.left()

    def _written(self, _kind: int, _database: str, table: str, row: int) -> None:
        """A row changed, on the app's connection (sync has one of its own). Nothing
        may be read here, in the middle of a write: looked at once it is over."""
        if table not in SYNCED:
            return
        if table == "notes":
            self._written_rows.add(row)
        if not self._noticing:
            self._noticing = True
            QTimer.singleShot(0, self._notice)

    def _notice(self) -> None:
        if not self._noticing:
            return
        self._noticing = False
        rows, self._written_rows = self._written_rows, set()
        self.runner.edited(self._has_big_text(rows))
        if self._looks_for_unsent and not self._next_unsent.isActive():
            self._next_unsent.start(UNSENT_AFTER_MS)

    def _has_big_text(self, rows: set[int]) -> bool:
        if not rows or len(rows) > ROWS_LOOKED_AT:
            return False
        marks = ", ".join("?" * len(rows))
        found = self._connection.execute(
            f"SELECT max(length(CAST(body AS BLOB))) FROM notes WHERE rowid IN ({marks})",
            tuple(rows),
        ).fetchall()
        biggest = found[0][0]
        return isinstance(biggest, int) and biggest > BIG_TEXT

    def _show(self, ended: Ended) -> None:
        """On the app's thread: what the round brought, on screen."""
        if self._finished:  # sent before it ended, and the app is ending too
            return
        if ended.outcome.stopped in SIGNED_OUT:
            self._stop_watching()
        report = ended.report
        if (report.follows or report.secret) and self._take_keys is not None:
            self._take_keys(self.login.vault, report.follows, report.secret)
        changed = list(dict.fromkeys(report.changed))
        if report.device and self._reminders is not None:
            self._reminders.use_device(report.device)
        records = [each for each in report.records.changed if each[0] != SETTING]
        if changed or records:
            self._manager.take_in(changed, bool(records))
            if self._reminders is not None:
                self._reminders.take_in(changed)
        taken = [item for kind, item in report.records.changed if kind == SETTING]
        if taken:
            self._manager.take_settings(taken)
        if report.held:
            self._manager.show_held(report.held)
        self._looks_for_unsent = True
        self._look_for_unsent()
        self.round_shown.emit(ended)

    def _look_for_unsent(self) -> None:
        """Which notes, a minute after they changed, are still not in the cloud;
        looked at again when the next will be that old (with none to go up, no
        timer runs)."""
        if not self._looks_for_unsent:
            return
        now = self._rounds.cloud.now()
        unsent: set[str] = set()
        soonest: int | None = None
        unmatched = self.unmatched()
        if unmatched != self._unmatched:
            self._unmatched = unmatched
            self.unmatched_changed.emit(unmatched)
        for note_id, at in sync_state.changed_at(self._connection, self.login.vault):
            if note_id in unmatched:
                continue  # told apart: they wait for the user, not the cloud
            try:
                left = millis_at(at) + UNSENT_AFTER_MS - now
            except ValueError:
                continue
            if left <= 0:
                unsent.add(note_id)
            elif soonest is None or left < soonest:
                soonest = left
        if soonest is None:
            self._next_unsent.stop()
        else:
            self._next_unsent.start(soonest)
        found = frozenset(unsent)
        if found != self._unsent:
            self._unsent = found
            self.unsent_changed.emit(found)

    def _stop_looking_for_unsent(self) -> None:
        self._looks_for_unsent = False
        self._next_unsent.stop()

    def feedback_lines(self) -> Sequence[str]:
        """What a report of a problem says of sync (none before a first round)."""
        place = self._rounds.place
        if place is None:
            return ()
        try:
            return diagnostics.lines(self._connection, place, self._rounds.failures)
        except apsw.Error as error:
            log.warning("sync: nothing to say of it: %s", type(error).__name__)
            return ()

    # Leaving, with the app going on

    def leaving(self) -> None:
        """On the app's thread, first: what is typed is saved, nothing changed
        from now on is told of, nothing is watched, and the connection is
        forgotten at once (the app quitting meanwhile, sync stays off; what is
        left to do goes with the login held here). Nothing, once another
        connection has taken its place: that one stays."""
        if self._replaced:
            return
        self._let_go_of_the_app()
        try:
            sync_connection.forget(self._connection)
        except apsw.Error as error:  # tried again once it has left
            log.error("sync: could not forget the connection: %s", type(error).__name__)

    def _let_go_of_the_app(self) -> None:
        """Once: what this sync was told of by the app, let go of."""
        if self._let_go:
            return  # another sync may have the connection's hook since
        self._let_go = True
        self._stop_watching()
        self._stop_looking_for_unsent()
        self._manager.save_all()
        self._connection.set_update_hook(None)
        self._notice()
        self._manager.left_note.disconnect(self._left_note)
        self._manager.held_released.disconnect(self._held_released)
        application = QGuiApplication.instance()
        if isinstance(application, QGuiApplication):
            application.applicationStateChanged.disconnect(self._state_changed)
        self._leaving_as = Settings(self._connection).device_id()

    def leave(self) -> None:
        """Off the app's thread: rounds end, what is waiting to be put getting
        as long as when the user quits, and this device comes off the list
        (signed out, with Dropbox). Nothing, once another connection has taken
        its place: this device is on that one's list, as that one's login."""
        if self._replaced:
            return
        self.runner.finish(QUIT_WAIT)
        self._rounds.sign_off(self._leaving_as)

    def left(self) -> None:
        """On the app's thread, last. A request still out (it takes no more
        than its own time) keeps garbage collected here until it is back."""
        if self.runner.running or self._still_watching():
            self._looking_again.start(LOOK_AGAIN_MS)
            return
        self._collector.stop()
        if not work_goes_on():
            gc.enable()

    def finish(self, seconds: float) -> None:
        """Ends it; seconds: how long what is waiting to be put may take."""
        self._finished = True
        self._stop_watching()
        self._stop_looking_for_unsent()
        self._connection.set_update_hook(None)
        self._notice()
        self._collector.stop()
        self.runner.finish(seconds)
        if not self.runner.running and not self._still_watching():
            gc.enable()

    def ended(self, seconds: float) -> bool:
        """Once finished: waits for a request still out to come back (it takes
        no more than its own time), seconds at most; whether it has. Nothing of
        sync then writes to the database any more."""
        return self.runner.join(seconds)
