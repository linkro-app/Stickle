"""The keys of a synced vault.

A vault's items are locked with its data key: random, one per vault, kept in
each attached device's encrypted database. The data key locks nothing itself:
each use has a key of its own, derived from it with keyed BLAKE2b and the
use's name as its personalization (as libsodium's crypto_kdf does), so no two
uses ever share a key.

Data keys are numbered from 1, and every locked file names the number it was
locked with. A number this device does not have means a newer version changed
keys: sync stops until the app is updated, and the file is not taken for a
damaged one. What must stay the same when keys change (the ids of records, and
hashes) comes from key 1.

A record whose id is not made when it is (a setting, a note brought in from
another app) has an id from its name, hashed with the data key: the same on
every device, and nothing a cloud could read the name back from. It reads as
a random UUID does, as a category's id does.

The keys/ folder holds the data key for whoever opens it another way: one file
for each, sealed for a key pair (stickle.crypto.sealed). The recovery key's
and the master password's are the pairs of the key file B, as they are, so a
device seals the data key for them without either being typed; the one who
types it on a device to come opens the pair, then the data key. keys/ cannot be
locked with the data key it opens, so each file carries a check value made
with the data key: only a device that has the key makes one, and a device
seals nothing for a pair whose check does not hold. Whoever can write in the
cloud cannot put their own pair in the user's place.

    {"check": "<base64>", "format": 1, "key": 1, "receiver": "recovery_key",
     "kdf": "argon2id", "opslimit": 2, "memlimit": 67108864, "salt": "<base64>",
     "box": "<the secret key, wrapped>", "public": "<base64>",
     "sealed": "<the data key, sealed>", "set_at": "<UTC time>"}
"""

import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

from stickle.crypto.keyfile import KeyFileError, WrongPasswordError
from stickle.crypto.sealed import (
    SealedSlot,
    decode_sealed_slot,
    encode_sealed_slot,
    open_pair,
    seal,
    unseal,
)

KEY_BYTES = 32
FIRST = 1
ITEMS = b"stickle-items"  # the key that locks each item's envelope
IDS = b"stickle-ids"  # the key that makes records' ids from their names
CHECKS = b"stickle-keys"  # the key that makes the check values of keys/
RECOVERY_KEY = "recovery_key"  # the recovery key's pair: its slot in the key file B
MASTER_PASSWORD = "master_password"  # the master password's pair
RECEIVERS = (RECOVERY_KEY, MASTER_PASSWORD)  # devices (0.8) and members (1.2) to come
KEY_FILE_FORMAT = 1


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

    def lasting_key(self, use: bytes) -> bytes:
        """A key for one use that stays the same when keys change: from key 1."""
        return derive(self.key(FIRST), use)

    def record_id(self, kind: str, name: str) -> str:
        """The id of the record of kind named name, the same on every device."""
        digest = hashlib.blake2b(
            f"{kind}\0{name}".encode(), digest_size=16, key=self.lasting_key(IDS)
        ).digest()
        return str(uuid.UUID(bytes=digest, version=4))


class AlteredReceiverError(Exception):
    """A keys/ file that does not hold together: damaged, or changed by someone
    without the data key."""


@dataclass(frozen=True)
class Receiver:
    """A file in keys/: the data key, sealed for one way of opening it."""

    id: str
    kind: str
    key: int  # the number of the data key sealed in it
    slot: SealedSlot | None  # None for a kind, or a format, this version does not know
    check: bytes
    written: Mapping[str, object] = field(default_factory=dict[str, object])  # all of it


def receiver_id(kind: str, public: bytes) -> str:
    """The keys/ file's id for a pair: the same wherever it is added from."""
    digest = hashlib.blake2b(
        b"stickle receiver\0" + kind.encode() + b"\0" + public, digest_size=16
    ).digest()
    return str(uuid.UUID(bytes=digest, version=4))


def _check(keys: DataKeys, number: int, kind: str, file_id: str, public: bytes) -> bytes:
    told = b"stickle keys/\0" + kind.encode() + b"\0" + file_id.encode() + b"\0" + public
    return hashlib.blake2b(told, digest_size=32, key=derive(keys.key(number), CHECKS)).digest()


def key_file(keys: DataKeys, kind: str, pair: SealedSlot) -> tuple[str, bytes]:
    """The id and bytes of a keys/ file for pair, as the key file B keeps it (its
    secret key wrapped with what the user types), with the current data key
    sealed for it."""
    if kind not in RECEIVERS:
        raise ValueError(f"not a way to open the data key: {kind!r}")
    number = keys.current
    file_id = receiver_id(kind, pair.public)
    written = encode_sealed_slot(
        SealedSlot(pair.secret, pair.public, seal(pair.public, keys.key(number)))
    )
    written |= {
        "check": base64.b64encode(_check(keys, number, kind, file_id, pair.public)).decode(),
        "format": KEY_FILE_FORMAT,
        "key": number,
        "receiver": kind,
    }
    return file_id, json.dumps(written, sort_keys=True, separators=(",", ":")).encode()


def read_key_file(file_id: str, data: bytes) -> Receiver:
    """A keys/ file named file_id; AlteredReceiverError if it does not hold together.
    One of a kind or format this version does not know is kept, and opens nothing."""
    try:
        written = json.loads(data.decode())
    except (UnicodeDecodeError, ValueError) as error:
        raise AlteredReceiverError("not JSON") from error
    if not isinstance(written, dict):
        raise AlteredReceiverError("not an object")
    fields = cast(dict[str, object], written)
    kind, number, number_format = fields.get("receiver"), fields.get("key"), fields.get("format")
    if not isinstance(kind, str) or not _number(number) or not _number(number_format):
        raise AlteredReceiverError("no receiver, key or format")
    try:
        check = base64.b64decode(cast(str, fields.get("check")), validate=True)
    except (TypeError, ValueError) as error:
        raise AlteredReceiverError("no check value") from error
    slot = None
    if kind in RECEIVERS and number_format == KEY_FILE_FORMAT:
        try:
            slot = decode_sealed_slot(fields)
        except KeyFileError as error:
            raise AlteredReceiverError(str(error)) from error
    return Receiver(file_id, kind, cast(int, number), slot, check, fields)


def _number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def is_trusted(receiver: Receiver, keys: DataKeys) -> bool:
    """Whether receiver was made by a device with the data key: only then is
    anything sealed for its pair."""
    if receiver.slot is None:
        return False
    try:
        made = _check(keys, receiver.key, receiver.kind, receiver.id, receiver.slot.public)
    except UnknownDataKeyError:
        return False
    return hmac.compare_digest(made, receiver.check)


def open_receiver(receiver: Receiver, typed: str) -> DataKeys:
    """The data key, from what the user typed (a recovery key in its canonical
    form). WrongPasswordError if typed does not open the pair,
    AlteredReceiverError if the file does not hold together."""
    if receiver.slot is None:
        raise AlteredReceiverError("a way of opening this version does not know")
    try:
        data_key = unseal(open_pair(receiver.slot, typed, receiver.kind), receiver.slot.sealed)
    except KeyFileError as error:
        raise AlteredReceiverError(str(error)) from error
    keys = DataKeys({receiver.key: data_key})
    if not is_trusted(receiver, keys):
        raise AlteredReceiverError("the check value does not hold")
    return keys


def open_with_recovery_key(receiver: Receiver, typed: str) -> DataKeys:
    """The data key; RecoveryKeyTypoError or WrongRecoveryKeyError
    (stickle.crypto.recovery) for what was typed, AlteredReceiverError."""
    from stickle.crypto.recovery import WrongRecoveryKeyError, canonical

    if receiver.kind != RECOVERY_KEY:
        raise ValueError("not the recovery key's file")
    try:
        return open_receiver(receiver, canonical(typed))
    except WrongPasswordError as error:
        raise WrongRecoveryKeyError from error


def open_with_master_password(receiver: Receiver, typed: str) -> DataKeys:
    """The data key; WrongPasswordError for what was typed, AlteredReceiverError."""
    if receiver.kind != MASTER_PASSWORD:
        raise ValueError("not the master password's file")
    return open_receiver(receiver, typed)
