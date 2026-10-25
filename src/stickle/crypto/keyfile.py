"""A key file: keys, each wrapped with something only the user knows.

The database key is random. When there is no credential store to keep it,
it is wrapped with a key derived from the user's password (Argon2id) and
stored in a key file, encrypted and authenticated (XChaCha20-Poly1305).
Other key files hold other slots (stickle.crypto.key_files).

    {"warning": ["Do not delete…", "…"], "format": 1, "slots": {"password":
     {"kdf": "argon2id", "opslimit": 3, "memlimit": 268435456,
      "salt": "<base64>", "box": "<base64>", "set_at": "<UTC time>"}}}

Slots, and fields, this version does not know are kept as they are when the
file is rewritten, so an older app never drops what a newer one added. The
file is replaced atomically: a crash leaves either the old or the new file.
"""

import base64
import binascii
import json
import os
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

FORMAT = 1
PASSWORD = "password"
KEY_BYTES = 32
# Read by whoever opens the file: what it is for, in the languages of the app.
WARNING = [
    "Do not delete, move or edit this file: Stickle needs it to open your notes.",
    "이 파일을 지우거나 옮기거나 고치지 마세요. Stickle이 메모를 여는 데 필요합니다.",
]
# libsodium's "moderate" Argon2id limits: about a third of a second and 256 MiB
# on a current PC, once per start. Stored with each slot, so they can be raised.
OPSLIMIT = 3
MEMLIMIT = 256 * 1024 * 1024


class KeyFileError(Exception):
    """The key file exists but cannot be understood; it is never overwritten."""


class WrongPasswordError(Exception):
    """The password does not open the key (or the slot was altered)."""


@dataclass(frozen=True)
class PasswordSlot:
    opslimit: int
    memlimit: int
    salt: bytes
    box: bytes  # nonce + ciphertext + tag
    set_at: str | None = None  # when it was made (UTC); unknown for slots from before 0.6


@dataclass(frozen=True)
class KeyFile:
    password: PasswordSlot | None
    other_slots: dict[str, object]  # kept unchanged

    @property
    def has_password(self) -> bool:
        return self.password is not None


def normalized(password: str) -> bytes:
    # The same password typed on macOS (decomposed) and elsewhere must match.
    return unicodedata.normalize("NFC", password).encode("utf-8")


def _aad(slot: str) -> bytes:
    return f"stickle key file {FORMAT} {slot}".encode()


def wrap_with_password(
    key: bytes,
    password: str,
    opslimit: int = OPSLIMIT,
    memlimit: int = MEMLIMIT,
    slot_name: str = PASSWORD,
    set_at: str | None = None,
) -> PasswordSlot:
    """key wrapped with password; slot_name binds it to its slot (a recovery key has its own)."""
    from nacl import pwhash, secret, utils

    if len(key) != KEY_BYTES:
        raise ValueError(f"key must be {KEY_BYTES} bytes")
    salt = utils.random(pwhash.argon2id.SALTBYTES)
    wrapping_key = pwhash.argon2id.kdf(
        secret.Aead.KEY_SIZE, normalized(password), salt, opslimit=opslimit, memlimit=memlimit
    )
    box = bytes(secret.Aead(wrapping_key).encrypt(key, _aad(slot_name)))
    return PasswordSlot(opslimit, memlimit, salt, box, set_at)


def unwrap_with_password(slot: PasswordSlot, password: str, slot_name: str = PASSWORD) -> bytes:
    from nacl import exceptions, pwhash, secret

    wrapping_key = pwhash.argon2id.kdf(
        secret.Aead.KEY_SIZE,
        normalized(password),
        slot.salt,
        opslimit=slot.opslimit,
        memlimit=slot.memlimit,
    )
    try:
        key = secret.Aead(wrapping_key).decrypt(slot.box, _aad(slot_name))
    except exceptions.CryptoError as error:
        raise WrongPasswordError from error
    if len(key) != KEY_BYTES:
        raise KeyFileError("wrapped key has the wrong length")
    return key


def b64_field(value: object) -> bytes:
    if not isinstance(value, str):
        raise KeyFileError("expected base64 text")
    try:
        return base64.b64decode(value, validate=True)
    except binascii.Error as error:
        raise KeyFileError("invalid base64") from error


def _int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise KeyFileError("expected a positive integer")
    return value


def decode_slot(value: object) -> PasswordSlot:
    if not isinstance(value, dict):
        raise KeyFileError("password slot is not an object")
    fields = cast(dict[str, object], value)
    if fields.get("kdf") != "argon2id":
        raise KeyFileError("unknown key derivation")
    set_at = fields.get("set_at")
    return PasswordSlot(
        opslimit=_int(fields.get("opslimit")),
        memlimit=_int(fields.get("memlimit")),
        salt=b64_field(fields.get("salt")),
        box=b64_field(fields.get("box")),
        set_at=set_at if isinstance(set_at, str) else None,
    )


def parse_document(data: bytes) -> dict[str, object]:
    """A key file's contents, checked: this version's format, with its slots."""
    try:
        parsed: object = json.loads(data)
    except (ValueError, UnicodeDecodeError) as error:
        raise KeyFileError("not JSON") from error
    if not isinstance(parsed, dict):
        raise KeyFileError("not an object")
    document = cast(dict[str, object], parsed)
    if document.get("format") != FORMAT:
        raise KeyFileError("unknown format")
    if not isinstance(document.get("slots"), dict):
        raise KeyFileError("no slots")
    return document


def slots_of(document: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], document["slots"])


def key_file_of(slots: dict[str, object]) -> KeyFile:
    password = slots.get(PASSWORD)
    return KeyFile(
        password=None if password is None else decode_slot(password),
        other_slots={name: v for name, v in slots.items() if name != PASSWORD},
    )


def read_key_file(path: Path) -> KeyFile | None:
    """The key file, or None when there is none."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return key_file_of(slots_of(parse_document(data)))


def encode_slot(slot: PasswordSlot) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "kdf": "argon2id",
        "opslimit": slot.opslimit,
        "memlimit": slot.memlimit,
        "salt": base64.b64encode(slot.salt).decode("ascii"),
        "box": base64.b64encode(slot.box).decode("ascii"),
    }
    if slot.set_at is not None:
        fields["set_at"] = slot.set_at
    return fields


def key_file_slots(key_file: KeyFile) -> dict[str, Any]:
    slots: dict[str, Any] = dict(key_file.other_slots)
    if key_file.password is not None:
        slots[PASSWORD] = encode_slot(key_file.password)
    return slots


def write_key_file(path: Path, key_file: KeyFile) -> None:
    """Replace the file atomically, flushed to disk before and after the swap."""
    write_slots(path, key_file_slots(key_file))


DOCUMENT_FIELDS = ("warning", "format", "slots")


def document_bytes(slots: dict[str, Any], kept: dict[str, object] | None = None) -> bytes:
    """A key file with these slots and the fields kept from the one it replaces
    (those this version does not know), the warning first, where it is seen."""
    document: dict[str, object] = {"warning": WARNING}
    document.update((name, v) for name, v in (kept or {}).items() if name not in DOCUMENT_FIELDS)
    document["format"] = FORMAT
    document["slots"] = slots
    # Not escaped, so the Korean line reads as such to whoever opens the file.
    return json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8")


def write_slots(path: Path, slots: dict[str, Any]) -> None:
    """A key file with these slots, replacing any atomically and durably."""
    write_bytes(path, document_bytes(slots))


def write_bytes(path: Path, data: bytes) -> None:
    """A key file's bytes, replacing any atomically and durably."""
    temporary = path.with_name(path.name + ".new")
    try:
        with temporary.open("wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    if sys.platform != "win32":
        # Make the rename itself durable.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
