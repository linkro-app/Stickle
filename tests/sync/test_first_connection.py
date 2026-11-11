"""A device joining a vault that was there before it, with notes of its own.
What merging would make is told first, reading the vault's notes once.
Merging lets the device's untouched welcome note go, and a what's-new note
the vault does not have; folds a note with the same text as one of the
vault's into that one (both out of the trash) as one note changed on two
devices; and stops for no big change. Keeping only the vault's notes backs up
first and removes the device's without a trace. A first round cut short is
carried on as it began."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, WEBDAV_IGNORING, FakeCloud, Kind, Request, Trouble
from sync_bed import VAULT, Clock, Device, settle

from stickle.core import made_notes
from stickle.data import sync_state
from stickle.data.versions import VersionRepository
from stickle.sync.engine import GETTING, cloud_only, first_look, joined
from stickle.sync.format import NOTES, item_at, item_path
from stickle.sync.targets.base import UnavailableError

WELCOME = "# Stickle에 오신 것을 환영합니다"
NEWS_ID = made_notes.news_id("0.6")
NEWS_EN, NEWS_KO = "# What's new in Stickle 0.6", "# Stickle 0.6에서 바뀐 점"


@pytest.fixture(autouse=True)
def made(monkeypatch: pytest.MonkeyPatch) -> None:
    """The texts taken for the app's own (stickle.core.made_notes)."""

    def welcome(text: str) -> bool:
        return text == WELCOME

    def news(note_id: str, text: str) -> bool:
        return note_id == NEWS_ID and text in (NEWS_EN, NEWS_KO)

    monkeypatch.setattr(made_notes, "welcome", welcome)
    monkeypatch.setattr(made_notes, "news", news)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(params=[DROPBOX, WEBDAV_IGNORING], ids=lambda k: k.name)
def kind(request: pytest.FixtureRequest) -> Kind:
    return request.param


@pytest.fixture
def two(tmp_path: Path, kind: Kind, clock: Clock) -> Iterator[tuple[FakeCloud, Device, Device]]:
    """a made the vault; b joins it."""
    cloud = FakeCloud(kind)
    a = Device("a", tmp_path, cloud, clock, whole=True)
    b = Device("b", tmp_path, cloud, clock, whole=True)
    yield cloud, a, b
    a.close()
    b.close()


def _in_vault(cloud: FakeCloud) -> set[str]:
    found = [item_at(path) for path in cloud.files()]
    return {at[1] for at in found if at is not None and at[0] == NOTES}


def _texts(device: Device) -> list[str]:
    return sorted(note.body for note in device.notes.all())


def _gets(cloud: FakeCloud, device: str, ids: list[str]) -> int:
    """How many times device fetched these notes' files."""
    paths = {f"{item_path(NOTES, note_id)}:" for note_id in ids}
    return sum(
        1 for line in cloud.log if line.split()[1:3] == [device, "get"] and line.split()[3] in paths
    )


def test_the_device_that_makes_the_vault_puts_its_welcome_note_too(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    cloud, a, _ = two
    welcome = a.notes.create(WELCOME).id
    a.sync()
    assert welcome in _in_vault(cloud)


def test_joining_with_the_welcome_note_only_lets_it_go_and_takes_the_vaults(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    cloud, a, b = two
    a.notes.create(WELCOME)
    a.notes.create("A의 메모")
    a.sync()
    mine = b.notes.create(WELCOME).id

    joined(b.db, VAULT)
    b.sync()

    assert b.notes.get(mine) is None and mine not in _in_vault(cloud)
    assert b.notes.deletion_records() == []
    assert _texts(b) == sorted([WELCOME, "A의 메모"])
    assert not sync_state.joining(b.db, VAULT)  # the round through: as any other since


def test_a_welcome_note_changed_is_put_as_any_other(two: tuple[FakeCloud, Device, Device]) -> None:
    cloud, a, b = two
    a.sync()
    ticked = b.notes.create(WELCOME + "\n- [x] 해 봄").id
    joined(b.db, VAULT)
    b.sync()
    assert ticked in _in_vault(cloud)


def test_the_first_look_tells_what_merging_would_make_and_takes_nothing_in(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    _, a, b = two
    for text in ["같은 글 하나", "같은 글 둘", "A만 1", "A만 2", "A만 3", "A만 4"]:
        a.notes.create(text)
        clock.tick()
    a.notes.delete(a.notes.create("A의 휴지통").id)
    a.sync()
    b.notes.create(WELCOME)
    for text in ["같은 글 하나", "같은 글 둘", "B만 1", "B만 2", "B만 3"]:
        b.notes.create(text)
    before = _texts(b)

    looked = first_look(b.db, b.place, b.clock, b.notes)

    assert (looked.here, looked.cloud, looked.alike) == (5, 6, 2)
    assert looked.recent == ["A만 4", "A만 3", "A만 2"]
    assert _texts(b) == before


def test_the_first_look_tells_how_far_it_has_got_and_writes_nothing_there(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    cloud, a, b = two
    for text in ("하나", "둘", "셋"):
        a.notes.create(text)
    a.sync()
    b.notes.create("B")
    before = cloud.files()
    told: list[tuple[str, int, int]] = []

    def moving(doing: str, done: int, of: int) -> None:
        told.append((doing, done, of))

    first_look(b.db, b.place, b.clock, b.notes, moving)

    assert told == [(GETTING, 1, 3), (GETTING, 2, 3), (GETTING, 3, 3)]
    assert cloud.files() == before
    assert not sync_state.joining(b.db, VAULT)


def test_the_first_look_counts_a_note_the_vault_has_by_its_id_alike_when_its_text_is(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    _, a, b = two
    same = a.notes.create("같은 메모").id
    apart = a.notes.create("A의 글").id
    a.sync()
    b.notes.create("같은 메모", note_id=same)  # kept from a vault made again, say
    b.notes.create("B의 글", note_id=apart)  # a conflict copy to come: not alike

    looked = first_look(b.db, b.place, b.clock, b.notes)

    assert (looked.here, looked.cloud, looked.alike) == (2, 2, 1)


def test_merging_folds_a_note_with_the_same_text_into_the_vaults(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    cloud, a, b = two
    there = a.notes.create("장보기: 우유").id
    a.sync()
    clock.tick()
    own = b.notes.create("장보기: 우유").id
    b.notes.update_body(own, "장보기: 우유, 빵", keep_earlier=True)
    clock.tick()
    b.notes.update_body(own, "장보기: 우유", keep_earlier=True)
    b.notes.set_color(own, "lavender")  # changed here: later than the vault's
    b.db.execute(
        "INSERT INTO note_layouts (note_id, config_key, monitor_key, rel_x, rel_y, updated_at)"
        " VALUES (?, 'main', '[]', 0.1, 0.2, ?)",
        (own, b.stamp()),
    )
    b.db.execute(
        "INSERT INTO note_sizes (note_id, width, height, updated_at) VALUES (?, 300, 200, ?)",
        (own, b.stamp()),
    )

    joined(b.db, VAULT)
    settle([a, b])

    assert b.notes.get(own) is None and own not in _in_vault(cloud)
    assert b.notes.deletion_records() == []
    for device in (a, b):
        notes = device.notes.all()
        assert [(n.id, n.body, n.color) for n in notes] == [(there, "장보기: 우유", "lavender")]
    for table in ("note_layouts", "note_sizes"):  # where it was on this computer
        moved = b.db.execute(f"SELECT count(*) FROM {table} WHERE note_id = ?", (there,))
        assert moved.fetchall()[0][0] == 1, table
    earlier = [each.first_line for each in VersionRepository(b.db, b.stamp).listed(there)]
    assert "장보기: 우유, 빵" in earlier


def test_a_note_folded_in_is_not_put_on_its_own_whichever_id_comes_first(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    cloud, a, b = two
    there = a.notes.create("같은 글", note_id="ffffffff-0000-4000-8000-000000000000").id
    a.sync()
    b.notes.create("같은 글", note_id="00000000-0000-4000-8000-000000000000")
    joined(b.db, VAULT)
    b.sync()
    assert _in_vault(cloud) == {there}
    assert [note.id for note in b.notes.all()] == [there]


def test_a_field_never_changed_here_stays_as_the_vault_has_it_whichever_device_id_is_higher(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    _, a, b = two  # b's id is the higher: a tie of the earliest times would go its way
    there = a.notes.create("같은 글", "pink").id  # its colour as made, never changed
    a.sync()
    b.notes.create("같은 글")  # in the colour new notes take here
    joined(b.db, VAULT)
    settle([a, b])
    for device in (a, b):
        notes = device.notes.all()
        assert [(n.id, n.color) for n in notes] == [(there, "pink")]


def test_two_alike_here_and_one_there_make_one_pair(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    _, a, b = two
    a.notes.create("할 일")
    a.sync()
    b.notes.create("할 일")
    b.notes.create("할 일")
    joined(b.db, VAULT)
    settle([a, b])
    assert _texts(a) == _texts(b) == ["할 일", "할 일"]


def test_a_note_in_the_trash_is_not_folded(two: tuple[FakeCloud, Device, Device]) -> None:
    cloud, a, b = two
    there = a.notes.create("같은 글").id
    a.sync()
    own = b.notes.create("같은 글").id
    b.notes.delete(own)
    assert first_look(b.db, b.place, b.clock, b.notes).alike == 0
    joined(b.db, VAULT)
    b.sync()
    assert _in_vault(cloud) == {there, own}  # put in that round, in the trash
    settle([a, b])
    for device in (a, b):
        kept = {n.id: n.deleted_at is not None for n in device.notes.all()}
        assert kept == {there: False, own: True}


def test_the_round_after_the_first_look_fetches_none_of_the_vaults_notes_again(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    cloud, a, b = two
    ids = [a.notes.create(f"A {i}").id for i in range(5)]
    a.sync()
    b.notes.create("B")
    first_look(b.db, b.place, b.clock, b.notes)
    assert _gets(cloud, "b", ids) == 5
    joined(b.db, VAULT)
    b.sync()
    assert _gets(cloud, "b", ids) == 5
    assert len(b.notes.all()) == 6


def test_keeping_only_the_vaults_notes_backs_up_first_and_leaves_no_trace(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    cloud, a, b = two
    a.notes.create("A")
    a.sync()
    b.notes.create("B")
    b.notes.delete(b.notes.create("B의 휴지통").id)
    vault = {p: d for p, d in cloud.files().items() if p.startswith(f"{NOTES}/")}
    backed_up: list[int] = []

    removed = cloud_only(b.db, b.notes, VAULT, lambda: backed_up.append(len(b.notes.all())))
    b.sync()

    assert (removed, backed_up) == (2, [2])  # the backup before anything went
    assert _texts(b) == ["A"] and b.notes.deletion_records() == []
    assert {p: d for p, d in cloud.files().items() if p.startswith(f"{NOTES}/")} == vault


def test_keeping_only_the_vaults_notes_removes_nothing_if_the_backup_fails(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    _, a, b = two
    a.sync()
    b.notes.create("B")

    def failing() -> None:
        raise OSError("the disk is full")

    with pytest.raises(OSError):
        cloud_only(b.db, b.notes, VAULT, failing)
    assert _texts(b) == ["B"] and not sync_state.joining(b.db, VAULT)


def test_what_goes_along_with_keeping_only_the_vaults_notes_is_all_or_nothing(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    _, a, b = two
    a.sync()
    b.notes.create("B")
    along: list[int] = []

    def counted() -> None:
        along.append(len(b.notes.all()))  # in the transaction: the notes already gone

    assert cloud_only(b.db, b.notes, VAULT, lambda: None, counted) == 1
    assert along == [0]

    c_note = b.notes.create("C").id

    def failing() -> None:
        raise OSError("could not keep the login")

    with pytest.raises(OSError):
        cloud_only(b.db, b.notes, VAULT, lambda: None, failing)
    assert [note.id for note in b.notes.all()] == [c_note]  # nothing let go of


def test_a_first_round_cut_short_is_carried_on_as_it_began(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    cloud, a, b = two
    there = a.notes.create("같은 글").id
    a.sync()
    b.notes.create(WELCOME)
    b.notes.create("같은 글")
    b.notes.create("B만")
    joined(b.db, VAULT)
    puts = [0]

    def cut(request: Request) -> Trouble | None:
        if (request.device, request.op) == ("b", "put") and request.path.startswith(f"{NOTES}/"):
            puts[0] += 1
            return Trouble.UNAVAILABLE if puts[0] == 1 else None
        return None

    cloud.trouble = cut
    with pytest.raises(UnavailableError):
        b.sync()
    cloud.trouble = lambda _request: None
    assert sync_state.joining(b.db, VAULT)
    settle([a, b])

    for device in (a, b):
        assert _texts(device) == ["B만", "같은 글"]
        assert device.notes.get(there) is not None


def test_whats_new_the_vault_does_not_have_untouched_is_let_go_of(
    two: tuple[FakeCloud, Device, Device],
) -> None:
    cloud, a, b = two
    a.notes.create("A")
    a.sync()
    b.notes.create(NEWS_KO, "sky", NEWS_ID)
    joined(b.db, VAULT)
    b.sync()
    assert NEWS_ID not in _in_vault(cloud) and b.notes.get(NEWS_ID) is None


def test_whats_new_the_vault_has_is_one_note(two: tuple[FakeCloud, Device, Device]) -> None:
    _, a, b = two
    a.notes.create(NEWS_EN, "sky", NEWS_ID)
    a.sync()
    b.notes.create(NEWS_KO, "sky", NEWS_ID)
    joined(b.db, VAULT)
    settle([a, b])
    for device in (a, b):
        assert _texts(device) == [NEWS_EN]


def test_joining_is_not_stopped_for_a_big_change(two: tuple[FakeCloud, Device, Device]) -> None:
    _, a, b = two
    ids = [a.notes.create(f"A {i}").id for i in range(12)]
    a.sync()
    for note_id in ids:  # the same notes, other texts: a vault made again elsewhere, say
        b.notes.create(f"B {note_id}", note_id=note_id)
    b.accepts = False
    joined(b.db, VAULT)
    b.sync()
    assert b.holds == 0
    assert len(b.notes.all()) == 24  # each with a conflict copy
