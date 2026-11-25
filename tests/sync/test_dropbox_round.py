"""Devices syncing through Dropbox in the test, over real HTTPS, as the app will
run them: their rounds, the watcher that brings a round forward when another
device changed something, and what they sign in with staying on the device."""

import logging
import secrets
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import apsw
import pytest
from dropbox_server import API, APP_KEY, FIRST_REFRESH, NOTIFY, Answer, DropboxState, dropbox_server
from sync_bed import Clock, device_id
from test_vault_rules import plain_in

from stickle.crypto.vault_keys import DataKeys
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.sync import diagnostics
from stickle.sync.devices import About
from stickle.sync.engine import Place, sync_vault
from stickle.sync.format import DEVICES, VAULT_FILE, VaultInfo, vault_file
from stickle.sync.runner import Runner
from stickle.sync.schedule import Schedule
from stickle.sync.targets.base import Absent
from stickle.sync.targets.dropbox import Dropbox
from stickle.sync.watcher import SAFETY_NET, Watcher, rounds_every

KEY = secrets.token_bytes(32)
KEYS = DataKeys({1: secrets.token_bytes(32)})
VAULT = str(uuid.uuid4())
SOON = 5.0  # seconds a test waits for what should come in seconds, at most


@dataclass
class Pc:
    name: str
    db: apsw.Connection
    notes: NoteRepository
    place: Place
    target: Dropbox
    clock: Clock
    renewed: list[str] = field(default_factory=list[str])

    def sync(self) -> None:
        about = About("A PC", "Windows", "0.7.0")
        sync_vault(self.db, self.place, self.clock, self.notes, about=about)

    def texts(self) -> list[str]:
        return sorted(note.body for note in self.notes.all())

    @property
    def own_file(self) -> str:
        return f"{DEVICES}/{device_id(self.name)}.json"


@dataclass
class Told:
    """A watcher's word, as the test hears it."""

    changes: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(0))
    working: list[bool] = field(default_factory=list[bool])
    said: threading.Event = field(default_factory=threading.Event)

    def changed(self) -> None:
        self.changes.release()

    def work(self, working: bool) -> None:
        self.working.append(working)
        self.said.set()

    def told(self, seconds: float = SOON) -> bool:
        return self.changes.acquire(timeout=seconds)


@pytest.fixture
def box() -> Iterator[DropboxState]:
    with dropbox_server() as running:
        running.hold = 0.5
        yield running


def a_pc(name: str, folder: Path, box: DropboxState, clock: Clock) -> Pc:
    db = open_store(folder / f"{name}.db", KEY)
    renewed: list[str] = []
    target = Dropbox(APP_KEY, FIRST_REFRESH, renewed.append, place=box.place, public=box.context)
    place = Place(target, VAULT, KEYS, device_id(name))
    return Pc(name, db, NoteRepository(db), place, target, clock, renewed)


@pytest.fixture
def pcs(tmp_path: Path, box: DropboxState) -> Iterator[tuple[Pc, Pc]]:
    clock = Clock()
    made = a_pc("aaaaaaaa", tmp_path, box, clock), a_pc("bbbbbbbb", tmp_path, box, clock)
    made[0].target.put(VAULT_FILE, vault_file(VaultInfo(VAULT)), Absent())
    yield made
    for pc in made:
        pc.target.close()
        pc.db.close()


@pytest.fixture
def watchers() -> Iterator[list[Watcher]]:
    made: list[Watcher] = []
    yield made
    for watcher in made:
        assert watcher.stop(SOON)


def watching(pc: Pc, box: DropboxState, watchers: list[Watcher]) -> Told:
    """pc's watcher on devices/, started, its first wait under way: what it
    told of taking its cursor heard (a round looks, whatever changed before)."""
    told = Told()
    waits = len(box.requests(NOTIFY))
    watcher = Watcher(pc.target.feed(DEVICES), told.changed, told.work, first_pause=0.05)
    watchers.append(watcher)
    watcher.start()
    for _ in range(int(SOON / 0.02)):
        if len(box.requests(NOTIFY)) > waits:
            assert told.told()
            return told
        time.sleep(0.02)
    raise AssertionError("it never waited")


# Rounds


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
    pcs: tuple[Pc, Pc], box: DropboxState
) -> None:
    laptop, desktop = pcs
    laptop.notes.create("before")
    laptop.sync()
    box.expire_access_tokens()

    laptop.notes.create("after")
    laptop.sync()
    desktop.sync()

    assert desktop.texts() == ["after", "before"]
    assert laptop.renewed == []  # Dropbox's refresh token stays as it was


def test_nothing_it_signs_in_with_is_on_dropbox_or_in_a_report_or_a_log(
    pcs: tuple[Pc, Pc], box: DropboxState, caplog: pytest.LogCaptureFixture
) -> None:
    laptop, desktop = pcs
    laptop.notes.create("a note")

    with caplog.at_level(logging.DEBUG):
        laptop.sync()
        desktop.sync()
        lines = diagnostics.lines(laptop.db, laptop.place)

    secret = {
        "app key": APP_KEY,
        "refresh token": FIRST_REFRESH,
        **{f"access token {n}": token for n, token in enumerate(box.issued)},
        "data key": KEYS.keys[1].hex(),
    }
    files = {path: stored.data for path, stored in box.files.items()}
    assert any(path == VAULT_FILE for path in files)
    assert plain_in(files, secret) == []
    logged = [each.getMessage() for each in caplog.records if each.name.startswith("stickle")]
    assert plain_in({"report": "\n".join(lines).encode()}, secret) == []
    assert plain_in({"log": "\n".join(logged).encode()}, secret) == []


# The watcher


def test_a_change_on_one_wakes_the_other_within_seconds(
    pcs: tuple[Pc, Pc], box: DropboxState, watchers: list[Watcher]
) -> None:
    laptop, desktop = pcs
    # Each device's first round in the vault: it writes its file twice in it (made,
    # then saying what it put), so the other may look twice then.
    laptop.sync()
    desktop.sync()
    told = watching(desktop, box, watchers)

    laptop.notes.create("written on the laptop")
    laptop.sync()

    assert told.told()
    desktop.sync()
    assert desktop.texts() == ["written on the laptop"]
    assert not told.told(1.0)  # once: its own round, reading, wrote nothing


def test_its_own_round_does_not_wake_it(
    pcs: tuple[Pc, Pc], box: DropboxState, watchers: list[Watcher]
) -> None:
    laptop, desktop = pcs
    desktop.sync()
    told = watching(laptop, box, watchers)
    looked = len(box.requests(API, "/2/files/list_folder/continue"))

    laptop.notes.create("written on the laptop")
    box.answer_late = 0.2  # Dropbox tells of each write before its answer is in
    laptop.sync()  # writes its own file in devices/

    for _ in range(int(SOON / 0.05)):
        if len(box.requests(API, "/2/files/list_folder/continue")) > looked:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the change was never looked at")
    assert laptop.own_file in box.files
    assert not told.told(1.0)


def test_notes_put_alone_wake_nobody_until_the_device_says_so(
    pcs: tuple[Pc, Pc], box: DropboxState, watchers: list[Watcher]
) -> None:
    laptop, desktop = pcs
    laptop.sync()
    desktop.sync()
    told = watching(desktop, box, watchers)

    for number in range(20):  # as the laptop puts its notes, its round not over yet
        box.put_by_hand(f"notes/{number:08}.json", b"a note")
    assert not told.told(1.0)

    box.put_by_hand(laptop.own_file, b"what the laptop put")  # the round's end
    assert told.told()
    assert not told.told(1.0)


def test_its_folder_deleted_wakes_it(
    pcs: tuple[Pc, Pc], box: DropboxState, watchers: list[Watcher]
) -> None:
    laptop, _desktop = pcs
    laptop.sync()
    told = watching(laptop, box, watchers)

    box.delete_by_hand("")  # the user deleted the Stickle folder
    assert told.told()


def test_a_cursor_too_old_is_taken_anew_and_a_round_looks(
    pcs: tuple[Pc, Pc], box: DropboxState, watchers: list[Watcher]
) -> None:
    laptop, _desktop = pcs
    laptop.sync()
    told = watching(laptop, box, watchers)
    latest = len(box.requests(API, "/2/files/list_folder/get_latest_cursor"))

    box.expire_cursors()

    assert told.told()
    assert len(box.requests(API, "/2/files/list_folder/get_latest_cursor")) == latest + 1


def test_waiting_that_fails_comes_back(
    pcs: tuple[Pc, Pc], box: DropboxState, watchers: list[Watcher]
) -> None:
    laptop, _desktop = pcs
    laptop.sync()
    box.notify_answers += [Answer(500, body=b"down"), Answer(503, body=b"down")]
    told = Told()
    watcher = Watcher(laptop.target.feed(DEVICES), told.changed, told.work, first_pause=0.05)
    watchers.append(watcher)
    watcher.start()

    for _ in range(int(SOON / 0.05)):
        if told.working[-1:] == [True]:
            break
        time.sleep(0.05)

    assert told.working == [False, True]
    assert [rounds_every(each) for each in told.working] == [5 * 60, SAFETY_NET]


def test_stopping_cuts_the_wait_and_nothing_more_goes(
    pcs: tuple[Pc, Pc], box: DropboxState, watchers: list[Watcher]
) -> None:
    laptop, _desktop = pcs
    laptop.sync()
    box.hold = 30.0
    watching(laptop, box, watchers)

    began = time.monotonic()
    assert watchers[0].stop(SOON)
    asked = len(box.asked)

    assert time.monotonic() - began < 1
    box.put_by_hand("devices/someone.json", b"d")
    time.sleep(0.5)
    assert len(box.asked) == asked


def test_a_change_brings_the_other_ones_round_forward(
    pcs: tuple[Pc, Pc], box: DropboxState, watchers: list[Watcher]
) -> None:
    """As the app runs it: the watcher's word goes to the rounds' thread, and
    while it waits as it should, rounds look for others' changes once an hour."""
    laptop, desktop = pcs
    laptop.sync()
    desktop.sync()

    @dataclass(frozen=True)
    class Turned:
        wait: float | None = None
        stopped: bool = False

    rounds = threading.Semaphore(0)

    def work(_full: bool) -> Turned:
        desktop.sync()
        rounds.release()
        return Turned()

    runner = Runner(
        Schedule(time.monotonic(), rounds_every(False), start_wait=3600.0),
        work,
        lambda _turn: None,
        time.monotonic,
    )
    told = Told()
    watcher = Watcher(
        desktop.target.feed(DEVICES),
        runner.changed_elsewhere,
        lambda working: (told.work(working), runner.set_every(rounds_every(working))),
        first_pause=0.05,
    )
    watchers.append(watcher)
    runner.start()
    watcher.start()
    try:
        assert told.said.wait(SOON) and told.working == [True]
        assert rounds.acquire(timeout=SOON)  # the one the cursor asked for

        laptop.notes.create("written on the laptop")
        laptop.sync()

        assert rounds.acquire(timeout=SOON)
        assert desktop.texts() == ["written on the laptop"]
    finally:
        runner.finish()
