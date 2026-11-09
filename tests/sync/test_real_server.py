"""How the real server of whoever runs this answers (real_server), and that the
fake of it in sync's tests answers the same (fake_cloud.SYNOLOGY); what differs
goes into the fake as another kind. And devices syncing through it.

Conditions are kept. A file written within the second has a weak tag, which no
condition holds for, and a write's answer gives no tag. A file deleted since
it was read is refused, as the standard says. These never write into a folder
that is not there: that alone leaves this server failing other writes for minutes.
"""

import secrets
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_cloud import SYNOLOGY
from real_server import account_given, another_way_in, folder_of_its_own
from sync_bed import Clock, device_id
from test_webdav_round import KEY, KEYS, Pc

from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.sync.engine import Place
from stickle.sync.format import VAULT_FILE, VaultInfo, vault_file
from stickle.sync.targets.base import Absent
from stickle.sync.targets.webdav import WebDav

pytestmark = pytest.mark.skipif(not account_given(), reason="no real server given")

NOTE = "notes/0f8e6f3c-1d9b-4a63-9c38-3b2a4f2f7d10.json"


@pytest.fixture
def target() -> Iterator[WebDav]:
    with folder_of_its_own() as made:
        made.put("notes/made-first.json", b"x", Absent())  # the folders, the way sync makes them
        yield made


def status(
    target: WebDav, method: str, path: str, body: bytes | None = None, **headers: str
) -> int:
    url = target._url(path)  # pyright: ignore[reportPrivateUsage]
    sent = {key.replace("_", "-"): value for key, value in headers.items()}
    return target._ask(method, url, body, sent).status  # pyright: ignore[reportPrivateUsage]


def settled_tag(target: WebDav, path: str) -> str:
    for _ in range(40):
        tag = target.get(path).tag
        assert tag is not None
        if not tag.startswith("W/"):
            return tag
        time.sleep(0.1)
    raise AssertionError("its tag stayed weak")


def test_a_file_is_made_only_if_none_is_there(target: WebDav) -> None:
    assert status(target, "PUT", NOTE, b"1", If_None_Match="*") == 201
    assert status(target, "PUT", NOTE, b"22", If_None_Match="*") == 412
    assert target.get(NOTE).data == b"1"
    assert SYNOLOGY.keeps_conditions


def test_a_write_says_no_tag_and_a_new_file_has_a_weak_one(target: WebDav) -> None:
    assert target.put(NOTE, b"1", Absent()) is None
    assert not SYNOLOGY.tells_tag_of_write
    tag = target.get(NOTE).tag
    assert tag is not None and tag.startswith("W/")
    assert SYNOLOGY.tags == "weak"


def test_a_settled_tag_is_held_to_when_written_over_or_deleted(target: WebDav) -> None:
    target.put(NOTE, b"1", Absent())
    first = settled_tag(target, NOTE)

    assert status(target, "PUT", NOTE, b"22", If_Match='"not its tag"') == 412
    assert status(target, "PUT", NOTE, b"22", If_Match=first) == 204
    second = settled_tag(target, NOTE)
    assert second != first
    assert status(target, "PUT", NOTE, b"333", If_Match=first) == 412
    assert status(target, "DELETE", NOTE, If_Match=first) == 412
    assert status(target, "DELETE", NOTE, If_Match=second) == 204
    assert status(target, "GET", NOTE) == 404


def test_two_writes_of_one_size_within_a_second_do_not_keep_a_tag(target: WebDav) -> None:
    target.put(NOTE, b"aaaa", Absent())
    first = target.get(NOTE).tag
    status(target, "PUT", NOTE, b"bbbb")
    assert target.get(NOTE).tag != first


def test_written_as_read_a_file_deleted_since_is_refused(target: WebDav) -> None:
    target.put(NOTE, b"1", Absent())
    read = settled_tag(target, NOTE)
    assert status(target, "DELETE", NOTE, If_Match=read) == 204

    assert status(target, "PUT", NOTE, b"22", If_Match=read) == 412
    assert status(target, "GET", NOTE) == 404
    assert not SYNOLOGY.makes_gone_as_read


def test_what_is_not_there_is_not_found(target: WebDav) -> None:
    assert status(target, "GET", NOTE) == 404
    assert status(target, "DELETE", NOTE, If_Match='"0"') == 404


def test_a_folder_lists_its_files_and_the_tags_a_read_gives(target: WebDav) -> None:
    target.put(NOTE, b"12345", Absent())
    listed = {entry.path: entry for entry in target.list("notes")}
    assert listed[NOTE].size == 5
    assert listed[NOTE].tag == target.get(NOTE).tag


def test_devices_keep_a_note_through_it_and_lose_neither_text(tmp_path: Path) -> None:
    with folder_of_its_own() as first:
        vault = str(uuid.uuid4())
        clock = Clock()

        def pc(name: str, target: WebDav) -> Pc:
            db = open_store(tmp_path / f"{name}-{secrets.token_hex(2)}.db", KEY)
            place = Place(target, vault, KEYS, device_id(name))
            return Pc(db, NoteRepository(db), place, target, clock)

        laptop = pc("aaaaaaaa", first)
        second = another_way_in(first)
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
            time.sleep(1.2)  # past the second in which a tag is weak
            laptop.sync()
            desktop.sync()

        assert laptop.texts() == desktop.texts()
        assert {"the laptop's", "the desktop's"} <= set(laptop.texts())
        second.close()
        laptop.db.close()
        desktop.db.close()
