"""A vault as a whole: making one, joining one, and making sure at the start of
every round that the vault synced with is still there, and still the one it
was.

A vault is made only where there is none: vault.json is written only if no
file is there, then read back. A cloud that does not keep conditions may let
two devices making one at once each write theirs, one over the other; the
device that reads back another's id has not made the vault, and writes nothing
more: it joins the one there. Only then are the data key's files written in
keys/, sealed for the recovery key and the master password.

Each round reads vault.json first. Gone (the user deleted the Stickle folder),
naming another vault (made again from another device), or of a newer format:
sync with that vault stops, and nothing is written there until the user says
what to do. A vault that went is never made again by itself: that would bring
back what someone meant to delete.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from stickle.crypto.keyfile import WrongPasswordError
from stickle.crypto.recovery import canonical
from stickle.crypto.sealed import SealedSlot
from stickle.crypto.vault_keys import (
    FIRST,
    RECOVERY_KEY,
    AlteredReceiverError,
    DataKeys,
    Receiver,
    is_trusted,
    key_file,
    new_data_key,
    open_receiver,
    read_key_file,
)
from stickle.sync.format import KEYS, VAULT_FILE, VaultInfo, item_at, read_vault_file, vault_file
from stickle.sync.targets.base import Absent, AsRead, NotThereError, RefusedError, Target


class VaultError(Exception):
    """The vault is not as this device left it: sync with it stops, and waits
    for the user to say what to do."""


class VaultGoneError(VaultError):
    """No vault.json: the vault was deleted (the Stickle folder, say)."""


class VaultChangedError(VaultError):
    """vault.json names another vault: made again from another device."""


class VaultThereError(VaultError):
    """A vault is there already, made by another device: join it instead."""


class NoWayInError(VaultError):
    """keys/ has nothing what was typed opens the data key with."""


@dataclass(frozen=True)
class Vault:
    vault: str
    keys: DataKeys


def confirm(target: Target, vault: str) -> VaultInfo:
    """What vault.json says, if it is still vault's. VaultGoneError,
    VaultChangedError; NewerFormatError (stickle.sync.format) for a newer
    format; UnreadableError for one that does not read (half written: later)."""
    try:
        read = target.get(VAULT_FILE)
    except NotThereError as error:
        raise VaultGoneError() from error
    info = read_vault_file(read.data)
    if info.vault != vault:
        raise VaultChangedError()
    return info


def make(target: Target, pairs: Mapping[str, SealedSlot]) -> Vault:
    """A new vault, with a data key of its own sealed for each pair (by kind:
    the recovery key's, the master password's). VaultThereError if there is
    one, or another device's came first."""
    made = Vault(str(uuid.uuid4()), DataKeys({FIRST: new_data_key()}))
    try:
        target.put(VAULT_FILE, vault_file(VaultInfo(made.vault)), Absent())
    except RefusedError as error:
        raise VaultThereError() from error
    try:
        confirm(target, made.vault)
    except (VaultGoneError, VaultChangedError) as error:
        raise VaultThereError() from error
    for kind, pair in sorted(pairs.items()):
        _put_key_file(target, made.keys, kind, pair)
    return made


def _put_key_file(target: Target, keys: DataKeys, kind: str, pair: SealedSlot) -> None:
    """The data key sealed for pair, in keys/: over one left by an earlier vault."""
    file_id, data = key_file(keys, kind, pair)
    path = f"{KEYS}/{file_id}.json"
    try:
        target.put(path, data, Absent())
        return
    except RefusedError:
        pass
    there = target.get(path)
    try:
        if is_trusted(read_key_file(file_id, there.data), keys):
            return
    except AlteredReceiverError:
        pass
    target.put(path, data, AsRead(there.tag))


def receivers(target: Target) -> list[Receiver]:
    """The files of keys/ that hold together, as they come."""
    found: list[Receiver] = []
    for entry in target.list(KEYS):
        at = item_at(entry.path)
        if at is None or at[0] != KEYS:
            continue
        try:
            found.append(read_key_file(at[1], target.get(entry.path).data))
        except NotThereError, AlteredReceiverError:
            continue
    return found


def join(target: Target, kind: str, typed: str) -> Vault:
    """The vault there and its data key, opened with what the user typed: the
    recovery key (any way it is written) or the master password. VaultGoneError
    if there is none; NoWayInError if typed opens nothing (RecoveryKeyTypoError,
    stickle.crypto.recovery, for a recovery key mistyped)."""
    try:
        info = read_vault_file(target.get(VAULT_FILE).data)
    except NotThereError as error:
        raise VaultGoneError() from error
    if kind == RECOVERY_KEY:
        typed = canonical(typed)
    for receiver in receivers(target):
        if receiver.kind != kind:
            continue
        try:
            return Vault(info.vault, open_receiver(receiver, typed))
        except WrongPasswordError, AlteredReceiverError:
            continue  # not this one: the next, if there is one
    raise NoWayInError()
