"""A key pair opened by what the user types, and keys sealed for it.

The recovery key and the master password each have a random key pair
(libsodium's crypto_box). Its secret key is wrapped with a key derived from
what the user types (stickle.crypto.keyfile), and what it opens is sealed for
its public key (crypto_box_seal). Sealing needs only the public key: the app
can make a key open with the recovery key without the recovery key itself,
as each device will when notes are synced. Changing what is typed rewraps the
secret key alone; the sealed keys stay as they are.

    {"kdf": "argon2id", "opslimit": 2, "memlimit": 67108864, "salt": "<base64>",
     "box": "<the secret key, wrapped>", "public": "<base64>",
     "sealed": "<the key it opens, sealed>", "set_at": "<UTC time>"}
"""

import base64
from dataclasses import dataclass
from typing import Any, cast

from stickle.crypto.keyfile import (
    KEY_BYTES,
    KeyFileError,
    PasswordSlot,
    b64_field,
    decode_slot,
    encode_slot,
    unwrap_with_password,
    wrap_with_password,
)


@dataclass(frozen=True)
class SealedSlot:
    secret: PasswordSlot  # the pair's secret key, wrapped with what the user types
    public: bytes
    sealed: bytes  # the key it opens, sealed for the public key

    @property
    def set_at(self) -> str | None:
        return self.secret.set_at


def seal(public: bytes, key: bytes) -> bytes:
    """key, sealed so that only the pair's secret key opens it."""
    from nacl.public import PublicKey, SealedBox

    if len(key) != KEY_BYTES:
        raise ValueError(f"key must be {KEY_BYTES} bytes")
    return bytes(SealedBox(PublicKey(public)).encrypt(key))


def make_sealed_slot(
    key: bytes, typed: str, opslimit: int, memlimit: int, slot_name: str, set_at: str | None
) -> SealedSlot:
    """A new key pair opened by typed, with key sealed for it."""
    from nacl.public import PrivateKey

    pair = PrivateKey.generate()
    secret = wrap_with_password(bytes(pair), typed, opslimit, memlimit, slot_name, set_at)
    public = bytes(pair.public_key)
    return SealedSlot(secret, public, seal(public, key))


def open_pair(slot: SealedSlot, typed: str, slot_name: str) -> bytes:
    """The pair's secret key; WrongPasswordError if typed does not open it,
    KeyFileError if the slot was altered (its public key is not the pair's)."""
    from nacl.public import PrivateKey

    secret = unwrap_with_password(slot.secret, typed, slot_name)
    if bytes(PrivateKey(secret).public_key) != slot.public:
        raise KeyFileError("the public key does not belong to the secret key")
    return secret


def unseal(secret: bytes, sealed: bytes) -> bytes:
    """A key sealed for the pair whose secret key this is (KeyFileError if not)."""
    from nacl.exceptions import CryptoError
    from nacl.public import PrivateKey, SealedBox

    try:
        key = SealedBox(PrivateKey(secret)).decrypt(sealed)
    except CryptoError as error:
        raise KeyFileError("the sealed key does not open") from error
    if len(key) != KEY_BYTES:
        raise KeyFileError("sealed key has the wrong length")
    return key


def open_sealed_slot(slot: SealedSlot, typed: str, slot_name: str) -> bytes:
    """The sealed key; WrongPasswordError if typed does not open the pair,
    KeyFileError if the slot was altered."""
    return unseal(open_pair(slot, typed, slot_name), slot.sealed)


def decode_sealed_slot(value: object) -> SealedSlot:
    if not isinstance(value, dict):
        raise KeyFileError("sealed slot is not an object")
    fields = cast(dict[str, object], value)
    return SealedSlot(
        decode_slot(fields), b64_field(fields.get("public")), b64_field(fields.get("sealed"))
    )


def encode_sealed_slot(slot: SealedSlot) -> dict[str, Any]:
    fields = encode_slot(slot.secret)
    fields["public"] = base64.b64encode(slot.public).decode("ascii")
    fields["sealed"] = base64.b64encode(slot.sealed).decode("ascii")
    return fields
