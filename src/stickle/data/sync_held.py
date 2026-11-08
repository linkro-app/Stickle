"""What a round of sync stopped for, kept beside the notes until a round takes
it in, as the user chooses (stickle.sync.engine.BigChangeError): many notes at
once that would go from sight here, to the trash or deleted for good, or
conflict copies that would be made.
"""

from collections.abc import Iterable
from dataclasses import dataclass

import apsw

TRASH = "trash"  # a note here, out of the trash, that would go to it
GONE = "gone"  # a note here, out of the trash, that would be deleted for good
COPY = "copy"  # a conflict copy that would be made


@dataclass(frozen=True)
class Held:
    note_id: str  # for a copy, the copy's own id
    kind: str
    copy_of: str | None = None  # the note a copy is of


def every(connection: apsw.Connection, vault: str) -> list[Held]:
    rows = connection.execute(
        "SELECT note_id, kind, copy_of FROM sync_held WHERE vault = ? ORDER BY kind, note_id",
        (vault,),
    )
    return [
        Held(str(note_id), str(kind), None if copy_of is None else str(copy_of))
        for note_id, kind, copy_of in rows
    ]


def keep(connection: apsw.Connection, vault: str, held: Iterable[Held]) -> None:
    """These, in place of what was kept."""
    clear(connection, vault)
    for each in held:
        connection.execute(
            "INSERT OR REPLACE INTO sync_held (vault, note_id, kind, copy_of) VALUES (?, ?, ?, ?)",
            (vault, each.note_id, each.kind, each.copy_of),
        )


def clear(connection: apsw.Connection, vault: str) -> None:
    connection.execute("DELETE FROM sync_held WHERE vault = ?", (vault,))
