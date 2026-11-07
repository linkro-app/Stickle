"""The vault two devices of 0.7 left (vault_sample_2), made once and kept as
files: a new device joining it takes in every note, record and device as they
were, whatever later version it is."""

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, FakeCloud
from test_vault_rules import plain_in
from vault_sample_2 import (
    BROUGHT,
    DATA_KEYS,
    DESK,
    EDITED,
    EMPTIED,
    FILES_SHA256,
    LAPTOP,
    NOTE,
    SAMPLE_2,
    START,
    TRASHED,
    VAULT,
)

from stickle.data.colors import ColorRepository
from stickle.data.imports import ImportRecords
from stickle.data.labels import LabelRepository
from stickle.data.notes import NoteRepository
from stickle.data.record_state import device_name
from stickle.data.schema import open_store
from stickle.sync.devices import About
from stickle.sync.engine import Place, sync_vault

NEW = "9b8a7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"


def sample_files() -> dict[str, bytes]:
    return {
        path.relative_to(SAMPLE_2).as_posix(): path.read_bytes()
        for path in SAMPLE_2.rglob("*")
        if path.is_file()
    }


def test_the_sample_is_the_one_made_then() -> None:
    files = sample_files()
    assert {path: hashlib.sha256(data).hexdigest() for path, data in files.items()} == FILES_SHA256


class Joined:
    def __init__(self, folder: Path) -> None:
        cloud = FakeCloud(DROPBOX)
        for path, data in sample_files().items():
            cloud.put_by_hand(path, data)
        self.db = open_store(folder / "new.db", bytes(32))
        self.notes = NoteRepository(self.db, clock=lambda: "2026-11-02T00:00:00.000Z")
        self.labels = LabelRepository(self.db)
        place = Place(cloud.target("new"), VAULT, DATA_KEYS, NEW)
        for _ in range(2):
            sync_vault(
                self.db, place, lambda: START + 86_400_000, self.notes, about=About("New", "Linux")
            )


@pytest.fixture(scope="module")
def joined(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Joined]:
    device = Joined(tmp_path_factory.mktemp("sample"))
    yield device
    device.db.close()


def test_its_notes_come_as_they_were(joined: Joined) -> None:
    notes = {note.id: note for note in joined.notes.all()}

    assert notes[NOTE].body == "A note in format 2\n한글 메모, 그대로 ✓\n⏰2026-12-01 09:00"
    assert notes[TRASHED].deleted
    assert EMPTIED not in notes
    assert notes[BROUGHT].body == "Brought in · 가져옴"
    edited = [note.body for note in notes.values() if "Edited on" in note.body]
    assert sorted(edited) == ["Edited on the desk · 책상에서", "Edited on the laptop · 노트북에서"]
    assert notes[EDITED].body == "Edited on the laptop · 노트북에서"  # edited later
    assert len(notes) == 6  # and the desk's own


def test_its_categories_marks_and_colours_alike_are_one(joined: Joined) -> None:
    categories = joined.labels.categories()
    assert [(c.name, c.color, c.icon) for c in categories] == [("Work · 업무", "blue", "briefcase")]
    marks = {mark.id: mark.name for mark in joined.labels.marks()}
    assert marks["todo"] == "Do · 할 것"
    assert sorted(name for name in marks.values() if name) == ["Do · 할 것", "Later · 나중에"]
    assert [color.hex for color in ColorRepository(joined.db).colors()] == ["#336699", "#993366"]

    work = categories[0].id
    labelled = {note.body: (note.label, note.marks) for note in joined.notes.all()}
    later = next(mark for mark, name in marks.items() if name == "Later · 나중에")
    assert labelled["A note in format 2\n한글 메모, 그대로 ✓\n⏰2026-12-01 09:00"] == (
        work,
        frozenset({later}),
    )
    assert labelled["Made on the desk · 책상"] == (work, frozenset({"todo"}))


def test_its_reminders_come_and_how_they_were_dealt_with(joined: Joined) -> None:
    rows = joined.db.execute(
        "SELECT kind, start_day, at_time, time_zone, repeat, weekdays, next_day, snoozed_until"
        " FROM reminders WHERE note_id = ? AND deleted_at IS NULL ORDER BY kind",
        (NOTE,),
    ).fetchall()

    assert rows == [
        ("note", "2026-11-20", "09:00", "Asia/Seoul", "weekly", "4", "2026-11-20",
         "2026-11-20T01:00:00.000Z"),
        ("text", "2026-12-01", "09:00", None, "none", "", "2026-12-01", None),
    ]  # fmt: skip


def test_the_note_brought_in_is_not_brought_in_again(joined: Joined) -> None:
    assert ImportRecords(joined.db).brought_in("sticky_notes", "sample-1", "Brought in · 가져옴")


def test_the_devices_are_known_by_name(joined: Joined) -> None:
    assert device_name(joined.db, LAPTOP) == "Laptop · 노트북"
    assert device_name(joined.db, DESK) == "Desk PC"


def test_nothing_in_it_is_in_the_clear() -> None:
    said = (
        "Work · 업무",
        "Later · 나중에",
        "Do · 할 것",
        "Laptop · 노트북",
        "Desk PC",
        "sticky_notes",
        "sample-1",
        "Asia/Seoul",
        "#993366",
        "Brought in",
    )
    assert plain_in(sample_files(), {f"said {i}": text for i, text in enumerate(said)}) == []
