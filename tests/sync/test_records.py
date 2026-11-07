"""The records of a vault, between two devices and a cloud in memory:
categories, marks, colours of the user's own and notes brought in, made or
changed on one, reach the other, each field's later change kept; a note's
category or mark that came after the note is taken once it comes."""

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, ONEDRIVE, WEBDAV_IGNORING, WEBDAV_NO_TAGS, FakeCloud, Kind

from stickle.core.clock import stamp_of
from stickle.crypto.vault_keys import DataKeys
from stickle.data import record_state
from stickle.data.colors import ColorRepository
from stickle.data.imports import SECRET_MARK, ImportRecords
from stickle.data.labels import LabelRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.sync.engine import Place, Report, sync_notes, sync_vault
from stickle.sync.format import RECORDS, item_path, open_envelope, seal
from stickle.sync.record_file import record_id

KEY = secrets.token_bytes(32)
KEYS = DataKeys({1: secrets.token_bytes(32)})
VAULT = "vault-1"
START = 1_762_483_200_000


class Times:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> int:
        return self.now

    def stamp(self) -> str:
        return stamp_of(datetime.fromtimestamp(self.now / 1000, UTC))

    def tick(self, millis: int = 1_000) -> None:
        self.now += millis


class Device:
    def __init__(self, name: str, folder: Path, cloud: FakeCloud, times: Times) -> None:
        self.id = f"{name}-0000-4000-8000-000000000000"
        self.db = open_store(folder / f"{name}.db", KEY)
        self.times = times
        self.notes = NoteRepository(self.db, clock=times.stamp, millis=times)
        self.labels = LabelRepository(self.db, clock=times.stamp, millis=times)
        self.colors = ColorRepository(self.db, clock=times.stamp, millis=times)
        self.imports = ImportRecords(self.db, clock=times.stamp, millis=times)
        self.place = Place(cloud.target(name), VAULT, KEYS, self.id)

    def sync(self) -> Report:
        return sync_vault(self.db, self.place, self.times, self.notes)

    def category_names(self) -> dict[str, str]:
        return {category.id: category.name for category in self.labels.categories()}


@pytest.fixture
def times() -> Times:
    return Times()


@pytest.fixture(params=[DROPBOX, ONEDRIVE, WEBDAV_NO_TAGS, WEBDAV_IGNORING], ids=lambda k: k.name)
def kind(request: pytest.FixtureRequest) -> Kind:
    return request.param


@pytest.fixture
def cloud(kind: Kind) -> FakeCloud:
    return FakeCloud(kind)


@pytest.fixture
def devices(tmp_path: Path, cloud: FakeCloud, times: Times) -> Iterator[tuple[Device, Device]]:
    pair = (Device("a", tmp_path, cloud, times), Device("b", tmp_path, cloud, times))
    yield pair
    for device in pair:
        device.db.close()


def settle(*devices: Device) -> None:
    for _ in range(4):
        for device in devices:
            device.sync()


def no_outbox(device: Device) -> bool:
    return not record_state.unconfirmed(device.db, VAULT)


def test_a_category_made_on_one_comes_to_the_other(
    devices: tuple[Device, Device], times: Times
) -> None:
    a, b = devices
    work = a.labels.create_category("일", "blue", "briefcase")

    settle(a, b)

    there = b.labels.category(work.id)
    assert there is not None
    assert (there.name, there.color, there.icon) == ("일", "blue", "briefcase")
    assert no_outbox(a) and no_outbox(b)


def test_each_field_keeps_its_later_change(devices: tuple[Device, Device], times: Times) -> None:
    a, b = devices
    work = a.labels.create_category("일", "blue")
    settle(a, b)

    b.labels.rename_category(work.id, "회사")
    times.tick()
    a.labels.set_category_color(work.id, "green")
    times.tick()
    a.labels.rename_category(work.id, "업무")  # later than b's
    b.labels.set_category_icon(work.id, "house")
    settle(a, b)

    for device in devices:
        there = device.labels.category(work.id)
        assert there is not None
        assert (there.name, there.color, there.icon) == ("업무", "green", "house")


def test_a_note_named_a_category_not_here_yet_takes_it_once_it_comes(
    devices: tuple[Device, Device],
) -> None:
    a, b = devices
    work = a.labels.create_category("일", "blue")
    note = a.notes.create("회의")
    a.notes.set_category(note.id, work.id)
    a.sync()

    sync_notes(b.db, b.place, b.times, b.notes)  # the note, without its category
    assert b.notes.get(note.id) is not None
    assert b.notes.get(note.id).label is None  # pyright: ignore[reportOptionalMemberAccess]

    b.sync()

    assert b.notes.get(note.id).label == work.id  # pyright: ignore[reportOptionalMemberAccess]


def test_a_note_put_in_a_category_deleted_elsewhere_names_it_on_both(
    devices: tuple[Device, Device], times: Times
) -> None:
    a, b = devices
    work = a.labels.create_category("일", "blue")
    note = a.notes.create("회의")
    settle(a, b)

    a.notes.remove_category(work.id, with_notes=False)
    times.tick()
    b.notes.set_category(note.id, work.id)  # not knowing
    settle(a, b)

    for device in devices:
        assert device.notes.get(note.id).label == work.id  # pyright: ignore[reportOptionalMemberAccess]
        there = device.labels.category(work.id)
        assert there is not None and there.deleted


def test_marks_built_in_and_own(devices: tuple[Device, Device]) -> None:
    a, b = devices
    later = a.labels.create_mark("나중에", "flag")
    a.labels.rename_mark("todo", "할 것")
    note = a.notes.create("장보기")
    a.notes.set_mark(note.id, later.id, True)

    settle(a, b)

    assert b.labels.mark("todo").name == "할 것"  # pyright: ignore[reportOptionalMemberAccess]
    assert b.labels.mark(later.id).name == "나중에"  # pyright: ignore[reportOptionalMemberAccess]
    assert b.notes.get(note.id).marks == {later.id}  # pyright: ignore[reportOptionalMemberAccess]
    names = {entry.path for entry in a.place.target.list(RECORDS)}
    assert item_path(RECORDS, record_id(KEYS, "mark", "todo")) in names
    assert not any("todo" in name for name in names)


def test_own_colours_added_and_removed(devices: tuple[Device, Device], times: Times) -> None:
    a, b = devices
    blue = a.colors.add("#336699")
    settle(a, b)
    assert [color.hex for color in b.colors.colors()] == ["#336699"]

    times.tick()
    b.colors.remove(blue.id)
    settle(a, b)

    assert a.colors.colors() == []


def test_a_note_brought_in_is_known_on_the_other_and_its_hash_goes_once_secret(
    devices: tuple[Device, Device], times: Times
) -> None:
    a, b = devices
    note = a.notes.create("통장")
    a.imports.record("sticky_notes", "s1", "통장", note.id)
    settle(a, b)
    assert b.imports.brought_in("sticky_notes", "s1", "통장")

    times.tick()
    a.notes.make_secret(note.id, "은행", b"sealed")
    settle(a, b)

    rows = b.db.execute("SELECT content_hash FROM import_records").fetchall()
    assert rows == [(SECRET_MARK,)]
    assert b.imports.brought_in("sticky_notes", "s1", "다른 글")  # changed or not: it came


def test_a_record_of_a_kind_not_known_is_left_as_it_is(
    devices: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = devices
    record = "7d1c7e9b-3d2f-4a6b-8c0d-1e2f3a4b5c6d"
    data = seal(KEYS, VAULT, RECORDS, record, {"record": "folder", "item": [1], "fields": {}})
    cloud.put_by_hand(item_path(RECORDS, record), data)

    settle(a, b)

    read = a.place.target.get(item_path(RECORDS, record))
    assert read.data == data
    assert open_envelope(KEYS, read.data, VAULT, RECORDS)[0].item == record
    assert record_state.get(a.db, VAULT, record) is None


def test_a_note_recorded_twice_from_one_source_has_one_text_on_both(
    devices: tuple[Device, Device], times: Times
) -> None:
    a, b = devices
    note = a.notes.create("장보기")
    settle(a, b)
    b.imports.record("xpad", "x-1", "장보기", note.id)
    times.tick()
    a.imports.record("xpad", "x-1", "장보기 끝", note.id)  # later, the same note again

    settle(a, b)

    rows = [
        sorted(device.db.execute("SELECT source, source_id, content_hash FROM import_records"))
        for device in devices
    ]
    assert rows[0] == rows[1]
    assert len(rows[0]) == 1
