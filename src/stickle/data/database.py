"""Open the encrypted local database.

The whole file is encrypted with SQLite3 Multiple Ciphers (ChaCha20-Poly1305,
every page authenticated), including the write-ahead log. The key is 32
random bytes (kept by the credential store, or wrapped with a password), not a password,
so the password key derivation is reduced to a single round: stretching a
random 256-bit key adds no security but would slow down every start.
"""

from pathlib import Path

import apsw

KEY_BYTES = 32


class WrongKeyError(Exception):
    """The file is not a database encrypted with this key (or is damaged)."""


def open_database(path: Path, key: bytes, read_only: bool = False) -> apsw.Connection:
    """Open (or create) the database. read_only: look without changing the file
    at all, not even its journal mode (for backups)."""
    if len(key) != KEY_BYTES:
        raise ValueError(f"key must be {KEY_BYTES} bytes")
    flags = (
        apsw.SQLITE_OPEN_READONLY
        if read_only
        else apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_CREATE
    )
    connection = apsw.Connection(str(path), flags=flags)
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
    # Sorting and temporary tables stay in memory instead of unencrypted temp files.
    connection.pragma("temp_store", "memory")
    return connection
