"""Files sync cannot take in as they are. One half there is read again, never
set aside; the same bytes failing three rounds running over ten minutes are
set aside (kept here for 30 days) and written over with what this device has,
and what another device wrote there comes back from that device. With nothing
of the item here, the file is passed over until something else is written
there. A copy under another name is merged into its note, then deleted; one
that does not open is left. A note with a field this version may not change
is neither taken in nor written over, and counted."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, WEBDAV_IGNORING, WEBDAV_NO_TAGS, FakeCloud, Kind, Trouble
from sync_bed import KEYS, START, VAULT, Clock, Device, cloud_notes, settle

from stickle.core.hlc import Hlc
from stickle.data import sync_left
from stickle.data.record_stamps import CATEGORY
from stickle.sync.contents import Contents, Field
from stickle.sync.format import NOTES, RECORDS, item_path, open_envelope, seal
from stickle.sync.left import FAILS, WAIT
from stickle.sync.note_file import NoteFile
from stickle.sync.record_file import record_id

MINUTE = 60 * 1000
DAY = 24 * 60 * MINUTE
DAMAGED = b'{"alg":1,"format":1,"id":"x","box":"not a box at all"}'


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(params=[DROPBOX, WEBDAV_IGNORING, WEBDAV_NO_TAGS], ids=lambda k: k.name)
def kind(request: pytest.FixtureRequest) -> Kind:
    return request.param


@pytest.fixture
def two(tmp_path: Path, kind: Kind, clock: Clock) -> Iterator[tuple[FakeCloud, Device, Device]]:
    cloud = FakeCloud(kind)
    a = Device("a", tmp_path, cloud, clock, whole=True, aside=tmp_path / "a-aside")
    b = Device("b", tmp_path, cloud, clock, whole=True, aside=tmp_path / "b-aside")
    yield cloud, a, b
    a.close()
    b.close()


def rounds(device: Device, clock: Clock, times: int = FAILS, apart: int = WAIT // 2) -> None:
    for _ in range(times):
        device.sync()
        clock.tick(apart)


def aside(device: Device) -> list[Path]:
    folder = device.place.aside
    assert folder is not None
    found = folder / VAULT
    return sorted(found.iterdir()) if found.is_dir() else []


def test_a_file_half_arrived_is_read_again_and_never_set_aside(
    tmp_path: Path, clock: Clock
) -> None:
    cloud = FakeCloud(DROPBOX)
    a = Device("a", tmp_path, cloud, clock, whole=True, aside=tmp_path / "aside")
    b = Device("b", tmp_path, cloud, clock, whole=True, aside=tmp_path / "b-aside")
    note = a.notes.create("다 온 메모")
    a.sync()
    path = item_path(NOTES, note.id)
    cloud.trouble = lambda request: Trouble.HALF if request.path == path else None
    rounds(b, clock, times=FAILS + 2, apart=WAIT)
    assert aside(b) == []
    cloud.trouble = lambda _request: None
    b.sync()
    assert b.texts() == {note.id: "다 온 메모"}
    a.close()
    b.close()


def test_the_same_bytes_failing_for_ten_minutes_are_set_aside_and_written_over(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    cloud, a, b = two
    note = a.notes.create("원래 글")
    settle([a, b])
    a.notes.update_body(note.id, "a가 고친 글")
    a.sync()
    cloud.put_by_hand(item_path(NOTES, note.id), DAMAGED)
    rounds(b, clock, times=FAILS - 1)  # not yet: twice
    assert aside(b) == []
    clock.tick(WAIT)
    b.sync()  # the third, ten minutes on: set aside, b's own put there
    assert [each.read_bytes() for each in aside(b)] == [DAMAGED]
    assert cloud_notes(cloud)[note.id].body.text == "원래 글"
    settle([a, b])  # a still has its text: it comes back
    assert a.texts() == b.texts() == {note.id: "a가 고친 글"}


def test_three_failures_within_ten_minutes_set_nothing_aside(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    cloud, a, b = two
    note = a.notes.create("메모")
    settle([a, b])
    cloud.put_by_hand(item_path(NOTES, note.id), DAMAGED)
    rounds(b, clock, times=FAILS + 1, apart=MINUTE)
    assert aside(b) == []


def test_bytes_changing_between_failures_start_the_count_again(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    cloud, a, b = two
    note = a.notes.create("메모")
    settle([a, b])
    for i in range(FAILS + 1):
        cloud.put_by_hand(item_path(NOTES, note.id), DAMAGED + bytes([65 + i]))
        b.sync()
        clock.tick(WAIT)
    assert aside(b) == []


def test_a_file_of_nothing_here_is_kept_aside_and_passed_over_until_written_again(
    two: tuple[FakeCloud, Device, Device], clock: Clock, kind: Kind
) -> None:
    cloud, a, b = two
    note = a.notes.create("b에는 아직 없는 메모")
    a.sync()
    path = item_path(NOTES, note.id)
    good = cloud.files()[path]
    cloud.put_by_hand(path, DAMAGED)
    rounds(b, clock, times=FAILS, apart=WAIT)
    assert [each.read_bytes() for each in aside(b)] == [DAMAGED]
    assert cloud.files()[path] == DAMAGED  # nothing of it here: not written over
    assert sync_left.get(b.db, VAULT, path) is not None
    reads = len([line for line in cloud.log if f" b get {path}" in line])
    b.sync()
    again = len([line for line in cloud.log if f" b get {path}" in line])
    assert again == reads + (1 if kind is WEBDAV_NO_TAGS else 0)  # passed over by its tag
    cloud.put_by_hand(path, good)
    b.sync()
    assert b.texts() == {note.id: "b에는 아직 없는 메모"}
    assert sync_left.get(b.db, VAULT, path) is None


def test_without_a_folder_to_keep_them_in_nothing_is_set_aside(
    tmp_path: Path, clock: Clock
) -> None:
    cloud = FakeCloud(DROPBOX)
    a, b = (
        Device("a", tmp_path, cloud, clock, whole=True),
        Device("b", tmp_path, cloud, clock, whole=True),
    )
    note = a.notes.create("메모")
    settle([a, b])
    cloud.put_by_hand(item_path(NOTES, note.id), DAMAGED)
    rounds(b, clock, times=FAILS + 2, apart=WAIT)
    assert cloud.files()[item_path(NOTES, note.id)] == DAMAGED
    a.close()
    b.close()


def test_bytes_set_aside_are_kept_thirty_days(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    cloud, a, b = two
    first, second = a.notes.create("하나"), a.notes.create("둘")
    settle([a, b])
    cloud.put_by_hand(item_path(NOTES, first.id), DAMAGED)
    rounds(b, clock, times=FAILS, apart=WAIT)
    assert len(aside(b)) == 1
    clock.tick(31 * DAY)
    settle([a, b])
    cloud.put_by_hand(item_path(NOTES, second.id), DAMAGED + b"2")
    rounds(b, clock, times=FAILS, apart=WAIT)
    assert [each.read_bytes() for each in aside(b)] == [DAMAGED + b"2"]


def test_a_record_failing_is_set_aside_and_written_over_too(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    cloud, a, b = two
    category = a.labels.create_category("일", "blue")
    settle([a, b])
    path = item_path(RECORDS, record_id(KEYS, CATEGORY, category.id))
    cloud.put_by_hand(path, DAMAGED)
    rounds(b, clock, times=FAILS, apart=WAIT)
    assert [each.read_bytes() for each in aside(b)] == [DAMAGED]
    head, _ = open_envelope(KEYS, cloud.files()[path], VAULT, RECORDS)
    assert head.item == record_id(KEYS, CATEGORY, category.id)


# Copies under other names


@pytest.mark.parametrize(
    "name", ["{} (1).json", "{} (conflicted copy 2026-11-07).json", "Copy of {}.json"]
)
def test_a_copy_under_another_name_is_merged_into_its_note_and_deleted(
    two: tuple[FakeCloud, Device, Device], name: str
) -> None:
    cloud, a, b = two
    note = a.notes.create("원래 글")
    settle([a, b])
    path = item_path(NOTES, note.id)
    copy = f"{NOTES}/{name.format(note.id)}"
    cloud.copy_by_hand(path, copy)
    settle([a, b])
    assert copy not in cloud.files()
    assert a.texts() == b.texts() == {note.id: "원래 글"}


def test_a_copy_holding_a_text_of_its_own_keeps_it_as_a_conflict_copy(
    two: tuple[FakeCloud, Device, Device], tmp_path: Path, clock: Clock
) -> None:
    cloud, a, b = two
    note = a.notes.create("원래 글")
    settle([a, b])
    path = item_path(NOTES, note.id)
    before = cloud.files()[path]
    c = Device("c", tmp_path, cloud, clock, whole=True)
    c.sync()
    c.notes.update_body(note.id, "사본에만 남은 글")
    c.sync()
    c.close()  # never seen again: its text is in the copy alone
    cloud.copy_by_hand(path, f"{NOTES}/{note.id} (1).json")
    cloud.put_by_hand(path, before)  # the note's own file back as it was
    b.notes.update_body(note.id, "b가 고친 글")  # on the text before, as the copy's is
    settle([a, b])
    assert a.texts() == b.texts()
    assert {"사본에만 남은 글", "b가 고친 글"} <= set(b.texts().values())
    assert f"{NOTES}/{note.id} (1).json" not in cloud.files()


def test_a_copy_that_does_not_open_is_left_and_not_read_again(
    two: tuple[FakeCloud, Device, Device], kind: Kind
) -> None:
    cloud, a, b = two
    a.notes.create("메모")
    settle([a, b])
    copy = f"{NOTES}/desktop.ini"
    cloud.put_by_hand(copy, b"[.ShellClassInfo]")
    b.sync()
    reads = len([line for line in cloud.log if f" b get {copy}" in line])
    b.sync()
    again = len([line for line in cloud.log if f" b get {copy}" in line])
    assert cloud.files()[copy] == b"[.ShellClassInfo]"
    assert again == reads + (1 if kind is WEBDAV_NO_TAGS else 0)
    assert aside(b) == []


def test_a_copy_of_a_record_is_merged_and_deleted(two: tuple[FakeCloud, Device, Device]) -> None:
    cloud, a, b = two
    category = a.labels.create_category("일", "blue")
    settle([a, b])
    record = record_id(KEYS, CATEGORY, category.id)
    copy = f"{RECORDS}/{record} (1).json"
    cloud.copy_by_hand(item_path(RECORDS, record), copy)
    settle([a, b])
    assert copy not in cloud.files()
    assert [c.name for c in b.labels.categories()] == ["일"]


# Fields this version may not change


def test_a_note_with_a_field_this_version_may_not_change_is_left_and_counted(
    two: tuple[FakeCloud, Device, Device], kind: Kind
) -> None:
    cloud, a, b = two
    note = a.notes.create("새 판이 잠근 메모")
    settle([a, b])
    path = item_path(NOTES, note.id)
    _, value = open_envelope(KEYS, cloud.files()[path], VAULT, NOTES)
    file = NoteFile.from_json(KEYS, value)
    lock = Field({"how": 2}, Hlc(START + DAY), "c-0000-4000-8000-000000000000", readonly=True)
    locked = seal(
        KEYS,
        VAULT,
        NOTES,
        note.id,
        NoteFile(
            Contents({**file.contents.fields, "lock2": lock}, file.contents.others), file.body
        ).to_json(),
    )
    cloud.put_by_hand(path, locked)
    b.notes.update_body(note.id, "그래도 고침")
    b.sync()
    assert cloud.files()[path] == locked
    assert sync_left.count(b.db, VAULT, sync_left.READ_ONLY, NOTES) == 1
    assert b.texts() == {note.id: "그래도 고침"}  # here as it was, not put
    reads = len([line for line in cloud.log if f" b get {path}" in line])
    b.notes.update_body(note.id, "또 고침")
    b.sync()
    again = len([line for line in cloud.log if f" b get {path}" in line])
    assert again == reads + (1 if kind is WEBDAV_NO_TAGS else 0)
    assert cloud.files()[path] == locked
