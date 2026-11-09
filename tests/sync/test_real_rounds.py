"""Rounds as the app runs them, over real HTTP: the way to the server made from
what this device keeps, with no test double in between."""

import secrets
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from real_server import account, account_given, folder_of_its_own
from webdav_server import HOST, Server, webdav

from stickle.app.syncing import Ended, _Rounds  # pyright: ignore[reportPrivateUsage]
from stickle.core.cloud_time import CloudTime
from stickle.crypto.vault_keys import DataKeys
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.sync_connection import WEBDAV, Login
from stickle.platform.clocks import since_start
from stickle.sync.format import VAULT_FILE, VaultInfo, vault_file
from stickle.sync.targets.base import Absent
from stickle.sync.targets.webdav import WebDav, parse_address

KEY = secrets.token_bytes(32)
KEYS = DataKeys({1: secrets.token_bytes(32)})
USER = "someone-signing-in"
PASSWORD = "the-password 비밀번호"
SHARE = "a-folder-of-theirs"


def a_pc(folder: Path, name: str, login: Login) -> _Rounds:
    store = folder / f"{name}.db"
    open_store(store, KEY).close()
    return _Rounds(store, KEY, login, CloudTime(since_start))


def write(rounds: _Rounds, text: str) -> str:
    db = open_store(rounds.store, KEY)
    note = NoteRepository(db).create(text)
    db.close()
    return note.id


def text_of(rounds: _Rounds, note_id: str) -> str | None:
    db = open_store(rounds.store, KEY)
    note = NoteRepository(db).get(note_id)
    db.close()
    return None if note is None else note.body


def through(ended: Ended) -> Ended:
    assert ended.outcome.wait is None and ended.outcome.stopped is None, ended.outcome
    return ended


def goes_across(folder: Path, login: Login, settle: float = 0.0) -> None:
    """settle: a server may turn down writing over a file written in the last second."""
    laptop, desktop = a_pc(folder, "laptop", login), a_pc(folder, "desktop", login)
    try:
        note_id = write(laptop, "written on the laptop")
        assert note_id in through(laptop(True)).report.put
        assert note_id in through(desktop(True)).report.changed
        assert text_of(desktop, note_id) == "written on the laptop"
        time.sleep(settle)
        # Back again, this time as the rounds between the full looks go.
        db = open_store(desktop.store, KEY)
        NoteRepository(db).update_body(note_id, "changed on the desktop")
        db.close()
        assert note_id in through(desktop(False)).report.put
        assert note_id in through(laptop(False)).report.changed
        assert text_of(laptop, note_id) == "changed on the desktop"
    finally:
        laptop.close()
        desktop.close()


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    root = tmp_path / "dav"
    (root / SHARE).mkdir(parents=True)
    with webdav(root, PASSWORD, user=USER) as running:
        yield running


def a_vault(address: str) -> str:
    vault = str(uuid.uuid4())
    target = WebDav(parse_address(address), USER, PASSWORD)
    target.put(VAULT_FILE, vault_file(VaultInfo(vault)), Absent())
    target.close()
    return vault


def test_a_note_goes_across_through_a_webdav_server(tmp_path: Path, server: Server) -> None:
    address = f"http://{HOST}:{server.port}/{SHARE}"
    login = Login(a_vault(address), WEBDAV, address, USER, PASSWORD, KEYS.keys)
    goes_across(tmp_path, login)


def test_the_cloud_clock_is_heard_from_the_server(tmp_path: Path, server: Server) -> None:
    address = f"http://{HOST}:{server.port}/{SHARE}"
    login = Login(a_vault(address), WEBDAV, address, USER, PASSWORD, KEYS.keys)
    pc = a_pc(tmp_path, "laptop", login)
    try:
        through(pc(True))
        # The server is on this computer: no difference to speak of, but heard.
        assert abs(pc.cloud.offset) < 5_000
        assert pc.cloud._samples  # pyright: ignore[reportPrivateUsage]
    finally:
        pc.close()


def test_a_wrong_password_stops_it(tmp_path: Path, server: Server) -> None:
    address = f"http://{HOST}:{server.port}/{SHARE}"
    login = Login(str(uuid.uuid4()), WEBDAV, address, USER, "not the password", KEYS.keys)
    pc = a_pc(tmp_path, "laptop", login)
    try:
        ended = pc(True)
        assert ended.stopped
        assert ended.outcome.stopped is not None
        assert ended.outcome.stopped.value == "sign in"
    finally:
        pc.close()


@pytest.mark.skipif(not account_given(), reason="no real server given")
def test_a_note_goes_across_through_the_real_server(tmp_path: Path) -> None:
    given = account()
    assert given is not None
    with folder_of_its_own() as target:
        vault = str(uuid.uuid4())
        target.put(VAULT_FILE, vault_file(VaultInfo(vault)), Absent())
        inside = target._address  # pyright: ignore[reportPrivateUsage]
        scheme = "https" if inside.secure else "http"
        port = "" if inside.port is None else f":{inside.port}"
        address = f"{scheme}://{inside.host}{port}{inside.path}"
        login = Login(vault, WEBDAV, address, given.user, given.password, KEYS.keys)
        goes_across(tmp_path, login, settle=1.2)
