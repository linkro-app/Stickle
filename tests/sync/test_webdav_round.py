"""Devices syncing through a real WebDAV server, over real HTTP, as the app will
run them; and what they sign in with staying on the device: not in any file or
file name on the server, an export, a feedback report or a log."""

import logging
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import apsw
import pytest
from sync_bed import Clock, device_id
from test_vault_rules import plain_in
from webdav_server import HOST, Server, webdav

from stickle.crypto.vault_keys import DataKeys
from stickle.data import sync_connection
from stickle.data.export import export_all
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.sync_connection import WEBDAV, Login
from stickle.sync import diagnostics
from stickle.sync.devices import About
from stickle.sync.engine import Place, sync_vault
from stickle.sync.format import VAULT_FILE, VaultInfo, vault_file
from stickle.sync.targets.base import Absent
from stickle.sync.targets.webdav import WebDav, parse_address

KEY = secrets.token_bytes(32)
KEYS = DataKeys({1: secrets.token_bytes(32)})
VAULT = str(uuid.uuid4())
USER = "someone-signing-in"
PASSWORD = "the-password 비밀번호"
SHARE = "a-folder-of-theirs"
WHAT_IS_SECRET = {
    "password": PASSWORD,
    "user": USER,
    "address": SHARE,
    "data key": KEYS.keys[1].hex(),
}


@dataclass
class Pc:
    db: apsw.Connection
    notes: NoteRepository
    place: Place
    target: WebDav
    clock: Clock

    def sync(self) -> None:
        about = About("A PC", "Linux", "0.7.0")
        sync_vault(self.db, self.place, self.clock, self.notes, about=about)

    def texts(self) -> list[str]:
        return sorted(note.body for note in self.notes.all())


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "dav"
    (folder / SHARE).mkdir(parents=True)
    return folder


@pytest.fixture
def server(root: Path) -> Iterator[Server]:
    with webdav(root, PASSWORD, user=USER) as running:
        yield running


def a_pc(name: str, folder: Path, server: Server, clock: Clock) -> Pc:
    db = open_store(folder / f"{name}.db", KEY)
    # As the app will: told nothing of what this server keeps to.
    target = WebDav(parse_address(f"http://{HOST}:{server.port}/{SHARE}"), USER, PASSWORD)
    place = Place(target, VAULT, KEYS, device_id(name))
    return Pc(db, NoteRepository(db), place, target, clock)


@pytest.fixture
def pcs(tmp_path: Path, server: Server) -> Iterator[tuple[Pc, Pc]]:
    clock = Clock()
    made = a_pc("aaaaaaaa", tmp_path, server, clock), a_pc("bbbbbbbb", tmp_path, server, clock)
    made[0].target.put(VAULT_FILE, vault_file(VaultInfo(VAULT)), Absent())
    yield made
    for pc in made:
        pc.target.close()
        pc.db.close()


def on_the_server(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_a_note_goes_to_the_other_device_and_a_change_comes_back(pcs: tuple[Pc, Pc]) -> None:
    laptop, desktop = pcs
    note = laptop.notes.create("written on the laptop")

    laptop.sync()
    laptop.clock.tick()
    desktop.sync()
    assert desktop.texts() == ["written on the laptop"]

    desktop.clock.tick(10_000)
    desktop.notes.update_body(note.id, "changed on the desktop")
    desktop.sync()
    laptop.clock.tick(10_000)
    laptop.sync()

    assert laptop.texts() == ["changed on the desktop"]


def test_two_devices_changing_one_note_keep_both_texts(pcs: tuple[Pc, Pc]) -> None:
    laptop, desktop = pcs
    note = laptop.notes.create("first")
    laptop.sync()
    desktop.sync()

    laptop.clock.tick(5_000)
    laptop.notes.update_body(note.id, "the laptop's")
    desktop.clock.tick(5_000)
    desktop.notes.update_body(note.id, "the desktop's")
    for _ in range(3):
        laptop.clock.tick(5_000)
        laptop.sync()
        desktop.sync()

    assert laptop.texts() == desktop.texts()
    assert {"the laptop's", "the desktop's"} <= set(laptop.texts())


def test_nothing_it_signs_in_with_is_in_the_vault(pcs: tuple[Pc, Pc], root: Path) -> None:
    laptop, desktop = pcs
    laptop.notes.create("a note")
    laptop.sync()
    desktop.sync()

    files = on_the_server(root)
    assert any(path.endswith(VAULT_FILE) for path in files)
    # The server's own folder name is in each path; what is inside it is what counts.
    inside = {path.removeprefix(f"{SHARE}/"): data for path, data in files.items()}
    assert plain_in(inside, WHAT_IS_SECRET) == []


def a_login() -> Login:
    return Login(VAULT, WEBDAV, f"https://nas.example.com/{SHARE}", USER, PASSWORD, KEYS.keys)


def test_a_login_is_kept_as_given_and_one_at_a_time(tmp_path: Path) -> None:
    db = open_store(tmp_path / "a.db", KEY)
    assert sync_connection.current(db) is None

    sync_connection.keep(db, a_login())
    assert sync_connection.current(db) == a_login()

    other = Login("another", WEBDAV, "https://b.example.com", "u", "p", {2: bytes(32)}, b"der")
    sync_connection.keep(db, other)
    assert sync_connection.current(db) == other

    sync_connection.no_longer_trusted(db)
    now = sync_connection.current(db)
    assert now is not None and now.certificate is None and now.secret == "p"

    sync_connection.forget(db)
    assert sync_connection.current(db) is None
    db.close()


def test_a_login_written_down_says_nothing_of_itself() -> None:
    written = repr(a_login())
    assert plain_in({"repr": written.encode()}, {**WHAT_IS_SECRET, "key": KEYS.keys[1].hex()}) == []


def test_a_login_is_not_in_an_export(tmp_path: Path) -> None:
    db = open_store(tmp_path / "a.db", KEY)
    NoteRepository(db).create("a note")
    sync_connection.keep(db, a_login())

    folder = export_all(db, tmp_path, "0.7.0", clock=lambda: "2026-11-09T00:00:00Z").folder

    files = {p.name: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    assert files
    assert plain_in(files, WHAT_IS_SECRET) == []
    db.close()


def test_a_login_is_not_in_a_feedback_report_or_a_log(
    pcs: tuple[Pc, Pc], caplog: pytest.LogCaptureFixture
) -> None:
    laptop, _desktop = pcs
    sync_connection.keep(laptop.db, a_login())
    laptop.notes.create("a note")

    with caplog.at_level(logging.DEBUG):
        laptop.sync()
        lines = diagnostics.lines(laptop.db, laptop.place)

    logged = [each.getMessage() for each in caplog.records if each.name.startswith("stickle")]
    assert plain_in({"report": "\n".join(lines).encode()}, WHAT_IS_SECRET) == []
    assert plain_in({"log": "\n".join(logged).encode()}, WHAT_IS_SECRET) == []
