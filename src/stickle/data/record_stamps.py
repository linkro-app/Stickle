"""When each field of a category, a mark, a colour of the user's own and a
note brought in from another app was last changed, and where: as
stickle.data.stamps for notes' fields, what sync goes by to keep the later of
two changes to a field. A field never changed has no stamp, the earliest time
of all. Each item changed here also keeps the change number it was last
changed at (the notes' counter), so that sync knows what changed since.

An item is known by its kind and its id here: a category's, a mark's, a
colour's; one brought in, by its app and its id there (import_item).
"""

from collections.abc import Iterable

import apsw

from stickle.core.hlc import Hlc, after
from stickle.data.stamps import Stamp

CATEGORY = "category"
MARK = "mark"
COLOR = "color"
IMPORT = "import"  # a note of another app, brought in: one field for each note it became
KINDS = (CATEGORY, MARK, COLOR, IMPORT)
_IMPORTED = "note:"


def import_item(source: str, source_id: str) -> str:
    return f"{source}\n{source_id}"


def imported_field(note_id: str) -> str:
    """An import record's field for a note it became."""
    return _IMPORTED + note_id


def _next_seq(connection: apsw.Connection) -> int:
    row = connection.execute(
        "UPDATE local_counters SET value = value + 1 WHERE key = 'change_seq' RETURNING value"
    ).fetchall()
    return int(row[0][0])  # pyright: ignore[reportArgumentType]


def touched(connection: apsw.Connection, kind: str, item: str) -> int:
    """The item changed here (made, say): its change number now."""
    seq = _next_seq(connection)
    connection.execute(
        "INSERT INTO record_changes (kind, item, change_seq) VALUES (?, ?, ?)"
        " ON CONFLICT (kind, item) DO UPDATE SET change_seq = excluded.change_seq",
        (kind, item, seq),
    )
    return seq


def _time(text: object) -> Hlc | None:
    try:
        return Hlc.parse(str(text))
    except ValueError:  # not ours to read: as never changed, and stamped anew
        return None


def changed(
    connection: apsw.Connection, kind: str, item: str, fields: Iterable[str], millis: int
) -> None:
    """These fields of the item changed here, at millis: each stamped then, or
    just after the time of the value it replaced if that is not earlier; not
    sent yet."""
    for name in fields:
        rows = connection.execute(
            "SELECT hlc FROM record_stamps WHERE kind = ? AND item = ? AND field = ?",
            (kind, item, name),
        ).fetchall()
        made = after(millis, _time(rows[0][0]) if rows else None)
        connection.execute(
            "INSERT INTO record_stamps (kind, item, field, hlc, device) VALUES (?, ?, ?, ?, NULL)"
            " ON CONFLICT (kind, item, field) DO UPDATE"
            " SET hlc = excluded.hlc, device = NULL, sent = NULL",
            (kind, item, name, made.text),
        )
    touched(connection, kind, item)


def of(connection: apsw.Connection, kind: str, item: str) -> dict[str, Stamp]:
    """The item's stamped fields; one not here was never changed."""
    found: dict[str, Stamp] = {}
    for name, text, device in connection.execute(
        "SELECT field, hlc, device FROM record_stamps WHERE kind = ? AND item = ?", (kind, item)
    ):
        when = _time(text)
        if when is not None:
            found[str(name)] = (when, None if device is None else str(device))
    return found


def sent_of(connection: apsw.Connection, kind: str, item: str) -> dict[str, int]:
    """When the item's fields first reached the cloud, those that have."""
    return {
        str(name): int(sent)  # pyright: ignore[reportArgumentType]
        for name, sent in connection.execute(
            "SELECT field, sent FROM record_stamps"
            " WHERE kind = ? AND item = ? AND sent IS NOT NULL",
            (kind, item),
        )
    }


def put(
    connection: apsw.Connection,
    kind: str,
    item: str,
    name: str,
    stamp: Stamp,
    sent: int | None,
) -> None:
    """A field's stamp as it is elsewhere (taken from the cloud, or sent there)."""
    when, device = stamp
    connection.execute(
        "INSERT INTO record_stamps (kind, item, field, hlc, device, sent)"
        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (kind, item, field) DO UPDATE"
        " SET hlc = excluded.hlc, device = excluded.device, sent = excluded.sent",
        (kind, item, name, when.text, device, sent),
    )


def change_seq(connection: apsw.Connection, kind: str, item: str) -> int:
    """The change number the item was last changed at here; 0: not since this was kept."""
    rows = connection.execute(
        "SELECT change_seq FROM record_changes WHERE kind = ? AND item = ?", (kind, item)
    ).fetchall()
    return int(rows[0][0]) if rows else 0  # pyright: ignore[reportArgumentType]
