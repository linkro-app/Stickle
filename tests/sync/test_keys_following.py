"""One recovery key and one spare password on every computer that syncs: a
device joining takes the vault's for its own (and gives it its spare password
if the vault has none), a new one made on a device is everyone's, two made
apart end as the later, and what the cloud is made to hold (a file gone, an
old one put back, one changed without the data key) never takes a device back.

Each device here has key files of its own (sync_bed: keyed), and takes what a
round brings as the app does."""

import json
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, FakeCloud
from sync_bed import KEY, VAULT, Clock, Device

from stickle.crypto import master, recovery, secret_notes
from stickle.crypto.key_files import B
from stickle.crypto.keyfile import KeyFileError, WrongPasswordError
from stickle.crypto.recovery import WrongRecoveryKeyError, canonical, open_recovery_pair
from stickle.crypto.sealed import SealedSlot
from stickle.crypto.vault_keys import (
    MASTER_PASSWORD,
    RECOVERY_KEY,
    DataKeys,
    key_file,
    read_key_file,
)
from stickle.data import sync_keys
from stickle.sync.format import KEYS, item_at, item_path
from stickle.sync.keys import JOINED, MASTER_CHANGED, MASTER_SET, NEW_RECOVERY_KEY
from stickle.sync.vault import NoWayInError, join

FAST = (1, 8192)  # the spare password's key stretching, lowered for tests


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud(DROPBOX)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def a(tmp_path: Path, cloud: FakeCloud, clock: Clock) -> Iterator[Device]:
    """The device that made the vault, a round through."""
    device = Device("a", tmp_path, cloud, clock, whole=True, keyed=True)
    device.made_the_vault()
    device.sync()
    yield device
    device.close()


@pytest.fixture
def b(tmp_path: Path, cloud: FakeCloud, clock: Clock) -> Iterator[Device]:
    """A device to join the vault, its own recovery key made."""
    device = Device("b", tmp_path, cloud, clock, whole=True, keyed=True)
    yield device
    device.close()


def opens(device: Device, typed: str) -> bool:
    assert device.files is not None
    slot = recovery.read_recovery(device.files)
    assert slot is not None
    try:
        return recovery.unwrap(slot, typed) == KEY
    except WrongRecoveryKeyError:
        return False


def opens_with_spare(device: Device, typed: str) -> bool:
    assert device.files is not None
    slot = master.read_master(device.files)
    if slot is None:
        return False
    try:
        return master.open_with_master(slot, typed) == KEY
    except WrongPasswordError:
        return False


def new_recovery_key(device: Device) -> str:
    assert device.files is not None
    device.clock.tick()
    made = recovery.generate()
    recovery.write_recovery(device.files, KEY, made, device.stamp())
    return made


def set_spare(device: Device, password: str) -> None:
    assert device.files is not None
    device.clock.tick()
    master.set_master(device.files, KEY, password, device.stamp(), FAST)


def key_files(cloud: FakeCloud, kind: str) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for path, data in cloud.files().items():
        at = item_at(path)
        if at is not None and at[0] == KEYS and read_key_file(at[1], data).kind == kind:
            found[path] = data
    return found


def told(device: Device) -> list[tuple[str | None, str | None]]:
    return [(each.told, each.by) for each in device.follows]


# Joining


def test_joining_takes_the_recovery_key_of_the_computers_it_syncs_with(
    a: Device, b: Device
) -> None:
    own = b.recovery_key

    b.sync()

    assert told(b) == [(JOINED, None)]  # the vault's maker did not say it is a
    assert opens(b, a.recovery_key)
    assert not opens(b, own)  # so the user is told
    assert len(key_files(b.cloud, RECOVERY_KEY)) == 1
    b.sync()
    assert b.follows == []  # agreed on: nothing more


def test_joining_after_turning_sync_off_takes_the_vaults_again(a: Device, b: Device) -> None:
    from stickle.sync.engine import joined

    b.sync()
    new_recovery_key(b)  # sync off meanwhile: made here alone
    joined(b.db, VAULT)  # sync turned on again with the vault

    b.sync()

    assert told(b)[0][0] == JOINED
    assert opens(b, a.recovery_key)


def test_a_spare_password_only_the_joining_computer_has_becomes_everyones(
    a: Device, b: Device
) -> None:
    set_spare(b, "b's spare password")

    b.sync()
    a.sync()

    assert len(key_files(a.cloud, MASTER_PASSWORD)) == 1
    assert told(a) == [(MASTER_SET, "B-PC 표식")]  # told it opens there now
    assert opens_with_spare(a, "b's spare password")
    assert opens_with_spare(b, "b's spare password")


def test_the_vaults_spare_password_is_taken_over_the_joining_computers_own(
    a: Device, b: Device
) -> None:
    set_spare(a, "a's spare password")
    a.sync()
    set_spare(b, "b's spare password")

    b.sync()

    assert opens_with_spare(b, "a's spare password")
    assert not opens_with_spare(b, "b's spare password")
    assert [each.kind for each in b.follows] == [RECOVERY_KEY, MASTER_PASSWORD]
    assert told(b)[1][0] is None  # told on the last page of joining, not again
    assert len(key_files(b.cloud, MASTER_PASSWORD)) == 1


# Changed on one computer


def test_a_new_recovery_key_made_on_one_computer_opens_every_computer(
    a: Device, b: Device, tmp_path: Path
) -> None:
    b.sync()
    old = a.recovery_key
    made = new_recovery_key(a)

    a.sync()
    assert len(key_files(a.cloud, RECOVERY_KEY)) == 1  # the old one's file gone
    b.sync()

    assert told(b) == [(NEW_RECOVERY_KEY, "A-PC 표식")]
    assert opens(b, made)
    assert not opens(b, old)
    # A computer to come joins with the new one only.
    with pytest.raises(NoWayInError):
        join(b.cloud.target("c"), RECOVERY_KEY, old)
    assert join(b.cloud.target("c"), RECOVERY_KEY, made).keys.keys == a.place.keys.keys


def test_a_spare_password_set_then_changed_on_one_computer_is_everyones(
    a: Device, b: Device
) -> None:
    b.sync()
    set_spare(a, "the first one")
    a.sync()
    b.sync()
    assert told(b) == [(MASTER_SET, "A-PC 표식")]
    assert a.files is not None

    a.clock.tick()
    master.change_master(a.files, "the first one", "the second one", a.stamp(), FAST)
    a.sync()
    b.sync()

    assert told(b) == [(MASTER_CHANGED, "A-PC 표식")]
    assert opens_with_spare(b, "the second one")
    assert not opens_with_spare(b, "the first one")
    assert len(key_files(b.cloud, MASTER_PASSWORD)) == 1


def test_two_made_apart_end_as_the_later_on_every_computer(a: Device, b: Device) -> None:
    b.sync()
    new_recovery_key(a)
    later = new_recovery_key(b)

    a.sync()
    b.sync()
    a.sync()
    b.sync()

    assert opens(a, later) and opens(b, later)
    assert len(key_files(a.cloud, RECOVERY_KEY)) == 1
    a.sync()
    b.sync()
    assert a.follows == b.follows == []


def test_the_computer_whose_new_key_lost_is_told(a: Device, b: Device) -> None:
    b.sync()
    new_recovery_key(a)
    later = new_recovery_key(b)
    a.sync()
    b.sync()

    a.sync()

    assert told(a) == [(NEW_RECOVERY_KEY, "B-PC 표식")]
    assert opens(a, later)


def test_a_new_one_made_here_meanwhile_is_not_written_over(a: Device, b: Device) -> None:
    b.sync()
    new_recovery_key(a)
    a.sync()
    b.taking = False
    b.sync()  # brings a's, not yet taken
    mine = new_recovery_key(b)  # the user makes another first

    b.take(b.follows)

    assert opens(b, mine)
    b.taking = True
    b.sync()  # then the later of the two is everyone's
    a.sync()
    assert opens(a, mine) and opens(b, mine)


# What the cloud is made to hold


def test_a_file_gone_from_the_cloud_is_put_back(a: Device, b: Device) -> None:
    b.sync()
    (path,) = key_files(a.cloud, RECOVERY_KEY)
    a.cloud.remove_by_hand(path)

    a.sync()
    b.sync()

    assert list(key_files(a.cloud, RECOVERY_KEY)) == [path]
    assert b.follows == []
    assert opens(a, a.recovery_key) and opens(b, a.recovery_key)


def test_an_old_file_put_back_is_not_gone_back_to(a: Device, b: Device) -> None:
    b.sync()
    old = key_files(a.cloud, RECOVERY_KEY)
    made = new_recovery_key(a)
    a.sync()
    b.sync()
    (new_path,) = key_files(a.cloud, RECOVERY_KEY)

    for path, data in old.items():  # beside the new one, then in its place
        a.cloud.put_by_hand(path, data)
    b.sync()
    assert b.follows == [] and set(key_files(a.cloud, RECOVERY_KEY)) == {new_path}
    a.cloud.remove_by_hand(new_path)
    for path, data in old.items():
        a.cloud.put_by_hand(path, data)
    b.sync()

    assert b.follows == []
    assert opens(b, made) and not opens(b, a.recovery_key)
    assert set(key_files(a.cloud, RECOVERY_KEY)) == {new_path}


def test_a_file_changed_without_the_data_key_is_not_taken(a: Device, b: Device) -> None:
    b.sync()
    (path,) = key_files(a.cloud, RECOVERY_KEY)
    # Someone who can write in the cloud: a pair of their own, set later...
    theirs = recovery.generate()
    pair = recovery.make_recovery_slot(KEY, theirs, "2099-01-01T00:00:00.000Z")
    their_id, their_file = key_file(DataKeys({1: secrets.token_bytes(32)}), RECOVERY_KEY, pair)
    a.cloud.put_by_hand(item_path(KEYS, their_id), their_file)
    b.sync()
    assert b.follows == []
    assert opens(b, a.recovery_key) and not opens(b, theirs)
    assert item_path(KEYS, their_id) in a.cloud.files()  # not theirs to remove either
    # ...or the user's file, said to be set later.
    written = json.loads(a.cloud.files()[path])
    written["set_at"] = "2099-01-01T00:00:00.000Z"
    a.cloud.put_by_hand(path, json.dumps(written).encode())

    a.sync()
    b.sync()

    assert b.follows == []
    file_id = path.removeprefix(f"{KEYS}/").removesuffix(".json")
    assert read_key_file(file_id, a.cloud.files()[path]).set_at < "2099"  # put right


def test_a_round_with_nothing_new_does_not_look_at_the_keys(a: Device, b: Device) -> None:
    b.sync()
    a.sync()
    a.full = b.full = False
    a.cloud.log.clear()

    b.sync()
    a.sync()
    assert not [line for line in a.cloud.log if f" {KEYS}" in line]

    made = new_recovery_key(a)
    a.sync()
    b.sync()  # told by a's own file of the one it put
    assert told(b) == [(NEW_RECOVERY_KEY, "A-PC 표식")]
    assert opens(b, made)


def test_a_key_file_that_does_not_read_now_is_left_for_a_later_round(a: Device, b: Device) -> None:
    b.sync()
    assert b.files is not None
    kept = b.files.path(B).read_bytes()
    second = b.files.second_path(B).read_bytes()
    b.files.path(B).write_bytes(b"{half written")
    b.files.second_path(B).write_bytes(b"{half written")
    new_recovery_key(a)
    a.sync()

    b.sync()
    assert b.follows == []

    b.files.path(B).write_bytes(kept)
    b.files.second_path(B).write_bytes(second)
    b.sync()
    assert told(b) == [(NEW_RECOVERY_KEY, "A-PC 표식")]


def test_what_is_agreed_on_is_no_key() -> None:
    columns = sync_keys.Agreed.__dataclass_fields__
    assert set(columns) == {"digest", "set_at", "device"}


# The secret notes


def test_the_secret_notes_open_with_the_old_recovery_key_until_sealed_again(
    a: Device, b: Device
) -> None:
    assert b.files is not None
    own = b.recovery_key
    pair = recovery.read_recovery(b.files)
    assert isinstance(pair, SealedSlot)
    secret = secret_notes.create(b.files, "secret password", pair.public, b.stamp(), FAST)

    b.sync()

    assert secret_notes.pair_aside(b.files) is True  # this computer's, from before
    assert secret_notes.open_with_pair_aside(b.files, canonical(own)) == secret
    taken = recovery.read_recovery(b.files)
    assert isinstance(taken, SealedSlot)
    with pytest.raises(secret_notes.NotKeptForRecoveryKeyError):
        secret_notes.open_with_recovery_key(
            b.files, open_recovery_pair(taken, a.recovery_key), taken.public
        )
    # Opened with their password, they are sealed for the one taken.
    secret_notes.keep_for_recovery_key(b.files, secret, taken.public)
    assert secret_notes.pair_aside(b.files) is None
    opened = secret_notes.open_with_recovery_key(
        b.files, open_recovery_pair(taken, a.recovery_key), taken.public
    )
    assert opened == secret
    with pytest.raises((WrongPasswordError, KeyFileError, secret_notes.NotKeptForRecoveryKeyError)):
        secret_notes.open_with_pair_aside(b.files, canonical(own))


def test_a_new_recovery_key_from_another_computer_keeps_the_one_they_open_with(
    a: Device, b: Device
) -> None:
    b.sync()
    assert b.files is not None
    taken = recovery.read_recovery(b.files)
    assert isinstance(taken, SealedSlot)
    secret = secret_notes.create(b.files, "secret password", taken.public, b.stamp(), FAST)
    new_recovery_key(a)
    a.sync()

    b.sync()

    assert secret_notes.pair_aside(b.files) is False  # not from before joining
    assert secret_notes.open_with_pair_aside(b.files, canonical(a.recovery_key)) == secret
