"""When each field of a note was last changed, and where: what sync goes by to
keep the later of two changes to a field (stickle.core.hlc).

A field never changed has no stamp, which counts as the earliest time of all:
a note just made, or made again from an id every device gives it (what's new),
does not undo what was done to it elsewhere. Its text is not a field: it goes
by versions. Each mark on a note is a field of its own, so that marks put on
it on two devices are both kept.
"""

from collections.abc import Iterable

import apsw

from stickle.core.hlc import Hlc, after

# The note's columns that are fields, and the fields' names.
FIELDS = {
    "color": "color",
    "opacity": "opacity",
    "label": "category",
    "always_on_top": "always_on_top",
    "hidden": "hidden",
    "collapsed": "collapsed",
    "locked": "locked",
    "deleted_at": "deleted_at",
    "secret_title": "secret_title",
}
_MARK = "mark:"

type Stamp = tuple[Hlc, str | None]  # the time, and the device (None: here)


def mark_field(mark_id: str) -> str:
    return _MARK + mark_id


def fields_of(columns: Iterable[str]) -> list[str]:
    """The fields that changing these columns changes."""
    return [FIELDS[column] for column in columns if column in FIELDS]


def _time(text: object) -> Hlc | None:
    try:
        return Hlc.parse(str(text))
    except ValueError:  # not ours to read: as never changed, and stamped anew
        return None


def changed(connection: apsw.Connection, note_id: str, fields: Iterable[str], millis: int) -> None:
    """These fields of the note changed here, at millis: each stamped then, or
    just after the time of the value it replaced if that is not earlier; not
    sent yet."""
    for name in fields:
        rows = connection.execute(
            "SELECT hlc FROM note_stamps WHERE note_id = ? AND field = ?", (note_id, name)
        ).fetchall()
        made = after(millis, _time(rows[0][0]) if rows else None)
        connection.execute(
            "INSERT INTO note_stamps (note_id, field, hlc, device) VALUES (?, ?, ?, NULL)"
            " ON CONFLICT (note_id, field) DO UPDATE"
            " SET hlc = excluded.hlc, device = NULL, sent = NULL",
            (note_id, name, made.text),
        )


def of(connection: apsw.Connection, note_id: str) -> dict[str, Stamp]:
    """The note's stamped fields; one not here was never changed."""
    found: dict[str, Stamp] = {}
    for name, text, device in connection.execute(
        "SELECT field, hlc, device FROM note_stamps WHERE note_id = ?", (note_id,)
    ):
        when = _time(text)
        if when is not None:
            found[str(name)] = (when, None if device is None else str(device))
    return found


def forget(connection: apsw.Connection, note_id: str) -> None:
    connection.execute("DELETE FROM note_stamps WHERE note_id = ?", (note_id,))
