"""The secret notes' key's record of the format 1 sample, made once and kept as
files, still opens and reads as it did: its password and the recovery key open
the same key, and a computer without secret notes takes it for its own."""

import base64
import hashlib
from pathlib import Path
from typing import cast

from vault_sample_3 import (
    DATA_KEYS,
    DEVICE,
    FILES_SHA256,
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
from stickle.sync.format import KEYS, RECORDS, item_at, item_path, open_envelope
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
