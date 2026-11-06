"""A vault's data keys: each use derives a key of its own, and a key number this
device does not have is told apart."""

import hashlib

import pytest

from stickle.crypto.vault_keys import (
    DataKeys,
    UnknownDataKeyError,
    derive,
    new_data_key,
)


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
