"""The secret notes' key, and their text encrypted with it.

One random key, for every secret note of the user, in the key file C
(stickle.crypto.key_files): wrapped with the secret notes' password, and
sealed for the master password's key pair (stickle.crypto.master), never for
the recovery key. Whoever can open the notes database (its key, the recovery
key) still cannot read a secret note.

    {"slots": {"secret_password": <a password slot>,
               "master_password": {"public": "<the pair's public key>",
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
from stickle.crypto.sealed import SealedSlot, open_pair, seal, unseal

PASSWORD_SLOT = "secret_password"
MASTER_SLOT = "master_password"
MIN_PASSWORD_LENGTH = 8


class SecretNoteError(Exception):
    """A secret note's text does not open with this key (another note's, or altered)."""


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
    """Whether secret notes were turned on here (KeyFileError if C is damaged)."""
    slots = files.load(C)
    return slots is not None and PASSWORD_SLOT in slots


def _master_seal(master_public: bytes, key: bytes) -> dict[str, Any]:
    return {
        "public": base64.b64encode(master_public).decode("ascii"),
        "sealed": base64.b64encode(seal(master_public, key)).decode("ascii"),
    }


def create(
    files: KeyFiles,
    password: str,
    master_public: bytes,
    set_at: str,
    strength: tuple[int, int] = (OPSLIMIT, MEMLIMIT),
) -> bytes:
    """A new secret notes' key, kept in C for the password and the master
    password; the key. Only where there is none yet."""
    if files.raw(C) is not None:
        raise KeyFileError("the secret notes' key exists already")
    key = secrets.token_bytes(KEY_BYTES)
    slot = wrap_with_password(key, password, *strength, slot_name=PASSWORD_SLOT, set_at=set_at)
    files.save(C, {PASSWORD_SLOT: encode_slot(slot), MASTER_SLOT: _master_seal(master_public, key)})
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


def open_with_master(files: KeyFiles, master: SealedSlot, typed: str) -> bytes:
    """The key, through the master password's pair (WrongPasswordError if typed
    is not the master password; KeyFileError if C was sealed for another pair)."""
    slots = files.load(C)
    if slots is None or not isinstance(slots.get(MASTER_SLOT), dict):
        raise KeyFileError("the secret notes' key is not kept for the master password")
    fields = cast(dict[str, object], slots[MASTER_SLOT])
    if b64_field(fields.get("public")) != master.public:
        raise KeyFileError("kept for another master password")
    return unseal(open_pair(master, typed, "master_password"), b64_field(fields.get("sealed")))


def change_password(
    files: KeyFiles,
    key: bytes,
    password: str,
    set_at: str,
    strength: tuple[int, int] = (OPSLIMIT, MEMLIMIT),
) -> None:
    """The same key, opened by a new password (the key known: notes opened)."""
    slots = dict(files.load(C) or {})
    slot = wrap_with_password(key, password, *strength, slot_name=PASSWORD_SLOT, set_at=set_at)
    slots[PASSWORD_SLOT] = encode_slot(slot)
    files.save(C, slots)


def keep_for_master(files: KeyFiles, key: bytes, master_public: bytes) -> None:
    """The key sealed again for a master password set anew (the key known)."""
    slots = dict(files.load(C) or {})
    slots[MASTER_SLOT] = _master_seal(master_public, key)
    files.save(C, slots)
