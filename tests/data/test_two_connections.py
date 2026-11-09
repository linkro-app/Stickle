"""The notes database used by the app and by sync at once, each on a connection
of its own: neither is turned down because the other is writing."""

import secrets
import threading
import time
from pathlib import Path

import apsw
import pytest

from stickle.data.database import KEY_BYTES, open_database
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.sync.place import writing

KEY = secrets.token_bytes(KEY_BYTES)
FORTH_AND_BACK = 200  # writes of each side, enough for them to meet


@pytest.fixture
def path(tmp_path: Path) -> Path:
    file = tmp_path / "notes.db"
    open_store(file, KEY).close()
    return file


def test_a_save_waits_for_the_other_connections_write(path: Path) -> None:
    app, other = open_database(path, KEY), open_database(path, KEY)
    notes = NoteRepository(app)
    note = notes.create("one")
    began = threading.Event()

    def holds() -> None:
        with writing(other):
            began.set()
            time.sleep(0.3)

    holding = threading.Thread(target=holds)
    holding.start()
    assert began.wait(5)
    started = time.monotonic()
    notes.update_body(note.id, "two")
    waited = time.monotonic() - started
    holding.join()
    assert waited > 0.2
    assert notes.get(note.id).body == "two"  # pyright: ignore[reportOptionalMemberAccess]
    app.close()
    other.close()


def test_a_save_that_reads_first_is_not_turned_down(path: Path) -> None:
    """update_body reads the note, then writes it, in one transaction."""
    app, other = open_database(path, KEY), open_database(path, KEY)
    notes = NoteRepository(app)
    mine = notes.create("0")
    theirs = notes.create("0")
    failed: list[BaseException] = []
    stop = threading.Event()

    def other_side() -> None:
        repository = NoteRepository(other)
        turn = 0
        try:
            while not stop.is_set():
                turn += 1
                with writing(other):
                    repository.update_body(theirs.id, str(turn))
        except BaseException as error:
            failed.append(error)

    side = threading.Thread(target=other_side)
    side.start()
    try:
        for turn in range(1, FORTH_AND_BACK):
            notes.update_body(mine.id, str(turn))
    finally:
        stop.set()
        side.join()
    assert failed == []
    assert notes.get(mine.id).body == str(FORTH_AND_BACK - 1)  # pyright: ignore[reportOptionalMemberAccess]
    app.close()
    other.close()


def test_what_is_written_together_is_committed_together(path: Path) -> None:
    app = open_database(path, KEY)
    with app:
        with app:
            app.execute("INSERT INTO local_counters VALUES ('a', 1)")
        assert app.in_transaction
    assert not app.in_transaction
    assert app.execute("SELECT value FROM local_counters WHERE key = 'a'").fetchall() == [(1,)]
    app.close()


def test_a_transaction_that_fails_leaves_nothing(path: Path) -> None:
    app = open_database(path, KEY)
    with pytest.raises(ValueError, match="no"), app:
        app.execute("INSERT INTO local_counters VALUES ('a', 1)")
        raise ValueError("no")
    assert not app.in_transaction
    assert app.execute("SELECT value FROM local_counters WHERE key = 'a'").fetchall() == []
    app.close()


def test_a_part_that_fails_leaves_the_rest(path: Path) -> None:
    app = open_database(path, KEY)
    with app:
        app.execute("INSERT INTO local_counters VALUES ('a', 1)")
        with pytest.raises(ValueError, match="no"), app:
            app.execute("INSERT INTO local_counters VALUES ('b', 1)")
            raise ValueError("no")
    found = app.execute("SELECT key FROM local_counters WHERE key IN ('a', 'b')").fetchall()
    assert found == [("a",)]
    app.close()


def test_a_look_at_a_backup_stays_a_look(path: Path) -> None:
    looked = open_database(path, KEY, read_only=True)
    assert looked.execute("SELECT count(*) FROM notes").fetchall() == [(0,)]
    with pytest.raises(apsw.ReadOnlyError):
        looked.execute("INSERT INTO local_counters VALUES ('a', 1)")
    looked.close()


def test_a_write_gives_up_if_its_turn_does_not_come(path: Path) -> None:
    holding, waiting = open_database(path, KEY), open_database(path, KEY, wait_ms=50)
    with holding, pytest.raises(apsw.BusyError), waiting:
        pytest.fail("got to write")
    assert not waiting.in_transaction
    holding.close()
    waiting.close()


def test_connections_to_other_files_do_not_wait(path: Path, tmp_path: Path) -> None:
    other_file = tmp_path / "other.db"
    open_store(other_file, KEY).close()
    one, other = open_database(path, KEY), open_database(other_file, KEY, wait_ms=50)
    with one, other:
        assert other.in_transaction
    one.close()
    other.close()


def test_a_turn_is_given_back_though_the_write_fails(path: Path) -> None:
    one, other = open_database(path, KEY), open_database(path, KEY, wait_ms=50)
    with pytest.raises(ValueError, match="no"), one:
        raise ValueError("no")
    with other:
        other.execute("INSERT INTO local_counters VALUES ('a', 1)")
    one.close()
    other.close()
