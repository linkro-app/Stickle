"""What devices tell one another of what they put (stickle.sync.devices), and
what a round asks the cloud for because of it: no folder listed unless it has
to be, only the files that changed read, nothing new asked of a quiet round."""

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fake_cloud import DROPBOX, WEBDAV, FakeCloud, Kind, Request, Trouble
from sync_bed import KEYS, VAULT, Clock, Device, settle

from stickle.data import record_state, sync_state
from stickle.sync.devices import KEPT
from stickle.sync.format import DEVICES, NOTES, RECORDS, VAULT_FILE, item_path, open_envelope
from stickle.sync.targets.base import UnavailableError

GET_VAULT = f"get {VAULT_FILE}"
LIST_DEVICES = f"list {DEVICES}"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(params=[DROPBOX, WEBDAV], ids=lambda k: k.name)
def kind(request: pytest.FixtureRequest) -> Kind:
    return request.param


@pytest.fixture
def cloud(kind: Kind) -> FakeCloud:
    return FakeCloud(kind)


@pytest.fixture
def pair(tmp_path: Path, cloud: FakeCloud, clock: Clock) -> Iterator[tuple[Device, Device]]:
    a = Device("a", tmp_path, cloud, clock, whole=True)
    b = Device("b", tmp_path, cloud, clock, whole=True)
    settle([a, b])  # their first rounds, all looked at
    a.full = b.full = False
    yield a, b
    a.close()
    b.close()


def asked(cloud: FakeCloud, device: Device, since: int) -> list[str]:
    """What the device asked of the cloud after the log's line since, and got."""
    found: list[str] = []
    for line in cloud.log[since:]:
        _number, name, what, path = line.split(" ", 3)
        if name == device.name:
            found.append(f"{what} {path.rpartition(':')[0]}")
    return found


def note_path(note_id: str) -> str:
    return item_path(NOTES, note_id)


def own_file(cloud: FakeCloud, device: Device) -> dict[str, object]:
    _, value = open_envelope(KEYS, cloud.files()[item_path(DEVICES, device.id)], VAULT, DEVICES)
    assert isinstance(value, dict)
    fields = value["fields"]
    assert isinstance(fields, dict)
    return {name: each["value"] for name, each in fields.items()}  # pyright: ignore


def test_a_quiet_round_asks_two_things(cloud: FakeCloud, pair: tuple[Device, Device]) -> None:
    a, _ = pair
    since = len(cloud.log)

    a.sync()
    a.sync()

    assert asked(cloud, a, since) == [GET_VAULT, LIST_DEVICES] * 2


def test_a_note_changed_elsewhere_is_all_that_is_read(
    cloud: FakeCloud, pair: tuple[Device, Device]
) -> None:
    a, b = pair
    note = a.notes.create("여기서 쓴 글")
    a.sync()
    since, before = len(cloud.log), cloud.bytes_read

    b.sync()

    assert asked(cloud, b, since) == [
        GET_VAULT,
        LIST_DEVICES,
        f"get {item_path(DEVICES, a.id)}",
        f"get {note_path(note.id)}",
    ]
    assert b.texts()[note.id] == "여기서 쓴 글"
    files = cloud.files()
    size = sum(len(files[p]) for p in (VAULT_FILE, item_path(DEVICES, a.id), note_path(note.id)))
    assert cloud.bytes_read - before == size


def test_a_record_changed_elsewhere_comes_the_same_way(
    cloud: FakeCloud, pair: tuple[Device, Device]
) -> None:
    a, b = pair
    made = a.labels.create_category("업무-표식", "blue")
    a.sync()
    since = len(cloud.log)

    b.sync()

    ask = asked(cloud, b, since)
    assert f"list {NOTES}" not in ask and f"list {RECORDS}" not in ask
    assert [c.id for c in b.labels.categories() if c.name == "업무-표식"] == [made.id]


def test_what_it_was_told_it_reads_once(cloud: FakeCloud, pair: tuple[Device, Device]) -> None:
    a, b = pair
    note = a.notes.create("한 번만")
    a.sync()
    b.sync()
    since = len(cloud.log)

    b.sync()

    assert f"get {note_path(note.id)}" not in asked(cloud, b, since)


def test_a_note_changed_here_is_read_back_only_where_tags_cannot_tell(
    cloud: FakeCloud, kind: Kind, pair: tuple[Device, Device]
) -> None:
    a, _ = pair
    note = a.notes.create("올린 글")
    a.sync()
    since = len(cloud.log)

    a.sync()
    got = asked(cloud, a, since).count(f"get {note_path(note.id)}")
    assert got == (0 if kind is DROPBOX else 1)  # the safety net where a write can be lost

    since = len(cloud.log)
    a.sync()
    assert f"get {note_path(note.id)}" not in asked(cloud, a, since)


def test_it_is_told_only_what_was_put_since_it_last_looked(
    cloud: FakeCloud, pair: tuple[Device, Device]
) -> None:
    a, b = pair
    first = a.notes.create("첫째")
    a.sync()
    b.sync()
    second = a.notes.create("둘째")
    a.sync()
    since = len(cloud.log)

    b.sync()

    ask = asked(cloud, b, since)
    assert f"get {note_path(second.id)}" in ask and f"get {note_path(first.id)}" not in ask


def test_a_full_look_lists_every_folder_and_reads_only_what_differs(
    cloud: FakeCloud, pair: tuple[Device, Device]
) -> None:
    a, b = pair
    alike = a.notes.create("그대로")
    a.sync()
    b.sync()
    changed = a.notes.create("새 글")
    a.sync()
    b.full = True
    since = len(cloud.log)

    b.sync()

    ask = asked(cloud, b, since)
    assert f"list {NOTES}" in ask and f"list {RECORDS}" in ask
    assert f"get {note_path(changed.id)}" in ask and f"get {note_path(alike.id)}" not in ask


def test_a_file_changed_with_no_word_of_it_waits_for_a_full_look(
    cloud: FakeCloud, pair: tuple[Device, Device]
) -> None:
    a, b = pair
    note = a.notes.create("글")
    settle([a, b], full=False)
    a.full = True
    copy = cloud.files()[note_path(note.id)]
    cloud.remove_by_hand(note_path(note.id))
    cloud.put_by_hand(note_path(note.id), copy)  # written anew by something else
    since = len(cloud.log)

    b.sync()
    assert f"get {note_path(note.id)}" not in asked(cloud, b, since)

    b.full = True
    b.sync()
    assert f"get {note_path(note.id)}" in asked(cloud, b, since)


def _failing(cloud: FakeCloud, path: str, trouble: Trouble) -> None:
    """The next request for that file goes wrong."""
    left = [trouble]

    def chosen(request: Request) -> Trouble | None:
        return left.pop() if left and request.path == path and request.op == "put" else None

    cloud.trouble = chosen


def test_what_could_not_be_told_is_told_the_next_round(
    cloud: FakeCloud, pair: tuple[Device, Device]
) -> None:
    a, b = pair
    note = a.notes.create("끊긴 뒤")
    _failing(cloud, item_path(DEVICES, a.id), Trouble.UNAVAILABLE)
    with pytest.raises(UnavailableError):
        a.sync()
    assert sync_state.untold(a.db, VAULT) == [note_path(note.id)]

    b.sync()
    assert note.id not in b.texts()

    a.sync()
    assert sync_state.untold(a.db, VAULT) == []
    b.sync()
    assert b.texts()[note.id] == "끊긴 뒤"


def test_a_put_whose_answer_was_lost_is_told_all_the_same(
    cloud: FakeCloud, pair: tuple[Device, Device]
) -> None:
    a, b = pair
    note = a.notes.create("답만 잃음")
    _failing(cloud, note_path(note.id), Trouble.LOST)
    with pytest.raises(UnavailableError):
        a.sync()
    assert note_path(note.id) in sync_state.untold(a.db, VAULT)

    a.sync()
    b.sync()

    assert b.texts()[note.id] == "답만 잃음"


def test_a_file_read_half_is_read_again_though_nothing_new_was_said(
    cloud: FakeCloud, pair: tuple[Device, Device]
) -> None:
    a, b = pair
    note = a.notes.create("반만 받음")
    a.sync()
    left = [Trouble.HALF]

    def chosen(request: Request) -> Trouble | None:
        wanted = request.op == "get" and request.path == note_path(note.id)
        return left.pop() if left and wanted else None

    cloud.trouble = chosen
    report = b.sync()
    assert report.left == [note.id]

    b.sync()

    assert b.texts()[note.id] == "반만 받음"


def test_a_device_further_behind_than_is_named_lists_the_folders(
    cloud: FakeCloud, pair: tuple[Device, Device]
) -> None:
    a, b = pair
    notes = [a.notes.create(f"글 {n}") for n in range(KEPT + 5)]
    a.sync()
    assert own_file(cloud, a)["forgot"] == own_file(cloud, a)["round"]
    since = len(cloud.log)

    b.sync()

    assert f"list {NOTES}" in asked(cloud, b, since)
    assert len(set(b.texts()) & {n.id for n in notes}) == len(notes)

    since = len(cloud.log)
    b.sync()
    assert f"list {NOTES}" not in asked(cloud, b, since)


def test_a_file_put_again_is_named_once_at_its_last_round(
    cloud: FakeCloud, pair: tuple[Device, Device]
) -> None:
    a, _ = pair
    note = a.notes.create("첫 글")
    a.sync()
    a.notes.update_body(note.id, "둘째 글")
    a.sync()

    said = own_file(cloud, a)
    recent = cast(list[list[object]], said["recent"])
    named = [each for each in recent if each[1] == note_path(note.id)]
    assert named == [[said["round"], note_path(note.id)]]


def test_a_devices_rounds_gone_back_have_the_others_list_the_folders(
    cloud: FakeCloud, pair: tuple[Device, Device]
) -> None:
    a, b = pair
    for text in ("하나", "둘"):
        a.notes.create(text)
        a.sync()
    b.sync()
    a.db.execute("DELETE FROM sync_devices WHERE device = ?", (a.id,))  # its count starts over
    cloud.remove_by_hand(item_path(DEVICES, a.id))
    a.notes.create("셋")
    a.sync()
    since = len(cloud.log)

    b.sync()

    assert f"list {NOTES}" in asked(cloud, b, since)
    assert "셋" in b.texts().values()


def test_writes_of_its_own_file_in_turn_differ_in_size(
    cloud: FakeCloud, pair: tuple[Device, Device]
) -> None:
    a, _ = pair
    sizes: list[int] = []
    pads: list[object] = []
    for day in range(3):
        a.clock.tick(25 * 60 * 60 * 1000 * (day + 1))
        a.sync()
        sizes.append(len(cloud.files()[item_path(DEVICES, a.id)]))
        pads.append(own_file(cloud, a)["pad"])
    assert sizes[0] != sizes[1] != sizes[2]
    assert pads[0] != pads[1] != pads[2]


def test_a_day_passing_tells_of_nothing_to_read(
    cloud: FakeCloud, pair: tuple[Device, Device]
) -> None:
    a, b = pair
    a.clock.tick(25 * 60 * 60 * 1000)
    a.sync()
    since = len(cloud.log)

    b.sync()

    # (And a day on for it too: it says so in its own file.)
    ask = [each for each in asked(cloud, b, since) if not each.startswith("put ")]
    assert ask == [GET_VAULT, LIST_DEVICES, f"get {item_path(DEVICES, a.id)}"]


def test_its_first_round_lists_though_it_was_asked_not_to(
    tmp_path: Path, cloud: FakeCloud, clock: Clock
) -> None:
    a = Device("a", tmp_path, cloud, clock, whole=True)
    a.notes.create("먼저 쓴 글")
    a.sync()
    b = Device("b", tmp_path, cloud, clock, whole=True)
    b.full = False
    since = len(cloud.log)

    b.sync()

    assert f"list {NOTES}" in asked(cloud, b, since)
    assert "먼저 쓴 글" in b.texts().values()
    a.close()
    b.close()


def test_how_far_it_has_taken_in_is_kept_only_once_a_round_is_through(
    cloud: FakeCloud, pair: tuple[Device, Device]
) -> None:
    a, b = pair
    note = a.notes.create("글")
    a.sync()
    taken = record_state.taken(b.db, VAULT)[a.id]
    left = [Trouble.UNAVAILABLE]

    def chosen(request: Request) -> Trouble | None:
        wanted = request.op == "get" and request.path == note_path(note.id)
        return left.pop() if left and wanted else None

    cloud.trouble = chosen
    with pytest.raises(UnavailableError):
        b.sync()
    assert record_state.taken(b.db, VAULT)[a.id] == taken

    b.sync()
    assert record_state.taken(b.db, VAULT)[a.id] > taken
    assert b.texts()[note.id] == "글"
