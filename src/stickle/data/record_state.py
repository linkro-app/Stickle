"""What sync knows of each record and device of a vault it syncs with, kept
in the local database (see stickle.sync.records), as stickle.data.sync_state
keeps it for notes.

A record is known by its file's id in the vault and by what it is here (its
kind and item, as stickle.data.record_stamps): the file as last seen there and
its tag, the file put there and not seen there yet (the outbox, kept until it
is), and the item's change number when it was last merged. A device, by its
own file there as last seen, and that file's tag.
"""

from collections.abc import Mapping
from dataclasses import dataclass

import apsw

from stickle.data.record_stamps import CATEGORY, COLOR, IMPORT, MARK


@dataclass(frozen=True)
class RecordState:
    kind: str
    item: str
    seen: str | None = None  # the file as last seen in the vault, as JSON
    tag: str | None = None  # that file's tag
    outbox: str | None = None  # the file put there and not seen there yet
    merged_seq: int = 0  # the item's change number when it was last merged


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _state(row: apsw.SQLiteValues) -> RecordState:
    kind, item, seen, tag, outbox, seq = row
    return RecordState(str(kind), str(item), _text(seen), _text(tag), _text(outbox), int(seq))  # pyright: ignore[reportArgumentType]


_COLUMNS = "kind, item, seen, tag, outbox, merged_seq"


def get(connection: apsw.Connection, vault: str, record: str) -> RecordState | None:
    rows = connection.execute(
        f"SELECT {_COLUMNS} FROM sync_records WHERE vault = ? AND record = ?", (vault, record)
    ).fetchall()
    return _state(rows[0]) if rows else None


def put(connection: apsw.Connection, vault: str, record: str, state: RecordState) -> None:
    connection.execute(
        "INSERT INTO sync_records (vault, record, kind, item, seen, tag, outbox, merged_seq)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (vault, record) DO UPDATE"
        " SET kind = excluded.kind, item = excluded.item, seen = excluded.seen,"
        " tag = excluded.tag, outbox = excluded.outbox, merged_seq = excluded.merged_seq",
        (
            vault,
            record,
            state.kind,
            state.item,
            state.seen,
            state.tag,
            state.outbox,
            state.merged_seq,
        ),
    )


def known(connection: apsw.Connection, vault: str) -> dict[str, RecordState]:
    """Every record of the vault sync knows of, by its file's id."""
    rows = connection.execute(
        f"SELECT record, {_COLUMNS} FROM sync_records WHERE vault = ?", (vault,)
    )
    return {str(row[0]): _state(row[1:]) for row in rows}


def unconfirmed(connection: apsw.Connection, vault: str) -> list[str]:
    """The records put in the vault and not seen there yet."""
    rows = connection.execute(
        "SELECT record FROM sync_records WHERE vault = ? AND outbox IS NOT NULL ORDER BY record",
        (vault,),
    )
    return [str(row[0]) for row in rows]


# Every item here, as (kind, item).
_HERE = f"""
    SELECT '{CATEGORY}' AS kind, id AS item FROM categories
    UNION ALL SELECT '{MARK}', id FROM marks
    UNION ALL SELECT '{COLOR}', id FROM custom_colors
    UNION SELECT '{IMPORT}', source || char(10) || source_id FROM import_records
"""


def changed(connection: apsw.Connection, vault: str) -> list[tuple[str, str]]:
    """The items here changed since they were last merged with the vault, or never merged."""
    rows = connection.execute(
        f"WITH here AS ({_HERE}) SELECT here.kind, here.item FROM here"
        " LEFT JOIN record_changes AS c ON c.kind = here.kind AND c.item = here.item"
        " LEFT JOIN sync_records AS s"
        " ON s.vault = ? AND s.kind = here.kind AND s.item = here.item"
        " WHERE s.record IS NULL OR coalesce(c.change_seq, 0) > s.merged_seq"
        " ORDER BY here.kind, here.item",
        (vault,),
    )
    return [(str(kind), str(item)) for kind, item in rows]


def device(
    connection: apsw.Connection, vault: str, device_id: str
) -> tuple[str | None, str | None]:
    """A device's file as last seen in the vault, and its tag."""
    rows = connection.execute(
        "SELECT seen, tag FROM sync_devices WHERE vault = ? AND device = ?", (vault, device_id)
    ).fetchall()
    return (_text(rows[0][0]), _text(rows[0][1])) if rows else (None, None)


def put_device(
    connection: apsw.Connection, vault: str, device_id: str, seen: str, tag: str | None
) -> None:
    connection.execute(
        "INSERT INTO sync_devices (vault, device, seen, tag) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (vault, device) DO UPDATE SET seen = excluded.seen, tag = excluded.tag",
        (vault, device_id, seen, tag),
    )


def taken(connection: apsw.Connection, vault: str) -> dict[str, int]:
    """Each device's last round of what it put that was taken in here."""
    rows = connection.execute("SELECT device, taken FROM sync_devices WHERE vault = ?", (vault,))
    return {str(device): int(round_) for device, round_ in rows}  # pyright: ignore[reportArgumentType]


def set_taken(connection: apsw.Connection, vault: str, rounds: Mapping[str, int]) -> None:
    for device, round_ in rounds.items():
        connection.execute(
            "UPDATE sync_devices SET taken = ? WHERE vault = ? AND device = ?",
            (round_, vault, device),
        )


def devices(connection: apsw.Connection, vault: str) -> dict[str, tuple[str | None, str | None]]:
    """Every device of the vault whose file was seen: its file, and its tag."""
    rows = connection.execute(
        "SELECT device, seen, tag FROM sync_devices WHERE vault = ?", (vault,)
    )
    return {str(device_id): (_text(seen), _text(tag)) for device_id, seen, tag in rows}


def device_name(connection: apsw.Connection, device_id: str) -> str | None:
    """What a device is called, as its file in a vault last said; None: not known."""
    rows = connection.execute(
        "SELECT json_extract(seen, '$.fields.name.value') FROM sync_devices"
        " WHERE device = ? AND json_valid(seen) ORDER BY vault",
        (device_id,),
    ).fetchall()
    names = [str(name) for (name,) in rows if isinstance(name, str) and name.strip()]
    return names[0] if names else None
