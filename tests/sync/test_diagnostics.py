"""What a feedback report says of sync: counts and a time, never a note's
text or title, a category's name, a device's name or id, the vault's id, or
a path."""

from pathlib import Path

from fake_cloud import DROPBOX, FakeCloud
from sync_bed import VAULT, Clock, Device, settle

from stickle.sync.diagnostics import lines
from stickle.sync.format import NOTES, item_path
from stickle.sync.left import FAILS, WAIT

DAMAGED = b'{"alg":1,"format":1,"id":"x","box":"not a box at all"}'


def test_a_device_never_synced_says_so(tmp_path: Path) -> None:
    a = Device("a", tmp_path, FakeCloud(DROPBOX), Clock(), whole=True)
    said = lines(a.db, a.place)
    assert said[0] == "Sync: format 1, 0 notes, 0 records, 0 devices, last round never"
    a.close()


def test_what_sync_says_is_counts_and_a_time_only(tmp_path: Path) -> None:
    cloud, clock = FakeCloud(DROPBOX), Clock()
    a = Device("a", tmp_path, cloud, clock, whole=True, aside=tmp_path / "aside a")
    b = Device("b", tmp_path, cloud, clock, whole=True)
    first = a.notes.create("비밀스러운 첫 메모")
    second = a.notes.create("두 번째 메모")
    a.labels.create_category("가족 일정", "blue")
    settle([a, b])
    a.notes.update_body(first.id, "a가 고친 글")
    b.notes.update_body(first.id, "b가 고친 글")
    a.sync()
    b.sync()
    a.sync()  # a conflict copy of the first note, here
    cloud.put_by_hand(item_path(NOTES, second.id), DAMAGED)
    for _ in range(FAILS):
        a.sync()
        clock.tick(WAIT)

    said = lines(a.db, a.place, failing=2)

    assert said[0].startswith("Sync: format 1, 3 notes, 5 records, 2 devices, last round 2025-")
    assert said[0].endswith(" UTC")
    assert said[1] == (
        "Sync now: 1 conflict copies, 2 rounds failing, 1 files set aside,"
        " 0 notes for a newer version"
    )
    text = "\n".join(said)
    for secret in (
        "메모",
        "가족 일정",
        "A-PC",
        a.id,
        b.id,
        first.id,
        VAULT,
        str(tmp_path),
        "aside a",
    ):
        assert secret not in text
    a.close()
    b.close()
