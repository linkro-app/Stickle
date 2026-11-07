"""A record here (a category, a mark, a colour of the user's own, a note of
another app brought in) as a record file (stickle.sync.record_file), and a
record file taken in.

The fields are the row's columns, by their names below: a category's and a
mark's name, colour, icon, place in the order, when it was made, when it was
deleted (or null) and the one of the same name it became one with (or null);
a colour's value as #rrggbb, place, when made and deleted. A note brought in
has a field for each note it became here, "note:<id>", whose value is the hash
of its text as it came and when ({"text": "<sha256>", "at": "<time>"}), or the
mark "secret" for its text once that note was made secret. A field never
changed here goes as changed at the earliest time of all, by this device;
fields this version does not keep go as last seen in the vault.

As for notes, a value this version cannot take (one of another shape) is left
in the file and not taken here.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import cast

import apsw

from stickle.core.colors import PALETTE, is_hex
from stickle.core.hlc import Hlc
from stickle.core.labels import BUILT_IN_MARKS
from stickle.data import record_stamps
from stickle.data.imports import SECRET_MARK
from stickle.data.labels import become
from stickle.data.record_stamps import CATEGORY, COLOR, IMPORT, MARK, imported_field
from stickle.data.stamps import Stamp
from stickle.sync.contents import Contents, Field
from stickle.sync.format import Json, is_id
from stickle.sync.record_file import RecordFile

EARLIEST = Hlc(0)
MADE = "made"
MERGED_INTO = "merged_into"
DELETED = "deleted_at"

# Each kind's fields, and their columns.
COLUMNS: dict[str, dict[str, str]] = {
    CATEGORY: {
        "name": "name",
        "color": "color",
        "icon": "icon",
        "position": "position",
        MADE: "created_at",
        DELETED: "deleted_at",
        MERGED_INTO: "merged_into",
    },
    MARK: {
        "name": "name",
        "icon": "icon",
        "position": "position",
        MADE: "created_at",
        DELETED: "deleted_at",
        MERGED_INTO: "merged_into",
    },
    COLOR: {"hex": "hex", "position": "position", MADE: "created_at", DELETED: "deleted_at"},
}
TABLES = {CATEGORY: "categories", MARK: "marks", COLOR: "custom_colors"}
_IMPORTED = imported_field("")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_KEY = re.compile(r"[a-z][a-z0-9_-]{0,63}")  # a colour or an icon a newer version may add
_HASH = re.compile(r"[0-9a-f]{64}")
_NOT_TAKEN = object()


@dataclass(frozen=True)
class HereRecord:
    """An item as it is on this device."""

    values: Mapping[str, Json] | None  # its fields' values; None: not here
    stamps: Mapping[str, Stamp]
    sent: Mapping[str, int]
    seq: int  # the change number it was last changed at


def _imported(connection: apsw.Connection, item: str) -> dict[str, Json] | None:
    source, _, source_id = item.partition("\n")
    rows = connection.execute(
        "SELECT note_id, content_hash, imported_at FROM import_records"
        " WHERE source = ? AND source_id = ?"
        " ORDER BY note_id, content_hash = ? DESC, imported_at DESC, content_hash DESC",
        (source, source_id, SECRET_MARK),
    ).fetchall()
    if not rows:
        return None
    values: dict[str, Json] = {}
    for note_id, text, at in rows:
        values.setdefault(imported_field(str(note_id)), {"text": str(text), "at": str(at)})
    return values


def here(connection: apsw.Connection, kind: str, item: str) -> HereRecord:
    values: dict[str, Json] | None
    if kind == IMPORT:
        values = _imported(connection, item)
    else:
        columns = COLUMNS[kind]
        listed = ", ".join(columns.values())
        rows = connection.execute(
            f"SELECT {listed} FROM {TABLES[kind]} WHERE id = ?", (item,)
        ).fetchall()
        values = (
            {name: cast(Json, each) for name, each in zip(columns, rows[0], strict=True)}
            if rows
            else None
        )
    return HereRecord(
        values,
        record_stamps.of(connection, kind, item),
        record_stamps.sent_of(connection, kind, item),
        record_stamps.change_seq(connection, kind, item),
    )


def mine(
    device: str, at: HereRecord, kind: str, item: str, head: RecordFile | None
) -> RecordFile | None:
    """The item as this device has it, as a file (head: as last seen or put);
    None when it has nothing of it."""
    if at.values is None:
        return None
    before = Contents() if head is None else head.contents
    fields = dict(before.fields)  # those not kept here go as they were
    for name, value in at.values.items():
        when, by = at.stamps.get(name, (EARLIEST, None))
        old = before.fields.get(name)
        sent = at.sent.get(name)
        if old is None:
            fields[name] = Field(value, when, by or device, sent=sent)
        else:
            fields[name] = replace(old, value=value, hlc=when, device=by or device, sent=sent)
    return RecordFile(kind, item, Contents(fields, before.others))


def _ours(kind: str, name: str) -> bool:
    if kind == IMPORT:
        return name.startswith(_IMPORTED) and is_id(name.removeprefix(_IMPORTED))
    return name in COLUMNS[kind]


def _time(value: Json, empty: bool = False) -> object:
    if value is None and empty:
        return None
    return value if isinstance(value, str) and _TIMESTAMP.fullmatch(value) else _NOT_TAKEN


def _value(kind: str, name: str, value: Json) -> object:
    """value as the column takes it, or _NOT_TAKEN."""
    if kind == IMPORT:
        if not isinstance(value, dict):
            return _NOT_TAKEN
        text, at = value.get("text"), value.get("at")
        if not isinstance(text, str) or not (text == SECRET_MARK or _HASH.fullmatch(text)):
            return _NOT_TAKEN
        return value if _time(at) is not _NOT_TAKEN else _NOT_TAKEN
    if name == "name":
        if value is None and kind == MARK:
            return None
        return (
            value if isinstance(value, str) and value.strip() and "\n" not in value else _NOT_TAKEN
        )
    if name == "color":
        ok = isinstance(value, str) and (value in PALETTE or _KEY.fullmatch(value))
        return value if ok else _NOT_TAKEN
    if name == "icon":
        return value if isinstance(value, str) and _KEY.fullmatch(value) else _NOT_TAKEN
    if name == "hex":
        return value if isinstance(value, str) and is_hex(value) else _NOT_TAKEN
    if name == "position":
        ok = isinstance(value, int) and not isinstance(value, bool)
        return value if ok else _NOT_TAKEN
    if name == MADE:
        return _time(value, empty=kind == MARK)
    if name == DELETED:
        return _time(value, empty=True)
    if name == MERGED_INTO:
        ok = value is None or (isinstance(value, str) and is_id(value))
        return value if ok else _NOT_TAKEN
    return _NOT_TAKEN


# What a new row takes for a field it came without (or with one not taken).
_DEFAULTS: dict[str, dict[str, object]] = {
    CATEGORY: {"color": "yellow", "icon": "tag", "position": 0, MADE: "1970-01-01T00:00:00.000Z"},
    MARK: {"icon": "tag", "position": 0, MADE: None},
    COLOR: {"position": 0, MADE: "1970-01-01T00:00:00.000Z"},
}
_REQUIRED = {CATEGORY: "name", MARK: "name", COLOR: "hex"}


def _write_row(
    connection: apsw.Connection,
    kind: str,
    item: str,
    values: Mapping[str, object],
    new: bool,
    now: str,
) -> bool:
    columns = COLUMNS[kind]
    if new:
        if kind != MARK and values.get(_REQUIRED[kind]) is None:
            return False  # nothing it could be made with
        row = {**_DEFAULTS[kind], **values}
        if kind == MARK and row.get("name") is None and item not in BUILT_IN_MARKS:
            return False
        names = ["id", *(columns[name] for name in row), "updated_at"]
        connection.execute(
            f"INSERT INTO {TABLES[kind]} ({', '.join(names)})"
            f" VALUES ({', '.join('?' for _ in names)})",
            (item, *row.values(), now),  # pyright: ignore[reportArgumentType]
        )
        return True
    if values:
        listed = ", ".join(f"{columns[name]} = ?" for name in values)
        connection.execute(
            f"UPDATE {TABLES[kind]} SET {listed}, updated_at = ? WHERE id = ?",
            (*values.values(), now, item),  # pyright: ignore[reportArgumentType]
        )
    return True


def _write_imported(connection: apsw.Connection, item: str, values: Mapping[str, object]) -> None:
    source, _, source_id = item.partition("\n")
    for name, value in values.items():
        assert isinstance(value, dict)
        taken = cast(dict[str, str], value)
        note_id, text, at = name.removeprefix(_IMPORTED), taken["text"], taken["at"]
        if text == SECRET_MARK:
            connection.execute(
                "DELETE FROM import_records WHERE source = ? AND source_id = ? AND note_id = ?"
                " AND content_hash != ?",
                (source, source_id, note_id, SECRET_MARK),
            )
        else:
            secret = connection.execute(
                "SELECT 1 FROM import_records WHERE source = ? AND source_id = ? AND note_id = ?"
                " AND content_hash = ?",
                (source, source_id, note_id, SECRET_MARK),
            ).fetchall()
            if secret:
                continue  # its text's hash is not brought back
            # One text a note came with, as every device has it.
            connection.execute(
                "DELETE FROM import_records WHERE source = ? AND source_id = ? AND note_id = ?"
                " AND content_hash != ?",
                (source, source_id, note_id, text),
            )
        there = connection.execute(
            "SELECT note_id FROM import_records"
            " WHERE source = ? AND source_id = ? AND content_hash = ?",
            (source, source_id, text),
        ).fetchall()
        if there and str(there[0][0]) < note_id:
            continue  # one text brought into two notes: the one every device keeps names the first
        connection.execute(
            "INSERT INTO import_records"
            " (source, source_id, content_hash, note_id, imported_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (source, source_id, content_hash) DO UPDATE SET"
            " note_id = excluded.note_id, imported_at = excluded.imported_at",
            (source, source_id, text, note_id, at),
        )


def take(
    connection: apsw.Connection, device: str, record: RecordFile, at: HereRecord, now: str
) -> int | None:
    """The item as the file has it, here: what is not here already, of what this
    version can take. Its change number after (None: not taken, nothing it could
    be made with)."""
    kind, item = record.kind, record.item
    values: dict[str, object] = {}
    stamped: dict[str, tuple[Stamp, int | None]] = {}
    for name, each in sorted(record.contents.fields.items()):
        if not _ours(kind, name):
            continue
        stamp: Stamp = (each.hlc, None if each.device == device else each.device)
        if at.values is not None and (
            at.values.get(name, _NOT_TAKEN) == each.value
            and at.stamps.get(name, (EARLIEST, None)) == stamp
            and at.sent.get(name) == each.sent
        ):
            continue
        value = _value(kind, name, each.value)
        if value is _NOT_TAKEN:
            continue
        values[name] = value
        stamped[name] = (stamp, each.sent)
    if at.values is not None and not stamped:
        return at.seq
    if kind == IMPORT:
        if not values:
            return None
        _write_imported(connection, item, values)
    elif not _write_row(connection, kind, item, values, at.values is None, now):
        return None
    for name, (stamp, sent) in stamped.items():
        record_stamps.put(connection, kind, item, name, stamp, sent)
    return record_stamps.touched(connection, kind, item)


# Of categories, marks or colours alike, which stays

type Shown = Callable[[str, str | None], str]  # a mark's id and name -> the name it shows here


def own_name(mark_id: str, name: str | None) -> str:
    return name or mark_id


def _first(kind: str, item: str, made: object) -> tuple[object, ...]:
    """The order every device keeps the first of alike by: a built-in mark first,
    then the first made, then the lowest id."""
    built_in = BUILT_IN_MARKS.index(item) if kind == MARK and item in BUILT_IN_MARKS else None
    return (built_in is None, built_in or 0, "" if made is None else str(made), item)


def _groups(names: Mapping[str, set[str]]) -> list[set[str]]:
    """Items that share a name, together (through one another, too)."""
    owner: dict[str, str] = {}
    groups: dict[str, set[str]] = {}
    for item, each in sorted(names.items()):
        joined = {item}
        for name in each:
            if name in owner:
                joined |= groups.pop(owner[name], set())
        for member in joined:
            for name in names[member]:
                owner[name] = item
        groups[item] = joined
    return [group for group in groups.values() if len(group) > 1]


def merge_alike(
    connection: apsw.Connection, millis: int, now: str, shown: Shown = own_name
) -> list[tuple[str, str]]:
    """Categories of one name, marks of one name (as stored, or as shown here),
    colours alike: each the one that comes first (_first), the others become one
    with it (a colour: deleted). Those changed, as (kind, item)."""
    changed: list[tuple[str, str]] = []
    live = "deleted_at IS NULL AND merged_into IS NULL"
    for kind, query in (
        (CATEGORY, f"SELECT id, name, NULL, created_at FROM categories WHERE {live}"),
        (MARK, f"SELECT id, name, NULL, created_at FROM marks WHERE {live}"),
        (COLOR, "SELECT id, NULL, hex, created_at FROM custom_colors WHERE deleted_at IS NULL"),
    ):
        made: dict[str, object] = {}
        names: dict[str, set[str]] = {}
        for item, name, hex_value, created in connection.execute(query):
            item = str(item)
            made[item] = created
            if kind == COLOR:
                names[item] = {str(hex_value)}
            else:
                own = None if name is None else str(name)
                found = {own_name(item, own).casefold()}
                if kind == MARK:
                    found.add(shown(item, own).casefold())
                names[item] = found
        for group in _groups(names):
            first, *others = sorted(group, key=lambda item: _first(kind, item, made[item]))
            for item in others:
                if kind == COLOR:
                    connection.execute(
                        "UPDATE custom_colors SET deleted_at = ?, updated_at = ? WHERE id = ?",
                        (now, now, item),
                    )
                    record_stamps.changed(connection, kind, item, [DELETED], millis)
                else:
                    connection.execute(
                        f"UPDATE {TABLES[kind]} SET merged_into = ?, updated_at = ? WHERE id = ?",
                        (first, now, item),
                    )
                    record_stamps.changed(connection, kind, item, [MERGED_INTO], millis)
                changed.append((kind, item))
    return changed


def merged(connection: apsw.Connection, kind: str) -> dict[str, str]:
    """The categories or marks that became one with another: each, and the one it is now."""
    table = TABLES[kind]
    rows = connection.execute(f"SELECT id FROM {table} WHERE merged_into IS NOT NULL")
    found = {str(item): become(connection, table, str(item)) for (item,) in rows}
    return {item: now for item, now in found.items() if now != item}
