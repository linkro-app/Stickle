"""Secret notes and the recovery key: the recovery key opens them too, a new one
made with their key opens them at once, and no step that stops halfway leaves
them out of reach of both their password and a recovery key."""

import secrets
from pathlib import Path

import pytest
from test_unlock import FAST, Store

from stickle.crypto import recovery
from stickle.crypto.key_files import B, C
from stickle.crypto.keyfile import KeyFileError, WrongPasswordError, encode_slot, wrap_with_password
from stickle.crypto.recovery import (
    OLD_SLOT,
    RecoveryKeyTypoError,
    WrongRecoveryKeyError,
    canonical,
    generate,
)
from stickle.crypto.secret_notes import NotKeptForRecoveryKeyError
from stickle.unlock import Unlock

KEY = secrets.token_bytes(32)
SECRET = "secret notes 1"


def unlocked(folder: Path) -> Unlock:
    unlock = Unlock(folder, Store("has_key"), FAST, master_strength=FAST)
    unlock.opened_key = KEY
    return unlock


def with_old_recovery_key(unlock: Unlock) -> str:
    """A recovery key as Stickle made them until 0.5: the database key wrapped with it."""
    old = generate()
    wrapped = wrap_with_password(KEY, canonical(old), 2, 8192, slot_name=OLD_SLOT)
    unlock.files.save(B, {OLD_SLOT: encode_slot(wrapped)})
    return old


def test_turned_on_they_open_with_the_recovery_key_too(tmp_path: Path) -> None:
    unlock = unlocked(tmp_path)
    recovery_key = unlock.make_recovery_key(KEY)

    key = unlock.turn_on_secret_notes(SECRET)

    assert unlock.open_secret_notes(SECRET) == key
    assert unlock.open_secret_notes_with_recovery_key(recovery_key) == key
    assert unlock.secret_notes_kept_for_recovery_key
    with pytest.raises(WrongRecoveryKeyError):
        unlock.open_secret_notes_with_recovery_key(generate())
    with pytest.raises(RecoveryKeyTypoError):
        unlock.open_secret_notes_with_recovery_key(recovery_key[:-1])


def test_the_master_password_does_not_open_them(tmp_path: Path) -> None:
    unlock = unlocked(tmp_path)
    unlock.make_recovery_key(KEY)
    unlock.set_master_password(KEY, "correct horse battery staple")
    unlock.turn_on_secret_notes(SECRET)

    assert not hasattr(unlock, "open_secret_notes_with_master")
    with pytest.raises(WrongPasswordError):
        unlock.open_secret_notes("correct horse battery staple")


@pytest.mark.parametrize("recovery_key", ["none", "old"])
def test_not_turned_on_without_a_recovery_key_to_seal_them_for(
    tmp_path: Path, recovery_key: str
) -> None:
    unlock = unlocked(tmp_path)
    if recovery_key == "old":
        with_old_recovery_key(unlock)

    with pytest.raises(KeyFileError):
        unlock.turn_on_secret_notes(SECRET)
    assert not unlock.has_secret_notes


def test_an_old_recovery_key_typed_once_opens_them_from_then_on(tmp_path: Path) -> None:
    unlock = unlocked(tmp_path)
    old = with_old_recovery_key(unlock)
    assert unlock.recovery_key_is_old

    with pytest.raises(WrongRecoveryKeyError):
        unlock.upgrade_recovery_key(generate())
    assert unlock.recovery_key_is_old
    unlock.upgrade_recovery_key(old)

    assert not unlock.recovery_key_is_old
    assert unlock.recovery_key_made_at is None  # not made now: when, still unknown
    assert unlock.open_with_recovery_key(old) == KEY  # the same key, as before
    key = unlock.turn_on_secret_notes(SECRET)
    assert unlock.open_secret_notes_with_recovery_key(old) == key


def test_an_old_recovery_key_for_other_notes_is_refused(tmp_path: Path) -> None:
    unlock = unlocked(tmp_path)
    old = with_old_recovery_key(unlock)
    unlock.opened_key = secrets.token_bytes(32)  # these notes opened with another key

    with pytest.raises(WrongRecoveryKeyError):
        unlock.upgrade_recovery_key(old)
    assert unlock.recovery_key_is_old


def test_a_new_recovery_key_made_with_their_key_opens_them_and_the_old_one_no_longer(
    tmp_path: Path,
) -> None:
    unlock = unlocked(tmp_path)
    first = unlock.make_recovery_key(KEY)
    key = unlock.turn_on_secret_notes(SECRET)

    second = unlock.make_recovery_key(KEY, key)

    assert unlock.open_secret_notes_with_recovery_key(second) == key
    assert unlock.open_with_recovery_key(second) == KEY
    with pytest.raises(WrongRecoveryKeyError):
        unlock.open_secret_notes_with_recovery_key(first)


def test_made_without_their_key_they_are_kept_for_it_once_opened(tmp_path: Path) -> None:
    unlock = unlocked(tmp_path)
    unlock.make_recovery_key(KEY)
    key = unlock.turn_on_secret_notes(SECRET)
    second = unlock.make_recovery_key(KEY)  # their key not at hand
    assert not unlock.secret_notes_kept_for_recovery_key
    with pytest.raises(NotKeptForRecoveryKeyError):
        unlock.open_secret_notes_with_recovery_key(second)

    unlock.keep_secret_notes_for_recovery_key(unlock.open_secret_notes(SECRET))

    assert unlock.secret_notes_kept_for_recovery_key
    assert unlock.open_secret_notes_with_recovery_key(second) == key


def test_stopped_before_the_new_recovery_key_is_written_the_old_one_still_opens_the_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Their key is sealed for the new pair first, then the pair is written. Cut
    # off in between, the old recovery key still opens the notes, and once their
    # password opens them they are sealed for it again.
    unlock = unlocked(tmp_path)
    first = unlock.make_recovery_key(KEY)
    key = unlock.turn_on_secret_notes(SECRET)

    def cut_off(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("stickle.unlock.save_recovery_slot", cut_off)
    with pytest.raises(OSError):
        unlock.make_recovery_key(KEY, key)
    monkeypatch.undo()

    assert unlock.open_with_recovery_key(first) == KEY
    assert unlock.open_secret_notes(SECRET) == key
    assert not unlock.secret_notes_kept_for_recovery_key
    unlock.keep_secret_notes_for_recovery_key(key)
    assert unlock.open_secret_notes_with_recovery_key(first) == key


def test_their_key_not_sealed_for_the_new_one_nothing_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlock = unlocked(tmp_path)
    first = unlock.make_recovery_key(KEY)
    key = unlock.turn_on_secret_notes(SECRET)
    before = (unlock.files.raw(B), unlock.files.raw(C))

    def cut_off(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("stickle.crypto.secret_notes.keep_for_recovery_key", cut_off)
    with pytest.raises(OSError):
        unlock.make_recovery_key(KEY, key)
    monkeypatch.undo()

    assert (unlock.files.raw(B), unlock.files.raw(C)) == before
    assert unlock.open_secret_notes_with_recovery_key(first) == key


def test_a_damaged_key_file_is_never_sealed_over(tmp_path: Path) -> None:
    unlock = unlocked(tmp_path)
    unlock.make_recovery_key(KEY)
    key = unlock.turn_on_secret_notes(SECRET)
    unlock.make_recovery_key(KEY)
    for path in (unlock.files.path(C), unlock.files.second_path(C)):
        path.write_bytes(b"damaged")

    assert unlock.secret_notes_kept_for_recovery_key  # nothing to do: not written over
    with pytest.raises(KeyFileError):
        unlock.keep_secret_notes_for_recovery_key(key)
    assert unlock.files.path(C).read_bytes() == b"damaged"


def test_the_recovery_key_slot_stays_as_recovery_makes_it(tmp_path: Path) -> None:
    unlock = unlocked(tmp_path)
    recovery_key = unlock.make_recovery_key(KEY)
    slot = recovery.read_recovery(unlock.files)
    assert slot is not None and recovery.unwrap(slot, recovery_key) == KEY
