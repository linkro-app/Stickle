"""The keys of a synced vault.

A vault's items are locked with its data key: random, one per vault, kept in
each attached device's encrypted database. The data key locks nothing itself:
each use has a key of its own, derived from it with keyed BLAKE2b and the
use's name as its personalization (as libsodium's crypto_kdf does), so no two
uses ever share a key.

Data keys are numbered from 1, and every locked file names the number it was
locked with. A number this device does not have means a newer version changed
keys: sync stops until the app is updated, and the file is not taken for a
damaged one.
"""

from collections.abc import Mapping
from dataclasses import dataclass

KEY_BYTES = 32
FIRST = 1
ITEMS = b"stickle-items"  # the key that locks each item's envelope


class UnknownDataKeyError(Exception):
    """A data key number this device does not have."""

    def __init__(self, number: int) -> None:
        super().__init__(f"no data key {number}")
        self.number = number


def new_data_key() -> bytes:
    from nacl import utils

    return utils.random(KEY_BYTES)


def derive(key: bytes, use: bytes) -> bytes:
    """The key for one use of key; use is at most 16 bytes."""
    from nacl import encoding, hash

    if len(use) > 16:
        raise ValueError("a use is named in at most 16 bytes")
    return hash.blake2b(
        b"", digest_size=KEY_BYTES, key=key, person=use, encoder=encoding.RawEncoder
    )


@dataclass(frozen=True)
class DataKeys:
    """A vault's data keys, by number."""

    keys: Mapping[int, bytes]

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError("no data key")
        for number, key in self.keys.items():
            if number < FIRST or len(key) != KEY_BYTES:
                raise ValueError(f"data key {number} is not a key")

    @property
    def current(self) -> int:
        """The number new files are locked with."""
        return max(self.keys)

    def key(self, number: int) -> bytes:
        try:
            return self.keys[number]
        except KeyError:
            raise UnknownDataKeyError(number) from None

    def item_key(self, number: int) -> bytes:
        return derive(self.key(number), ITEMS)
