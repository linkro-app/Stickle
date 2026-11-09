"""Open the encrypted local database.

The whole file is encrypted with SQLite3 Multiple Ciphers (ChaCha20-Poly1305,
every page authenticated), including the write-ahead log. The key is 32
random bytes (kept by the credential store, or wrapped with a password), not a password,
so the password key derivation is reduced to a single round: stretching a
random 256-bit key adds no security but would slow down every start.
"""

import os
import threading
from pathlib import Path
from types import TracebackType
from typing import Any, override

import apsw

KEY_BYTES = 32
WAIT_MS = 3_000  # for another connection's write to end, before a write gives up

_turns: dict[str, threading.Lock] = {}
_turns_guard = threading.Lock()


class WrongKeyError(Exception):
    """The file is not a database encrypted with this key (or is damaged)."""


def _turn_of(path: str) -> threading.Lock:
    """What the connections of this process to one file take turns with."""
    with _turns_guard:
        return _turns.setdefault(os.path.normcase(Path(path).resolve()), threading.Lock())


class _Connection(apsw.Connection):
    """A connection whose transactions (with connection: ...) begin as ones that
    write, one at a time among this process's connections to the file. Sync
    writes on a connection of its own: a transaction begun by reading and writing
    later is turned down at once if that one wrote in between, however long it is
    willing to wait; and SQLite's waiting, which sleeps and tries again, can miss
    every gap between the writes of a connection that goes on writing."""

    def __init__(self, path: str, wait_ms: int, **kwargs: Any) -> None:
        super().__init__(path, **kwargs)
        self.set_busy_timeout(wait_ms)
        self._wait = wait_ms / 1000
        self._turn = _turn_of(path)
        self._begun: list[bool] = []

    @override
    def __enter__(self) -> apsw.Connection:
        begins = not self.in_transaction
        if begins:
            if not self._turn.acquire(timeout=self._wait):
                raise apsw.BusyError("another connection goes on writing")
            try:
                self.execute("BEGIN IMMEDIATE")
            except BaseException:
                self._turn.release()
                raise
        self._begun.append(begins)
        return super().__enter__()

    @override
    def __exit__(
        self,
        etype: type[BaseException] | None,
        evalue: BaseException | None,
        etraceback: TracebackType | None,
    ) -> bool:
        begun = self._begun.pop()
        try:
            answer = super().__exit__(etype, evalue, etraceback)
            if begun:
                self.execute("COMMIT" if etype is None else "ROLLBACK")
            return answer
        except BaseException:
            if begun and self.in_transaction:
                self.execute("ROLLBACK")
            raise
        finally:
            if begun:
                self._turn.release()


def open_database(
    path: Path, key: bytes, read_only: bool = False, wait_ms: int = WAIT_MS
) -> apsw.Connection:
    """Open (or create) the database. read_only: look without changing the file
    at all, not even its journal mode (for backups). wait_ms: how long a write
    waits for another connection's to end."""
    if len(key) != KEY_BYTES:
        raise ValueError(f"key must be {KEY_BYTES} bytes")
    flags = (
        apsw.SQLITE_OPEN_READONLY
        if read_only
        else apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_CREATE
    )
    if read_only:
        connection = apsw.Connection(str(path), flags=flags)
        connection.set_busy_timeout(wait_ms)
    else:
        connection = _Connection(str(path), wait_ms, flags=flags)
    try:
        connection.pragma("cipher", "chacha20")
        connection.pragma("kdf_iter", 1)
        connection.pragma("hexkey", key.hex())
        # Reading the schema is the first access that decrypts a page.
        connection.execute("SELECT count(*) FROM sqlite_schema").fetchall()
    except apsw.NotADBError as error:
        connection.close()
        raise WrongKeyError(str(path.name)) from error
    if read_only:
        return connection
    connection.pragma("journal_mode", "wal")
    # Every committed save reaches the disk before the commit returns, so a power
    # loss keeps the last save. (SQLite's default today, pinned so it stays.)
    connection.pragma("synchronous", "FULL")
    # What is deleted or replaced is overwritten, not left in free pages: the text
    # of a note made secret must not stay readable with the database's key.
    connection.pragma("secure_delete", True)
    # Sorting and temporary tables stay in memory instead of unencrypted temp files.
    connection.pragma("temp_store", "memory")
    return connection
