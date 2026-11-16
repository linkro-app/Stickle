"""The places, size and settings of the format 1 sample, made once and kept as
files, still read as they did, and a computer takes them: the note opens
where it was put for as many monitors, its places found by number (set on
another device), and the settings follow."""

import hashlib
from pathlib import Path

from vault_sample_4 import (
    DATA_KEYS,
    DATABASE_KEY,
    DESK,
    DEVICE,
    FILES_SHA256,
    LAPTOP,
    MOVED_AT,
    NOTE,
    NOTE_TEXT,
    SAMPLE_4,
    SENT,
    SETTINGS,
    SIZE,
    VAULT,
)

from stickle.core.hlc import Hlc
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.record_stamps import SETTING, VALUE
from stickle.data.schema import open_store
from stickle.data.settings import SETTINGS as ALL_SETTINGS
from stickle.data.settings import Settings
from stickle.sync import local, local_records
from stickle.sync.format import NOTES, RECORDS, item_path, open_envelope
from stickle.sync.note_file import NoteFile
from stickle.sync.record_file import RecordFile, record_id

OTHER = "0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"  # the computer taking them


def sample_files() -> dict[str, bytes]:
    return {
        path.relative_to(SAMPLE_4).as_posix(): path.read_bytes()
        for path in SAMPLE_4.rglob("*")
        if path.is_file()
    }


def the_note() -> NoteFile:
    data = sample_files()[item_path(NOTES, NOTE)]
    head, value = open_envelope(DATA_KEYS, data, VAULT, NOTES)
    assert head.item == NOTE
    return NoteFile.from_json(DATA_KEYS, value)


def the_setting(key: str) -> RecordFile:
    record = record_id(DATA_KEYS, SETTING, key)
    head, value = open_envelope(
        DATA_KEYS, sample_files()[item_path(RECORDS, record)], VAULT, RECORDS
    )
    assert head.item == record
    return RecordFile.from_json(value)


def test_the_sample_is_the_one_made_then() -> None:
    files = sample_files()

    assert {path: hashlib.sha256(data).hexdigest() for path, data in files.items()} == FILES_SHA256


def test_its_note_holds_its_places_and_size() -> None:
    fields = the_note().contents.fields

    assert fields["place:1"].value == {
        "monitor": 1,
        "name": "eDP-1",
        "setup": ["eDP-1"],
        "x": 0.125,
        "y": 0.25,
    }
    assert fields["place:2"].value == {
        "monitor": 2,
        "name": "DELL U2720Q",
        "setup": ["eDP-1", "DELL U2720Q"],
        "x": 0.5,
        "y": 0.375,
    }
    assert fields["size"].value == {"width": 320, "height": 240}
    for name in ("place:1", "place:2", "size"):
        assert (fields[name].hlc, fields[name].device, fields[name].sent) == (
            Hlc(MOVED_AT),
            DEVICE,
            SENT,
        )


def test_its_settings_read_as_written() -> None:
    for key, value in SETTINGS.items():
        record = the_setting(key)
        assert (record.kind, record.item) == (SETTING, key)
        field = record.contents.fields[VALUE]
        assert (field.value, field.hlc, field.device, field.sent) == (
            value,
            Hlc(MOVED_AT),
            DEVICE,
            SENT,
        )
        assert ALL_SETTINGS[key].scope == "shared"


def test_a_computer_takes_them(tmp_path: Path) -> None:
    db = open_store(tmp_path / "notes.db", DATABASE_KEY)
    try:
        notes = NoteRepository(db)
        local.take(db, notes, OTHER, NOTE, the_note(), local.here(db, notes, NOTE))
        for key in SETTINGS:
            at = local_records.here(db, SETTING, key)
            local_records.take(db, OTHER, the_setting(key), at, "2026-11-17T10:00:00.000Z")

        came = notes.get(NOTE)
        assert came is not None and came.body == NOTE_TEXT
        layouts = LayoutRepository(db)
        assert layouts.places(NOTE) == {1: LAPTOP, 2: DESK}
        assert layouts.size(NOTE) == SIZE
        assert layouts.elsewhere(NOTE) == {1, 2}  # set on another device: by number
        settings = Settings(db)
        assert {key: settings.get(ALL_SETTINGS[key]) for key in SETTINGS} == SETTINGS
    finally:
        db.close()
