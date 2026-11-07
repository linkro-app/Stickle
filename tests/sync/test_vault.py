"""A vault as a whole: made only where there is none, joined with the recovery
key or the master password, and checked at the start of every round. Gone,
made again from another device, or newer: sync with it stops and writes
nothing there. Two devices making one at once end with one vault, even on a
server that keeps no conditions. Another vault's file is no device's to write
over but the one whose note's place it is in. A device whose disk was copied
is told apart from the copy: the one that sees it takes a new id, and what
it changed before keeps the old one."""

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_cloud import (
    DROPBOX,
    ONEDRIVE,
    WEBDAV_IGNORING,
    WEBDAV_NO_TAGS,
    FakeCloud,
    Kind,
    Request,
    Trouble,
    Turns,
)
from sync_bed import KEYS, VAULT, Clock, Device, settle

from stickle.crypto import master, recovery
from stickle.crypto.recovery import RecoveryKeyTypoError
from stickle.crypto.sealed import make_sealed_slot
from stickle.crypto.vault_keys import MASTER_PASSWORD, RECOVERY_KEY
from stickle.data.record_stamps import CATEGORY
from stickle.sync.format import (
    DEVICES,
    NOTES,
    RECORDS,
    VAULT_FILE,
    NewerFormatError,
    UnreadableError,
    VaultInfo,
    item_path,
    json_bytes,
    open_envelope,
    seal,
    vault_file,
)
from stickle.sync.format import (
    KEYS as KEYS_FOLDER,
)
from stickle.sync.note_file import NoteFile
from stickle.sync.record_file import record_id
from stickle.sync.targets.base import UnavailableError
from stickle.sync.vault import (
    NoWayInError,
    VaultChangedError,
    VaultGoneError,
    VaultThereError,
    join,
    make,
)

DAY = 24 * 60 * 60 * 1000
STRANGER = "0a1b2c3d-1111-4222-8333-444455556666"  # a note no device has
DATABASE_KEY = bytes([7]) * 32
RECOVERY = recovery.generate()
PASSWORD = "a master password"
PAIRS = {
    RECOVERY_KEY: recovery.make_recovery_slot(DATABASE_KEY, RECOVERY, "2026-11-07T00:00:00Z"),
    MASTER_PASSWORD: make_sealed_slot(DATABASE_KEY, PASSWORD, 1, 8192, master.SLOT, None),
}


@pytest.fixture
def clock() -> Clock:
    return Clock()


def two_on(kind: Kind, folder: Path, clock: Clock) -> tuple[FakeCloud, Device, Device]:
    cloud = FakeCloud(kind)
    return (
        cloud,
        Device("a", folder, cloud, clock, whole=True),
        Device("b", folder, cloud, clock, whole=True),
    )


@pytest.fixture(params=[DROPBOX, WEBDAV_IGNORING], ids=lambda k: k.name)
def kind(request: pytest.FixtureRequest) -> Kind:
    return request.param


@pytest.fixture
def two(tmp_path: Path, kind: Kind, clock: Clock) -> Iterator[tuple[FakeCloud, Device, Device]]:
    cloud, a, b = two_on(kind, tmp_path, clock)
    yield cloud, a, b
    a.close()
    b.close()


# Made and joined


def test_a_vault_is_made_where_there_is_none_and_joined_with_either_way_in() -> None:
    cloud = FakeCloud(DROPBOX)
    made = make(cloud.target("a"), PAIRS)
    folders = sorted({path.split("/")[0] for path in cloud.files()})
    assert folders == [KEYS_FOLDER, VAULT_FILE]
    assert len([path for path in cloud.files() if path.startswith(f"{KEYS_FOLDER}/")]) == 2
    by_key = join(cloud.target("b"), RECOVERY_KEY, recovery.format_key(RECOVERY).lower())
    by_password = join(cloud.target("c"), MASTER_PASSWORD, PASSWORD)
    for joined in (by_key, by_password):
        assert joined.vault == made.vault
        assert joined.keys.key(1) == made.keys.key(1)


def test_what_opens_nothing_does_not_join() -> None:
    cloud = FakeCloud(DROPBOX)
    make(cloud.target("a"), PAIRS)
    with pytest.raises(NoWayInError):
        join(cloud.target("b"), MASTER_PASSWORD, "not the password")
    with pytest.raises(NoWayInError):
        join(cloud.target("b"), RECOVERY_KEY, recovery.generate())
    with pytest.raises(RecoveryKeyTypoError):
        join(cloud.target("b"), RECOVERY_KEY, RECOVERY[:-1] + ("A" if RECOVERY[-1] != "A" else "B"))
    with pytest.raises(VaultGoneError):
        join(FakeCloud(DROPBOX).target("b"), MASTER_PASSWORD, PASSWORD)


def test_a_vault_is_not_made_over_one_there() -> None:
    cloud = FakeCloud(ONEDRIVE)
    first = make(cloud.target("a"), PAIRS)
    before = cloud.files()
    with pytest.raises(VaultThereError):
        make(cloud.target("b"), PAIRS)
    assert cloud.files() == before
    assert join(cloud.target("b"), MASTER_PASSWORD, PASSWORD).vault == first.vault


def test_two_devices_making_one_at_once_end_with_one_on_a_server_keeping_no_conditions() -> None:
    turns = Turns([0, 1, 0])  # a writes vault.json, b writes its own over it, a reads it back
    cloud = FakeCloud(WEBDAV_IGNORING, turns=turns)
    made: dict[str, str] = {}

    def making(name: str) -> None:
        made[name] = make(cloud.target(name), PAIRS).vault

    failed = turns.run({"a": lambda: making("a"), "b": lambda: making("b")})
    assert set(failed) == {"a"} and isinstance(failed["a"], VaultThereError)
    cloud.turns = None
    assert join(cloud.target("c"), MASTER_PASSWORD, PASSWORD).vault == made["b"]
    assert len([path for path in cloud.files() if path.startswith(f"{KEYS_FOLDER}/")]) == 2


def test_the_key_files_an_earlier_vault_left_are_written_over() -> None:
    cloud = FakeCloud(DROPBOX)
    make(cloud.target("a"), PAIRS)
    keys = {path: data for path, data in cloud.files().items() if path != VAULT_FILE}
    cloud.vanish()
    for path, data in keys.items():  # vault.json alone deleted: keys/ left from the old one
        cloud.put_by_hand(path, data)
    again = make(cloud.target("a"), PAIRS)
    joined = join(cloud.target("b"), MASTER_PASSWORD, PASSWORD)
    assert joined.vault == again.vault and joined.keys.key(1) == again.keys.key(1)


# Checked every round


def test_a_vault_gone_stops_sync_with_it_and_is_not_made_again(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    cloud, a, b = two
    note = a.notes.create("남는 메모")
    settle([a, b])
    cloud.vanish()
    a.notes.update_body(note.id, "그사이 고친 글")
    for device in (a, b):
        with pytest.raises(VaultGoneError):
            device.sync()
    assert cloud.files() == {}
    assert a.texts() == {note.id: "그사이 고친 글"}
    assert b.texts() == {note.id: "남는 메모"}


@pytest.mark.parametrize(
    "there",
    [
        pytest.param(vault_file(VaultInfo("vault-2")), id="another vault"),
        pytest.param(json_bytes({"format": 2, "kind": "personal", "vault": VAULT}), id="newer"),
        pytest.param(b'{"format": 1, "vau', id="half there"),
    ],
)
def test_a_vault_json_not_as_left_stops_the_round_before_anything_is_written(
    two: tuple[FakeCloud, Device, Device], there: bytes
) -> None:
    cloud, a, b = two
    a.notes.create("남는 메모")
    settle([a, b])
    a.notes.create("새 메모")
    cloud.put_by_hand(VAULT_FILE, there)
    before = cloud.files()
    with pytest.raises((VaultChangedError, NewerFormatError, UnreadableError)):
        a.sync()
    assert cloud.files() == before


# Another vault's files


def test_another_vaults_file_in_a_notes_place_is_written_over_only_by_the_device_with_the_note(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    cloud, a, b = two
    note = a.notes.create("이 보관함의 메모")
    settle([a, b])
    cloud.put_by_hand(item_path(NOTES, note.id), seal(KEYS, "vault-2", NOTES, note.id, {}))
    other = seal(KEYS, "vault-2", NOTES, STRANGER, {})
    cloud.put_by_hand(item_path(NOTES, STRANGER), other)
    report = b.sync()
    assert report.left == []
    assert cloud.files()[item_path(NOTES, STRANGER)] == other  # no device has that note
    _, value = open_envelope(KEYS, cloud.files()[item_path(NOTES, note.id)], VAULT, NOTES)
    assert NoteFile.from_json(KEYS, value).body.text == "이 보관함의 메모"
    settle([a, b])
    assert a.texts() == b.texts() == {note.id: "이 보관함의 메모"}


def test_another_vaults_file_in_a_records_place_is_written_over_by_the_device_with_it(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    cloud, a, b = two
    category = a.labels.create_category("일", "blue")
    settle([a, b])
    record = record_id(KEYS, CATEGORY, category.id)
    path = item_path(RECORDS, record)
    cloud.put_by_hand(path, seal(KEYS, "vault-2", RECORDS, record, {}))
    b.sync()
    head, _ = open_envelope(KEYS, cloud.files()[path], VAULT, RECORDS)
    assert head.item == record
    settle([a, b])
    assert [c.name for c in a.labels.categories()] == [c.name for c in b.labels.categories()]
    assert [c.name for c in b.labels.categories()] == ["일"]


# A device's disk copied


def copy_of(device: Device, name: str, folder: Path, cloud: FakeCloud, clock: Clock) -> Device:
    """Another device made from a copy of device's disk: its database, its id."""
    device.close()
    shutil.copyfile(device.path, folder / f"{name}.db")
    device.reopen()
    copy = Device(name, folder, cloud, clock, whole=True)
    copy.id = device.id
    copy.place = type(device.place)(cloud.target(name), VAULT, KEYS, device.id)
    copy.reopen()
    return copy


@pytest.mark.parametrize(
    "kind", [DROPBOX, ONEDRIVE, WEBDAV_IGNORING, WEBDAV_NO_TAGS], ids=lambda k: k.name
)
def test_of_two_devices_with_one_id_the_one_that_sees_the_other_takes_a_new_one(
    tmp_path: Path, kind: Kind, clock: Clock
) -> None:
    cloud, a, b = two_on(kind, tmp_path, clock)
    note = a.notes.create("복제 전")
    settle([a, b])
    old = a.id
    a2 = copy_of(a, "a2", tmp_path, cloud, clock)
    a.notes.update_body(note.id, "원래 PC에서 고침")
    a.notes.set_color(note.id, "pink")
    a.sync()
    clock.tick(DAY)  # a day on: the copy says again it synced
    other = a2.notes.create("복사한 PC의 메모")
    a2.sync()
    clock.tick()
    a.sync()
    assert a2.id == old and a.id != old  # a saw the copy's write: a takes a new id
    settle([a, a2, b])
    assert a.texts() == a2.texts() == b.texts()
    assert set(a.texts()) == {note.id, other.id}
    devices = sorted(path for path in cloud.files() if path.startswith(f"{DEVICES}/"))
    assert devices == sorted(item_path(DEVICES, each) for each in {a.id, old, b.id})
    assert a.stamp_of(note.id, "color")[1] == old  # changed before: the old id kept
    for device in (a, a2, b):
        device.close()


def test_a_device_whose_own_write_lost_its_answer_keeps_its_id(
    tmp_path: Path, clock: Clock
) -> None:
    own = item_path(DEVICES, "a-0000-4000-8000-000000000000")

    def lose(request: Request) -> Trouble | None:
        return Trouble.LOST if (request.op, request.path) == ("put", own) else None

    cloud, a, b = two_on(DROPBOX, tmp_path, clock)
    old = a.id
    cloud.trouble = lose
    with pytest.raises(Exception):  # noqa: B017 - the answer lost
        a.sync()
    cloud.trouble = lambda _request: None
    a.notes.create("그다음")
    settle([a, b])
    assert a.id == old
    a.close()
    b.close()


def test_a_vault_gone_back_to_an_earlier_day_is_not_taken_for_a_copy(
    tmp_path: Path, clock: Clock
) -> None:
    cloud, a, b = two_on(DROPBOX, tmp_path, clock)
    a.notes.create("메모")
    settle([a, b])
    back_to = cloud.requests
    for _ in range(3):  # days on, each device saying again it synced
        clock.tick(DAY)
        settle([a, b])
    old = (a.id, b.id)
    cloud.go_back_to(back_to)
    settle([a, b])
    assert (a.id, b.id) == old
    a.close()
    b.close()


def test_a_device_whose_round_ended_after_it_took_a_new_id_goes_on_as_that_one(
    tmp_path: Path, clock: Clock
) -> None:
    cloud, a, b = two_on(DROPBOX, tmp_path, clock)
    a.notes.create("메모")
    settle([a, b])
    old = a.id
    a2 = copy_of(a, "a2", tmp_path, cloud, clock)
    clock.tick(DAY)
    a2.sync()
    cloud.trouble = lambda request: (
        Trouble.UNAVAILABLE
        if (request.device, request.op) == ("a", "list") and request.path == "records"
        else None
    )
    with pytest.raises(UnavailableError):
        a.sync_as(old)  # it took a new id, then the round failed: its caller never heard
    cloud.trouble = lambda _request: None
    report = a.sync_as(old)
    assert report.device not in (old, a2.id)
    assert a.sync_as(old).device == report.device  # the same one: not taken again
    devices = [path for path in cloud.files() if path.startswith(f"{DEVICES}/")]
    assert len(devices) == 3
    for device in (a, a2, b):
        device.close()


def test_a_device_renewing_its_id_puts_nothing_of_what_it_changed_before_again(
    tmp_path: Path, clock: Clock
) -> None:
    cloud, a, b = two_on(DROPBOX, tmp_path, clock)
    note = a.notes.create("메모")
    a.notes.set_color(note.id, "pink")
    a.labels.create_category("일", "blue")
    settle([a, b])
    a2 = copy_of(a, "a2", tmp_path, cloud, clock)
    clock.tick(DAY)
    a2.sync()
    clock.tick()
    before = {
        path: data
        for path, data in cloud.files().items()
        if path.startswith((f"{NOTES}/", f"{RECORDS}/"))
    }
    report = a.sync()
    assert report.device != a2.id
    assert report.put == report.changed == report.records.put == []
    assert a.stamp_of(note.id, "color")[1] == a2.id  # what a changed before: the old id
    after = cloud.files()
    assert {path: after[path] for path in before} == before
    for device in (a, a2, b):
        device.close()
