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
bound to the note's id: moved onto another note, it does not open. A text
that comes from another computer keeps the id it was bound to beside it
(stickle.core.note.SealedText): a conflict copy's opens as its original's did.

    {"slots": {"key_number": "<a random id>", "secret_password": {..., "how": "changed"},
               "recovery_key": {...}}}

The key's number and how its password was set are slots and fields 0.6 does
not know: kept as they are when it writes C again.

A device that syncs takes the recovery key of the computers it syncs with for
its own (stickle.crypto.following): its pair in B is replaced, and the key sealed
for the old one could no longer be opened by the old recovery key. So the pair
it is sealed for is kept beside it first, its secret key as wrapped with the old
recovery key ("pair"), and "before_sync" if that was the recovery key of this
computer before it joined the others; once the key is sealed for the new pair
(keep_for_recovery_key), the old one is no longer kept.

    "recovery_key": {"public": ..., "sealed": ..., "pair": <a password slot>,
                     "before_sync": true}

A computer that had secret notes of its own before it synced with others that
have theirs keeps its key until the two passwords are matched: the others' key
waits beside it, by its number, as its slots are in the vault ("others"), so
that their notes open here with their password too. Matched, the others' key is
this computer's: its slots take the place of the old ones, and the old key goes
among the old keys, encrypted with the key of now ("old_keys"), with any that
were there before: earlier texts, old backups and exports sealed with it open
still, with the password of now.

    "others": {"<number>": {"secret_password": <a password slot>,
                            "recovery_key": {"public": ..., "sealed": ...},
                            "vault": false, "by": "<a computer's name>"}},
    "old_keys": {"<number>": "<that key, encrypted with this one>"}

Another key may wait there too: one another computer sealed its own secret
notes with apart, which went to the vault all the same (a server that does not
keep to conditional writes let two computers make the vault's key at once).
It is kept only by a computer that holds notes sealed with it, marked as not
the vault's ("vault": false), with the name of the computer that used it. It is
never matched with: its password typed, the notes sealed with it are sealed
again with this computer's key, and it goes among the old keys (adopt).
"""

import base64
import hashlib
import json
import secrets
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, cast

from stickle.core.note import SealedText
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
RECOVERY_SLOT = "recovery_key"
ASIDE = "pair"  # the pair the key is sealed for, kept once B no longer has it
BEFORE_SYNC = "before_sync"  # that pair was this computer's before it joined
# Which key it is: a random id given when it is made (or, made by 0.6, when 0.7
# first starts), the same on every computer that has it, so that a key file of
# another computer is taken only for the same key (stickle.sync.secret_key).
KEY_NUMBER = "key_number"
# The others' key, while this computer keeps its own; the keys sealed with before.
OTHERS = "others"
OLD_KEYS = "old_keys"
# In an entry of OTHERS: whether it is the vault's key (else one used apart, by
# the computer named "by"). An entry without it is the vault's.
VAULT = "vault"
BY = "by"
# How the password was set, in its slot: changed, or set anew with the recovery key.
HOW = "how"
CHANGED = "changed"
RESET = "reset"
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


def open_sealed(key: bytes, note_id: str, sealed: SealedText) -> str:
    """The text of the note note_id, sealed as it holds it (bound to it, or to
    the note it says)."""
    return open_text(key, sealed.opens_as(note_id), sealed.data)


def open_with_any(keys: Iterable[bytes], note_id: str, sealed: SealedText) -> str:
    """The text of the note note_id, with the first of keys it opens with (the
    one it was sealed with first, best): SecretNoteError if none."""
    for key in keys:
        try:
            return open_sealed(key, note_id, sealed)
        except SecretNoteError:
            continue
    raise SecretNoteError(note_id)


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
        C,
        {
            KEY_NUMBER: new_key_number(),
            PASSWORD_SLOT: encode_slot(slot),
            RECOVERY_SLOT: _recovery_seal(recovery_public, key),
        },
    )
    return key


def new_key_number() -> str:
    return str(uuid.uuid4())


def key_number(slots: Mapping[str, object] | None) -> str | None:
    """The key's number in C's slots (None: none, or not one)."""
    number = None if slots is None else slots.get(KEY_NUMBER)
    return number if isinstance(number, str) and _is_number(number) else None


def _is_number(text: str) -> bool:
    try:
        return str(uuid.UUID(text)) == text
    except ValueError:
        return False


def number_the_key(files: KeyFiles) -> bool:
    """A key made by 0.6 given its number, once; whether it was now. Nothing
    without secret notes, or with C damaged (never written over)."""
    if files.raw(C) is None:
        return False
    slots = _slots(files)
    if key_number(slots) is not None:
        return False
    slots[KEY_NUMBER] = new_key_number()
    _save(files, slots)
    return True


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


def _exported_slots(document: object) -> dict[str, object]:
    """The slots of C as it went out with an export (its whole document)."""
    if not isinstance(document, dict):
        raise KeyFileError("no secret notes' key in the export")
    slots = cast(dict[str, object], document).get("slots")
    if not isinstance(slots, dict):
        raise KeyFileError("no secret notes' key in the export")
    return cast(dict[str, object], slots)


def open_exported_with_password(document: object, password: str) -> bytes:
    """The key of secret notes exported encrypted, by their password of that day
    (WrongPasswordError otherwise)."""
    slots = _exported_slots(document)
    if PASSWORD_SLOT not in slots:
        raise KeyFileError("no password slot in the export")
    return unwrap_with_password(decode_slot(slots[PASSWORD_SLOT]), password, PASSWORD_SLOT)


def open_exported_with_pair(document: object, pair_secret: bytes, pair_public: bytes) -> bytes:
    """The key of secret notes exported encrypted, by the recovery key's pair of
    that day (stickle.crypto.recovery.open_exported_pair);
    NotKeptForRecoveryKeyError if it is not sealed for that pair."""
    slots = _exported_slots(document)
    if not isinstance(slots.get(RECOVERY_SLOT), dict):
        raise NotKeptForRecoveryKeyError("not kept for a recovery key")
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
    how: str = CHANGED,
) -> None:
    """The same key, opened by a new password in place of the old one (the key
    known: opened with the old password, or the recovery key: how is RESET)."""
    slots = _slots(files)
    slot = wrap_with_password(key, password, *strength, slot_name=PASSWORD_SLOT, set_at=set_at)
    slots[PASSWORD_SLOT] = encode_slot(slot) | {HOW: how}
    _save(files, slots)


def keep_for_recovery_key(files: KeyFiles, key: bytes, recovery_public: bytes) -> None:
    """The key sealed for a recovery key's pair, in place of any earlier one
    (the key known): a pair kept aside is no longer."""
    slots = _slots(files)
    slots[RECOVERY_SLOT] = _recovery_seal(recovery_public, key)
    _save(files, slots)


def _recovery_fields(files: KeyFiles) -> dict[str, object] | None:
    slots = files.load(C)
    if slots is None or not isinstance(slots.get(RECOVERY_SLOT), dict):
        return None
    return dict(cast(dict[str, object], slots[RECOVERY_SLOT]))


def keep_pair_aside(files: KeyFiles, pair: SealedSlot, before_sync: bool) -> None:
    """Before the recovery key's pair in B is replaced by another (sync): the
    key, if sealed for that pair, keeps it beside it, so that its recovery key
    still opens the secret notes. before_sync: it is this computer's own, from
    before it joined the computers it syncs with. Nothing without secret notes,
    or with the key sealed for another pair (one kept aside already stays)."""
    if files.raw(C) is None:
        return
    fields = _recovery_fields(files)
    if fields is None or b64_field(fields.get("public")) != pair.public or ASIDE in fields:
        return
    fields[ASIDE] = encode_slot(pair.secret)
    if before_sync:
        fields[BEFORE_SYNC] = True
    slots = _slots(files)
    slots[RECOVERY_SLOT] = fields
    _save(files, slots)


def pair_aside(files: KeyFiles) -> bool | None:
    """Whether the key is sealed for a pair kept aside (no longer B's): None if
    not; else whether it is the one from before this computer joined the others."""
    try:
        fields = _recovery_fields(files)
    except KeyFileError:
        return None
    if fields is None or ASIDE not in fields:
        return None
    return fields.get(BEFORE_SYNC) is True


def open_with_pair_aside(files: KeyFiles, typed: str) -> bytes:
    """The key, through the pair kept aside, by the recovery key it was made
    with (canonical); NotKeptForRecoveryKeyError if none is, WrongPasswordError
    if typed is not that recovery key."""
    fields = _recovery_fields(files)
    if fields is None or ASIDE not in fields:
        raise NotKeptForRecoveryKeyError("no pair kept aside")
    public = b64_field(fields.get("public"))
    secret = open_pair(SealedSlot(decode_slot(fields[ASIDE]), public, b""), typed, RECOVERY_SLOT)
    return unseal(secret, b64_field(fields.get("sealed")))


def _recovery_of(slots: Mapping[str, object]) -> dict[str, object] | None:
    fields = slots.get(RECOVERY_SLOT)
    if not isinstance(fields, dict):
        return None
    return {name: value for name, value in cast(dict[str, object], fields).items()}


def seen_digest(slots: Mapping[str, object] | None) -> str | None:
    """What tells C as one round saw it from C as the app finds it later: its
    key's number, its password slot and what is sealed for the recovery key
    (not a pair kept beside it). None without C."""
    if slots is None:
        return None
    recovery = _recovery_of(slots) or {}
    told = {
        KEY_NUMBER: slots.get(KEY_NUMBER),
        PASSWORD_SLOT: slots.get(PASSWORD_SLOT),
        RECOVERY_SLOT: {name: recovery.get(name) for name in ("public", "sealed")},
    }
    text = json.dumps(told, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(b"stickle secret notes key\0" + text.encode()).hexdigest()


def take(
    files: KeyFiles,
    number: str,
    password: Mapping[str, object] | None,
    recovery: Mapping[str, object] | None,
    seen: str | None,
) -> bool:
    """C takes the slots of the computers this one syncs with for the key
    numbered number: its password slot, and what is sealed for the recovery
    key (None: that one stays). Without C, C is made of them (both given). Only
    if C is still as seen (seen_digest), and holds that key: False otherwise,
    nothing written. A pair kept beside the recovery key's slot stays only while
    the slot taken is sealed for it. KeyFileError for slots that do not read."""
    if password is not None:
        decode_slot(dict(password))
    if recovery is not None:
        b64_field(recovery.get("public"))
        b64_field(recovery.get("sealed"))
    current = files.load(C)
    if seen_digest(current) != seen:
        return False
    if current is None:
        if password is None or recovery is None:
            raise KeyFileError("a new C needs both slots")
        files.save(
            C,
            {KEY_NUMBER: number, PASSWORD_SLOT: dict(password), RECOVERY_SLOT: dict(recovery)},
        )
        return True
    slots = dict(current)
    if key_number(slots) != number:
        return False
    if password is not None:
        slots[PASSWORD_SLOT] = dict(password)
    if recovery is not None:
        before = _recovery_of(slots) or {}
        taken: dict[str, object] = {
            name: value for name, value in recovery.items() if name not in (ASIDE, BEFORE_SYNC)
        }
        if ASIDE in before and before.get("public") == taken.get("public"):
            taken |= {name: before[name] for name in (ASIDE, BEFORE_SYNC) if name in before}
        slots[RECOVERY_SLOT] = taken
    _save(files, slots)
    return True


# The others' key, and matching the passwords


def others_of(slots: Mapping[str, object] | None) -> dict[str, dict[str, object]]:
    """The others' keys C keeps beside its own, by number: their slots."""
    found = None if slots is None else slots.get(OTHERS)
    if not isinstance(found, dict):
        return {}
    return {
        str(number): dict(cast(dict[str, object], each))
        for number, each in cast(dict[str, object], found).items()
        if isinstance(each, dict) and _is_number(str(number))
    }


def others(files: KeyFiles) -> dict[str, dict[str, object]]:
    """The others' keys C keeps (none without C, or with C damaged)."""
    try:
        return others_of(files.load(C))
    except KeyFileError:
        return {}


def kept_entry(
    password: Mapping[str, object],
    recovery: Mapping[str, object],
    vault: bool = True,
    by: str | None = None,
) -> dict[str, object]:
    """An entry of OTHERS as C keeps it: the vault's key (vault), or one another
    computer (named by) used apart."""
    entry: dict[str, object] = {PASSWORD_SLOT: dict(password), RECOVERY_SLOT: dict(recovery)}
    if not vault:
        entry |= {VAULT: False, BY: by}
    return entry


def is_the_vaults(entry: Mapping[str, object]) -> bool:
    return entry.get(VAULT) is not False


def keep_other(
    files: KeyFiles,
    number: str,
    password: Mapping[str, object],
    recovery: Mapping[str, object],
    vault: bool = True,
    by: str | None = None,
) -> bool:
    """C keeps the slots of the others' key numbered number beside its own,
    in place of what it kept of it: the vault's (vault), or one the computer
    named by used apart. Whether it changed. Not for its own key (False), nor
    without C. KeyFileError for slots that do not read."""
    decode_slot(dict(password))
    b64_field(recovery.get("public"))
    b64_field(recovery.get("sealed"))
    slots = _slots(files)
    if key_number(slots) in (None, number) or not _is_number(number):
        return False
    kept = others_of(slots)
    wanted = kept_entry(password, recovery, vault, by)
    if kept.get(number) == wanted:
        return False
    slots[OTHERS] = kept | {number: wanted}
    _save(files, slots)
    return True


def adopt(files: KeyFiles, own: bytes, number: str, key: bytes) -> None:
    """A key another computer used apart (numbered number, opened: key) no
    longer kept beside: the notes sealed with it are sealed with this computer's
    (own) now, and it goes among the old keys, encrypted with own."""
    slots = _slots(files)
    kept = others_of(slots)
    if kept.pop(number, None) is None or key_number(slots) in (None, number):
        raise KeyFileError("no such key kept")
    found = slots.get(OLD_KEYS)
    old = dict(cast(dict[str, object], found)) if isinstance(found, dict) else {}
    old[number] = _seal_old(own, number, key)
    slots[OLD_KEYS] = old
    if kept:
        slots[OTHERS] = kept
    else:
        slots.pop(OTHERS, None)
    _save(files, slots)


def _other(files: KeyFiles, number: str) -> dict[str, object]:
    kept = others(files).get(number)
    if kept is None:
        raise KeyFileError("no such key kept")
    return kept


def open_other_with_password(files: KeyFiles, number: str, password: str) -> bytes:
    """The others' key numbered number; WrongPasswordError if password is not theirs."""
    slot = decode_slot(_other(files, number).get(PASSWORD_SLOT))
    return unwrap_with_password(slot, password, PASSWORD_SLOT)


def open_other_with_pair(
    files: KeyFiles, number: str, pair_secret: bytes, pair_public: bytes
) -> bytes:
    """The others' key, through the recovery key's pair it is sealed for;
    NotKeptForRecoveryKeyError if it is sealed for another."""
    fields = _other(files, number).get(RECOVERY_SLOT)
    if not isinstance(fields, dict):
        raise NotKeptForRecoveryKeyError("not kept for a recovery key")
    sealed = cast(dict[str, object], fields)
    if b64_field(sealed.get("public")) != pair_public:
        raise NotKeptForRecoveryKeyError("kept for another recovery key")
    return unseal(pair_secret, b64_field(sealed.get("sealed")))


def _public(sealed: object) -> bytes | None:
    """The public key a slot sealed for a recovery key is sealed for (None: not one)."""
    if not isinstance(sealed, dict):
        return None
    try:
        return b64_field(cast(dict[str, object], sealed).get("public"))
    except KeyFileError:
        return None


def _old_aad(number: str) -> bytes:
    return f"stickle old secret notes key {number}".encode()


def _seal_old(key: bytes, number: str, old: bytes) -> str:
    from nacl import secret

    return base64.b64encode(secret.Aead(key).encrypt(old, _old_aad(number))).decode("ascii")


def old_numbers(slots: Mapping[str, object] | None) -> frozenset[str]:
    """The numbers of the keys sealed with before that C keeps (nothing opened)."""
    found = None if slots is None else slots.get(OLD_KEYS)
    if not isinstance(found, dict):
        return frozenset()
    return frozenset(str(number) for number in cast(dict[str, object], found))


def old_keys_of(slots: Mapping[str, object] | None, key: bytes) -> dict[str, bytes]:
    """The keys sealed with before, by number, opened with the key of now (one
    that does not open with it is left out)."""
    from nacl import exceptions, secret

    found = None if slots is None else slots.get(OLD_KEYS)
    if not isinstance(found, dict):
        return {}
    opened: dict[str, bytes] = {}
    for number, sealed in cast(dict[str, object], found).items():
        try:
            old = secret.Aead(key).decrypt(b64_field(sealed), _old_aad(str(number)))
        except KeyFileError, exceptions.CryptoError, ValueError:
            continue
        if len(old) == KEY_BYTES:
            opened[str(number)] = old
    return opened


def old_keys(files: KeyFiles, key: bytes) -> dict[str, bytes]:
    """The keys C keeps of before, opened with key (its key of now)."""
    try:
        return old_keys_of(files.load(C), key)
    except KeyFileError:
        return {}


def keeps_old_keys(files: KeyFiles) -> bool:
    """Whether C keeps keys of before (which open only with its key of now)."""
    try:
        slots = files.load(C)
    except KeyFileError:
        return True  # damaged: not to be written over as if it kept none
    return slots is not None and isinstance(slots.get(OLD_KEYS), dict)


def match(
    files: KeyFiles,
    own: bytes | None,
    number: str,
    key: bytes,
    recovery_public: bytes | None = None,
) -> None:
    """The passwords matched: the others' key numbered number (key, opened)
    is this computer's from now on, its slots as the others have them — but
    sealed for the recovery key of recovery_public (this computer's now) if
    theirs is sealed for another (one made here since, say: the others' is
    sealed for it once they follow). This computer's key till now (own; None
    when nothing was sealed with it) and the ones it kept of before go among
    the old keys, encrypted with it. The others' key is no longer kept beside it."""
    slots = _slots(files)
    kept = others_of(slots)
    theirs = kept.pop(number, None)
    mine = key_number(slots)
    if theirs is None or not is_the_vaults(theirs) or mine is None or mine == number:
        raise KeyFileError("no such key to match with")
    if own is None and OLD_KEYS in slots:
        raise KeyFileError("the keys of before open only with this computer's")
    old = {} if own is None else old_keys_of(slots, own)
    if own is not None:
        old[mine] = own
    old.pop(number, None)
    matched: dict[str, object] = {
        name: value
        for name, value in slots.items()
        if name not in (PASSWORD_SLOT, RECOVERY_SLOT, OTHERS, OLD_KEYS, KEY_NUMBER)
    }
    sealed = theirs[RECOVERY_SLOT]
    if recovery_public is not None and _public(sealed) != recovery_public:
        sealed = _recovery_seal(recovery_public, key)
    matched |= {KEY_NUMBER: number, PASSWORD_SLOT: theirs[PASSWORD_SLOT], RECOVERY_SLOT: sealed}
    if old:
        matched[OLD_KEYS] = {each: _seal_old(key, each, old_key) for each, old_key in old.items()}
    if kept:
        matched[OTHERS] = kept
    _save(files, matched)
