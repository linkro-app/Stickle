"""Going back to a backup on a computer that syncs, or did: what the copy put in
place of the notes (stickle.data.backups) takes of them as they are now.

Sync goes on as it is now: how this computer reaches the cloud, what it knows
of each vault (each note's and record's file there, what waits to go, the
other devices, the files it leaves as they are), its counter of changes, how
far the cloud's clock is from its own, and the settings that go with them. A
backup's would have the next round take what the cloud has, changed since, in
place of what was gone back to: going back would be undone without a word.

What changed since the backup was taken is changed again, here and now: each
field of a note or of a record (a category, a mark, a colour of the user's
own, a note brought in, a setting that follows the user) whose time is not the
one it has now is stamped anew
(stickle.data.stamps), and a text that is not the one now becomes this
computer's, of now. So the next round takes what was gone back to to every
computer that syncs, as any change made here; what did not change since goes
nowhere. The text a note has now is kept among its earlier ones
(stickle.data.versions), as put aside by going back.

The settings about the key files (which recovery key the user said they keep)
are as now, and so is what sync knows of them: the key files are not gone back
to (stickle.data.backups.KEY_SETTINGS). How a reminder was dealt with is not
gone back on: one set as it is now keeps
how it was dealt with, so that none rings again on every computer for what
was dealt with already. A note secret then and not now, or the other way, does
not keep its text of now among its earlier ones: in the clear beside a secret
one's, or sealed beside an ordinary one's, it would not be as they are kept.

On a computer that syncs, the notes and records made since the backup are kept
as they are now, with all that is theirs: sync knows them, and they would be
gone from this computer alone. One that does not sync goes back as before; once
sync is on again, the cloud gives them back.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import apsw

from stickle.core.clock import stamp_at
from stickle.core.hlc import Hlc, after
from stickle.data import stamps, versions
from stickle.data.backups import KEY_SETTINGS
from stickle.data.database import open_database
from stickle.data.record_stamps import CATEGORY, COLOR, IMPORT, MARK, SETTING, imported_field
from stickle.data.reminders import NOTE, RULE_FIELD, STATE_FIELD, TEXT, text_field
from stickle.data.schema import latest_version, schema_version, upgrade
from stickle.data.settings import (
    DEVICE_ID,
    DEVICE_NAME,
    SYNC_EVERY,
    SYNC_GONE_BACK,
    SYNC_STOP_SEEN,
    SYNC_STOP_TOLD,
    SYNC_TRASHED,
    Setting,
    Settings,
)
from stickle.data.sync_connection import OFFSET

RESTORING = ".restoring"  # as stickle.data.backups names the copy
# This computer's own, as they are now: how it syncs and what it knows of each vault.
SYNC_TABLES = (
    "sync_connection",
    "sync_notes",
    "sync_vaults",
    "sync_records",
    "sync_devices",
    "sync_left",
    "sync_staged",
    "sync_held",
    "sync_untold",
    "sync_keys",
)
SYNC_SETTINGS: tuple[Setting[object], ...] = (
    DEVICE_ID,  # pyright: ignore[reportAssignmentType]
    DEVICE_NAME,  # pyright: ignore[reportAssignmentType]
    SYNC_EVERY,  # pyright: ignore[reportAssignmentType]
    SYNC_STOP_TOLD,  # pyright: ignore[reportAssignmentType]
    SYNC_STOP_SEEN,  # pyright: ignore[reportAssignmentType]
    SYNC_TRASHED,  # pyright: ignore[reportAssignmentType]
)
# A note's own fields, stamped anew when it comes back (its marks too).
NOTE_FIELDS = tuple(f for f in stamps.FIELDS.values() if f != "conflict_of")
# The tables of what is a note's, but its text and fields (moved with it).
NOTE_TABLES = ("note_layouts", "note_sizes", "note_marks", "reminders", "note_stamps")
# How a reminder was dealt with: kept as it is now.
DEALT_WITH = ("next_day", "snoozed_until", "acked_at", "acked_device")
# Each kind of record: its table, and how an item of it is named.
RECORDS = {
    CATEGORY: ("categories", "id"),
    MARK: ("marks", "id"),
    COLOR: ("custom_colors", "id"),
    IMPORT: ("import_records", "source || char(10) || source_id"),
    SETTING: ("shared_settings", "key"),
}
# Records kept as they are now, with what sync knows of them.
AS_NOW = {(SETTING, each.key) for each in KEY_SETTINGS if each.scope == "shared"}

type StampRow = tuple[str, str | None, int | None]  # time, device (None: here), sent


@dataclass(frozen=True)
class GoneBack:
    """What the line telling of it says: the backup's time, and the backup of
    the notes as they were before, to go back to that (its file's name)."""

    to: str
    before: str


class _Changes:
    """The change numbers given out: each after every number used now or then."""

    def __init__(self, copy: apsw.Connection, now: apsw.Connection) -> None:
        self.last = max(_counter(copy, "change_seq") or 0, _counter(now, "change_seq") or 0)

    def next(self) -> int:
        self.last += 1
        return self.last


def _counter(db: apsw.Connection, key: str) -> int | None:
    rows = db.execute("SELECT value FROM local_counters WHERE key = ?", (key,)).fetchall()
    return int(rows[0][0]) if rows else None  # pyright: ignore[reportArgumentType]


def _columns(db: apsw.Connection, table: str, leaving: Iterable[str] = ()) -> list[str]:
    left = set(leaving)
    return [str(row[1]) for row in db.execute(f"PRAGMA table_info({table})") if row[1] not in left]


def _copy_rows(
    now: apsw.Connection,
    copy: apsw.Connection,
    table: str,
    where: str = "1",
    params: Sequence[apsw.SQLiteValue] = (),
    leaving: Iterable[str] = (),
) -> None:
    """Rows of now's table put in the copy's as they are (leaving: columns the
    copy gives itself)."""
    names = _columns(now, table, leaving)
    listed = ", ".join(names)
    rows = now.execute(f"SELECT {listed} FROM {table} WHERE {where}", tuple(params)).fetchall()
    marks = ", ".join("?" * len(names))
    copy.executemany(f"INSERT OR REPLACE INTO {table} ({listed}) VALUES ({marks})", rows)


def _ids(db: apsw.Connection, query: str) -> set[str]:
    return {str(row[0]) for row in db.execute(query)}


def _time(text: str) -> Hlc | None:
    try:
        return Hlc.parse(text)
    except ValueError:  # not ours to read: as never changed
        return None


def _anew(millis: int, *before: StampRow | None) -> str:
    """A field's time changed at millis, after every time it had."""
    times = [t for row in before if row is not None and (t := _time(row[0])) is not None]
    return after(millis, max(times, default=None)).text


def _note_stamps(db: apsw.Connection, note_id: str) -> dict[str, StampRow]:
    return {
        str(name): (str(hlc), None if device is None else str(device), sent)  # pyright: ignore[reportAssignmentType]
        for name, hlc, device, sent in db.execute(
            "SELECT field, hlc, device, sent FROM note_stamps WHERE note_id = ?", (note_id,)
        )
    }


def _put_note_stamp(db: apsw.Connection, note_id: str, name: str, row: StampRow | None) -> None:
    db.execute("DELETE FROM note_stamps WHERE note_id = ? AND field = ?", (note_id, name))
    if row is not None:
        db.execute(
            "INSERT INTO note_stamps (note_id, field, hlc, device, sent) VALUES (?, ?, ?, ?, ?)",
            (note_id, name, *row),
        )


def _record_stamps(db: apsw.Connection, kind: str, item: str) -> dict[str, StampRow]:
    return {
        str(name): (str(hlc), None if device is None else str(device), sent)  # pyright: ignore[reportAssignmentType]
        for name, hlc, device, sent in db.execute(
            "SELECT field, hlc, device, sent FROM record_stamps WHERE kind = ? AND item = ?",
            (kind, item),
        )
    }


def _put_record_stamp(
    db: apsw.Connection, kind: str, item: str, name: str, row: StampRow | None
) -> None:
    db.execute(
        "DELETE FROM record_stamps WHERE kind = ? AND item = ? AND field = ?", (kind, item, name)
    )
    if row is not None:
        db.execute(
            "INSERT INTO record_stamps (kind, item, field, hlc, device, sent)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (kind, item, name, *row),
        )


def _restamped(
    then: Mapping[str, StampRow],
    now: Mapping[str, StampRow],
    millis: int,
    as_now: Iterable[str] = (),
) -> tuple[dict[str, StampRow | None], bool]:
    """Each field's stamp in the copy: as it is now if it did not change since
    (or is kept as now, as_now), else anew. Whether any changed."""
    kept = set(as_now)
    found: dict[str, StampRow | None] = {}
    changed = False
    for name in sorted(then.keys() | now.keys() | kept):
        old, new = then.get(name), now.get(name)
        if name in kept or (old is not None and new is not None and old[:2] == new[:2]):
            found[name] = new
        else:
            found[name] = (_anew(millis, old, new), None, None)
            changed = True
    return found, changed


# What a reminder dealt with keeps of now


def _dealt_with(copy: apsw.Connection, now: apsw.Connection, note_id: str) -> set[str]:
    """The note's reminders set as they are now, dealt with as they are now:
    their fields kept as now's, how they were dealt with put in the copy."""
    kept: set[str] = set()
    then_rule = copy.execute(
        "SELECT hlc, device FROM note_stamps WHERE note_id = ? AND field = ?", (note_id, RULE_FIELD)
    ).fetchall()
    now_rule = now.execute(
        "SELECT hlc, device FROM note_stamps WHERE note_id = ? AND field = ?", (note_id, RULE_FIELD)
    ).fetchall()
    own = f"SELECT id, {', '.join(DEALT_WITH)} FROM reminders WHERE note_id = ? AND kind = ?"
    if then_rule and then_rule == now_rule:
        rows = now.execute(own + " AND deleted_at IS NULL", (note_id, NOTE)).fetchall()
        there = {
            str(row[0])
            for row in copy.execute(
                "SELECT id FROM reminders WHERE note_id = ? AND kind = ?", (note_id, NOTE)
            )
        }
        if len(rows) == 1 and str(rows[0][0]) in there:
            _set_dealt_with(copy, str(rows[0][0]), rows[0][1:])
            kept.add(STATE_FIELD)
    written = "SELECT id, start_day, at_time FROM reminders WHERE note_id = ? AND kind = ?"
    alive = " AND deleted_at IS NULL"
    nows = {
        (str(day), str(at)): str(row_id)
        for row_id, day, at in now.execute(written + alive, (note_id, TEXT))
    }
    for row_id, day, at in copy.execute(written + alive, (note_id, TEXT)).fetchall():
        same = nows.get((str(day), str(at)))
        if same is None:
            continue
        values = now.execute(
            f"SELECT {', '.join(DEALT_WITH)} FROM reminders WHERE id = ?", (same,)
        ).fetchall()[0]
        _set_dealt_with(copy, str(row_id), values)
        kept.add(text_field(str(day), str(at)))
    return kept


def _set_dealt_with(copy: apsw.Connection, row_id: str, values: Sequence[apsw.SQLiteValue]) -> None:
    setting = ", ".join(f"{column} = ?" for column in DEALT_WITH)
    copy.execute(f"UPDATE reminders SET {setting} WHERE id = ?", (*values, row_id))


# Notes


_TEXT = f"SELECT {versions.TEXT_COLUMNS}"


def _changed_note(
    copy: apsw.Connection, now: apsw.Connection, note_id: str, millis: int, changes: _Changes
) -> None:
    """A note there then and now: what changed since, changed again now."""
    at = stamp_at(millis)
    then_text = copy.execute(_TEXT + " FROM notes WHERE id = ?", (note_id,)).fetchall()[0]
    now_text = now.execute(_TEXT + " FROM notes WHERE id = ?", (note_id,)).fetchall()[0]
    kept = _dealt_with(copy, now, note_id)
    found, changed = _restamped(
        _note_stamps(copy, note_id), _note_stamps(now, note_id), millis, kept
    )
    for name, row in found.items():
        _put_note_stamp(copy, note_id, name, row)
    if then_text[:2] != now_text[:2]:
        if (then_text[1] is None) == (now_text[1] is None):
            # Kept as the note keeps its texts: in the clear, or sealed if secret.
            # One secret then and not now, or the other way, has it in the backup
            # made before going back alone.
            versions.keep_text(copy, note_id, now_text, at, restored=True)
        copy.execute("UPDATE notes SET text_at = ?, text_from = NULL WHERE id = ?", (at, note_id))
        changed = True
    else:
        copy.execute(
            "UPDATE notes SET text_at = ?, text_from = ? WHERE id = ?",
            (now_text[3], now_text[4], note_id),
        )
    if changed:
        copy.execute(
            "UPDATE notes SET change_seq = ?, updated_at = ? WHERE id = ?",
            (changes.next(), at, note_id),
        )
    else:
        seq = now.execute("SELECT change_seq FROM notes WHERE id = ?", (note_id,)).fetchall()[0][0]
        copy.execute("UPDATE notes SET change_seq = ? WHERE id = ?", (seq, note_id))


def _back_note(copy: apsw.Connection, note_id: str, millis: int, changes: _Changes) -> None:
    """A note there then and not now (emptied since, say): all of it changed now."""
    at = stamp_at(millis)
    then = _note_stamps(copy, note_id)
    marks = {
        stamps.mark_field(str(row[0]))
        for row in copy.execute("SELECT mark_id FROM note_marks WHERE note_id = ?", (note_id,))
    }
    for name in sorted({*then, *NOTE_FIELDS, *marks}):
        _put_note_stamp(copy, note_id, name, (_anew(millis, then.get(name)), None, None))
    copy.execute(
        "UPDATE notes SET text_at = ?, text_from = NULL, change_seq = ?, updated_at = ?"
        " WHERE id = ?",
        (at, changes.next(), at, note_id),
    )


def _move_note(copy: apsw.Connection, now: apsw.Connection, note_id: str) -> None:
    """A note made since, with all that is its own, as it is now."""
    _copy_rows(now, copy, "notes", "id = ?", (note_id,), leaving=("seq",))
    for table in NOTE_TABLES:
        _copy_rows(now, copy, table, "note_id = ?", (note_id,))
    _copy_rows(now, copy, "note_versions", "note_id = ?", (note_id,), leaving=("id",))
    copy.execute("DELETE FROM deletion_records WHERE note_id = ?", (note_id,))


# Records


def _items(db: apsw.Connection, kind: str) -> set[str]:
    table, named = RECORDS[kind]
    return _ids(db, f"SELECT DISTINCT {named} FROM {table}")


def _move_item(copy: apsw.Connection, now: apsw.Connection, kind: str, item: str) -> None:
    table, named = RECORDS[kind]
    _copy_rows(now, copy, table, f"{named} = ?", (item,))
    where = "kind = ? AND item = ?"
    _copy_rows(now, copy, "record_stamps", where, (kind, item))
    _copy_rows(now, copy, "record_changes", where, (kind, item))


def _item_as_now(copy: apsw.Connection, now: apsw.Connection, kind: str, item: str) -> None:
    """A record as it is now, all of it (none of it, if it is not here now)."""
    table, named = RECORDS[kind]
    copy.execute(f"DELETE FROM {table} WHERE {named} = ?", (item,))
    for each in ("record_stamps", "record_changes"):
        copy.execute(f"DELETE FROM {each} WHERE kind = ? AND item = ?", (kind, item))
    _move_item(copy, now, kind, item)


def _change_number(db: apsw.Connection, kind: str, item: str) -> int | None:
    rows = db.execute(
        "SELECT change_seq FROM record_changes WHERE kind = ? AND item = ?", (kind, item)
    ).fetchall()
    return int(rows[0][0]) if rows else None  # pyright: ignore[reportArgumentType]


def _set_change_number(db: apsw.Connection, kind: str, item: str, seq: int | None) -> None:
    db.execute("DELETE FROM record_changes WHERE kind = ? AND item = ?", (kind, item))
    if seq is not None:
        db.execute(
            "INSERT INTO record_changes (kind, item, change_seq) VALUES (?, ?, ?)",
            (kind, item, seq),
        )


def _changed_item(
    copy: apsw.Connection,
    now: apsw.Connection,
    kind: str,
    item: str,
    millis: int,
    changes: _Changes,
    moved_notes: set[str],
) -> None:
    """A record there then and now: what changed since, changed again now. A
    note brought in that became a note made since keeps that, as now."""
    then, current = _record_stamps(copy, kind, item), _record_stamps(now, kind, item)
    as_now: set[str] = set()
    if kind == IMPORT:
        as_now = {imported_field(note_id) for note_id in moved_notes}
    found, changed = _restamped(then, current, millis, as_now & (then.keys() | current.keys()))
    for name, row in found.items():
        _put_record_stamp(copy, kind, item, name, row)
    if kind == IMPORT and moved_notes:
        marks = ", ".join("?" * len(moved_notes))
        _copy_rows(
            now,
            copy,
            "import_records",
            f"source || char(10) || source_id = ? AND note_id IN ({marks})",
            (item, *sorted(moved_notes)),
        )
    seq = changes.next() if changed else _change_number(now, kind, item)
    _set_change_number(copy, kind, item, seq)


def _back_item(copy: apsw.Connection, kind: str, item: str, millis: int, changes: _Changes) -> None:
    """A record there then and not now: all of it changed now."""
    for name, row in _record_stamps(copy, kind, item).items():
        _put_record_stamp(copy, kind, item, name, (_anew(millis, row), None, None))
    _set_change_number(copy, kind, item, changes.next())


# All of it


def keep_sync_state(
    database: Path, key: bytes, now: apsw.Connection, millis: int, told: GoneBack | None = None
) -> None:
    """The copy staged next to the database (now: the database, open) made to
    go on with sync as it is now; millis: the time of now, as notes are stamped
    with (the cloud's, while syncing). told: what the line telling of it says,
    on a computer that syncs."""
    staged = database.with_name(database.name + RESTORING)
    looked = open_database(staged, key, read_only=True)
    try:
        version = schema_version(looked)
    finally:
        looked.close()
    if version < latest_version():
        upgrade(staged, key, version)  # its tables as now's first, no copy kept aside
    copy = open_database(staged, key)
    try:
        with copy:
            _keep(copy, now, millis, told)
    finally:
        copy.close()


def _keep(copy: apsw.Connection, now: apsw.Connection, millis: int, told: GoneBack | None) -> None:
    syncs = bool(now.execute("SELECT 1 FROM sync_connection").fetchall())
    changes = _Changes(copy, now)
    then_notes = _ids(copy, "SELECT id FROM notes")
    now_notes = _ids(now, "SELECT id FROM notes")
    moved: set[str] = (now_notes - then_notes) if syncs else set()
    for note_id in sorted(moved):
        _move_note(copy, now, note_id)
    for note_id in sorted(then_notes & now_notes):
        _changed_note(copy, now, note_id, millis, changes)
    for note_id in sorted(then_notes - now_notes):
        _back_note(copy, note_id, millis, changes)
    # Emptied since, and not in the backup: emptied still (the vault was told so).
    there = then_notes | moved | _ids(copy, "SELECT note_id FROM deletion_records")
    for note_id in sorted(_ids(now, "SELECT note_id FROM deletion_records") - there):
        _copy_rows(now, copy, "deletion_records", "note_id = ?", (note_id,))
    for kind, item in sorted(AS_NOW):
        _item_as_now(copy, now, kind, item)
    for kind in RECORDS:
        then_items, now_items = _items(copy, kind), _items(now, kind)
        as_now = {item for each, item in AS_NOW if each == kind}
        then_items, now_items = then_items - as_now, now_items - as_now
        for item in sorted(now_items - then_items) if syncs else ():
            _move_item(copy, now, kind, item)
        for item in sorted(then_items & now_items):
            _changed_item(copy, now, kind, item, millis, changes, moved)
        for item in sorted(then_items - now_items):
            _back_item(copy, kind, item, millis, changes)
    for table in SYNC_TABLES:
        copy.execute(f"DELETE FROM {table}")
        _copy_rows(now, copy, table)
    if not syncs:
        # What was made since is not here: once sync is on again, it comes anew.
        copy.execute(
            "DELETE FROM sync_notes WHERE note_id NOT IN (SELECT id FROM notes)"
            " AND note_id NOT IN (SELECT note_id FROM deletion_records)"
        )
        for kind, (table, named) in RECORDS.items():
            copy.execute(
                f"DELETE FROM sync_records WHERE kind = ? AND item NOT IN"
                f" (SELECT {named} FROM {table})",
                (kind,),
            )
    copy.execute("UPDATE local_counters SET value = ? WHERE key = 'change_seq'", (changes.last,))
    offset = _counter(now, OFFSET)
    copy.execute("DELETE FROM local_counters WHERE key = ?", (OFFSET,))
    if offset is not None:
        copy.execute("INSERT INTO local_counters (key, value) VALUES (?, ?)", (OFFSET, offset))
    kept = tuple(s.key for s in SYNC_SETTINGS)
    marks = ", ".join("?" * len(kept))
    copy.execute(f"DELETE FROM device_settings WHERE key IN ({marks})", kept)
    _copy_rows(now, copy, "device_settings", f"key IN ({marks})", kept)
    settings = Settings(copy)
    settings.clear(SYNC_GONE_BACK)  # the backup's, if it was taken while one was told
    if syncs and told is not None:
        settings.set(SYNC_GONE_BACK, {"to": told.to, "before": told.before})
