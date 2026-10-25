"""The local database layout and how it is upgraded.

Each schema version is reached in its own step. A step is applied to a copy
of the database, the copy is checked (SQLite integrity, every note and its
text still there, search index consistent), and only then does it replace
the original. A failed step leaves the database at the last good version,
so a fixed app can continue from there. Before upgrading, a backup copy is
kept (the newest few): restoring it is the way back to an older app, which
refuses a database newer than itself.
"""

import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import apsw

from stickle.core.clock import Clock, utc_now
from stickle.data.database import open_database

V1 = """
-- seq is the search index's link to the note. An explicit INTEGER PRIMARY KEY,
-- because implicit rowids may be renumbered by VACUUM, which would scramble the index.
CREATE TABLE notes (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    -- NUL ends strings inside SQLite's text functions, which would hide the rest of
    -- the note from search. It is never meaningful in a note, so it is refused here;
    -- the editor and importers remove it before saving.
    body TEXT NOT NULL CHECK (instr(body, char(0)) = 0),
    color TEXT NOT NULL,
    opacity REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL DEFAULT 'active',
    label TEXT,
    always_on_top INTEGER NOT NULL DEFAULT 1,
    locked INTEGER NOT NULL DEFAULT 0,
    collapsed INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    auto_height INTEGER NOT NULL DEFAULT 0,
    zoom REAL NOT NULL DEFAULT 1.0,
    sleep_until TEXT,
    style_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    deleted_at TEXT,
    change_seq INTEGER NOT NULL
);
CREATE VIRTUAL TABLE notes_fts USING fts5(
    body, tokenize = 'trigram', content = 'notes', content_rowid = 'seq'
);
-- Keep the index in step with the notes, whoever changes them.
CREATE TRIGGER notes_fts_insert AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, body) VALUES (new.seq, new.body);
END;
CREATE TRIGGER notes_fts_delete AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, body) VALUES ('delete', old.seq, old.body);
END;
CREATE TRIGGER notes_fts_update AFTER UPDATE OF body ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, body) VALUES ('delete', old.seq, old.body);
    INSERT INTO notes_fts(rowid, body) VALUES (new.seq, new.body);
END;

-- Window position per monitor configuration, as fractions of the monitor.
CREATE TABLE note_layouts (
    note_id TEXT NOT NULL,
    config_key TEXT NOT NULL,
    monitor_key TEXT NOT NULL,
    rel_x REAL NOT NULL,
    rel_y REAL NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (note_id, config_key)
);

-- Settings for this device only (language, autostart, ...).
CREATE TABLE device_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Settings that will follow the user to every device once sync exists.
CREATE TABLE shared_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
-- Counters of this device, such as the change sequence.
CREATE TABLE local_counters (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT INTO local_counters (key, value) VALUES ('change_seq', 0);
"""

V2 = """
-- A note emptied from the trash leaves only this: when it was deleted and
-- when emptied. Sync will pass it on, so another device does not take the
-- note it still has for a new one. Kept a year after emptying.
CREATE TABLE deletion_records (
    note_id TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL,
    purged_at TEXT NOT NULL
);
"""

V3 = """
-- What a note is about: one per note, kept in notes.label by id, so that a
-- renamed category stays on its notes. A deleted category keeps its row,
-- so that a note brought back from the trash can bring it back too. One row
-- each, so that categories made on two devices are both kept by sync.
CREATE TABLE categories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    color TEXT NOT NULL,
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
-- How a note is to be dealt with: any number per note. A built-in mark has
-- no name until the user gives it one, and is shown in the chosen language.
CREATE TABLE marks (
    id TEXT PRIMARY KEY,
    name TEXT CHECK (name IS NULL OR length(trim(name)) > 0),
    icon TEXT NOT NULL,
    position INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
INSERT INTO marks (id, icon, position, updated_at) VALUES
    ('todo', 'todo', 1, '2026-10-02T00:00:00.000Z'),
    ('urgent', 'urgent', 2, '2026-10-02T00:00:00.000Z'),
    ('important', 'important', 3, '2026-10-02T00:00:00.000Z'),
    ('waiting', 'waiting', 4, '2026-10-02T00:00:00.000Z');
CREATE TABLE note_marks (
    note_id TEXT NOT NULL,
    mark_id TEXT NOT NULL,
    PRIMARY KEY (note_id, mark_id)
);
"""

V4 = """
-- Each category shows as an icon of its own. Categories made before have the
-- plain tag; an icon a later version added is kept as it is, and shown as the
-- tag by a version that lacks it.
ALTER TABLE categories ADD COLUMN icon TEXT NOT NULL DEFAULT 'tag';
"""

V5 = """
-- Marks' icons come from the same set as categories'. The built-in marks get
-- theirs, and every icon a mark had takes the name of its like in the set.
UPDATE marks SET icon = CASE icon
    WHEN 'todo' THEN 'square-check'
    WHEN 'urgent' THEN 'triangle-alert'
    WHEN 'important' THEN 'star'
    WHEN 'waiting' THEN 'clock'
    WHEN 'bulb' THEN 'lightbulb'
    WHEN 'person' THEN 'user'
    WHEN 'home' THEN 'house'
    WHEN 'question' THEN 'circle-help'
    ELSE icon
END;
"""

V6 = """
-- Notes brought in from other sticky-note apps: which note of which app became
-- which note here, and its text as it came, so that bringing notes in again
-- brings only new ones. A note changed since in the other app comes again.
CREATE TABLE import_records (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    note_id TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id, content_hash)
);
"""

V7 = """
-- A third copy of the key files that the notes do not need to be opened (the
-- recovery key's, the secret notes'), as they are byte for byte: they come back
-- from here if both copies of a file are lost. See stickle.crypto.key_files.
CREATE TABLE key_copies (name TEXT PRIMARY KEY, content BLOB NOT NULL);
"""

# MIGRATIONS[n] brings the database from version n to version n + 1.
MIGRATIONS: list[str] = [V1, V2, V3, V4, V5, V6, V7]
BACKUPS_KEPT = 3


class NewerSchemaError(Exception):
    """The database was written by a newer version of the app."""


log = logging.getLogger(__name__)

TITLE_LENGTH = 60


@dataclass(frozen=True)
class NotesDiff:
    """Notes a failed step would have lost, altered or added, by their first line."""

    missing: list[str] = field(default_factory=list[str])
    changed: list[str] = field(default_factory=list[str])
    added: list[str] = field(default_factory=list[str])

    def __bool__(self) -> bool:
        return bool(self.missing or self.changed or self.added)


class MigrationError(Exception):
    """A step failed; the database stays at the last good version."""

    def __init__(self, message: str, diff: NotesDiff | None = None) -> None:
        super().__init__(message)
        self.diff = diff


def latest_version() -> int:
    return len(MIGRATIONS)


def schema_version(connection: apsw.Connection) -> int:
    return int(connection.pragma("user_version"))


def open_store(path: Path, key: bytes, clock: Clock = utc_now) -> apsw.Connection:
    """Open the database at the current schema version, upgrading it if needed."""
    connection = open_database(path, key)
    version = schema_version(connection)
    if version > latest_version():
        connection.close()
        raise NewerSchemaError(f"database version {version}, app knows {latest_version()}")
    if version == latest_version():
        repair_search_index(connection)
        return connection
    connection.pragma("wal_checkpoint", "TRUNCATE")
    connection.close()
    if version > 0:  # a new, empty database has nothing to keep
        backup(path, version, clock)
    for target in range(version + 1, latest_version() + 1):
        _migrate_step(path, key, target)
    return open_database(path, key)


def backup(path: Path, version: int, clock: Clock = utc_now) -> Path:
    """Copy the (encrypted) database aside, keeping the newest few copies."""
    folder = path.parent / "backups"
    folder.mkdir(exist_ok=True)
    # The time comes first so that names sort oldest to newest. (File times would not
    # do: copies can keep the original's time.)
    stamp = clock().replace(":", "").replace("-", "")
    target = folder / f"{path.stem}-{stamp}-v{version}.db"
    shutil.copyfile(path, target)
    copies = sorted(folder.glob(f"{path.stem}-*-v*.db"))
    for old in copies[:-BACKUPS_KEPT]:
        old.unlink()
    return target


def _sidecars(path: Path) -> list[Path]:
    return [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm")]


def _remove(path: Path) -> None:
    for file in [path, *_sidecars(path)]:
        file.unlink(missing_ok=True)


type Snapshot = dict[str, tuple[str, str]]  # note id -> (body hash, first line)


def first_line(body: str) -> str:
    line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    return line[:TITLE_LENGTH]


def _snapshot(connection: apsw.Connection) -> Snapshot | None:
    tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_schema")}
    if "notes" not in tables:
        return None
    return {
        str(note_id): (hashlib.sha256(str(body).encode()).hexdigest(), first_line(str(body)))
        for note_id, body in connection.execute("SELECT id, body FROM notes")
    }


def compare(before: Snapshot, after: Snapshot) -> NotesDiff:
    return NotesDiff(
        missing=sorted(before[i][1] for i in before.keys() - after.keys()),
        changed=sorted(
            before[i][1] for i in before.keys() & after.keys() if before[i][0] != after[i][0]
        ),
        added=sorted(after[i][1] for i in after.keys() - before.keys()),
    )


# rank = 1 also compares the index with the notes; without it only the index's
# own structure is checked, and a missing entry goes unnoticed.
_CHECK_INDEX = "INSERT INTO notes_fts(notes_fts, rank) VALUES ('integrity-check', 1)"


def search_index_is_consistent(connection: apsw.Connection) -> bool:
    try:
        connection.execute(_CHECK_INDEX)
    except apsw.CorruptError:
        return False
    return True


def repair_search_index(connection: apsw.Connection) -> bool:
    """Rebuild the search index from the notes if it disagrees with them.

    The index is derived data, so rebuilding it loses nothing. Returns whether
    it was rebuilt.
    """
    if search_index_is_consistent(connection):
        return False
    with connection:
        connection.execute("INSERT INTO notes_fts(notes_fts) VALUES ('rebuild')")
    log.warning("search index did not match the notes and was rebuilt")
    return True


def _verify(connection: apsw.Connection, before: Snapshot | None) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchall()
    if result != [("ok",)]:
        raise MigrationError(f"integrity check failed: {result[:3]}")
    after = _snapshot(connection)
    if before is not None:
        diff = compare(before, after or {})
        if diff:
            raise MigrationError(
                f"notes differ after the upgrade: {len(diff.missing)} missing,"
                f" {len(diff.changed)} changed, {len(diff.added)} added",
                diff,
            )
    repair_search_index(connection)
    if not search_index_is_consistent(connection):
        raise MigrationError("search index could not be rebuilt")


def _migrate_step(path: Path, key: bytes, target: int) -> None:
    work = path.with_name(path.name + ".migrating")
    _remove(work)
    shutil.copy2(path, work)
    try:
        connection = open_database(work, key)
        try:
            before = _snapshot(connection)
            with connection:
                connection.execute(MIGRATIONS[target - 1])
                connection.pragma("user_version", target)
            _verify(connection, before)
            connection.pragma("wal_checkpoint", "TRUNCATE")
        finally:
            connection.close()
    except MigrationError:
        _remove(work)
        raise
    except Exception as error:
        _remove(work)
        raise MigrationError(f"upgrade to version {target} failed") from error
    # The original was checkpointed and closed: its sidecar files are empty, and a
    # stale write-ahead log must not be replayed onto the new file.
    for sidecar in _sidecars(path):
        sidecar.unlink(missing_ok=True)
    work.replace(path)
