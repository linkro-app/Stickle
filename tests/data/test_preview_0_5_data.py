"""Notes and key files left by the 0.5.1 preview open in this version: the key
files take their new names with a second copy, and the recovery key written
then still opens the notes until a new one is made."""

import json
import shutil
from pathlib import Path

import pytest
from preview_fixture_0_5 import FIXTURE, NOTES, PASSWORD, RECOVERY_KEY, SCHEMA_VERSION, STORE_KEY

from stickle.crypto.key_files import A, B, KeyFiles
from stickle.crypto.keyfile import WARNING
from stickle.crypto.recovery import WrongRecoveryKeyError, read_recovery
from stickle.crypto.sealed import SealedSlot
from stickle.data.database import open_database
from stickle.data.key_copies import KeyCopies
from stickle.data.notes import NoteRepository
from stickle.data.schema import latest_version, open_store, schema_version
from stickle.platform.credentials import CredentialStore
from stickle.unlock import Blocked, NeedPassword, Unlock

NOW = "2026-10-26T09:00:00.000Z"


def no_store() -> CredentialStore:
    raise AssertionError("password mode reads no credential store")


@pytest.fixture
def password_folder(tmp_path: Path) -> Path:
    """The preview's files, copied: those in the repository are never opened for writing."""
    target = tmp_path / "password"
    shutil.copytree(FIXTURE / "password", target)
    return target


@pytest.fixture
def store_folder(tmp_path: Path) -> Path:
    target = tmp_path / "store"
    shutil.copytree(FIXTURE / "store", target)
    return target


def bodies(path: Path, key: bytes) -> list[str]:
    connection = open_store(path, key)
    try:
        assert schema_version(connection) == latest_version()
        return sorted(note.body for note in NoteRepository(connection).all())
    finally:
        connection.close()


def test_the_fixture_is_as_0_5_left_it() -> None:
    names = sorted(p.name for p in (FIXTURE / "password").iterdir())
    assert names == ["keys.json", "notes.db", "recovery.json"]
    connection = open_database(FIXTURE / "store" / "notes.db", STORE_KEY)
    assert schema_version(connection) == SCHEMA_VERSION
    connection.close()


def test_the_password_opens_the_notes_and_the_files_take_their_new_names(
    password_folder: Path,
) -> None:
    unlock = Unlock(password_folder, no_store, clock=lambda: NOW)

    assert unlock.outcome() == NeedPassword(create=False)
    key = unlock.enter_password(PASSWORD)
    assert bodies(unlock.database, key) == sorted(NOTES)
    left = sorted(p.name for p in password_folder.iterdir() if p.is_file())
    assert left == [A, B, "notes.db"]
    files = KeyFiles(password_folder)
    for name in (A, B):
        assert files.second_path(name).read_bytes() == files.path(name).read_bytes()
        assert json.loads(files.path(name).read_text(encoding="utf-8"))["warning"] == WARNING


def test_the_recovery_key_from_0_5_opens_them_until_a_new_one_is_made(
    password_folder: Path,
) -> None:
    unlock = Unlock(password_folder, no_store, clock=lambda: NOW)
    key = unlock.open_with_recovery_key(RECOVERY_KEY)
    assert key == unlock.enter_password(PASSWORD)

    new = unlock.make_recovery_key(key)

    slot = read_recovery(unlock.files)
    assert isinstance(slot, SealedSlot) and slot.set_at == NOW
    assert unlock.open_with_recovery_key(new) == key
    with pytest.raises(WrongRecoveryKeyError):
        unlock.open_with_recovery_key(RECOVERY_KEY)


def test_notes_whose_key_was_in_the_store_open_with_the_recovery_key(store_folder: Path) -> None:
    # Brought as portable notes: the store's key is not here, the recovery key is.
    unlock = Unlock(store_folder, no_store, portable=True)

    assert unlock.outcome() == Blocked("key_missing")
    assert unlock.open_with_recovery_key(RECOVERY_KEY) == STORE_KEY
    assert bodies(unlock.database, STORE_KEY) == sorted(NOTES)


def test_once_open_the_notes_keep_the_recovery_file_too(password_folder: Path) -> None:
    unlock = Unlock(password_folder, no_store)
    connection = open_store(unlock.database, unlock.enter_password(PASSWORD))
    try:
        unlock.keep_copies(connection)
        assert KeyCopies(connection).get(B) == unlock.files.path(B).read_bytes()
        assert KeyCopies(connection).get(A) is None  # needed to open them: never in them
    finally:
        connection.close()
