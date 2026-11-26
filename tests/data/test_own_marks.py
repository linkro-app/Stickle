"""The marks of this device's own file kept: what tells it from a copy of
its disk, in a vault gone back to an earlier day too."""

from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest

from stickle.data import sync_state
from stickle.data.schema import open_store

KEY = bytes(range(32))


@pytest.fixture
def db(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


def test_the_last_mark_of_a_day_stays_after_more_writes_than_are_kept(
    db: apsw.Connection,
) -> None:
    sync_state.add_own_mark(db, "v", "first-day-early", 100)
    sync_state.add_own_mark(db, "v", "first-day-last", 100)
    for at in range(sync_state.MARKS_KEPT + 50):  # a busy day after
        sync_state.add_own_mark(db, "v", f"busy-{at}", 101)

    marks = sync_state.own_marks(db, "v")

    assert marks[0] == f"busy-{sync_state.MARKS_KEPT + 49}"  # the latest first
    assert "first-day-last" in marks  # a vault gone back to that day shows it
    assert "first-day-early" not in marks
    assert len(marks) == sync_state.MARKS_KEPT + 1


def test_a_day_longer_ago_than_is_kept_goes_once_not_among_the_latest(
    db: apsw.Connection,
) -> None:
    sync_state.add_own_mark(db, "v", "long-ago", 1)
    for at in range(sync_state.MARKS_KEPT):
        sync_state.add_own_mark(db, "v", f"later-{at}", 2)
    assert "long-ago" in sync_state.own_marks(db, "v")  # within the days kept

    sync_state.add_own_mark(db, "v", "today", 1 + sync_state.DAYS_KEPT)

    assert "long-ago" not in sync_state.own_marks(db, "v")


def test_marks_kept_before_their_days_were_still_count_among_the_latest(
    db: apsw.Connection,
) -> None:
    db.execute("INSERT INTO sync_vaults (vault, own_mark) VALUES ('v', 'older newer')")

    sync_state.add_own_mark(db, "v", "now", 5)

    assert sync_state.own_marks(db, "v") == ["now", "older", "newer"]
