import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest

from stickle.core.note import content_hash
from stickle.data import schema
from stickle.data.database import KEY_BYTES, open_database
from stickle.data.notes import NoteRepository
from stickle.data.schema import (
    BACKUPS_KEPT,
    V1,
    MigrationError,
    NewerSchemaError,
    backup,
    open_store,
    schema_version,
    search_index_is_consistent,
)
from stickle.data.search import search

KEY = secrets.token_bytes(KEY_BYTES)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "notes.db"


def ticking_clock() -> Iterator[str]:
    for second in range(60):
        yield f"2026-09-26T10:00:{second:02d}.000Z"
    raise AssertionError("clock ran out")


def make_v1(path: Path, bodies: list[str]) -> list[str]:
    """A database at version 1, whatever the app's latest (the tests' steps follow it)."""
    latest = schema.MIGRATIONS
    schema.MIGRATIONS = [V1]
    try:
        connection = open_store(path, KEY)
    finally:
        schema.MIGRATIONS = latest
    # Written as version 1 had it: the app's code follows the latest version.
    ids = [str(uuid.uuid4()) for _ in bodies]
    with connection:
        for note_id, body in zip(ids, bodies, strict=True):
            connection.execute(
                "INSERT INTO notes (id, body, color, created_at, updated_at, content_hash,"
                " change_seq) VALUES (?, ?, 'yellow', '2026-09-26T10:00:00.000Z',"
                " '2026-09-26T10:00:00.000Z', ?, 1)",
                (note_id, body, content_hash(body)),
            )
    connection.close()
    return ids


def bodies(path: Path) -> dict[str, str]:
    """The notes' text, read as any version has it."""
    connection = open_database(path, KEY)
    try:
        return {str(i): str(b) for i, b in connection.execute("SELECT id, body FROM notes")}
    finally:
        connection.close()


def version_of(path: Path) -> int:
    connection = open_database(path, KEY)
    try:
        return schema_version(connection)
    finally:
        connection.close()


def leftovers(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.glob("*.migrating*"))


def backups(path: Path) -> list[Path]:
    return sorted((path.parent / "backups").glob("*.db"))


def test_new_database_reaches_the_latest_version_without_a_backup(path: Path) -> None:
    connection = open_store(path, KEY)
    assert schema_version(connection) == schema.latest_version()
    connection.close()
    assert backups(path) == []
    assert leftovers(path) == []


def test_opening_the_latest_version_changes_nothing(path: Path) -> None:
    connection = open_store(path, KEY)
    ids = [NoteRepository(connection).create(body).id for body in ["회의록", "장보기"]]
    connection.close()
    connection = open_store(path, KEY)
    connection.close()
    assert set(bodies(path)) == set(ids)
    assert backups(path) == []


def test_upgrade_keeps_notes_and_leaves_a_backup(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_v1(path, ["회의록을 내일까지", "😀 여러\n줄"])
    before = bodies(path)
    monkeypatch.setattr(schema, "MIGRATIONS", [V1, "ALTER TABLE notes ADD COLUMN extra TEXT"])

    connection = open_store(path, KEY)
    assert schema_version(connection) == 2
    connection.close()

    assert bodies(path) == before
    assert len(backups(path)) == 1
    assert version_of(backups(path)[0]) == 1  # the backup still opens with the old app
    assert leftovers(path) == []


def test_failed_step_stays_at_the_last_good_version(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_v1(path, ["회의록"])
    before = bodies(path)
    broken = "ALTER TABLE notes ADD COLUMN extra TEXT; SELECT no_such_function();"
    monkeypatch.setattr(schema, "MIGRATIONS", [V1, broken])

    with pytest.raises(MigrationError):
        open_store(path, KEY)

    assert version_of(path) == 1
    assert bodies(path) == before
    assert leftovers(path) == []


def test_fixed_app_continues_from_the_last_good_version(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_v1(path, ["회의록"])
    before = bodies(path)
    monkeypatch.setattr(schema, "MIGRATIONS", [V1, "SELECT no_such_function();"])
    with pytest.raises(MigrationError):
        open_store(path, KEY)

    monkeypatch.setattr(schema, "MIGRATIONS", [V1, "ALTER TABLE notes ADD COLUMN extra TEXT"])
    connection = open_store(path, KEY)
    assert schema_version(connection) == 2
    connection.close()
    assert bodies(path) == before


def test_migration_that_loses_notes_is_refused(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_v1(path, ["회의록\n내일까지", "장보기"])
    before = bodies(path)
    monkeypatch.setattr(schema, "MIGRATIONS", [V1, "DELETE FROM notes WHERE body = '장보기'"])

    with pytest.raises(MigrationError) as failure:
        open_store(path, KEY)

    assert version_of(path) == 1
    assert bodies(path) == before
    diff = failure.value.diff
    assert diff is not None
    assert diff.missing == ["장보기"] and diff.changed == [] and diff.added == []


def test_migration_that_alters_text_is_refused(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = "  \n회의록\n둘째 줄"
    make_v1(path, [original, "그대로"])
    step = (
        "UPDATE notes SET body = 'x' WHERE body <> '그대로';"
        " INSERT INTO notes (id, body, color, created_at, updated_at, content_hash, change_seq)"
        " VALUES ('new', '새 메모', 'yellow', '', '', '', 0)"
    )
    monkeypatch.setattr(schema, "MIGRATIONS", [V1, step])

    with pytest.raises(MigrationError) as failure:
        open_store(path, KEY)
    assert sorted(bodies(path).values()) == sorted([original, "그대로"])
    diff = failure.value.diff
    assert diff is not None
    # Shown by their first non-empty line, so the user can tell which notes they are.
    assert diff.changed == ["회의록"] and diff.added == ["새 메모"] and diff.missing == []


def break_search_index(path: Path) -> None:
    """Drop one note from the search index behind the triggers' back."""
    connection = open_database(path, KEY)
    seq, body = connection.execute("SELECT seq, body FROM notes LIMIT 1").fetchall()[0]
    with connection:
        connection.execute(
            "INSERT INTO notes_fts(notes_fts, rowid, body) VALUES ('delete', ?, ?)", (seq, body)
        )
    assert not search_index_is_consistent(connection)
    connection.close()


def test_the_index_check_notices_a_missing_entry(path: Path) -> None:
    # Regression: the default FTS5 check only looks at the index's own structure.
    make_v1(path, ["회의록"])
    break_search_index(path)


def test_a_damaged_search_index_is_rebuilt_on_opening(
    path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ids = make_v1(path, ["회의록을 내일까지"])
    break_search_index(path)

    connection = open_store(path, KEY)
    assert search_index_is_consistent(connection)
    assert search(connection, "회의록") == ids
    connection.close()
    assert "search index" in caplog.text
    assert "회의록" not in caplog.text


def test_an_upgrade_that_only_upsets_the_index_is_repaired(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_v1(path, ["회의록", "장보기"])
    before = bodies(path)
    breaks_index = (
        "INSERT INTO notes_fts(notes_fts, rowid, body)"
        " SELECT 'delete', seq, body FROM notes WHERE body = '장보기'"
    )
    monkeypatch.setattr(schema, "MIGRATIONS", [V1, breaks_index])

    connection = open_store(path, KEY)
    assert schema_version(connection) == 2
    assert search_index_is_consistent(connection)
    connection.close()
    assert bodies(path) == before


def test_several_steps_run_in_order(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_v1(path, ["회의록"])
    steps = [
        V1,
        "CREATE TABLE step2 (n INTEGER)",
        "INSERT INTO step2 VALUES (3)",  # fails unless step 2 ran first
    ]
    monkeypatch.setattr(schema, "MIGRATIONS", steps)

    connection = open_store(path, KEY)
    assert schema_version(connection) == 3
    assert connection.execute("SELECT n FROM step2").fetchall() == [(3,)]
    connection.close()
    assert len(backups(path)) == 1  # one backup per upgrade, not per step


def test_newer_database_is_refused_and_left_untouched(path: Path) -> None:
    make_v1(path, ["회의록"])
    connection = open_database(path, KEY)
    connection.pragma("user_version", 99)
    connection.close()
    data = path.read_bytes()

    with pytest.raises(NewerSchemaError):
        open_store(path, KEY)
    assert path.read_bytes() == data
    assert backups(path) == []


def test_only_the_newest_backups_are_kept(path: Path) -> None:
    make_v1(path, ["회의록"])
    clock = ticking_clock()
    made = [backup(path, 1, lambda: next(clock)) for _ in range(BACKUPS_KEPT + 2)]

    assert backups(path) == made[-BACKUPS_KEPT:]


def test_backup_is_encrypted_and_opens_with_the_same_key(path: Path) -> None:
    make_v1(path, ["회의록을 내일까지"])
    copy = backup(path, 1)

    assert "회의록".encode() not in copy.read_bytes()
    plain = apsw.Connection(str(copy))
    with pytest.raises(apsw.NotADBError):
        plain.execute("SELECT count(*) FROM sqlite_schema").fetchall()
    plain.close()
    assert list(bodies(copy).values()) == ["회의록을 내일까지"]


def test_upgrading_failing_again_and_again_keeps_the_copy_to_go_back_to_the_app_before(
    path: Path,
) -> None:
    # An upgrade stopped at the same step each start leaves the database a
    # version on: copies of that one would push out the only one the app
    # before opens.
    make_v1(path, ["회의록"])
    clock = ticking_clock()
    before = backup(path, 11, lambda: next(clock))
    made = [backup(path, 20, lambda: next(clock)) for _ in range(BACKUPS_KEPT + 2)]

    assert backups(path) == [before, *made[-BACKUPS_KEPT:]]


def test_the_copy_kept_from_before_is_the_latest_older_than_those_kept(path: Path) -> None:
    make_v1(path, ["회의록"])
    clock = ticking_clock()
    made = [backup(path, version, lambda: next(clock)) for version in (8, 11, 20, 31, 40, 52)]

    # 31, 40 and 52 kept: of those older, 20 (the app before 31's), not 8 or 11.
    assert backups(path) == made[2:]
