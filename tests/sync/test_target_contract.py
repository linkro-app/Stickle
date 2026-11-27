"""What every target answers alike (stickle.sync.targets.base). It runs on each
fake cloud and on the real targets, so that the fakes sync's tests run on
answer as the real clouds do: if a fake differs from its cloud, no amount of
testing on it shows how sync fares there.

What a file holds changes size from one write to the next here: a WebDAV
server may make its tag from the second a file was written and its size, and
then two writes of one size within a second keep the tag (test_wsgidav_facts).
Sync cannot count on a condition there either; its tests meet that on the fake
WebDAV, which does the same."""

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import real_dropbox
import real_onedrive
from dropbox_server import APP_KEY, dropbox_server
from dropbox_server import FIRST_REFRESH as DROPBOX_REFRESH
from fake_cloud import KINDS, FakeCloud, Kind
from graph_server import CLIENT_ID, FIRST_REFRESH, graph_server
from real_server import account_given, folder_of_its_own
from webdav_server import HOST, webdav

from stickle.sync.targets.base import (
    Absent,
    AsRead,
    NotThereError,
    Read,
    RefusedError,
    Target,
)
from stickle.sync.targets.dropbox import Dropbox
from stickle.sync.targets.onedrive import OneDrive
from stickle.sync.targets.webdav import WebDav, parse_address

NOTE = "notes/0f8e6f3c-1d9b-4a63-9c38-3b2a4f2f7d10.json"


# A real server, through the real target. It keeps conditions, and is told so
# for the test to see that they reach it as they should.
WSGIDAV = Kind("wsgidav", "seconds")


# The real server of whoever runs this, told it keeps conditions likewise.
REAL_SERVER = Kind("real-server", "weak")

# Graph in the test, over HTTPS, through the real OneDrive target; and the
# real OneDrive of whoever runs this.
GRAPH = Kind("graph", "etag")
REAL_ONEDRIVE = Kind("real-onedrive", "etag")

# Dropbox's API in the test, over HTTPS, through the real Dropbox target; and
# the real Dropbox of whoever runs this.
DROPBOX_API = Kind("dropbox-api", "rev")
REAL_DROPBOX = Kind("real-dropbox", "rev")


@pytest.fixture(
    params=[
        *KINDS,
        WSGIDAV,
        GRAPH,
        DROPBOX_API,
        *([REAL_SERVER] if account_given() else []),
        *([REAL_ONEDRIVE] if real_onedrive.account_given() else []),
        *([REAL_DROPBOX] if real_dropbox.account_given() else []),
    ],
    ids=lambda kind: kind.name,
)
def kind(request: pytest.FixtureRequest) -> Kind:
    return request.param


@pytest.fixture
def cloud(kind: Kind) -> FakeCloud:
    return FakeCloud(kind)


@pytest.fixture
def target(kind: Kind, cloud: FakeCloud, tmp_path: Path) -> Iterator[Target]:
    if kind is REAL_SERVER:
        with folder_of_its_own(keeps_conditions=True) as real:
            yield real
        return
    if kind is REAL_ONEDRIVE:
        with real_onedrive.folder_of_its_own() as real:
            yield real
        return
    if kind is REAL_DROPBOX:
        with real_dropbox.folder_of_its_own() as real:
            yield real
        return
    if kind is GRAPH:
        with graph_server() as graph:
            drive = OneDrive(
                CLIENT_ID,
                FIRST_REFRESH,
                lambda _token: None,
                place=graph.place,
                public=graph.context,
            )
            yield drive
            drive.close()
        return
    if kind is DROPBOX_API:
        with dropbox_server() as box:
            dropbox = Dropbox(
                APP_KEY, DROPBOX_REFRESH, lambda _token: None, place=box.place, public=box.context
            )
            yield dropbox
            dropbox.close()
        return
    if kind is not WSGIDAV:
        yield cloud.target("laptop")
        return
    root = tmp_path / "dav"
    root.mkdir()
    with webdav(root) as running:
        real = WebDav(
            parse_address(f"http://{HOST}:{running.port}"), "stickle", "", keeps_conditions=True
        )
        yield real
        real.close()


def settled(target: Target, path: str) -> Read:
    """Read until its tag is a strong one: a WebDAV server may give a weak tag
    for a file written within the second, which no condition holds for."""
    for _ in range(20):
        read = target.get(path)
        if read.tag is None or not read.tag.startswith("W/"):
            return read
        time.sleep(0.1)  # for a real server: its second has to pass
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

    assert sorted((e.path, e.size) for e in listed) == [("notes/a.json", 1), ("notes/b.json", 2)]
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
