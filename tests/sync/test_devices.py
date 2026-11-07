"""Each device writes its own file in the vault, and only when what it says
changed or a day went by; it reads the others', for what each is called."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, WEBDAV_IGNORING, FakeCloud, Kind
from test_records import Device, Times

from stickle.data.record_state import device_name
from stickle.sync.devices import DAY, About, sync_devices, this_device
from stickle.sync.engine import Report, sync_vault
from stickle.sync.format import DEVICES, item_path


class Named(Device):
    def __init__(self, *args: object, name: str) -> None:
        super().__init__(*args)  # pyright: ignore[reportArgumentType]
        self.about = About(name, "Linux", "0.7.0")

    def sync(self) -> Report:
        return sync_vault(self.db, self.place, self.times, self.notes, about=self.about)


@pytest.fixture
def times() -> Times:
    return Times()


@pytest.fixture(params=[DROPBOX, WEBDAV_IGNORING], ids=lambda k: k.name)
def cloud(request: pytest.FixtureRequest) -> FakeCloud:
    kind: Kind = request.param
    return FakeCloud(kind)


@pytest.fixture
def devices(tmp_path: Path, cloud: FakeCloud, times: Times) -> Iterator[tuple[Named, Named]]:
    pair = (
        Named("a", tmp_path, cloud, times, name="노트북"),
        Named("b", tmp_path, cloud, times, name="회사 PC"),
    )
    yield pair
    for device in pair:
        device.db.close()


def writes(cloud: FakeCloud, device: Named) -> int:
    """Its own file written, as the cloud took it."""
    path = item_path(DEVICES, device.id)
    return sum(1 for line in cloud.log if line.endswith(f" put {path}: ok"))


def test_each_knows_what_the_other_is_called(devices: tuple[Named, Named]) -> None:
    a, b = devices
    a.sync()
    b.sync()
    a.sync()

    assert device_name(a.db, b.id) == "회사 PC"
    assert device_name(b.db, a.id) == "노트북"
    assert device_name(a.db, "c-0000-4000-8000-000000000000") is None


def test_its_own_file_is_written_when_it_has_something_to_say(
    devices: tuple[Named, Named], cloud: FakeCloud, times: Times
) -> None:
    a, b = devices
    for _ in range(3):
        a.sync()
        times.tick(60_000)
    assert writes(cloud, a) == 1

    a.about = About("새 노트북", "Linux", "0.7.0")
    a.sync()
    assert writes(cloud, a) == 2
    times.tick(DAY)
    a.sync()
    assert writes(cloud, a) == 3

    b.sync()
    assert device_name(b.db, a.id) == "새 노트북"


def test_a_file_lost_here_is_found_there_and_written_over(
    devices: tuple[Named, Named], cloud: FakeCloud, times: Times
) -> None:
    a, _ = devices
    a.sync()
    a.db.execute("DELETE FROM sync_devices")  # as if this device had not kept it
    a.about = About("새 이름", "Linux", "0.7.0")

    sync_devices(a.db, a.place, a.times, a.about)

    a.db.execute("DELETE FROM sync_devices")
    sync_devices(a.db, a.place, a.times, About("다른 이름", "Linux", "0.7.0"))
    assert writes(cloud, a) == 3


def test_this_device_is_called_as_its_system_calls_it() -> None:
    about = this_device()
    assert about.name
    assert about.os
    assert this_device("거실 PC").name == "거실 PC"
