"""Daily backups of the notes database, and going back to one.

Once a day the database is copied, still encrypted with the same key, into
backups/daily next to it, and copies older than a week are removed, though
the newest is always kept: after a long time away there is still one. The key
files are never copied or restored, so a backup opens with the key in use
now (a new password or recovery key only wraps the same key again), and going
back to a backup never brings back an old password.

Going back replaces the whole database. The notes as they are just before
are backed up first, marked as such, so that going back can be undone the
same way. The copy is written in two steps: next to the database while the
app runs (so a failure changes nothing and can be told), then put in place
once the database is closed.

The copies made before an upgrade (stickle.data.schema.backup) are kept
apart, in backups itself.
"""

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import apsw

from stickle.core.clock import Clock, utc_now
from stickle.data.database import WrongKeyError, open_database

KEPT_DAYS = 7
BEFORE_RESTORE = "-before-restore"
PARTIAL = ".partial"
RESTORING = ".restoring"
# notes-20261018T031500123Z.db, or with BEFORE_RESTORE before .db
_NAME = re.compile(r"^(?P<stem>.+)-(?P<stamp>\d{8}T\d{9}Z)(?P<before>-before-restore)?\.db$")

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Backup:
    path: Path
    taken: datetime  # in UTC
    before_restore: bool  # made just before going back to another backup


@dataclass(frozen=True)
class Contents:
    notes: int  # not in the trash
    newer: bool  # written by a newer Stickle, which this one cannot open


def folder(database: Path) -> Path:
    return database.parent / "backups" / "daily"


def _moment(clock: Clock) -> datetime:
    return datetime.fromisoformat(clock().replace("Z", "+00:00"))


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S") + f"{moment.microsecond // 1000:03d}Z"


def _local_day(moment: datetime) -> date:
    return moment.astimezone().date()


def backups(database: Path) -> list[Backup]:
    """The backups there are, newest first. Unfinished copies are not among them."""
    found: list[Backup] = []
    try:
        names = [entry.name for entry in os.scandir(folder(database)) if entry.is_file()]
    except FileNotFoundError:
        return []
    for name in names:
        match = _NAME.match(name)
        if match is None or match["stem"] != database.stem:
            continue
        taken = datetime.strptime(match["stamp"][:-4], "%Y%m%dT%H%M%S").replace(
            microsecond=int(match["stamp"][-4:-1]) * 1000, tzinfo=UTC
        )
        found.append(Backup(folder(database) / name, taken, match["before"] is not None))
    return sorted(found, key=lambda backup: backup.taken, reverse=True)


def take(
    connection: apsw.Connection,
    database: Path,
    clock: Clock = utc_now,
    before_restore: bool = False,
) -> Path:
    """Copy the open database into a new backup, encrypted as it is.

    The copy is written under a name the list leaves out and renamed once it is
    complete, so a crash or a full disk never leaves half a backup among them.
    """
    target_folder = folder(database)
    target_folder.mkdir(parents=True, exist_ok=True)
    for stale in target_folder.glob(f"*{PARTIAL}"):
        stale.unlink(missing_ok=True)
    name = f"{database.stem}-{_stamp(_moment(clock))}{BEFORE_RESTORE if before_restore else ''}.db"
    target = target_folder / name
    partial = target.with_name(name + PARTIAL)
    try:
        # SQLite3 Multiple Ciphers writes the copy encrypted with the same key.
        connection.execute("VACUUM INTO ?", (str(partial),))
        with partial.open("rb+") as file:
            os.fsync(file.fileno())
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return target


def due(database: Path, clock: Clock = utc_now) -> bool:
    """Whether today (on this computer's clock) has no backup yet."""
    today = _local_day(_moment(clock))
    return not any(_local_day(backup.taken) == today for backup in backups(database))


def prune(database: Path, clock: Clock = utc_now) -> list[Path]:
    """Remove backups older than KEPT_DAYS days, always keeping the newest."""
    oldest = _moment(clock) - timedelta(days=KEPT_DAYS)
    removed: list[Path] = []
    for backup in backups(database)[1:]:
        if backup.taken < oldest:
            backup.path.unlink(missing_ok=True)
            removed.append(backup.path)
    return removed


def take_daily(connection: apsw.Connection, database: Path, clock: Clock = utc_now) -> Path | None:
    """The day's backup, if there is none yet, then the old ones removed."""
    taken = take(connection, database, clock) if due(database, clock) else None
    prune(database, clock)
    return taken


def contents(backup: Path, key: bytes, latest_version: int) -> Contents | None:
    """What a backup holds, or None when it cannot be read with this key."""
    try:
        connection = open_database(backup, key, read_only=True)
    except WrongKeyError, apsw.Error:
        return None
    try:
        version = int(connection.pragma("user_version"))
        if version > latest_version:
            return Contents(0, newer=True)
        try:
            rows = connection.execute("SELECT count(*) FROM notes WHERE deleted_at IS NULL")
        except apsw.SQLError:
            return Contents(0, newer=False)
        count = rows.fetchall()[0][0]
        return Contents(count if isinstance(count, int) else 0, newer=False)
    except apsw.Error:
        return None
    finally:
        connection.close()


def _sidecars(database: Path) -> list[Path]:
    return [database.with_name(database.name + suffix) for suffix in ("-wal", "-shm")]


def stage_restore(database: Path, backup: Path) -> Path:
    """Copy a backup next to the database, ready to take its place once it is
    closed (finish_restore). The database itself is not touched."""
    staged = database.with_name(database.name + RESTORING)
    partial = staged.with_name(staged.name + PARTIAL)
    try:
        with backup.open("rb") as source, partial.open("wb") as copy:
            while chunk := source.read(1 << 20):
                copy.write(chunk)
            copy.flush()
            os.fsync(copy.fileno())
        partial.replace(staged)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return staged


def finish_restore(database: Path) -> bool:
    """Put the staged backup in place of the closed database. Whether there was one."""
    staged = database.with_name(database.name + RESTORING)
    if not staged.exists():
        return False
    # The database was closed cleanly, so its write-ahead log holds nothing more,
    # and an old one must not be replayed onto the restored file.
    for sidecar in _sidecars(database):
        sidecar.unlink(missing_ok=True)
    staged.replace(database)
    return True


def cancel_restore(database: Path) -> None:
    database.with_name(database.name + RESTORING).unlink(missing_ok=True)
