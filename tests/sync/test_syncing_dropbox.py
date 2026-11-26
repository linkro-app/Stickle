"""Sync with Dropbox alongside the app: its long poll watched beside the rounds,
from the first round to the end, and Stickle signed out of the account as sync
is left; against a Dropbox of the test's own (dropbox_server)."""

# pyright: reportPrivateUsage=false
import gc
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import apsw
import pytest
from dropbox_server import (
    ACCOUNT,
    APP_KEY,
    FIRST_REFRESH,
    NOTIFY,
    Answer,
    DropboxState,
    dropbox_server,
)
from PySide6.QtCore import QCoreApplication, QEvent
from pytestqt.qtbot import QtBot
from sync_bed import KEY

from stickle.app.notes import NoteManager
from stickle.app.syncing import Ended, Syncing, _Rounds, cloud_time
from stickle.core.clock import Clocks
from stickle.core.cloud_time import CloudTime
from stickle.crypto import master, recovery
from stickle.crypto.sealed import make_sealed_slot
from stickle.crypto.vault_keys import MASTER_PASSWORD, RECOVERY_KEY
from stickle.data import sync_connection
from stickle.data.colors import ColorRepository
from stickle.data.labels import LabelRepository
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import SYNC_EVERY, Settings
from stickle.data.sync_connection import DROPBOX, WEBDAV, Login
from stickle.data.versions import VersionRepository
from stickle.platform.clocks import since_start
from stickle.sync.format import DEVICES, item_path
from stickle.sync.outcome import Stopped
from stickle.sync.schedule import EVERY
from stickle.sync.targets import dropbox
from stickle.sync.targets.dropbox import App
from stickle.sync.vault import Vault, make
from stickle.sync.watcher import SAFETY_NET

ROUND_MS = 15_000
SOON = 5.0  # seconds for what should come at once
HOUR = 3600.0
DATABASE_KEY = bytes([7]) * 32
PAIRS = {
    RECOVERY_KEY: recovery.make_recovery_slot(
        DATABASE_KEY, recovery.generate(), "2026-11-25T00:00:00Z"
    ),
    MASTER_PASSWORD: make_sealed_slot(DATABASE_KEY, "a spare", 1, 8192, master.SLOT, None),
}


def said(told: Any) -> object:
    return told.args[0]


def until(done: Callable[[], bool], seconds: float = SOON) -> bool:
    ends = time.monotonic() + seconds
    while time.monotonic() < ends:
        if done():
            return True
        time.sleep(0.02)
    return done()


@pytest.fixture(autouse=True)
def windows_deleted() -> Iterator[None]:
    yield
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.enable()
    gc.collect()


@pytest.fixture
def box() -> Iterator[DropboxState]:
    with dropbox_server() as serving:
        serving.hold = 0.5  # a long poll's wait, at most
        yield serving


def app_of(box: DropboxState) -> App:
    return App(APP_KEY, box.place, box.context)


def vault_there(box: DropboxState) -> Vault:
    target = app_of(box).target(FIRST_REFRESH, lambda _token: None)
    try:
        return make(target, PAIRS)
    finally:
        target.close()


@dataclass
class Pc:
    db: apsw.Connection
    syncing: Syncing
    qtbot: QtBot
    box: DropboxState

    def round(self) -> Ended:
        with self.qtbot.waitSignal(self.syncing.round_shown, timeout=ROUND_MS) as told:
            self.syncing.sync_now()
        ended = said(told)
        assert isinstance(ended, Ended)
        return ended

    def another_round(self) -> Ended:
        """One that comes by itself."""
        with self.qtbot.waitSignal(self.syncing.round_shown, timeout=ROUND_MS) as told:
            pass
        ended = said(told)
        assert isinstance(ended, Ended)
        return ended

    @property
    def every(self) -> float:
        return self.syncing.runner._schedule._every

    def watching(self) -> bool:
        return self.syncing._still_watching()

    def kept(self) -> Login:
        login = sync_connection.current(self.db)
        assert login is not None
        return login


def make_pc(qtbot: QtBot, tmp_path: Path, box: DropboxState, app: App | None) -> Pc:
    vault = vault_there(box)
    path = tmp_path / "a.db"
    db = open_store(path, KEY)
    Settings(db).set(SYNC_EVERY, 30)  # as set for another cloud: not Dropbox's
    login = Login(vault.vault, DROPBOX, "", ACCOUNT, FIRST_REFRESH, vault.keys.keys)
    sync_connection.keep(db, login)
    clocks = Clocks()
    manager = NoteManager(
        NoteRepository(db, clocks.stamp, clocks.millis),
        idle_ms=10,
        max_ms=50,
        settings=Settings(db),
        layouts=LayoutRepository(db, clocks.stamp, clocks.millis),
        labels=LabelRepository(db, clocks.stamp, clocks.millis),
        colors=ColorRepository(db, clocks.stamp, clocks.millis),
        versions=VersionRepository(db),
    )
    syncing = Syncing(
        db, path, KEY, login, cloud_time(db, clocks), manager, dropbox=app, start_wait=HOUR
    )
    syncing.start()
    return Pc(db, syncing, qtbot, box)


@pytest.fixture
def pc(qtbot: QtBot, tmp_path: Path, box: DropboxState) -> Iterator[Pc]:
    made = make_pc(qtbot, tmp_path, box, app_of(box))
    yield made
    made.syncing.finish(0.0)
    assert until(lambda: not made.watching())
    made.db.close()


def watched(pc: Pc) -> None:
    """Its first round through, the round its cursor asked for too, and its
    long poll answering."""
    ended = pc.round()
    assert ended.outcome.stopped is None and ended.outcome.wait is None
    pc.another_round()  # the cursor's
    assert until(lambda: pc.every == SAFETY_NET)


def test_rounds_look_every_few_minutes_until_the_long_poll_answers(pc: Pc) -> None:
    assert pc.every == EVERY  # not as the user set it for another cloud
    assert not pc.watching()  # not before the rounds reach Dropbox
    watched(pc)
    assert pc.watching()


def test_another_devices_change_brings_a_round_forward(pc: Pc, box: DropboxState) -> None:
    watched(pc)

    box.put_by_hand(item_path(DEVICES, "b" * 32), b"{}")  # as another device wrote it

    pc.another_round()  # within seconds, an hour before one is due


def test_its_own_rounds_do_not_wake_it(pc: Pc) -> None:
    watched(pc)
    pc.round()  # it writes its own device's file once a day: then as well
    with pc.qtbot.assertNotEmitted(pc.syncing.round_shown, wait=1500):
        pass


def test_woken_from_sleep_it_waits_anew_and_a_round_looks(pc: Pc, box: DropboxState) -> None:
    watched(pc)
    box.hold = 30.0  # the waits from now on: one under way when it wakes
    began = len(box.requests(NOTIFY))
    assert until(lambda: len(box.requests(NOTIFY)) > began)
    waits = len(box.requests(NOTIFY))
    with pc.qtbot.waitSignal(pc.syncing.round_shown, timeout=ROUND_MS):
        pc.syncing.woke()
    assert until(lambda: len(box.requests(NOTIFY)) > waits, 2.0)  # not half a minute on


def test_quitting_cuts_the_long_poll_and_asks_nothing_more(
    qtbot: QtBot, tmp_path: Path, box: DropboxState
) -> None:
    box.hold = 30.0  # a wait under way when the app ends
    made = make_pc(qtbot, tmp_path, box, app_of(box))
    try:
        made.round()
        made.another_round()
        assert until(lambda: bool(box.requests(NOTIFY)))  # waiting, for half a minute
        began = time.monotonic()
        made.syncing.finish(0.0)
        assert until(lambda: not made.watching(), 1.0)
        assert time.monotonic() - began < 1.5
        asked = len(box.asked)
        time.sleep(0.5)
        assert len(box.asked) == asked
    finally:
        made.db.close()


def test_garbage_is_collected_again_once_both_threads_are_over(pc: Pc) -> None:
    watched(pc)
    pc.syncing.finish(0.0)
    assert until(lambda: not pc.watching() and not pc.syncing.runner.running)
    pc.syncing.left()  # as after a disconnect: looked at again until both are over
    assert gc.isenabled()


def test_leaving_takes_the_device_off_then_signs_out(pc: Pc, box: DropboxState) -> None:
    watched(pc)
    device = Settings(pc.db).device_id()
    assert item_path(DEVICES, device) in box.files
    pc.syncing.leaving()
    pc.syncing.leave()
    assert item_path(DEVICES, device) not in box.files
    assert box.revoked == 1 and FIRST_REFRESH not in box.refresh_tokens
    assert until(lambda: not pc.watching())  # cut, not waited for: left() looks again
    pc.syncing.left()


def test_leaving_with_dropbox_out_of_reach_ends_all_the_same(pc: Pc, box: DropboxState) -> None:
    watched(pc)
    pc.syncing.leaving()
    box.answers += [Answer(500, body=b"down") for _ in range(8)]
    pc.syncing.leave()  # nothing raised
    assert box.revoked == 0


def test_signed_out_of_dropbox_it_stops_watching(pc: Pc, box: DropboxState) -> None:
    watched(pc)
    box.refusing_refresh = True
    box.expire_access_tokens()
    ended = pc.round()
    assert ended.outcome.stopped is Stopped.SIGN_IN
    assert until(lambda: not pc.watching())
    assert pc.every == EVERY


def test_the_interval_set_for_other_clouds_is_kept_but_not_used(pc: Pc) -> None:
    pc.syncing.set_looks_every(15 * 60)
    assert Settings(pc.db).get(SYNC_EVERY) == 15 * 60
    assert pc.every == EVERY


def test_a_build_without_stickle_registered_tries_again_later(
    qtbot: QtBot, tmp_path: Path, box: DropboxState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dropbox, "registered", lambda: None)
    made = make_pc(qtbot, tmp_path, box, None)
    try:
        ended = made.round()
        assert ended.outcome.stopped is None and ended.outcome.wait is not None
        assert made.kept().stopped is None and not made.watching()
    finally:
        made.syncing.finish(0.0)
        made.db.close()


def test_a_token_renewed_on_another_thread_is_kept_by_the_rounds(
    tmp_path: Path, box: DropboxState
) -> None:
    path = tmp_path / "a.db"
    open_store(path, KEY).close()
    vault = vault_there(box)
    login = Login(vault.vault, DROPBOX, "", ACCOUNT, FIRST_REFRESH, vault.keys.keys)
    rounds = _Rounds(path, KEY, login, CloudTime(since_start, 0))
    db = rounds.database()
    sync_connection.keep(db, login)
    try:
        elsewhere = threading.Thread(target=rounds._renewed, args=("from the watcher",))
        elsewhere.start()
        elsewhere.join()
        assert rounds.login.secret == "from the watcher"
        assert secret_of(db) == FIRST_REFRESH  # not on that thread
        rounds._keep_renewal()  # as the round ends
        assert secret_of(db) == "from the watcher"
    finally:
        rounds.close()


def test_a_password_is_never_taken_for_a_refresh_token(tmp_path: Path) -> None:
    db = open_store(tmp_path / "a.db", KEY)
    try:
        login = Login("v" * 32, WEBDAV, "https://example.com/dav", "me", "a password", {})
        sync_connection.keep(db, login)
        sync_connection.renew(db, login.vault, "a password", "a token")
        assert secret_of(db) == "a password"
    finally:
        db.close()


def secret_of(db: apsw.Connection) -> str:
    login = sync_connection.current(db)
    assert login is not None
    return login.secret
