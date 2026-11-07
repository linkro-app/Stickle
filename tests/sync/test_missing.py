"""A note the vault had and has no file for any more. A list may show a file
late: only a read that finds none counts. On a device that went through a
round with the vault within the year, it cannot have been emptied and
cleared: the vault went back to an earlier day, or the file was deleted, and
the device puts its own back. On one away more than a year, it was: the note
goes to the trash there (said once), put with its text if it was edited
there since, else kept there only and not looked for again. A record's file
gone is always put back."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, WEBDAV_IGNORING, FakeCloud, Kind, Trouble
from sync_bed import KEYS, VAULT, Clock, Device, cloud_notes, settle

from stickle.data import sync_state
from stickle.data.record_stamps import CATEGORY
from stickle.sync.format import NOTES, RECORDS, item_path
from stickle.sync.merge import YEAR
from stickle.sync.record_file import record_id

DAY = 24 * 60 * 60 * 1000


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(params=[DROPBOX, WEBDAV_IGNORING], ids=lambda k: k.name)
def kind(request: pytest.FixtureRequest) -> Kind:
    return request.param


@pytest.fixture
def two(tmp_path: Path, kind: Kind, clock: Clock) -> Iterator[tuple[FakeCloud, Device, Device]]:
    cloud = FakeCloud(kind)
    a = Device("a", tmp_path, cloud, clock, whole=True)
    b = Device("b", tmp_path, cloud, clock, whole=True)
    yield cloud, a, b
    a.close()
    b.close()


def gets(cloud: FakeCloud, device: str, path: str) -> int:
    return len([line for line in cloud.log if f" {device} get {path}:" in line])


def emptied_and_cleared(a: Device, b: Device, note_id: str, clock: Clock) -> None:
    """b empties the note, and a year on its deletion is cleared from the vault;
    a, away all that time, syncs no more."""
    b.notes.delete(note_id)
    b.notes.purge(note_id)
    b.sync()
    clock.tick(YEAR + DAY)
    b.sync()  # the deletion a year in the vault: its file removed


# Within the year


def test_a_notes_file_deleted_by_hand_is_put_back(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    cloud, a, b = two
    note = a.notes.create("남아야 할 메모")
    settle([a, b])
    cloud.remove_by_hand(item_path(NOTES, note.id))
    clock.tick(30 * DAY)
    report = b.sync()
    assert report.trashed == [] and report.put == [note.id]
    settle([a, b])
    for device in (a, b):
        found = device.notes.get(note.id)
        assert found is not None and found.deleted_at is None and found.body == "남아야 할 메모"


def test_notes_made_after_the_day_a_vault_went_back_to_are_put_back(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    cloud, a, b = two
    first = a.notes.create("되돌린 날에도 있던 메모")
    settle([a, b])
    back_to = cloud.requests
    later = a.notes.create("그 뒤에 만든 메모")
    settle([a, b])
    cloud.go_back_to(back_to)
    assert item_path(NOTES, later.id) not in cloud.files()
    settle([a, b])
    assert item_path(NOTES, later.id) in cloud.files()
    for device in (a, b):
        assert device.notes.deleted() == []
        assert set(device.texts()) == {first.id, later.id}


def test_a_file_a_list_shows_late_is_not_taken_for_gone(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    cloud, a, b = two
    note = a.notes.create("처음 글")
    settle([a, b])
    path = item_path(NOTES, note.id)
    a.notes.update_body(note.id, "a가 고친 글")
    cloud.trouble = lambda request: (
        Trouble.LATE if (request.op, request.path) == ("put", path) else None
    )
    a.sync()
    cloud.trouble = lambda _request: None
    report = b.sync()  # not in the list yet: read, and there
    assert report.put == [] and report.trashed == []
    assert b.texts() == {note.id: "a가 고친 글"}


def test_a_note_emptied_here_too_is_put_back_within_the_year_and_let_go_after(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    cloud, a, b = two
    note = a.notes.create("비울 메모")
    settle([a, b])
    a.notes.delete(note.id)
    a.notes.purge(note.id)
    settle([a, b])
    path = item_path(NOTES, note.id)
    deletion = cloud_notes(cloud)[note.id].to_json()
    cloud.remove_by_hand(path)
    a.sync()
    assert cloud_notes(cloud)[note.id].to_json() == deletion  # put back as it was
    cloud.remove_by_hand(path)
    clock.tick(YEAR + DAY)
    a.sync()
    assert path not in cloud.files()
    assert sync_state.get(a.db, VAULT, note.id) is None
    assert a.notes.deletion_records() == []


def test_a_records_file_gone_is_put_back(two: tuple[FakeCloud, Device, Device]) -> None:
    cloud, a, b = two
    category = a.labels.create_category("일", "blue")
    settle([a, b])
    path = item_path(RECORDS, record_id(KEYS, CATEGORY, category.id))
    cloud.remove_by_hand(path)
    b.sync()
    assert path in cloud.files()
    settle([a, b])
    assert [c.name for c in a.labels.categories()] == [c.name for c in b.labels.categories()]


# Away more than a year


def test_a_note_emptied_and_cleared_while_away_goes_to_the_trash_here_only(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    cloud, a, b = two
    note = a.notes.create("오래 쉰 PC의 메모")
    settle([a, b])
    emptied_and_cleared(a, b, note.id, clock)
    path = item_path(NOTES, note.id)
    assert path not in cloud.files()

    report = a.sync()

    assert report.trashed == [note.id]
    there = a.notes.get(note.id)
    assert there is not None and there.deleted_at is not None
    assert path not in cloud.files()  # kept here only
    state = sync_state.get(a.db, VAULT, note.id)
    assert state is not None and state.gone
    before = gets(cloud, "a", path)
    assert a.sync().trashed == []
    assert gets(cloud, "a", path) == before  # not looked for again
    assert b.notes.get(note.id) is None


def test_a_note_edited_while_away_goes_to_the_trash_with_its_text_everywhere(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    cloud, a, b = two
    note = a.notes.create("오래 쉰 PC의 메모")
    settle([a, b])
    a.notes.update_body(note.id, "쉬는 동안 고친 글")
    emptied_and_cleared(a, b, note.id, clock)

    assert a.sync().trashed == [note.id]
    settle([a, b])

    for device in (a, b):
        there = device.notes.get(note.id)
        assert there is not None and there.deleted_at is not None
        assert there.body == "쉬는 동안 고친 글"
    assert item_path(NOTES, note.id) in cloud.files()
    assert cloud_notes(cloud)[note.id].body.text == "쉬는 동안 고친 글"


def test_a_note_kept_here_only_taken_out_of_the_trash_goes_back_to_every_device(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    _, a, b = two
    note = a.notes.create("되살릴 메모")
    settle([a, b])
    emptied_and_cleared(a, b, note.id, clock)
    a.sync()
    a.notes.restore(note.id)
    settle([a, b])
    for device in (a, b):
        there = device.notes.get(note.id)
        assert there is not None and there.deleted_at is None


def test_a_note_kept_here_only_is_emptied_a_year_after_it_went_in_the_trash(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    cloud, a, b = two
    note = a.notes.create("이 PC에만 남은 메모")
    settle([a, b])
    emptied_and_cleared(a, b, note.id, clock)
    a.sync()
    clock.tick(YEAR + DAY)
    a.sync()
    assert a.notes.get(note.id) is None
    assert sync_state.get(a.db, VAULT, note.id) is None
    before = len(cloud.log)
    a.sync()
    assert not [line for line in cloud.log[before:] if note.id in line]


def test_a_device_never_through_a_round_since_the_upgrade_puts_its_notes_back(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    cloud, a, b = two
    note = a.notes.create("메모")
    settle([a, b])
    a.db.execute("UPDATE sync_vaults SET synced_at = NULL")  # as a round of the version before
    cloud.remove_by_hand(item_path(NOTES, note.id))
    clock.tick(YEAR + DAY)
    assert a.sync().trashed == []
    assert item_path(NOTES, note.id) in cloud.files()
