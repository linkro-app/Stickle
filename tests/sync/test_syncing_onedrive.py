"""Sync with OneDrive alongside the app: each refresh token Microsoft gives on
the way is the one kept, against a Graph of the test's own (graph_server)."""

# pyright: reportPrivateUsage=false
import gc
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import apsw
import pytest
from graph_server import ACCOUNT, CLIENT_ID, FIRST_REFRESH, Graph, graph_server
from PySide6.QtCore import QCoreApplication, QEvent
from pytestqt.qtbot import QtBot
from sync_bed import KEY

from stickle.app.notes import NoteManager
from stickle.app.syncing import Ended, Syncing, _Rounds, cloud_time, look_first
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
from stickle.data.settings import Settings
from stickle.data.sync_connection import ONEDRIVE, Login
from stickle.data.versions import VersionRepository
from stickle.platform.clocks import since_start
from stickle.sync.targets import onedrive
from stickle.sync.targets.onedrive import App
from stickle.sync.vault import Vault, make

ROUND_MS = 15_000
HOUR = 3600.0
DATABASE_KEY = bytes([7]) * 32
PAIRS = {
    RECOVERY_KEY: recovery.make_recovery_slot(
        DATABASE_KEY, recovery.generate(), "2026-11-22T00:00:00Z"
    ),
    MASTER_PASSWORD: make_sealed_slot(DATABASE_KEY, "a spare", 1, 8192, master.SLOT, None),
}


def said(told: Any) -> object:
    """What the signal waited for said first."""
    return told.args[0]


@pytest.fixture(autouse=True)
def windows_deleted() -> Iterator[None]:
    yield
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.enable()
    gc.collect()


@pytest.fixture
def graph() -> Iterator[Graph]:
    with graph_server() as serving:
        yield serving


def app_of(graph: Graph) -> App:
    return App(CLIENT_ID, graph.place, graph.context)


def vault_there(graph: Graph) -> Vault:
    target = app_of(graph).target(FIRST_REFRESH, lambda _token: None)
    try:
        return make(target, PAIRS)
    finally:
        target.close()


def secret_of(db: apsw.Connection) -> str:
    login = sync_connection.current(db)
    assert login is not None
    return login.secret


@dataclass
class Pc:
    db: apsw.Connection
    path: Path
    login: Login
    syncing: Syncing
    qtbot: QtBot

    def round(self) -> Ended:
        with self.qtbot.waitSignal(self.syncing.round_shown, timeout=ROUND_MS) as told:
            self.syncing.sync_now()
        ended = said(told)
        assert isinstance(ended, Ended)
        return ended

    def kept(self) -> Login:
        login = sync_connection.current(self.db)
        assert login is not None
        return login


def make_pc(qtbot: QtBot, tmp_path: Path, graph: Graph, app: App | None) -> Pc:
    vault = vault_there(graph)
    path = tmp_path / "a.db"
    db = open_store(path, KEY)
    login = Login(vault.vault, ONEDRIVE, "", ACCOUNT, FIRST_REFRESH, vault.keys.keys)
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
        db, path, KEY, login, cloud_time(db, clocks), manager, onedrive=app, start_wait=HOUR
    )
    syncing.start()
    return Pc(db, path, login, syncing, qtbot)


@pytest.fixture
def pc(qtbot: QtBot, tmp_path: Path, graph: Graph) -> Iterator[Pc]:
    made = make_pc(qtbot, tmp_path, graph, app_of(graph))
    yield made
    made.syncing.finish(0.0)
    made.db.close()


def test_a_round_keeps_the_refresh_token_it_was_given(pc: Pc, graph: Graph) -> None:
    graph.issued_refresh.clear()
    ended = pc.round()
    assert ended.outcome.stopped is None and ended.outcome.wait is None
    assert graph.issued_refresh  # renewed on the way
    assert pc.kept().secret == graph.issued_refresh[-1]
    assert pc.syncing.login.secret == graph.issued_refresh[-1]


def test_a_connection_kept_since_is_not_written_over(pc: Pc, graph: Graph) -> None:
    pc.round()
    anew = replace(pc.kept(), secret="signed in anew")
    sync_connection.keep(pc.db, anew)  # as when the user signed in again meanwhile
    graph.expire_access_tokens()
    pc.round()
    assert pc.kept().secret == "signed in anew"


def test_a_token_renewed_in_a_transaction_is_kept_once_it_ends(
    tmp_path: Path, graph: Graph
) -> None:
    path = tmp_path / "a.db"
    open_store(path, KEY).close()
    vault = vault_there(graph)
    login = Login(vault.vault, ONEDRIVE, "", ACCOUNT, FIRST_REFRESH, vault.keys.keys)
    rounds = _Rounds(path, KEY, login, CloudTime(since_start, 0))
    db = rounds.database()
    sync_connection.keep(db, login)
    try:
        with pytest.raises(RuntimeError), db:  # the round's own writing, given up
            rounds._renewed("the newest")
            raise RuntimeError
        assert secret_of(db) == FIRST_REFRESH
        rounds._keep_renewal()  # as the round ends
        assert secret_of(db) == "the newest"
        rounds._renewed("newer still")  # out of a transaction: at once
        assert secret_of(db) == "newer still"
    finally:
        rounds.close()


def test_a_build_without_stickle_registered_tries_again_later(
    qtbot: QtBot, tmp_path: Path, graph: Graph, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onedrive, "registered", lambda: None)
    made = make_pc(qtbot, tmp_path, graph, None)
    try:
        ended = made.round()
        assert ended.outcome.stopped is None and ended.outcome.wait is not None
        assert made.kept().secret == FIRST_REFRESH and made.kept().stopped is None
    finally:
        made.syncing.finish(0.0)
        made.db.close()


def test_a_look_before_turning_on_tells_of_the_tokens_it_was_given(
    tmp_path: Path, graph: Graph
) -> None:
    path = tmp_path / "a.db"
    open_store(path, KEY).close()
    vault = vault_there(graph)
    login = Login(vault.vault, ONEDRIVE, "", ACCOUNT, FIRST_REFRESH, vault.keys.keys)
    graph.issued_refresh.clear()
    told: list[str] = []
    look = look_first(path, KEY, login, renewed=told.append, onedrive=app_of(graph))
    assert look is not None
    assert told and told == graph.issued_refresh
