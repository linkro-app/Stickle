"""The recovery key and the master password, one of each on every computer that
syncs: what a round does with keys/ (stickle.crypto.vault_keys) and the pairs of
this device's key file B (stickle.crypto.following).

For each kind (the recovery key's, the master password's), this device keeps
the pair it and the vault last agreed on (stickle.data.sync_keys), and so
tells which side changed since:

- the same on both sides: nothing to do but tidy up, the files of that kind
  older than the vault's latest removed;
- nothing agreed yet (joining a vault, or the first round after making one):
  the vault's is taken here; with none in the vault, this device's is put there
  (a master password only this computer had becomes every computer's);
- changed here only (a new recovery key, a master password set or changed):
  put there, and the other files of that kind removed: the old recovery key
  no longer opens the vault;
- changed there only: taken here, and told (who did it and when), unless it is
  only B having lost its slot;
- changed on both: the one set later is everyone's, the other device told;
- the vault's gone (deleted), or one set before the one agreed on (an old file
  put back): this device's is put there again. What was agreed on is never
  gone back from.

What is taken here is not written by the round: B is the app's to write, on its
thread (Follow); only then is it agreed on. Looked at only when it may have
changed: on a full look, when a device said it put a file in keys/, when
nothing is agreed on yet, and when B's pairs are no longer the ones agreed on.
A file whose check does not hold is neither taken nor removed.
"""

import logging
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass

import apsw

from stickle.crypto.following import digest_of
from stickle.crypto.sealed import SealedSlot
from stickle.crypto.vault_keys import (
    MASTER_PASSWORD,
    RECEIVERS,
    RECOVERY_KEY,
    AlteredReceiverError,
    DataKeys,
    Receiver,
    is_trusted,
    key_file,
    read_key_file,
    receiver_id,
)
from stickle.data import sync_keys, sync_state
from stickle.data.sync_keys import Agreed
from stickle.sync.format import KEYS, item_at, item_path
from stickle.sync.place import Place, writing
from stickle.sync.targets.base import Absent, AsRead, NotThereError, RefusedError

log = logging.getLogger(__name__)

# What a pair taken from the vault is told as (stickle.app.protection_changes).
JOINED = "sync_joined"  # this computer's recovery key is now the others'
NEW_RECOVERY_KEY = "recovery_key"
MASTER_SET = "master_password_set"
MASTER_CHANGED = "master_password_changed"


@dataclass(frozen=True)
class Follow:
    """A pair of the vault for this device to take: B's slot of its kind
    becomes it, if B's is still the one seen (stickle.crypto.following.follow),
    and then what is agreed on is it."""

    kind: str
    pair: SealedSlot  # as keys/ holds it (what is sealed in it is the data key's)
    seen: str | None  # the digest of B's pair of that kind when this was decided
    agreed: Agreed
    told: str | None  # what to tell of it (JOINED, ...); None: nothing
    by: str | None  # the name of the device that set it, as it says (None: not known)
    joining: bool  # nothing was agreed on before: the vault's is taken, as joining


@dataclass(frozen=True)
class _Found:
    receiver: Receiver
    tag: str | None

    @property
    def pair(self) -> SealedSlot:
        assert self.receiver.slot is not None
        return self.receiver.slot


def _order(set_at: str, file_id: str, digest: str | None) -> tuple[str, str, str]:
    return set_at, file_id, digest or ""


def _found_order(found: _Found) -> tuple[str, str, str]:
    receiver = found.receiver
    return _order(receiver.set_at, receiver.id, digest_of(found.pair))


def newest(receivers: list[Receiver], keys: DataKeys) -> dict[str, Receiver]:
    """The vault's pair of each kind: of the files that hold, the one set last."""
    found: dict[str, Receiver] = {}
    for receiver in receivers:
        if receiver.kind not in RECEIVERS or not is_trusted(receiver, keys):
            continue
        there = found.get(receiver.kind)
        later = there is None or _found_order(_Found(receiver, None)) > _found_order(
            _Found(there, None)
        )
        if later:
            found[receiver.kind] = receiver
    return found


def _set_at(pair: SealedSlot) -> str:
    return pair.set_at or ""


class _Keys:
    def __init__(
        self,
        connection: apsw.Connection,
        place: Place,
        mine: Mapping[str, SealedSlot],
        name_of: Callable[[str], str | None],
    ) -> None:
        self._db = connection
        self._place = place
        self._mine = mine
        self._name_of = name_of
        self.follows: list[Follow] = []

    def listed(self) -> dict[str, list[_Found]]:
        """The files of keys/ that hold, by kind, the one set last first."""
        place = self._place
        found: dict[str, list[_Found]] = {}
        for entry in place.target.list(KEYS):
            at = item_at(entry.path)
            if at is None or at[0] != KEYS:
                continue
            try:
                read = place.target.get(entry.path)
                receiver = read_key_file(at[1], read.data)
            except NotThereError, AlteredReceiverError:
                continue
            if receiver.kind in RECEIVERS and is_trusted(receiver, place.keys):
                found.setdefault(receiver.kind, []).append(_Found(receiver, read.tag))
        for files in found.values():
            files.sort(key=_found_order, reverse=True)
        return found

    def run(self, kind: str, files: list[_Found], was: Agreed | None) -> None:
        ours = self._mine.get(kind)
        our = digest_of(ours)
        theirs = files[0] if files else None
        their = None if theirs is None else digest_of(theirs.pair)
        if theirs is not None and their == our:
            self._agree(kind, _agreed(theirs))
            self._remove(files[1:])
            return
        if was is None:  # nothing agreed on yet: the vault's, if it has one
            if theirs is not None:
                told = JOINED if kind == RECOVERY_KEY and ours is not None else None
                self._follow(kind, theirs, our, told, joining=True)
            elif ours is not None:
                self._put(kind, ours, self._place.device, files)
            else:
                self._agree(kind, Agreed(None, "", None))
            return
        if ours is None:  # B lost its slot (set aside): the vault's back
            if theirs is not None:
                told = None if their == was.digest else _what(kind, None, theirs.pair)
                self._follow(kind, theirs, None, told)
            return
        mine_changed = our != was.digest
        theirs_changed = their != was.digest
        if theirs is not None and theirs_changed and theirs.receiver.set_at < was.set_at:
            theirs_changed = False  # put back from before what was agreed on: not gone back to
            theirs = None
        if theirs is None or (mine_changed and not theirs_changed):
            device = self._place.device if mine_changed else was.device
            self._put(kind, ours, device, files)
        elif not mine_changed:
            self._follow(kind, theirs, our, _what(kind, ours, theirs.pair))
        elif _order(_set_at(ours), receiver_id(kind, ours.public), our) > _found_order(theirs):
            self._put(kind, ours, self._place.device, files)
        else:  # changed on both, there later: this device is told
            self._follow(kind, theirs, our, _what(kind, ours, theirs.pair))

    def _agree(self, kind: str, agreed: Agreed) -> None:
        if sync_keys.agreed(self._db, self._place.vault).get(kind) == agreed:
            return
        with writing(self._db):
            sync_keys.agree(self._db, self._place.vault, kind, agreed)

    def _follow(
        self, kind: str, theirs: _Found, seen: str | None, told: str | None, joining: bool = False
    ) -> None:
        agreed = _agreed(theirs)
        by = None if agreed.device is None else self._name_of(agreed.device)
        self.follows.append(Follow(kind, theirs.pair, seen, agreed, told, by, joining))

    def _put(self, kind: str, pair: SealedSlot, device: str | None, files: list[_Found]) -> None:
        """pair in keys/ (over a file of its own there that does not hold, or is
        another way of it: a master password changed), then every other file of
        its kind removed; agreed on once it is there."""
        place = self._place
        file_id, data = key_file(place.keys, kind, pair, device)
        path = item_path(KEYS, file_id)
        with writing(self._db):
            sync_state.add_untold(self._db, place.vault, path)
        try:
            place.target.put(path, data, Absent())
        except RefusedError:
            try:
                place.target.put(path, data, AsRead(place.target.get(path).tag))
            except NotThereError, RefusedError:
                return  # changed meanwhile: looked at again next round
        self._agree(kind, Agreed(digest_of(pair), _set_at(pair), device))
        self._remove([found for found in files if found.receiver.id != file_id])
        log.info("sync: this computer's %s put in the cloud", _WHAT[kind])

    def _remove(self, files: list[_Found]) -> None:
        for found in files:  # gone already, or changed since (looked at again): left
            with suppress(NotThereError, RefusedError):
                self._place.target.delete(item_path(KEYS, found.receiver.id), AsRead(found.tag))


_WHAT = {RECOVERY_KEY: "recovery key", MASTER_PASSWORD: "spare password"}


def _agreed(found: _Found) -> Agreed:
    receiver = found.receiver
    return Agreed(digest_of(found.pair), receiver.set_at, receiver.device)


def _what(kind: str, ours: SealedSlot | None, theirs: SealedSlot) -> str:
    """What taking theirs in place of ours is told as."""
    if kind == RECOVERY_KEY:
        return NEW_RECOVERY_KEY
    return MASTER_CHANGED if ours is not None and ours.public == theirs.public else MASTER_SET


def sync_keys_of(
    connection: apsw.Connection,
    place: Place,
    mine: Callable[[], Mapping[str, SealedSlot]],
    told: bool,
    full: bool,
    name_of: Callable[[str], str | None] = lambda _device: None,
) -> list[Follow]:
    """A round's look at keys/, if it may have changed (told: a device said it
    put a file there; full: a full look): what it puts there is put, what this
    device is to take returned. mine: B's pairs, as they are (KeyFileError or
    OSError if B does not read now: nothing is done this round)."""
    vault = place.vault
    was = sync_keys.agreed(connection, vault)
    try:
        pairs = mine()
    except Exception as error:  # B is the app's: looked at again next round
        log.warning("sync: the key file does not read now: %s", type(error).__name__)
        return []
    changed = any(
        kind not in was or was[kind].digest != digest_of(pairs.get(kind)) for kind in RECEIVERS
    )
    if not (full or told or changed):
        return []
    keys = _Keys(connection, place, pairs, name_of)
    listed = keys.listed()
    for kind in RECEIVERS:
        keys.run(kind, listed.get(kind, []), was.get(kind))
    return keys.follows
