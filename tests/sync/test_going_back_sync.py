"""Going back to a backup on a computer that syncs: what was gone back to goes to
every computer at the next round, as any change made there; a note emptied
since comes back; the notes made since stay; what did not change since goes
nowhere; a text changed elsewhere meanwhile is kept in a conflict copy; going
back again to the notes as they were before brings every computer back. The
settings that follow the user go back on every computer too, but which
recovery key was said kept, as the key files are not gone back to. One that
does not sync goes back alone, and the cloud gives back what was made since
once it syncs again."""

from collections.abc import Iterator
from datetime import date, time
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, FakeCloud
from sync_bed import KEY, KEYS, VAULT, Clock, Device, settle

from stickle.core.reminders import Rule
from stickle.data import backups, record_stamps, sync_connection
from stickle.data.going_back import GoneBack, keep_sync_state
from stickle.data.record_stamps import SETTING
from stickle.data.settings import MORNING, RECOVERY_KEY_KEPT, SYNC_GONE_BACK, Settings
from stickle.data.versions import VersionRepository

DAY = 24 * 60 * 60 * 1000


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud(DROPBOX)


@pytest.fixture
def pair(tmp_path: Path, cloud: FakeCloud) -> Iterator[tuple[Device, Device]]:
    clock = Clock()
    devices = (Device("a", tmp_path, cloud, clock, True), Device("b", tmp_path, cloud, clock, True))
    for device in devices:
        syncs(device)
    yield devices
    for device in devices:
        device.close()


def syncs(device: Device) -> None:
    """The device keeps a connection, as one that syncs does."""
    login = sync_connection.Login(VAULT, sync_connection.WEBDAV, "https://dav", "u", "p", KEYS.keys)
    sync_connection.keep(device.db, login)


def back_up(device: Device) -> Path:
    device.clock.tick()
    return backups.take(device.db, device.path, device.stamp)


def go_back(device: Device, backup: Path, told: GoneBack | None = None) -> None:
    """As Stickle does it: the backup staged, sync kept as it is now, put in place."""
    device.clock.tick()
    backups.stage_restore(device.path, backup)
    keep_sync_state(device.path, KEY, device.db, device.millis(), told)
    device.db.close()
    assert backups.finish_restore(device.path)
    device.reopen()


def test_what_was_gone_back_to_goes_to_every_computer(pair: tuple[Device, Device]) -> None:
    a, b = pair
    note = a.notes.create("회의 9시")
    settle([a, b])
    backup = back_up(a)
    b.clock.tick()
    b.notes.update_body(note.id, "회의 10시", keep_earlier=True)
    b.notes.set_color(note.id, "blue")
    settle([a, b])

    go_back(a, backup)
    settle([a, b])

    for device in pair:
        there = device.notes.get(note.id)
        assert there is not None
        assert (there.body, there.color) == ("회의 9시", "yellow")
    # The text each had is among its earlier ones.
    for device in pair:
        kept = VersionRepository(device.db, device.stamp).listed(note.id)
        assert any(each.first_line == "회의 10시" for each in kept)
    on_a = VersionRepository(a.db, a.stamp).listed(note.id)
    assert [each.restored for each in on_a if each.first_line == "회의 10시"] == [True]


def test_a_note_emptied_since_comes_back(pair: tuple[Device, Device]) -> None:
    a, b = pair
    note = a.notes.create("장보기")
    settle([a, b])
    backup = back_up(a)
    a.notes.delete(note.id)
    a.notes.purge(note.id)
    settle([a, b])
    assert b.notes.get(note.id) is None

    go_back(a, backup)
    settle([a, b])

    for device in pair:
        there = device.notes.get(note.id)
        assert there is not None and there.body == "장보기" and there.deleted_at is None


def test_the_notes_made_since_stay(pair: tuple[Device, Device]) -> None:
    a, b = pair
    a.notes.create("처음")
    settle([a, b])
    backup = back_up(a)
    made = b.notes.create("나중에 만든 메모")
    category = b.labels.create_category("나중", "green")
    b.notes.set_category(made.id, category.id)
    settle([a, b])

    go_back(a, backup)
    settle([a, b])

    for device in pair:
        there = device.notes.get(made.id)
        assert there is not None and there.body == "나중에 만든 메모"
        assert there.label == category.id
        assert any(each.id == category.id for each in device.labels.categories())


def test_what_did_not_change_since_goes_nowhere(
    pair: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = pair
    for text in ("하나", "둘"):
        a.notes.create(text)
    settle([a, b])
    backup = back_up(a)
    files = cloud.files()

    go_back(a, backup)
    report = a.sync()

    assert report.put == [] and report.records.put == []
    assert {p: d for p, d in cloud.files().items() if not p.startswith("devices/")} == {
        p: d for p, d in files.items() if not p.startswith("devices/")
    }


def test_a_text_changed_elsewhere_meanwhile_is_kept_in_a_copy(pair: tuple[Device, Device]) -> None:
    a, b = pair
    note = a.notes.create("회의 9시")
    settle([a, b])
    backup = back_up(a)
    a.notes.update_body(note.id, "회의 10시", keep_earlier=True)
    settle([a, b])
    b.clock.tick()
    b.notes.update_body(note.id, "회의 11시", keep_earlier=True)  # not sent yet

    go_back(a, backup)
    a.sync()
    settle([a, b])

    for device in pair:
        texts = device.texts()
        assert texts[note.id] == "회의 9시"
        assert sorted(texts.values()) == ["회의 11시", "회의 9시"]  # the other in a copy


def test_a_note_not_changed_here_since_keeps_a_later_text_from_elsewhere(
    pair: tuple[Device, Device],
) -> None:
    a, b = pair
    note = a.notes.create("회의 9시")
    settle([a, b])
    backup = back_up(a)
    b.clock.tick()
    b.notes.update_body(note.id, "회의 11시", keep_earlier=True)  # not here yet

    go_back(a, backup)
    settle([a, b])

    for device in pair:
        assert device.texts() == {note.id: "회의 11시"}


def test_going_back_to_before_brings_every_computer_back(pair: tuple[Device, Device]) -> None:
    a, b = pair
    note = a.notes.create("회의 9시")
    settle([a, b])
    backup = back_up(a)
    a.notes.update_body(note.id, "회의 10시", keep_earlier=True)
    settle([a, b])
    before = back_up(a)  # as Stickle backs up the notes before going back

    go_back(a, backup)
    settle([a, b])
    go_back(a, before)
    settle([a, b])

    for device in pair:
        there = device.notes.get(note.id)
        assert there is not None and there.body == "회의 10시"


def test_the_line_telling_of_it_is_kept_on_a_computer_that_syncs(
    pair: tuple[Device, Device],
) -> None:
    a, _b = pair
    backup = back_up(a)

    go_back(
        a, backup, GoneBack("2026-11-15T01:02:03.000Z", "a-20261115T010203000Z-before-restore.db")
    )

    assert Settings(a.db).get(SYNC_GONE_BACK) == {
        "to": "2026-11-15T01:02:03.000Z",
        "before": "a-20261115T010203000Z-before-restore.db",
    }


def test_one_that_does_not_sync_goes_back_alone_and_takes_it_out_once_on(
    pair: tuple[Device, Device],
) -> None:
    a, b = pair
    note = a.notes.create("회의 9시")
    settle([a, b])
    backup = back_up(a)
    b.notes.update_body(note.id, "회의 10시", keep_earlier=True)
    made = b.notes.create("나중에 만든 메모")
    settle([a, b])
    sync_connection.forget(a.db)  # sync turned off on a

    go_back(a, backup, GoneBack("2026-11-15T01:02:03.000Z", "x.db"))

    assert a.notes.get(made.id) is None  # gone back alone, as before
    assert Settings(a.db).get(SYNC_GONE_BACK) is None
    there = b.notes.get(note.id)
    assert there is not None and there.body == "회의 10시"

    syncs(a)  # on again
    settle([a, b])

    for device in pair:
        assert device.texts()[note.id] == "회의 9시"
        assert device.texts()[made.id] == "나중에 만든 메모"


def test_a_reminder_dealt_with_since_does_not_ring_again(pair: tuple[Device, Device]) -> None:
    a, b = pair
    note = a.notes.create("약 먹기")
    a.reminders.set(note.id, Rule(date(2025, 11, 7), time(9), "Asia/Seoul", "daily"))
    settle([a, b])
    backup = back_up(a)
    a.clock.tick(DAY)
    a.notes.set_color(note.id, "blue")  # and gone back on
    own = a.reminders.of_note(note.id)
    assert own is not None
    dealt = a.reminders.dealt_with(own.id)
    settle([a, b])
    a.clock.tick()

    go_back(a, backup)
    settle([a, b])

    for device in pair:
        there, kept = device.notes.get(note.id), device.reminders.of_note(note.id)
        assert there is not None and there.color == "yellow"
        assert kept is not None and kept.next_day == dealt.next_day


def test_a_category_renamed_since_goes_back_everywhere(pair: tuple[Device, Device]) -> None:
    a, b = pair
    category = a.labels.create_category("회사", "blue")
    settle([a, b])
    backup = back_up(a)
    b.clock.tick()
    b.labels.rename_category(category.id, "일")
    settle([a, b])

    go_back(a, backup)
    settle([a, b])

    for device in pair:
        names = {each.id: each.name for each in device.labels.categories()}
        assert names[category.id] == "회사"


def test_a_setting_gone_back_to_goes_to_every_computer(pair: tuple[Device, Device]) -> None:
    a, b = pair
    a.settings.set(MORNING, "07:00")
    settle([a, b])
    backup = back_up(a)
    b.clock.tick()
    b.settings.set(MORNING, "09:30")
    settle([a, b])

    go_back(a, backup)
    settle([a, b])

    for device in pair:
        assert device.settings.get(MORNING) == "07:00"


def test_which_recovery_key_was_said_kept_is_not_gone_back_on(pair: tuple[Device, Device]) -> None:
    a, b = pair
    a.settings.set(RECOVERY_KEY_KEPT, "a" * 64)
    settle([a, b])
    backup = back_up(a)
    b.clock.tick()
    b.settings.set(RECOVERY_KEY_KEPT, "b" * 64)
    settle([a, b])
    stamped = record_stamps.of(a.db, SETTING, RECOVERY_KEY_KEPT.key)

    a.clock.tick()  # as Stickle does it, the key files' state kept first
    backups.stage_restore(a.path, backup)
    backups.keep_key_state(a.path, KEY, a.db)
    keep_sync_state(a.path, KEY, a.db, a.millis())
    a.db.close()
    assert backups.finish_restore(a.path)
    a.reopen()

    assert a.settings.get(RECOVERY_KEY_KEPT) == "b" * 64
    assert record_stamps.of(a.db, SETTING, RECOVERY_KEY_KEPT.key) == stamped  # no change
    settle([a, b])
    for device in pair:
        assert device.settings.get(RECOVERY_KEY_KEPT) == "b" * 64
