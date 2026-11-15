"""The secret notes' key, one for every computer that syncs: a computer without
secret notes takes the others' key (their password, their recovery key), a
password changed or set anew on one is everyone's and told with who did it,
two changed apart end as the later, what is sealed for the recovery key goes
to the one the others have, and a computer with a key of its own is left as it
is (its passwords are matched later). The cloud never holds the key or the
password in the clear, and cannot put another in their place.

Each device has key files of its own (sync_bed: keyed) and takes what a round
brings as the app does."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fake_cloud import DROPBOX, FakeCloud
from sync_bed import KEY, KEYS, VAULT, Clock, Device

from stickle.crypto import recovery, secret_notes
from stickle.crypto.key_files import C
from stickle.crypto.keyfile import WrongPasswordError
from stickle.crypto.recovery import open_recovery_pair
from stickle.crypto.sealed import SealedSlot
from stickle.data import sync_left
from stickle.sync.format import RECORDS, item_path, open_envelope
from stickle.sync.secret_key import CHANGED_TOLD, RESET_TOLD, record_of

FAST = (1, 8192)
PASSWORD = "the secret notes' password"


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud(DROPBOX)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def a(tmp_path: Path, cloud: FakeCloud, clock: Clock) -> Iterator[Device]:
    """The device that made the vault, its secret notes turned on."""
    device = Device("a", tmp_path, cloud, clock, whole=True, keyed=True)
    device.made_the_vault()
    turn_on(device)
    device.sync()
    yield device
    device.close()


@pytest.fixture
def b(tmp_path: Path, cloud: FakeCloud, clock: Clock) -> Iterator[Device]:
    """A device to join, without secret notes."""
    device = Device("b", tmp_path, cloud, clock, whole=True, keyed=True)
    yield device
    device.close()


def files(device: Device) -> secret_notes.KeyFiles:
    assert device.files is not None
    return device.files


def turn_on(device: Device, password: str = PASSWORD) -> bytes:
    pair = recovery.read_recovery(files(device))
    assert isinstance(pair, SealedSlot)
    device.clock.tick()
    return secret_notes.create(files(device), password, pair.public, device.stamp(), FAST)


def key_of(device: Device, password: str) -> bytes | None:
    try:
        return secret_notes.open_with_password(files(device), password)
    except WrongPasswordError:
        return None


def by_recovery_key(device: Device, typed: str) -> bytes | None:
    pair = recovery.read_recovery(files(device))
    assert isinstance(pair, SealedSlot)
    try:
        return secret_notes.open_with_recovery_key(
            files(device), open_recovery_pair(pair, typed), pair.public
        )
    except recovery.WrongRecoveryKeyError, secret_notes.NotKeptForRecoveryKeyError:
        return None


def change(device: Device, key: bytes, password: str, how: str = secret_notes.CHANGED) -> None:
    device.clock.tick()
    secret_notes.change_password(files(device), key, password, device.stamp(), FAST, how)


def told(device: Device) -> list[tuple[str | None, str | None]]:
    return [(each.told, each.by) for each in device.secret]


def the_record(cloud: FakeCloud) -> dict[str, object]:
    path = item_path(RECORDS, record_of(KEYS))
    _head, value = open_envelope(KEYS, cloud.files()[path], VAULT, RECORDS)
    assert isinstance(value, dict)
    return dict(value)


def fields_of(record: dict[str, object]) -> dict[str, dict[str, object]]:
    return cast(dict[str, dict[str, object]], record["fields"])


def test_a_computer_without_secret_notes_takes_the_others_key(a: Device, b: Device) -> None:
    key = key_of(a, PASSWORD)
    assert key is not None

    b.sync()

    assert key_of(b, PASSWORD) == key  # the same key, the same password
    assert by_recovery_key(b, a.recovery_key) == key  # and the same recovery key
    assert told(b) == [(None, None)]  # turned on there: nothing to tell
    slots = files(b).load(C)
    assert secret_notes.key_number(slots) == secret_notes.key_number(files(a).load(C))
    b.sync()
    assert b.secret == []


def test_the_cloud_holds_neither_the_key_nor_the_password(a: Device) -> None:
    raw = a.cloud.files()[item_path(RECORDS, record_of(KEYS))]
    key = key_of(a, PASSWORD)
    assert key is not None

    assert key not in raw and PASSWORD.encode() not in raw
    assert b"secret_password" not in raw and b"public" not in raw  # locked with the data key
    fields = fields_of(the_record(a.cloud))
    number = secret_notes.key_number(files(a).load(C))
    assert set(fields) == {"current", f"password.{number}", f"recovery.{number}"}


def test_a_password_changed_on_one_computer_is_everyones_and_told(a: Device, b: Device) -> None:
    b.sync()
    key = key_of(a, PASSWORD)
    assert key is not None

    change(a, key, "a new password")
    a.sync()
    b.sync()

    assert told(b) == [(CHANGED_TOLD, "A-PC 표식")]
    assert key_of(b, "a new password") == key
    assert key_of(b, PASSWORD) is None


def test_a_password_set_anew_with_the_recovery_key_is_told_so(a: Device, b: Device) -> None:
    b.sync()
    key = key_of(a, PASSWORD)
    assert key is not None

    change(a, key, "set anew", secret_notes.RESET)
    a.sync()
    b.sync()

    assert told(b) == [(RESET_TOLD, "A-PC 표식")]
    assert key_of(b, "set anew") == key


def test_two_changed_apart_end_as_the_later_and_the_other_is_told(a: Device, b: Device) -> None:
    b.sync()
    key = key_of(a, PASSWORD)
    assert key is not None
    change(a, key, "a's")
    change(b, key, "b's, later")

    a.sync()
    b.sync()
    a.sync()

    assert told(a) == [(CHANGED_TOLD, "B-PC 표식")]
    assert key_of(a, "b's, later") == key and key_of(b, "b's, later") == key
    b.sync()
    a.sync()
    assert a.secret == b.secret == []


def test_a_new_recovery_key_with_the_secret_notes_sealed_for_it_opens_them_everywhere(
    a: Device, b: Device
) -> None:
    b.sync()
    key = key_of(a, PASSWORD)
    assert key is not None
    a.clock.tick()
    made = recovery.generate()
    slot = recovery.make_recovery_slot(KEY, made, a.stamp())
    secret_notes.keep_for_recovery_key(files(a), key, slot.public)  # C first, as Stickle does
    recovery.save_recovery_slot(files(a), slot)

    a.sync()
    b.sync()

    assert by_recovery_key(b, made) == key
    assert told(b) == [(None, None)]  # the recovery key is told of, not this


def test_what_is_sealed_for_an_old_recovery_key_goes_to_the_one_the_others_have(
    tmp_path: Path, cloud: FakeCloud, clock: Clock
) -> None:
    """A computer with secret notes joins and puts their key, still sealed for
    its own old recovery key: once it opens them, every computer has them sealed
    for the recovery key they share."""
    maker = Device("m", tmp_path, cloud, clock, whole=True, keyed=True)
    maker.made_the_vault()
    maker.sync()
    joining = Device("j", tmp_path, cloud, clock, whole=True, keyed=True)
    key = turn_on(joining)
    joining.sync()  # takes the vault's recovery key, puts its secret notes' key
    maker.sync()
    assert key_of(maker, PASSWORD) == key
    assert by_recovery_key(maker, maker.recovery_key) is None  # sealed for j's old one

    shared = recovery.read_recovery(files(joining))
    assert isinstance(shared, SealedSlot)
    secret_notes.keep_for_recovery_key(files(joining), key, shared.public)  # opened there
    joining.sync()
    maker.sync()

    assert by_recovery_key(maker, maker.recovery_key) == key
    assert by_recovery_key(joining, maker.recovery_key) == key
    maker.close()
    joining.close()


def test_a_computer_with_a_key_of_its_own_is_left_as_it_is(a: Device, b: Device) -> None:
    own = turn_on(b, "b's own password")
    record = a.cloud.files()[item_path(RECORDS, record_of(KEYS))]

    b.sync()

    assert b.secret == []
    assert key_of(b, "b's own password") == own  # its own, still
    assert a.cloud.files()[item_path(RECORDS, record_of(KEYS))] == record  # nor the vault's


def test_a_record_gone_from_the_cloud_is_put_back(a: Device, b: Device) -> None:
    b.sync()
    path = item_path(RECORDS, record_of(KEYS))
    a.cloud.remove_by_hand(path)

    b.sync()

    assert path in a.cloud.files()
    a.sync()
    assert a.secret == []


def test_a_password_slot_put_back_from_before_is_not_gone_back_to(a: Device, b: Device) -> None:
    b.sync()
    path = item_path(RECORDS, record_of(KEYS))
    old = a.cloud.files()[path]
    key = key_of(a, PASSWORD)
    assert key is not None
    change(a, key, "the new one")
    a.sync()
    b.sync()

    a.cloud.put_by_hand(path, old)
    b.sync()

    assert b.secret == []
    assert key_of(b, "the new one") == key
    assert a.cloud.files()[path] != old  # put right again


def test_a_record_changed_without_the_data_key_is_not_taken(a: Device, b: Device) -> None:
    b.sync()
    path = item_path(RECORDS, record_of(KEYS))
    data = bytearray(a.cloud.files()[path])
    data[-20] ^= 1
    a.cloud.put_by_hand(path, bytes(data))

    b.sync()

    assert b.secret == []
    assert key_of(b, PASSWORD) is not None


def test_the_record_round_leaves_it_to_its_own_step(a: Device, b: Device) -> None:
    b.sync()
    a.sync()

    assert item_path(RECORDS, record_of(KEYS)) not in sync_left.every(b.db, VAULT)
    rows = b.db.execute("SELECT count(*) FROM sync_records WHERE record = ?", (record_of(KEYS),))
    assert rows.fetchall()[0][0] == 0


def test_a_key_made_by_0_6_is_put_once_numbered(
    tmp_path: Path, cloud: FakeCloud, clock: Clock
) -> None:
    device = Device("o", tmp_path, cloud, clock, whole=True, keyed=True)
    device.made_the_vault()
    turn_on(device)
    slots = dict(files(device).load(C) or {})
    del slots[secret_notes.KEY_NUMBER]  # as 0.6 made it
    files(device).save(C, slots)

    device.sync()
    assert item_path(RECORDS, record_of(KEYS)) not in cloud.files()

    assert secret_notes.number_the_key(files(device))
    device.sync()
    assert item_path(RECORDS, record_of(KEYS)) in cloud.files()
    device.close()


def test_what_is_kept_is_the_record_and_its_fields_as_this_version_writes_them(
    a: Device,
) -> None:
    record = the_record(a.cloud)
    assert record["record"] == "secret_notes_key"
    current = fields_of(record)["current"]
    assert current["value"] == secret_notes.key_number(files(a).load(C))
    assert json.dumps(record)  # plain JSON throughout
