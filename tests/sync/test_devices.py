"""Each device writes its own file in the vault, and only when what it says
changed or a day went by; it reads the others', for what each is called."""

from collections.abc import Iterator, Mapping
from pathlib import Path

import apsw
import pytest
from fake_cloud import DROPBOX, WEBDAV_IGNORING, FakeCloud, Kind
from test_records import VAULT, Device, Times, vault_cloud

from stickle.core.hlc import Hlc
from stickle.data import record_state
from stickle.data.record_state import device_name
from stickle.sync.contents import Contents, Field
from stickle.sync.devices import APP, DAY, About, others, sync_devices, this_device
from stickle.sync.engine import Report, sync_vault
from stickle.sync.format import DEVICES, item_path, json_bytes


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
    return vault_cloud(kind)


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
    a.sync()  # its first: said at the start, and what it put at the end
    first = writes(cloud, a)
    for _ in range(2):
        times.tick(60_000)
        a.sync()
    assert writes(cloud, a) == first

    a.about = About("새 노트북", "Linux", "0.7.0")
    a.sync()
    assert writes(cloud, a) == first + 1
    times.tick(DAY)
    a.sync()
    assert writes(cloud, a) == first + 2

    b.sync()
    assert device_name(b.db, a.id) == "새 노트북"


def test_a_file_lost_here_is_found_there_and_written_over(
    devices: tuple[Named, Named], cloud: FakeCloud, times: Times
) -> None:
    a, _ = devices
    a.sync()
    first = writes(cloud, a)
    a.db.execute("DELETE FROM sync_devices")  # as if this device had not kept it
    a.about = About("새 이름", "Linux", "0.7.0")

    sync_devices(a.db, a.place, a.times, a.about)

    a.db.execute("DELETE FROM sync_devices")
    sync_devices(a.db, a.place, a.times, About("다른 이름", "Linux", "0.7.0"))
    assert writes(cloud, a) == first + 2


def test_its_own_file_gone_from_the_vault_is_written_again_at_once(
    devices: tuple[Named, Named], cloud: FakeCloud, times: Times
) -> None:
    """Nothing to say, and said within the day: it is still not there to read."""
    a, b = devices
    a.sync()
    b.sync()
    cloud.remove_by_hand(item_path(DEVICES, a.id))

    times.tick(60_000)
    a.sync()

    assert item_path(DEVICES, a.id) in cloud.files()
    b.db.execute("DELETE FROM sync_devices")
    b.sync()
    assert device_name(b.db, a.id) == "노트북"


def test_its_own_file_gone_for_more_than_a_day_is_written_again(
    devices: tuple[Named, Named], cloud: FakeCloud, times: Times
) -> None:
    """A condition on a file that is no longer there is not met, round after round."""
    a, _ = devices
    a.sync()
    cloud.remove_by_hand(item_path(DEVICES, a.id))

    times.tick(DAY)
    a.sync()

    assert item_path(DEVICES, a.id) in cloud.files()


def test_a_device_whose_file_came_back_is_read_again_from_its_first_round(
    devices: tuple[Named, Named], cloud: FakeCloud, times: Times
) -> None:
    """Its rounds begin again: the others look at the folders, and miss nothing."""
    a, b = devices
    note = a.notes.create("처음").id
    a.sync()
    b.sync()
    cloud.remove_by_hand(item_path(DEVICES, a.id))
    times.tick(60_000)
    a.sync()
    times.tick(60_000)
    a.notes.update_body(note, "고침", keep_earlier=True)
    a.sync()

    times.tick(60_000)
    b.sync()

    found = b.notes.get(note)
    assert found is not None
    assert found.body == "고침"


def test_the_other_devices_are_told_of_by_name_and_not_this_one(
    devices: tuple[Named, Named], times: Times
) -> None:
    a, b = devices
    a.sync()
    b.sync()
    a.sync()

    told = others(a.db, VAULT, a.id)
    assert [(o.name, o.os, o.version, o.synced) for o in told] == [
        ("회사 PC", "Linux", "0.7.0", times())
    ]
    assert [o.name for o in others(b.db, VAULT, b.id)] == ["노트북"]


def test_they_come_in_the_order_of_their_names_whatever_the_case(
    devices: tuple[Named, Named], tmp_path: Path, cloud: FakeCloud, times: Times
) -> None:
    a, b = devices
    b.about = About("Zebra", "Linux", "0.7.0")
    third = Named("c", tmp_path, cloud, times, name="apple")
    for each in (a, b, third, a):
        each.sync()

    assert [o.name for o in others(a.db, VAULT, a.id)] == ["apple", "Zebra"]
    third.db.close()


def test_one_whose_file_is_gone_is_no_longer_told_of_but_keeps_its_name(
    devices: tuple[Named, Named], cloud: FakeCloud, times: Times
) -> None:
    """Its name still says who wrote an earlier version."""
    a, b = devices
    a.sync()
    b.sync()
    a.sync()
    assert others(a.db, VAULT, a.id) != []

    cloud.remove_by_hand(item_path(DEVICES, b.id))
    times.tick(60_000)
    a.sync()

    assert others(a.db, VAULT, a.id) == []
    assert device_name(a.db, b.id) == "회사 PC"


def test_one_that_comes_back_is_told_of_again(
    devices: tuple[Named, Named], cloud: FakeCloud, times: Times
) -> None:
    a, b = devices
    a.sync()
    b.sync()
    a.sync()
    cloud.remove_by_hand(item_path(DEVICES, b.id))
    times.tick(60_000)
    a.sync()

    times.tick(60_000)
    b.sync()
    times.tick(60_000)
    a.sync()

    assert [o.name for o in others(a.db, VAULT, a.id)] == ["회사 PC"]


def test_a_file_there_again_unchanged_is_told_of_again(
    devices: tuple[Named, Named], cloud: FakeCloud, times: Times
) -> None:
    """The same file back, the same tag: not read, and there all the same."""
    a, b = devices
    a.sync()
    b.sync()
    a.sync()
    path = item_path(DEVICES, b.id)
    kept = cloud.files()[path]
    cloud.remove_by_hand(path)
    times.tick(60_000)
    a.sync()
    assert others(a.db, VAULT, a.id) == []

    cloud.put_by_hand(path, kept)
    times.tick(60_000)
    a.sync()

    assert [o.name for o in others(a.db, VAULT, a.id)] == ["회사 PC"]


def test_which_are_there_is_written_only_when_it_changes(
    devices: tuple[Named, Named],
    cloud: FakeCloud,
    times: Times,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = devices
    a.sync()
    b.sync()
    a.sync()
    written: list[dict[str, bool]] = []
    real = record_state.set_listed

    def noted(db: apsw.Connection, vault: str, there: Mapping[str, bool]) -> None:
        written.append(dict(there))
        real(db, vault, there)

    monkeypatch.setattr("stickle.sync.devices.record_state.set_listed", noted)
    times.tick(60_000)
    sync_devices(a.db, a.place, a.times, a.about)
    assert written == []

    cloud.remove_by_hand(item_path(DEVICES, b.id))
    sync_devices(a.db, a.place, a.times, a.about)
    assert written == [{b.id: False}]


@pytest.mark.parametrize(
    "saying",
    [
        {"os": "Linux", "version": "0.7.0", "kind": APP},  # no name
        {"name": "   ", "os": "Linux", "version": "0.7.0", "kind": APP},
        {"name": 7, "os": "Linux", "version": "0.7.0", "kind": APP},
        {"name": "확장", "os": "Linux", "version": "0.7.0", "kind": "extension"},
    ],
    ids=["no name", "blank", "not text", "not an app"],
)
def test_one_not_saying_what_it_is_called_or_not_an_app_is_passed_over(
    devices: tuple[Named, Named], saying: dict[str, object]
) -> None:
    a, _ = devices
    other = "c-0000-4000-8000-000000000000"
    fields = {k: Field(v, Hlc(1), other) for k, v in saying.items()}  # pyright: ignore[reportArgumentType]
    record_state.put_device(a.db, VAULT, other, _kept(Contents(fields, {})), None)

    assert others(a.db, VAULT, a.id) == []


def test_what_it_does_not_say_is_left_out_of_the_rest(devices: tuple[Named, Named]) -> None:
    a, _ = devices
    other = "c-0000-4000-8000-000000000000"
    fields = {
        "name": Field("낡은 PC", Hlc(1), other),
        "kind": Field(APP, Hlc(1), other),
        "os": Field(3, Hlc(1), other),
        "version": Field(None, Hlc(1), other),
        "synced": Field(True, Hlc(1), other),
    }
    record_state.put_device(a.db, VAULT, other, _kept(Contents(fields, {})), None)

    assert [(o.name, o.os, o.version, o.synced) for o in others(a.db, VAULT, a.id)] == [
        ("낡은 PC", "", "", None)
    ]


def _kept(contents: Contents) -> str:
    return json_bytes(contents.to_json()).decode("utf-8")


def test_this_device_is_called_as_its_system_calls_it() -> None:
    about = this_device()
    assert about.name
    assert about.os
    assert this_device("거실 PC").name == "거실 PC"
