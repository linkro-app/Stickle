"""A vault's data keys: each use derives a key of its own, and a key number this
device does not have is told apart. Records' ids are the same on every device
with the key, and no other. keys/ opens the data key with the recovery key or
the master password, as the key file B keeps their pairs; a pair someone put
there without the data key is not trusted, and a data key someone sealed in
its place does not open."""

import base64
import hashlib
import json
import uuid

import pytest

from stickle.crypto import master, recovery
from stickle.crypto.keyfile import WrongPasswordError
from stickle.crypto.recovery import RecoveryKeyTypoError, WrongRecoveryKeyError
from stickle.crypto.sealed import SealedSlot, encode_sealed_slot, make_sealed_slot, seal
from stickle.crypto.vault_keys import (
    MASTER_PASSWORD,
    RECOVERY_KEY,
    AlteredReceiverError,
    DataKeys,
    UnknownDataKeyError,
    derive,
    is_trusted,
    key_file,
    new_data_key,
    open_with_master_password,
    open_with_recovery_key,
    read_key_file,
    receiver_id,
)

KEYS = DataKeys({1: bytes(range(32))})
OTHER_KEYS = DataKeys({1: bytes(range(1, 33))})
DATABASE_KEY = bytes([7]) * 32
RECOVERY = recovery.generate()
RECOVERY_PAIR = recovery.make_recovery_slot(DATABASE_KEY, RECOVERY, "2026-11-07T00:00:00Z")
PASSWORD = "a master password"
MASTER_PAIR = make_sealed_slot(DATABASE_KEY, PASSWORD, 1, 8192, master.SLOT, None)


def written(data: bytes) -> dict[str, object]:
    return json.loads(data)


def as_bytes(fields: dict[str, object]) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


def test_a_use_derives_a_key_of_its_own() -> None:
    key = new_data_key()

    items, other = derive(key, b"stickle-items"), derive(key, b"stickle-other")

    assert len(items) == 32 and items != other and items != key
    assert derive(key, b"stickle-items") == items
    # keyed BLAKE2b, personalized with the use: what libsodium.js computes too
    assert items == hashlib.blake2b(b"", digest_size=32, key=key, person=b"stickle-items").digest()
    with pytest.raises(ValueError):
        derive(key, b"a use named too long")


def test_the_current_key_is_the_highest_number_and_others_are_told_apart() -> None:
    keys = DataKeys({1: bytes(32), 2: bytes([1]) * 32})

    assert keys.current == 2
    assert keys.item_key(1) != keys.item_key(2)
    with pytest.raises(UnknownDataKeyError) as raised:
        keys.item_key(3)
    assert raised.value.number == 3


@pytest.mark.parametrize("keys", [{}, {0: bytes(32)}, {1: bytes(16)}])
def test_only_keys_make_data_keys(keys: dict[int, bytes]) -> None:
    with pytest.raises(ValueError):
        DataKeys(keys)


def test_a_records_id_is_the_same_on_every_device_with_the_key() -> None:
    on_one, on_another = KEYS, DataKeys({1: bytes(range(32))})

    made = on_one.record_id("setting", "reminder_snooze")

    assert made == on_another.record_id("setting", "reminder_snooze")
    assert made != OTHER_KEYS.record_id("setting", "reminder_snooze")
    assert made != on_one.record_id("import", "reminder_snooze")  # kinds kept apart
    assert made != on_one.record_id("setting", "reminder_snooze2")
    assert uuid.UUID(made).version == 4 and str(uuid.UUID(made)) == made  # as a category's
    assert "reminder" not in made
    assert DataKeys({1: bytes(range(32)), 2: bytes(32)}).record_id("setting", "x") == (
        on_one.record_id("setting", "x")
    )  # when keys change, ids do not


def test_the_pairs_are_the_key_file_bs_slots() -> None:
    assert (RECOVERY_KEY, MASTER_PASSWORD) == (recovery.SLOT, master.SLOT)


def test_the_recovery_key_opens_the_data_key_as_written_or_typed_loosely() -> None:
    file_id, data = key_file(KEYS, RECOVERY_KEY, RECOVERY_PAIR)
    receiver = read_key_file(file_id, data)

    assert open_with_recovery_key(receiver, RECOVERY) == KEYS
    assert open_with_recovery_key(receiver, RECOVERY.lower().replace("-", " ")) == KEYS
    assert is_trusted(receiver, KEYS)
    # the pair as the key file B keeps it: what opens B opens this
    kept = encode_sealed_slot(RECOVERY_PAIR)
    for name in ("box", "salt", "public", "opslimit", "memlimit", "set_at"):
        assert written(data)[name] == kept[name]
    assert written(data)["sealed"] != kept["sealed"]  # the data key, not the database key
    assert recovery.unwrap(RECOVERY_PAIR, RECOVERY) == DATABASE_KEY


def test_a_wrong_or_mistyped_recovery_key_opens_nothing() -> None:
    receiver = read_key_file(*key_file(KEYS, RECOVERY_KEY, RECOVERY_PAIR))

    with pytest.raises(WrongRecoveryKeyError):
        open_with_recovery_key(receiver, recovery.generate())
    with pytest.raises(RecoveryKeyTypoError):
        open_with_recovery_key(receiver, RECOVERY[:-1])


def test_the_master_password_opens_the_data_key() -> None:
    receiver = read_key_file(*key_file(KEYS, MASTER_PASSWORD, MASTER_PAIR))

    assert open_with_master_password(receiver, PASSWORD) == KEYS
    with pytest.raises(WrongPasswordError):
        open_with_master_password(receiver, "not the password")


def test_a_pair_added_from_any_device_has_the_same_file() -> None:
    first_id, first = key_file(KEYS, MASTER_PASSWORD, MASTER_PAIR)
    second_id, second = key_file(KEYS, MASTER_PASSWORD, MASTER_PAIR)

    assert first_id == second_id == receiver_id(MASTER_PASSWORD, MASTER_PAIR.public)
    assert first != second  # sealed anew each time
    assert first_id != key_file(KEYS, RECOVERY_KEY, RECOVERY_PAIR)[0]


def test_someone_elses_pair_in_the_users_place_is_not_trusted() -> None:
    file_id, data = key_file(KEYS, RECOVERY_KEY, RECOVERY_PAIR)
    theirs = make_sealed_slot(bytes(32), "their own", 1, 8192, recovery.SLOT, None)
    swapped = written(data) | {
        name: value
        for name, value in encode_sealed_slot(theirs).items()
        if name in ("box", "salt", "public")
    }

    assert not is_trusted(read_key_file(file_id, as_bytes(swapped)), KEYS)
    # a file of their own, its check made with a key of their own
    their_id, their_file = key_file(OTHER_KEYS, RECOVERY_KEY, theirs)
    assert not is_trusted(read_key_file(their_id, their_file), KEYS)
    # the user's own file, copied under another name
    assert not is_trusted(read_key_file(their_id, data), KEYS)
    assert not is_trusted(read_key_file(file_id, data), OTHER_KEYS)


def test_another_data_key_sealed_in_the_users_file_does_not_open() -> None:
    file_id, data = key_file(KEYS, RECOVERY_KEY, RECOVERY_PAIR)
    public = base64.b64decode(str(written(data)["public"]))
    resealed = written(data) | {
        "sealed": base64.b64encode(seal(public, bytes(32))).decode()  # anyone can seal for it
    }

    with pytest.raises(AlteredReceiverError):
        open_with_recovery_key(read_key_file(file_id, as_bytes(resealed)), RECOVERY)


def test_a_damaged_file_does_not_hold_together() -> None:
    file_id, data = key_file(KEYS, RECOVERY_KEY, RECOVERY_PAIR)
    theirs = make_sealed_slot(bytes(32), "x", 1, 8192, recovery.SLOT, None)
    their_public = written(data) | {"public": base64.b64encode(theirs.public).decode()}
    for damaged in (
        b"",
        b"[]",
        data[: len(data) // 2],
        as_bytes({k: v for k, v in written(data).items() if k != "check"}),
        as_bytes(written(data) | {"key": 0}),
        as_bytes(written(data) | {"box": "not base64!"}),
    ):
        with pytest.raises(AlteredReceiverError):
            read_key_file(file_id, damaged)
    with pytest.raises(AlteredReceiverError):  # the public key is not the secret key's
        open_with_recovery_key(read_key_file(file_id, as_bytes(their_public)), RECOVERY)


def test_a_way_of_opening_this_version_does_not_know_is_kept_and_opens_nothing() -> None:
    file_id, data = key_file(KEYS, RECOVERY_KEY, RECOVERY_PAIR)
    for later in (
        written(data) | {"receiver": "device", "device": "laptop"},
        written(data) | {"format": 2},
    ):
        receiver = read_key_file(file_id, as_bytes(later))
        assert receiver.slot is None and receiver.written == later
        assert not is_trusted(receiver, KEYS)


def test_only_a_pair_makes_a_keys_file() -> None:
    with pytest.raises(ValueError):
        key_file(KEYS, "device", RECOVERY_PAIR)
    assert isinstance(RECOVERY_PAIR, SealedSlot)
    assert derive(KEYS.key(1), b"stickle-keys") != derive(KEYS.key(1), b"stickle-items")
