"""The master password: a second way back, which the user remembers.

Like the recovery key, it opens a key pair (stickle.crypto.sealed) in the key
file B, and the notes' database key is sealed for that pair; later the secret
notes' key is too, which the recovery key never opens. It is optional, set in
the settings, and never removed. Changing it rewraps the pair's secret key
alone: whatever was sealed for the pair still opens. Set again without the
current one (forgotten), it is a new pair.

Its key is derived with Argon2id's "sensitive" limits (about a second): it is
typed rarely, and guarding the most is worth the wait.
"""

from stickle.crypto.key_files import B, KeyFiles
from stickle.crypto.keyfile import KeyFileError, unwrap_with_password, wrap_with_password
from stickle.crypto.sealed import (
    SealedSlot,
    decode_sealed_slot,
    encode_sealed_slot,
    make_sealed_slot,
    open_sealed_slot,
)

SLOT = "master_password"
OPSLIMIT = 4  # libsodium's argon2id OPSLIMIT_SENSITIVE
MEMLIMIT = 1024 * 1024 * 1024  # MEMLIMIT_SENSITIVE


def read_master(files: KeyFiles) -> SealedSlot | None:
    """The master password's slot, or None without one (KeyFileError if damaged)."""
    slots = files.load(B)
    if slots is None or SLOT not in slots:
        return None
    return decode_sealed_slot(slots[SLOT])


def _slots(files: KeyFiles) -> dict[str, object]:
    try:
        return dict(files.load(B) or {})
    except KeyFileError:
        files.set_aside(B)
        return {}


def set_master(
    files: KeyFiles,
    database_key: bytes,
    password: str,
    set_at: str,
    strength: tuple[int, int] = (OPSLIMIT, MEMLIMIT),
) -> None:
    """A new key pair opened by password, with database_key sealed for it, in
    place of any earlier one; the file's other slots stay."""
    slots = _slots(files)
    slots[SLOT] = encode_sealed_slot(
        make_sealed_slot(database_key, password, *strength, SLOT, set_at)
    )
    files.save(B, slots)


def change_master(
    files: KeyFiles,
    current: str,
    new: str,
    set_at: str,
    strength: tuple[int, int] = (OPSLIMIT, MEMLIMIT),
) -> None:
    """The same pair, opened by new instead of current (WrongPasswordError if
    current is not it)."""
    slot = read_master(files)
    if slot is None:
        raise KeyFileError("no master password to change")
    secret = unwrap_with_password(slot.secret, current, SLOT)
    rewrapped = wrap_with_password(secret, new, *strength, slot_name=SLOT, set_at=set_at)
    slots = _slots(files)
    slots[SLOT] = encode_sealed_slot(SealedSlot(rewrapped, slot.public, slot.sealed))
    files.save(B, slots)


def open_with_master(slot: SealedSlot, typed: str) -> bytes:
    """The database key; WrongPasswordError, or KeyFileError if the slot was altered."""
    return open_sealed_slot(slot, typed, SLOT)
