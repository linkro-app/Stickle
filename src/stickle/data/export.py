"""Copy notes out of the database, as Markdown files and as JSON.

export_markdown is the way out when the app cannot use a database (an update
failed, or it was saved by a newer version): it reads only the note id and
text, which every version has, and never changes the database. Each note
becomes one file named after its first line; deleted notes go into a
"deleted" folder.

export_all is the export the user asks for from the app: the same Markdown
files (the trash's in "trash"), and stickle-export.json with every row of
every table the user's data is kept in, so that nothing stays behind, data
added by later versions included. Only what is derived from other data (the
search index) is left out. Nothing is encrypted: the user is told so.

Secret notes go out as the user chooses (SecretExport): encrypted as they are
stored, their Markdown file holding only their title and a line saying so,
with the locked copies of their key beside them in the JSON (opened again with
their password or the master password); or opened with their key, their text
in place of the encrypted one. Their files are named after their titles.
"""

import base64
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

import apsw

from stickle.core.clock import Clock, utc_now
from stickle.crypto.secret_notes import open_text
from stickle.data.database import open_database
from stickle.data.schema import first_line

# Not allowed in file names on Windows (the strictest of the three systems).
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
NAME_LENGTH = 50


class ExportError(Exception):
    """The database has no notes this app can read."""


@dataclass(frozen=True)
class ExportResult:
    folder: Path
    notes: int
    deleted: int
    still_encrypted: int = 0  # secret notes left as they are stored


@dataclass(frozen=True)
class SecretExport:
    """How secret notes go out."""

    key: bytes | None = None  # theirs, to open them; None: left encrypted
    locked_line: str = "Locked secret note"  # a locked one's Markdown, below its title
    key_copies: dict[str, object] | None = None  # their key's locked copies, if left so


@dataclass(frozen=True)
class _Secret:
    title: str
    sealed: bytes


def file_stem(body: str, note_id: str) -> str:
    # Characters not in Unicode yet are left out too: macOS refuses them in names.
    line = "".join(c for c in first_line(body) if unicodedata.category(c) != "Cn")
    title = " ".join(_UNSAFE.sub(" ", line).split())[:NAME_LENGTH]
    title = title.rstrip(". ") or "note"
    # Part of the id keeps names unique and never a reserved name such as CON.
    suffix = _UNSAFE.sub("", note_id)[:8]
    return f"{title} ({suffix})" if suffix else title


def _locked_markdown(title: str, locked_line: str) -> str:
    """A secret note left encrypted: the title typed for it (if any) and a line
    saying it is encrypted."""
    return f"# {title}\n\n{locked_line}\n" if title else f"{locked_line}\n"


def _rows(connection: apsw.Connection) -> list[tuple[str, str, bool, str | None]]:
    """(id, body, deleted, the title of a secret one, None if it is not)."""
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(notes)")}
    if not {"id", "body"} <= columns:
        raise ExportError("no notes table")
    deleted = "deleted_at IS NOT NULL" if "deleted_at" in columns else "0"
    secret = (
        "CASE WHEN secret_body IS NULL THEN NULL ELSE coalesce(secret_title, '') END"
        if "secret_body" in columns
        else "NULL"
    )
    query = f"SELECT id, body, {deleted}, {secret} FROM notes"
    return [
        (str(i), str(b), bool(d), None if t is None else str(t))
        for i, b, d, t in connection.execute(query)
    ]


def _free(path: Path) -> Path:
    candidate, number = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} {number}{path.suffix}")
        number += 1
    return candidate


def _new_file(place: Path, stem: str, note_id: str) -> TextIO:
    """A new file in place named stem, or a plain name with the note's id if the
    file system refuses that one (each refuses some characters the others take).
    newline="" keeps the text exactly as stored."""
    try:
        return _free(place / f"{stem}.md").open("x", encoding="utf-8", newline="")
    except OSError:
        plain = _free(place / f"{file_stem('', note_id)}.md")
        return plain.open("x", encoding="utf-8", newline="")


def export_markdown(
    database: Path,
    key: bytes,
    target: Path,
    clock: Clock = utc_now,
    locked_line: str = "Encrypted secret note",
) -> ExportResult:
    """Write every note into a new folder inside target; nothing is overwritten.
    A secret note, still encrypted (its key is not at hand here), is written as
    its title and locked_line, as the full export does."""
    connection = open_database(database, key)
    try:
        rows = _rows(connection)
    finally:
        connection.close()
    stamp = clock()[:19].replace(":", "").replace("-", "")
    folder = _free(target / f"Stickle notes {stamp}")
    folder.mkdir(parents=True)
    deleted_count = 0
    for note_id, body, deleted, secret_title in rows:
        place = folder / "deleted" if deleted else folder
        place.mkdir(exist_ok=True)
        if secret_title is not None:
            text = _locked_markdown(secret_title, locked_line)
            stem = file_stem(secret_title, note_id)
        else:
            text, stem = body, file_stem(body, note_id)
        with _new_file(place, stem, note_id) as file:
            file.write(text)
        deleted_count += deleted
    return ExportResult(folder, len(rows) - deleted_count, deleted_count)


EXPORT_FORMAT = 1
JSON_NAME = "stickle-export.json"
TRASH_FOLDER = "trash"


def _derived(table: str) -> bool:
    """Tables rebuilt from others (the search index and its parts) or SQLite's own."""
    return table.startswith(("notes_fts", "sqlite_"))


def _value(value: object) -> object:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    return value


def _tables(connection: apsw.Connection) -> dict[str, list[dict[str, object]]]:
    names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name"
        )
    ]
    tables: dict[str, list[dict[str, object]]] = {}
    for name in names:
        if _derived(name):
            continue
        # The names come from the schema itself, not from outside.
        columns = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{name}")')]
        listed = ", ".join(f'"{column}"' for column in columns)
        tables[name] = [
            dict(zip(columns, (_value(value) for value in row), strict=True))
            for row in connection.execute(f'SELECT {listed} FROM "{name}"')
        ]
    return tables


def _secrets(connection: apsw.Connection) -> dict[str, _Secret]:
    """The secret notes (none before the database had them), by id."""
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(notes)")}
    if "secret_body" not in columns:
        return {}
    rows = connection.execute(
        "SELECT id, secret_title, secret_body FROM notes WHERE secret_body IS NOT NULL"
    )
    return {str(i): _Secret(str(t or ""), bytes(b)) for i, t, b in rows}


def _read_all(
    connection: apsw.Connection,
) -> tuple[
    int,
    dict[str, list[dict[str, object]]],
    list[tuple[str, str, bool, str | None]],
    dict[str, _Secret],
]:
    """One consistent reading, even if a note is saved meanwhile."""
    connection.execute("BEGIN")
    try:
        return (
            int(str(connection.pragma("user_version"))),
            _tables(connection),
            _rows(connection),
            _secrets(connection),
        )
    finally:
        connection.execute("COMMIT")


def _opened(
    tables: dict[str, list[dict[str, object]]], texts: dict[str, str]
) -> dict[str, list[dict[str, object]]]:
    """The tables with each secret note's text in place of its encrypted one."""
    notes = [
        {**row, "body": texts[str(row["id"])], "secret_body": None}
        if str(row.get("id")) in texts
        else row
        for row in tables.get("notes", [])
    ]
    return {**tables, "notes": notes}


def export_all(
    connection: apsw.Connection,
    target: Path,
    app_version: str,
    clock: Clock = utc_now,
    secret: SecretExport | None = None,
) -> ExportResult:
    """Write everything into a new folder inside target; nothing is overwritten.

    With secret notes and their key, every one is opened first: SecretNoteError
    (from stickle.crypto.secret_notes) before anything is written if one does
    not open with it.
    """
    secret = secret or SecretExport()
    version, tables, rows, secrets = _read_all(connection)
    texts: dict[str, str] = {}
    if secret.key is not None:
        texts = {i: open_text(secret.key, i, s.sealed) for i, s in secrets.items()}
        tables = _opened(tables, texts)
    moment = datetime.fromisoformat(clock().replace("Z", "+00:00")).astimezone()
    folder = _free(target / f"Stickle export {moment:%Y-%m-%d %H%M}")
    folder.mkdir(parents=True)
    deleted_count = 0
    for note_id, body, deleted, _secret_title in rows:
        place = folder / TRASH_FOLDER if deleted else folder
        place.mkdir(exist_ok=True)
        locked = secrets.get(note_id)
        if locked is not None:
            # Named after the title typed for it: its text, if opened, may say more.
            text = texts.get(note_id, _locked_markdown(locked.title, secret.locked_line))
            stem = file_stem(locked.title, note_id)
        else:
            text, stem = body, file_stem(body, note_id)
        with _new_file(place, stem, note_id) as file:
            file.write(text)
        deleted_count += deleted
    document: dict[str, object] = {
        "format": EXPORT_FORMAT,
        "app": "Stickle",
        "app_version": app_version,
        "schema_version": version,
        "exported_at": clock(),
        "tables": tables,
    }
    if secrets and secret.key is None and secret.key_copies is not None:
        document["secret_notes_key"] = secret.key_copies
    with (folder / JSON_NAME).open("x", encoding="utf-8", newline="\n") as file:
        json.dump(document, file, ensure_ascii=False, indent=1)
        file.write("\n")
    still = 0 if secret.key is not None else len(secrets)
    return ExportResult(folder, len(rows) - deleted_count, deleted_count, still)
