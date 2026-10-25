"""The key files kept in the database as well (stickle.crypto.key_files).

Only a way back when both copies of a file are gone: the files count. A copy
that cannot be read or written here is logged, never in the way.
"""

import logging

import apsw

log = logging.getLogger(__name__)


class KeyCopies:
    def __init__(self, connection: apsw.Connection) -> None:
        self._connection = connection

    def get(self, name: str) -> bytes | None:
        try:
            row = self._connection.execute(
                "SELECT content FROM key_copies WHERE name = ?", (name,)
            ).fetchone()
        except apsw.Error as error:
            log.warning("could not read a key copy: %s", type(error).__name__)
            return None
        if row is None or not isinstance(row[0], bytes):
            return None
        return row[0]

    def put(self, name: str, content: bytes) -> None:
        try:
            self._connection.execute(
                "INSERT INTO key_copies (name, content) VALUES (?, ?)"
                " ON CONFLICT (name) DO UPDATE SET content = excluded.content",
                (name, content),
            )
        except apsw.Error as error:
            log.warning("could not keep a key copy: %s", type(error).__name__)
