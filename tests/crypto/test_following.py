"""Taking the vault's recovery key or master password in the key file B: B's
slot becomes the vault's pair with the database key sealed for it, only if B's
is still the one the round saw; the secret notes' key keeps the old recovery
key's pair beside it first, and a key file looked at from sync's thread is
never written."""

import secrets
from pathlib import Path

import pytest

from stickle.crypto import master, recovery, secret_notes
from stickle.crypto.following import digest_of, follow, kept_pairs, pairs_in
from stickle.crypto.key_files import B, C, KeyFiles
from stickle.crypto.keyfile import KeyFileError, WrongPasswordError, wrap_with_password
from stickle.crypto.recovery import canonical, open_recovery_pair
from stickle.crypto.sealed import SealedSlot
from stickle.crypto.vault_keys import MASTER_PASSWORD, RECOVERY_KEY

DATABASE_KEY = secrets.token_bytes(32)
FAST = (1, 8192)
WHEN = "2026-11-15T09:00:00.000Z"


def files_with_recovery_key(folder: Path) -> tuple[KeyFiles, str]:
    files = KeyFiles(folder)
    typed = recovery.generate()
    recovery.write_recovery(files, DATABASE_KEY, typed, WHEN)
    return files, typed


def the_vaults() -> tuple[SealedSlot, str]:
    """A pair as keys/ holds it: what is sealed in it is another key."""
    typed = recovery.generate()
    return recovery.make_recovery_slot(
        secrets.token_bytes(32), typed, "2026-11-16T00:00:00.000Z"
    ), typed


def test_b_takes_the_pair_with_the_database_key_sealed_for_it(tmp_path: Path) -> None:
    files, own = files_with_recovery_key(tmp_path)
    theirs, typed = the_vaults()
    seen = digest_of(pairs_in(files.load(B)).get(RECOVERY_KEY))

    assert follow(files, RECOVERY_KEY, theirs, DATABASE_KEY, seen)

    slot = recovery.read_recovery(files)
    assert slot is not None
    assert recovery.unwrap(slot, typed) == DATABASE_KEY
    with pytest.raises(recovery.WrongRecoveryKeyError):
        recovery.unwrap(slot, own)
    assert digest_of(pairs_in(files.load(B))[RECOVERY_KEY]) == digest_of(theirs)


def test_b_changed_since_the_round_saw_it_is_left_as_it_is(tmp_path: Path) -> None:
    files, _own = files_with_recovery_key(tmp_path)
    seen = digest_of(pairs_in(files.load(B)).get(RECOVERY_KEY))
    made = recovery.generate()
    recovery.write_recovery(files, DATABASE_KEY, made, WHEN)  # meanwhile, here
    before = files.path(B).read_bytes()
    theirs, _typed = the_vaults()

    assert not follow(files, RECOVERY_KEY, theirs, DATABASE_KEY, seen)

    assert files.path(B).read_bytes() == before
    assert not follow(files, MASTER_PASSWORD, theirs, DATABASE_KEY, "a digest it never had")


def test_a_spare_password_taken_where_there_was_none_keeps_the_recovery_key(
    tmp_path: Path,
) -> None:
    files, own = files_with_recovery_key(tmp_path)
    spare = master.read_master(files)
    assert spare is None
    taken = SealedSlot(
        wrap_with_password(secrets.token_bytes(32), "theirs", *FAST, master.SLOT, WHEN),
        secrets.token_bytes(32),
        b"",
    )

    assert follow(files, MASTER_PASSWORD, taken, DATABASE_KEY, None)

    slot = recovery.read_recovery(files)
    assert slot is not None and recovery.unwrap(slot, own) == DATABASE_KEY


def test_an_old_recovery_key_from_before_its_pairs_is_replaced(tmp_path: Path) -> None:
    files = KeyFiles(tmp_path)
    old = recovery.generate()
    files.save(B, {recovery.OLD_SLOT: {"kdf": "argon2id"}})  # its shape is enough here
    theirs, typed = the_vaults()

    assert follow(files, RECOVERY_KEY, theirs, DATABASE_KEY, None)

    slots = files.load(B)
    assert slots is not None and recovery.OLD_SLOT not in slots
    slot = recovery.read_recovery(files)
    assert slot is not None and recovery.unwrap(slot, typed) == DATABASE_KEY
    assert old  # never asked for


def test_the_secret_notes_keep_the_old_pair_until_sealed_for_the_new(tmp_path: Path) -> None:
    files, own = files_with_recovery_key(tmp_path)
    pair = pairs_in(files.load(B))[RECOVERY_KEY]
    key = secret_notes.create(files, "secret password", pair.public, WHEN, FAST)
    theirs, typed = the_vaults()

    assert follow(files, RECOVERY_KEY, theirs, DATABASE_KEY, digest_of(pair), before_sync=True)

    assert secret_notes.pair_aside(files) is True
    assert secret_notes.open_with_pair_aside(files, canonical(own)) == key
    with pytest.raises(WrongPasswordError):
        secret_notes.open_with_pair_aside(files, canonical(typed))
    new = pairs_in(files.load(B))[RECOVERY_KEY]
    secret_notes.keep_for_recovery_key(files, key, new.public)
    assert secret_notes.pair_aside(files) is None
    opened = secret_notes.open_with_recovery_key(files, open_recovery_pair(new, typed), new.public)
    assert opened == key


def test_the_secret_notes_keep_the_first_pair_they_were_sealed_for(tmp_path: Path) -> None:
    """Taken twice before they open: the one they open with stays."""
    files, own = files_with_recovery_key(tmp_path)
    pair = pairs_in(files.load(B))[RECOVERY_KEY]
    key = secret_notes.create(files, "secret password", pair.public, WHEN, FAST)
    first, _ = the_vaults()
    follow(files, RECOVERY_KEY, first, DATABASE_KEY, digest_of(pair), before_sync=True)
    second, _ = the_vaults()

    follow(files, RECOVERY_KEY, second, DATABASE_KEY, digest_of(first))

    assert secret_notes.pair_aside(files) is True
    assert secret_notes.open_with_pair_aside(files, canonical(own)) == key


def test_the_old_pair_not_kept_for_a_disk_that_fails_leaves_b_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files, own = files_with_recovery_key(tmp_path)
    pair = pairs_in(files.load(B))[RECOVERY_KEY]
    secret_notes.create(files, "secret password", pair.public, WHEN, FAST)
    before = files.path(B).read_bytes()
    saving = files.save

    def failing(name: str, slots: dict[str, object]) -> None:
        if name == C:
            raise OSError("the disk is full")
        saving(name, slots)

    monkeypatch.setattr(files, "save", failing)
    theirs, _ = the_vaults()

    with pytest.raises(OSError):
        follow(files, RECOVERY_KEY, theirs, DATABASE_KEY, digest_of(pair))

    assert files.path(B).read_bytes() == before
    slot = recovery.read_recovery(files)
    assert slot is not None and recovery.unwrap(slot, own) == DATABASE_KEY


def test_a_key_file_looked_at_is_never_written(tmp_path: Path) -> None:
    files, _own = files_with_recovery_key(tmp_path)
    second = files.second_path(B).read_bytes()
    files.path(B).unlink()  # gone while Stickle runs

    pairs = kept_pairs(files)  # the second copy, read

    assert RECOVERY_KEY in pairs
    assert not files.path(B).exists()  # not put back: that is the app's to do
    files.second_path(B).write_bytes(b"{half written")
    with pytest.raises(KeyFileError):
        kept_pairs(files)
    assert files.second_path(B).read_bytes() == b"{half written"
    assert not list(tmp_path.glob("*.damaged*")) and not list(tmp_path.glob("*/*.damaged*"))
    files.second_path(B).write_bytes(second)
    files.second_path(B).unlink()
    with pytest.raises(KeyFileError):  # none at all: B is put back from the notes first
        kept_pairs(files)
