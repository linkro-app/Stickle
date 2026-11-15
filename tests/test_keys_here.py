"""What takes the vault's recovery key or master password here, as a round brings
it: B's slot written first, then agreed on with the vault; one made on another
computer told at once with its name, this computer's own taken for the others'
on joining told later, one changed here meanwhile left as it is."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from test_unlock import FAST, Store

from stickle.app.application import KeysHere
from stickle.app.protection_changes import ProtectionChanges
from stickle.crypto import recovery
from stickle.crypto.following import digest_of
from stickle.crypto.sealed import SealedSlot
from stickle.crypto.vault_keys import RECOVERY_KEY
from stickle.data import sync_keys
from stickle.data.schema import open_store
from stickle.data.settings import Settings
from stickle.data.sync_keys import Agreed
from stickle.sync.keys import JOINED, NEW_RECOVERY_KEY, Follow
from stickle.unlock import Unlock

KEY = secrets.token_bytes(32)
VAULT = "a vault"


@pytest.fixture
def unlock(tmp_path: Path) -> Unlock:
    unlock = Unlock(tmp_path, Store("has_key"), FAST, master_strength=FAST)
    unlock.opened_key = KEY
    unlock.make_recovery_key(KEY)
    return unlock


@pytest.fixture
def db(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


def the_vaults(unlock: Unlock, told: str | None, joining: bool = False) -> tuple[Follow, str]:
    mine = recovery.read_recovery(unlock.files)
    assert isinstance(mine, SealedSlot)
    typed = recovery.generate()
    theirs = recovery.make_recovery_slot(secrets.token_bytes(32), typed, "2026-11-05T05:03:00.000Z")
    agreed = Agreed(digest_of(theirs), "2026-11-05T05:03:00.000Z", "laptop-id")
    return Follow(RECOVERY_KEY, theirs, digest_of(mine), agreed, told, "Laptop", joining), typed


def test_taken_and_then_agreed_on_and_told_at_once(unlock: Unlock, db: apsw.Connection) -> None:
    changes = ProtectionChanges(Settings(db))
    keys = KeysHere(unlock, db, changes)
    told: list[str] = []
    keys.told = lambda: told.append("told")
    follow, typed = the_vaults(unlock, NEW_RECOVERY_KEY)

    keys(VAULT, [follow])

    assert unlock.open_with_recovery_key(typed) == KEY
    assert sync_keys.agreed(db, VAULT)[RECOVERY_KEY] == follow.agreed
    assert told == ["told"]
    (change,) = changes.to_tell()
    assert (change.what, change.elsewhere, change.by, change.at) == (
        NEW_RECOVERY_KEY,
        True,
        "Laptop",
        "2026-11-05T05:03:00.000Z",
    )


def test_joining_is_told_later_as_a_change_made_here(unlock: Unlock, db: apsw.Connection) -> None:
    changes = ProtectionChanges(Settings(db))
    keys = KeysHere(unlock, db, changes)
    told: list[str] = []
    keys.told = lambda: told.append("told")

    keys(VAULT, [the_vaults(unlock, JOINED, joining=True)[0]])

    assert told == [] and changes.to_tell() == []
    assert changes.due()
    assert [change.what for change in changes.to_tell()] == [JOINED]
    assert unlock.secret_notes_pair_aside is None  # none to keep aside


def test_one_changed_here_meanwhile_is_left_and_not_agreed_on(
    unlock: Unlock, db: apsw.Connection
) -> None:
    changes = ProtectionChanges(Settings(db))
    keys = KeysHere(unlock, db, changes)
    follow, typed = the_vaults(unlock, NEW_RECOVERY_KEY)
    made = unlock.make_recovery_key(KEY)

    keys(VAULT, [follow])

    assert unlock.open_with_recovery_key(made) == KEY
    with pytest.raises(recovery.WrongRecoveryKeyError):
        unlock.open_with_recovery_key(typed)
    assert sync_keys.agreed(db, VAULT) == {}
    assert changes.to_tell() == []


def test_one_not_written_is_taken_at_a_later_round(
    unlock: Unlock, db: apsw.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = KeysHere(unlock, db, None)
    follow, _ = the_vaults(unlock, NEW_RECOVERY_KEY)

    def full(*_args: object) -> None:
        raise OSError("the disk is full")

    monkeypatch.setattr(unlock.files, "save", full)
    keys(VAULT, [follow])

    assert sync_keys.agreed(db, VAULT) == {}  # not agreed on: the round brings it again
