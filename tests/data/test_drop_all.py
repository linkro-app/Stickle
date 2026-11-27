"""Every note removed in one go, as each would be removed on its own: keeping
only the cloud's notes on joining a vault (stickle.sync.engine.cloud_only)."""

import secrets
import shutil
from pathlib import Path

import apsw

from stickle.core.layout import Place, Size
from stickle.crypto.secret_notes import seal_text
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store

KEY = secrets.token_bytes(32)
SECRET_KEY = secrets.token_bytes(32)
NOW = "2026-01-01T09:00:00.000Z"


def clock() -> str:
    return NOW


def made(path: Path) -> list[str]:
    """Notes of every sort, with what goes with them; their ids."""
    db = open_store(path, KEY)
    try:
        notes = NoteRepository(db, clock)
        plain = notes.create("그냥 메모")
        notes.update_body(plain.id, "고친 메모", keep_earlier=True)  # an earlier text
        notes.set_color(plain.id, "pink")  # a field's stamp
        LayoutRepository(db, clock).save(plain.id, Size(260, 240), (1, Place(1, "", 0.1, 0.1)))
        trashed = notes.create("휴지통 메모")
        notes.delete(trashed.id)
        secret = notes.create("비밀 메모")
        notes.make_secret(secret.id, "은행", seal_text(SECRET_KEY, secret.id, "비밀 메모"))
        emptied = notes.create("비운 메모")
        notes.delete(emptied.id)
        notes.purge(emptied.id)  # a deletion record of a note gone: stays
        with db:
            # A deletion record of a note here too (one brought back since):
            # drop leaves none of the note's.
            db.execute(
                "INSERT INTO deletion_records (note_id, deleted_at, purged_at) VALUES (?, ?, ?)",
                (trashed.id, NOW, NOW),
            )
            for vault in ("old", "now"):
                for note_id in (plain.id, trashed.id):
                    db.execute(
                        "INSERT INTO sync_notes (vault, note_id, seen) VALUES (?, ?, ?)",
                        (vault, note_id, '{"body": {"id": "v", "text": "그때 글"}}'),
                    )
        return [plain.id, trashed.id, secret.id]
    finally:
        db.close()


def tables(db: apsw.Connection) -> dict[str, list[apsw.SQLiteValues]]:
    """Every table's rows but the counters' and the search index's own."""
    names = [
        str(row[0])
        for row in db.execute("SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name")
        if not str(row[0]).startswith(("notes_fts", "sqlite_")) and row[0] != "local_counters"
    ]
    return {name: db.execute(f"SELECT * FROM {name} ORDER BY 1, 2").fetchall() for name in names}


def test_removed_in_one_go_as_each_on_its_own_would_be(tmp_path: Path) -> None:
    ids = made(tmp_path / "each.db")
    shutil.copyfile(tmp_path / "each.db", tmp_path / "all.db")
    each = open_store(tmp_path / "each.db", KEY)
    every = open_store(tmp_path / "all.db", KEY)
    try:
        notes = NoteRepository(each, clock)
        with each:
            for note_id in ids:
                notes.drop(note_id)

        assert NoteRepository(every, clock).drop_all() == len(ids)

        assert tables(every) == tables(each)
        assert every.execute("SELECT count(*) FROM notes").fetchall() == [(0,)]
        once = every.execute("SELECT note_id FROM once_secret").fetchall()
        assert once == [(ids[2],)]  # the secret one remembered
        assert every.execute("SELECT count(*) FROM deletion_records").fetchall() == [(1,)]
        assert (
            every.execute("SELECT seen FROM sync_notes").fetchall()
            == [('{"body":{"id":"v"}}',)] * 4
        )  # in every vault, without their text
        assert every.execute(
            "SELECT count(*) FROM notes_fts WHERE notes_fts MATCH '메모'"
        ).fetchall() == [(0,)]
    finally:
        each.close()
        every.close()


def test_what_sync_knows_of_a_note_is_found_by_the_note_alone(tmp_path: Path) -> None:
    db = open_store(tmp_path / "notes.db", KEY)
    try:
        plan = db.execute(
            "EXPLAIN QUERY PLAN UPDATE sync_notes SET seen = NULL WHERE note_id IN (?)", ("n",)
        ).fetchall()
        assert any("sync_notes_by_note" in str(row) for row in plan), plan
    finally:
        db.close()
