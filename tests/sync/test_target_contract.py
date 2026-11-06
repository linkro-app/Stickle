"""What every target answers alike (stickle.sync.targets.base). It runs on each
fake cloud now, and on the real targets as they come, so that the fakes sync's
tests run on answer as the real clouds do: if a fake differs from its cloud, no
amount of testing on it shows how sync fares there.

What a file holds changes size from one write to the next here: a WebDAV
server may make its tag from the second a file was written and its size, and
then two writes of one size within a second keep the tag (test_wsgidav_facts).
Sync cannot count on a condition there either; its tests meet that on the fake
WebDAV, which does the same."""

import os
from collections.abc import Iterator

import pytest
from fake_cloud import KINDS, FakeCloud, Kind

from stickle.sync.targets.base import (
    Absent,
    AsRead,
    Changes,
    CursorExpiredError,
    NotThereError,
    Read,
    RefusedError,
    Target,
)

NOTE = "notes/0f8e6f3c-1d9b-4a63-9c38-3b2a4f2f7d10.json"


@pytest.fixture(params=KINDS, ids=lambda kind: kind.name)
def kind(request: pytest.FixtureRequest) -> Kind:
    return request.param


@pytest.fixture
def cloud(kind: Kind) -> FakeCloud:
    return FakeCloud(kind)


@pytest.fixture
def target(cloud: FakeCloud) -> Target:
    return cloud.target("laptop")


def settled(target: Target, path: str) -> Read:
    """Read until its tag is a strong one: a WebDAV server may give a weak tag
    for a file written within the second, which no condition holds for."""
    for _ in range(20):
        read = target.get(path)
        if read.tag is None or not read.tag.startswith("W/"):
            return read
    raise AssertionError(f"{path} kept a weak tag")


def comes_back(target: Target, path: str, data: bytes) -> None:
    """Its content as written. A WebDAV server may show a file still being
    written: one read may come back short, never otherwise; read again, it is
    whole (sync takes a file that will not open for "not all there yet")."""
    first = target.get(path).data
    if first != data:
        assert data.startswith(first), "a read that is not a part of what was written"
        assert target.get(path).data == data


def test_a_file_is_made_only_if_none_is_there(target: Target) -> None:
    tag = target.put(NOTE, b"1", Absent())
    comes_back(target, NOTE, b"1")
    if tag is not None and not tag.startswith("W/"):
        assert settled(target, NOTE).tag == tag  # the tag it gave is the file's

    if target.keeps_conditions:
        with pytest.raises(RefusedError):
            target.put(NOTE, b"22", Absent())
        comes_back(target, NOTE, b"1")  # nothing changed
    else:
        target.put(NOTE, b"22", Absent())  # goes through: sync keeps it until it is seen
        comes_back(target, NOTE, b"22")


def test_a_file_is_written_over_only_if_it_is_still_as_read(target: Target) -> None:
    target.put(NOTE, b"1", Absent())
    first = settled(target, NOTE)

    target.put(NOTE, b"22", AsRead(first.tag))
    comes_back(target, NOTE, b"22")

    if target.keeps_conditions:
        with pytest.raises(RefusedError):
            target.put(NOTE, b"333", AsRead(first.tag))  # read before "two"
        comes_back(target, NOTE, b"22")
    else:
        target.put(NOTE, b"333", AsRead(first.tag))
        comes_back(target, NOTE, b"333")


def test_a_file_is_deleted_only_if_it_is_still_as_read(target: Target) -> None:
    target.put(NOTE, b"1", Absent())
    first = settled(target, NOTE)
    target.put(NOTE, b"22", AsRead(first.tag))

    if target.keeps_conditions:
        with pytest.raises(RefusedError):
            target.delete(NOTE, AsRead(first.tag))
        comes_back(target, NOTE, b"22")
        target.delete(NOTE, AsRead(settled(target, NOTE).tag))
    else:
        target.delete(NOTE, AsRead(first.tag))
    with pytest.raises(NotThereError):
        target.get(NOTE)


def test_what_is_not_there(target: Target) -> None:
    with pytest.raises(NotThereError):
        target.get(NOTE)
    with pytest.raises(NotThereError):
        target.delete(NOTE, AsRead("0"))
    # Written as read once, gone since: refused, or made again (wsgidav makes it,
    # where the standard says refused). Sync copes with both.
    try:
        target.put(NOTE, b"1", AsRead("0"))
    except RefusedError:
        with pytest.raises(NotThereError):
            target.get(NOTE)
    else:
        comes_back(target, NOTE, b"1")


def test_a_folder_lists_the_files_right_in_it(target: Target) -> None:
    files = {
        "vault.json": b"{}",
        "notes/a.json": b"a",
        "notes/b.json": b"bb",
        "records/c.json": b"ccc",
    }
    for path, data in files.items():
        target.put(path, data, Absent())

    listed = list(target.list("notes"))

    assert [(e.path, e.size) for e in listed] == [("notes/a.json", 1), ("notes/b.json", 2)]
    assert [e.path for e in target.list("records")] == ["records/c.json"]
    assert [e.path for e in target.list("")] == ["vault.json"]
    assert list(target.list("keys")) == []
    for entry in target.list("notes"):
        tag = settled(target, entry.path).tag
        if entry.tag is not None and not entry.tag.startswith("W/"):
            assert entry.tag == tag


def test_a_megabyte_comes_back_as_it_went(target: Target) -> None:
    data = os.urandom(1024 * 1024)
    target.put(NOTE, data, Absent())
    comes_back(target, NOTE, data)


def test_written_again_as_it_was_the_tag_may_or_may_not_change(target: Target) -> None:
    # Dropbox keeps its rev; others give a new tag. Sync relies on neither.
    target.put(NOTE, b"1", Absent())
    read = settled(target, NOTE)
    target.put(NOTE, b"1", AsRead(read.tag))
    comes_back(target, NOTE, b"1")


def seen(pages: Iterator[Changes]) -> tuple[set[str], set[str], str]:
    changed: set[str] = set()
    gone: set[str] = set()
    cursor = ""
    for page in pages:
        for entry in page.changed:
            changed.add(entry.path)
            gone.discard(entry.path)
        for path in page.gone:
            gone.add(path)
            changed.discard(path)
        cursor = page.cursor
    return changed, gone, cursor


def test_what_changed_since_a_cursor_comes_page_by_page(target: Target) -> None:
    target.put("notes/kept.json", b"k", Absent())
    target.put("notes/old.json", b"o", Absent())
    first = target.changes(None)
    if first is None:
        assert target.wait_for_change("c0", 0) is None  # neither: sync compares lists
        return
    _changed, _gone, cursor = seen(first)
    old = settled(target, "notes/old.json")
    target.put("notes/new.json", b"n", Absent())
    target.delete("notes/old.json", AsRead(old.tag))
    for number in range(4):  # some clouds show changes a little later
        target.put(f"records/{number}.json", b"r", Absent())

    changed: set[str] = set()
    gone: set[str] = set()
    for _ in range(10):  # looked again until all is shown, as sync does
        pages = target.changes(cursor)
        assert pages is not None
        more_changed, more_gone, cursor = seen(pages)
        changed = (changed - more_gone) | more_changed
        gone = (gone - more_changed) | more_gone
        if len(changed) == 5:
            break

    assert changed == {"notes/new.json"} | {f"records/{n}.json" for n in range(4)}
    assert gone == {"notes/old.json"}
    pages = target.changes(cursor)
    assert pages is not None and seen(pages)[:2] == (set(), set())  # nothing since


def test_an_old_cursor_starts_over(cloud: FakeCloud, target: Target) -> None:
    pages = target.changes(None)
    if pages is None:
        return
    cursor = seen(pages)[2]
    cloud.expire_cursors()  # a real cloud does this after some days
    with pytest.raises(CursorExpiredError):
        list(target.changes(cursor) or [])


def test_waiting_tells_a_change_made_elsewhere(cloud: FakeCloud, target: Target) -> None:
    pages = target.changes(None)
    if pages is None:
        return
    cursor = seen(pages)[2]
    answer = target.wait_for_change(cursor, 30)
    if answer is None:
        return  # no long poll: sync looks every so often
    assert answer is False
    cloud.target("desktop").put(NOTE, b"from the desktop", Absent())
    for _ in range(5):
        if target.wait_for_change(cursor, 30):
            break
    else:
        raise AssertionError("the change was never told")
