"""The secret notes' key in the key file C, and their text encrypted with it."""

import base64
import secrets
from pathlib import Path
from typing import cast

import pytest

from stickle.crypto import secret_notes
from stickle.crypto.key_files import B, C, KeyFiles
from stickle.crypto.keyfile import KeyFileError, WrongPasswordError, encode_slot, wrap_with_password
from stickle.crypto.master import read_master, set_master
from stickle.crypto.recovery import (
    WrongRecoveryKeyError,
    generate,
    make_recovery_slot,
    open_recovery_pair,
    read_recovery,
    save_recovery_slot,
    write_recovery,
)
from stickle.crypto.sealed import SealedSlot, open_pair, seal
from stickle.crypto.secret_notes import (
    RECOVERY_SLOT,
    NotKeptForRecoveryKeyError,
    SecretNoteError,
    change_password,
    create,
    has_secret_notes,
    keep_for_recovery_key,
    open_text,
    open_with_password,
    open_with_recovery_key,
    recovery_public,
    seal_text,
)

DATABASE_KEY = secrets.token_bytes(32)
FAST = (1, 8192)
NOW = "2026-10-26T09:00:00.000Z"
MASTER = "correct horse battery staple"
PASSWORD = "secret notes 1"
RECOVERY_KEY = generate()


def with_recovery_key(folder: Path, recovery_key: str = RECOVERY_KEY) -> KeyFiles:
    files = KeyFiles(folder)
    write_recovery(files, DATABASE_KEY, recovery_key, NOW)
    set_master(files, DATABASE_KEY, MASTER, NOW, FAST)
    return files


def pair(files: KeyFiles) -> SealedSlot:
    slot = read_recovery(files)
    assert isinstance(slot, SealedSlot)
    return slot


def open_with(files: KeyFiles, recovery_key: str) -> bytes:
    slot = pair(files)
    return open_with_recovery_key(files, open_recovery_pair(slot, recovery_key), slot.public)


def test_the_key_opens_with_the_password_and_the_recovery_key(tmp_path: Path) -> None:
    files = with_recovery_key(tmp_path)
    assert not has_secret_notes(files)

    key = create(files, PASSWORD, pair(files).public, NOW, FAST)

    assert has_secret_notes(files)
    assert open_with_password(files, PASSWORD) == key
    assert open_with(files, RECOVERY_KEY) == key
    with pytest.raises(WrongPasswordError):
        open_with_password(files, "not the password")
    with pytest.raises(WrongRecoveryKeyError):
        open_with(files, generate())


def test_neither_the_notes_key_nor_the_master_password_reaches_it(tmp_path: Path) -> None:
    # What someone at a computer left on could have: the database key, and a
    # master password set there. Neither is in C, nor opens it.
    files = with_recovery_key(tmp_path)
    key = create(files, PASSWORD, pair(files).public, NOW, FAST)

    written = b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    for secret in (key, PASSWORD.encode(), MASTER.encode(), RECOVERY_KEY.encode()):
        assert secret not in written
    slots = files.load(C)
    assert slots is not None and set(slots) == {"key_number", "secret_password", "recovery_key"}
    master = read_master(files)
    assert master is not None
    with pytest.raises(KeyFileError):  # not sealed for the master password's pair
        open_with_recovery_key(files, open_pair(master, MASTER, "master_password"), master.public)
    b_slots = files.load(B)
    assert b_slots is not None and "secret" not in str(b_slots)  # B knows nothing of it


def test_there_is_only_ever_one_key(tmp_path: Path) -> None:
    files = with_recovery_key(tmp_path)
    create(files, PASSWORD, pair(files).public, NOW, FAST)
    with pytest.raises(KeyFileError):
        create(files, "another password", pair(files).public, NOW, FAST)


def test_a_key_file_gone_while_running_is_never_made_anew(tmp_path: Path) -> None:
    # Taken away by a clean-up tool or a sync while Stickle runs: a new key
    # would have been written over its other copies, and the notes lost.
    files = with_recovery_key(tmp_path)
    key = create(files, PASSWORD, pair(files).public, NOW, FAST)
    files.path(C).unlink()

    assert has_secret_notes(files)
    with pytest.raises(KeyFileError):
        create(files, "another password", pair(files).public, NOW, FAST)
    assert open_with_password(files, PASSWORD) == key
    assert files.path(C).read_bytes() == files.second_path(C).read_bytes()


def test_a_damaged_key_file_still_counts_as_there(tmp_path: Path) -> None:
    files = with_recovery_key(tmp_path)
    create(files, PASSWORD, pair(files).public, NOW, FAST)
    files.path(C).write_bytes(b"not a key file")
    files.second_path(C).write_bytes(b"not a key file")

    assert has_secret_notes(files)
    with pytest.raises(KeyFileError):
        create(files, "another password", pair(files).public, NOW, FAST)


def test_a_new_password_opens_the_same_key_and_the_old_one_no_longer(tmp_path: Path) -> None:
    files = with_recovery_key(tmp_path)
    key = create(files, PASSWORD, pair(files).public, NOW, FAST)

    change_password(files, key, "a new one 2", NOW, FAST)

    assert open_with_password(files, "a new one 2") == key
    with pytest.raises(WrongPasswordError):
        open_with_password(files, PASSWORD)
    assert open_with(files, RECOVERY_KEY) == key  # the recovery key, as before


def test_a_new_recovery_key_opens_them_once_kept_for(tmp_path: Path) -> None:
    files = with_recovery_key(tmp_path)
    key = create(files, PASSWORD, pair(files).public, NOW, FAST)
    new_key = generate()
    save_recovery_slot(files, make_recovery_slot(DATABASE_KEY, new_key, NOW))  # a new pair
    assert recovery_public(files) != pair(files).public
    with pytest.raises(NotKeptForRecoveryKeyError):
        open_with(files, new_key)  # sealed for the old pair

    keep_for_recovery_key(files, key, pair(files).public)

    assert open_with(files, new_key) == key
    assert open_with_password(files, PASSWORD) == key


def test_sealed_for_the_master_password_by_an_earlier_build_it_is_dropped(
    tmp_path: Path,
) -> None:
    # Before the master password was taken off them, C held the key sealed for
    # it. Kept for the recovery key, C no longer has that seal.
    files = with_recovery_key(tmp_path)
    master = read_master(files)
    assert master is not None
    created = create(files, PASSWORD, pair(files).public, NOW, FAST)
    slots = dict(files.load(C) or {})
    del slots["recovery_key"]
    slots["master_password"] = {
        "public": base64.b64encode(master.public).decode("ascii"),
        "sealed": base64.b64encode(seal(master.public, created)).decode("ascii"),
    }
    files.save(C, slots)
    assert recovery_public(files) is None
    with pytest.raises(NotKeptForRecoveryKeyError):
        open_with(files, RECOVERY_KEY)

    keep_for_recovery_key(files, created, pair(files).public)

    slots_now = files.load(C)
    assert slots_now is not None
    assert set(slots_now) == {"key_number", "secret_password", "recovery_key"}
    assert open_with(files, RECOVERY_KEY) == created


def test_c_is_never_written_from_nothing(tmp_path: Path) -> None:
    # Only a C that is there is written again: one made from a single slot would
    # count as secret notes turned on, with no password to open them.
    files = with_recovery_key(tmp_path)
    key = secrets.token_bytes(32)
    with pytest.raises(KeyFileError):
        keep_for_recovery_key(files, key, pair(files).public)
    with pytest.raises(KeyFileError):
        change_password(files, key, PASSWORD, NOW, FAST)
    assert not has_secret_notes(files)


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


# Synced: the key's number, how its password was set, and taking the others' slots


def test_a_key_made_now_has_a_number_and_one_made_by_0_6_gets_one_once(tmp_path: Path) -> None:
    files = with_recovery_key(tmp_path)
    create(files, PASSWORD, pair(files).public, NOW, FAST)
    made = secret_notes.key_number(files.load(C))
    assert made is not None
    assert not secret_notes.number_the_key(files)  # numbered already

    slots = dict(files.load(C) or {})
    del slots[secret_notes.KEY_NUMBER]  # as 0.6 made it
    files.save(C, slots)
    assert secret_notes.number_the_key(files)
    numbered = secret_notes.key_number(files.load(C))
    assert numbered is not None and numbered != made
    assert not secret_notes.number_the_key(files)
    assert secret_notes.key_number(files.load(C)) == numbered


def test_no_key_and_a_damaged_one_are_never_numbered(tmp_path: Path) -> None:
    files = with_recovery_key(tmp_path)
    assert not secret_notes.number_the_key(files)
    assert files.raw(C) is None
    files.path(C).write_bytes(b"{damaged")
    files.second_path(C).parent.mkdir(exist_ok=True)
    files.second_path(C).write_bytes(b"{damaged")
    with pytest.raises(KeyFileError):
        secret_notes.number_the_key(files)
    assert files.path(C).read_bytes() == b"{damaged"


def test_what_0_6_does_not_know_stays_as_it_writes_c_again(tmp_path: Path) -> None:
    """Its number, how its password was set, a slot to come: kept by the ways
    0.6 writes C too (a new password, sealed for a new recovery key, a password
    set anew with the recovery key: the same code as 0.6's)."""
    files = with_recovery_key(tmp_path)
    key = create(files, PASSWORD, pair(files).public, NOW, FAST)
    slots = dict(files.load(C) or {})
    slots["old_keys"] = {"a-number": "a key, locked"}  # one to come
    files.save(C, slots)
    number = secret_notes.key_number(slots)

    change_password(files, key, "second", NOW, FAST, secret_notes.RESET)
    written = cast(dict[str, object], (files.load(C) or {})[secret_notes.PASSWORD_SLOT])
    assert written["how"] == "reset"
    keep_for_recovery_key(files, key, pair(files).public)
    change_password(files, key, "third", NOW, FAST)

    after = files.load(C) or {}
    assert secret_notes.key_number(after) == number
    assert after["old_keys"] == {"a-number": "a key, locked"}
    assert open_with_password(files, "third") == key


def changed_elsewhere(key: bytes, password: str) -> dict[str, object]:
    """A password slot as another computer sets it, for the same key."""
    slot = wrap_with_password(
        key, password, *FAST, secret_notes.PASSWORD_SLOT, "2026-11-15T09:00:00.000Z"
    )
    written: dict[str, object] = {**encode_slot(slot), "how": "changed"}
    return written


def test_the_others_password_slot_is_taken_for_the_same_key_as_seen(tmp_path: Path) -> None:
    files = with_recovery_key(tmp_path)
    key = create(files, PASSWORD, pair(files).public, NOW, FAST)
    seen = secret_notes.seen_digest(files.load(C))
    number = secret_notes.key_number(files.load(C))
    assert number is not None

    assert secret_notes.take(files, number, changed_elsewhere(key, "theirs"), None, seen)

    assert open_with_password(files, "theirs") == key
    assert open_with(files, RECOVERY_KEY) == key  # what is sealed for it stays
    later = secret_notes.seen_digest(files.load(C))
    assert not secret_notes.take(files, number, changed_elsewhere(key, "again"), None, seen)
    assert not secret_notes.take(files, "another-number", changed_elsewhere(key, "x"), None, later)
    assert open_with_password(files, "theirs") == key


def test_without_c_the_others_make_it(tmp_path: Path) -> None:
    files = with_recovery_key(tmp_path)
    key = secrets.token_bytes(32)
    public = pair(files).public
    recovery = {
        "public": base64.b64encode(public).decode(),
        "sealed": base64.b64encode(seal(public, key)).decode(),
    }

    assert secret_notes.take(files, "a-number", changed_elsewhere(key, "theirs"), recovery, None)

    assert has_secret_notes(files)
    assert open_with_password(files, "theirs") == key
    assert open_with(files, RECOVERY_KEY) == key
    assert secret_notes.key_number(files.load(C)) is None  # not a uuid: kept, but not one
    with pytest.raises(KeyFileError):
        secret_notes.take(
            files, "x", {"kdf": "nothing"}, None, secret_notes.seen_digest(files.load(C))
        )


def test_a_pair_kept_aside_stays_only_while_what_is_taken_is_sealed_for_it(tmp_path: Path) -> None:
    files = with_recovery_key(tmp_path)
    key = create(files, PASSWORD, pair(files).public, NOW, FAST)
    secret_notes.keep_pair_aside(files, pair(files), before_sync=True)
    number = secret_notes.key_number(files.load(C))
    assert number is not None
    sealed = cast(dict[str, object], (files.load(C) or {})[RECOVERY_SLOT])
    same = {name: value for name, value in sealed.items() if name in ("public", "sealed")}
    seen = secret_notes.seen_digest(files.load(C))

    assert secret_notes.take(files, number, None, same, seen)  # sealed for the same pair
    assert secret_notes.pair_aside(files) is True

    other = generate()
    other_pair = make_recovery_slot(DATABASE_KEY, other, NOW)
    theirs = {
        "public": base64.b64encode(other_pair.public).decode(),
        "sealed": base64.b64encode(seal(other_pair.public, key)).decode(),
    }
    assert secret_notes.take(files, number, None, theirs, secret_notes.seen_digest(files.load(C)))
    assert secret_notes.pair_aside(files) is None
