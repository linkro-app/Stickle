"""The secret notes' key's record of the format 1 sample, made once and kept as
files, still opens and reads as it did: its password and the recovery key open
the same key, and a computer without secret notes takes it for its own. Its
secret note reads as it was sealed, opens with that key, and comes to a
computer locked, by its title, its reminder ringing there."""

import base64
import hashlib
from datetime import datetime
from pathlib import Path
from typing import cast

from vault_sample_3 import (
    DATA_KEYS,
    DATABASE_KEY,
    DEVICE,
    FILES_SHA256,
    NOTE,
    NOTE_REMINDER,
    NOTE_SENT,
    NOTE_TEXT,
    NOTE_TITLE,
    NUMBER,
    PASSWORD_TEXT,
    RECORD,
    RECOVERY_KEY_TEXT,
    SAMPLE_3,
    SECRET_KEY,
    VAULT,
)

from stickle.crypto import secret_notes
from stickle.crypto.key_files import KeyFiles
from stickle.crypto.keyfile import decode_slot, unwrap_with_password
from stickle.crypto.recovery import canonical
from stickle.crypto.sealed import open_pair, unseal
from stickle.crypto.vault_keys import RECOVERY_KEY, read_key_file
from stickle.data.notes import NoteRepository
from stickle.data.reminders import ReminderRepository
from stickle.data.schema import open_store
from stickle.sync import local
from stickle.sync.format import KEYS, NOTES, RECORDS, item_at, item_path, open_envelope
from stickle.sync.note_file import NoteFile, Secret
from stickle.sync.record_file import RecordFile
from stickle.sync.secret_key import CURRENT, KIND, PASSWORD, RECOVERY


def sample_files() -> dict[str, bytes]:
    return {
        path.relative_to(SAMPLE_3).as_posix(): path.read_bytes()
        for path in SAMPLE_3.rglob("*")
        if path.is_file()
    }


def the_record() -> RecordFile:
    data = sample_files()[item_path(RECORDS, RECORD)]
    head, value = open_envelope(DATA_KEYS, data, VAULT, RECORDS)
    assert head.item == RECORD
    return RecordFile.from_json(value)


def test_the_sample_is_the_one_made_then() -> None:
    files = sample_files()

    assert {path: hashlib.sha256(data).hexdigest() for path, data in files.items()} == FILES_SHA256


def test_its_record_reads_as_written() -> None:
    record = the_record()

    assert record.kind == KIND
    fields = record.contents.fields
    assert set(fields) == {CURRENT, PASSWORD + NUMBER, RECOVERY + NUMBER}
    assert fields[CURRENT].value == NUMBER
    assert all(each.device == DEVICE for each in fields.values())
    password = cast(dict[str, object], fields[PASSWORD + NUMBER].value)
    assert password[secret_notes.HOW] == secret_notes.RESET


def test_its_password_and_the_recovery_key_open_the_same_key() -> None:
    fields = the_record().contents.fields
    password = decode_slot(fields[PASSWORD + NUMBER].value)
    assert unwrap_with_password(password, PASSWORD_TEXT, secret_notes.PASSWORD_SLOT) == SECRET_KEY
    (path,) = [p for p in sample_files() if (at := item_at(p)) is not None and at[0] == KEYS]
    receiver = read_key_file(cast(tuple[str, str], item_at(path))[1], sample_files()[path])
    assert receiver.kind == RECOVERY_KEY and receiver.slot is not None
    pair_secret = open_pair(receiver.slot, canonical(RECOVERY_KEY_TEXT), RECOVERY_KEY)
    sealed = cast(dict[str, str], fields[RECOVERY + NUMBER].value)
    assert base64.b64decode(sealed["public"]) == receiver.slot.public
    assert unseal(pair_secret, base64.b64decode(sealed["sealed"])) == SECRET_KEY


def test_a_computer_without_secret_notes_takes_it_for_its_own(tmp_path: Path) -> None:
    fields = the_record().contents.fields
    files = KeyFiles(tmp_path)

    taken = secret_notes.take(
        files,
        NUMBER,
        cast(dict[str, object], fields[PASSWORD + NUMBER].value),
        cast(dict[str, object], fields[RECOVERY + NUMBER].value),
        None,
    )

    assert taken
    assert secret_notes.open_with_password(files, PASSWORD_TEXT) == SECRET_KEY


def the_note() -> NoteFile:
    data = sample_files()[item_path(NOTES, NOTE)]
    head, value = open_envelope(DATA_KEYS, data, VAULT, NOTES)
    assert head.item == NOTE
    return NoteFile.from_json(DATA_KEYS, value)


def test_its_secret_note_reads_as_it_was_sealed_and_opens_with_the_key() -> None:
    note = the_note()

    text = note.body.text
    assert isinstance(text, Secret)
    assert (text.bound, text.key, text.reminders) == (NOTE, NUMBER, (NOTE_REMINDER,))
    assert note.body.sent == NOTE_SENT and note.body.device == DEVICE
    assert note.contents.fields["secret_title"].value == NOTE_TITLE
    assert secret_notes.open_text(SECRET_KEY, text.bound, text.sealed) == NOTE_TEXT


def test_its_secret_note_comes_to_a_computer_locked(tmp_path: Path) -> None:
    db = open_store(tmp_path / "notes.db", DATABASE_KEY)
    try:
        notes = NoteRepository(db)
        other = "0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"

        local.take(db, notes, other, NOTE, the_note(), local.here(db, notes, NOTE))

        came = notes.get(NOTE)
        sealed = None if came is None else came.sealed_text
        assert came is not None and sealed is not None
        assert (came.body, came.secret_title, came.key_number) == ("", NOTE_TITLE, NUMBER)
        assert secret_notes.open_sealed(SECRET_KEY, NOTE, sealed) == NOTE_TEXT
        assert ReminderRepository(db).text_times(NOTE) == [datetime(2026, 11, 20, 18, 30)]
    finally:
        db.close()
