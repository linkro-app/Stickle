"""Settings that follow the user to every device, between devices and a cloud
in memory: one changed on one reaches the others, each keeping its later
change; those kept on each device never go to the vault; a value this version
cannot take, or a setting it does not know, is left there as it is. Joining,
each keeps its later change and one never changed here takes the vault's, and
which recovery key was said kept here is let go of; keeping only the vault's
notes takes its settings."""

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fake_cloud import DROPBOX, FakeCloud
from sync_bed import KEYS, VAULT, Clock, Device, settle

from stickle.core.hlc import Hlc
from stickle.data import record_stamps
from stickle.data.record_stamps import SETTING, VALUE
from stickle.data.settings import (
    DEVICE_NAME,
    MAGNET,
    MORNING,
    RECENT_CALENDARS,
    RECOVERY_KEY_KEPT,
    RELOCK_MINUTES,
    SHORTCUTS,
    SNOOZE_MINUTES,
    SYNC_EVERY,
    UPDATE_CHECK,
)
from stickle.sync.engine import cloud_only, joined
from stickle.sync.format import RECORDS, Json, item_at, item_path, open_envelope, seal
from stickle.sync.record_file import record_id

HOUR = 60 * 60 * 1000
OTHER = "c0c0c0c0-0000-4000-8000-000000000000"  # a device of a newer version


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud(DROPBOX)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def pair(tmp_path: Path, cloud: FakeCloud, clock: Clock) -> Iterator[tuple[Device, Device]]:
    devices = (Device("a", tmp_path, cloud, clock, True), Device("b", tmp_path, cloud, clock, True))
    yield devices
    for device in devices:
        device.close()


def in_vault(cloud: FakeCloud) -> dict[str, Json]:
    """The settings in the vault, by key: their values."""
    found: dict[str, Json] = {}
    for path, data in cloud.files().items():
        at = item_at(path)
        if at is None or at[0] != RECORDS:
            continue
        value = open_envelope(KEYS, data, VAULT, RECORDS)[1]
        assert isinstance(value, dict)
        if value["record"] == SETTING:
            fields = cast(dict[str, dict[str, Json]], value["fields"])
            found[cast(str, value["item"])] = fields[VALUE]["value"]
    return found


def put_setting(cloud: FakeCloud, key: str, value: Json, at: int) -> str:
    """A setting's record as another device left it in the vault; its path."""
    record = record_id(KEYS, SETTING, key)
    field = {"device": OTHER, "hlc": Hlc(at).text, "value": value}
    contents: Json = {"record": SETTING, "item": key, "fields": {VALUE: field}}
    path = item_path(RECORDS, record)
    cloud.put_by_hand(path, seal(KEYS, VAULT, RECORDS, record, contents))
    return path


def test_a_setting_changed_on_one_comes_to_the_other(pair: tuple[Device, Device]) -> None:
    a, b = pair
    a.settings.set(MAGNET, False)
    a.settings.set(MORNING, "07:30")
    a.settings.set(RECENT_CALENDARS, ["korean"])

    a.sync()
    report = b.sync()

    assert (b.settings.get(MAGNET), b.settings.get(MORNING)) == (False, "07:30")
    assert b.settings.get(RECENT_CALENDARS) == ["korean"]
    assert {item for kind, item in report.records.changed if kind == SETTING} == {
        MAGNET.key,
        MORNING.key,
        RECENT_CALENDARS.key,
    }


def test_each_setting_keeps_its_later_change(pair: tuple[Device, Device], clock: Clock) -> None:
    a, b = pair
    settle(pair)
    b.settings.set(SNOOZE_MINUTES, 30)
    a.settings.set(MORNING, "06:00")
    clock.tick()
    a.settings.set(SNOOZE_MINUTES, 60)
    b.settings.set(MORNING, "08:00")

    settle(pair)

    for device in pair:
        assert (device.settings.get(SNOOZE_MINUTES), device.settings.get(MORNING)) == (60, "08:00")


def test_a_change_is_later_than_the_one_it_replaced_whatever_the_clock_says(
    pair: tuple[Device, Device],
) -> None:
    a, b = pair
    a.settings.set(MAGNET, False)
    settle(pair)
    b.skew = -HOUR  # its clock an hour behind
    b.settings.set(MAGNET, True)

    settle(pair)

    assert a.settings.get(MAGNET) is True


def test_the_settings_kept_on_each_computer_never_go(
    pair: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = pair
    a.settings.set(SYNC_EVERY, 60)
    a.settings.set(SHORTCUTS, {"new-note": "Ctrl+Alt+M"})
    a.settings.set(DEVICE_NAME, "회사 노트북")
    a.settings.set(UPDATE_CHECK, False)
    a.settings.set(MAGNET, False)

    settle(pair)

    assert in_vault(cloud) == {MAGNET.key: False}
    assert b.settings.get(SYNC_EVERY) == SYNC_EVERY.default
    assert b.settings.get(SHORTCUTS) == {}
    assert b.settings.get(UPDATE_CHECK) is True
    assert b.settings.get(DEVICE_NAME) == ""


def test_set_as_it_is_it_is_no_change(pair: tuple[Device, Device]) -> None:
    a, _ = pair
    a.settings.set(MAGNET, False)
    settle(pair)
    seq = record_stamps.change_seq(a.db, SETTING, MAGNET.key)
    stamped = record_stamps.of(a.db, SETTING, MAGNET.key)

    a.settings.set(MAGNET, False)

    assert record_stamps.change_seq(a.db, SETTING, MAGNET.key) == seq
    assert record_stamps.of(a.db, SETTING, MAGNET.key) == stamped
    assert a.sync().records.put == []


def test_a_value_this_version_cannot_take_stays_in_the_vault_till_changed_here(
    pair: tuple[Device, Device], cloud: FakeCloud, clock: Clock
) -> None:
    a, b = pair
    a.settings.set(SNOOZE_MINUTES, 30)
    settle(pair)
    put_setting(cloud, SNOOZE_MINUTES.key, 15, clock.now + 1_000)  # a choice of a newer version

    settle(pair)

    assert [device.settings.get(SNOOZE_MINUTES) for device in pair] == [30, 30]
    assert in_vault(cloud)[SNOOZE_MINUTES.key] == 15
    clock.tick(5_000)
    b.settings.set(SNOOZE_MINUTES, 60)
    settle(pair)
    assert [device.settings.get(SNOOZE_MINUTES) for device in pair] == [60, 60]
    assert in_vault(cloud)[SNOOZE_MINUTES.key] == 60


def test_a_setting_this_version_does_not_know_is_left_as_it_is(
    pair: tuple[Device, Device], cloud: FakeCloud, clock: Clock
) -> None:
    a, b = pair
    newer = put_setting(cloud, "note_zoom", 1.5, clock.now)
    # One kept on each device here, as if it followed the user: not taken.
    own = put_setting(cloud, SHORTCUTS.key, {"new-note": "Ctrl+Q"}, clock.now)
    files = cloud.files()

    settle(pair)

    assert {path: cloud.files()[path] for path in (newer, own)} == {
        path: files[path] for path in (newer, own)
    }
    for device in pair:
        assert device.settings.get(SHORTCUTS) == {}
        keys = {str(key) for (key,) in device.db.execute("SELECT key FROM shared_settings")}
        assert keys == set()
    a.settings.set(MAGNET, False)
    settle(pair)
    assert b.settings.get(MAGNET) is False


def test_joining_each_setting_keeps_its_later_change(
    tmp_path: Path, cloud: FakeCloud, clock: Clock
) -> None:
    a = Device("a", tmp_path, cloud, clock, True)
    b = Device("b", tmp_path, cloud, clock, True)  # with settings of its own, before it joins
    a.settings.set(MAGNET, False)
    a.settings.set(RELOCK_MINUTES, 15)
    clock.tick()
    b.settings.set(MORNING, "06:00")
    clock.tick()
    a.settings.set(MORNING, "08:00")
    clock.tick()
    b.settings.set(SNOOZE_MINUTES, 30)
    a.sync()

    joined(b.db, VAULT)
    settle([a, b])

    for device in (a, b):
        assert device.settings.get(MAGNET) is False
        assert device.settings.get(RELOCK_MINUTES) == 15  # never changed on b: the vault's
        assert device.settings.get(MORNING) == "08:00"
        assert device.settings.get(SNOOZE_MINUTES) == 30
    for device in (a, b):
        device.close()


def test_joining_lets_go_of_which_recovery_key_was_said_kept_here(
    tmp_path: Path, cloud: FakeCloud, clock: Clock
) -> None:
    a = Device("a", tmp_path, cloud, clock, True)
    b = Device("b", tmp_path, cloud, clock, True)
    a.settings.set(RECOVERY_KEY_KEPT, "a" * 64)
    a.sync()
    clock.tick()
    b.settings.set(RECOVERY_KEY_KEPT, "b" * 64)  # its own key, kept later than the vault's

    joined(b.db, VAULT)
    settle([a, b])

    assert [device.settings.get(RECOVERY_KEY_KEPT) for device in (a, b)] == ["a" * 64] * 2
    for device in (a, b):
        device.close()


def test_joining_a_vault_that_has_no_word_on_it_keeps_this_ones(
    tmp_path: Path, cloud: FakeCloud, clock: Clock
) -> None:
    a = Device("a", tmp_path, cloud, clock, True)
    b = Device("b", tmp_path, cloud, clock, True)
    a.sync()
    b.settings.set(RECOVERY_KEY_KEPT, "b" * 64)

    joined(b.db, VAULT)
    settle([a, b])

    # It counts only while it is the one the notes have: here it is not, anywhere.
    assert [device.settings.get(RECOVERY_KEY_KEPT) for device in (a, b)] == ["b" * 64] * 2
    for device in (a, b):
        device.close()


def test_keeping_only_the_vaults_notes_takes_its_settings(
    tmp_path: Path, cloud: FakeCloud, clock: Clock
) -> None:
    a = Device("a", tmp_path, cloud, clock, True)
    b = Device("b", tmp_path, cloud, clock, True)
    a.settings.set(MORNING, "08:00")
    a.sync()
    clock.tick()
    b.settings.set(MORNING, "06:00")
    b.settings.set(SNOOZE_MINUTES, 30)

    cloud_only(b.db, b.notes, VAULT, lambda: None)
    settle([a, b])

    for device in (a, b):
        assert device.settings.get(MORNING) == "08:00"
        assert device.settings.get(SNOOZE_MINUTES) == 30  # the vault had none
    for device in (a, b):
        device.close()
