"""The secret notes' key in the key file C, and their text encrypted with it."""

import secrets
from pathlib import Path

import pytest

from stickle.crypto.key_files import B, C, KeyFiles
from stickle.crypto.keyfile import KeyFileError, WrongPasswordError
from stickle.crypto.master import read_master, set_master
from stickle.crypto.recovery import generate, write_recovery
from stickle.crypto.secret_notes import (
    SecretNoteError,
    change_password,
    create,
    has_secret_notes,
    keep_for_master,
    open_text,
    open_with_master,
    open_with_password,
    seal_text,
)

DATABASE_KEY = secrets.token_bytes(32)
FAST = (1, 8192)
NOW = "2026-10-26T09:00:00.000Z"
MASTER = "correct horse battery staple"
PASSWORD = "secret notes 1"


def with_master(folder: Path) -> KeyFiles:
    files = KeyFiles(folder)
    write_recovery(files, DATABASE_KEY, generate(), NOW)
    set_master(files, DATABASE_KEY, MASTER, NOW, FAST)
    return files


def master_public(files: KeyFiles) -> bytes:
    master = read_master(files)
    assert master is not None
    return master.public


def test_the_key_opens_with_the_password_and_the_master_password(tmp_path: Path) -> None:
    files = with_master(tmp_path)
    assert not has_secret_notes(files)

    key = create(files, PASSWORD, master_public(files), NOW, FAST)

    assert has_secret_notes(files)
    assert open_with_password(files, PASSWORD) == key
    master = read_master(files)
    assert master is not None and open_with_master(files, master, MASTER) == key
    with pytest.raises(WrongPasswordError):
        open_with_password(files, "not the password")
    with pytest.raises(WrongPasswordError):
        open_with_master(files, master, "not the master password either")


def test_neither_the_notes_key_nor_the_recovery_key_reaches_it(tmp_path: Path) -> None:
    # What someone at a computer left on could have: the database key, a recovery
    # key written down. Neither is in C, nor opens it.
    files = with_master(tmp_path)
    key = create(files, PASSWORD, master_public(files), NOW, FAST)

    written = b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    for secret in (key, PASSWORD.encode(), MASTER.encode()):
        assert secret not in written
    slots = files.load(C)
    assert slots is not None and set(slots) == {"secret_password", "master_password"}
    b_slots = files.load(B)
    assert b_slots is not None and "secret" not in str(b_slots)  # B knows nothing of it


def test_there_is_only_ever_one_key(tmp_path: Path) -> None:
    files = with_master(tmp_path)
    create(files, PASSWORD, master_public(files), NOW, FAST)
    with pytest.raises(KeyFileError):
        create(files, "another password", master_public(files), NOW, FAST)


def test_a_new_password_opens_the_same_key(tmp_path: Path) -> None:
    files = with_master(tmp_path)
    key = create(files, PASSWORD, master_public(files), NOW, FAST)

    change_password(files, key, "a new one 2", NOW, FAST)

    assert open_with_password(files, "a new one 2") == key
    with pytest.raises(WrongPasswordError):
        open_with_password(files, PASSWORD)


def test_a_master_password_set_anew_is_kept_for_once_the_key_is_known(tmp_path: Path) -> None:
    files = with_master(tmp_path)
    key = create(files, PASSWORD, master_public(files), NOW, FAST)
    set_master(files, DATABASE_KEY, "forgot it, a new one", NOW, FAST)  # a new pair
    renewed = read_master(files)
    assert renewed is not None
    with pytest.raises(KeyFileError):
        open_with_master(files, renewed, "forgot it, a new one")  # sealed for the old pair

    keep_for_master(files, key, renewed.public)

    assert open_with_master(files, renewed, "forgot it, a new one") == key


def test_a_notes_text_opens_on_that_note_only(tmp_path: Path) -> None:
    key = secrets.token_bytes(32)
    sealed = seal_text(key, "note-a", "비밀 내용 secret")

    assert open_text(key, "note-a", sealed) == "비밀 내용 secret"
    assert b"secret" not in sealed
    with pytest.raises(SecretNoteError):
        open_text(key, "note-b", sealed)  # moved onto another note
    with pytest.raises(SecretNoteError):
        open_text(secrets.token_bytes(32), "note-a", sealed)
    assert seal_text(key, "note-a", "same") != seal_text(key, "note-a", "same")  # fresh nonce
