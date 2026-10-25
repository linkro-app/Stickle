"""Reading and changing notes in the local database.

Deleting only marks a note, which then sits in the trash (kept 365 days).
A note is removed only when emptied from the trash, or at once when it has no
text, and then a deletion record takes its place. Every change stamps the note with this device's
next change number, which never goes backwards even if the clock does.
"""

import uuid

import apsw

from stickle.core.clock import Clock, days_before, utc_now
from stickle.core.note import DEFAULT_COLOR, Note, content_hash
from stickle.data.search import search

COLUMNS = (
    "id, body, color, hidden, always_on_top, created_at, updated_at, content_hash, deleted_at,"
    " change_seq, opacity, status, label, locked, collapsed, auto_height, zoom, sleep_until,"
    " style_id, secret_title, secret_body"
)


class NoteNotFoundError(KeyError):
    pass


class NoteDeletedError(ValueError):
    """Deleted notes can only be restored."""


class NoteNotDeletedError(ValueError):
    """Only a note in the trash can be emptied from it."""


class NoteNotBlankError(ValueError):
    """Only a note with no text can be removed past the trash."""


class NoteIsSecretError(ValueError):
    """A secret note's text is kept encrypted only, never as body."""


class NoteNotSecretError(ValueError):
    """Only a secret note has a sealed text and a title of its own."""


# Notes stay in the trash this long, and deletion records this long after emptying.
KEEP_DAYS = 365


def _note(row: apsw.SQLiteValues, marks: frozenset[str] = frozenset()) -> Note:
    (
        note_id,
        body,
        color,
        hidden,
        on_top,
        created,
        updated,
        digest,
        deleted,
        seq,
        opacity,
        status,
        label,
        locked,
        collapsed,
        auto_height,
        zoom,
        sleep_until,
        style_id,
        secret_title,
        secret_body,
    ) = row
    return Note(
        id=str(note_id),
        body=str(body),
        color=str(color),
        hidden=bool(hidden),
        always_on_top=bool(on_top),
        created_at=str(created),
        updated_at=str(updated),
        content_hash=str(digest),
        deleted_at=None if deleted is None else str(deleted),
        change_seq=int(seq),  # pyright: ignore[reportArgumentType]
        opacity=float(opacity),  # pyright: ignore[reportArgumentType]
        status=str(status),
        label=None if label is None else str(label),
        marks=marks,
        locked=bool(locked),
        collapsed=bool(collapsed),
        auto_height=bool(auto_height),
        zoom=float(zoom),  # pyright: ignore[reportArgumentType]
        sleep_until=None if sleep_until is None else str(sleep_until),
        style_id=None if style_id is None else str(style_id),
        secret_title=None if secret_title is None else str(secret_title),
        sealed=secret_body if isinstance(secret_body, bytes) else None,
    )


class NoteRepository:
    def __init__(self, connection: apsw.Connection, clock: Clock = utc_now) -> None:
        self._db = connection
        self._clock = clock

    def _next_seq(self) -> int:
        row = self._db.execute(
            "UPDATE local_counters SET value = value + 1 WHERE key = 'change_seq' RETURNING value"
        ).fetchall()
        return int(row[0][0])  # pyright: ignore[reportArgumentType]

    def _marks(self, note_id: str | None = None) -> dict[str, frozenset[str]]:
        """The marks of one note, or of every note: note id -> mark ids."""
        if note_id is None:
            rows = self._db.execute("SELECT note_id, mark_id FROM note_marks")
        else:
            rows = self._db.execute(
                "SELECT note_id, mark_id FROM note_marks WHERE note_id = ?", (note_id,)
            )
        found: dict[str, set[str]] = {}
        for owner, mark in rows:
            found.setdefault(str(owner), set()).add(str(mark))
        return {owner: frozenset(marks) for owner, marks in found.items()}

    def get(self, note_id: str) -> Note | None:
        rows = self._db.execute(f"SELECT {COLUMNS} FROM notes WHERE id = ?", (note_id,)).fetchall()
        if not rows:
            return None
        return _note(rows[0], self._marks(note_id).get(note_id, frozenset()))

    def _require(self, note_id: str) -> Note:
        note = self.get(note_id)
        if note is None:
            raise NoteNotFoundError(note_id)
        return note

    def create(
        self, body: str = "", color: str = DEFAULT_COLOR, note_id: str | None = None
    ) -> Note:
        """note_id: one decided by the caller (the same on every device), else a new one."""
        note_id = note_id or str(uuid.uuid4())
        now = self._clock()
        with self._db:
            self._db.execute(
                "INSERT INTO notes (id, body, color, created_at, updated_at, content_hash,"
                " change_seq) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (note_id, body, color, now, now, content_hash(body), self._next_seq()),
            )
        return self._require(note_id)

    def _change(self, note_id: str, assignments: dict[str, object]) -> Note:
        with self._db:
            self._require(note_id)
            columns = "".join(f"{name} = ?, " for name in assignments)
            self._db.execute(
                f"UPDATE notes SET {columns}updated_at = ?, change_seq = ? WHERE id = ?",
                (*assignments.values(), self._clock(), self._next_seq(), note_id),  # pyright: ignore[reportArgumentType]
            )
        return self._require(note_id)

    def _require_live(self, note_id: str) -> Note:
        note = self._require(note_id)
        if note.deleted:
            raise NoteDeletedError(note_id)
        return note

    def update_body(self, note_id: str, body: str) -> Note:
        note = self._require_live(note_id)
        if note.secret:
            raise NoteIsSecretError(note_id)  # its text goes in sealed only
        if body == note.body:
            return note  # nothing changed: no new change number
        return self._change(note_id, {"body": body, "content_hash": content_hash(body)})

    # Secret notes: the text only ever encrypted here (stickle.crypto.secret_notes)

    def make_secret(self, note_id: str, title: str, sealed: bytes) -> Note:
        """The note's text replaced by sealed (it, encrypted), in one step, and gone
        from the database: overwritten where it was, out of the search index."""
        self._require_live(note_id)
        note = self._change(
            note_id,
            {
                "body": "",
                "secret_body": sealed,
                "secret_title": title,
                "content_hash": content_hash(sealed.hex()),
            },
        )
        self.forget_old_text()
        return note

    def update_secret_body(self, note_id: str, sealed: bytes) -> Note:
        note = self._require_live(note_id)
        if not note.secret:
            raise NoteNotSecretError(note_id)
        if sealed == note.sealed:
            return note
        return self._change(
            note_id, {"secret_body": sealed, "content_hash": content_hash(sealed.hex())}
        )

    def set_secret_title(self, note_id: str, title: str) -> Note:
        if not self._require_live(note_id).secret:
            raise NoteNotSecretError(note_id)
        return self._change(note_id, {"secret_title": title})

    def make_ordinary(self, note_id: str, body: str) -> Note:
        """A secret note an ordinary one again, its text (decrypted) as given."""
        if not self._require_live(note_id).secret:
            raise NoteNotSecretError(note_id)
        return self._change(
            note_id,
            {
                "body": body,
                "secret_body": None,
                "secret_title": None,
                "content_hash": content_hash(body),
            },
        )

    def forget_old_text(self) -> None:
        """Text replaced or removed leaves no trace a key to the database would
        read: freed pages are overwritten (secure_delete is on), the search
        index is merged so its old entries go, and the log is folded in."""
        self._db.execute("INSERT INTO notes_fts(notes_fts) VALUES ('optimize')")
        self._db.pragma("wal_checkpoint", "TRUNCATE")

    def set_color(self, note_id: str, color: str) -> Note:
        """color is a palette key; the colours drawn are worked out from it."""
        note = self._require_live(note_id)
        if note.color == color:
            return note
        return self._change(note_id, {"color": color})

    def set_always_on_top(self, note_id: str, on_top: bool) -> Note:
        note = self._require_live(note_id)
        if note.always_on_top == on_top:
            return note
        return self._change(note_id, {"always_on_top": int(on_top)})

    def set_opacity(self, note_id: str, opacity: float) -> Note:
        """How see-through the note is while another window is in use, 0.1 to 1.0."""
        if not 0.1 <= opacity <= 1.0:
            raise ValueError(f"opacity {opacity}")
        note = self._require_live(note_id)
        if note.opacity == opacity:
            return note
        return self._change(note_id, {"opacity": opacity})

    def set_locked(self, note_id: str, locked: bool) -> Note:
        """Kept where it is, as it is: moving, resizing and editing are refused."""
        note = self._require_live(note_id)
        if note.locked == locked:
            return note
        return self._change(note_id, {"locked": int(locked)})

    def set_collapsed(self, note_id: str, collapsed: bool) -> Note:
        """Folded to its title bar; its size is kept with its place (note_layouts)."""
        note = self._require_live(note_id)
        if note.collapsed == collapsed:
            return note
        return self._change(note_id, {"collapsed": int(collapsed)})

    def set_hidden(self, note_id: str, hidden: bool) -> Note:
        note = self._require_live(note_id)
        if note.hidden == hidden:
            return note
        return self._change(note_id, {"hidden": int(hidden)})

    def set_category(self, note_id: str, category_id: str | None) -> Note:
        """What the note is about: a category not deleted, or None for none."""
        note = self._require_live(note_id)
        if note.label == category_id:
            return note
        if category_id is not None:
            rows = self._db.execute(
                "SELECT 1 FROM categories WHERE id = ? AND deleted_at IS NULL", (category_id,)
            ).fetchall()
            if not rows:
                raise KeyError(category_id)
        return self._change(note_id, {"label": category_id})

    def set_mark(self, note_id: str, mark_id: str, on: bool) -> Note:
        """Put a mark not deleted on the note, or take one off."""
        note = self._require_live(note_id)
        if (mark_id in note.marks) == on:
            return note
        if on:
            rows = self._db.execute(
                "SELECT 1 FROM marks WHERE id = ? AND deleted_at IS NULL", (mark_id,)
            ).fetchall()
            if not rows:
                raise KeyError(mark_id)
        with self._db:
            if on:
                self._db.execute(
                    "INSERT INTO note_marks (note_id, mark_id) VALUES (?, ?)", (note_id, mark_id)
                )
            else:
                self._db.execute(
                    "DELETE FROM note_marks WHERE note_id = ? AND mark_id = ?", (note_id, mark_id)
                )
            # The note itself counts as changed, so that sync notices.
            self._change(note_id, {})
        return self._require(note_id)

    def delete(self, note_id: str) -> Note:
        self._require_live(note_id)
        return self._change(note_id, {"deleted_at": self._clock()})

    def restore(self, note_id: str) -> Note:
        """Out of the trash, with its category if that was deleted since (see
        _category_back)."""
        note = self._require(note_id)
        if not note.deleted:
            return note
        with self._db:
            changes: dict[str, object] = {"deleted_at": None}
            category_id = self._category_back(note.label)
            if category_id != note.label:
                changes["label"] = category_id
            self._change(note_id, changes)
        return self._require(note_id)

    def _category_back(self, category_id: str | None) -> str | None:
        """The category a note coming back from the trash goes into: its own, brought
        back too if it was deleted, or another of the same name made since."""
        if category_id is None:
            return None
        rows = self._db.execute(
            "SELECT name, deleted_at FROM categories WHERE id = ?", (category_id,)
        ).fetchall()
        if not rows:
            return None
        name, deleted = rows[0]
        if deleted is None:
            return category_id
        folded = str(name).casefold()
        live = self._db.execute(
            "SELECT id, name FROM categories WHERE deleted_at IS NULL ORDER BY position"
        ).fetchall()
        same = next((str(i) for i, n in live if str(n).casefold() == folded), None)
        if same is not None:
            return same
        self._db.execute(
            "UPDATE categories SET deleted_at = NULL, updated_at = ?,"
            " position = (SELECT coalesce(max(position), 0) + 1 FROM categories) WHERE id = ?",
            (self._clock(), category_id),
        )
        return category_id

    def remove_category(self, category_id: str, with_notes: bool) -> list[str]:
        """Delete a category. Its notes lose it, or (with_notes) go into the trash
        together, all at one moment, so that they come back together. The ids of
        those notes."""
        ids: list[str] = []
        with self._db:
            rows = self._db.execute(
                "SELECT deleted_at FROM categories WHERE id = ?", (category_id,)
            ).fetchall()
            if not rows or rows[0][0] is not None:
                raise KeyError(category_id)
            now = self._clock()
            self._db.execute(
                "UPDATE categories SET deleted_at = ?, updated_at = ? WHERE id = ?",
                (now, now, category_id),
            )
            ids = [
                str(row[0])
                for row in self._db.execute(
                    "SELECT id FROM notes WHERE label = ? AND deleted_at IS NULL", (category_id,)
                ).fetchall()
            ]
            for note_id in ids:
                # A deleted note keeps its category, which comes back with it.
                self._change(note_id, {"deleted_at": now} if with_notes else {"label": None})
        return ids

    def remove_mark(self, mark_id: str) -> list[str]:
        """Delete a mark: every note that had it, deleted or not, loses it. The ids of
        the notes not deleted that had it."""
        ids: list[str] = []
        with self._db:
            rows = self._db.execute(
                "SELECT deleted_at FROM marks WHERE id = ?", (mark_id,)
            ).fetchall()
            if not rows or rows[0][0] is not None:
                raise KeyError(mark_id)
            now = self._clock()
            self._db.execute(
                "UPDATE marks SET deleted_at = ?, updated_at = ? WHERE id = ?", (now, now, mark_id)
            )
            owners = self._db.execute(
                "SELECT note_id FROM note_marks WHERE mark_id = ?", (mark_id,)
            ).fetchall()
            self._db.execute("DELETE FROM note_marks WHERE mark_id = ?", (mark_id,))
            for (owner,) in owners:
                note = self.get(str(owner))
                if note is not None and not note.deleted:
                    ids.append(note.id)
                    self._change(note.id, {})  # changed, so that sync notices
        return ids

    def deleted_with(self, note_id: str) -> list[Note]:
        """The notes put into the trash at the same moment as this one, it too."""
        note = self._require(note_id)
        if note.deleted_at is None:
            return []
        return self._list("deleted_at = ?", "seq", (note.deleted_at,))

    def _list(self, where: str, order: str, params: tuple[str, ...] = ()) -> list[Note]:
        rows = self._db.execute(
            f"SELECT {COLUMNS} FROM notes WHERE {where} ORDER BY {order}", params
        ).fetchall()
        marks = self._marks()
        return [_note(row, marks.get(str(row[0]), frozenset())) for row in rows]

    def visible(self) -> list[Note]:
        return self._list("deleted_at IS NULL AND hidden = 0", "created_at, seq")

    def hidden(self) -> list[Note]:
        """Most recently changed first."""
        return self._list("deleted_at IS NULL AND hidden = 1", "change_seq DESC")

    def deleted(self) -> list[Note]:
        """The trash: most recently deleted first."""
        return self._list("deleted_at IS NOT NULL", "deleted_at DESC, change_seq DESC")

    def purge(self, note_id: str) -> None:
        """Empty a deleted note from the trash for good: only a deletion record stays.

        The note, its places on screen, its marks and its search entry go, in one
        transaction.
        """
        with self._db:
            note = self._require(note_id)
            if note.deleted_at is None:
                raise NoteNotDeletedError(note_id)
            self._db.execute(
                "INSERT INTO deletion_records (note_id, deleted_at, purged_at) VALUES (?, ?, ?)"
                " ON CONFLICT (note_id) DO UPDATE SET deleted_at = excluded.deleted_at,"
                " purged_at = excluded.purged_at",
                (note_id, note.deleted_at, self._clock()),
            )
            self._db.execute("DELETE FROM note_layouts WHERE note_id = ?", (note_id,))
            self._db.execute("DELETE FROM note_marks WHERE note_id = ?", (note_id,))
            self._db.execute("DELETE FROM notes WHERE id = ?", (note_id,))

    def discard(self, note_id: str) -> None:
        """Remove a note with no text (spaces and line breaks at most) for good, past
        the trash: there is nothing in it to bring back. Only a deletion record stays."""
        with self._db:
            note = self._require_live(note_id)
            # A secret note's body is always empty: its text is sealed, unseen here.
            if note.body.strip() or note.secret:
                raise NoteNotBlankError(note_id)
            self.delete(note_id)
            self.purge(note_id)

    def empty_trash(self) -> int:
        """Purge every deleted note, all or none; how many there were."""
        notes = self.deleted()
        with self._db:
            for note in notes:
                self.purge(note.id)
        return len(notes)

    def purge_expired(self, days: int = KEEP_DAYS) -> int:
        """Purge notes in the trash longer than days, and forget deletion records
        emptied longer ago than that; how many notes were purged."""
        cutoff = days_before(self._clock(), days)
        expired = self._list("deleted_at IS NOT NULL AND deleted_at < ?", "seq", (cutoff,))
        with self._db:
            for note in expired:
                self.purge(note.id)
            self._db.execute("DELETE FROM deletion_records WHERE purged_at < ?", (cutoff,))
        return len(expired)

    def deletion_records(self) -> list[tuple[str, str, str]]:
        """(note id, deleted at, emptied at) of notes emptied from the trash."""
        rows = self._db.execute(
            "SELECT note_id, deleted_at, purged_at FROM deletion_records ORDER BY purged_at"
        )
        return [(str(a), str(b), str(c)) for a, b, c in rows]

    def live(self) -> list[Note]:
        """Every note not deleted, shown or hidden; most recently changed first."""
        return self._list("deleted_at IS NULL", "change_seq DESC")

    def matching(self, term: str) -> set[str]:
        """Ids of notes whose text contains term, in the trash or not."""
        return set(search(self._db, term))

    def last_deleted(self) -> Note | None:
        notes = self._list("deleted_at IS NOT NULL", "change_seq DESC LIMIT 1")
        return notes[0] if notes else None

    def all(self) -> list[Note]:
        """Every note, deleted ones included (export, tests)."""
        return self._list("1", "seq")
