"""A round looks at all that changed in the vault before it takes anything in:
the files it reads are kept here, one at a time, and taken in from there. One
kept by a round that broke off, and listed with the same tag since, is not
fetched again, where tags tell writes apart."""

from pathlib import Path

import pytest
from fake_cloud import DROPBOX, WEBDAV_WEAK, FakeCloud, Kind, Request, Trouble
from sync_bed import VAULT, Clock, Device, settle

from stickle.data import sync_staged
from stickle.sync.format import NOTES, item_path
from stickle.sync.targets.base import UnavailableError


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _gets(cloud: FakeCloud, device: str, path: str) -> int:
    return sum(1 for line in cloud.log if line.split()[1:4] == [device, "get", f"{path}:"])


def _records_down(request: Request) -> Trouble | None:
    if (request.device, request.op, request.path) == ("b", "list", "records"):
        return Trouble.UNAVAILABLE
    return None


@pytest.mark.parametrize(("kind", "again"), [(DROPBOX, 0), (WEBDAV_WEAK, 1)], ids=["rev", "weak"])
def test_a_file_read_by_a_round_that_broke_off_is_taken_in_without_fetching_it_again(
    tmp_path: Path, clock: Clock, kind: Kind, again: int
) -> None:
    cloud = FakeCloud(kind)
    a = Device("a", tmp_path, cloud, clock, whole=True)
    b = Device("b", tmp_path, cloud, clock, whole=True)
    note_id = a.notes.create("메모").id
    settle([a, b])
    a.notes.update_body(note_id, "고친 글")
    clock.tick(2_000)
    a.sync()
    path = item_path(NOTES, note_id)
    read = _gets(cloud, "b", path)

    cloud.trouble = _records_down
    with pytest.raises(UnavailableError):
        b.sync()
    there = b.notes.get(note_id)
    assert there is not None and there.body == "메모"  # looked at, nothing taken in
    assert _gets(cloud, "b", path) == read + 1
    assert path in sync_staged.tags(b.db, VAULT)

    cloud.trouble = lambda _request: None
    b.sync()
    there = b.notes.get(note_id)
    assert there is not None and there.body == "고친 글"
    assert _gets(cloud, "b", path) == read + 1 + again  # tags alike may hide a write
    assert sync_staged.tags(b.db, VAULT) == {}  # the round through: none kept
    a.close()
    b.close()
