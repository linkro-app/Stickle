"""How Dropbox answers what its target asks: the real Dropbox of whoever runs
this (real_dropbox), and Dropbox's API the tests run against (dropbox_server),
which has to answer alike for those tests to count. On the real one only: the
long poll as it answers, the watcher on it, and devices syncing through it.

Seen on the real one, with a test account. A file is made only if none is
there, even of the same bytes, and keeps the rev its write gave. The same
bytes written again at its rev keep that rev, and are not refused. A write
over a file holds to the rev read, and a file deleted since is not made again.
A delete at another rev than the file's is refused as a conflict; at any rev,
of a file not there, Dropbox says there is none. A long poll asked to wait 30
seconds, with nothing changing, answered in under 40 that nothing had, with
no backoff; one whose cursor is behind a change answers at once, and a change
another device makes while it waits is told within seconds. Two sign-ins of
one account are apart: revoking one leaves the other working, and the one
revoked is turned down at once (its access token, then its refresh token).
Disconnected in the account's Connected apps, the app's access tokens stop at
once too. An old cursor (reset) and a backoff could not be brought about
there; the tests on the fake Dropbox keep to what the documentation says.

What a failing test prints never has a token or a cursor in it. An assert here
is on statuses, error tags, yes or no and what the test wrote, never on an
answer as a whole.
"""

import http.client
import json
import secrets
import threading
import time
import uuid
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest
import real_dropbox
from dropbox_server import APP_KEY, FIRST_REFRESH, dropbox_server
from fake_cloud import DROPBOX as DROPBOX_KIND
from sync_bed import Clock, device_id
from test_dropbox_round import KEY, KEYS, Pc, Told

from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.sync.engine import Place
from stickle.sync.format import DEVICES, VAULT_FILE, VaultInfo, vault_file
from stickle.sync.targets.base import Absent, AsRead, NotThereError, RefusedError, Waited
from stickle.sync.targets.dropbox import (
    DROPBOX,
    PAGE,
    Dropbox,
    _error_tags,  # pyright: ignore[reportPrivateUsage]
    _rpc,  # pyright: ignore[reportPrivateUsage]
)
from stickle.sync.watcher import Watcher

NOTE = "notes/0f8e6f3c-1d9b-4a63-9c38-3b2a4f2f7d10.json"
MINE = f"{DEVICES}/aaaaaaaa.json"
THEIRS = f"{DEVICES}/bbbbbbbb.json"
SHORTEST_WAIT = 30  # seconds: the least a long poll may be asked to wait
JITTER = 90  # seconds Dropbox may add to a long poll's wait
PROMPTLY = 60.0  # seconds within which a change made should be told, on the real one

real_only = pytest.mark.skipif(not real_dropbox.account_given(), reason="no real Dropbox given")


@contextmanager
def a_dropbox(kind: str, page: int = PAGE) -> Generator[Dropbox]:
    if kind == "real-dropbox":
        with real_dropbox.folder_of_its_own(page) as real:
            yield real
        return
    with dropbox_server() as box:
        made = Dropbox(
            APP_KEY,
            FIRST_REFRESH,
            lambda _token: None,
            place=box.place,
            public=box.context,
            page=page,
        )
        yield made
        made.close()


KINDS = ["dropbox-api", *(["real-dropbox"] if real_dropbox.account_given() else [])]


@pytest.fixture(params=KINDS)
def drive(request: pytest.FixtureRequest) -> Iterator[Dropbox]:
    with a_dropbox(request.param) as made:
        yield made


@pytest.fixture(params=KINDS)
def paged(request: pytest.FixtureRequest) -> Iterator[Dropbox]:
    """Listing two files a page."""
    with a_dropbox(request.param, page=2) as made:
        yield made


@pytest.fixture
def real() -> Iterator[Dropbox]:
    with real_dropbox.folder_of_its_own() as made:
        yield made


@pytest.fixture
def other(real: Dropbox) -> Iterator[Dropbox]:
    """Another device, in the same folder."""
    made = real_dropbox.another_way_in(real)
    yield made
    made.close()


def is_real(drive: Dropbox) -> bool:
    return drive._place == DROPBOX  # pyright: ignore[reportPrivateUsage]


def rpc(drive: Dropbox, path: str, args: dict[str, object]) -> tuple[int, set[str]]:
    """A request to the API host as the target makes it: its status, and the
    tags of what went wrong."""
    answer = _rpc(drive._shared, drive._api, path, args)  # pyright: ignore[reportPrivateUsage]
    return answer.status, _error_tags(answer)


def where(drive: Dropbox, path: str) -> str:
    return drive._path(path)  # pyright: ignore[reportPrivateUsage]


def long_poll(drive: Dropbox, cursor: str, seconds: int) -> tuple[int, dict[str, object], float]:
    """A long poll asked to wait that many seconds: its status, what it said,
    and how long it took."""
    place = drive._place  # pyright: ignore[reportPrivateUsage]
    connection = http.client.HTTPSConnection(
        place.notify,
        place.port(2),
        timeout=seconds + JITTER + 30,
        context=drive._shared.context(),  # pyright: ignore[reportPrivateUsage]
    )
    body = json.dumps({"cursor": cursor, "timeout": seconds}).encode()
    began = time.monotonic()
    try:
        connection.request(
            "POST",
            "/2/files/list_folder/longpoll",
            body=body,
            headers={"User-Agent": "Stickle", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        data = response.read()
    finally:
        connection.close()
    said = cast(dict[str, object], json.loads(data)) if response.status == 200 else {}
    return response.status, said, time.monotonic() - began


# What the target asks, alike on both


def test_a_file_is_made_only_if_none_is_there_even_of_the_same_bytes(drive: Dropbox) -> None:
    assert drive.put(NOTE, b"1", Absent()) is not None
    with pytest.raises(RefusedError):
        drive.put(NOTE, b"1", Absent())
    with pytest.raises(RefusedError):
        drive.put(NOTE, b"22", Absent())
    assert drive.get(NOTE).data == b"1"


def test_a_file_keeps_the_rev_its_write_gave(drive: Dropbox) -> None:
    rev = drive.put(NOTE, b"1", Absent())
    assert rev is not None and DROPBOX_KIND.tells_tag_of_write
    if is_real(drive):
        time.sleep(10)  # time for anything the cloud does to a file once given it
    assert drive.get(NOTE).tag == rev
    assert [entry.tag for entry in drive.list("notes")] == [rev]


def test_the_same_bytes_again_at_its_rev_keep_the_rev(drive: Dropbox) -> None:
    first = drive.put(NOTE, b"1", Absent())
    again = drive.put(NOTE, b"1", AsRead(first))
    assert again == first and not DROPBOX_KIND.same_content_new_tag
    assert drive.get(NOTE).tag == first


def test_a_file_is_written_over_only_at_the_rev_read(drive: Dropbox) -> None:
    first = drive.put(NOTE, b"1", Absent())
    second = drive.put(NOTE, b"22", AsRead(first))
    assert second is not None and second != first
    with pytest.raises(RefusedError):
        drive.put(NOTE, b"333", AsRead(first))
    assert drive.get(NOTE).data == b"22"


def test_a_file_deleted_since_it_was_read_is_not_made_again(drive: Dropbox) -> None:
    rev = drive.put(NOTE, b"1", Absent())
    drive.delete(NOTE, AsRead(rev))
    with pytest.raises(RefusedError):
        drive.put(NOTE, b"22", AsRead(rev))
    with pytest.raises(NotThereError):
        drive.get(NOTE)
    assert not DROPBOX_KIND.makes_gone_as_read


def test_a_delete_holds_to_the_rev(drive: Dropbox) -> None:
    first = drive.put(NOTE, b"1", Absent())
    now = drive.put(NOTE, b"22", AsRead(first))
    assert first is not None and now is not None

    status, tags = rpc(
        drive, "/2/files/delete_v2", {"path": where(drive, NOTE), "parent_rev": first}
    )
    assert (status, "conflict" in tags, "not_found" in tags) == (409, True, False)
    with pytest.raises(RefusedError):
        drive.delete(NOTE, AsRead(first))
    assert drive.get(NOTE).data == b"22"

    drive.delete(NOTE, AsRead(now))
    status, tags = rpc(drive, "/2/files/delete_v2", {"path": where(drive, NOTE), "parent_rev": now})
    assert (status, "not_found" in tags) == (409, True)
    with pytest.raises(NotThereError):
        drive.delete(NOTE, AsRead(now))


def test_a_file_is_made_in_folders_not_there_yet(drive: Dropbox) -> None:
    drive.put("deep/er/note.json", b"1", Absent())
    assert [entry.path for entry in drive.list("deep/er")] == ["deep/er/note.json"]


def test_a_folder_is_listed_whole_a_page_at_a_time(paged: Dropbox) -> None:
    for n in range(5):
        paged.put(f"many/{n}.json", b"1", Absent())
    assert sorted(entry.path for entry in paged.list("many")) == [
        f"many/{n}.json" for n in range(5)
    ]


# On the real one only: the long poll as it answers, and what waits on it


@real_only
def test_a_wait_with_no_change_ends_saying_so(real: Dropbox) -> None:
    real.put(MINE, b"a", Absent())
    feed = real.feed(DEVICES)
    status, said, took = long_poll(real, feed.latest(), SHORTEST_WAIT)
    feed.close()
    assert (status, said.get("changes")) == (200, False)
    assert set(said) <= {"changes", "backoff"}
    assert SHORTEST_WAIT - 5 <= took <= SHORTEST_WAIT + JITTER + 15


@real_only
def test_a_wait_is_told_at_once_of_another_device_s_change(real: Dropbox, other: Dropbox) -> None:
    real.put(MINE, b"a", Absent())
    feed = real.feed(DEVICES)
    cursor = feed.latest()
    waited: list[Waited] = []
    waiting = threading.Thread(target=lambda: waited.append(feed.wait(cursor)), daemon=True)
    waiting.start()
    time.sleep(3)  # the wait under way

    began = time.monotonic()
    other.put(THEIRS, b"b", Absent())
    waiting.join(PROMPTLY)

    assert not waiting.is_alive() and waited[0].changes is True
    assert time.monotonic() - began < PROMPTLY
    others, after = feed.since(cursor)
    assert others is True and after != cursor
    feed.close()


@real_only
def test_its_own_write_is_told_and_known_for_its_own(real: Dropbox) -> None:
    rev = real.put(MINE, b"a", Absent())
    feed = real.feed(DEVICES)
    cursor = feed.latest()
    real.put(MINE, b"aa", AsRead(rev))

    assert feed.wait(cursor).changes is True
    assert feed.since(cursor)[0] is False
    feed.close()


@real_only
def test_the_watcher_wakes_for_another_device_and_not_for_its_own(
    real: Dropbox, other: Dropbox
) -> None:
    rev = real.put(MINE, b"a", Absent())
    told = Told()
    watcher = Watcher(real.feed(DEVICES), told.changed, told.work)
    watcher.start()
    try:
        assert told.told(PROMPTLY)  # its cursor taken: a round looks once
        real.put(MINE, b"aa", AsRead(rev))
        assert not told.told(20)
        other.put(THEIRS, b"b", Absent())
        assert told.told(PROMPTLY)
        assert told.working == [True]
    finally:
        assert watcher.stop(5)


@real_only
def test_devices_keep_a_note_through_it_and_lose_neither_text(
    real: Dropbox, other: Dropbox, tmp_path: Path
) -> None:
    vault = str(uuid.uuid4())
    clock = Clock()

    def pc(name: str, target: Dropbox) -> Pc:
        db = open_store(tmp_path / f"{name}-{secrets.token_hex(2)}.db", KEY)
        place = Place(target, vault, KEYS, device_id(name))
        return Pc(name, db, NoteRepository(db), place, target, clock)

    laptop = pc("aaaaaaaa", real)
    desktop = pc("bbbbbbbb", other)
    real.put(VAULT_FILE, vault_file(VaultInfo(vault)), Absent())

    note = laptop.notes.create("first")
    laptop.sync()
    desktop.sync()
    assert desktop.texts() == ["first"]

    clock.tick(5_000)
    laptop.notes.update_body(note.id, "the laptop's")
    desktop.notes.update_body(note.id, "the desktop's")
    for _ in range(3):
        clock.tick(5_000)
        laptop.sync()
        desktop.sync()

    assert laptop.texts() == desktop.texts()
    assert {"the laptop's", "the desktop's"} <= set(laptop.texts())
    laptop.db.close()
    desktop.db.close()
