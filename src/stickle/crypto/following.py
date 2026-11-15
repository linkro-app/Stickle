"""The recovery key and the master password as every computer that syncs has
them: one of each, the vault's (stickle.crypto.vault_keys).

The vault's keys/ holds each as the key file B does: the pair, its secret key
wrapped with what the user types. A device that joins, or finds that another
made a new recovery key or set its master password, takes that pair for its own:
B's slot of that kind becomes it, with the notes' database key sealed for it.
The old recovery key no longer opens this computer's notes, and so it is told
(where sync decides it: stickle.sync.keys). Only the app's thread writes B; the
thread of sync looks at it (KeyFiles.peek) and says what to take.

The secret notes' key is not B's: still sealed for the old pair, it keeps that
pair beside it (stickle.crypto.secret_notes.keep_pair_aside) until it is sealed
for the new one, the next time the secret notes open with their password.
"""

import logging
from collections.abc import Mapping

from stickle.crypto import recovery, secret_notes
from stickle.crypto.key_files import B, KeyFiles
from stickle.crypto.keyfile import KeyFileError
from stickle.crypto.sealed import SealedSlot, decode_sealed_slot, encode_sealed_slot, seal
from stickle.crypto.vault_keys import RECEIVERS, RECOVERY_KEY, pair_digest

log = logging.getLogger(__name__)


def pairs_in(slots: Mapping[str, object] | None) -> dict[str, SealedSlot]:
    """The pairs the key file B holds, by kind (a recovery key from before 0.6,
    wrapping the database key itself, is none). KeyFileError for one damaged."""
    if slots is None:
        return {}
    return {kind: decode_sealed_slot(slots[kind]) for kind in RECEIVERS if kind in slots}


def kept_pairs(files: KeyFiles) -> dict[str, SealedSlot]:
    """B's pairs as they are now, looked at from any thread (KeyFiles.peek):
    KeyFileError or OSError if B does not read now, or is not there at all
    (while the notes are open, it is put back from a copy)."""
    slots = files.peek(B)
    if slots is None:
        raise KeyFileError("no key file B")
    return pairs_in(slots)


def digest_of(pair: SealedSlot | None) -> str | None:
    return None if pair is None else pair_digest(pair)


def follow(
    files: KeyFiles,
    kind: str,
    pair: SealedSlot,
    database_key: bytes,
    seen: str | None,
    before_sync: bool = False,
) -> bool:
    """B takes pair for kind (its secret key as wrapped, its public key), the
    database key sealed for it, if B's pair of that kind is still the one seen
    (its digest; None: B had none). False, nothing written, if it changed since
    (a new one made here meanwhile: the next round decides again). For the
    recovery key, the secret notes' key sealed for the pair replaced keeps it
    first (before_sync: this computer's own, from before it joined): should
    that fail to be written (OSError), B is left as it was. KeyFileError for B
    damaged, with no sound copy."""
    slots = dict(files.load(B) or {})
    current = pairs_in(slots).get(kind)
    if digest_of(current) != seen:
        return False
    if kind == RECOVERY_KEY and current is not None:
        try:
            secret_notes.keep_pair_aside(files, current, before_sync)
        except KeyFileError as error:  # damaged, never written over: nothing to keep it in
            log.warning("secret notes' old recovery key not kept: %s", type(error).__name__)
    slots[kind] = encode_sealed_slot(
        SealedSlot(pair.secret, pair.public, seal(pair.public, database_key))
    )
    if kind == RECOVERY_KEY:
        slots.pop(recovery.OLD_SLOT, None)
    files.save(B, slots)
    return True
