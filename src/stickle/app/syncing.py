"""Sync running alongside the app: rounds on a thread of their own
(stickle.sync.runner), on a connection of their own to the notes database, and
what they bring shown here.

Imported only when this device syncs: a run with sync off does not load it.
What it takes to reach a cloud (TLS, HTTP) is loaded by the thread, when it
first needs it, so a start is not held up by it.
"""

import gc
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import apsw
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from stickle.app.labels import mark_name
from stickle.app.notes import NoteManager
from stickle.app.reminders import ReminderKeeper
from stickle.core.clock import Clocks
from stickle.core.cloud_time import CloudTime
from stickle.core.labels import Mark
from stickle.crypto.vault_keys import DataKeys
from stickle.data import sync_connection
from stickle.data.backups import take
from stickle.data.database import open_database
from stickle.data.notes import NoteRepository
from stickle.data.settings import SYNC_EVERY, Settings
from stickle.data.sync_connection import WEBDAV, Login
from stickle.platform.clocks import since_start
from stickle.sync import diagnostics
from stickle.sync.devices import this_device
from stickle.sync.engine import (
    FirstLook,
    Place,
    Progress,
    Report,
    cloud_only,
    first_look,
    joined,
    sync_vault,
)
from stickle.sync.outcome import LONGEST_WAIT, Outcome, Stopped, after
from stickle.sync.runner import Runner, Until
from stickle.sync.schedule import Schedule
from stickle.sync.targets.base import CertificateError, Target

log = logging.getLogger(__name__)

QUIT_WAIT = 5.0  # seconds what is waiting to be put may take when the user quits
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
# Those looked at again when the app starts: it may have been dealt with since.
LOOKED_AT_AGAIN = {Stopped.BIG_CHANGE.value, Stopped.NEWER.value}


def cloud_time(connection: apsw.Connection, clocks: Clocks) -> CloudTime:
    """The cloud's clock as last known, and what this run stamps things with from now on."""
    cloud = CloudTime(since_start, sync_connection.cloud_offset(connection))
    clocks.cloud = cloud.now
    return cloud


def _shown_name(mark_id: str, name: str | None) -> str:
    return mark_name(Mark(mark_id, name, "", 0))


@dataclass(frozen=True)
class Ended:
    """A round over: what it brought (as far as it got), and what comes of it."""

    report: Report
    outcome: Outcome
    certificate: bytes | None = None  # what a server showed, if that stopped it

    @property
    def wait(self) -> float | None:
        return self.outcome.wait

    @property
    def stopped(self) -> bool:
        return self.outcome.stopped is not None


@dataclass
class _Rounds:
    """What runs on sync's thread."""

    store: Path
    key: bytes
    login: Login
    cloud: CloudTime
    make_target: Callable[[Callable[[int, int], None], Callable[[], None]], Target] | None = None
    over: Callable[[], bool] = lambda: False
    _db: apsw.Connection | None = None
    _target: Target | None = None
    failures: int = 0
    accept: bool = False  # the user lets the next round take in what it stopped for
    place: Place | None = None
    progress: Progress | None = None
    _stopped: str | None = None
    _offset: int = 0

    def database(self) -> apsw.Connection:
        if self._db is None:
            self._db = open_database(self.store, self.key, wait_ms=WAIT_MS)
            self._db.pragma("cache_size", -CACHE_KIB)
            self._stopped = self.login.stopped
            self._offset = self.cloud.offset
        return self._db

    def _no_longer_trusted(self) -> None:
        sync_connection.no_longer_trusted(self.database())

    def _new_target(self) -> Target:
        login = self.login
        if self.make_target is not None:
            return self.make_target(self.cloud.heard, self._no_longer_trusted)
        if login.kind != WEBDAV:
            raise ValueError(login.kind)
        from stickle.sync.targets.webdav import WebDav, parse_address

        return WebDav(
            parse_address(login.address),
            login.user,
            login.secret,
            login.certificate,
            heard=self.cloud.heard,
            no_longer_trusted=self._no_longer_trusted,
        )

    def place_of(self, db: apsw.Connection) -> Place:
        if self._target is None:
            self._target = Until(self._new_target(), self.over)
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
        take(self.database(), self.store)

    def accept_next(self) -> None:
        """The next round takes in what it stopped for, after a backup."""
        self.accept = True

    def first_look(self) -> FirstLook | None:
        """What merging with the vault this device just joined would make; None
        if it could not be looked at."""
        try:
            db = self.database()
            return first_look(db, self.place_of(db), self.cloud.now, self.repository(db))
        except Exception as error:  # the user is told, and may try again
            log.warning("sync: could not look at the vault: %s", type(error).__name__)
            return None

    def merge_first(self) -> None:
        """This device's notes are merged with the vault's it just joined."""
        joined(self.database(), self.login.vault)

    def only_the_cloud(self) -> Ended:
        """This device's notes let go of, after a backup, for the vault's it just joined."""
        db = self.database()
        notes = self.repository(db)
        gone = [note.id for note in notes.all()]
        cloud_only(db, notes, self.login.vault, self._back_up)
        return Ended(Report(changed=gone), Outcome())

    def __call__(self, full: bool) -> Ended:
        report = Report()
        error: Exception | None = None
        db: apsw.Connection | None = None
        try:
            db = self.database()
            sync_vault(
                db,
                self.place_of(db),
                self.cloud.now,
                self.repository(db),
                shown=_shown_name,
                about=this_device(),
                full=full,
                accept=self._back_up if self.accept else None,
                report=report,
                progress=self.progress,
            )
        except Exception as failed:  # told apart below, whatever it was
            error = failed
        outcome = self._outcome(error, report)
        self.failures = outcome.failures
        if error is None:
            self.accept = False
        if db is not None:
            self._keep(db, outcome, report)
        return Ended(
            report,
            outcome,
            error.certificate if isinstance(error, CertificateError) else None,
        )

    def _outcome(self, error: Exception | None, report: Report) -> Outcome:
        try:
            outcome = after(error, self.failures, report.full)
        except Exception as unknown:
            log.error("sync: a round failed: %s", type(unknown).__name__)
            return Outcome(wait=LONGEST_WAIT, failures=self.failures + 1)
        if outcome.wait is not None and outcome.failures == 1:  # once, not every few minutes
            log.info("sync: a round failed for now: %s", type(error).__name__)
        return outcome

    def _keep(self, db: apsw.Connection, outcome: Outcome, report: Report) -> None:
        """What is to last beyond this run, and the memory a round took given back."""
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
        self._target = None
        if self._db is not None:
            self._db.close()
            self._db = None


class Syncing(QObject):
    # A round over and what it brought shown (Ended).
    round_shown = Signal(object)
    # What merging with a vault just joined would make (FirstLook, None: not known).
    first_looked = Signal(object)
    # A round's way through the notes: what it is doing, how many so far, of how many.
    moving = Signal(str, int, int)
    _round_ended = Signal(object)
    _moved = Signal(str, int, int)

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
    ) -> None:
        """store, key: the notes database. make_target: how to reach the cloud,
        if not as login says (given the two things to tell of what it hears)."""
        super().__init__()
        self._connection = connection
        self._manager = manager
        self._reminders = reminders
        self._rounds = _Rounds(store, key, login, cloud, make_target)
        self._rounds.progress = self._moved.emit
        every = Settings(connection).get(SYNC_EVERY)
        stopped = login.stopped is not None and login.stopped not in LOOKED_AT_AGAIN
        self.runner = Runner(
            Schedule(self._now(), every, stopped),
            self._rounds,
            self._round_ended.emit,
            self._now,
            self._rounds.close,
        )
        self._rounds.over = lambda: self.runner.over
        self._written_rows: set[int] = set()
        self._noticing = False
        self._round_ended.connect(self._show)
        self._moved.connect(self.moving)  # from sync's thread to the app's
        self._collector = QTimer(self)
        self._collector.setInterval(COLLECT_EVERY_MS)
        self._collector.timeout.connect(self._collect)

    @staticmethod
    def _now() -> float:
        return since_start() / 1000

    def start(self) -> None:
        """Rounds as they fall due, from now on; changes made here noticed."""
        gc.disable()  # see _collect
        self._collector.start()
        self._connection.set_update_hook(self._written)
        self._manager.left_note.connect(self.runner.left)
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

    def _state_changed(self, state: Qt.ApplicationState) -> None:
        if state == Qt.ApplicationState.ApplicationActive:
            self.runner.focused()

    def woke(self) -> None:
        self.runner.woke()

    def hurry(self) -> None:
        """Send what is waiting now: the app may not go on for long (a session ending)."""
        self._notice()
        self.runner.hurry()

    def sync_now(self) -> None:
        self.runner.asked()

    # What the user decides (the windows that ask are elsewhere)

    def take_big_change(self) -> None:
        """The big change a round stopped for is taken in, after a backup."""
        self.runner.do(self._rounds.accept_next)
        self.runner.asked()

    def look_first(self) -> None:
        """What merging with the vault just joined would make: first_looked says."""
        self.runner.do(lambda: self.first_looked.emit(self._rounds.first_look()))

    def merge_first(self) -> None:
        """This device's notes merged with those of the vault it just joined."""
        self.runner.do(self._rounds.merge_first)
        self.runner.asked()

    def keep_only_the_cloud(self) -> None:
        """This device's notes let go of, after a backup, for the vault's."""
        self.runner.do(lambda: self._round_ended.emit(self._rounds.only_the_cloud()))
        self.runner.asked()

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
        report = ended.report
        changed = list(dict.fromkeys(report.changed))
        if report.device and self._reminders is not None:
            self._reminders.use_device(report.device)
        if changed or report.records.changed:
            self._manager.take_in(changed, bool(report.records.changed))
            if self._reminders is not None:
                self._reminders.take_in(changed)
        self.round_shown.emit(ended)

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

    def finish(self, seconds: float) -> None:
        """Ends it; seconds: how long what is waiting to be put may take."""
        self._connection.set_update_hook(None)
        self._notice()
        self._collector.stop()
        self.runner.finish(seconds)
        if not self.runner.running:
            gc.enable()
