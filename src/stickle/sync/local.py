"""A note here as a note file (stickle.sync.note_file), and a note file taken in.

The note's own fields are its columns, by the names stickle.data.stamps gives
them (its category is "category", each mark "mark:<id>"), with their values in
JSON: the colour's key, the opacity, the category's id or null, true or false,
and when it went into the trash (a timestamp, stickle.core.clock) or null. A
field never changed here has no stamp: it goes as changed at the earliest time
of all, by this device. Fields this version does not keep here (a newer
version's, a conflict copy's mark) go as they were last seen in the vault.

A value taken in is checked first. One this version cannot take (a category
or a mark this device does not know yet, a value of another shape) is left in
the file as it is and not taken here: the note keeps what it has, and the
later change stays the file's, taken once the category or mark comes. One
deleted is taken: the note names it on every device alike.

The text is at a version: the one this device put in the vault and has not
seen there yet, else the one last seen there, if it has not changed here
since. Changed, it is a new version made on that one, edited when it was last
saved here (by the time the notes are stamped with: the cloud's, while
syncing). Emptied here, it is a version that deletes the note, made on that
one; emptied here before the vault was seen to have the note, one made on none.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC

import apsw

from stickle.core.clock import moment_of
from stickle.core.colors import is_color
from stickle.core.hlc import Hlc
from stickle.core.note import Note
from stickle.crypto.vault_keys import DataKeys
from stickle.data import stamps
from stickle.data.notes import NoteRepository
from stickle.data.stamps import Stamp
from stickle.data.sync_state import NoteState
from stickle.sync.contents import Contents, Field
from stickle.sync.format import Json, json_bytes, parse_json
from stickle.sync.merge import COPY_OF
from stickle.sync.note_file import BODY, NoteFile, new_version

# The fields kept here, and their columns. A secret note's title goes with its
# text, which this version does not sync yet.
COLUMNS = {field: column for column, field in stamps.FIELDS.items() if field != "secret_title"}
MARK = stamps.mark_field("")
EARLIEST = Hlc(0)
_SWITCHES = {"always_on_top", "hidden", "collapsed", "locked"}
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_COLOR_KEY = re.compile(r"[a-z][a-z0-9_-]{0,31}")  # one a newer version may add
_NOT_TAKEN = object()


def millis_of(timestamp: str) -> int:
    return int(moment_of(timestamp).astimezone(UTC).timestamp() * 1000)


def known(contents: Contents) -> set[str]:
    """The note's fields that this version knows."""
    return {*COLUMNS, COPY_OF, *(name for name in contents.fields if name.startswith(MARK))}


def _ours(name: str) -> bool:
    return name in COLUMNS or name.startswith(MARK)


def kept(text: str | None) -> NoteFile | None:
    """A note file as sync_state keeps it."""
    return None if text is None else NoteFile.kept(parse_json(text.encode("utf-8")))


def as_kept(note: NoteFile, text: bool = True) -> str:
    """note as sync_state keeps it; without its text (text False) for a note
    emptied here."""
    written = note.to_json()
    if not text:
        body = written[BODY]
        assert isinstance(body, dict)
        written[BODY] = {name: each for name, each in body.items() if name != "text"}
    return json_bytes(written).decode("utf-8")


@dataclass(frozen=True)
class Here:
    """The note as it is on this device."""

    note: Note | None  # None: not here
    emptied_at: str | None  # when it was emptied here, if it was (its deletion record)
    stamps: Mapping[str, Stamp]
    sent: Mapping[str, int]
    text_at: str | None = None  # when its text was last saved

    @property
    def mark(self) -> tuple[int | None, str | None]:
        """What tells whether it changed since it was looked at."""
        return (None if self.note is None else self.note.change_seq, self.emptied_at)


def here(connection: apsw.Connection, notes: NoteRepository, note_id: str) -> Here:
    note = notes.get(note_id)
    emptied = text_at = None
    if note is None:
        rows = connection.execute(
            "SELECT purged_at FROM deletion_records WHERE note_id = ?", (note_id,)
        ).fetchall()
        emptied = str(rows[0][0]) if rows else None
    else:
        rows = connection.execute(
            "SELECT coalesce(text_at, updated_at) FROM notes WHERE id = ?", (note_id,)
        ).fetchall()
        text_at = str(rows[0][0])
    return Here(
        note,
        emptied,
        stamps.of(connection, note_id),
        stamps.sent_of(connection, note_id),
        text_at,
    )


def _value(note: Note, name: str) -> Json:
    if name.startswith(MARK):
        return name.removeprefix(MARK) in note.marks
    if name == "opacity":
        return note.opacity
    if name in _SWITCHES:
        return bool(getattr(note, name))
    if name == "category":
        return note.label
    if name == "deleted_at":
        return note.deleted_at
    assert name == "color", name
    return note.color


def _field(at: Here, note: Note, name: str, device: str, before: Field | None) -> Field:
    when, by = at.stamps.get(name, (EARLIEST, None))
    value, sent = _value(note, name), at.sent.get(name)
    if before is None:
        return Field(value, when, by or device, sent=sent)
    return replace(before, value=value, hlc=when, device=by or device, sent=sent)


def _head(state: NoteState | None) -> NoteFile | None:
    """What the note's next version is made on: the file put and not seen yet,
    else the one last seen."""
    if state is None:
        return None
    return kept(state.outbox if state.outbox is not None else state.seen)


def mine(keys: DataKeys, device: str, at: Here, state: NoteState | None) -> NoteFile | None:
    """The note as this device has it, as a file; None when it has nothing of it."""
    head = _head(state)
    note = at.note
    if note is None:
        if at.emptied_at is None:
            return None
        if head is not None and head.body.deleted:
            return head
        parents = [] if head is None else [head.body]
        gone = new_version(keys, parents, None, millis_of(at.emptied_at), device)
        return NoteFile(Contents() if head is None else head.contents, gone)
    before = Contents() if head is None else head.contents
    names = {name for name in before.fields if name.startswith(MARK)}
    names |= {name for name in at.stamps if name.startswith(MARK)}
    names |= {stamps.mark_field(mark) for mark in note.marks}
    names |= set(COLUMNS)
    fields = {name: each for name, each in before.fields.items() if not _ours(name)}
    for name in sorted(names):
        fields[name] = _field(at, note, name, device, before.fields.get(name))
    contents = Contents(fields, before.others)
    if head is not None and not head.body.deleted and head.body.text == note.body:
        return NoteFile(contents, head.body)
    edited = millis_of(at.text_at or note.updated_at)
    parents = [] if head is None else [head.body]
    return NoteFile(contents, new_version(keys, parents, note.body, edited, device))


def _exists(connection: apsw.Connection, table: str, row_id: str) -> bool:
    """Whether the category or mark is known here, deleted or not: a note may
    name one deleted on another device and shows it, there and here alike."""
    return bool(connection.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)).fetchall())


def _column_value(connection: apsw.Connection, name: str, value: Json) -> object:
    """value as the column takes it, or _NOT_TAKEN."""
    if name == "color":
        ok = isinstance(value, str) and (is_color(value) or _COLOR_KEY.fullmatch(value))
        return value if ok else _NOT_TAKEN
    if name == "opacity":
        if isinstance(value, int | float) and not isinstance(value, bool) and 0.1 <= value <= 1:
            return float(value)
        return _NOT_TAKEN
    if name in _SWITCHES:
        return int(value) if isinstance(value, bool) else _NOT_TAKEN
    if name == "category":
        if value is None or (isinstance(value, str) and _exists(connection, "categories", value)):
            return value
        return _NOT_TAKEN
    if name == "deleted_at":
        ok = value is None or (isinstance(value, str) and _TIMESTAMP.fullmatch(value))
        return value if ok else _NOT_TAKEN
    return _NOT_TAKEN


def take(
    connection: apsw.Connection,
    notes: NoteRepository,
    device: str,
    note_id: str,
    note: NoteFile,
    at: Here,
) -> int | None:
    """The note as the file has it, here: what is not here already, of what this
    version can take. Its change number after (None: not here)."""
    if note.body.deleted:
        if at.note is not None:
            notes.take_in_emptied(note_id)
        return None
    text = note.body.text if at.note is None or note.body.text != at.note.body else None
    columns: dict[str, object] = {}
    marks: dict[str, bool] = {}
    stamped: dict[str, tuple[Stamp, int | None]] = {}
    for name, each in sorted(note.contents.fields.items()):
        if not _ours(name):
            continue
        stamp: Stamp = (each.hlc, None if each.device == device else each.device)
        now = at.note is not None and (
            _value(at.note, name) == each.value
            and at.stamps.get(name, (EARLIEST, None)) == stamp
            and at.sent.get(name) == each.sent
        )
        if now:
            continue
        if name.startswith(MARK):
            mark = name.removeprefix(MARK)
            if not isinstance(each.value, bool) or (
                each.value and not _exists(connection, "marks", mark)
            ):
                continue
            marks[mark] = each.value
        else:
            value = _column_value(connection, name, each.value)
            if value is _NOT_TAKEN:
                continue
            columns[COLUMNS[name]] = value
        stamped[name] = (stamp, each.sent)
    if at.note is not None and text is None and not columns and not marks and not stamped:
        return at.note.change_seq
    came_from = None if note.body.device == device else note.body.device
    return notes.take_in(note_id, text, came_from, columns, marks, stamped)
