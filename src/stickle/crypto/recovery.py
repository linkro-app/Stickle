"""The recovery key: a way back to the notes when the usual key is lost.

It opens a key pair whose public key the notes database key is sealed for
(stickle.crypto.sealed), in the key file B (stickle.crypto.key_files). The
recovery key itself is never stored or logged: whoever has it can open the
notes, and only the user keeps it. Recovery keys made before 0.6 wrapped the
database key directly; they still open it, until a new one is made.

It reads like K7QM-2HXP-…: 32 characters in groups of four, from an
alphabet without look-alikes (Crockford's base 32: no I, L, O or U).
Typing O for 0 or I and L for 1 is read as meant, letter case, spaces and
dashes do not matter, and the last character checks the others, so a
mistyped key is told apart from a wrong one. 31 characters carry 155
random bits.

Making a new recovery key replaces the earlier one's slot (a new key pair),
so the old key stops working; the file's other slots stay.
"""

import hashlib
import secrets

from stickle.crypto.key_files import B, KeyFiles
from stickle.crypto.keyfile import (
    KeyFileError,
    PasswordSlot,
    WrongPasswordError,
    decode_slot,
    unwrap_with_password,
)
from stickle.crypto.sealed import (
    SealedSlot,
    decode_sealed_slot,
    encode_sealed_slot,
    make_sealed_slot,
    open_sealed_slot,
)

SLOT = "recovery_key"  # a key pair (0.6)
OLD_SLOT = "recovery"  # the database key wrapped directly (until 0.5)
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
DATA_CHARS = 31
GROUP = 4
# The key is long and random, so light key stretching is enough (libsodium's
# "interactive" limits); it still costs anyone guessing, and opens quickly.
OPSLIMIT = 2
MEMLIMIT = 64 * 1024 * 1024
LOOK_ALIKES = str.maketrans({"O": "0", "I": "1", "L": "1"})


class RecoveryKeyTypoError(ValueError):
    """Not a recovery key as written: a character is missing, extra or mistyped."""


class WrongRecoveryKeyError(Exception):
    """A well-formed key, but not the one that goes with these notes."""


def _check(data: str) -> str:
    return ALPHABET[hashlib.blake2b(data.encode("ascii"), digest_size=1).digest()[0] % 32]


def generate() -> str:
    data = "".join(secrets.choice(ALPHABET) for _ in range(DATA_CHARS))
    return format_key(data + _check(data))


def format_key(canonical: str) -> str:
    return "-".join(canonical[i : i + GROUP] for i in range(0, len(canonical), GROUP))


def canonical(typed: str) -> str:
    """The key as its 32 characters, or RecoveryKeyTypoError."""
    text = "".join(typed.split()).replace("-", "").upper().translate(LOOK_ALIKES)
    if len(text) != DATA_CHARS + 1 or any(c not in ALPHABET for c in text):
        raise RecoveryKeyTypoError("not 32 characters of a recovery key")
    if _check(text[:DATA_CHARS]) != text[DATA_CHARS]:
        raise RecoveryKeyTypoError("the check character does not match")
    return text


type RecoverySlot = SealedSlot | PasswordSlot  # the latter from before 0.6


def write_recovery(files: KeyFiles, database_key: bytes, recovery_key: str, set_at: str) -> None:
    """A new key pair opened by recovery_key, with database_key sealed for it,
    in place of any earlier recovery key. A damaged file is set aside first."""
    try:
        slots = dict(files.load(B) or {})
    except KeyFileError:
        files.set_aside(B)
        slots = {}
    slots.pop(OLD_SLOT, None)
    slot = make_sealed_slot(database_key, canonical(recovery_key), OPSLIMIT, MEMLIMIT, SLOT, set_at)
    slots[SLOT] = encode_sealed_slot(slot)
    files.save(B, slots)


def read_recovery(files: KeyFiles) -> RecoverySlot | None:
    """The recovery key's slot, or None without one (KeyFileError if damaged)."""
    slots = files.load(B)
    if slots is None:
        return None
    if SLOT in slots:
        return decode_sealed_slot(slots[SLOT])
    if OLD_SLOT in slots:
        return decode_slot(slots[OLD_SLOT])
    return None


def unwrap(slot: RecoverySlot, typed: str) -> bytes:
    """The database key; RecoveryKeyTypoError or WrongRecoveryKeyError otherwise."""
    try:
        if isinstance(slot, SealedSlot):
            return open_sealed_slot(slot, canonical(typed), SLOT)
        return unwrap_with_password(slot, canonical(typed), slot_name=OLD_SLOT)
    except WrongPasswordError as error:
        raise WrongRecoveryKeyError from error
    except KeyFileError as error:
        raise WrongRecoveryKeyError from error
