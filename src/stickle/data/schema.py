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

V8 = """
-- Secret notes: their text, encrypted with the secret notes' key and bound to
-- the note's id, in secret_body; body stays empty, so the search index (made
-- from body) never holds it. The title typed for one is plain (the database
-- itself is encrypted), as it shows while the note is locked.
ALTER TABLE notes ADD COLUMN secret_title TEXT;
ALTER TABLE notes ADD COLUMN secret_body BLOB;
"""

V9 = """
-- Colours the user added to the palette, one row each, so that colours added
-- on two devices are both kept by sync. A note stores the colour itself
-- (#rrggbb), not this row's id: removing a colour here leaves notes as they are.
CREATE TABLE custom_colors (
    id TEXT PRIMARY KEY,
    hex TEXT NOT NULL,
    position INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
"""

V10 = """
-- Notes that were secret but are not among the notes any more: emptied from
-- the trash while secret, or not in a backup gone back to. A backup holding the
-- earlier text of one in the clear keeps it sealed when gone back to. Kept on
-- this computer only (sync has the deletion records), ids alone, for good: the
-- copies made before an upgrade can be older than a year.
CREATE TABLE once_secret (note_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL);

-- A note brought in from another app and made secret keeps no hash of its text
-- there (it would tell whether a guess of the text is right): one record per
-- note of that app, marked, stops it coming in again (stickle.data.imports).
INSERT OR IGNORE INTO import_records (source, source_id, content_hash, note_id, imported_at)
    SELECT source, source_id, 'secret', note_id, min(imported_at) FROM import_records
    WHERE note_id IN (SELECT id FROM notes WHERE secret_body IS NOT NULL)
    GROUP BY source, source_id, note_id;
DELETE FROM import_records WHERE content_hash != 'secret'
    AND note_id IN (SELECT id FROM notes WHERE secret_body IS NOT NULL);
"""

V11 = """
-- Reminders, one row each: a note's own ('note', one a note at most) and, in
-- time, those written in its text. A row keeps its rule, never a list of times
-- worked out ahead (stickle.core.reminders): the day it starts, in its calendar,
-- the time, the time zone it was set in (NULL: wherever it rings), how it repeats
-- (weekly: the days, Monday 0 to Sunday 6, as '0,2,4') and the day it rings next
-- (NULL: dealt with, for good). snoozed_until: it rings then instead (UTC).
-- hide_note: the note is out of sight until it rings. acked_at and acked_device:
-- when and where it was last dealt with, so that sync can quiet it elsewhere.
-- Turned off, a row is only marked deleted, for sync to pass on.
CREATE TABLE reminders (
    id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    calendar TEXT NOT NULL DEFAULT 'gregorian',
    start_day TEXT NOT NULL,
    at_time TEXT NOT NULL,
    time_zone TEXT,
    repeat TEXT NOT NULL DEFAULT 'none',
    weekdays TEXT NOT NULL DEFAULT '',
    next_day TEXT,
    snoozed_until TEXT,
    hide_note INTEGER NOT NULL DEFAULT 0,
    acked_at TEXT,
    acked_device TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX reminders_by_note ON reminders (note_id);
"""

# A main or spare place readable as one (an invalid one, not even JSON, would
# make SQLite's json functions fail), and the number of monitors it was stored
# with: those named in it, or at least its monitor's number without names.
_READABLE_PLACE = (
    "CASE WHEN json_valid(monitor_key) THEN json_type(monitor_key, '$.number') END = 'integer'"
)
_MONITORS_THEN = """CAST(CASE
    WHEN json_type(monitor_key, '$.setup') = 'array'
        AND json_array_length(monitor_key, '$.setup') > 0
    THEN json_array_length(monitor_key, '$.setup')
    ELSE max(1, json_extract(monitor_key, '$.number'))
END AS TEXT)"""

V12 = f"""
-- A note's size, one whatever the monitors, kept apart from where it is: kept
-- too where windows cannot be placed, and changing it leaves the places as
-- they were. Until now a note's main and spare place had one size: the main
-- place's (else the spare's) is taken.
CREATE TABLE note_sizes (
    note_id TEXT PRIMARY KEY,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO note_sizes (note_id, width, height, updated_at)
    SELECT note_id, width, height, updated_at FROM note_layouts
    WHERE config_key = 'main' AND {_READABLE_PLACE};
INSERT OR IGNORE INTO note_sizes (note_id, width, height, updated_at)
    SELECT note_id, width, height, updated_at FROM note_layouts
    WHERE config_key = 'spare' AND {_READABLE_PLACE};

-- A place for each number of monitors (stickle.core.layout), in place of the
-- main and the spare one: each goes under the number of monitors it was
-- stored with. Two for the same number keep the main one; the rest go (a
-- spare place beside it, places that could not be read).
UPDATE OR IGNORE note_layouts SET config_key = {_MONITORS_THEN}
    WHERE config_key = 'main' AND {_READABLE_PLACE};
UPDATE OR IGNORE note_layouts SET config_key = {_MONITORS_THEN}
    WHERE config_key = 'spare' AND {_READABLE_PLACE};
DELETE FROM note_layouts WHERE config_key IN ('main', 'spare');
ALTER TABLE note_layouts DROP COLUMN width;
ALTER TABLE note_layouts DROP COLUMN height;
"""

V13 = """
-- The version whose "what's new" note was last given is this device's own
-- (stickle.data.settings): its value moves over, unless this device has one.
INSERT OR IGNORE INTO device_settings (key, value)
    SELECT key, value FROM shared_settings WHERE key = 'news_seen';
DELETE FROM shared_settings WHERE key = 'news_seen';
"""

V14 = """
-- A note's earlier texts (stickle.data.versions), on this computer only: the
-- text it had before it changed. A secret note's are encrypted as its text is
-- (sealed; body NULL). content_hash is worked out as the note's: the same text
-- is kept once, and the one the note has now is not shown. made_at, made_by:
-- when it became the note's text and where (NULL: here); restored: put aside by
-- going back to an earlier one, at kept_at. Kept 30 days from kept_at.
CREATE TABLE note_versions (
    id INTEGER PRIMARY KEY,
    note_id TEXT NOT NULL,
    body TEXT CHECK (body IS NULL OR instr(body, char(0)) = 0),
    sealed BLOB,
    content_hash TEXT NOT NULL,
    made_at TEXT NOT NULL,
    made_by TEXT,
    restored INTEGER NOT NULL DEFAULT 0,
    kept_at TEXT NOT NULL,
    CHECK ((body IS NULL) != (sealed IS NULL))
);
CREATE INDEX note_versions_by_note ON note_versions (note_id, kept_at);

-- When a note's text became what it is, and where from (NULL: here), for when
-- it is kept as an earlier one. Before now: NULL, its updated_at taken instead.
ALTER TABLE notes ADD COLUMN text_at TEXT;
ALTER TABLE notes ADD COLUMN text_from TEXT;
"""

# A note's last change as a field's time (stickle.core.hlc); one not a time, 0.
_CHANGED_AT = (
    "printf('%013d-0000', CAST(round(unixepoch(notes.updated_at, 'subsec') * 1000) AS INTEGER))"
)

V15 = f"""
-- When each field of a note was last changed (stickle.data.stamps), so that
-- sync keeps the later of two changes to it: the time (stickle.core.hlc) and
-- the device (NULL: here). A field never changed has none, the earliest time
-- of all. sent: when the value first reached the cloud, by the cloud's clock
-- (NULL: not yet), which a year in the trash is counted from. The fields are
-- the note's own but its text, which goes by versions, and one for each mark
-- on it. Notes changed before now take the time they last changed for all
-- their fields; a note never changed since it was made, none.
CREATE TABLE note_stamps (
    note_id TEXT NOT NULL,
    field TEXT NOT NULL,
    hlc TEXT NOT NULL,
    device TEXT,
    sent INTEGER,
    PRIMARY KEY (note_id, field)
) WITHOUT ROWID;
WITH fields(name) AS (VALUES ('color'), ('opacity'), ('category'), ('always_on_top'),
    ('hidden'), ('collapsed'), ('locked'), ('deleted_at'), ('secret_title'))
INSERT INTO note_stamps (note_id, field, hlc)
    SELECT notes.id, fields.name, {_CHANGED_AT} FROM notes, fields
    WHERE notes.updated_at != notes.created_at;
INSERT INTO note_stamps (note_id, field, hlc)
    SELECT notes.id, 'mark:' || note_marks.mark_id, {_CHANGED_AT}
    FROM note_marks JOIN notes ON notes.id = note_marks.note_id
    WHERE notes.updated_at != notes.created_at;
"""

V16 = """
-- What sync knows of each note in a vault it syncs with (stickle.data.sync_state):
-- the note's file as last seen there, as JSON, and that file's tag; the file
-- put there and not seen there yet (kept until it is: what this device's next
-- version is made on); the note's change number when it was last merged; and
-- whether its text is at a version that deletes it. A note emptied or made
-- secret has its text taken out of both files.
CREATE TABLE sync_notes (
    vault TEXT NOT NULL,
    note_id TEXT NOT NULL,
    seen TEXT,
    tag TEXT,
    outbox TEXT,
    merged_seq INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (vault, note_id)
) WITHOUT ROWID;
-- Where the cloud's list of changes was last followed to, for a cloud that tells them.
CREATE TABLE sync_vaults (vault TEXT PRIMARY KEY, cursor TEXT);
"""


def _changed_at(column: str) -> str:
    """A row's time as a field's (stickle.core.hlc); one not a time, 0."""
    return f"printf('%013d-0000', CAST(round(unixepoch({column}, 'subsec') * 1000) AS INTEGER))"


_BUILT_IN_MARKS = "('todo', 'urgent', 'important', 'waiting')"

V17 = f"""
-- When each field of a category, a mark, a colour of the user's own and a note
-- brought in from another app was last changed, and where, as note_stamps for
-- notes (stickle.data.record_stamps): kind 'category', 'mark', 'color' or
-- 'import'; item the row's id (one brought in: its app and its id there, a line
-- apart). record_changes: the change number each was last changed at here.
CREATE TABLE record_stamps (
    kind TEXT NOT NULL,
    item TEXT NOT NULL,
    field TEXT NOT NULL,
    hlc TEXT NOT NULL,
    device TEXT,
    sent INTEGER,
    PRIMARY KEY (kind, item, field)
) WITHOUT ROWID;
CREATE TABLE record_changes (
    kind TEXT NOT NULL,
    item TEXT NOT NULL,
    change_seq INTEGER NOT NULL,
    PRIMARY KEY (kind, item)
) WITHOUT ROWID;

-- Of two categories or marks of one name, or two colours alike, made on two
-- devices, the one made first is kept: marks and colours keep when they were
-- made too (before now: when they last changed; a built-in mark comes first,
-- and has none). merged_into: the one a category or mark became one with.
ALTER TABLE marks ADD COLUMN created_at TEXT;
ALTER TABLE custom_colors ADD COLUMN created_at TEXT;
ALTER TABLE categories ADD COLUMN merged_into TEXT;
ALTER TABLE marks ADD COLUMN merged_into TEXT;
UPDATE marks SET created_at = updated_at WHERE id NOT IN {_BUILT_IN_MARKS};
UPDATE custom_colors SET created_at = updated_at;

-- Rows from before now take the time they last changed for all their fields; a
-- built-in mark never changed, none.
WITH fields(name) AS (VALUES ('name'), ('color'), ('icon'), ('position'), ('deleted_at'))
INSERT INTO record_stamps (kind, item, field, hlc)
    SELECT 'category', categories.id, fields.name, {_changed_at("categories.updated_at")}
    FROM categories, fields;
WITH fields(name) AS (VALUES ('name'), ('icon'), ('position'), ('deleted_at'))
INSERT INTO record_stamps (kind, item, field, hlc)
    SELECT 'mark', marks.id, fields.name, {_changed_at("marks.updated_at")} FROM marks, fields
    WHERE marks.id NOT IN {_BUILT_IN_MARKS} OR marks.updated_at != '2026-10-02T00:00:00.000Z';
WITH fields(name) AS (VALUES ('hex'), ('position'), ('deleted_at'))
INSERT INTO record_stamps (kind, item, field, hlc)
    SELECT 'color', custom_colors.id, fields.name, {_changed_at("custom_colors.updated_at")}
    FROM custom_colors, fields;
INSERT OR IGNORE INTO record_stamps (kind, item, field, hlc)
    SELECT 'import', source || char(10) || source_id, 'note:' || note_id,
        {_changed_at("imported_at")}
    FROM import_records;
"""

V18 = """
-- What sync knows of each record (a category, a mark, a colour, a note brought
-- in) in a vault it syncs with (stickle.data.record_state), as sync_notes for
-- notes: record, the file's id there; kind and item, what it is here (as
-- record_stamps); the file as last seen there and its tag, the file put there
-- and not seen there yet, and the item's change number when it was last merged.
CREATE TABLE sync_records (
    vault TEXT NOT NULL,
    record TEXT NOT NULL,
    kind TEXT NOT NULL,
    item TEXT NOT NULL,
    seen TEXT,
    tag TEXT,
    outbox TEXT,
    merged_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (vault, record)
) WITHOUT ROWID;
-- The devices that sync with a vault, as each one's own file there was last
-- seen (what it is called, its system and version), and that file's tag.
CREATE TABLE sync_devices (
    vault TEXT NOT NULL,
    device TEXT NOT NULL,
    seen TEXT,
    tag TEXT,
    PRIMARY KEY (vault, device)
) WITHOUT ROWID;
-- The day a note's own reminder first rings, as it was set: what it rings on
-- when how it was dealt with elsewhere was for an earlier setting of it.
ALTER TABLE reminders ADD COLUMN first_day TEXT;
UPDATE reminders SET first_day = next_day WHERE kind = 'note';
"""

V19 = """
-- Of each vault: when this device last went through a round with it, by the
-- cloud's clock (a note's file gone from it is taken for a deletion cleared
-- a year after only when that was more than a year back), and the marks of this
-- device's writes of its own file there, the last year's (one there with a
-- mark of none of them was written by another device under this one's id: a
-- copy of its disk; one with an earlier one, the vault gone back a while).
ALTER TABLE sync_vaults ADD COLUMN synced_at INTEGER;
ALTER TABLE sync_vaults ADD COLUMN own_mark TEXT;
-- A note whose file the vault lost while this device was away for more than
-- a year: kept here only, in the trash, and not looked for again.
ALTER TABLE sync_notes ADD COLUMN gone INTEGER NOT NULL DEFAULT 0;
-- Files of a vault sync leaves as they are, by their place there, and why: one
-- that does not open (how many rounds running, since when), one set aside,
-- one with a field this version may not change, one that is not this vault's
-- or not where an item's own file is. Its tag (or, with none, a hash of its
-- bytes) tells it from what is written there next.
CREATE TABLE sync_left (
    vault TEXT NOT NULL,
    path TEXT NOT NULL,
    tag TEXT,
    digest TEXT NOT NULL,
    why TEXT NOT NULL,
    fails INTEGER NOT NULL DEFAULT 0,
    first_at INTEGER NOT NULL,
    PRIMARY KEY (vault, path)
) WITHOUT ROWID;
"""

V20 = """
-- Files of a vault a round has read before taking anything in (it looks at
-- all that changed first), by their place there: their bytes, or none for one
-- read and not there, and their tag. What it takes in is read from here, one
-- at a time, and they go once the round is through; one still listed with the
-- same tag by the next round is not fetched again.
CREATE TABLE sync_staged (
    vault TEXT NOT NULL,
    path TEXT NOT NULL,
    tag TEXT,
    data BLOB,
    PRIMARY KEY (vault, path)
) WITHOUT ROWID;
-- What a round stopped for, many notes at once going from sight here (to the
-- trash, or deleted for good) or conflict copies made (copy_of: the note one
-- is of): kept until a round takes them in, as the user chooses.
CREATE TABLE sync_held (
    vault TEXT NOT NULL,
    note_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    copy_of TEXT,
    PRIMARY KEY (vault, note_id, kind)
) WITHOUT ROWID;
-- A vault this device joined, which was there before it, until a round with
-- it is through: its rounds merge as a first connection does.
ALTER TABLE sync_vaults ADD COLUMN joining INTEGER NOT NULL DEFAULT 0;
"""

# MIGRATIONS[n] brings the database from version n to version n + 1.
MIGRATIONS: list[str] = [
    *(V1, V2, V3, V4, V5, V6, V7, V8, V9, V10, V11, V12, V13, V14, V15, V16),
    *(V17, V18, V19, V20),
]
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
    upgrade(path, key, version)
    return open_database(path, key)


def upgrade(path: Path, key: bytes, version: int) -> None:
    """Bring the closed database at path from version to the latest, with no copy
    kept aside: for one that is a copy itself (a backup being gone back to)."""
    for target in range(version + 1, latest_version() + 1):
        _migrate_step(path, key, target)


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
