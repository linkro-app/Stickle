"""Stickle's own export, brought in again: on a new computer, after a disk failed,
or from one computer to another before notes are synced.

Read from stickle-export.json (stickle.data.export): the notes (in the trash too),
their categories and marks, the user's own colours and the notes' own
reminders. Settings, places on screen and keys stay with the computer they were
made on. An export made by a later version is read for what this one knows; no
note is left out for a field it does not.

A note keeps its id: brought in once, or there already (its own export brought
back), it is passed over, and the same note on two computers is one note once
they sync. A secret note exported encrypted comes in only with its key of the
day (opened with its password of the day, or the recovery key of the day) and
the key here, to be locked again with it; one exported in the clear comes in as
an ordinary note.
"""

import base64
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import apsw

from stickle.core.clock import Clock, utc_now
from stickle.core.colors import DEFAULT_COLOR, PALETTE, is_hex
from stickle.core.labels import BUILT_IN_MARKS, DEFAULT_ICON, LABEL_ICONS
from stickle.crypto.recovery import open_exported_pair
from stickle.crypto.secret_notes import (
    SecretNoteError,
    open_exported_with_pair,
    open_exported_with_password,
    open_text,
    seal_text,
)
from stickle.data.colors import ColorRepository, OwnColorsFullError
from stickle.data.export import EXPORT_FORMAT, JSON_NAME
from stickle.data.imports import ImportRecords
from stickle.data.labels import LabelRepository, MarkNameError
from stickle.data.notes import NoteRepository
from stickle.data.schema import latest_version
from stickle.importers.base import ImportedNote, ImportResult, SourceError, clean_text
from stickle.importers.bring_in import BroughtIn

SOURCE = "stickle"
LARGEST = 512 * 1024 * 1024  # bytes: far beyond any export, short of what would hang


@dataclass(frozen=True)
class ExportedNote:
    id: str
    body: str  # its text; "" for a secret note exported encrypted
    color: str
    opacity: float = 1.0
    always_on_top: bool = True
    locked: bool = False
    collapsed: bool = False
    hidden: bool = False
    in_trash: bool = False
    category: str | None = None  # the export's id for it
    marks: tuple[str, ...] = ()  # the export's ids for them
    secret_title: str | None = None  # a secret note's title; None for an ordinary one
    sealed: bytes | None = None  # a secret note's text, still encrypted

    @property
    def was_secret(self) -> bool:
        """A secret note exported in the clear: an ordinary note here."""
        return self.secret_title is not None and self.sealed is None


@dataclass(frozen=True)
class ExportedLabel:
    """A category or a mark of the export."""

    id: str
    name: str | None  # None: a built-in mark's own name
    icon: str
    color: str = DEFAULT_COLOR  # a category's


@dataclass
class StickleExport:
    path: Path  # the export's JSON
    exported_at: str
    newer: bool  # made by a later version than this one
    notes: list[ExportedNote] = field(default_factory=list[ExportedNote])
    categories: list[ExportedLabel] = field(default_factory=list[ExportedLabel])
    marks: list[ExportedLabel] = field(default_factory=list[ExportedLabel])
    own_colors: list[str] = field(default_factory=list[str])
    reminders: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    secret_key: object = None  # the secret notes' key file of the day, if they went out locked
    recovery_pair: object = None  # the recovery key's pair of the day, likewise
    unreadable: int = 0  # rows of notes too damaged to read

    @property
    def locked_secrets(self) -> int:
        return sum(note.sealed is not None for note in self.notes)


@dataclass
class StickleExportResult(ImportResult):
    """The preview of an export, and the export itself."""

    export: StickleExport | None = None


@dataclass
class BroughtExport(BroughtIn):
    trashed: int = 0  # into the trash, as they were
    secret: int = 0  # secret notes, locked again with the key here
    secret_left: int = 0  # secret notes left out: their key of the day was not given
    was_secret: int = 0  # secret notes exported in the clear, ordinary notes here
    colors_left: int = 0  # own colours past the most kept


def export_file(path: Path) -> Path | None:
    """The export's JSON, from its folder or itself; None if it is not there."""
    candidate = path / JSON_NAME if path.is_dir() else path
    return candidate if candidate.is_file() and candidate.suffix.lower() == ".json" else None


def _document(path: Path) -> dict[str, object] | None:
    """The export's JSON as made by Stickle, or None if it is not one."""
    try:
        if path.stat().st_size > LARGEST:
            return None
        with path.open(encoding="utf-8") as file:
            parsed: object = json.load(file)
    except OSError, UnicodeDecodeError, ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    document = cast(dict[str, object], parsed)
    if document.get("app") != "Stickle" or not isinstance(document.get("tables"), dict):
        return None
    return document


def recognise(path: Path) -> Path | None:
    """The export's JSON if path is Stickle's export (its folder or its JSON)."""
    found = export_file(path)
    return found if found is not None and _document(found) is not None else None


def _rows(tables: dict[str, object], name: str) -> list[dict[str, object]]:
    rows = tables.get(name)
    if not isinstance(rows, list):
        return []
    return [
        cast(dict[str, object], row) for row in cast(list[object], rows) if isinstance(row, dict)
    ]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _flag(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return default


def _bytes(value: object) -> bytes | None:
    if not isinstance(value, dict):
        return None
    encoded = cast(dict[str, object], value).get("base64")
    if not isinstance(encoded, str):
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError:
        return None


def _color(value: object) -> str:
    text = _text(value)
    return text if text is not None and (text in PALETTE or is_hex(text)) else DEFAULT_COLOR


def _opacity(value: object) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool) and 0.1 <= value <= 1.0:
        return float(value)
    return 1.0


def _note(row: dict[str, object], marks: dict[str, list[str]]) -> ExportedNote | None:
    note_id, body = _text(row.get("id")), _text(row.get("body"))
    if not note_id or body is None:
        return None
    sealed = _bytes(row.get("secret_body"))
    title = _text(row.get("secret_title"))
    return ExportedNote(
        id=note_id,
        body="" if sealed is not None else clean_text(body),
        color=_color(row.get("color")),
        opacity=_opacity(row.get("opacity")),
        always_on_top=_flag(row.get("always_on_top"), True),
        locked=_flag(row.get("locked"), False),
        collapsed=_flag(row.get("collapsed"), False),
        hidden=_flag(row.get("hidden"), False),
        in_trash=row.get("deleted_at") is not None,
        category=_text(row.get("label")),
        marks=tuple(marks.get(note_id, [])),
        # A title without the text sealed: exported in the clear.
        secret_title=(title or "") if sealed is not None or title is not None else None,
        sealed=sealed,
    )


def _position(row: dict[str, object]) -> int:
    value = row.get("position")
    return value if isinstance(value, int) else 0


def _labels(rows: list[dict[str, object]], category: bool) -> list[ExportedLabel]:
    found: list[ExportedLabel] = []
    live = sorted((row for row in rows if row.get("deleted_at") is None), key=_position)
    for row in live:
        label_id, name = _text(row.get("id")), _text(row.get("name"))
        if not label_id or (category and not (name and name.strip())):
            continue
        icon, color = _text(row.get("icon")), _text(row.get("color"))
        found.append(
            ExportedLabel(
                id=label_id,
                name=name if name and name.strip() else None,
                icon=icon if icon in LABEL_ICONS else DEFAULT_ICON,
                color=color if color in PALETTE else DEFAULT_COLOR,
            )
        )
    return found


def read_export(path: Path) -> StickleExport:
    """The export at path (its folder or its JSON); SourceError if it is not one."""
    found = export_file(path)
    document = _document(found) if found is not None else None
    if found is None or document is None:
        raise SourceError("not a Stickle export")
    tables = cast(dict[str, object], document["tables"])
    form, schema = document.get("format"), document.get("schema_version")
    newer = (isinstance(form, int) and form > EXPORT_FORMAT) or (
        isinstance(schema, int) and schema > latest_version()
    )
    marks: dict[str, list[str]] = {}
    for row in _rows(tables, "note_marks"):
        note_id, mark_id = _text(row.get("note_id")), _text(row.get("mark_id"))
        if note_id and mark_id:
            marks.setdefault(note_id, []).append(mark_id)
    export = StickleExport(found, _text(document.get("exported_at")) or "", newer)
    for row in _rows(tables, "notes"):
        note = _note(row, marks)
        if note is None:
            export.unreadable += 1
        else:
            export.notes.append(note)
    export.categories = _labels(_rows(tables, "categories"), category=True)
    export.marks = _labels(_rows(tables, "marks"), category=False)
    colors = [
        row
        for row in _rows(tables, "custom_colors")
        if row.get("deleted_at") is None and is_hex(_text(row.get("hex")) or "")
    ]
    colors.sort(key=_position)
    export.own_colors = [str(row["hex"]) for row in colors]
    export.reminders = [
        row
        for row in _rows(tables, "reminders")
        if row.get("kind", "note") == "note" and row.get("deleted_at") is None
    ]
    export.secret_key = document.get("secret_notes_key")
    export.recovery_pair = document.get("recovery_key_pair")
    return export


def read(path: Path) -> StickleExportResult:
    """The export as other apps' notes are previewed (see stickle.importers.sources)."""
    export = read_export(path)
    notes = [
        ImportedNote(
            source_id=note.id,
            body=note.body if note.sealed is None else f"# {note.secret_title}",
            color=note.color,
            hidden=note.hidden,
        )
        for note in export.notes
    ]
    return StickleExportResult(notes=notes, unreadable=export.unreadable, export=export)


def here_already(connection: apsw.Connection, note_id: str) -> bool:
    """The note is here (its own export brought back), or came in before."""
    rows = connection.execute(
        "SELECT 1 FROM notes WHERE id = ? UNION ALL"
        " SELECT 1 FROM import_records WHERE source = ? AND source_id = ?",
        (note_id, SOURCE, note_id),
    ).fetchall()
    return bool(rows)


def new_in_export(export: StickleExport, connection: apsw.Connection) -> int:
    """How many of the export's notes have not come in (for the preview)."""
    return sum(not here_already(connection, note.id) for note in export.notes)


# The columns of a note's own reminder, as this version keeps them.
REMINDER_COLUMNS = (
    "calendar",
    "start_day",
    "at_time",
    "time_zone",
    "repeat",
    "weekdays",
    "next_day",
    "snoozed_until",
    "hide_note",
    "acked_at",
    "acked_device",
    "created_at",
    "updated_at",
)


def _bring_reminder(connection: apsw.Connection, row: dict[str, object], note_id: str) -> None:
    """The note's own reminder as it was (a calendar this version does not know
    too: it is kept, unread, as a later version left it)."""
    if not _text(row.get("start_day")) or not _text(row.get("at_time")):
        return
    reminder_id = _text(row.get("id")) or str(uuid.uuid4())
    taken = connection.execute("SELECT 1 FROM reminders WHERE id = ?", (reminder_id,)).fetchall()
    if taken:
        reminder_id = str(uuid.uuid4())
    columns = [c for c in REMINDER_COLUMNS if c in row and row[c] is not None]
    values = [row[c] for c in columns]
    listed = ", ".join(("id", "note_id", "kind", *columns))
    marks = ", ".join("?" * (3 + len(columns)))
    connection.execute(
        f"INSERT INTO reminders ({listed}) VALUES ({marks})",
        (reminder_id, note_id, "note", *values),  # pyright: ignore[reportArgumentType]
    )


def _categories(export: StickleExport, labels: LabelRepository) -> dict[str, str]:
    """The export's category ids, as categories here: by name, made if missing."""
    here = {category.name.casefold(): category.id for category in labels.categories()}
    mapped: dict[str, str] = {}
    for category in export.categories:
        assert category.name is not None
        key = category.name.casefold()
        if key not in here:
            here[key] = labels.create_category(category.name, category.color, category.icon).id
        mapped[category.id] = here[key]
    return mapped


def _marks(export: StickleExport, labels: LabelRepository) -> dict[str, str]:
    """The export's mark ids, as marks here: a built-in one as itself, another by
    name, made if missing."""
    live = labels.marks()
    ids = {mark.id for mark in live}
    named = {mark.name.casefold(): mark.id for mark in live if mark.name}
    mapped: dict[str, str] = {}
    for mark in export.marks:
        if mark.id in BUILT_IN_MARKS and mark.id in ids:
            mapped[mark.id] = mark.id
        elif mark.name and mark.name.casefold() in named:
            mapped[mark.id] = named[mark.name.casefold()]
        elif mark.name:
            try:
                made = labels.create_mark(mark.name, mark.icon)
            except MarkNameError:  # a built-in mark's name here, as translated
                continue
            named[mark.name.casefold()] = mapped[mark.id] = made.id
    return mapped


def _own_colors(export: StickleExport, colors: ColorRepository) -> int:
    """The export's own colours added after those here; how many did not fit."""
    left = 0
    for hex_value in export.own_colors:
        try:
            colors.add(hex_value)
        except OwnColorsFullError:
            left += 1
    return left


def bring_in_export(
    export: StickleExport,
    connection: apsw.Connection,
    old_key: bytes | None = None,
    new_key: bytes | None = None,
    clock: Clock = utc_now,
    on_note: Callable[[str], None] = lambda _: None,
) -> BroughtExport:
    """Every note of the export not here yet, all at once or not at all.

    old_key: the secret notes' key of the export's day; new_key: theirs here.
    Without both, secret notes exported encrypted are left out (to come in
    another time). on_note: told of each note brought in, by its id.
    """
    notes = NoteRepository(connection, clock)
    labels = LabelRepository(connection, clock)
    records = ImportRecords(connection, clock)
    brought = BroughtExport()
    reminders = {str(row.get("note_id")): row for row in export.reminders}
    with connection:
        categories = _categories(export, labels)
        marks = _marks(export, labels)
        brought.colors_left = _own_colors(export, ColorRepository(connection, clock))
        for note in export.notes:
            if here_already(connection, note.id):
                brought.already_here += 1
                continue
            sealed: bytes | None = None
            if note.sealed is not None:
                if old_key is None or new_key is None:
                    brought.secret_left += 1
                    continue
                try:
                    sealed = seal_text(new_key, note.id, open_text(old_key, note.id, note.sealed))
                except SecretNoteError:
                    brought.secret_left += 1
                    continue
            notes.create(note.body, note.color, note_id=note.id)
            if note.opacity != 1.0:
                notes.set_opacity(note.id, note.opacity)
            if not note.always_on_top:
                notes.set_always_on_top(note.id, False)
            if note.locked:
                notes.set_locked(note.id, True)
            if note.collapsed:
                notes.set_collapsed(note.id, True)
            if note.hidden:
                notes.set_hidden(note.id, True)
            if note.category in categories:
                notes.set_category(note.id, categories[note.category])
            for mark_id in note.marks:
                if mark_id in marks:
                    notes.set_mark(note.id, marks[mark_id], True)
            if note.id in reminders:
                _bring_reminder(connection, reminders[note.id], note.id)
            records.record(SOURCE, note.id, note.body, note.id)
            if sealed is not None:
                # Its record keeps no hash of its text (see stickle.data.imports).
                notes.make_secret(note.id, note.secret_title or "", sealed)
                brought.secret += 1
            elif note.was_secret:
                brought.was_secret += 1
            if note.in_trash:
                notes.delete(note.id)
                brought.trashed += 1
            else:
                brought.note_ids.append(note.id)
            on_note(note.id)
    return brought


def secrets_waiting(export: StickleExport, connection: apsw.Connection) -> int:
    """How many of its secret notes, exported encrypted, have not come in yet:
    they wait for their key of the day."""
    return sum(
        note.sealed is not None and not here_already(connection, note.id) for note in export.notes
    )


def key_with_password(export: StickleExport, password: str) -> bytes:
    """Its secret notes' key of the day, by their password then (WrongPasswordError,
    or KeyFileError if the export holds no copy of the key)."""
    return open_exported_with_password(export.secret_key, password)


def key_with_recovery_key(export: StickleExport, typed: str) -> bytes:
    """Its secret notes' key of the day, by the recovery key then
    (RecoveryKeyTypoError, WrongRecoveryKeyError, NotKeptForRecoveryKeyError)."""
    secret, public = open_exported_pair(export.recovery_pair, typed)
    return open_exported_with_pair(export.secret_key, secret, public)
