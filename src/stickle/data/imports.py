"""Which notes of other apps were brought in already (see stickle.importers).

A note is known by a hash of its text as it came. Once the note it became is
made secret here, those hashes go (they would tell whether a guess of its
text is right, from a full export too): one record says it came, whatever
its text, so it is not brought in again, in the clear, even changed.

For sync, the records of one note of another app are one item
(stickle.data.record_stamps), with a field for each note it became here.
"""

import hashlib
from collections.abc import Callable

import apsw

from stickle.core.clock import Clock, utc_now
from stickle.core.hlc import wall_millis
from stickle.data import record_stamps
from stickle.data.record_stamps import IMPORT, import_item, imported_field

SECRET_MARK = "secret"  # in place of the hashes of a note made secret

# The records of the note given (by id) brought down to one per note of the
# other app, marked. (The secret notes there were already are marked by the
# upgrade to version 10, stickle.data.schema.)
HIDE_HASHES = f"""
INSERT OR IGNORE INTO import_records (source, source_id, content_hash, note_id, imported_at)
    SELECT source, source_id, '{SECRET_MARK}', note_id, min(imported_at) FROM import_records
    WHERE note_id = ? GROUP BY source, source_id, note_id;
DELETE FROM import_records WHERE note_id = ? AND content_hash != '{SECRET_MARK}';
"""


def text_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def hide_text_hashes(
    connection: apsw.Connection, note_id: str, millis: Callable[[], int] = wall_millis
) -> None:
    """The note was made secret: its texts' hashes replaced by the mark."""
    sources = connection.execute(
        "SELECT DISTINCT source, source_id FROM import_records"
        f" WHERE note_id = ? AND content_hash != '{SECRET_MARK}'",
        (note_id,),
    ).fetchall()
    connection.execute(HIDE_HASHES, (note_id, note_id))
    for source, source_id in sources:
        item = import_item(str(source), str(source_id))
        record_stamps.changed(connection, IMPORT, item, [imported_field(note_id)], millis())


class ImportRecords:
    def __init__(
        self,
        connection: apsw.Connection,
        clock: Clock = utc_now,
        millis: Callable[[], int] = wall_millis,
    ) -> None:
        self._connection = connection
        self._clock = clock
        self._millis = millis

    def brought_in(self, source: str, source_id: str, body: str) -> bool:
        """Whether this note, as it is now, came in before (or, made secret here,
        as it was then)."""
        rows = self._connection.execute(
            "SELECT 1 FROM import_records WHERE source = ? AND source_id = ?"
            " AND content_hash IN (?, ?)",
            (source, source_id, text_hash(body), SECRET_MARK),
        ).fetchall()
        return bool(rows)

    def record(self, source: str, source_id: str, body: str, note_id: str) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO import_records"
                " (source, source_id, content_hash, note_id, imported_at) VALUES (?, ?, ?, ?, ?)",
                (source, source_id, text_hash(body), note_id, self._clock()),
            )
            if self._connection.changes():
                record_stamps.changed(
                    self._connection,
                    IMPORT,
                    import_item(source, source_id),
                    [imported_field(note_id)],
                    self._millis(),
                )
