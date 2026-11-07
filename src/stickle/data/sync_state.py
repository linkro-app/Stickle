"""What sync knows of each note in a vault, kept beside the notes.

For each note: its file as last seen in the vault and that file's tag, the
file put there and not seen there yet (the outbox), the note's change number
when it was last merged, and whether its text is at a version that deletes it.
The files are JSON, as stickle.sync writes them; to this module they are text.

A note emptied from the trash, or made secret, has its text taken out of both
files here: its text is never kept in the clear after that. What is left of
them, the versions and fields, is what the next version is made on.

Each vault is kept apart: a device that synced with another vault before
keeps what it knew of that one.
"""

from collections.abc import Iterable
from dataclasses import dataclass

import apsw


@dataclass(frozen=True)
class NoteState:
    seen: str | None = None  # the note's file as last seen in the vault
    tag: str | None = None  # that file's tag
    outbox: str | None = None  # the file put there, not seen there yet
    merged_seq: int = 0  # the note's change number when last merged
    deleted: bool = False  # its text is at a version that deletes it
    gone: bool = False  # its file went from the vault: kept here only, not looked for


_COLUMNS = "seen, tag, outbox, merged_seq, deleted, gone"


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _state(row: apsw.SQLiteValues) -> NoteState:
    seen, tag, outbox, seq, deleted, gone = row
    return NoteState(
        _text(seen),
        _text(tag),
        _text(outbox),
        int(seq),  # pyright: ignore[reportArgumentType]
        bool(deleted),
        bool(gone),
    )


def get(connection: apsw.Connection, vault: str, note_id: str) -> NoteState | None:
    rows = connection.execute(
        f"SELECT {_COLUMNS} FROM sync_notes WHERE vault = ? AND note_id = ?", (vault, note_id)
    ).fetchall()
    return _state(rows[0]) if rows else None


def put(connection: apsw.Connection, vault: str, note_id: str, state: NoteState) -> None:
    connection.execute(
        "INSERT INTO sync_notes (vault, note_id, seen, tag, outbox, merged_seq, deleted, gone)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (vault, note_id) DO UPDATE SET"
        " seen = excluded.seen, tag = excluded.tag, outbox = excluded.outbox,"
        " merged_seq = excluded.merged_seq, deleted = excluded.deleted, gone = excluded.gone",
        (
            vault,
            note_id,
            state.seen,
            state.tag,
            state.outbox,
            state.merged_seq,
            int(state.deleted),
            int(state.gone),
        ),
    )


def drop(connection: apsw.Connection, vault: str, note_id: str) -> None:
    connection.execute("DELETE FROM sync_notes WHERE vault = ? AND note_id = ?", (vault, note_id))


def forget_text(connection: apsw.Connection, note_id: str) -> None:
    """The note's text out of what every vault's files here hold of it."""
    connection.execute(
        "UPDATE sync_notes SET seen = json_remove(seen, '$.body.text'),"
        " outbox = json_remove(outbox, '$.body.text') WHERE note_id = ?",
        (note_id,),
    )


def _ids(rows: Iterable[apsw.SQLiteValues]) -> list[str]:
    return [str(row[0]) for row in rows]


def changed(connection: apsw.Connection, vault: str) -> list[str]:
    """Notes not secret that changed here since they were last merged, or never
    were: by id."""
    return _ids(
        connection.execute(
            "SELECT notes.id FROM notes LEFT JOIN sync_notes"
            " ON sync_notes.vault = ? AND sync_notes.note_id = notes.id"
            " WHERE notes.secret_body IS NULL"
            " AND (sync_notes.note_id IS NULL OR notes.change_seq > sync_notes.merged_seq)"
            " ORDER BY notes.id",
            (vault,),
        )
    )


def mentioning(connection: apsw.Connection, vault: str, ids: Iterable[str]) -> list[str]:
    """Notes whose file in the vault, as last seen or put, or whose category or
    marks here, name one of these ids (of categories and marks): by id."""
    found: set[str] = set()
    for each in ids:
        # A category is a value ("<id>"), a mark a field's name ("mark:<id>").
        value, mark = f'"{each}"', f'"mark:{each}"'
        found.update(
            _ids(
                connection.execute(
                    "SELECT note_id FROM sync_notes WHERE vault = ?"
                    " AND (instr(coalesce(seen, ''), ?) OR instr(coalesce(outbox, ''), ?)"
                    " OR instr(coalesce(seen, ''), ?) OR instr(coalesce(outbox, ''), ?))"
                    " UNION SELECT id FROM notes WHERE label = ?"
                    " UNION SELECT note_id FROM note_marks WHERE mark_id = ?",
                    (vault, value, value, mark, mark, each, each),
                )
            )
        )
    return sorted(found)


def emptied(connection: apsw.Connection, vault: str) -> list[str]:
    """Notes emptied from the trash here that the vault has, not deleted there yet."""
    return _ids(
        connection.execute(
            "SELECT sync_notes.note_id FROM sync_notes JOIN deletion_records"
            " ON deletion_records.note_id = sync_notes.note_id"
            " WHERE sync_notes.vault = ? AND sync_notes.deleted = 0"
            " AND NOT EXISTS (SELECT 1 FROM notes WHERE notes.id = sync_notes.note_id)"
            " ORDER BY sync_notes.note_id",
            (vault,),
        )
    )


def unconfirmed(connection: apsw.Connection, vault: str) -> list[str]:
    """Notes with a file put and not seen in the vault yet."""
    return _ids(
        connection.execute(
            "SELECT note_id FROM sync_notes WHERE vault = ? AND outbox IS NOT NULL"
            " ORDER BY note_id",
            (vault,),
        )
    )


def known(connection: apsw.Connection, vault: str) -> dict[str, NoteState]:
    """Every note of the vault sync knows of, by id."""
    rows = connection.execute(
        f"SELECT note_id, {_COLUMNS} FROM sync_notes WHERE vault = ?", (vault,)
    )
    return {str(row[0]): _state(row[1:]) for row in rows}


def cursor(connection: apsw.Connection, vault: str) -> str | None:
    rows = connection.execute("SELECT cursor FROM sync_vaults WHERE vault = ?", (vault,)).fetchall()
    return _text(rows[0][0]) if rows else None


def set_cursor(connection: apsw.Connection, vault: str, at: str | None) -> None:
    connection.execute(
        "INSERT INTO sync_vaults (vault, cursor) VALUES (?, ?)"
        " ON CONFLICT (vault) DO UPDATE SET cursor = excluded.cursor",
        (vault, at),
    )


def _vault_value(connection: apsw.Connection, vault: str, column: str) -> object:
    rows = connection.execute(f"SELECT {column} FROM sync_vaults WHERE vault = ?", (vault,))
    found = rows.fetchall()
    return found[0][0] if found else None


def _set_vault_value(
    connection: apsw.Connection, vault: str, column: str, value: int | str
) -> None:
    connection.execute(
        f"INSERT INTO sync_vaults (vault, {column}) VALUES (?, ?)"
        f" ON CONFLICT (vault) DO UPDATE SET {column} = excluded.{column}",
        (vault, value),
    )


def synced_at(connection: apsw.Connection, vault: str) -> int | None:
    """When this device last went through a round with the vault, by the
    cloud's clock; None: never."""
    value = _vault_value(connection, vault, "synced_at")
    return None if value is None else int(value)  # pyright: ignore[reportArgumentType]


def set_synced_at(connection: apsw.Connection, vault: str, at: int) -> None:
    _set_vault_value(connection, vault, "synced_at", at)


def own_mark(connection: apsw.Connection, vault: str) -> str | None:
    """The mark of the last write of this device's own file in the vault."""
    return _text(_vault_value(connection, vault, "own_mark"))


def set_own_mark(connection: apsw.Connection, vault: str, mark: str) -> None:
    _set_vault_value(connection, vault, "own_mark", mark)
