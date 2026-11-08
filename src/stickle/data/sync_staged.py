"""Files of a vault a round has read before taking anything in, kept beside the
notes by their place in the vault: what the round takes in is read from here,
one at a time, so that a round looking at many changed files at once does not
hold them all, and nothing is fetched twice.

A file read and not there is kept too, with no bytes.
"""

from dataclasses import dataclass

import apsw


@dataclass(frozen=True)
class Staged:
    tag: str | None
    data: bytes | None  # None: read, and not there


def get(connection: apsw.Connection, vault: str, path: str) -> Staged | None:
    rows = connection.execute(
        "SELECT tag, data FROM sync_staged WHERE vault = ? AND path = ?", (vault, path)
    ).fetchall()
    if not rows:
        return None
    tag, data = rows[0]
    return Staged(None if tag is None else str(tag), None if data is None else bytes(data))  # pyright: ignore[reportArgumentType]


def put(connection: apsw.Connection, vault: str, path: str, staged: Staged) -> None:
    connection.execute(
        "INSERT INTO sync_staged (vault, path, tag, data) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (vault, path) DO UPDATE SET tag = excluded.tag, data = excluded.data",
        (vault, path, staged.tag, staged.data),
    )


def tags(connection: apsw.Connection, vault: str) -> dict[str, str | None]:
    """The tag of each file kept, by its place."""
    rows = connection.execute("SELECT path, tag FROM sync_staged WHERE vault = ?", (vault,))
    return {str(path): None if tag is None else str(tag) for path, tag in rows}


def clear(connection: apsw.Connection, vault: str) -> None:
    connection.execute("DELETE FROM sync_staged WHERE vault = ?", (vault,))
