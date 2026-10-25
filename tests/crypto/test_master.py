"""The master password: a key pair in the key file B, beside the recovery key's."""

import secrets
from pathlib import Path

import pytest

from stickle.crypto.key_files import B, KeyFiles
from stickle.crypto.keyfile import WrongPasswordError
from stickle.crypto.master import SLOT, change_master, open_with_master, read_master, set_master
from stickle.crypto.recovery import generate, read_recovery, unwrap, write_recovery

KEY = secrets.token_bytes(32)
FAST = (1, 8192)
NOW, LATER = "2026-10-26T01:00:00.000Z", "2026-10-27T02:00:00.000Z"
MASTER = "correct horse battery staple"


def test_it_opens_the_notes_key_and_nothing_else_does(tmp_path: Path) -> None:
    files = KeyFiles(tmp_path)
    set_master(files, KEY, MASTER, NOW, FAST)

    slot = read_master(files)
    assert slot is not None and slot.set_at == NOW
    assert open_with_master(slot, MASTER) == KEY
    with pytest.raises(WrongPasswordError):
        open_with_master(slot, "wrong horse battery staple")


def test_it_and_the_recovery_key_keep_each_others_slots(tmp_path: Path) -> None:
    files = KeyFiles(tmp_path)
    write_recovery(files, KEY, generate(), NOW)
    set_master(files, KEY, MASTER, NOW, FAST)
    recovery_key = generate()
    write_recovery(files, KEY, recovery_key, LATER)  # a new recovery key later

    master = read_master(files)
    assert master is not None and open_with_master(master, MASTER) == KEY
    recovery = read_recovery(files)
    assert recovery is not None and unwrap(recovery, recovery_key) == KEY
    slots = files.load(B)
    assert slots is not None and SLOT in slots


def test_changing_it_keeps_the_pair_and_what_was_sealed(tmp_path: Path) -> None:
    files = KeyFiles(tmp_path)
    set_master(files, KEY, MASTER, NOW, FAST)
    before = read_master(files)

    change_master(files, MASTER, "a new phrase to remember well", LATER, FAST)

    after = read_master(files)
    assert before is not None and after is not None
    assert after.public == before.public and after.sealed == before.sealed
    assert after.set_at == LATER
    assert open_with_master(after, "a new phrase to remember well") == KEY
    with pytest.raises(WrongPasswordError):
        open_with_master(after, MASTER)


def test_it_is_changed_only_with_the_current_one(tmp_path: Path) -> None:
    files = KeyFiles(tmp_path)
    set_master(files, KEY, MASTER, NOW, FAST)
    before = files.path(B).read_bytes()

    with pytest.raises(WrongPasswordError):
        change_master(files, "not the master password", "a new phrase to remember", LATER, FAST)
    assert files.path(B).read_bytes() == before


def test_set_again_when_forgotten_it_is_a_new_pair(tmp_path: Path) -> None:
    files = KeyFiles(tmp_path)
    set_master(files, KEY, MASTER, NOW, FAST)
    before = read_master(files)

    set_master(files, KEY, "another phrase to remember", LATER, FAST)

    after = read_master(files)
    assert before is not None and after is not None and after.public != before.public
    with pytest.raises(WrongPasswordError):
        open_with_master(after, MASTER)


def test_neither_it_nor_the_key_is_written_anywhere(tmp_path: Path) -> None:
    set_master(KeyFiles(tmp_path), KEY, MASTER, NOW, FAST)

    written = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    assert MASTER.encode() not in written
    assert KEY not in written
