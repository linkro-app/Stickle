"""A note's earlier texts, kept on this computer only.

Before a note's text changes, the text it had is kept: once for each edit (the
app says which save begins one), and whenever a text from elsewhere, or an
earlier one gone back to, takes its place. Each says when it became the note's
text and where from (None: this computer), or, put aside by going back to an
earlier one, when that was. They are kept 30 days from when they were put
aside, the newest 50 of a note at most, and the same text once: kept again, it
takes the newer place.

A secret note's are encrypted as its text is, and they follow the note: made
secret, its earlier texts are sealed too, and the plain ones overwritten where
they were; made ordinary, they are opened again. They never go to the vault,
but go into the full export with every other table.
"""

from collections.abc import Callable
from dataclasses import dataclass

import apsw

from stickle.core.clock import Clock, days_before, utc_now
from stickle.core.markdown import note_title
from stickle.core.note import content_hash
from stickle.crypto.secret_notes import SecretNoteError

KEEP_DAYS = 30
KEEP_PER_NOTE = 50
# What the list reads of each text: enough for its first line, never the whole.
LISTED_CHARS = 400


@dataclass(frozen=True)
class EarlierVersion:
    id: int
    # When it became the note's text, or, put aside by going back, when that was.
    at: str
    came_from: str | None  # the device it came from; None: this computer
    restored: bool  # put aside by going back to an earlier one
    # As its title shows (Markdown marks left out); None while sealed (a secret note's).
    first_line: str | None

    @property
    def sealed(self) -> bool:
        return self.first_line is None


def keep(db: apsw.Connection, note_id: str, now: str, restored: bool = False) -> bool:
    """The note's text as it is now, kept as an earlier one before it changes;
    False when there is none to keep (no note, or no text)."""
    rows = db.execute(
        "SELECT body, secret_body, content_hash, coalesce(text_at, updated_at), text_from"
        " FROM notes WHERE id = ?",
        (note_id,),
    ).fetchall()
    if not rows:
        return False
    body, sealed, digest, made_at, made_by = rows[0]
    if sealed is None and not str(body).strip():
        return False
    db.execute(
        "DELETE FROM note_versions WHERE note_id = ? AND content_hash = ?", (note_id, digest)
    )
    db.execute(
        "INSERT INTO note_versions (note_id, body, sealed, content_hash, made_at, made_by,"
        " restored, kept_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            note_id,
            None if sealed is not None else str(body),
            sealed,
            digest,
            made_at,
            made_by,
            int(restored),
            now,
        ),
    )
    prune(db, now, note_id)
    return True


def prune(db: apsw.Connection, now: str, note_id: str | None = None) -> int:
    """Earlier texts put aside more than 30 days ago, and those past a note's
    newest 50, gone: of one note, or of all. How many went."""
    one = "" if note_id is None else " AND note_id = ?"
    within = () if note_id is None else (note_id,)
    db.execute(
        f"DELETE FROM note_versions WHERE kept_at < ?{one}", (days_before(now, KEEP_DAYS), *within)
    )
    gone = db.changes()
    db.execute(
        "DELETE FROM note_versions WHERE id IN (SELECT id FROM (SELECT id, row_number() OVER"
        " (PARTITION BY note_id ORDER BY kept_at DESC, id DESC) AS place FROM note_versions"
        f" WHERE 1{one}) WHERE place > ?)",
        (*within, KEEP_PER_NOTE),
    )
    return gone + db.changes()


def plain_count(db: apsw.Connection, note_id: str) -> int:
    return int(
        db.execute(  # pyright: ignore[reportArgumentType]
            "SELECT count(*) FROM note_versions WHERE note_id = ? AND sealed IS NULL", (note_id,)
        ).fetchall()[0][0]
    )


def seal_all(db: apsw.Connection, note_id: str, seal: Callable[[str], bytes]) -> int:
    """A note made secret: its earlier texts in the clear sealed as its text is
    (seal: text -> encrypted). Their plain text is overwritten once the database
    clears away what was freed (see NoteRepository.forget_old_text). How many."""
    rows = db.execute(
        "SELECT id, body FROM note_versions WHERE note_id = ? AND sealed IS NULL", (note_id,)
    ).fetchall()
    for version_id, body in rows:
        sealed = seal(str(body))
        db.execute(
            "UPDATE note_versions SET body = NULL, sealed = ?, content_hash = ? WHERE id = ?",
            (sealed, content_hash(sealed.hex()), version_id),
        )
    return len(rows)


def open_all(db: apsw.Connection, note_id: str, open_: Callable[[bytes], str]) -> int:
    """A secret note made ordinary: its earlier texts opened (open_: encrypted ->
    text). One that does not open stays as it is, sealed, rather than be lost.
    The same text opened twice is kept once. How many were opened."""
    rows = db.execute(
        "SELECT id, sealed FROM note_versions WHERE note_id = ? AND sealed IS NOT NULL",
        (note_id,),
    ).fetchall()
    opened = 0
    for version_id, sealed in rows:
        try:
            body = open_(bytes(sealed))  # pyright: ignore[reportArgumentType]
        except SecretNoteError:
            continue
        db.execute(
            "UPDATE note_versions SET body = ?, sealed = NULL, content_hash = ? WHERE id = ?",
            (body, content_hash(body), version_id),
        )
        opened += 1
    db.execute(
        "DELETE FROM note_versions WHERE note_id = ? AND EXISTS (SELECT 1 FROM note_versions"
        " AS newer WHERE newer.note_id = note_versions.note_id"
        " AND newer.content_hash = note_versions.content_hash AND (newer.kept_at,"
        " newer.id) > (note_versions.kept_at, note_versions.id))",
        (note_id,),
    )
    return opened


def forget(db: apsw.Connection, note_id: str) -> None:
    """All of a note's earlier texts gone (the note emptied from the trash)."""
    db.execute("DELETE FROM note_versions WHERE note_id = ?", (note_id,))


def text_of(db: apsw.Connection, note_id: str, version_id: int) -> str | bytes | None:
    """One earlier text of the note, whole: plain, or encrypted (a secret note's)."""
    rows = db.execute(
        "SELECT body, sealed FROM note_versions WHERE id = ? AND note_id = ?",
        (version_id, note_id),
    ).fetchall()
    if not rows:
        return None
    body, sealed = rows[0]
    return bytes(sealed) if sealed is not None else str(body)  # pyright: ignore[reportArgumentType]


class VersionRepository:
    """The earlier texts a note shows, and taking them away."""

    def __init__(self, connection: apsw.Connection, clock: Clock = utc_now) -> None:
        self._db = connection
        self._clock = clock

    def _listed_rows(self, note_id: str, limit: str = "") -> list[apsw.SQLiteValues]:
        return self._db.execute(
            "SELECT v.id, CASE WHEN v.restored THEN v.kept_at ELSE v.made_at END AS shown_at,"
            " v.made_by, v.restored, substr(v.body, 1, ?) FROM note_versions AS v"
            " JOIN notes AS n ON n.id = v.note_id"
            " WHERE v.note_id = ? AND v.kept_at >= ? AND v.content_hash != n.content_hash"
            f" ORDER BY shown_at DESC, v.id DESC{limit}",
            (LISTED_CHARS, note_id, days_before(self._clock(), KEEP_DAYS)),
        ).fetchall()

    def listed(self, note_id: str) -> list[EarlierVersion]:
        """Newest first; neither one more than 30 days old (put away at the next
        start) nor the text the note has now."""
        return [
            EarlierVersion(
                id=int(version_id),  # pyright: ignore[reportArgumentType]
                at=str(at),
                came_from=None if made_by is None else str(made_by),
                restored=bool(restored),
                first_line=None if start is None else note_title(str(start)),
            )
            for version_id, at, made_by, restored, start in self._listed_rows(note_id)
        ]

    def has_any(self, note_id: str) -> bool:
        return bool(self._listed_rows(note_id, " LIMIT 1"))

    def text(self, note_id: str, version_id: int) -> str | bytes | None:
        return text_of(self._db, note_id, version_id)

    def delete(self, note_id: str, version_id: int) -> bool:
        """One taken away for good; False if it was not there."""
        gone = False
        with self._db:
            self._db.execute(
                "DELETE FROM note_versions WHERE id = ? AND note_id = ?", (version_id, note_id)
            )
            gone = self._db.changes() > 0
        if gone:
            self._clear_away()
        return gone

    def delete_all(self, note_id: str) -> int:
        """All of a note's taken away for good; how many there were."""
        gone = 0
        with self._db:
            forget(self._db, note_id)
            gone = self._db.changes()
        if gone:
            self._clear_away()
        return gone

    def prune(self) -> int:
        """At start: those of every note past 30 days or their note's newest 50."""
        gone = 0
        with self._db:
            gone = prune(self._db, self._clock())
        return gone

    def _clear_away(self) -> None:
        # The pages freed are overwritten (secure_delete is on); the log too.
        self._db.pragma("wal_checkpoint", "TRUNCATE")
