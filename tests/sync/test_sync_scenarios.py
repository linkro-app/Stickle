"""Cases of sync between devices that matter enough to be kept as they are,
rather than left for the property test to come across: the trash and emptying
it crossing on two devices, one conflict seen on two, the same text reached
two ways, the vault gone back to what it was, an answer lost, clocks hours
off or far in the future, the notes of 0.6 synced the first time, and the app
ended in the middle of a round."""

import importlib.util
import shutil
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from fake_cloud import DROPBOX, WEBDAV, WEBDAV_IGNORING, FakeCloud, Request, Trouble, Turns
from sync_bed import KEYS, VAULT, Clock, Device, cloud_notes, settle
from test_vault_rules import WHAT_THE_USER_WROTE, plain_in

from stickle.core.hlc import END, Hlc
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.versions import VersionRepository
from stickle.platform.credentials import CredentialStore
from stickle.sync.contents import Contents
from stickle.sync.format import NOTES, item_path, seal
from stickle.sync.merge import YEAR, copy_id
from stickle.sync.note_file import NoteFile
from stickle.sync.targets.base import UnavailableError
from stickle.unlock import Unlock

HOUR = 60 * 60 * 1000


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud(DROPBOX)


@pytest.fixture
def two(tmp_path: Path, cloud: FakeCloud, clock: Clock) -> Iterator[tuple[Device, Device]]:
    pair = (Device("a", tmp_path, cloud, clock), Device("b", tmp_path, cloud, clock))
    yield pair
    for device in pair:
        device.close()


def shared(a: Device, b: Device, text: str = "함께 쓰는 메모") -> str:
    """A note made on a and synced to b."""
    note = a.notes.create(text)
    settle([a, b])
    return note.id


# The trash and emptying it, done on two devices before either saw the other's


def _edit(device: Device, note_id: str) -> None:
    device.notes.update_body(note_id, "다른 PC에서 고친 글", keep_earlier=True)


def _color(device: Device, note_id: str) -> None:
    device.notes.set_color(note_id, "blue")


def _trash(device: Device, note_id: str) -> None:
    device.notes.delete(note_id)


def _empty(device: Device, note_id: str) -> None:
    device.notes.delete(note_id)
    device.notes.purge(note_id)


@pytest.mark.parametrize(
    ("one", "other", "text", "color", "trashed"),
    [
        (_trash, _edit, "다른 PC에서 고친 글", "yellow", True),
        (_trash, _color, "함께 쓰는 메모", "blue", True),
        (_empty, _edit, "다른 PC에서 고친 글", "yellow", True),  # back, in the trash
        (_empty, _color, None, None, None),  # emptied: there is no new text
        (_empty, _empty, None, None, None),
    ],
    ids=["trash x text", "trash x field", "empty x text", "empty x field", "empty x empty"],
)
def test_the_trash_and_emptying_it_cross_as_the_table_says(
    two: tuple[Device, Device],
    cloud: FakeCloud,
    clock: Clock,
    one: Callable[[Device, str], None],
    other: Callable[[Device, str], None],
    text: str | None,
    color: str | None,
    trashed: bool | None,
) -> None:
    a, b = two
    note_id = shared(a, b)
    clock.tick()
    one(a, note_id)
    clock.tick()
    other(b, note_id)
    settle([a, b])
    for device in two:
        note = device.notes.get(note_id)
        if text is None:
            assert note is None
        else:
            assert note is not None
            assert (note.body, note.color, note.deleted) == (text, color, trashed)
    if text is None:
        assert cloud_notes(cloud)[note_id].body.deleted


def test_emptied_on_one_taken_out_on_the_other_stays_emptied(
    two: tuple[Device, Device], clock: Clock
) -> None:
    a, b = two
    note_id = shared(a, b)
    a.notes.delete(note_id)
    settle([a, b])
    clock.tick()
    a.notes.purge(note_id)
    clock.tick()
    b.notes.restore(note_id)  # before b saw it emptied
    settle([a, b])
    assert a.notes.get(note_id) is None and b.notes.get(note_id) is None


def test_emptied_on_both_and_by_the_year_is_one_deletion(
    two: tuple[Device, Device], cloud: FakeCloud, clock: Clock
) -> None:
    a, b = two
    note_id = shared(a, b)
    a.notes.delete(note_id)
    settle([a, b])
    a.notes.purge(note_id)
    b.notes.purge(note_id)
    a.sync()
    b.sync()
    deletion = cloud_notes(cloud)[note_id].body
    assert deletion.deleted and len(deletion.parents) == 1  # one version, made alike
    other = shared(a, b, "1년 휴지통")
    a.notes.delete(other)
    settle([a, b])
    clock.tick(YEAR + 1_000)
    a.sync()  # a year in the trash: emptied by itself, by every device alike
    b.sync()
    assert b.notes.get(other) is None
    assert len(cloud_notes(cloud)[other].body.parents) == 1


# One conflict, two devices, one copy


def test_one_conflict_seen_on_two_devices_makes_one_copy(tmp_path: Path, clock: Clock) -> None:
    """With a server that keeps no conditions b's write goes over a's unseen,
    and c, which took a's in between, merges the same two texts as a does."""
    cloud = FakeCloud(WEBDAV_IGNORING)
    a, b, c = (Device(name, tmp_path, cloud, clock) for name in "abc")
    note_id = shared(a, b, "회의 9시")
    settle([a, b, c])
    clock.tick()
    a.notes.update_body(note_id, "회의 10시", keep_earlier=True)
    clock.tick()
    b.notes.update_body(note_id, "회의 11시", keep_earlier=True)
    # b reads the first text, a reads it and puts its own, c takes a's, b puts its own.
    cloud.turns = Turns([1, 1, 0, 0, 0, 1, 1, 0])
    assert cloud.turns.run({d.name: d.round for d in (a, b, c)}) == {}
    # a and c both read b's, each merges it with a's, and each puts the same.
    cloud.turns = Turns([0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1])
    assert cloud.turns.run({d.name: d.round for d in (a, c)}) == {}
    cloud.turns = None
    settle([a, b, c])
    assert a.shown() == b.shown() == c.shown()
    texts = sorted(a.texts().values())
    assert texts == ["회의 10시", "회의 11시"]  # the note and one copy of the other
    copies = [each for each in a.shown() if each != note_id]
    assert len(copies) == 1
    joined = cloud_notes(cloud)[note_id].body
    assert copies[0] in [copy_id(KEYS, note_id, parent) for parent in joined.parents]
    made_by = {line.split()[1] for line in cloud.log if f"put notes/{copies[0]}.json: ok" in line}
    assert made_by == {"a", "c"}  # each merged it, and made the same copy
    for device in (a, b, c):
        device.close()


def test_the_same_text_reached_two_ways_is_one_version(
    two: tuple[Device, Device], cloud: FakeCloud, clock: Clock
) -> None:
    a, b = two
    note_id = shared(a, b, "장보기")
    clock.tick()
    a.notes.update_body(note_id, "장보기: 우", keep_earlier=True)
    a.sync()  # a's way goes through a version b never sees
    clock.tick()
    a.notes.update_body(note_id, "장보기: 우유", keep_earlier=True)
    b.notes.update_body(note_id, "장보기: 우유", keep_earlier=True)
    settle([a, b])
    assert len(a.shown()) == 1 and a.shown() == b.shown()
    assert len(cloud_notes(cloud)[note_id].body.parents) == 2  # joined, no copy


def test_the_same_edit_on_two_devices_is_the_same_version(
    two: tuple[Device, Device], cloud: FakeCloud, clock: Clock
) -> None:
    a, b = two
    note_id = shared(a, b, "장보기")
    first = cloud_notes(cloud)[note_id].body
    clock.tick()
    a.notes.update_body(note_id, "장보기: 우유", keep_earlier=True)
    b.notes.update_body(note_id, "장보기: 우유", keep_earlier=True)
    settle([a, b])
    assert len(a.shown()) == 1 and a.shown() == b.shown()
    assert cloud_notes(cloud)[note_id].body.parents == (first.id,)


def test_a_vault_gone_back_gets_each_devices_newer_versions_again(
    two: tuple[Device, Device], cloud: FakeCloud, clock: Clock
) -> None:
    a, b = two
    first = shared(a, b, "첫째")
    second = shared(a, b, "둘째")
    then = cloud.requests
    clock.tick()
    a.notes.update_body(first, "첫째, 고침", keep_earlier=True)
    b.notes.set_color(second, "pink")
    settle([a, b])
    cloud.go_back_to(then)  # as Dropbox's Rewind leaves it
    assert cloud_notes(cloud)[first].body.text == "첫째"
    settle([a, b])
    assert cloud_notes(cloud)[first].body.text == "첫째, 고침"
    assert cloud_notes(cloud)[second].contents.fields["color"].value == "pink"
    assert a.shown() == b.shown() and len(a.shown()) == 2  # no copies


def test_an_edit_on_a_version_whose_answer_was_lost_is_no_conflict(
    two: tuple[Device, Device], cloud: FakeCloud, clock: Clock
) -> None:
    a, b = two
    note_id = shared(a, b, "처음")
    clock.tick()
    a.notes.update_body(note_id, "a가 쓴 글", keep_earlier=True)
    cloud.trouble = lambda request: (
        Trouble.LOST if (request.device, request.op) == ("a", "put") else None
    )
    with pytest.raises(UnavailableError):
        a.sync()
    cloud.trouble = lambda _request: None
    b.sync()  # it went through: b takes it, and goes on writing on it
    clock.tick()
    b.notes.update_body(note_id, "a가 쓴 글, b가 이어 씀", keep_earlier=True)
    b.sync()
    settle([a, b])
    assert a.shown() == b.shown() and len(a.shown()) == 1
    assert a.texts()[note_id] == "a가 쓴 글, b가 이어 씀"


# Clocks


def test_a_change_made_on_what_a_clock_hours_ahead_wrote_wins_all_the_same(
    two: tuple[Device, Device], clock: Clock
) -> None:
    a, b = two
    note_id = shared(a, b)
    a.skew = 9 * HOUR  # its clock read 9 hours off before it was set right
    a.notes.set_color(note_id, "pink")
    settle([a, b])
    clock.tick(HOUR)
    b.notes.set_color(note_id, "blue")  # seen pink, made it blue: an hour on, 8 hours "earlier"
    settle([a, b])
    for device in two:
        note = device.notes.get(note_id)
        assert note is not None and note.color == "blue"


def test_changes_neither_saw_follow_the_clocks_and_still_agree(
    two: tuple[Device, Device], clock: Clock
) -> None:
    """What is left of a wrong clock: of two changes made without seeing each
    other, the one of the clock ahead stays. Nothing is lost, all agree."""
    a, b = two
    note_id = shared(a, b)
    a.skew = 9 * HOUR
    a.notes.set_color(note_id, "pink")
    clock.tick(HOUR)
    b.notes.set_color(note_id, "blue")
    settle([a, b])
    assert a.shown() == b.shown()
    note = a.notes.get(note_id)
    assert note is not None and note.color == "pink"


def test_a_clock_set_back_goes_on_after_what_it_wrote(
    two: tuple[Device, Device], clock: Clock
) -> None:
    a, b = two
    note_id = shared(a, b)
    a.skew = YEAR  # a year ahead, then set right
    a.notes.set_color(note_id, "pink")
    settle([a, b])
    a.skew = 0
    a.notes.set_color(note_id, "green")
    b.notes.set_hidden(note_id, True)  # another field: its own time
    settle([a, b])
    note = b.notes.get(note_id)
    assert note is not None and (note.color, note.hidden) == ("green", True)


def test_a_file_from_a_clock_at_the_end_of_time_is_changed_all_the_same(
    two: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = two
    note_id = shared(a, b)
    there = cloud_notes(cloud)[note_id]
    fields = dict(there.contents.fields)
    fields["color"] = replace(fields["color"], value="pink", hlc=END)
    put = NoteFile(Contents(fields, there.contents.others), there.body)
    cloud.put_by_hand(item_path(NOTES, note_id), seal(KEYS, VAULT, NOTES, note_id, put.to_json()))
    settle([a, b])
    b.notes.set_color(note_id, "blue")  # no time is after it: this one is at it too
    settle([a, b])
    assert a.shown() == b.shown()
    changed = cloud_notes(cloud)[note_id].contents.fields["color"]
    assert changed.hlc == END and changed.value == "blue"


# 0.6's notes, synced the first time


def _preview_0_6() -> ModuleType:
    """What the 0.6 preview left (tests/data, beside the other previews')."""
    path = Path(__file__).parents[1] / "data" / "preview_fixture_0_6.py"
    spec = importlib.util.spec_from_file_location("preview_fixture_0_6", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_notes_from_0_6_sync_as_they_were(tmp_path: Path, cloud: FakeCloud, clock: Clock) -> None:
    def no_store() -> CredentialStore:
        raise AssertionError("portable notes read no credential store")

    preview = _preview_0_6()
    password: str = preview.PASSWORD
    trashed: str = preview.TRASHED
    secret: str = preview.SECRET_TEXT
    folder = tmp_path / "preview-0.6.0"
    shutil.copytree(preview.FIXTURE, folder)
    unlock = Unlock(folder, no_store, portable=True)
    unlock.outcome()
    key = unlock.enter_password(password)
    old = Device("b", tmp_path, cloud, clock)
    old.db.close()
    old.db = open_store(folder / "notes.db", key)
    old.notes = NoteRepository(old.db, clock=old.stamp, millis=old.millis)
    new = Device("a", tmp_path, cloud, clock)
    settle([old, new])
    there = {note.id: note for note in old.notes.all() if not note.secret}
    assert new.shown() == {i: each for i, each in old.shown().items() if i in there}
    assert any(note.body == trashed and note.deleted for note in new.notes.all())
    assert plain_in(cloud.files(), {"the secret note": secret}) == []
    # A note changed since it was made has its fields at that time: one changed
    # now, on the other, is later.
    changed = next(i for i in there if new.stamp_of(i, "color")[0] != Hlc(0))
    new.notes.set_color(changed, "lavender")
    settle([old, new])
    note = old.notes.get(changed)
    assert note is not None and note.color == "lavender"
    old.close()
    new.close()


# What a device keeps, and what it reads


def test_a_text_taken_in_says_where_it_came_from_once_it_is_an_earlier_one(
    two: tuple[Device, Device], clock: Clock
) -> None:
    a, b = two
    note_id = shared(a, b, "a의 첫 글")
    clock.tick()
    a.notes.update_body(note_id, "a의 둘째 글", keep_earlier=True)
    settle([a, b])
    clock.tick()
    a.notes.update_body(note_id, "a의 셋째 글", keep_earlier=True)
    settle([a, b])
    earlier = VersionRepository(b.db, clock=b.stamp).listed(note_id)
    assert [(each.first_line, each.came_from) for each in earlier] == [
        ("a의 둘째 글", a.id),
        ("a의 첫 글", a.id),
    ]


@pytest.mark.xfail(
    strict=True,
    reason="a reader misses a write that leaves the file the tag it had, until the"
    " devices' own files tell it which notes changed",
)
def test_two_writes_a_webdav_server_tags_alike_are_both_taken_in(
    tmp_path: Path, clock: Clock
) -> None:
    """A WebDAV server whose tags are made of the second and the size (wsgidav)
    gives one tag to two writes of one size within a second."""
    cloud = FakeCloud(WEBDAV)
    a, b = Device("a", tmp_path, cloud, clock), Device("b", tmp_path, cloud, clock)
    note_id = shared(a, b)
    there = cloud_notes(cloud)[note_id]

    def written(color: str, at: int) -> bytes:
        fields = dict(there.contents.fields)
        fields["color"] = replace(fields["color"], value=color, hlc=Hlc(at))
        note = NoteFile(Contents(fields, there.contents.others), there.body)
        return seal(KEYS, VAULT, NOTES, note_id, note.to_json())

    path = item_path(NOTES, note_id)
    cloud.requests += -cloud.requests % 4  # what comes next falls in one second
    cloud.put_by_hand(path, written("blue", clock.now + 1))  # as another device writes them
    b.sync()  # lists it and reads it, in the same second
    cloud.put_by_hand(path, written("pink", clock.now + 2))  # the same size, the same second
    settle([a, b])
    note = b.notes.get(note_id)
    assert note is not None and note.color == "pink"
    a.close()
    b.close()


def test_one_note_changed_elsewhere_is_the_one_file_read(
    two: tuple[Device, Device], cloud: FakeCloud, clock: Clock
) -> None:
    a, b = two
    ids = [a.notes.create(f"메모 {i}").id for i in range(5)]
    settle([a, b])
    clock.tick()
    a.notes.update_body(ids[2], "하나만 고침", keep_earlier=True)
    a.sync()
    asked: list[Request] = []
    cloud.trouble = lambda request: asked.append(request) if request.device == "b" else None
    b.sync()
    assert [(r.op, r.path) for r in asked] == [("list", "notes"), ("get", f"notes/{ids[2]}.json")]


def test_nothing_written_is_in_the_vault_as_text(
    two: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = two
    note_id = shared(a, b, WHAT_THE_USER_WROTE["note"])
    b.notes.update_body(note_id, WHAT_THE_USER_WROTE["title"], keep_earlier=True)
    a.notes.create(WHAT_THE_USER_WROTE["setting value"])
    settle([a, b])
    a.notes.delete(note_id)
    a.notes.purge(note_id)
    settle([a, b])
    assert plain_in(cloud.files(), WHAT_THE_USER_WROTE) == []


def test_ended_in_the_middle_of_a_round_it_goes_on_and_loses_nothing(
    two: tuple[Device, Device], cloud: FakeCloud, clock: Clock
) -> None:
    class EndedError(Exception):
        pass

    a, b = two
    note_id = shared(a, b)
    clock.tick()
    a.notes.update_body(note_id, "끝나기 전에 쓴 글", keep_earlier=True)
    made = a.notes.create("끝나기 전에 만든 메모")

    def ended(request: Request) -> None:
        if request.op == "put":
            raise EndedError()

    cloud.trouble = ended
    with pytest.raises(EndedError):
        a.sync()
    cloud.trouble = lambda _request: None
    a.reopen()
    settle([a, b])
    assert b.texts()[note_id] == "끝나기 전에 쓴 글"
    assert b.texts()[made.id] == "끝나기 전에 만든 메모"
    assert a.shown() == b.shown()
