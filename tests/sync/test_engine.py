"""A round of sync, between two devices and a cloud in memory: what one
changes reaches the other, what both change is merged as both merge it, and
nothing written is lost when a request fails, an answer is lost, or a cloud
does not keep conditions."""

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fake_cloud import (
    DROPBOX,
    ONEDRIVE,
    WEBDAV,
    WEBDAV_IGNORING,
    WEBDAV_NO_TAGS,
    FakeCloud,
    Kind,
    Request,
    Trouble,
    Turns,
)

from stickle.core.clock import stamp_of
from stickle.crypto.vault_keys import DataKeys
from stickle.data import stamps, sync_state
from stickle.data.labels import LabelRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.versions import VersionRepository
from stickle.sync import engine, local
from stickle.sync.contents import Contents, Field
from stickle.sync.engine import Place, Report, sync_notes
from stickle.sync.format import (
    NOTES,
    NewerFormatError,
    UnknownKeyError,
    item_path,
    open_envelope,
    seal,
)
from stickle.sync.merge import COPY_OF, YEAR, copy_id
from stickle.sync.note_file import NoteFile, new_version
from stickle.sync.targets.base import RefusedError, UnavailableError

KEY = secrets.token_bytes(32)
KEYS = DataKeys({1: secrets.token_bytes(32)})
VAULT = "vault-1"
START = 1_762_483_200_000  # 2025-11-07, by every clock here unless a test moves one


class Times:
    """The time, set by the test: the cloud's, and the devices' as they go by it."""

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
        self.name = name
        self.id = f"{name}-0000-4000-8000-000000000000"
        self.db = open_store(folder / f"{name}.db", KEY)
        self.times = times
        self.notes = NoteRepository(self.db, clock=times.stamp, millis=times)
        self.place = Place(cloud.target(name), VAULT, KEYS, self.id)

    def sync(self) -> Report:
        return sync_notes(self.db, self.place, self.times, self.notes)

    def round(self) -> None:
        """A round, on a thread of its own (Turns)."""
        self.sync()

    def state(self, note_id: str) -> sync_state.NoteState:
        found = sync_state.get(self.db, VAULT, note_id)
        assert found is not None
        return found

    def texts(self) -> dict[str, str]:
        return {note.id: note.body for note in self.notes.all()}


@pytest.fixture
def times() -> Times:
    return Times()


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud(DROPBOX)


@pytest.fixture
def devices(tmp_path: Path, cloud: FakeCloud, times: Times) -> Iterator[tuple[Device, Device]]:
    pair = (Device("a", tmp_path, cloud, times), Device("b", tmp_path, cloud, times))
    yield pair
    for device in pair:
        device.db.close()


def in_cloud(cloud: FakeCloud, note_id: str) -> NoteFile:
    data = cloud.files()[item_path(NOTES, note_id)]
    _, contents = open_envelope(KEYS, data, VAULT, NOTES)
    return NoteFile.from_json(KEYS, contents)


def put_by_hand(cloud: FakeCloud, note_id: str, note: NoteFile) -> None:
    cloud.put_by_hand(item_path(NOTES, note_id), seal(KEYS, VAULT, NOTES, note_id, note.to_json()))


def note_files(cloud: FakeCloud) -> list[str]:
    return [path for path in cloud.files() if path.startswith("notes/")]


def test_a_note_made_on_one_device_reaches_the_other(
    devices: tuple[Device, Device], cloud: FakeCloud, times: Times
) -> None:
    a, b = devices
    note = a.notes.create("장보기: 우유")
    times.tick()
    a.notes.set_color(note.id, "blue")
    times.tick()
    a.sync()
    times.tick()
    report = b.sync()
    there = b.notes.get(note.id)
    assert there is not None and there.body == "장보기: 우유" and there.color == "blue"
    assert report.changed == [note.id]
    # The colour as changed there, by that device, and when it reached the cloud.
    color = stamps.of(b.db, note.id)["color"]
    assert color[1] == a.id and color[0].millis == START + 1_000
    assert stamps.sent_of(b.db, note.id)["color"] == START + 2_000
    assert in_cloud(cloud, note.id).body.text == "장보기: 우유"
    assert b.sync().changed == []  # nothing new: nothing taken in again


def test_an_edit_goes_across_and_the_earlier_text_stays(
    devices: tuple[Device, Device], times: Times
) -> None:
    a, b = devices
    note = a.notes.create("첫 글")
    a.sync()
    b.sync()
    times.tick()
    b.notes.update_body(note.id, "고친 글", keep_earlier=True)
    times.tick()
    b.sync()
    a.sync()
    assert a.texts() == {note.id: "고친 글"}
    earlier = VersionRepository(a.db, clock=times.stamp).listed(note.id)
    assert [each.first_line for each in earlier] == ["첫 글"]
    assert earlier[0].came_from is None  # written here, before it went across
    rows = a.db.execute("SELECT text_from FROM notes WHERE id = ?", (note.id,)).fetchall()
    assert rows[0][0] == b.id


def test_a_version_is_when_its_text_was_saved_not_when_it_was_synced(
    devices: tuple[Device, Device], cloud: FakeCloud, times: Times
) -> None:
    a, _ = devices
    note = a.notes.create("오프라인에서 쓴 글")
    saved = times.now
    times.tick(8 * 60 * 60 * 1000)  # back online hours later
    a.sync()
    assert in_cloud(cloud, note.id).body.edited == saved


def test_text_changed_on_both_keeps_both(
    devices: tuple[Device, Device], cloud: FakeCloud, times: Times
) -> None:
    a, b = devices
    note = a.notes.create("회의 9시")
    a.sync()
    b.sync()
    times.tick()
    a.notes.update_body(note.id, "회의 10시", keep_earlier=True)
    times.tick()
    b.notes.update_body(note.id, "회의 11시", keep_earlier=True)  # the later one
    a.sync()
    b.sync()  # b merges: its text, edited later, stays the note's
    a.sync()
    original = in_cloud(cloud, note.id)
    assert original.body.text == "회의 11시" and len(original.body.parents) == 2
    copies = [each for each in a.texts() if each != note.id]
    assert copies in ([copy_id(KEYS, note.id, parent)] for parent in original.body.parents)
    assert a.texts() == b.texts() == {note.id: "회의 11시", copies[0]: "회의 10시"}
    assert len(note_files(cloud)) == 2  # one copy, made once
    assert in_cloud(cloud, copies[0]).contents.fields[COPY_OF].value == {
        "note": note.id,
        "device": a.id,
        "edited": START + 1_000,
    }


def test_each_field_keeps_its_later_change(devices: tuple[Device, Device], times: Times) -> None:
    a, b = devices
    note = a.notes.create("색과 투명도")
    a.sync()
    b.sync()
    times.tick()
    a.notes.set_color(note.id, "blue")
    a.notes.set_locked(note.id, True)
    times.tick()
    b.notes.set_opacity(note.id, 0.5)
    b.notes.set_color(note.id, "pink")  # later than a's blue
    a.sync()
    b.sync()
    a.sync()
    for device in devices:
        there = device.notes.get(note.id)
        assert there is not None
        assert (there.color, there.opacity, there.locked) == ("pink", 0.5, True)
    assert len(a.texts()) == 1  # fields alone make no copy


def test_the_trash_and_emptying_it_go_across(devices: tuple[Device, Device], times: Times) -> None:
    a, b = devices
    note = a.notes.create("버릴 메모")
    a.sync()
    b.sync()
    times.tick()
    a.notes.delete(note.id)
    a.sync()
    b.sync()
    there = b.notes.get(note.id)
    assert there is not None and there.deleted
    times.tick()
    a.notes.purge(note.id)
    a.sync()
    b.sync()
    assert b.notes.get(note.id) is None
    assert [row[0] for row in b.notes.deletion_records()] == [note.id]
    assert b.texts() == {}


def test_a_text_changed_where_the_note_was_emptied_comes_back_in_the_trash(
    devices: tuple[Device, Device], cloud: FakeCloud, times: Times
) -> None:
    a, b = devices
    note = a.notes.create("장보기")
    a.sync()
    b.sync()
    times.tick()
    a.notes.delete(note.id)
    a.notes.purge(note.id)
    times.tick()
    b.notes.update_body(note.id, "장보기: 우유, 빵", keep_earlier=True)
    a.sync()
    b.sync()
    a.sync()
    for device in devices:
        there = device.notes.get(note.id)
        assert there is not None and there.deleted and there.body == "장보기: 우유, 빵"
    assert in_cloud(cloud, note.id).body.text == "장보기: 우유, 빵"


def test_an_answer_lost_is_not_taken_for_a_conflict(
    devices: tuple[Device, Device], cloud: FakeCloud, times: Times
) -> None:
    a, b = devices
    note = a.notes.create("처음")
    a.sync()
    b.sync()
    times.tick()
    a.notes.update_body(note.id, "답을 잃은 글", keep_earlier=True)
    cloud.trouble = lambda request: (
        Trouble.LOST if (request.device, request.op) == ("a", "put") else None
    )
    with pytest.raises(UnavailableError):
        a.sync()
    cloud.trouble = lambda _request: None
    assert a.state(note.id).outbox is not None
    times.tick()
    a.notes.update_body(note.id, "답을 잃은 글, 이어서", keep_earlier=True)
    a.sync()
    b.sync()
    assert a.texts() == b.texts() == {note.id: "답을 잃은 글, 이어서"}
    body = in_cloud(cloud, note.id).body
    assert len(body.parents) == 1 and body.generation == 3  # made on the one lost, no join


def test_a_new_note_whose_answer_was_lost_is_not_made_twice(
    devices: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = devices
    note = a.notes.create("새 메모")
    cloud.trouble = lambda request: Trouble.LOST if request.op == "put" else None
    with pytest.raises(UnavailableError):
        a.sync()
    cloud.trouble = lambda _request: None
    a.sync()
    assert a.state(note.id).outbox is None
    b.sync()
    assert b.texts() == {note.id: "새 메모"}
    assert len(note_files(cloud)) == 1


@pytest.mark.parametrize("kind", [DROPBOX, ONEDRIVE, WEBDAV], ids=lambda kind: kind.name)
def test_a_write_refused_is_read_again_and_merged(tmp_path: Path, kind: Kind) -> None:
    times = Times()
    cloud = FakeCloud(kind)
    a, b = Device("a", tmp_path, cloud, times), Device("b", tmp_path, cloud, times)
    note = a.notes.create("함께 쓰는 메모")
    a.sync()
    b.sync()
    times.tick()
    a.notes.set_color(note.id, "blue")
    b.notes.set_opacity(note.id, 0.5)
    # b lists, a lists and writes, then b writes on what it listed: refused.
    cloud.turns = Turns([1, 0, 0, 0])
    failed = cloud.turns.run({"a": a.round, "b": b.round})
    cloud.turns = None
    assert failed == {}
    assert any("b put" in line and "RefusedError" in line for line in cloud.log)
    a.sync()
    for device in (a, b):
        there = device.notes.get(note.id)
        assert there is not None and (there.color, there.opacity) == ("blue", 0.5)


@pytest.mark.parametrize("kind", [WEBDAV_IGNORING, WEBDAV_NO_TAGS], ids=lambda kind: kind.name)
def test_a_cloud_that_keeps_no_conditions_loses_nothing(tmp_path: Path, kind: Kind) -> None:
    times = Times()
    cloud = FakeCloud(kind)
    a, b = Device("a", tmp_path, cloud, times), Device("b", tmp_path, cloud, times)
    note = a.notes.create("조건 없는 서버")
    a.sync()
    b.sync()
    times.tick()
    a.notes.update_body(note.id, "a가 고친 글", keep_earlier=True)
    times.tick()
    b.notes.set_color(note.id, "green")
    # b reads, a writes, b writes over it without having seen it.
    cloud.turns = Turns([1, 0, 1, 0, 0, 1])
    failed = cloud.turns.run({"a": a.round, "b": b.round})
    cloud.turns = None
    assert failed == {}
    for _ in range(2):
        a.sync()
        b.sync()
    for device in (a, b):
        there = device.notes.get(note.id)
        assert there is not None and (there.body, there.color) == ("a가 고친 글", "green")


def test_a_file_not_whole_yet_is_left_for_a_later_round(
    devices: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = devices
    note = a.notes.create("반쯤 온 파일")
    a.sync()
    cloud.trouble = lambda request: Trouble.HALF if request.op == "get" else None
    assert b.sync().left == [note.id]
    assert b.texts() == {}
    cloud.trouble = lambda _request: None
    b.sync()
    assert b.texts() == {note.id: "반쯤 온 파일"}


def test_secret_notes_are_not_synced_yet_and_keep_no_text_here(
    devices: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = devices
    note = a.notes.create("비밀이 될 글")
    a.sync()
    a.notes.make_secret(note.id, "제목", b"sealed")
    a.sync()
    b.sync()
    assert in_cloud(cloud, note.id).body.text == "비밀이 될 글"  # as it was: not put again
    kept = a.state(note.id)
    assert kept is not None and kept.seen is not None and "비밀이 될 글" not in kept.seen
    hidden = a.notes.create("처음부터 비밀")
    a.notes.make_secret(hidden.id, "제목", b"sealed")
    a.sync()
    assert item_path(NOTES, hidden.id) not in cloud.files()


def test_an_emptied_note_keeps_no_text_here(devices: tuple[Device, Device]) -> None:
    a, _ = devices
    note = a.notes.create("비울 글")
    a.sync()
    a.notes.delete(note.id)
    a.notes.purge(note.id)
    kept = a.state(note.id)
    assert kept is not None and kept.seen is not None and "비울 글" not in kept.seen
    a.sync()  # the deletion is made on what is left
    assert a.state(note.id).deleted


def test_an_emptied_note_whose_deletion_did_not_go_up_goes_at_the_next_round(
    devices: tuple[Device, Device], cloud: FakeCloud
) -> None:
    """Found by the property test: what was kept of the note, its text taken
    out, was written back as a text that is none, and read no more."""
    a, b = devices
    note = a.notes.create("비우고 못 올린 글")
    a.sync()
    b.sync()
    a.notes.delete(note.id)
    a.notes.purge(note.id)
    cloud.trouble = lambda request: Trouble.UNAVAILABLE if request.op == "put" else None
    with pytest.raises(UnavailableError):
        a.sync()
    cloud.trouble = lambda _request: None
    kept = a.state(note.id)
    assert kept.seen is not None and "비우고 못 올린 글" not in kept.seen
    a.sync()
    b.sync()
    assert in_cloud(cloud, note.id).body.deleted
    assert b.notes.get(note.id) is None


def test_what_this_version_does_not_know_is_kept(
    devices: tuple[Device, Device], cloud: FakeCloud, times: Times
) -> None:
    a, b = devices
    category = LabelRepository(a.db).create_category("일", "blue")
    note = a.notes.create("새 판이 쓴 칸")
    a.notes.set_category(note.id, category.id)
    a.sync()
    there = in_cloud(cloud, note.id)
    shape = Field("round", there.contents.fields["color"].hlc, a.id, sent=START)
    put_by_hand(
        cloud,
        note.id,
        NoteFile(
            type(there.contents)(
                {**there.contents.fields, "shape": shape}, {**there.contents.others, "extra": 1}
            ),
            there.body,
        ),
    )
    b.sync()
    taken = b.notes.get(note.id)
    assert taken is not None and taken.label is None  # b has no such category: not taken
    times.tick()
    b.notes.set_color(note.id, "blue")
    b.sync()
    after = in_cloud(cloud, note.id)
    assert after.contents.fields["shape"].value == "round"
    assert after.contents.others["extra"] == 1
    assert after.contents.fields["category"].value == category.id
    assert after.contents.fields["color"].value == "blue"


@pytest.mark.parametrize(
    ("written", "stopped"),
    [(b'"format":2', NewerFormatError), (b'"key":2', UnknownKeyError)],
    ids=["a newer format", "a key this device has not"],
)
def test_a_note_a_newer_version_wrote_stops_the_round(
    devices: tuple[Device, Device],
    cloud: FakeCloud,
    written: bytes,
    stopped: type[Exception],
) -> None:
    """Until the app is updated: not taken for a damaged file, nor written over."""
    a, b = devices
    note = a.notes.create("새 판이 쓴 메모")
    a.sync()
    path = item_path(NOTES, note.id)
    newer = cloud.files()[path].replace(written[:-1] + b"1", written)
    cloud.put_by_hand(path, newer)
    with pytest.raises(stopped):
        b.sync()
    a.notes.set_color(note.id, "blue")
    with pytest.raises(stopped):
        a.sync()
    assert cloud.files()[path] == newer
    assert b.texts() == {}


def test_a_note_another_version_may_not_change_is_left_alone(
    devices: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = devices
    note = a.notes.create("읽기 전용")
    a.sync()
    there = in_cloud(cloud, note.id)
    locked = Field({"kind": "newer"}, there.contents.fields["color"].hlc, a.id, readonly=True)
    put_by_hand(
        cloud,
        note.id,
        NoteFile(type(there.contents)({**there.contents.fields, "lock": locked}), there.body),
    )
    assert b.sync().left == [note.id]
    assert b.texts() == {}


def test_a_change_made_while_merging_is_merged_too(
    devices: tuple[Device, Device], cloud: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = devices
    note = a.notes.create("처음 글")
    built = local.mine
    calls: list[int] = []

    def mine_then_edited(
        keys: DataKeys, device: str, at: local.Here, state: sync_state.NoteState | None
    ) -> NoteFile | None:
        made = built(keys, device, at, state)
        if not calls:
            a.notes.update_body(note.id, "그사이 고친 글")  # as the user, while sync merges
        calls.append(1)
        return made

    monkeypatch.setattr(local, "mine", mine_then_edited)
    a.sync()
    assert in_cloud(cloud, note.id).body.text == "그사이 고친 글"
    assert a.sync().put == []


def test_the_trash_empties_a_year_after_it_reached_the_cloud(
    devices: tuple[Device, Device], cloud: FakeCloud, times: Times
) -> None:
    a, b = devices
    note = a.notes.create("1년 휴지통")
    a.notes.delete(note.id)
    times.tick(30 * 24 * 60 * 60 * 1000)  # in the trash a month before syncing
    a.sync()
    b.sync()
    times.tick(YEAR - 1_000)
    a.sync()
    assert a.notes.get(note.id) is not None  # a year since the trash, not since it reached it
    times.tick(1_000)
    a.sync()
    assert a.notes.get(note.id) is None
    assert in_cloud(cloud, note.id).body.deleted
    b.sync()
    assert b.notes.get(note.id) is None


def test_a_deletion_leaves_the_vault_a_year_after_it_reached_it(
    devices: tuple[Device, Device], cloud: FakeCloud, times: Times
) -> None:
    a, b = devices
    note = a.notes.create("지울 글")
    a.sync()
    a.notes.delete(note.id)
    a.notes.purge(note.id)
    a.sync()
    b.sync()
    times.tick(YEAR)
    b.sync()
    assert note_files(cloud) == []
    assert sync_state.get(b.db, VAULT, note.id) is None and b.notes.deletion_records() == []
    a.sync()
    assert sync_state.get(a.db, VAULT, note.id) is None and a.notes.deletion_records() == []


def test_a_note_emptied_here_before_it_was_seen_comes_back_in_the_trash(
    devices: tuple[Device, Device], times: Times
) -> None:
    a, b = devices
    note = a.notes.create("같은 ID", note_id="5f0c1d6e-1111-4222-8333-444455556666")
    b.notes.create("같은 ID", note_id=note.id)
    b.notes.delete(note.id)
    b.notes.purge(note.id)  # before b ever synced it
    times.tick()
    a.sync()
    b.sync()
    there = b.notes.get(note.id)
    assert there is not None and there.deleted and there.body == "같은 ID"


@pytest.mark.parametrize(
    ("kind", "reads"), [(DROPBOX, 0), (WEBDAV, 1)], ids=["unique tags", "tags of the second"]
)
def test_what_was_put_is_read_back_only_where_tags_do_not_tell(
    devices: tuple[Device, Device], tmp_path: Path, kind: Kind, reads: int
) -> None:
    times = Times()
    cloud = FakeCloud(kind)
    a = Device("c", tmp_path, cloud, times)
    a.notes.create("올린 글")
    a.sync()
    requests: list[Request] = []
    cloud.trouble = lambda request: requests.append(request)
    a.sync()
    a.sync()
    assert [request.op for request in requests].count("get") == reads


def test_a_merge_is_put_as_the_other_device_merges_it(
    devices: tuple[Device, Device], cloud: FakeCloud, times: Times
) -> None:
    a, b = devices
    note = a.notes.create("같은 결과")
    a.sync()
    b.sync()
    times.tick()
    a.notes.update_body(note.id, "a의 글", keep_earlier=True)
    times.tick()
    b.notes.update_body(note.id, "b의 글", keep_earlier=True)
    b.sync()
    a.sync()  # a merges: the copy is put by a
    b.sync()
    first = cloud.files()
    a.sync()
    b.sync()
    assert cloud.files() == first  # nothing more to put: both made the same


def test_no_text_of_a_note_made_by_hand_in_the_cloud_is_taken_for_another(
    devices: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = devices
    note = a.notes.create("제자리")
    a.sync()
    other = a.notes.create("다른 메모")
    a.sync()
    data = cloud.files()[item_path(NOTES, other.id)]
    cloud.put_by_hand(item_path(NOTES, note.id), data)  # another note's file in its place
    report = b.sync()
    assert report.left == [note.id]
    assert b.texts() == {other.id: "다른 메모"}


def test_a_note_made_elsewhere_with_no_fields_is_made_here(
    devices: tuple[Device, Device], cloud: FakeCloud
) -> None:
    _, b = devices
    note_id = "0a1b2c3d-1111-4222-8333-444455556666"
    first = new_version(KEYS, [], "다른 앱이 쓴 메모", START, "web-0000")
    put_by_hand(cloud, note_id, NoteFile(Contents(), first))
    b.sync()
    there = b.notes.get(note_id)
    assert there is not None and there.body == "다른 앱이 쓴 메모" and not there.deleted


def test_a_note_refused_again_and_again_is_left_for_a_later_round(
    devices: tuple[Device, Device], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = devices
    note = a.notes.create("계속 거절")
    a.sync()
    a.notes.set_color(note.id, "blue")
    puts: list[str] = []

    def refusing(path: str, *_rest: object) -> str | None:
        puts.append(path)
        raise RefusedError(path)

    monkeypatch.setattr(a.place.target, "put", refusing)
    assert a.sync().left == [note.id]
    assert len(puts) == engine.TRIES
