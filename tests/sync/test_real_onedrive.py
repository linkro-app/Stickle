"""How OneDrive answers what its target asks: the real OneDrive of whoever runs
this (real_onedrive), and the Graph the tests run against (graph_server), which
has to answer alike for those tests to count. On the real one only: what the
target does not ask but was looked at, and devices syncing through it.

Seen on the real one. A file is made only if none is there, and keeps the tag
its write gave. A write over a file holds to the tag it was read with, both
when its upload session is made and when the file is sent to that: a file
written or deleted in between is neither written over nor made again. A
simple upload holds to a tag too, but makes again a file deleted since it was
read, so the target does not write over with one. A read takes two requests
whichever way: the file's own address only sends to a download address.
Nothing outside the app's folder shows.

What a failing test prints never has an address Graph gave in it: those carry
what lets anyone download or upload. An assert here is on statuses, counts,
yes or no and what the test wrote, never on an answer as a whole.
"""

import http.client
import json
import secrets
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest
import real_onedrive
from fake_cloud import ONEDRIVE
from graph_server import CLIENT_ID, FIRST_REFRESH, graph_server
from sync_bed import Clock, device_id
from test_onedrive_round import KEY, KEYS, Pc

from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.sync.engine import Place
from stickle.sync.format import VAULT_FILE, VaultInfo, vault_file
from stickle.sync.targets.base import Absent, AsRead, NotThereError
from stickle.sync.targets.onedrive import (
    CONTENT_HOSTS,
    MICROSOFT,
    OneDrive,
    _hosted,  # pyright: ignore[reportPrivateUsage]
)

NOTE = "notes/0f8e6f3c-1d9b-4a63-9c38-3b2a4f2f7d10.json"
MADE_ONLY = "@microsoft.graph.conflictBehavior=fail"
LISTED = "$select=name,size,eTag,file"

real_only = pytest.mark.skipif(not real_onedrive.account_given(), reason="no real OneDrive given")


@pytest.fixture(
    params=["graph", *(["real-onedrive"] if real_onedrive.account_given() else [])],
)
def drive(request: pytest.FixtureRequest) -> Iterator[OneDrive]:
    if request.param == "real-onedrive":
        with real_onedrive.folder_of_its_own() as real:
            yield real
        return
    with graph_server() as graph:
        made = OneDrive(
            CLIENT_ID, FIRST_REFRESH, lambda _token: None, place=graph.place, public=graph.context
        )
        yield made
        made.close()


@pytest.fixture
def real() -> Iterator[OneDrive]:
    with real_onedrive.folder_of_its_own() as made:
        yield made


def is_real(drive: OneDrive) -> bool:
    return drive._place == MICROSOFT  # pyright: ignore[reportPrivateUsage]


@dataclass(frozen=True)
class Answered:
    status: int
    data: bytes = field(repr=False)
    headers: http.client.HTTPMessage = field(repr=False)

    @property
    def json(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.data))

    @property
    def code(self) -> str | None:
        try:
            return cast(dict[str, dict[str, str]], json.loads(self.data))["error"]["code"]
        except ValueError, KeyError, TypeError:
            return None

    @property
    def values(self) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], self.json["value"])

    def address(self, name: str) -> str:
        """One Graph gave, as text; never to be asserted on."""
        return cast(str, self.json[name])


def ask(
    drive: OneDrive, method: str, place: str, body: bytes | None = None, **headers: str
) -> Answered:
    """A request to Graph with the access token; or, place being a whole
    address, to a download or upload address Graph gave, with none."""
    sent = {
        "User-Agent": "Stickle",
        **{key.replace("_", "-"): value for key, value in headers.items()},
    }
    if place.startswith("https://"):
        parts = urlsplit(place)
        host, port = parts.hostname or "", parts.port
        place = f"{parts.path}?{parts.query}" if parts.query else parts.path
    else:
        microsoft = drive._place  # pyright: ignore[reportPrivateUsage]
        host, port = microsoft.graph, microsoft.port
        sent["Authorization"] = f"Bearer {drive._tokens.access()}"  # pyright: ignore[reportPrivateUsage]
    connection = http.client.HTTPSConnection(
        host,
        port,
        timeout=30,
        context=drive._https(),  # pyright: ignore[reportPrivateUsage]
    )
    try:
        connection.request(method, place, body=body, headers=sent)
        response = connection.getresponse()
        return Answered(response.status, response.read(), response.headers)
    finally:
        connection.close()


def item(drive: OneDrive, path: str, then: str = "") -> str:
    return drive._item(path, then)  # pyright: ignore[reportPrivateUsage]


def made_only(drive: OneDrive, path: str, data: bytes) -> Answered:
    url = f"{item(drive, path, '/content')}?{MADE_ONLY}"
    return ask(drive, "PUT", url, data, Content_Type="application/octet-stream")


def session(drive: OneDrive, path: str, tag: str) -> Answered:
    body = json.dumps({"item": {"@microsoft.graph.conflictBehavior": "replace"}}).encode()
    return ask(
        drive,
        "POST",
        item(drive, path, "/createUploadSession"),
        body,
        Content_Type="application/json",
        If_Match=tag,
    )


def sent_to(drive: OneDrive, made: Answered, data: bytes) -> int:
    upload = made.address("uploadUrl")
    return ask(
        drive, "PUT", upload, data, Content_Range=f"bytes 0-{len(data) - 1}/{len(data)}"
    ).status


def on_the_hosts_told(address: str) -> bool:
    parts = urlsplit(address)
    return (
        parts.scheme == "https"
        and parts.port is None
        and _hosted(parts.hostname or "", CONTENT_HOSTS)
    )


# What the target asks, alike on both


def test_a_file_is_made_only_if_none_is_there(drive: OneDrive) -> None:
    assert made_only(drive, NOTE, b"1").status == 201
    again = made_only(drive, NOTE, b"22")
    assert (again.status, again.code) == (409, "nameAlreadyExists")
    assert drive.get(NOTE).data == b"1"


def test_a_file_keeps_the_tag_its_write_gave(drive: OneDrive) -> None:
    tag = drive.put(NOTE, b"1", Absent())
    assert tag is not None and ONEDRIVE.tells_tag_of_write
    if is_real(drive):
        time.sleep(10)  # time for anything the cloud does to a file once given it
    assert drive.get(NOTE).tag == tag
    assert [entry.tag for entry in drive.list("notes")] == [tag]


def test_a_file_written_again_as_it_was_has_a_new_tag(drive: OneDrive) -> None:
    first = drive.put(NOTE, b"1", Absent())
    again = drive.put(NOTE, b"1", AsRead(first))
    assert again is not None and again != first
    assert ONEDRIVE.same_content_new_tag


def test_an_upload_session_is_made_only_as_read(drive: OneDrive) -> None:
    tag = drive.put(NOTE, b"1", Absent())
    assert tag is not None
    assert session(drive, NOTE, '"not its tag"').status == 412
    assert session(drive, NOTE, tag).status == 200


@pytest.mark.parametrize("meanwhile", ["written", "deleted"])
def test_a_file_changed_after_its_upload_session_was_made_is_not_written_over(
    drive: OneDrive, meanwhile: str
) -> None:
    tag = drive.put(NOTE, b"1", Absent())
    assert tag is not None
    made = session(drive, NOTE, tag)
    assert made.status == 200
    if meanwhile == "written":
        drive.put(NOTE, b"theirs", AsRead(tag))  # another device, as the target writes over
    else:
        drive.delete(NOTE, AsRead(tag))

    status = sent_to(drive, made, b"mine")

    assert status == 404
    if meanwhile == "written":
        assert drive.get(NOTE).data == b"theirs"
    else:
        with pytest.raises(NotThereError):
            drive.get(NOTE)
        assert not ONEDRIVE.makes_gone_as_read


def test_a_delete_holds_to_the_tag(drive: OneDrive) -> None:
    first = drive.put(NOTE, b"1", Absent())
    assert first is not None
    drive.put(NOTE, b"22", AsRead(first))
    assert ask(drive, "DELETE", item(drive, NOTE), If_Match=first).status == 412
    now = drive.get(NOTE).tag
    assert now is not None
    assert ask(drive, "DELETE", item(drive, NOTE), If_Match=now).status == 204
    assert ask(drive, "DELETE", item(drive, NOTE), If_Match=now).status == 404


def test_a_file_is_made_in_folders_not_there_yet(drive: OneDrive) -> None:
    assert made_only(drive, "deep/er/note.json", b"1").status == 201
    assert [entry.path for entry in drive.list("deep/er")] == ["deep/er/note.json"]


def test_a_folder_is_listed_a_page_at_a_time_from_graph(drive: OneDrive) -> None:
    for n in range(3):
        drive.put(f"many/{n}.json", b"1", Absent())
    first = ask(drive, "GET", f"{item(drive, 'many', '/children')}?{LISTED}&$top=2")
    assert (first.status, len(first.values)) == (200, 2)
    link = urlsplit(first.address("@odata.nextLink"))
    graph = drive._place.graph  # pyright: ignore[reportPrivateUsage]
    on_graph = link.scheme == "https" and link.hostname == graph and link.path.startswith("/v1.0/")
    assert on_graph
    rest = ask(drive, "GET", f"{link.path}?{link.query}")
    assert (rest.status, len(rest.values)) == (200, 1)
    whole = ask(drive, "GET", f"{item(drive, 'many', '/children')}?{LISTED}&$top=200")
    assert (whole.status, len(whole.values)) == (200, 3)


# On the real one only: what the target does not ask, or cannot be played here


@real_only
def test_a_simple_upload_holds_to_a_tag_but_makes_a_deleted_file_again(real: OneDrive) -> None:
    def put(data: bytes, tag: str) -> int:
        url = item(real, NOTE, "/content")
        return ask(
            real, "PUT", url, data, Content_Type="application/octet-stream", If_Match=tag
        ).status

    tag = real.put(NOTE, b"1", Absent())
    assert tag is not None
    assert put(b"22", '"not its tag"') == 412
    assert put(b"22", tag) == 200
    assert put(b"333", tag) == 412
    now = real.get(NOTE).tag
    assert now is not None
    real.delete(NOTE, AsRead(now))
    assert put(b"4444", now) == 201  # made again: why a write over takes an upload session
    assert real.get(NOTE).data == b"4444"


@real_only
def test_a_read_takes_two_requests_whichever_way(real: OneDrive) -> None:
    real.put(NOTE, b"12345", Absent())
    details = ask(real, "GET", item(real, NOTE))
    content = ask(real, "GET", item(real, NOTE, "/content"))
    assert content.status == 302 and content.headers.get("ETag") is None
    where = content.headers.get("Location") or ""
    download = ask(real, "GET", where)
    assert download.status == 200 and download.data == b"12345"
    tagged_alike = download.headers.get("ETag") == details.json["eTag"]
    assert tagged_alike


@real_only
def test_download_and_upload_addresses_are_on_the_hosts_told(real: OneDrive) -> None:
    tag = real.put(NOTE, b"1", Absent())
    assert tag is not None
    details = ask(real, "GET", item(real, NOTE))
    made = session(real, NOTE, tag)
    content = ask(real, "GET", item(real, NOTE, "/content"))
    addresses = [
        details.address("@microsoft.graph.downloadUrl"),
        made.address("uploadUrl"),
        content.headers.get("Location") or "",
    ]
    told = [on_the_hosts_told(address) for address in addresses]
    assert told == [True, True, True]


@real_only
def test_what_changed_in_the_apps_folder_can_be_asked(real: OneDrive) -> None:
    """Not asked by the target, which learns of changes as on WebDAV; seen for later."""
    real.put(NOTE, b"1", Absent())
    whole = ask(real, "GET", "/v1.0/me/drive/special/approot/delta")
    inside = ask(real, "GET", item(real, "", "/delta"))
    followed = [isinstance(each.json.get("value"), list) for each in (whole, inside)]
    assert (whole.status, inside.status, followed) == (200, 200, [True, True])


@real_only
def test_nothing_outside_the_apps_folder_shows(real: OneDrive) -> None:
    root = ask(real, "GET", "/v1.0/me/drive/root/children")
    assert (root.status, len(root.values)) == (200, 0)
    account = ask(real, "GET", "/v1.0/me")
    assert account.status == 403  # what Graph answers for what the app was not given


@real_only
def test_devices_keep_a_note_through_it_and_lose_neither_text(tmp_path: Path) -> None:
    with real_onedrive.folder_of_its_own() as first:
        second = real_onedrive.another_way_in(first)
        vault = str(uuid.uuid4())
        clock = Clock()

        def pc(name: str, target: OneDrive) -> Pc:
            db = open_store(tmp_path / f"{name}-{secrets.token_hex(2)}.db", KEY)
            place = Place(target, vault, KEYS, device_id(name))
            return Pc(db, NoteRepository(db), place, target, clock)

        laptop = pc("aaaaaaaa", first)
        desktop = pc("bbbbbbbb", second)
        first.put(VAULT_FILE, vault_file(VaultInfo(vault)), Absent())

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
        second.close()
        laptop.db.close()
        desktop.db.close()
