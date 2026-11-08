"""A big change another device made: ten notes or more here, out of the trash,
that would go to it or be deleted for good, and conflict copies that would be
made, all told in one round. The device that would take it in stops before
taking in or putting anything, and keeps what it stopped for; its user lets it
go on (after a backup), and it stops again only for ten more it was not told
of. What is not counted: notes not here, texts changed, notes taken out of the
trash or emptied from it, a year in the trash here."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, WEBDAV_IGNORING, FakeCloud, Kind
from sync_bed import VAULT, Clock, Device, settle

from stickle.data import sync_held
from stickle.data.sync_held import COPY, GONE, TRASH
from stickle.sync.engine import BIG, BigChangeError
from stickle.sync.format import DEVICES, NOTES, item_path
from stickle.sync.merge import YEAR
from stickle.sync.outcome import Stopped, after

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
    b.accepts = False  # the user here is asked
    yield cloud, a, b
    a.close()
    b.close()


def _made(a: Device, b: Device, count: int) -> list[str]:
    ids = [a.notes.create(f"메모 {i}").id for i in range(count)]
    settle([a, b])
    return ids


def _held(device: Device) -> dict[str, list[str]]:
    kinds: dict[str, list[str]] = {}
    for each in sync_held.every(device.db, VAULT):
        kinds.setdefault(each.kind, []).append(each.note_id)
    return {kind: sorted(ids) for kind, ids in kinds.items()}


def _vault(cloud: FakeCloud) -> dict[str, bytes]:
    return {p: d for p, d in cloud.files().items() if not p.startswith(f"{DEVICES}/")}


def _in_trash(device: Device, ids: list[str]) -> list[str]:
    return [i for i in ids if (n := device.notes.get(i)) is not None and n.deleted_at is not None]


def test_ten_notes_put_in_the_trash_elsewhere_stop_the_round_before_anything_goes(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    cloud, a, b = two
    ids = _made(a, b, BIG + 2)
    for note_id in ids[:BIG]:
        a.notes.delete(note_id)
    a.labels.create_category("집", "sky")
    a.sync()
    clock.tick()
    b.notes.update_body(ids[-1], "B에서 고친 글")
    vault = _vault(cloud)

    with pytest.raises(BigChangeError):
        b.sync()

    assert _vault(cloud) == vault  # nothing put: what was changed here neither
    assert _in_trash(b, ids) == []
    assert "집" not in [each.name for each in b.labels.categories()]  # the records wait too
    assert _held(b) == {TRASH: sorted(ids[:BIG])}
    assert after(BigChangeError([])).stopped is Stopped.BIG_CHANGE


def test_nine_go_through(two: tuple[FakeCloud, Device, Device]) -> None:
    _, a, b = two
    ids = _made(a, b, BIG)
    for note_id in ids[: BIG - 1]:
        a.notes.delete(note_id)
    a.sync()
    b.sync()
    assert _in_trash(b, ids) == ids[: BIG - 1]
    assert _held(b) == {}


def test_the_trash_deleted_for_good_and_conflict_copies_count_together(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    _, a, b = two
    ids = _made(a, b, BIG)
    for note_id in ids[:4]:
        a.notes.delete(note_id)
    for note_id in ids[4:7]:  # out of the trash on b: gone from its desktop for good
        a.notes.delete(note_id)
        a.notes.purge(note_id)
    for note_id in ids[7:]:
        a.notes.update_body(note_id, "A에서 고친 글")
        b.notes.update_body(note_id, "B에서 고친 글")
    a.sync()
    clock.tick()
    with pytest.raises(BigChangeError) as stopped:
        b.sync()
    held = _held(b)
    assert held[TRASH] == sorted(ids[:4])
    assert held[GONE] == sorted(ids[4:7])
    assert len(held[COPY]) == 3
    copies = [each for each in stopped.value.held if each.kind == COPY]
    assert sorted(each.copy_of or "" for each in copies) == sorted(ids[7:])
    assert all(b.notes.get(each.note_id) is None for each in copies)


def test_let_go_on_it_backs_up_first_and_takes_it_all_in(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    _, a, b = two
    ids = _made(a, b, BIG + 1)
    for note_id in ids[:BIG]:
        a.notes.delete(note_id)
    a.sync()
    b.notes.update_body(ids[-1], "B에서 고친 글")
    with pytest.raises(BigChangeError):
        b.sync()
    assert b.backups == 0

    b.sync(accept=b.back_up)

    assert b.backups == 1
    assert _in_trash(b, ids) == ids[:BIG]
    assert _held(b) == {}
    a.sync()
    there = a.notes.get(ids[-1])
    assert there is not None and there.body == "B에서 고친 글"  # held, then put
    b.sync()  # nothing held: a round as any other
    assert b.backups == 1


def test_ten_more_while_it_waited_stop_it_again_with_them_all(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    _, a, b = two
    ids = _made(a, b, 2 * BIG)
    for note_id in ids[:BIG]:
        a.notes.delete(note_id)
    a.sync()
    with pytest.raises(BigChangeError):
        b.sync()
    for note_id in ids[BIG:]:
        a.notes.delete(note_id)
    a.sync()

    with pytest.raises(BigChangeError):
        b.sync(accept=b.back_up)

    assert b.backups == 0  # stopped before anything was taken in
    assert _held(b) == {TRASH: sorted(ids)}
    assert _in_trash(b, ids) == []


def test_a_few_more_than_it_was_told_of_go_in_with_the_rest(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    _, a, b = two
    ids = _made(a, b, BIG + 3)
    for note_id in ids[:BIG]:
        a.notes.delete(note_id)
    a.sync()
    with pytest.raises(BigChangeError):
        b.sync()
    for note_id in ids[BIG:]:
        a.notes.delete(note_id)
    a.sync()
    b.sync(accept=b.back_up)
    assert _in_trash(b, ids) == ids


def test_what_is_not_counted(two: tuple[FakeCloud, Device, Device], clock: Clock) -> None:
    _, a, b = two
    many = BIG + 2
    edited = _made(a, b, many)
    taken_out = _made(a, b, many)
    emptied = _made(a, b, many)
    for note_id in taken_out + emptied:
        a.notes.delete(note_id)
    b.accepts = True  # what it holds for so far: taken in
    settle([a, b])
    b.accepts, b.holds = False, 0
    for note_id in edited:
        a.notes.update_body(note_id, "고친 글")
    for note_id in taken_out:
        a.notes.restore(note_id)
    for note_id in emptied:  # in the trash on b too
        a.notes.purge(note_id)
    new = [a.notes.create(f"새 메모 {i}").id for i in range(many)]
    in_the_trash = [a.notes.create(f"휴지통 메모 {i}").id for i in range(many)]
    for note_id in in_the_trash:  # never here out of it
        a.notes.delete(note_id)
    a.sync()
    clock.tick()

    b.sync()

    assert b.holds == 0
    assert all((n := b.notes.get(i)) is not None and n.body == "고친 글" for i in edited)
    assert _in_trash(b, taken_out) == []
    assert all(b.notes.get(i) is None for i in emptied)
    assert all(b.notes.get(i) is not None for i in new)
    assert _in_trash(b, in_the_trash) == in_the_trash


def test_a_year_in_the_trash_here_is_not_counted(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    _, a, b = two
    ids = _made(a, b, BIG + 2)
    for note_id in ids:
        b.notes.delete(note_id)
    settle([a, b])
    clock.tick(YEAR + DAY)
    b.sync()
    assert b.holds == 0
    assert all(b.notes.get(i) is None for i in ids)


def test_a_device_away_for_more_than_a_year_stops_for_the_notes_the_vault_lost(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    cloud, a, b = two
    ids = _made(a, b, BIG + 2)
    clock.tick(YEAR + DAY)  # b away all that time
    for note_id in ids:  # emptied elsewhere, the deletions cleared a year after
        cloud.remove_by_hand(item_path(NOTES, note_id))
    with pytest.raises(BigChangeError):
        b.sync()
    assert _held(b) == {TRASH: sorted(ids)}
    assert _in_trash(b, ids) == []
    report = b.sync(accept=b.back_up)
    assert sorted(report.trashed) == sorted(ids)
    assert _in_trash(b, ids) == ids


def test_what_it_stopped_for_is_kept_and_looked_at_again_without_fetching_it_again(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    cloud, a, b = two
    ids = _made(a, b, BIG)
    for note_id in ids:
        a.notes.delete(note_id)
    a.sync()
    with pytest.raises(BigChangeError):
        b.sync()
    gets = sum(1 for line in cloud.log if line.split()[1:3] == ["b", "get"])
    b.reopen()  # the app started again
    assert _held(b) == {TRASH: sorted(ids)}

    with pytest.raises(BigChangeError):
        b.sync()  # looked at again when the app starts

    again = sum(1 for line in cloud.log if line.split()[1:3] == ["b", "get"])
    assert again - gets == 1  # vault.json only: the notes kept since
    for note_id in ids:  # taken out of the trash there since
        a.notes.restore(note_id)
    a.sync()
    b.sync()
    assert _held(b) == {}
    assert _in_trash(b, ids) == []
