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
"""

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import apsw

from stickle.core.clock import Clock, utc_now
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


def file_stem(body: str, note_id: str) -> str:
    title = " ".join(_UNSAFE.sub(" ", first_line(body)).split())[:NAME_LENGTH]
    title = title.rstrip(". ") or "note"
    # Part of the id keeps names unique and never a reserved name such as CON.
    suffix = _UNSAFE.sub("", note_id)[:8]
    return f"{title} ({suffix})" if suffix else title


def _rows(connection: apsw.Connection) -> list[tuple[str, str, bool]]:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(notes)")}
    if not {"id", "body"} <= columns:
        raise ExportError("no notes table")
    deleted = "deleted_at IS NOT NULL" if "deleted_at" in columns else "0"
    query = f"SELECT id, body, {deleted} FROM notes"
    return [(str(i), str(b), bool(d)) for i, b, d in connection.execute(query)]


def _free(path: Path) -> Path:
    candidate, number = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} {number}{path.suffix}")
        number += 1
    return candidate


def export_markdown(
    database: Path, key: bytes, target: Path, clock: Clock = utc_now
) -> ExportResult:
    """Write every note into a new folder inside target; nothing is overwritten."""
    connection = open_database(database, key)
    try:
        rows = _rows(connection)
    finally:
        connection.close()
    stamp = clock()[:19].replace(":", "").replace("-", "")
    folder = _free(target / f"Stickle notes {stamp}")
    folder.mkdir(parents=True)
    deleted_count = 0
    for note_id, body, deleted in rows:
        place = folder / "deleted" if deleted else folder
        place.mkdir(exist_ok=True)
        path = _free(place / f"{file_stem(body, note_id)}.md")
        # newline="" keeps the text exactly as stored.
        with path.open("x", encoding="utf-8", newline="") as file:
            file.write(body)
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


def _read_all(
    connection: apsw.Connection,
) -> tuple[int, dict[str, list[dict[str, object]]], list[tuple[str, str, bool]]]:
    """One consistent reading, even if a note is saved meanwhile."""
    connection.execute("BEGIN")
    try:
        return int(str(connection.pragma("user_version"))), _tables(connection), _rows(connection)
    finally:
        connection.execute("COMMIT")


def export_all(
    connection: apsw.Connection,
    target: Path,
    app_version: str,
    clock: Clock = utc_now,
) -> ExportResult:
    """Write everything into a new folder inside target; nothing is overwritten."""
    version, tables, rows = _read_all(connection)
    moment = datetime.fromisoformat(clock().replace("Z", "+00:00")).astimezone()
    folder = _free(target / f"Stickle export {moment:%Y-%m-%d %H%M}")
    folder.mkdir(parents=True)
    deleted_count = 0
    for note_id, body, deleted in rows:
        place = folder / TRASH_FOLDER if deleted else folder
        place.mkdir(exist_ok=True)
        path = _free(place / f"{file_stem(body, note_id)}.md")
        with path.open("x", encoding="utf-8", newline="") as file:
            file.write(body)
        deleted_count += deleted
    document = {
        "format": EXPORT_FORMAT,
        "app": "Stickle",
        "app_version": app_version,
        "schema_version": version,
        "exported_at": clock(),
        "tables": tables,
    }
    with (folder / JSON_NAME).open("x", encoding="utf-8", newline="\n") as file:
        json.dump(document, file, ensure_ascii=False, indent=1)
        file.write("\n")
    return ExportResult(folder, len(rows) - deleted_count, deleted_count)
