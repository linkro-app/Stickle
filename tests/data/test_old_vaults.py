"""What this device keeps of vaults it no longer syncs with."""

import json
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest

from stickle.data import sync_connection, sync_state
from stickle.data.schema import open_store

KEY = bytes(range(32))
TABLES = ("sync_staged", "sync_held", "sync_untold", "sync_left")
KEPT = ("sync_notes", "sync_records", "sync_devices", "sync_vaults", "sync_keys")


@pytest.fixture
def db(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


def seen(text: str | None, secret: bool = False) -> str:
    body: dict[str, object] = {"id": "v1", "deleted": False}
    if secret:
        body["text"] = None
        body["secret"] = {"sealed": "AAAA", "bound": "n1"}
    elif text is not None:
        body["text"] = text
    return json.dumps({"id": "n1", "body": body, "fields": {}})


def kept_of(db: apsw.Connection, vault: str) -> None:
    """A round's worth of rows of vault, those it makes for a while too."""
    with db:
        db.execute(
            "INSERT INTO sync_notes (vault, note_id, seen, tag, outbox) VALUES (?, ?, ?, ?, ?)",
            (vault, "n1", seen("그때의 글"), "t1", None),
        )
        db.execute(
            "INSERT INTO sync_notes (vault, note_id, seen, tag, outbox) VALUES (?, ?, ?, ?, ?)",
            (vault, "n2", seen(None, secret=True), "t2", None),
        )
        db.execute(
            "INSERT INTO sync_notes (vault, note_id, seen, tag, outbox) VALUES (?, ?, ?, ?, ?)",
            (vault, "n3", seen("보낸 글"), "t3", seen("보낼 글")),
        )
        db.execute(
            "INSERT INTO sync_records (vault, record, kind, item, seen)"
            " VALUES (?, 'r', 'k', 'i', '{}')",
            (vault,),
        )
        db.execute("INSERT INTO sync_devices (vault, device, seen) VALUES (?, 'd', '{}')", (vault,))
        db.execute("INSERT INTO sync_vaults (vault, cursor) VALUES (?, 'c')", (vault,))
        db.execute(
            "INSERT INTO sync_keys (vault, kind, digest, set_at) VALUES (?, 'B', 'x', 'now')",
            (vault,),
        )
        db.execute("INSERT INTO sync_staged (vault, path, data) VALUES (?, 'p', x'00')", (vault,))
        db.execute(
            "INSERT INTO sync_held (vault, note_id, kind) VALUES (?, 'n1', 'gone')", (vault,)
        )
        db.execute("INSERT INTO sync_untold (vault, path) VALUES (?, 'p')", (vault,))
        db.execute(
            "INSERT INTO sync_left (vault, path, digest, why, first_at)"
            " VALUES (?, 'p', 'd', 'w', 0)",
            (vault,),
        )


def count(db: apsw.Connection, table: str, vault: str) -> int:
    rows = db.execute(f"SELECT count(*) FROM {table} WHERE vault = ?", (vault,)).fetchall()
    return int(rows[0][0])  # pyright: ignore[reportArgumentType]


def rows_of(db: apsw.Connection, vault: str) -> list[object]:
    return [
        db.execute(f"SELECT * FROM {table} WHERE vault = ? ORDER BY 2", (vault,)).fetchall()
        for table in TABLES + KEPT
    ]


def test_another_vault_keeps_what_its_notes_were_and_not_what_rounds_made_for_a_while(
    db: apsw.Connection,
) -> None:
    kept_of(db, "old")
    kept_of(db, "now")
    as_now = rows_of(db, "now")
    notes_then = db.execute("SELECT * FROM sync_notes WHERE vault = 'old' ORDER BY 2").fetchall()

    with db:
        sync_state.let_go_of_others(db, "now")

    assert rows_of(db, "now") == as_now  # the one synced with: as it was
    for table in TABLES:
        assert count(db, table, "old") == 0
    for table in KEPT:
        assert count(db, table, "old") > 0
    # Its text too: how a note is told unchanged here, if this device goes back.
    assert db.execute("SELECT * FROM sync_notes WHERE vault = 'old' ORDER BY 2").fetchall() == (
        notes_then
    )


def test_with_no_vault_synced_with_every_vault_is_another(db: apsw.Connection) -> None:
    kept_of(db, "one")
    kept_of(db, "two")

    with db:
        sync_state.let_go_of_others(db, None)

    for vault in ("one", "two"):
        for table in TABLES:
            assert count(db, table, vault) == 0
        assert count(db, "sync_notes", vault) == 3


def test_letting_go_again_writes_nothing(db: apsw.Connection) -> None:
    kept_of(db, "old")
    with db:
        sync_state.let_go_of_others(db, None)
    before = db.total_changes()

    with db:
        sync_state.let_go_of_others(db, None)

    assert db.total_changes() == before


def test_a_connection_to_another_vault_lets_go_of_what_rounds_made_of_the_one_before(
    db: apsw.Connection,
) -> None:
    kept_of(db, "before")
    kept_of(db, "after")  # its first look, fetched: what its first round takes in
    as_after = rows_of(db, "after")

    sync_connection.keep(db, sync_connection.Login("after", sync_connection.WEBDAV, "", "", "", {}))

    for table in TABLES:
        assert count(db, table, "before") == 0
    assert rows_of(db, "after") == as_after
