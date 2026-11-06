"""How the WebDAV server tried answers, over real HTTP: the fake WebDAV of
sync's tests answers the same (fake_cloud.WEBDAV). Its tags are strong, and
its conditions kept: a file is made only if none is there, written over or
deleted only if still as read. But a tag is made from the second the file was
written and its size, so two writes of one size within a second keep it.

Checked again on each server tried later (the NAS, others): what differs goes
into the fake as another kind."""

import base64
import re
import ssl
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import trustme
from fake_cloud import WEBDAV
from webdav_server import Server, webdav

FOLDER = "/Stickle/notes"
NOTE = f"{FOLDER}/0f8e6f3c-1d9b-4a63-9c38-3b2a4f2f7d10.json"


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    root = tmp_path / "dav"
    (root / "Stickle" / "notes").mkdir(parents=True)
    with webdav(root) as running:
        yield running


def ask(
    server: Server, method: str, path: str, body: bytes | None = None, **headers: str
) -> tuple[int, dict[str, str], bytes]:
    connection = server.connection()
    try:
        connection.request(
            method, path, body=body, headers={k.replace("_", "-"): v for k, v in headers.items()}
        )
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        connection.close()


def tag(server: Server, path: str) -> str:
    status, headers, _body = ask(server, "GET", path)
    assert status == 200
    return headers["etag"]


def test_a_file_is_made_only_if_none_is_there(server: Server) -> None:
    assert ask(server, "PUT", NOTE, b"1", If_None_Match="*")[0] == 201
    assert ask(server, "PUT", NOTE, b"22", If_None_Match="*")[0] == 412
    assert ask(server, "GET", NOTE)[2] == b"1"


def test_its_tags_are_strong_and_held_to_when_written_over_or_deleted(server: Server) -> None:
    ask(server, "PUT", NOTE, b"1", If_None_Match="*")
    first = tag(server, NOTE)
    assert not first.startswith("W/")

    assert ask(server, "PUT", NOTE, b"22", If_Match='"not-its-tag"')[0] == 412
    assert ask(server, "PUT", NOTE, b"22", If_Match=first)[0] in (200, 201, 204)
    second = tag(server, NOTE)
    assert second != first
    assert ask(server, "PUT", NOTE, b"333", If_Match=first)[0] == 412  # read before "22"
    assert ask(server, "DELETE", NOTE, If_Match=first)[0] == 412
    assert ask(server, "DELETE", NOTE, If_Match=second)[0] == 204
    assert ask(server, "GET", NOTE)[0] == 404
    assert WEBDAV.keeps_conditions


def test_written_as_read_a_file_deleted_since_is_made_again(server: Server) -> None:
    # The standard says 412; wsgidav makes it. So does the fake WebDAV.
    ask(server, "PUT", NOTE, b"1", If_None_Match="*")
    read = tag(server, NOTE)
    ask(server, "DELETE", NOTE, If_Match=read)

    assert ask(server, "PUT", NOTE, b"22", If_Match=read)[0] == 201
    assert ask(server, "GET", NOTE)[2] == b"22"
    assert WEBDAV.makes_gone_as_read


def test_two_writes_of_one_size_within_a_second_keep_its_tag(server: Server) -> None:
    for _ in range(5):  # until both writes fall within one second
        ask(server, "PUT", NOTE, b"aaaa")
        first = tag(server, NOTE)
        ask(server, "PUT", NOTE, b"bbbb")
        second = tag(server, NOTE)
        if first.split("-")[1:] == second.split("-")[1:]:
            break
        time.sleep(0.2)
    else:
        raise AssertionError("never two writes within one second")

    assert second == first and ask(server, "GET", NOTE)[2] == b"bbbb"
    # The condition cannot tell the change: as the fake WebDAV's "seconds" tags.
    assert ask(server, "PUT", NOTE, b"cccc", If_Match=first)[0] in (200, 201, 204)
    assert WEBDAV.tags == "seconds"


def test_a_folder_lists_its_files_and_their_tags(server: Server) -> None:
    for name, data in (("a.json", b"1"), ("b.json", b"22")):
        ask(server, "PUT", f"{FOLDER}/{name}", data, If_None_Match="*")

    status, _headers, body = ask(server, "PROPFIND", f"{FOLDER}/", Depth="1")

    assert status == 207
    text = body.decode()
    for name in ("a.json", "b.json"):
        assert f"{FOLDER}/{name}" in text
        assert tag(server, f"{FOLDER}/{name}").strip('"') in text
    assert len(re.findall(r"getetag", text, re.IGNORECASE)) >= 2


def test_a_wrong_password_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "dav"
    root.mkdir()
    with webdav(root, password="right one") as running:
        wrong = "Basic " + base64.b64encode(b"stickle:wrong one").decode()
        right = "Basic " + base64.b64encode(b"stickle:right one").decode()
        assert ask(running, "PROPFIND", "/", Depth="0", Authorization=wrong)[0] == 401
        assert ask(running, "PROPFIND", "/", Depth="0", Authorization=right)[0] == 207


def test_over_tls_a_test_certificate_is_trusted_only_where_it_is_told(tmp_path: Path) -> None:
    root = tmp_path / "dav"
    root.mkdir()
    with webdav(root, ca=trustme.CA()) as running:
        assert ask(running, "PROPFIND", "/", Depth="0")[0] == 207  # its own CA trusted
        connection = running.connection(ssl.create_default_context())
        with pytest.raises(ssl.SSLCertVerificationError):
            connection.request("PROPFIND", "/", headers={"Depth": "0"})
        connection.close()
