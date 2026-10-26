"""The secret notes' key, and their text encrypted with it.

One random key, for every secret note of the user, in the key file C
(stickle.crypto.key_files): wrapped with the secret notes' password, and
sealed for the recovery key's pair (stickle.crypto.recovery), so that a
forgotten password is set anew with the recovery key. Whoever can open the
notes database (its key, the master password) still cannot read a secret note.

    {"slots": {"secret_password": <a password slot>,
               "recovery_key": {"public": "<the pair's public key>",
                                "sealed": "<the key, sealed for it>"}}}

Each note's text is encrypted on its own with that key (XChaCha20-Poly1305),
bound to the note's id: moved onto another note, it does not open.
"""

import base64
import secrets
from typing import Any, cast

from stickle.crypto.key_files import C, KeyFiles
from stickle.crypto.keyfile import (
    KEY_BYTES,
    MEMLIMIT,
    OPSLIMIT,
    KeyFileError,
    PasswordSlot,
    b64_field,
    decode_slot,
    encode_slot,
    unwrap_with_password,
    wrap_with_password,
)
from stickle.crypto.sealed import seal, unseal

PASSWORD_SLOT = "secret_password"
RECOVERY_SLOT = "recovery_key"
# Sealed for the master password while it opened them too (before 0.6 was out):
# no longer read, and left out whenever C is written again.
OLD_MASTER_SLOT = "master_password"
MIN_PASSWORD_LENGTH = 8


class SecretNoteError(Exception):
    """A secret note's text does not open with this key (another note's, or altered)."""


class NotKeptForRecoveryKeyError(KeyFileError):
    """The secret notes' key is not sealed for this recovery key's pair (made
    before they were, or since without it)."""


def _aad(note_id: str) -> bytes:
    return f"stickle secret note {note_id}".encode()


def seal_text(key: bytes, note_id: str, text: str) -> bytes:
    """The note's text encrypted for that note alone (nonce + ciphertext + tag)."""
    from nacl import secret

    return bytes(secret.Aead(key).encrypt(text.encode("utf-8"), _aad(note_id)))


def open_text(key: bytes, note_id: str, sealed: bytes) -> str:
    from nacl import exceptions, secret

    try:
        return secret.Aead(key).decrypt(sealed, _aad(note_id)).decode("utf-8")
    except (exceptions.CryptoError, UnicodeDecodeError) as error:
        raise SecretNoteError(note_id) from error


def has_secret_notes(files: KeyFiles) -> bool:
    """Whether secret notes were turned on here: C is there in some copy, even
    one that cannot be read. Never "not yet" for a key that is only damaged or
    out of reach, or turning them on again would put a new key over it."""
    return files.raw(C) is not None


def _recovery_seal(recovery_public: bytes, key: bytes) -> dict[str, Any]:
    return {
        "public": base64.b64encode(recovery_public).decode("ascii"),
        "sealed": base64.b64encode(seal(recovery_public, key)).decode("ascii"),
    }


def create(
    files: KeyFiles,
    password: str,
    recovery_public: bytes,
    set_at: str,
    strength: tuple[int, int] = (OPSLIMIT, MEMLIMIT),
) -> bytes:
    """A new secret notes' key, kept in C for the password and the recovery
    key; the key. Only where there is none yet."""
    if files.raw(C) is not None:
        raise KeyFileError("the secret notes' key exists already")
    key = secrets.token_bytes(KEY_BYTES)
    slot = wrap_with_password(key, password, *strength, slot_name=PASSWORD_SLOT, set_at=set_at)
    files.save(
        C, {PASSWORD_SLOT: encode_slot(slot), RECOVERY_SLOT: _recovery_seal(recovery_public, key)}
    )
    return key


def _password_slot(files: KeyFiles) -> PasswordSlot:
    slots = files.load(C)
    if slots is None or PASSWORD_SLOT not in slots:
        raise KeyFileError("no secret notes' key")
    return decode_slot(slots[PASSWORD_SLOT])


def password_set_at(files: KeyFiles) -> str | None:
    return _password_slot(files).set_at


def open_with_password(files: KeyFiles, password: str) -> bytes:
    """The key; WrongPasswordError if the password is not the secret notes'."""
    return unwrap_with_password(_password_slot(files), password, PASSWORD_SLOT)


def recovery_public(files: KeyFiles) -> bytes | None:
    """The public key of the recovery key the secret notes' key is sealed for;
    None if it is sealed for none (KeyFileError if C is damaged)."""
    slots = files.load(C)
    if slots is None or not isinstance(slots.get(RECOVERY_SLOT), dict):
        return None
    fields = cast(dict[str, object], slots[RECOVERY_SLOT])
    return b64_field(fields.get("public"))


def open_with_recovery_key(files: KeyFiles, pair_secret: bytes, pair_public: bytes) -> bytes:
    """The key, through the recovery key's pair (its secret key opened by the
    recovery key typed); NotKeptForRecoveryKeyError if it is not sealed for that pair."""
    slots = files.load(C)
    if slots is None or not isinstance(slots.get(RECOVERY_SLOT), dict):
        raise NotKeptForRecoveryKeyError("the secret notes' key is not kept for a recovery key")
    fields = cast(dict[str, object], slots[RECOVERY_SLOT])
    if b64_field(fields.get("public")) != pair_public:
        raise NotKeptForRecoveryKeyError("kept for another recovery key")
    return unseal(pair_secret, b64_field(fields.get("sealed")))


def _slots(files: KeyFiles) -> dict[str, object]:
    """C's slots, to be written again: never a C made from nothing here."""
    slots = files.load(C)
    if slots is None:
        raise KeyFileError("no secret notes' key")
    return dict(slots)


def _save(files: KeyFiles, slots: dict[str, object]) -> None:
    slots.pop(OLD_MASTER_SLOT, None)
    files.save(C, slots)


def change_password(
    files: KeyFiles,
    key: bytes,
    password: str,
    set_at: str,
    strength: tuple[int, int] = (OPSLIMIT, MEMLIMIT),
) -> None:
    """The same key, opened by a new password in place of the old one (the key
    known: opened with the old password or the recovery key)."""
    slots = _slots(files)
    slot = wrap_with_password(key, password, *strength, slot_name=PASSWORD_SLOT, set_at=set_at)
    slots[PASSWORD_SLOT] = encode_slot(slot)
    _save(files, slots)


def keep_for_recovery_key(files: KeyFiles, key: bytes, recovery_public: bytes) -> None:
    """The key sealed for a recovery key's pair, in place of any earlier one
    (the key known)."""
    slots = _slots(files)
    slots[RECOVERY_SLOT] = _recovery_seal(recovery_public, key)
    _save(files, slots)
