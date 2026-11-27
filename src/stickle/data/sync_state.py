"""What sync knows of each note in a vault, kept beside the notes.

For each note: its file as last seen in the vault and that file's tag, the
file put there and not seen there yet (the outbox), the note's change number
when it was last merged, and whether its text is at a version that deletes it.
The files are JSON, as stickle.sync writes them; to this module they are text.

A note emptied from the trash, or made secret, has its text taken out of both
files here (a secret note's sealed one too): its text is never kept in the
clear after that. What is left of them, the versions and fields, is what the
next version is made on. A secret note's text is kept as it is sealed.

Each vault is kept apart: a device that synced with another vault before
keeps what it knew of that one, to merge on if it goes back to it, but for
what rounds keep for a while (let_go_of_others). The text it last saw there
stays: it is how a note is told unchanged here since (stickle.sync.local).
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
    # A deletion came for it while it was being edited here, and waits: its file
    # gone later is that deletion's year in the vault over, not the vault gone back.
    held_gone: bool = False


_COLUMNS = "seen, tag, outbox, merged_seq, deleted, gone, held_gone"


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _state(row: apsw.SQLiteValues) -> NoteState:
    seen, tag, outbox, seq, deleted, gone, held_gone = row
    return NoteState(
        _text(seen),
        _text(tag),
        _text(outbox),
        int(seq),  # pyright: ignore[reportArgumentType]
        bool(deleted),
        bool(gone),
        bool(held_gone),
    )


def get(connection: apsw.Connection, vault: str, note_id: str) -> NoteState | None:
    rows = connection.execute(
        f"SELECT {_COLUMNS} FROM sync_notes WHERE vault = ? AND note_id = ?", (vault, note_id)
    ).fetchall()
    return _state(rows[0]) if rows else None


def put(connection: apsw.Connection, vault: str, note_id: str, state: NoteState) -> None:
    connection.execute(
        "INSERT INTO sync_notes (vault, note_id, seen, tag, outbox, merged_seq, deleted, gone,"
        " held_gone) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (vault, note_id) DO UPDATE"
        " SET seen = excluded.seen, tag = excluded.tag, outbox = excluded.outbox,"
        " merged_seq = excluded.merged_seq, deleted = excluded.deleted, gone = excluded.gone,"
        " held_gone = excluded.held_gone",
        (
            vault,
            note_id,
            state.seen,
            state.tag,
            state.outbox,
            state.merged_seq,
            int(state.deleted),
            int(state.gone),
            int(state.held_gone),
        ),
    )


def drop(connection: apsw.Connection, vault: str, note_id: str) -> None:
    connection.execute("DELETE FROM sync_notes WHERE vault = ? AND note_id = ?", (vault, note_id))


def forget_text(connection: apsw.Connection, note_id: str) -> None:
    """The note's text out of what every vault's files here hold of it."""
    forget_text_of(connection, "?", (note_id,))


def forget_text_of(
    connection: apsw.Connection, notes: str, values: tuple[object, ...] = ()
) -> None:
    """forget_text for each note the query notes (with values) gives the ids of."""
    connection.execute(
        "UPDATE sync_notes SET seen = json_remove(seen, '$.body.text', '$.body.secret'),"
        " outbox = json_remove(outbox, '$.body.text', '$.body.secret')"
        f" WHERE note_id IN ({notes})",
        values,  # pyright: ignore[reportArgumentType]
    )


# What a round keeps of a vault for a while, made again by the next one.
_FOR_A_WHILE = ("sync_staged", "sync_held", "sync_untold", "sync_left")


def let_go_of_others(connection: apsw.Connection, vault: str | None) -> None:
    """Of every vault but vault (None: of every vault, none synced with): what
    rounds keep for a while, made again by the next one there (files fetched,
    what a round stopped for, files put and not yet told of, files left). What
    each note was there stays, to merge on if this device goes back to it."""
    for table in _FOR_A_WHILE:
        connection.execute(f"DELETE FROM {table} WHERE vault IS NOT ?", (vault,))


def _ids(rows: Iterable[apsw.SQLiteValues]) -> list[str]:
    return [str(row[0]) for row in rows]


def changed(connection: apsw.Connection, vault: str) -> list[str]:
    """Notes that changed here since they were last merged, or never were: by
    id. A secret note sealed with a key the vault's is not goes nowhere yet, and
    stays among them."""
    return _ids(
        connection.execute(
            "SELECT notes.id FROM notes LEFT JOIN sync_notes"
            " ON sync_notes.vault = ? AND sync_notes.note_id = notes.id"
            " WHERE sync_notes.note_id IS NULL OR notes.change_seq > sync_notes.merged_seq"
            " ORDER BY notes.id",
            (vault,),
        )
    )


def sealed_here(connection: apsw.Connection, old: Iterable[str] = ()) -> list[str]:
    """Secret notes sealed with this computer's key (in the trash too), or with
    one of old (the numbers of its keys of before): those go to the vault only
    while its key is this computer's, these once sealed again (stickle.sync.engine)."""
    numbers = sorted(old)
    within = ", ".join("?" * len(numbers))
    return _ids(
        connection.execute(
            "SELECT id FROM notes WHERE secret_body IS NOT NULL AND (secret_key_number IS NULL"
            f" OR secret_key_number IN ({within})) ORDER BY id",
            numbers,
        )
    )


def sealed_with(connection: apsw.Connection, number: str) -> bool:
    """Whether a note here (in the trash too) is sealed with the key numbered number."""
    rows = connection.execute(
        "SELECT 1 FROM notes WHERE secret_body IS NOT NULL AND secret_key_number = ? LIMIT 1",
        (number,),
    ).fetchall()
    return bool(rows)


def sealed_here_in_vault(connection: apsw.Connection, vault: str) -> bool:
    """Whether a secret note sealed with this computer's key is in the vault
    (went there while its key was the vault's, as this computer saw it)."""
    rows = connection.execute(
        "SELECT 1 FROM notes JOIN sync_notes ON sync_notes.vault = ?"
        " AND sync_notes.note_id = notes.id"
        " WHERE notes.secret_body IS NOT NULL AND notes.secret_key_number IS NULL LIMIT 1",
        (vault,),
    ).fetchall()
    return bool(rows)


def changed_at(connection: apsw.Connection, vault: str) -> list[tuple[str, str]]:
    """Notes that changed here since they were last merged, or never were: by
    id, with when they last changed."""
    rows = connection.execute(
        "SELECT notes.id, notes.updated_at FROM notes LEFT JOIN sync_notes"
        " ON sync_notes.vault = ? AND sync_notes.note_id = notes.id"
        " WHERE sync_notes.note_id IS NULL OR notes.change_seq > sync_notes.merged_seq",
        (vault,),
    )
    return [(str(note_id), str(at)) for note_id, at in rows]


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


def joining(connection: apsw.Connection, vault: str) -> bool:
    """Whether this device joined the vault, which was there before it, and has
    not been through a round with it since."""
    return bool(_vault_value(connection, vault, "joining"))


def set_joining(connection: apsw.Connection, vault: str, joining: bool) -> None:
    _set_vault_value(connection, vault, "joining", int(joining))


def secret_key(connection: apsw.Connection, vault: str) -> str | None:
    """The number of the vault's secret notes' key, as a round last saw it
    (stickle.sync.secret_key); None: not seen yet."""
    return _text(_vault_value(connection, vault, "secret_key"))


def set_secret_key(connection: apsw.Connection, vault: str, number: str) -> None:
    _set_vault_value(connection, vault, "secret_key", number)


def synced_at(connection: apsw.Connection, vault: str) -> int | None:
    """When this device last went through a round with the vault, by the
    cloud's clock; None: never."""
    value = _vault_value(connection, vault, "synced_at")
    return None if value is None else int(value)  # pyright: ignore[reportArgumentType]


def set_synced_at(connection: apsw.Connection, vault: str, at: int) -> None:
    _set_vault_value(connection, vault, "synced_at", at)


# The marks of this device's own file kept: the latest, and the last of each
# day for longer. It is written once a day, and after each round that puts
# something (dozens a day while the user writes): a vault gone back to some
# day of the last DAYS_KEPT shows the last mark of that day, this device's.
MARKS_KEPT = 400
DAYS_KEPT = 400


def untold(connection: apsw.Connection, vault: str) -> list[str]:
    """Files put, or about to be, that this device has not said so yet."""
    return _ids(
        connection.execute("SELECT path FROM sync_untold WHERE vault = ? ORDER BY path", (vault,))
    )


def add_untold(connection: apsw.Connection, vault: str, path: str) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO sync_untold (vault, path) VALUES (?, ?)", (vault, path)
    )


def told(connection: apsw.Connection, vault: str, paths: Iterable[str]) -> None:
    """These have been said."""
    for path in paths:
        connection.execute("DELETE FROM sync_untold WHERE vault = ? AND path = ?", (vault, path))


def _dated_marks(connection: apsw.Connection, vault: str) -> list[tuple[int | None, str]]:
    """(day, mark) of each kept, the latest first; day: of the cloud's time,
    in days since 1970 (None: kept before marks were kept with theirs)."""
    text = _text(_vault_value(connection, vault, "own_mark"))
    marks: list[tuple[int | None, str]] = []
    for word in [] if text is None else text.split():
        day, _, mark = word.rpartition(":")
        marks.append((int(day) if day.isdigit() else None, mark))
    return marks


def own_marks(connection: apsw.Connection, vault: str) -> list[str]:
    """The marks of this device's writes of its own file in the vault, the
    latest first: the last MARKS_KEPT, and the last of each of DAYS_KEPT days."""
    return [mark for _day, mark in _dated_marks(connection, vault)]


def add_own_mark(connection: apsw.Connection, vault: str, mark: str, day: int) -> None:
    """mark, written on day (of the cloud's time, in days since 1970)."""
    kept: list[tuple[int | None, str]] = []
    days: set[int] = set()
    for at, (when, each) in enumerate([(day, mark), *_dated_marks(connection, vault)]):
        last_of_its_day = when is not None and when not in days and day - when < DAYS_KEPT
        if when is not None:
            days.add(when)
        if at < MARKS_KEPT or last_of_its_day:
            kept.append((when, each))
    words = [each if when is None else f"{when}:{each}" for when, each in kept]
    _set_vault_value(connection, vault, "own_mark", " ".join(words))
