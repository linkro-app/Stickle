"""Storing where notes are on screen, and their size."""

import json
import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest

from stickle.core.layout import Place, Size
from stickle.data import schema
from stickle.data.database import KEY_BYTES
from stickle.data.layouts import LayoutRepository
from stickle.data.schema import open_store

KEY = secrets.token_bytes(KEY_BYTES)
PLACE = Place(2, "DELL U2720Q: HDMI-1", 0.25, 0.5, ("ELDTV", "DELL U2720Q: HDMI-1"))
ALONE = Place(1, "ELDTV", 0.1, 0.1, ("ELDTV",))
SIZE = Size(260, 240)


def ticking() -> Iterator[str]:
    for minute in range(60):
        yield f"2026-11-06T10:{minute:02d}:00.000Z"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


def times(db: apsw.Connection) -> dict[str, str]:
    rows = db.execute("SELECT config_key, updated_at FROM note_layouts").fetchall()
    (size,) = db.execute("SELECT updated_at FROM note_sizes").fetchall()
    return {str(key): str(at) for key, at in rows} | {"size": str(size[0])}


def test_places_and_the_size_are_stored_and_read_back(db: apsw.Connection) -> None:
    layouts = LayoutRepository(db)

    layouts.save("note", SIZE, (2, PLACE))
    layouts.save("note", SIZE, (1, ALONE))

    assert layouts.places("note") == {2: PLACE, 1: ALONE}
    assert layouts.size("note") == SIZE
    assert layouts.places("other") == {}
    assert layouts.size("other") is None


def test_a_size_alone_is_kept_where_no_place_can_be(db: apsw.Connection) -> None:
    layouts = LayoutRepository(db)

    layouts.save("note", Size(400, 300))

    assert layouts.places("note") == {}
    assert layouts.size("note") == Size(400, 300)


def test_saving_again_replaces_only_the_place_for_as_many_monitors(db: apsw.Connection) -> None:
    layouts = LayoutRepository(db)
    layouts.save("note", SIZE, (2, PLACE))
    layouts.save("note", SIZE, (1, ALONE))
    moved = Place(1, "ELDTV", 0.7, 0.2, ("ELDTV",))

    layouts.save("note", Size(300, 200), (1, moved))

    assert layouts.places("note") == {2: PLACE, 1: moved}
    assert layouts.size("note") == Size(300, 200)


def test_only_what_changed_takes_a_new_time(db: apsw.Connection) -> None:
    clock = ticking()
    layouts = LayoutRepository(db, lambda: next(clock))
    layouts.save("note", SIZE, (2, PLACE))
    layouts.save("note", SIZE, (1, ALONE))
    first = times(db)

    layouts.save("note", SIZE, (1, Place(1, "ELDTV", 0.3, 0.3, ("ELDTV",))))  # moved
    moved = times(db)
    layouts.save("note", Size(500, 400), (1, Place(1, "ELDTV", 0.3, 0.3, ("ELDTV",))))  # resized
    resized = times(db)

    assert moved["1"] != first["1"]
    assert (moved["2"], moved["size"]) == (first["2"], first["size"])
    assert resized["size"] != moved["size"]
    assert (resized["1"], resized["2"]) == (moved["1"], moved["2"])


@pytest.mark.parametrize(
    ("monitor_key", "values"),
    [
        ("not json", (0.1, 0.1)),
        ('{"number": "2", "name": "", "setup": []}', (0.1, 0.1)),
        ('{"number": true, "name": "", "setup": []}', (0.1, 0.1)),
        ('{"number": 1, "name": "", "setup": [3]}', (0.1, 0.1)),
        ('{"number": 1, "name": "", "setup": []}', ("x", 0.1)),
        ('{"number": 1, "name": "", "setup": []}', (1e308 * 10, 0.1)),
        ('{"number": 0, "name": "", "setup": []}', (0.1, 0.1)),
    ],
)
def test_rows_that_make_no_sense_are_ignored(
    db: apsw.Connection, monitor_key: str, values: tuple[object, ...]
) -> None:
    db.execute(
        "INSERT INTO note_layouts VALUES ('note', '1', ?, ?, ?, '2026-11-06')",
        (monitor_key, *values),  # pyright: ignore[reportArgumentType]
    )

    assert LayoutRepository(db).places("note") == {}


@pytest.mark.parametrize("config_key", ["main", "spare", "from-a-newer-version", "0", "-1", "²"])
def test_unknown_kinds_of_place_are_ignored(db: apsw.Connection, config_key: str) -> None:
    layouts = LayoutRepository(db)
    layouts.save("note", SIZE, (2, PLACE))
    db.execute("UPDATE note_layouts SET config_key = ?", (config_key,))

    assert layouts.places("note") == {}


@pytest.mark.parametrize("values", [(-20, 240), (260, 10**9), ("wide", 240), (260.5, 240)])
def test_sizes_that_make_no_sense_are_ignored(
    db: apsw.Connection, values: tuple[object, object]
) -> None:
    db.execute(
        "INSERT INTO note_sizes VALUES ('note', ?, ?, '2026-11-06')",
        values,  # pyright: ignore[reportArgumentType]
    )

    assert LayoutRepository(db).size("note") is None


# From a main and a spare place (until 0.6) to a place for each number of monitors


def old_key(number: int, name: str, setup: list[str]) -> str:
    return json.dumps({"number": number, "name": name, "setup": setup})


def upgraded(
    path: Path, rows: list[tuple[object, ...]], monkeypatch: pytest.MonkeyPatch
) -> LayoutRepository:
    """The rows written as 0.6 did, then the database opened by this version."""
    monkeypatch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:11])
    connection = open_store(path, KEY)
    with connection:
        connection.executemany(
            "INSERT INTO note_layouts VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-31T09:00:00.000Z')",
            rows,  # pyright: ignore[reportArgumentType]
        )
    connection.close()
    monkeypatch.undo()
    return LayoutRepository(open_store(path, KEY))


def test_main_and_spare_go_under_the_number_of_monitors_they_were_stored_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layouts = upgraded(
        tmp_path / "notes.db",
        [
            ("both", "main", old_key(2, "LG", ["DELL", "LG"]), 0.6, 0.1, 280, 200),
            ("both", "spare", old_key(1, "DELL", ["DELL"]), 0.4, 0.5, 280, 200),
            ("three", "main", old_key(3, "B", ["P", "A", "B"]), 0.2, 0.2, 300, 300),
        ],
        monkeypatch,
    )

    assert layouts.places("both") == {
        2: Place(2, "LG", 0.6, 0.1, ("DELL", "LG")),
        1: Place(1, "DELL", 0.4, 0.5, ("DELL",)),
    }
    assert layouts.size("both") == Size(280, 200)
    assert layouts.places("three") == {3: Place(3, "B", 0.2, 0.2, ("P", "A", "B"))}


def test_two_stored_with_as_many_monitors_keep_the_main_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layouts = upgraded(
        tmp_path / "notes.db",
        [
            ("note", "main", old_key(2, "LG", ["DELL", "LG"]), 0.3, 0.3, 260, 220),
            ("note", "spare", old_key(1, "DELL", ["DELL", "BenQ"]), 0.7, 0.2, 260, 220),
        ],
        monkeypatch,
    )

    assert layouts.places("note") == {2: Place(2, "LG", 0.3, 0.3, ("DELL", "LG"))}


def test_without_the_names_of_the_monitors_its_number_is_how_many_there_were_at_least(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layouts = upgraded(
        tmp_path / "notes.db",
        [("note", "main", old_key(2, "", []), 0.5, 0.5, 260, 240)],
        monkeypatch,
    )

    assert layouts.places("note") == {2: Place(2, "", 0.5, 0.5, ())}


def test_a_spare_place_alone_keeps_its_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layouts = upgraded(
        tmp_path / "notes.db",
        [
            ("note", "main", "not json", 0.5, 0.5, 900, 900),
            ("note", "spare", old_key(1, "DELL", ["DELL"]), 0.4, 0.5, 280, 200),
        ],
        monkeypatch,
    )

    assert layouts.places("note") == {1: Place(1, "DELL", 0.4, 0.5, ("DELL",))}
    assert layouts.size("note") == Size(280, 200)


@pytest.mark.parametrize(
    "monitor_key",
    ["not json", "[1, 2]", '{"number": "2", "name": "", "setup": []}', '"text"', ""],
)
def test_places_no_version_could_read_go_without_stopping_the_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, monitor_key: str
) -> None:
    layouts = upgraded(
        tmp_path / "notes.db",
        [
            ("broken", "main", monitor_key, 0.5, 0.5, 250, 250),
            ("fine", "main", old_key(1, "DELL", ["DELL"]), 0.1, 0.1, 300, 340),
        ],
        monkeypatch,
    )

    assert layouts.places("broken") == {}
    assert layouts.size("broken") is None
    assert layouts.places("fine") == {1: Place(1, "DELL", 0.1, 0.1, ("DELL",))}
