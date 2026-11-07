"""Categories and marks of one name, and colours alike, made apart on two
devices, become one on every device: the first made stays, the notes of the
other move to it, and a change made to a note since still wins."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, WEBDAV_IGNORING, FakeCloud, Kind
from test_records import VAULT, Device, Times

from stickle.data import record_state
from stickle.sync.engine import Report, sync_vault


class Speaking(Device):
    """A device whose built-in marks show in a language of its own."""

    def __init__(self, *args: object, names: dict[str, str] | None = None) -> None:
        super().__init__(*args)  # pyright: ignore[reportArgumentType]
        self.names = names or {}

    def shown(self, mark_id: str, name: str | None) -> str:
        return name or self.names.get(mark_id, mark_id)

    def sync(self) -> Report:
        return sync_vault(self.db, self.place, self.times, self.notes, self.shown)


@pytest.fixture
def times() -> Times:
    return Times()


@pytest.fixture(params=[DROPBOX, WEBDAV_IGNORING], ids=lambda k: k.name)
def cloud(request: pytest.FixtureRequest) -> FakeCloud:
    kind: Kind = request.param
    return FakeCloud(kind)


@pytest.fixture
def devices(tmp_path: Path, cloud: FakeCloud, times: Times) -> Iterator[list[Speaking]]:
    made = [
        Speaking("a", tmp_path, cloud, times, names={"todo": "To do"}),
        Speaking("b", tmp_path, cloud, times, names={"todo": "할 일"}),
        Speaking("c", tmp_path, cloud, times),
    ]
    yield made
    for device in made:
        device.db.close()


def settle(*devices: Device) -> None:
    for _ in range(4):
        for device in devices:
            device.sync()
    for device in devices:
        assert not record_state.unconfirmed(device.db, VAULT)


def names(device: Device) -> list[str]:
    return sorted(category.name for category in device.labels.categories())


def test_two_categories_of_one_name_become_the_first_made(
    devices: list[Speaking], times: Times
) -> None:
    a, b, _ = devices
    first = a.labels.create_category("Work", "blue")
    times.tick()
    second = b.labels.create_category("work", "green")
    on_b = b.notes.create("회의")
    b.notes.set_category(on_b.id, second.id)

    settle(a, b)

    for device in (a, b):
        assert names(device) == ["Work"]
        assert device.notes.get(on_b.id).label == first.id  # pyright: ignore[reportOptionalMemberAccess]
        assert device.labels.category(second.id) is not None  # kept, become one with the first


def test_a_device_that_put_a_note_in_the_other_offline_moves_it_too(
    devices: list[Speaking], times: Times
) -> None:
    a, b, c = devices
    first = a.labels.create_category("일", "blue")
    times.tick()
    second = b.labels.create_category("일", "green")
    settle(b, c)  # c has b's only
    settle(a, b)  # they become one; c is away
    times.tick()
    late = c.notes.create("늦은 메모")
    c.notes.set_category(late.id, second.id)

    settle(a, b, c)

    for device in devices:
        assert names(device) == ["일"]
        assert device.notes.get(late.id).label == first.id  # pyright: ignore[reportOptionalMemberAccess]


def test_a_note_given_another_category_since_keeps_it(
    devices: list[Speaking], times: Times
) -> None:
    a, b, _ = devices
    first = a.labels.create_category("일", "blue")
    times.tick()
    second = b.labels.create_category("일", "green")
    home = b.labels.create_category("집", "yellow")
    note = b.notes.create("장보기")
    b.notes.set_category(note.id, second.id)
    b.sync()
    a.sync()  # a makes them one, and moves the note
    assert a.notes.get(note.id).label == first.id  # pyright: ignore[reportOptionalMemberAccess]
    times.tick()
    b.notes.set_category(note.id, home.id)  # b, not knowing yet

    settle(a, b)

    for device in (a, b):
        assert device.notes.get(note.id).label == home.id  # pyright: ignore[reportOptionalMemberAccess]


def test_renamed_to_the_name_of_another_they_become_one(
    devices: list[Speaking], times: Times
) -> None:
    a, b, _ = devices
    second = b.labels.create_category("일2", "green")
    settle(a, b)
    times.tick()
    a.labels.create_category("일", "blue")  # made after b's, but before b renames it
    a.sync()
    times.tick()

    b.labels.rename_category(second.id, "일")  # b, not knowing of a's
    settle(a, b)

    for device in (a, b):  # b's was made first: it stays
        assert [category.id for category in device.labels.categories()] == [second.id]


def test_a_mark_named_as_a_built_in_one_shows_becomes_it(
    devices: list[Speaking], times: Times
) -> None:
    a, b, _ = devices
    own = b.labels.create_mark("To do", "flag")  # b shows the built-in one as 할 일
    note = b.notes.create("전화하기")
    b.notes.set_mark(note.id, own.id, True)

    settle(a, b)  # a shows it as To do: the two are one

    for device in (a, b):
        assert [mark.id for mark in device.labels.marks()] == [
            "todo",
            "urgent",
            "important",
            "waiting",
        ]
        assert device.notes.get(note.id).marks == {"todo"}  # pyright: ignore[reportOptionalMemberAccess]


def test_three_alike_on_three_devices_become_the_first(
    devices: list[Speaking], times: Times
) -> None:
    made: list[str] = []
    for device in devices:
        made.append(device.labels.create_category("일", "blue").id)
        times.tick()
    notes: list[str] = []
    for device, category in zip(devices, made, strict=True):
        note = device.notes.create(f"{device.id} 메모")
        device.notes.set_category(note.id, category)
        notes.append(note.id)
    settle(devices[2], devices[1])  # the later two, first
    settle(*devices)

    for device in devices:
        assert [category.id for category in device.labels.categories()] == [made[0]]
        assert {device.notes.get(n).label for n in notes} == {made[0]}  # pyright: ignore[reportOptionalMemberAccess]


def test_colours_alike_become_one_and_removed_stay_removed(
    devices: list[Speaking], times: Times
) -> None:
    a, b, _ = devices
    a.colors.add("#336699")
    times.tick()
    b.colors.add("#336699")
    b.colors.add("#993366")
    settle(a, b)
    for device in (a, b):
        assert [color.hex for color in device.colors.colors()] == ["#336699", "#993366"]
        live = device.db.execute("SELECT count(*) FROM custom_colors WHERE deleted_at IS NULL")
        assert live.fetchall() == [(2,)]

    times.tick()
    kept = next(color for color in a.colors.colors() if color.hex == "#336699")
    a.colors.remove(kept.id)
    settle(a, b)

    for device in (a, b):
        assert [color.hex for color in device.colors.colors()] == ["#993366"]
