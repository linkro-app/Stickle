"""Devices syncing through Graph in the test, over real HTTPS, as the app will
run them; and what they sign in with staying on the device: not in any file on
OneDrive, a feedback report or a log."""

import logging
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import apsw
import pytest
from graph_server import CLIENT_ID, FIRST_REFRESH, Graph, graph_server
from sync_bed import Clock, device_id
from test_vault_rules import plain_in

from stickle.crypto.vault_keys import DataKeys
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.sync import diagnostics
from stickle.sync.devices import About
from stickle.sync.engine import Place, sync_vault
from stickle.sync.format import VAULT_FILE, VaultInfo, vault_file
from stickle.sync.targets.base import Absent
from stickle.sync.targets.onedrive import OneDrive

KEY = secrets.token_bytes(32)
KEYS = DataKeys({1: secrets.token_bytes(32)})
VAULT = str(uuid.uuid4())


@dataclass
class Pc:
    db: apsw.Connection
    notes: NoteRepository
    place: Place
    target: OneDrive
    clock: Clock
    renewed: list[str] = field(default_factory=list[str])

    def sync(self) -> None:
        about = About("A PC", "Windows", "0.7.0")
        sync_vault(self.db, self.place, self.clock, self.notes, about=about)

    def texts(self) -> list[str]:
        return sorted(note.body for note in self.notes.all())


@pytest.fixture
def graph() -> Iterator[Graph]:
    with graph_server() as running:
        yield running


def a_pc(name: str, folder: Path, graph: Graph, clock: Clock) -> Pc:
    db = open_store(folder / f"{name}.db", KEY)
    renewed: list[str] = []
    target = OneDrive(
        CLIENT_ID, FIRST_REFRESH, renewed.append, place=graph.place, public=graph.context
    )
    place = Place(target, VAULT, KEYS, device_id(name))
    return Pc(db, NoteRepository(db), place, target, clock, renewed)


@pytest.fixture
def pcs(tmp_path: Path, graph: Graph) -> Iterator[tuple[Pc, Pc]]:
    clock = Clock()
    made = a_pc("aaaaaaaa", tmp_path, graph, clock), a_pc("bbbbbbbb", tmp_path, graph, clock)
    made[0].target.put(VAULT_FILE, vault_file(VaultInfo(VAULT)), Absent())
    yield made
    for pc in made:
        pc.target.close()
        pc.db.close()


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


def test_an_access_token_run_out_between_rounds_is_made_again(
    pcs: tuple[Pc, Pc], graph: Graph
) -> None:
    laptop, desktop = pcs
    laptop.notes.create("before")
    laptop.sync()
    graph.expire_access_tokens()

    laptop.notes.create("after")
    laptop.sync()
    desktop.sync()

    assert desktop.texts() == ["after", "before"]
    assert len(set(laptop.renewed)) == 2  # each new refresh token told


def test_nothing_it_signs_in_with_is_on_onedrive_or_in_a_report_or_a_log(
    pcs: tuple[Pc, Pc], graph: Graph, caplog: pytest.LogCaptureFixture
) -> None:
    laptop, desktop = pcs
    laptop.notes.create("a note")

    with caplog.at_level(logging.DEBUG):
        laptop.sync()
        desktop.sync()
        lines = diagnostics.lines(laptop.db, laptop.place)

    secret = {
        "client": CLIENT_ID,
        "first refresh token": FIRST_REFRESH,
        **{f"refresh token {n}": token for n, token in enumerate(graph.refresh_tokens)},
        **{f"access token {n}": token for n, token in enumerate(graph.issued)},
        "data key": KEYS.keys[1].hex(),
    }
    files = {path: stored.data for path, stored in graph.files.items()}
    assert any(path == VAULT_FILE for path in files)
    assert plain_in(files, secret) == []
    logged = [each.getMessage() for each in caplog.records if each.name.startswith("stickle")]
    assert plain_in({"report": "\n".join(lines).encode()}, secret) == []
    assert plain_in({"log": "\n".join(logged).encode()}, secret) == []
