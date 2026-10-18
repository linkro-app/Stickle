"""Which notes of other apps were brought in already (see stickle.importers)."""

import hashlib

import apsw

from stickle.core.clock import Clock, utc_now


def text_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ImportRecords:
    def __init__(self, connection: apsw.Connection, clock: Clock = utc_now) -> None:
        self._connection = connection
        self._clock = clock

    def brought_in(self, source: str, source_id: str, body: str) -> bool:
        """Whether this note, as it is now, came in before."""
        rows = self._connection.execute(
            "SELECT 1 FROM import_records WHERE source = ? AND source_id = ? AND content_hash = ?",
            (source, source_id, text_hash(body)),
        ).fetchall()
        return bool(rows)

    def record(self, source: str, source_id: str, body: str, note_id: str) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO import_records"
            " (source, source_id, content_hash, note_id, imported_at) VALUES (?, ?, ?, ?, ?)",
            (source, source_id, text_hash(body), note_id, self._clock()),
        )
