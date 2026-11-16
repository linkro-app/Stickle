"""Where notes are on screen, between devices and a cloud in memory: a place
for as many monitors as there are moves with the note to the devices with as
many, the others' places for other numbers left as they are; the size goes
alone; a place only arranged here goes nowhere and gives way to one set
elsewhere; another device's place is told apart (found by number); a value
of another shape is left in the vault; a conflict copy takes no place; a
note folded into another on joining hands its places over."""

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fake_cloud import DROPBOX, FakeCloud
from sync_bed import KEYS, VAULT, Clock, Device, settle

from stickle.core.hlc import Hlc
from stickle.core.layout import Place, Size
from stickle.data import stamps
from stickle.data.layouts import LayoutRepository
from stickle.sync.contents import Contents, Field
from stickle.sync.engine import joined
from stickle.sync.format import NOTES, Json, item_path, open_envelope, seal
from stickle.sync.note_file import NoteFile

LAPTOP = Place(1, "eDP-1", 0.1, 0.2, ("eDP-1",))
DESK = Place(2, "HDMI-1", 0.5, 0.25, ("eDP-1", "HDMI-1"))
OTHER_DESK = Place(1, "HDMI-1", 0.3, 0.3, ("eDP-1", "HDMI-1"))
SMALL, LARGE = Size(240, 180), Size(400, 300)


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


def layouts(device: Device) -> LayoutRepository:
    return LayoutRepository(device.db, device.stamp, device.millis)


def file_of(cloud: FakeCloud, note_id: str) -> NoteFile:
    data = cloud.files()[item_path(NOTES, note_id)]
    return NoteFile.from_json(KEYS, open_envelope(KEYS, data, VAULT, NOTES)[1])


def test_a_place_goes_to_the_devices_with_as_many_monitors(
    pair: tuple[Device, Device], clock: Clock
) -> None:
    a, b = pair
    note = a.notes.create("회의")
    layouts(a).save(note.id, SMALL, (1, LAPTOP))
    settle(pair)
    layouts(b).save(note.id, SMALL, (2, DESK))  # b has two monitors
    clock.tick()
    layouts(a).save(note.id, SMALL, (1, Place(1, "eDP-1", 0.6, 0.6, ("eDP-1",))))

    settle(pair)

    for device in pair:
        assert layouts(device).places(note.id) == {
            1: Place(1, "eDP-1", 0.6, 0.6, ("eDP-1",)),
            2: DESK,
        }


def test_the_size_goes_alone(pair: tuple[Device, Device], clock: Clock) -> None:
    a, b = pair
    note = a.notes.create("장보기")
    layouts(a).save(note.id, SMALL, (2, DESK))
    settle(pair)
    clock.tick()
    layouts(b).save(note.id, LARGE)  # resized, not moved
    clock.tick()
    layouts(a).save(note.id, SMALL, (2, OTHER_DESK))  # moved later, the same size

    settle(pair)

    for device in pair:
        assert layouts(device).size(note.id) == LARGE
        assert layouts(device).places(note.id) == {2: OTHER_DESK}


def test_a_move_is_a_change_of_the_note_not_of_its_text(pair: tuple[Device, Device]) -> None:
    a, b = pair
    note = a.notes.create("회의")
    settle(pair)
    version = file_of(a.cloud, note.id).body.id
    before = a.notes.get(note.id)
    assert before is not None

    layouts(a).save(note.id, SMALL, (1, LAPTOP))

    after = a.notes.get(note.id)
    assert after is not None and after.change_seq > before.change_seq
    assert after.updated_at == before.updated_at  # when it last changed, as shown
    settle(pair)
    assert file_of(a.cloud, note.id).body.id == version  # no new version of its text
    assert layouts(b).places(note.id) == {1: LAPTOP}
    assert b.notes.get(note.id).body == "회의"  # pyright: ignore[reportOptionalMemberAccess]


def test_a_place_only_arranged_goes_nowhere_and_gives_way(
    pair: tuple[Device, Device], cloud: FakeCloud, clock: Clock
) -> None:
    a, b = pair
    note = a.notes.create("환영")
    layouts(a).arrange(note.id, 1, LAPTOP, SMALL)
    settle(pair)

    assert "place:1" not in file_of(cloud, note.id).contents.fields
    assert layouts(b).places(note.id) == {}
    clock.tick()
    layouts(b).save(note.id, LARGE, (1, OTHER_DESK))
    settle(pair)
    for device in pair:
        assert layouts(device).places(note.id) == {1: OTHER_DESK}
        assert layouts(device).size(note.id) == LARGE


def test_arranged_it_stays_where_it_was_put(tmp_path: Path, cloud: FakeCloud, clock: Clock) -> None:
    a = Device("a", tmp_path, cloud, clock, True)
    note = a.notes.create("환영")
    layouts(a).arrange(note.id, 1, LAPTOP, SMALL)
    layouts(a).arrange(note.id, 1, OTHER_DESK, LARGE)  # once arranged, kept

    assert layouts(a).places(note.id) == {1: LAPTOP}
    assert layouts(a).size(note.id) == SMALL
    assert stamps.of(a.db, note.id) == {}
    a.close()


def test_a_place_set_elsewhere_is_told_apart(pair: tuple[Device, Device]) -> None:
    a, b = pair
    note = a.notes.create("회의")
    layouts(a).save(note.id, SMALL, (2, DESK))
    settle(pair)
    layouts(b).save(note.id, SMALL, (1, LAPTOP))
    settle(pair)

    assert layouts(a).elsewhere(note.id) == {1}
    assert layouts(b).elsewhere(note.id) == {2}


def test_a_place_of_another_shape_is_left_in_the_vault(
    pair: tuple[Device, Device], cloud: FakeCloud, clock: Clock
) -> None:
    a, _ = pair
    note = a.notes.create("회의")
    layouts(a).save(note.id, SMALL, (2, DESK))
    settle(pair)
    there = file_of(cloud, note.id)
    newer = dict(there.contents.fields)
    newer["place:2"] = Field(  # a newer version's, with more in it
        {"monitor": 2, "name": "HDMI-1", "setup": [], "x": 0.5, "y": 0.5, "desk": 3},
        Hlc(clock.now + 1_000),
        "c0c0c0c0-0000-4000-8000-000000000000",
    )
    changed = NoteFile(Contents(newer, there.contents.others), there.body)
    path = item_path(NOTES, note.id)
    cloud.put_by_hand(path, seal(KEYS, VAULT, NOTES, note.id, changed.to_json()))

    settle(pair)

    for device in pair:
        assert layouts(device).places(note.id) == {2: DESK}
    place = file_of(cloud, note.id).contents.fields["place:2"].value
    assert isinstance(place, dict) and cast(dict[str, Json], place)["desk"] == 3


def test_a_conflict_copy_takes_no_place(pair: tuple[Device, Device], clock: Clock) -> None:
    a, b = pair
    note = a.notes.create("회의 9시")
    layouts(a).save(note.id, SMALL, (1, LAPTOP))
    settle(pair)
    a.notes.update_body(note.id, "회의 10시", keep_earlier=True)
    clock.tick()
    b.notes.update_body(note.id, "회의 11시", keep_earlier=True)

    settle(pair)

    copies = [each.id for each in a.notes.copies()]
    assert len(copies) == 1
    for device in pair:
        assert layouts(device).places(copies[0]) == {}
        assert layouts(device).size(copies[0]) == SMALL


def test_a_note_folded_into_the_vaults_on_joining_hands_its_places_over(
    tmp_path: Path, cloud: FakeCloud, clock: Clock
) -> None:
    a = Device("a", tmp_path, cloud, clock, True)
    b = Device("b", tmp_path, cloud, clock, True)
    theirs = a.notes.create("같은 글")
    layouts(a).save(theirs.id, SMALL, (2, DESK))
    a.sync()
    mine = b.notes.create("같은 글")
    layouts(b).save(mine.id, LARGE, (1, LAPTOP))

    joined(b.db, VAULT)
    settle([a, b])

    assert b.notes.get(mine.id) is None
    for device in (a, b):
        assert layouts(device).places(theirs.id) == {1: LAPTOP, 2: DESK}
    for device in (a, b):
        device.close()
