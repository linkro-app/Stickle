"""The secret notes' key, one for every computer that syncs: its key file C
(stickle.crypto.secret_notes) as a record of the vault, and what a round does
with it.

The record is in records/, locked with the data key as every record is: a
cloud cannot read the secret notes' key from it, try passwords on it, or put
another key in its place. Its id is a fixed name's hash (DataKeys.record_id).

    {"record": "secret_notes_key",
     "fields": {"current": {"value": "<key number>", ...},
                "password.<key number>": {"value": <the password slot>, ...},
                "recovery.<key number>": {"value": {"public": ..., "sealed": ...}, ...},
                "box.<key number>": {"value": {"public": ...}, ...}}}

Each field is {value, hlc, device}, as every record's (stickle.sync.contents):
the time a slot was set, in milliseconds, and the device that set it. "current"
is the vault's key, set when the record is made and never changed by this
version; the other fields are the slots of each key, by its number, so two keys
put at once never mix.

A round looks at it when it may have changed (on a full look, when a device
said it put it, when this device's C is not as last agreed on), and:

- the vault has none and this device has C: it is made, only where there is
  none (a device that finds one made meanwhile takes it as below);
- only the vault has it: this device takes it for its C (its secret notes
  turned on, with the same password and recovery key as the others);
- both, the same key: the password slot as the recovery key's and spare
  password's pairs are (stickle.sync.keys): changed here only, put; there
  only, taken and told (who, when, and whether it was set anew with the
  recovery key); on both, the later. What is sealed for the recovery key: the
  slot sealed for the vault's recovery key, as this device has agreed on it,
  over one that is not, either way; else as it is;
- another key: the vault's kept beside this device's own, in C (SecretOther),
  until the user matches their passwords (stickle.app.secret_dialogs.settle);
  and if notes sealed with this device's own went to the vault already (two
  devices made the vault's key at once, on a server that does not keep to
  conditional writes), its own slots put in the record, by its number, so
  that they open without it.

A key another device used apart, whose slots are in the record, is kept beside
C too by a device that holds notes sealed with it (not as the vault's: their
notes are sealed again with this device's key once its password is typed).

Each key's box (its public key, stickle.crypto.secret_notes) goes with its
slots, put by a device whose C has it and the record not, and taken by one
whose C has none: a device that never held the key seals into it what a note
made secret elsewhere left here in the clear (vault_boxes, for a round that
needs one its C does not have yet).

Only the app's thread writes C: what this device is to take goes back to it
(SecretFollow), taken only if C is still as the round saw it. The vault's key
is kept as each round sees it: the secret notes go to the vault sealed with it
only (stickle.sync.engine).
"""

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

import apsw

from stickle.core.clock import millis_at
from stickle.core.hlc import Hlc
from stickle.crypto.following import digest_of
from stickle.crypto.keyfile import KeyFileError, b64_field, decode_slot
from stickle.crypto.sealed import SealedSlot
from stickle.crypto.secret_notes import (
    BOX,
    HOW,
    KEY_NUMBER,
    PASSWORD_SLOT,
    RECOVERY_SLOT,
    RESET,
    box_of,
    kept_entry,
    key_number,
    others_of,
    seen_digest,
)
from stickle.crypto.vault_keys import RECOVERY_KEY, DataKeys
from stickle.data import sync_keys, sync_state
from stickle.data.sync_keys import Agreed
from stickle.sync.contents import Contents, Field
from stickle.sync.format import (
    RECORDS,
    FormatError,
    Json,
    item_path,
    open_envelope,
    seal,
)
from stickle.sync.place import Place, writing
from stickle.sync.record_file import RecordFile
from stickle.sync.targets.base import Absent, AsRead, NotThereError, RefusedError

log = logging.getLogger(__name__)

KIND = "secret_notes_key"
NAME = "secret notes"
CURRENT = "current"
PASSWORD = "password."
RECOVERY = "recovery."
BOXES = "box."
# What this device and the vault last agreed on (stickle.data.sync_keys).
AGREED_PASSWORD = "secret_password"
AGREED_RECOVERY = "secret_recovery"
AGREED_BOX = "secret_box"
# What a password taken from the vault is told as (stickle.app.protection_changes).
CHANGED_TOLD = "secret_password_changed"
RESET_TOLD = "secret_password_reset"
TRIES = 3


def record_of(keys: DataKeys) -> str:
    """The id of the secret notes' key's record in the vault."""
    return keys.record_id(KIND, NAME)


@dataclass(frozen=True)
class SecretFollow:
    """The vault's slots of the secret notes' key for this device's C: its
    password slot, what is sealed for the recovery key (None: that one stays)."""

    number: str
    password: Mapping[str, object] | None
    recovery: Mapping[str, object] | None
    seen: str | None  # C as the round saw it (stickle.crypto.secret_notes.seen_digest)
    agreed: Mapping[str, Agreed]  # to keep as agreed on once taken, by what
    told: str | None  # what to tell of it (CHANGED_TOLD, RESET_TOLD); None: nothing
    by: str | None  # the name of the device that set the password taken
    at: str | None  # when it was set
    box: Mapping[str, object] | None = None  # the key's box, for a C without one


@dataclass(frozen=True)
class SecretOther:
    """The vault's key, another than this device's C holds, for C to keep beside
    its own until the passwords are matched (stickle.crypto.secret_notes.keep_other):
    its number and slots."""

    number: str
    password: Mapping[str, object]
    recovery: Mapping[str, object]
    vault: bool = True  # the vault's key; else one another device used apart
    by: str | None = None  # the name of the device that used it apart
    box: Mapping[str, object] | None = None  # its box, if the vault has it


@dataclass(frozen=True)
class _Vaults:
    current: str
    contents: Contents
    tag: str | None

    def slot(self, prefix: str) -> Mapping[str, object] | None:
        found = self.contents.fields.get(prefix + self.current)
        if found is None or not isinstance(found.value, dict):
            return None
        return cast(dict[str, object], found.value)

    def field(self, prefix: str) -> Field | None:
        return self.contents.fields.get(prefix + self.current)

    def slot_of(self, prefix: str, number: str) -> Mapping[str, object] | None:
        found = self.contents.fields.get(prefix + number)
        if found is None or not isinstance(found.value, dict):
            return None
        return cast(dict[str, object], found.value)

    def numbers(self) -> list[str]:
        """The numbers of the keys whose password slot the record has."""
        return sorted(
            name[len(PASSWORD) :] for name in self.contents.fields if name.startswith(PASSWORD)
        )


def _digest(number: str | None, slot: Mapping[str, object] | None) -> str | None:
    """Of a slot of the key numbered number (None without one)."""
    if number is None or slot is None:
        return None
    return seen_digest({KEY_NUMBER: number, PASSWORD_SLOT: dict(slot)})


def _recovery_digest(number: str | None, slot: Mapping[str, object] | None) -> str | None:
    if number is None or slot is None:
        return None
    sealed = {name: slot.get(name) for name in ("public", "sealed")}
    return seen_digest({KEY_NUMBER: number, RECOVERY_SLOT: sealed})


def _box_digest(number: str | None, box: Mapping[str, object] | None) -> str | None:
    """Of the box of the key numbered number (None without one)."""
    public = None if box is None else box_of({BOX: box})
    if number is None or public is None:
        return None
    return hashlib.sha256(b"stickle secret notes box\0" + number.encode() + public).hexdigest()


def _box(found: Mapping[str, object] | None) -> Mapping[str, object] | None:
    """A box as C or the vault keeps it, if it reads as one."""
    return found if found is not None and box_of({BOX: found}) is not None else None


def mine_box(slots: Mapping[str, object]) -> Mapping[str, object] | None:
    found = slots.get(BOX)
    return _box(cast(dict[str, object], found)) if isinstance(found, dict) else None


def _set_at(slot: Mapping[str, object] | None) -> str:
    when = None if slot is None else slot.get("set_at")
    return when if isinstance(when, str) else ""


def _hlc(set_at: str) -> Hlc:
    try:
        return Hlc(max(millis_at(set_at), 0))
    except ValueError:
        return Hlc(0)


def _public(slot: Mapping[str, object] | None) -> bytes | None:
    if slot is None:
        return None
    try:
        return b64_field(slot.get("public"))
    except KeyFileError:
        return None


def _readable(password: Mapping[str, object] | None, recovery: Mapping[str, object] | None) -> bool:
    """Whether the vault's slots read as slots (else they are not taken)."""
    try:
        if password is not None:
            decode_slot(dict(password))
        if recovery is not None:
            b64_field(recovery.get("public"))
            b64_field(recovery.get("sealed"))
    except KeyFileError:
        return False
    return True


class _SecretKey:
    def __init__(
        self,
        connection: apsw.Connection,
        place: Place,
        mine: Mapping[str, object] | None,
        sealed_for: bytes | None,
        name_of: Callable[[str], str | None],
    ) -> None:
        self._db = connection
        self._place = place
        self._mine = mine
        self._sealed_for = sealed_for  # the vault's recovery key's pair, as agreed on here
        self._name_of = name_of
        self.follows: list[SecretFollow | SecretOther] = []

    @property
    def _path(self) -> str:
        return item_path(RECORDS, record_of(self._place.keys))

    def read(self) -> _Vaults | None:
        """The vault's record; None if there is none. FormatError for one that
        does not open (left for a later round, as any record would be)."""
        place = self._place
        try:
            read = place.target.get(self._path)
        except NotThereError:
            return None
        _head, value = open_envelope(place.keys, read.data, place.vault, RECORDS)
        record = RecordFile.from_json(value)
        if record.kind != KIND:
            raise FormatError("not the secret notes' key")
        current = record.contents.fields.get(CURRENT)
        if current is None or not isinstance(current.value, str):
            raise FormatError("no key in the secret notes' key")
        return _Vaults(current.value, record.contents, read.tag)

    def run(self) -> None:
        for _ in range(TRIES):
            try:
                if self._once(self.read()):
                    return
            except RefusedError:
                continue  # made or changed meanwhile: read again
        log.info("sync: the secret notes' key left for a later round")

    def _once(self, theirs: _Vaults | None) -> bool:
        """What is done with the vault's record as read; False to read it again."""
        if theirs is not None:
            self._saw(theirs.current)
        mine = self._mine
        number = key_number(mine)
        if mine is not None and number is None:
            return True  # not numbered yet (the app does it as it starts)
        if theirs is None:
            if mine is None or number is None:
                return True
            return self._put(
                None, number, mine_password(mine), _recovery(mine), Absent(), mine_box(mine)
            )
        if mine is None:
            password, recovery = theirs.slot(PASSWORD), theirs.slot(RECOVERY)
            if password is None or recovery is None or not _readable(password, recovery):
                return True  # not one to take
            self._follow(theirs, password, recovery, None, told=False, box=_box(theirs.slot(BOXES)))
            return True
        if number is not None:
            self._apart(theirs, number, mine)
        if number is None or number != theirs.current:
            self._other(theirs, mine)  # another key: matched later, by the user
            return number is None or self._own_apart(theirs, number, mine)
        return self._same_key(theirs, number, mine)

    def _keeping(self, number: str) -> bool:
        return any(isinstance(each, SecretOther) and each.number == number for each in self.follows)

    def _other(self, theirs: _Vaults, mine: Mapping[str, object]) -> None:
        """The vault's key, kept beside this device's own (if it is not as kept)
        for their passwords to be matched."""
        password, recovery = theirs.slot(PASSWORD), theirs.slot(RECOVERY)
        if password is None or recovery is None or not _readable(password, recovery):
            return
        kept = others_of(mine).get(theirs.current)
        box = _box(theirs.slot(BOXES)) or (None if kept is None else mine_box(kept))
        wanted = kept_entry(password, recovery, box=box)
        if kept != wanted and not self._keeping(theirs.current):
            self.follows.append(SecretOther(theirs.current, password, recovery, box=box))

    def _apart(self, theirs: _Vaults, number: str, mine: Mapping[str, object]) -> None:
        """Keys other devices used apart, whose slots the record has, that notes
        here are sealed with: kept beside this device's (if not as kept), with
        the name of the device that put them."""
        kept = others_of(mine)
        for other in theirs.numbers():
            if other in (number, theirs.current) or not sync_state.sealed_with(self._db, other):
                continue
            password, recovery = theirs.slot_of(PASSWORD, other), theirs.slot_of(RECOVERY, other)
            if password is None or recovery is None or not _readable(password, recovery):
                continue
            field = theirs.contents.fields.get(PASSWORD + other)
            by = None if field is None else self._name_of(field.device)
            before = kept.get(other)
            box = _box(theirs.slot_of(BOXES, other)) or (
                None if before is None else mine_box(before)
            )
            wanted = kept_entry(password, recovery, vault=False, by=by, box=box)
            if before != wanted and not self._keeping(other):
                self.follows.append(
                    SecretOther(other, password, recovery, vault=False, by=by, box=box)
                )

    def _own_apart(self, theirs: _Vaults, number: str, mine: Mapping[str, object]) -> bool:
        """This device's own key, another than the vault's: its slots put in the
        record (if not there as they are) once notes sealed with it are in the
        vault, for them to open without this device. False to read it again."""
        if not sync_state.sealed_here_in_vault(self._db, self._place.vault):
            return True
        ours, ours_sealed = mine_password(mine), _recovery(mine)
        if ours is None or ours_sealed is None:
            return True
        there, there_sealed = theirs.slot_of(PASSWORD, number), theirs.slot_of(RECOVERY, number)
        our_box = mine_box(mine)
        box = our_box if _box(theirs.slot_of(BOXES, number)) is None else None
        if (
            _digest(number, ours) == _digest(number, there)
            and _recovery_digest(number, ours_sealed) == _recovery_digest(number, there_sealed)
            and box is None
        ):
            return True
        return self._put(theirs, number, ours, ours_sealed, AsRead(theirs.tag), box)

    def _same_key(self, theirs: _Vaults, number: str, mine: Mapping[str, object]) -> bool:
        was = sync_keys.agreed(self._db, self._place.vault)
        ours, their = mine_password(mine), theirs.slot(PASSWORD)
        our, there = _digest(number, ours), _digest(number, their)
        put_password = take_password = False
        if our != there:
            if ours is None:
                take_password = their is not None  # C lost its slot: the vault's back
            elif their is None:
                put_password = True
            else:
                put_password = _ours_wins(ours, their, our, there, was.get(AGREED_PASSWORD))
                take_password = not put_password
        ours_sealed, their_sealed = _recovery(mine), theirs.slot(RECOVERY)
        put_recovery = take_recovery = False
        if self._sealed_for is not None:
            mine_fits = _public(ours_sealed) == self._sealed_for
            theirs_fits = _public(their_sealed) == self._sealed_for
            take_recovery = theirs_fits and not mine_fits
            put_recovery = mine_fits and not theirs_fits
        if take_password and not _readable(their, None):
            take_password = False
        if take_recovery and not _readable(None, their_sealed):
            take_recovery = False
        # The key's box: put from a C that has it, taken by one that has none.
        our_box, their_box = mine_box(mine), _box(theirs.slot(BOXES))
        put_box, take_box = their_box is None and our_box is not None, our_box is None
        take_box = take_box and their_box is not None
        if put_password or put_recovery or put_box:
            return self._put(
                theirs,
                number,
                ours if put_password else None,
                ours_sealed if put_recovery else None,
                AsRead(theirs.tag),
                our_box if put_box else None,
            )
        if take_password or take_recovery or take_box:
            self._follow(
                theirs,
                their if take_password else None,
                their_sealed if take_recovery else None,
                mine,
                told=take_password,
                box=their_box if take_box else None,
            )
            return True
        self._agree(number, ours, ours_sealed)
        return True

    def _put(
        self,
        theirs: _Vaults | None,
        number: str,
        password: Mapping[str, object] | None,
        recovery: Mapping[str, object] | None,
        condition: Absent | AsRead,
        box: Mapping[str, object] | None = None,
    ) -> bool:
        """This device's slots in the vault's record (made, without one), and
        its key's box: True once there, False to read it again (RefusedError)."""
        place = self._place
        device = place.device
        contents = Contents() if theirs is None else theirs.contents
        if theirs is None:
            contents = contents.changed(CURRENT, number, _hlc(_set_at(password)), device)
        if password is not None:
            value = cast(Json, dict(password))
            contents = contents.changed(PASSWORD + number, value, _hlc(_set_at(password)), device)
        if recovery is not None:
            value = cast(Json, dict(recovery))
            stamp = _hlc(_set_at(password)) if theirs is None else _later(theirs)
            contents = contents.changed(RECOVERY + number, value, stamp, device)
        if box is not None:
            # Made from the key: the same wherever it is put, at any time.
            value = cast(Json, dict(box))
            contents = contents.changed(BOXES + number, value, Hlc(0), device)
        record = record_of(place.keys)
        data = seal(
            place.keys, place.vault, RECORDS, record, RecordFile(KIND, "", contents).to_json()
        )
        with writing(self._db):
            sync_state.add_untold(self._db, place.vault, self._path)
        place.target.put(self._path, data, condition)  # RefusedError: read again
        if theirs is None:
            self._saw(number)
        mine = self._mine or {}
        self._agree(number, mine_password(mine), _recovery(mine))
        log.info("sync: the secret notes' key put in the cloud")
        return True

    def _follow(
        self,
        theirs: _Vaults,
        password: Mapping[str, object] | None,
        recovery: Mapping[str, object] | None,
        mine: Mapping[str, object] | None,
        told: bool,
        box: Mapping[str, object] | None = None,
    ) -> None:
        number = theirs.current
        set_by = theirs.field(PASSWORD)
        agreed = {
            AGREED_PASSWORD: Agreed(
                _digest(number, password or (None if mine is None else mine_password(mine))),
                _set_at(password or (None if mine is None else mine_password(mine))),
                None if set_by is None else set_by.device,
            ),
            AGREED_RECOVERY: Agreed(
                _recovery_digest(number, recovery or (None if mine is None else _recovery(mine))),
                "",
                None,
            ),
            AGREED_BOX: Agreed(
                _box_digest(number, box or (None if mine is None else mine_box(mine))), "", None
            ),
        }
        what = None
        by = at = None
        if told and password is not None:
            what = RESET_TOLD if password.get(HOW) == RESET else CHANGED_TOLD
            by = None if set_by is None else self._name_of(set_by.device)
            at = _set_at(password) or None
        self.follows.append(
            SecretFollow(
                number, password, recovery, seen_digest(mine), agreed, what, by, at, box=box
            )
        )

    def _saw(self, number: str) -> None:
        """The vault's key, kept for the notes' round (stickle.data.sync_state)."""
        vault = self._place.vault
        if sync_state.secret_key(self._db, vault) != number:
            with writing(self._db):
                sync_state.set_secret_key(self._db, vault, number)

    def _agree(
        self,
        number: str,
        password: Mapping[str, object] | None,
        recovery: Mapping[str, object] | None,
    ) -> None:
        vault = self._place.vault
        now = sync_keys.agreed(self._db, vault)
        box = None if self._mine is None else mine_box(self._mine)
        wanted = {
            AGREED_PASSWORD: Agreed(_digest(number, password), _set_at(password), None),
            AGREED_RECOVERY: Agreed(_recovery_digest(number, recovery), "", None),
            AGREED_BOX: Agreed(_box_digest(number, box), "", None),
        }
        if all(now.get(kind) == each for kind, each in wanted.items()):
            return
        with writing(self._db):
            for kind, each in wanted.items():
                sync_keys.agree(self._db, vault, kind, each)


def _ours_wins(
    ours: Mapping[str, object],
    theirs: Mapping[str, object],
    our: str | None,
    their: str | None,
    agreed: Agreed | None,
) -> bool:
    """Whether this device's password slot is put over the vault's (else the
    vault's is taken), the two differing: the one changed since they were last
    agreed on; changed on both, the later. One set before what was agreed on
    (an old record put back) is never taken."""
    before = None if agreed is None else agreed.digest
    mine_changed, theirs_changed = our != before, their != before
    if agreed is not None and theirs_changed and _set_at(theirs) < agreed.set_at:
        return True
    if mine_changed != theirs_changed:
        return mine_changed
    if not mine_changed:
        return True  # neither changed, yet they differ: this device's is what was agreed on
    return (_set_at(ours), our or "") > (_set_at(theirs), their or "")


def _later(theirs: _Vaults) -> Hlc:
    """A time after the field's in the vault: what is sealed for the recovery
    key has no time of its own, and the one that fits wins whenever it is put."""
    field = theirs.field(RECOVERY)
    return Hlc(0) if field is None else field.hlc.following()


def mine_password(slots: Mapping[str, object]) -> Mapping[str, object] | None:
    found = slots.get(PASSWORD_SLOT)
    return cast(dict[str, object], found) if isinstance(found, dict) else None


def _recovery(slots: Mapping[str, object]) -> Mapping[str, object] | None:
    """What is sealed for the recovery key, without a pair kept beside it (this
    computer's own, never put)."""
    found = slots.get(RECOVERY_SLOT)
    if not isinstance(found, dict):
        return None
    return {
        name: value
        for name, value in cast(dict[str, object], found).items()
        if name in ("public", "sealed")
    }


def sync_secret_key(
    connection: apsw.Connection,
    place: Place,
    mine: Callable[[], Mapping[str, object] | None],
    pairs: Callable[[], Mapping[str, SealedSlot]],
    told: bool,
    full: bool,
    name_of: Callable[[str], str | None] = lambda _device: None,
    taking: bytes | None = None,
) -> list[SecretFollow | SecretOther]:
    """A round's look at the secret notes' key, if it may have changed (told: a
    device said it put the record; full: a full look): what it puts there is
    put, what this device is to take returned. mine: C's slots as they are
    (None without C; KeyFileError or OSError if C does not read now: nothing is
    done this round); pairs: B's, as they are; taking: the public key of the
    vault's recovery key pair this round brings for B, not taken yet (what is
    sealed for the recovery key is weighed against it)."""
    vault = place.vault
    try:
        slots = mine()
        sealed_for = taking or _agreed_recovery_key(connection, vault, pairs())
    except Exception as error:  # the app's: looked at again next round
        log.warning("sync: the key files do not read now: %s", type(error).__name__)
        return []
    was = sync_keys.agreed(connection, vault)
    number = key_number(slots)
    box_was = was.get(AGREED_BOX)
    changed = slots is not None and (
        was.get(AGREED_PASSWORD) is None
        or was[AGREED_PASSWORD].digest != _digest(number, mine_password(slots))
        or was.get(AGREED_RECOVERY) is None
        or was[AGREED_RECOVERY].digest != _recovery_digest(number, _recovery(slots))
        or (None if box_was is None else box_was.digest) != _box_digest(number, mine_box(slots))
    )
    if not (full or told or changed):
        return []
    step = _SecretKey(connection, place, slots, sealed_for, name_of)
    try:
        step.run()
    except FormatError:
        log.warning("sync: the secret notes' key does not open: left for a later round")
        return []
    return step.follows


def vault_boxes(connection: apsw.Connection, place: Place) -> dict[str, bytes]:
    """The boxes the vault's record has, by the numbers of their keys: their
    public keys (none if it has none, or it does not open)."""
    step = _SecretKey(connection, place, None, None, lambda _device: None)
    try:
        theirs = step.read()
    except FormatError as error:
        log.warning("sync: the secret notes' key does not read now: %s", type(error).__name__)
        return {}
    if theirs is None:
        return {}
    found: dict[str, bytes] = {}
    for name, each in theirs.contents.fields.items():
        if not name.startswith(BOXES) or not isinstance(each.value, dict):
            continue
        public = box_of({BOX: cast(dict[str, object], each.value)})
        if public is not None:
            found[name[len(BOXES) :]] = public
    return found


def _agreed_recovery_key(
    connection: apsw.Connection, vault: str, pairs: Mapping[str, SealedSlot]
) -> bytes | None:
    """The public key of the vault's recovery key pair, if this device's B has it
    as agreed on with the vault (else None: what is sealed for it is not weighed)."""
    pair = pairs.get(RECOVERY_KEY)
    agreed = sync_keys.agreed(connection, vault).get(RECOVERY_KEY)
    if pair is None or agreed is None or agreed.digest != digest_of(pair):
        return None
    return pair.public
