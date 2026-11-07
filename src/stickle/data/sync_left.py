"""The files of a vault sync leaves as they are, kept beside the notes, by their
place in the vault (stickle.sync.left): one that does not open, and for how
many rounds running; one set aside; one with a field this version may not
change; one that is not this vault's, or not where an item's own file is.

Each is told from what is written there next by its tag or, from a cloud
that gives none, a hash of its bytes.
"""

from dataclasses import dataclass

import apsw

UNREADABLE = "unreadable"  # does not open: read again each round, counted
SET_ASIDE = "set aside"  # its bytes kept here; nothing of it taken in
READ_ONLY = "read only"  # a field this version may not change: an update opens it
NOT_OURS = "not ours"  # another vault's, or no item's own: nothing of this device's


@dataclass(frozen=True)
class Left:
    tag: str | None
    digest: str  # SHA-256 of its bytes, hex
    why: str
    fails: int = 0  # rounds running it did not open
    first_at: int = 0  # the first of them, by the cloud's clock


_COLUMNS = "tag, digest, why, fails, first_at"


def _left(row: apsw.SQLiteValues) -> Left:
    tag, digest, why, fails, first_at = row
    return Left(
        None if tag is None else str(tag),
        str(digest),
        str(why),
        int(fails),  # pyright: ignore[reportArgumentType]
        int(first_at),  # pyright: ignore[reportArgumentType]
    )


def get(connection: apsw.Connection, vault: str, path: str) -> Left | None:
    rows = connection.execute(
        f"SELECT {_COLUMNS} FROM sync_left WHERE vault = ? AND path = ?", (vault, path)
    ).fetchall()
    return _left(rows[0]) if rows else None


def every(connection: apsw.Connection, vault: str) -> dict[str, Left]:
    rows = connection.execute(f"SELECT path, {_COLUMNS} FROM sync_left WHERE vault = ?", (vault,))
    return {str(row[0]): _left(row[1:]) for row in rows}


def put(connection: apsw.Connection, vault: str, path: str, left: Left) -> None:
    connection.execute(
        "INSERT INTO sync_left (vault, path, tag, digest, why, fails, first_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (vault, path) DO UPDATE SET"
        " tag = excluded.tag, digest = excluded.digest, why = excluded.why,"
        " fails = excluded.fails, first_at = excluded.first_at",
        (vault, path, left.tag, left.digest, left.why, left.fails, left.first_at),
    )


def drop(connection: apsw.Connection, vault: str, path: str) -> None:
    connection.execute("DELETE FROM sync_left WHERE vault = ? AND path = ?", (vault, path))


def count(connection: apsw.Connection, vault: str, why: str, folder: str) -> int:
    """How many files of the vault's folder ("notes") are left for that reason."""
    rows = connection.execute(
        "SELECT count(*) FROM sync_left WHERE vault = ? AND why = ? AND substr(path, 1, ?) = ?",
        (vault, why, len(folder) + 1, f"{folder}/"),
    ).fetchall()
    return int(rows[0][0])  # pyright: ignore[reportArgumentType]
