"""The Connections page with sync on, against a WebDAV server of the test's own:
what it shows and sets, and leaving: nothing of the notes is touched, and of
the Stickle folder only this computer's own file goes. And the notes that, a
minute after they changed, have not reached the cloud."""

import gc
import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import apsw
import pytest
from PySide6.QtCore import (
    QCoreApplication,
    QDate,
    QDateTime,
    QEvent,
    QLocale,
    QObject,
    Qt,
    QTime,
    QTimer,
    Signal,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QWidget
from pytestqt.qtbot import QtBot
from real_server import account, account_given, folder_of_its_own
from test_turn_on import (
    DB_KEY,
    MS,
    PAIRS,
    PASSWORD,
    Bench,
    FakeSyncing,
    Keys,
    Pc,
    make_pc,
    on_server,
    target_of,
)
from update_translations import TS_DIR, compile_to
from webdav_server import HOST, Server, webdav

from stickle.app.application import connect_sync, disconnect_sync
from stickle.app.background import Job
from stickle.app.i18n import Translations
from stickle.app.settings_window import SettingsWindow
from stickle.app.sync_display import SyncDisplay
from stickle.app.sync_panel import ChangeName, DisconnectSync, SyncPanel, on_one_line, server_of
from stickle.app.sync_setup import SyncSetup
from stickle.app.syncing import (
    LEAVING_TIMEOUT,
    QUIT_WAIT,
    Ended,
    Syncing,
    cloud_time,
    stopped_as_kept,
)
from stickle.app.syncing import Syncing as SyncRun
from stickle.app.times import day_text
from stickle.app.turn_on_sync import TurnOnSync
from stickle.core.hlc import Hlc
from stickle.core.updates import FEED_HOST
from stickle.data import record_state, sync_connection, sync_state
from stickle.data.settings import DEVICE_NAME, SYNC_EVERY, Settings
from stickle.data.sync_connection import WEBDAV, Login
from stickle.sync.contents import Contents, Field
from stickle.sync.devices import APP, others, this_device
from stickle.sync.engine import Report
from stickle.sync.format import DEVICES, item_path, json_bytes
from stickle.sync.outcome import Outcome, Stopped
from stickle.sync.targets.base import Target, UnavailableError
from stickle.sync.targets.webdav import WebDav
from stickle.sync.vault import make


class Collecting:
    """Whether the app has garbage collected by itself, kept off in fact: the test
    server's threads are not sync's, and would collect windows on their own."""

    def __init__(self) -> None:
        self.on = True

    def enable(self) -> None:
        self.on = True

    def disable(self) -> None:
        self.on = False

    def isenabled(self) -> bool:
        return self.on

    def collect(self) -> int:
        return 0


@pytest.fixture(autouse=True)
def collecting(monkeypatch: pytest.MonkeyPatch) -> Iterator[Collecting]:
    made = Collecting()
    monkeypatch.setattr("stickle.app.syncing.gc", made)
    monkeypatch.setattr("stickle.app.background.gc", made)
    gc.disable()
    yield made
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.enable()
    gc.collect()  # here, and not on some thread of the next tests' servers


@pytest.fixture
def share(tmp_path: Path) -> Path:
    root = tmp_path / "dav"
    (root / "share").mkdir(parents=True)
    return root


class Computer:
    """A computer with the app's way of starting and leaving sync."""

    def __init__(self, pc: Pc, keys: Keys) -> None:
        self.pc = pc
        self.keys = keys
        self.live: Syncing | None = None
        self.begun = 0
        self.through = 0

    @property
    def db(self) -> apsw.Connection:
        return self.pc.db

    @property
    def device(self) -> str:
        return Settings(self.db).device_id()

    def connect(self, login: Login, joining: bool) -> Syncing:
        self.live = self.pc.connect(login, joining)
        self.begun, self.through = 0, 0
        work = self.live.runner._work  # pyright: ignore[reportPrivateUsage]

        def counted(full: bool) -> Ended:
            self.begun += 1
            try:
                return work(full)
            finally:
                self.through += 1

        self.live.runner._work = counted  # pyright: ignore[reportPrivateUsage]
        return self.live

    def disconnect(self) -> None:
        assert self.live is not None
        disconnect_sync(self.db, self.live, self.pc.clocks)
        self.live = None

    def setup(self) -> SyncSetup:
        return SyncSetup(
            lambda: self.live,
            self.keys.keys(),
            lambda: len(self.pc.manager.listed_notes()),
            lambda: PAIRS,
            self.connect,
            self.disconnect,
        )

    def page(self, qtbot: QtBot) -> SettingsWindow:
        window = SettingsWindow(self.pc.manager, Translations(), sync_setup=self.setup())
        qtbot.addWidget(window)
        window.show()
        window.tabs.setCurrentWidget(window.connections_page)
        window.refresh()
        return window

    def round_over(self, qtbot: QtBot, timeout: int = MS) -> None:
        """One more round, begun after this was asked, through and shown."""
        assert self.live is not None
        last = self.begun + 1
        self.live.sync_now()
        qtbot.waitUntil(lambda: self.through >= last, timeout=timeout)
        qtbot.wait(20)  # what it brought, shown

    def end(self) -> None:
        self.pc.end()


@dataclass
class Bed:
    server: Server
    login: Login
    first: Computer
    second: Computer
    root: Path

    def folder(self) -> Path:
        return self.root / "share" / "Stickle"

    def files(self) -> dict[str, bytes]:
        """Every file of the Stickle folder: by where it is in it."""
        return {
            path.relative_to(self.folder()).as_posix(): path.read_bytes()
            for path in sorted(self.folder().rglob("*"))
            if path.is_file()
        }

    def own_file(self, computer: Computer) -> str:
        return item_path(DEVICES, computer.device)


@pytest.fixture
def bed(qtbot: QtBot, tmp_path: Path, share: Path) -> Iterator[Bed]:
    first = Computer(make_pc(tmp_path, "first"), Keys())
    second = Computer(make_pc(tmp_path, "second"), Keys())
    with webdav(share, PASSWORD) as server:
        target = target_of(server)
        made = make(target, PAIRS)
        target.close()
        login = Login(
            made.vault, WEBDAV, on_server(server), "stickle", PASSWORD, dict(made.keys.keys)
        )
        made_bed = Bed(server, login, first, second, share)
        first.connect(login, False)
        qtbot.waitUntil(lambda: _synced(first), timeout=MS)
        yield made_bed
        first.end()
        second.end()


def _synced(computer: Computer) -> bool:
    return computer.live is not None and computer.live.status.text() != ""


def join(bed: Bed, qtbot: QtBot) -> Computer:
    bed.second.connect(bed.login, True)
    qtbot.waitUntil(lambda: _synced(bed.second), timeout=MS)
    bed.first.round_over(qtbot)
    return bed.second


def panel_of(window: SettingsWindow) -> SyncPanel:
    assert window.sync_panel is not None
    return window.sync_panel


def the_dialog[T: QWidget](window: SettingsWindow, kind: type[T]) -> T:
    found = window.findChild(kind)
    assert isinstance(found, kind)
    return found


def press_disconnect(window: SettingsWindow) -> DisconnectSync:
    panel_of(window).disconnect_button.click()
    return the_dialog(window, DisconnectSync)


def disconnect(window: SettingsWindow, qtbot: QtBot, computer: Computer) -> None:
    dialog = press_disconnect(window)
    dialog.disconnect_button.click()
    qtbot.waitUntil(lambda: computer.live is None, timeout=MS)


# What it shows


def test_it_says_where_and_as_whom_and_never_the_password(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    panel = panel_of(window)
    assert panel.account.text() == f"WebDAV · {HOST}:{bed.server.port} · stickle"
    for label in window.findChildren(QLabel):
        assert PASSWORD not in label.text()


def test_what_comes_from_elsewhere_is_shown_as_it_is_written(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    panel = panel_of(window)
    for label in (panel.account, panel.status, window.connections):
        assert label.textFormat() == Qt.TextFormat.PlainText


@pytest.mark.parametrize(
    ("address", "server"),
    [
        ("https://nas.example/dav/stickle", "nas.example"),
        ("https://nas.example:5006/dav", "nas.example:5006"),
        ("http://[fe80::1]:8080/a", "[fe80::1]:8080"),
    ],
)
def test_of_an_address_it_shows_the_server_alone(address: str, server: str) -> None:
    assert server_of(address) == server


def test_it_says_how_sync_stands_and_goes_on_saying_so(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    panel = panel_of(window)
    assert panel.status.text().startswith("Synced with WebDAV · ")
    assert panel.status.accessibleName() == panel.status.text()
    assert bed.first.live is not None
    with qtbot.waitSignal(bed.first.live.status.changed, timeout=MS):
        bed.first.live.status.moved("putting", 30, 50)
    assert panel.status.text() == "Uploading to WebDAV · 30 of 50 notes"


def test_the_address_list_replaces_the_one_sentence(bed: Bed, qtbot: QtBot) -> None:
    class On(QObject):
        enabled = True
        switched = Signal()

        def set_enabled(self, _on: bool) -> None:
            pass

    updates = On()
    window = SettingsWindow(
        bed.first.pc.manager,
        Translations(),
        sync_setup=bed.first.setup(),
        updates=updates,  # pyright: ignore[reportArgumentType]
    )
    qtbot.addWidget(window)
    lines = window.connections.text().split("\n")
    assert lines == [
        "Stickle connects to:",
        f"· {HOST}:{bed.server.port} — to sync with WebDAV",
        f"· {FEED_HOST} — a daily check for a new version (you can turn it off in General)",
    ]
    updates.enabled = False
    window.refresh()
    assert window.connections.text().split("\n") == lines[:2]


def test_with_no_update_check_only_the_server_is_listed(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    assert FEED_HOST not in window.connections.text()
    assert f"{HOST}:{bed.server.port}" in window.connections.text()


def test_every_part_of_it_is_reached_by_the_keyboard_in_order(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    panel = panel_of(window)
    in_sight = [each for each in panel.tab_order() if each.isVisibleTo(window)]
    assert panel.review_button not in in_sight  # only while stopped for what a window asks
    wanted = [*in_sight, window.sync_guide.link]
    walked: list[QWidget] = []
    at: QWidget = window.turn_on_sync_button  # out of sight, with sync on: what comes after it
    while len(walked) < len(wanted):
        following = at.nextInFocusChain()
        assert following is not None
        at = following
        if at.focusPolicy() & Qt.FocusPolicy.TabFocus and at.isVisibleTo(window):
            walked.append(at)
    assert walked == wanted
    for widget in panel.tab_order():
        assert widget.focusPolicy() & Qt.FocusPolicy.TabFocus
    assert panel.computers.accessibleName() == "Computers that sync"
    assert panel.every_label.buddy() is panel.every_box


def test_it_appears_with_sync_on_and_is_replaced_when_it_goes(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    assert window.sync_panel is not None
    assert window.sync_panel.isVisible()
    assert not window.turn_on_sync_button.isVisible()
    assert window.sync_guide.isVisible()
    disconnect(window, qtbot, bed.first)
    qtbot.waitUntil(lambda: window.sync_panel is None, timeout=MS)
    assert window.turn_on_sync_button.isVisible()
    assert "Stickle connects to:" not in window.connections.text()


def test_its_words_change_with_the_language(bed: Bed, qtbot: QtBot, tmp_path: Path) -> None:
    window = bed.first.page(qtbot)
    folder = tmp_path / "translations"
    folder.mkdir()
    compile_to(TS_DIR / "stickle_ko.ts", folder / "stickle_ko.qm")
    translations = Translations(folder)
    try:
        translations.apply("ko")
        panel = panel_of(window)
        assert "동기화됨" in panel.status.text()
        assert panel.computers.item(0).text().startswith("이 PC — ")
        assert panel.every_box.currentText() == "5분마다"
    finally:
        translations.apply("en")  # no file needed: it takes away what the test installed
        QLocale.setDefault(QLocale.system())


# Sync now


def test_sync_now_goes_grey_until_the_round_is_over(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    panel = panel_of(window)
    assert panel.sync_now_button.isEnabled()
    asked_before = len(bed.server.asked)
    panel.sync_now_button.click()
    assert not panel.sync_now_button.isEnabled()
    qtbot.waitUntil(panel.sync_now_button.isEnabled, timeout=MS)
    assert len(bed.server.asked) > asked_before


def test_the_key_focus_comes_back_to_the_button(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow, timeout=MS)
    panel = panel_of(window)
    panel.sync_now_button.setFocus()
    qtbot.waitUntil(panel.sync_now_button.hasFocus, timeout=MS)
    panel.sync_now_button.click()
    qtbot.waitUntil(panel.sync_now_button.isEnabled, timeout=MS)
    assert panel.sync_now_button.hasFocus()


def test_the_key_focus_moved_on_stays_where_it_went(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow, timeout=MS)
    panel = panel_of(window)
    panel.sync_now_button.setFocus()
    qtbot.waitUntil(panel.sync_now_button.hasFocus, timeout=MS)
    panel.sync_now_button.click()
    panel.rename_button.setFocus()
    qtbot.waitUntil(panel.sync_now_button.isEnabled, timeout=MS)
    assert panel.rename_button.hasFocus()


def test_a_round_that_never_says_it_ended_gives_the_button_back(
    bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = bed.first.page(qtbot)
    panel = panel_of(window)
    assert bed.first.live is not None
    monkeypatch.setattr(bed.first.live, "sync_now", lambda: None)
    panel._asked.setInterval(10)  # pyright: ignore[reportPrivateUsage]
    panel.sync_now_button.click()
    assert not panel.sync_now_button.isEnabled()
    qtbot.waitUntil(panel.sync_now_button.isEnabled, timeout=MS)


# How often it looks


def test_it_shows_how_often_and_sets_it_at_once(
    bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    Settings(bed.first.db).set(SYNC_EVERY, 15 * 60)
    window = bed.first.page(qtbot)
    panel = panel_of(window)
    assert panel.every_box.currentText() == "Every 15 minutes"
    assert [panel.every_box.itemText(i) for i in range(panel.every_box.count())] == [
        "Every 30 seconds",
        "Every minute",
        "Every 5 minutes",
        "Every 15 minutes",
        "Every 30 minutes",
        "Every hour",
    ]
    told: list[float] = []
    assert bed.first.live is not None
    monkeypatch.setattr(bed.first.live.runner, "set_every", told.append)
    index = panel.every_box.findData(60 * 60)
    panel.every_box.setCurrentIndex(index)
    panel.every_box.activated.emit(index)
    assert told == [60 * 60]
    assert Settings(bed.first.db).get(SYNC_EVERY) == 60 * 60


# The computers that sync


def test_this_computer_comes_first_and_the_others_after_it(bed: Bed, qtbot: QtBot) -> None:
    second = join(bed, qtbot)
    Settings(second.db).set(DEVICE_NAME, "노트북")
    second.round_over(qtbot)
    bed.first.round_over(qtbot)
    window = bed.first.page(qtbot)
    computers = panel_of(window).computers
    assert computers.item(0).text() == f"This computer — {this_device().name}"
    assert computers.count() == 2
    system = this_device().os
    assert computers.item(1).text().startswith(f"노트북 — {system} · Stickle ")
    assert computers.item(1).text().endswith(" · last synced today")


def test_the_list_keeps_up_while_the_page_is_open(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    assert panel_of(window).computers.count() == 1
    join(bed, qtbot)
    assert panel_of(window).computers.count() == 2


def _other_said(computer: Computer, vault: str, name: str, **rest: object) -> None:
    other = "c-0000-4000-8000-000000000000"
    said: dict[str, object] = {"name": name, "os": "Linux", "version": "0.6.9", "kind": APP, **rest}
    fields = {k: Field(v, Hlc(1), other) for k, v in said.items()}  # pyright: ignore[reportArgumentType]
    kept = json_bytes(Contents(fields, {}).to_json()).decode("utf-8")
    record_state.put_device(computer.db, vault, other, kept, None)


def test_a_day_that_was_not_today_is_said_as_a_day(bed: Bed, qtbot: QtBot) -> None:
    long_ago = QDate.currentDate().addDays(-40)
    at = QDateTime(long_ago, QTime(12, 0))
    _other_said(bed.first, bed.login.vault, "오래된 PC", synced=at.toMSecsSinceEpoch())
    window = bed.first.page(qtbot)
    assert (
        panel_of(window).computers.item(1).text()
        == f"오래된 PC — Linux · Stickle 0.6.9 · last synced {day_text(long_ago)}"
    )


def test_a_name_with_marks_in_it_stays_as_it_is(bed: Bed, qtbot: QtBot) -> None:
    _other_said(bed.first, bed.login.vault, "%2 <b>PC</b>\n둘째 줄")
    window = bed.first.page(qtbot)
    assert (
        panel_of(window).computers.item(1).text() == "%2 <b>PC</b> 둘째 줄 — Linux · Stickle 0.6.9"
    )


def test_a_name_is_put_on_one_line() -> None:
    assert on_one_line("  첫째 줄\n둘째\t줄 ") == "첫째 줄 둘째 줄"


def test_a_long_list_scrolls_rather_than_growing(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    computers = panel_of(window).computers
    one_row = computers.height()
    for number in range(6):
        other = f"{number}-0000-4000-8000-000000000000"
        kept = json_bytes(
            Contents(
                {
                    "name": Field(f"PC {number}", Hlc(1), other),
                    "kind": Field(APP, Hlc(1), other),
                },
                {},
            ).to_json()
        ).decode("utf-8")
        record_state.put_device(bed.first.db, bed.login.vault, other, kept, None)
    panel_of(window)._show_computers()  # pyright: ignore[reportPrivateUsage]
    assert computers.count() == 7
    row = computers.sizeHintForRow(0)
    assert computers.height() == row * 4 + 2 * computers.frameWidth()
    assert computers.height() < row * 7
    assert one_row <= computers.height()


# Its name


def rename_dialog(window: SettingsWindow) -> ChangeName:
    panel_of(window).rename_button.click()
    return the_dialog(window, ChangeName)


def test_a_name_given_reaches_the_other_computer(bed: Bed, qtbot: QtBot) -> None:
    second = join(bed, qtbot)
    window = bed.first.page(qtbot)
    dialog = rename_dialog(window)
    dialog.name.setText("  거실 PC  ")
    dialog.ok_button.click()
    assert Settings(bed.first.db).get(DEVICE_NAME) == "거실 PC"
    assert panel_of(window).computers.item(0).text() == "This computer — 거실 PC"
    qtbot.waitUntil(
        lambda: (
            [o.name for o in others(second.db, bed.login.vault, second.device)] == ["거실 PC"]
            or second.round_over(qtbot) is not None
        ),
        timeout=MS,
    )


def test_a_blank_name_goes_back_to_what_the_system_calls_it(bed: Bed, qtbot: QtBot) -> None:
    Settings(bed.first.db).set(DEVICE_NAME, "거실 PC")
    window = bed.first.page(qtbot)
    dialog = rename_dialog(window)
    assert dialog.name.text() == "거실 PC"
    assert dialog.name.placeholderText() == this_device().name
    dialog.name.setText("   ")
    dialog.ok_button.click()
    assert Settings(bed.first.db).get(DEVICE_NAME) == ""
    assert panel_of(window).computers.item(0).text() == f"This computer — {this_device().name}"


def test_cancelling_keeps_the_name(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    dialog = rename_dialog(window)
    dialog.name.setText("다른 이름")
    dialog.cancel_button.click()
    assert Settings(bed.first.db).get(DEVICE_NAME) == ""


def test_a_name_cannot_be_longer_than_it_may_be_kept(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    dialog = rename_dialog(window)
    dialog.name.setText("가" * 100)
    assert len(dialog.chosen()) == 64
    dialog.ok_button.click()
    assert Settings(bed.first.db).get(DEVICE_NAME) == "가" * 64


# Leaving


def state_of_the_notes(computer: Computer) -> list[object]:
    rows = computer.db.execute(
        "SELECT id, body, deleted_at, color, change_seq FROM notes ORDER BY id"
    )
    return [tuple(row) for row in rows]


def test_the_window_says_what_will_happen(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    dialog = press_disconnect(window)
    assert dialog.windowTitle() == "Disconnect"
    assert dialog.isModal()
    words = [dialog.turns_off.text(), dialog.folder_stays.text(), dialog.password_goes.text()]
    assert words == [
        "This turns off sync on this computer. Your notes stay on this computer.",
        "The Stickle folder in WebDAV isn't deleted, so your other computers keep syncing.",
        "The server password saved on this computer is deleted too.",
    ]
    dialog.reject()


@pytest.mark.parametrize("how", ["button", "esc", "enter"])
def test_not_going_through_with_it_leaves_all_as_it_was(bed: Bed, qtbot: QtBot, how: str) -> None:
    window = bed.first.page(qtbot)
    dialog = press_disconnect(window)
    live = bed.first.live
    finished: list[int] = []
    dialog.finished.connect(finished.append)
    assert dialog.cancel_button.isDefault()
    assert not dialog.disconnect_button.isDefault()
    assert not dialog.disconnect_button.autoDefault()
    match how:
        case "button":
            dialog.cancel_button.click()
        case "esc":
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
        case _:
            QTest.keyClick(dialog.cancel_button, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: finished != [], timeout=MS)
    assert bed.first.live is live
    assert live is not None and live.runner.running
    assert sync_connection.current(bed.first.db) is not None


def test_leaving_touches_no_note(bed: Bed, qtbot: QtBot) -> None:
    pc = bed.first.pc
    kept = pc.notes.create("남을 메모")
    trashed = pc.notes.create("휴지통의 메모")
    pc.notes.delete(trashed.id)
    bed.first.round_over(qtbot)
    before = state_of_the_notes(bed.first)
    window = bed.first.page(qtbot)

    disconnect(window, qtbot, bed.first)

    assert state_of_the_notes(bed.first) == before
    found = pc.notes.get(kept.id)
    assert found is not None and found.body == "남을 메모"


def test_of_the_folder_only_its_own_file_goes(bed: Bed, qtbot: QtBot) -> None:
    bed.first.pc.notes.create("메모")
    bed.first.round_over(qtbot)
    before = bed.files()
    own = bed.own_file(bed.first)
    assert own in before
    window = bed.first.page(qtbot)

    disconnect(window, qtbot, bed.first)

    after = bed.files()
    assert own not in after
    assert after == {path: data for path, data in before.items() if path != own}


def test_it_forgets_how_to_reach_the_cloud(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    disconnect(window, qtbot, bed.first)
    assert sync_connection.current(bed.first.db) is None
    rows = bed.first.db.execute("SELECT count(*) FROM sync_connection").fetchall()
    assert rows[0][0] == 0
    offset = bed.first.db.execute(
        "SELECT count(*) FROM local_counters WHERE key = 'cloud_offset'"
    ).fetchall()
    assert offset[0][0] == 0


def test_this_run_goes_by_this_computers_clock_again(bed: Bed, qtbot: QtBot) -> None:
    assert bed.first.pc.clocks.cloud is not None
    window = bed.first.page(qtbot)
    disconnect(window, qtbot, bed.first)
    assert bed.first.pc.clocks.cloud is None


def test_nothing_more_is_asked_of_the_server(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    live = bed.first.live
    assert live is not None
    disconnect(window, qtbot, bed.first)
    qtbot.waitUntil(lambda: not live.runner.running, timeout=MS)
    asked = len(bed.server.asked)
    bed.first.pc.notes.create("끊은 뒤의 메모")
    qtbot.wait(300)
    live.runner.edited()  # told or not, it no longer goes
    live.runner.asked()
    qtbot.wait(300)
    assert len(bed.server.asked) == asked


def test_garbage_is_collected_as_usual_after_it(
    bed: Bed, qtbot: QtBot, collecting: Collecting
) -> None:
    assert not collecting.isenabled()
    window = bed.first.page(qtbot)
    live = bed.first.live
    assert live is not None
    collector = live._collector  # pyright: ignore[reportPrivateUsage]
    assert collector.isActive()
    disconnect(window, qtbot, bed.first)
    qtbot.waitUntil(collecting.isenabled, timeout=MS)
    assert not collector.isActive()


def test_collecting_stays_off_while_other_work_goes_on(
    bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, collecting: Collecting
) -> None:
    live = bed.first.live
    assert live is not None
    live.runner.finish(0.0)
    qtbot.waitUntil(lambda: not live.runner.running, timeout=MS)
    monkeypatch.setattr("stickle.app.syncing.work_goes_on", lambda: True)
    collecting.disable()
    live.left()
    assert not collecting.isenabled()


def test_a_round_told_of_once_it_has_ended_is_not_shown(bed: Bed, qtbot: QtBot) -> None:
    """Sent as the app ended: what it would be shown with is closing."""
    live = bed.first.live
    assert live is not None
    live.finish(0.0)
    shown: list[object] = []
    live.round_shown.connect(shown.append)
    live._show(Ended(Report(), Outcome()))  # pyright: ignore[reportPrivateUsage]
    assert shown == []


def test_what_was_just_typed_goes_up_before_it_leaves(bed: Bed, qtbot: QtBot) -> None:
    second = join(bed, qtbot)
    note = bed.first.pc.manager.new_note()
    note.editor.setPlainText("처음 글")
    bed.first.pc.manager.save_all()
    made = note.note_id
    assert made is not None
    bed.first.round_over(qtbot)
    second.round_over(qtbot)
    note.editor.setPlainText("끊기 직전에 쓴 글")
    window = bed.first.page(qtbot)

    disconnect(window, qtbot, bed.first)
    second.round_over(qtbot)

    got = second.pc.notes.get(made)
    assert got is not None and got.body == "끊기 직전에 쓴 글"


def test_while_it_leaves_it_cannot_be_closed_or_pressed_again(
    bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = bed.first.page(qtbot)
    let_go = threading.Event()
    live = bed.first.live
    assert live is not None
    real_leave = live.leave

    def held() -> None:
        let_go.wait(MS / 1000)
        real_leave()

    monkeypatch.setattr(live, "leave", held)
    dialog = press_disconnect(window)
    finished: list[int] = []
    dialog.finished.connect(finished.append)
    dialog.disconnect_button.click()
    assert not dialog.disconnect_button.isEnabled()
    assert not dialog.cancel_button.isEnabled()
    assert dialog.message.isVisible()
    assert dialog.message.text() == "Disconnecting…"
    dialog.reject()
    dialog.close()
    qtbot.wait(50)
    assert finished == []
    assert dialog.isVisible()
    assert bed.first.live is live
    assert sync_connection.current(bed.first.db) is not None
    let_go.set()
    qtbot.waitUntil(lambda: finished != [], timeout=MS)
    assert sync_connection.current(bed.first.db) is None


def test_the_window_is_not_held_up_while_it_leaves(
    bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A note can still be typed in, and the app goes on, whatever the network does."""
    window = bed.first.page(qtbot)
    let_go = threading.Event()
    live = bed.first.live
    assert live is not None
    monkeypatch.setattr(live, "leave", lambda: let_go.wait(MS / 1000))
    dialog = press_disconnect(window)
    dialog.disconnect_button.click()
    ran: list[bool] = []
    timer = QTimer()
    timer.timeout.connect(lambda: ran.append(True))
    timer.start(1)
    qtbot.waitUntil(lambda: len(ran) > 3, timeout=MS)
    timer.stop()
    let_go.set()
    qtbot.waitUntil(lambda: bed.first.live is None, timeout=MS)


def test_with_the_server_gone_it_still_leaves(bed: Bed, qtbot: QtBot, share: Path) -> None:
    window = bed.first.page(qtbot)
    live = bed.first.live
    assert live is not None

    def nobody_there() -> Target:
        raise UnavailableError()

    monkeypatch_reach(live, nobody_there)
    disconnect(window, qtbot, bed.first)
    assert sync_connection.current(bed.first.db) is None
    assert bed.own_file(bed.first) in bed.files()  # it could not be reached: it stays


def monkeypatch_reach(live: Syncing, reach: Callable[[], Target]) -> None:
    def replaced(*_args: object) -> Target:
        return reach()

    live._rounds._reach = replaced  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]


def test_saying_why_it_stays_listed_says_no_address_or_name(
    bed: Bed, qtbot: QtBot, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    window = bed.first.page(qtbot)
    live = bed.first.live
    assert live is not None

    def nobody_there() -> Target:
        raise UnavailableError(f"{bed.login.address} {bed.login.user}")

    monkeypatch_reach(live, nobody_there)
    disconnect(window, qtbot, bed.first)
    assert "this device stays listed: UnavailableError" in caplog.text
    assert bed.login.address not in caplog.text
    assert str(bed.server.port) not in caplog.text
    assert PASSWORD not in caplog.text


def test_a_fault_on_the_way_out_still_ends_it(
    bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = bed.first.page(qtbot)
    live = bed.first.live
    assert live is not None

    def broken() -> None:
        raise ValueError("broke")

    monkeypatch.setattr(live, "leave", broken)
    disconnect(window, qtbot, bed.first)
    assert sync_connection.current(bed.first.db) is None
    live.runner.finish(0.0)


def test_a_database_that_will_not_forget_does_not_hold_the_window(
    bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    window = bed.first.page(qtbot)
    calls: list[bool] = []

    def refused() -> None:
        calls.append(True)
        raise apsw.BusyError("busy")

    dialog = press_disconnect(window)
    monkeypatch.setattr(dialog, "_disconnected", refused)
    finished: list[int] = []
    dialog.finished.connect(finished.append)
    dialog.disconnect_button.click()
    qtbot.waitUntil(lambda: finished != [], timeout=MS)
    assert calls == [True]
    assert "could not forget the connection: BusyError" in caplog.text


def test_after_it_a_change_here_is_noticed_by_nothing(bed: Bed, qtbot: QtBot) -> None:
    """The app's connection no longer tells a thread that is gone."""
    window = bed.first.page(qtbot)
    live = bed.first.live
    assert live is not None
    disconnect(window, qtbot, bed.first)
    written = live._written_rows  # pyright: ignore[reportPrivateUsage]
    bed.first.pc.notes.create("끊은 뒤의 메모")
    qtbot.wait(20)
    assert written == set()
    assert not live._noticing  # pyright: ignore[reportPrivateUsage]


def test_leaving_stops_listening_to_the_app(
    bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = bed.first.live
    assert live is not None
    told: list[str] = []
    monkeypatch.setattr(live.runner, "left", lambda: told.append("left"))
    monkeypatch.setattr(live.runner, "focused", lambda: told.append("focused"))
    live.leaving()
    bed.first.pc.manager.left_note.emit()
    application = QGuiApplication.instance()
    assert isinstance(application, QGuiApplication)
    application.applicationStateChanged.emit(Qt.ApplicationState.ApplicationActive)
    assert told == []
    assert live.runner.running  # nothing has ended yet


def test_a_request_still_out_keeps_collecting_off_until_it_is_back(
    bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, collecting: Collecting
) -> None:
    live = bed.first.live
    assert live is not None
    goes_on = [True]
    monkeypatch.setattr(type(live.runner), "running", property(lambda _self: goes_on[0]))
    monkeypatch.setattr("stickle.app.syncing.LOOK_AGAIN_MS", 5)
    collecting.disable()
    live.left()
    qtbot.wait(30)
    assert not collecting.isenabled()
    goes_on[0] = False
    monkeypatch.setattr("stickle.app.syncing.work_goes_on", lambda: False)
    qtbot.waitUntil(collecting.isenabled, timeout=MS)


def test_what_a_round_ending_late_says_is_not_kept(
    bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its connection forgotten meanwhile, the cloud's clock would come back."""
    live = bed.first.live
    assert live is not None
    rounds = live._rounds  # pyright: ignore[reportPrivateUsage]
    kept: list[int] = []

    def noted(_db: object, offset: int) -> None:
        kept.append(offset)

    monkeypatch.setattr("stickle.app.syncing.sync_connection.keep_cloud_offset", noted)
    rounds._offset = 10**9  # pyright: ignore[reportPrivateUsage]
    rounds._keep(bed.first.db, Outcome(), Report())  # pyright: ignore[reportPrivateUsage]
    assert len(kept) == 1  # going on, it is kept
    live.runner.finish(0.0)
    qtbot.waitUntil(lambda: not live.runner.running, timeout=MS)
    assert rounds.over()
    rounds._offset = 10**9  # pyright: ignore[reportPrivateUsage]
    rounds._keep(bed.first.db, Outcome(), Report())  # pyright: ignore[reportPrivateUsage]
    assert len(kept) == 1


def test_on_the_way_out_a_request_is_given_less_time(
    bed: Bed, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = bed.first.live
    assert live is not None
    given: list[object] = []
    real = WebDav.__init__

    def noted(self: WebDav, *args: Any, **kwargs: Any) -> None:
        given.append(kwargs.get("timeout"))
        real(self, *args, **kwargs)

    monkeypatch.setattr(WebDav, "__init__", noted)
    live.leaving()
    live.leave()
    assert given == [LEAVING_TIMEOUT]


def test_when_it_last_went_through_is_by_this_computers_clock(
    bed: Bed, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = bed.first.live
    assert live is not None
    monkeypatch.setattr(type(live._rounds.cloud), "offset", property(lambda _self: 60_000))  # pyright: ignore[reportPrivateUsage]
    sync_state.set_synced_at(bed.first.db, bed.login.vault, 1_700_000_060_000)
    shown = live.last_synced()
    assert shown is not None
    assert shown.toMSecsSinceEpoch() == 1_700_000_000_000


def test_what_waits_to_go_up_is_counted(bed: Bed, qtbot: QtBot) -> None:
    live = bed.first.live
    assert live is not None
    bed.first.round_over(qtbot)
    assert live.to_put() == 0
    bed.first.pc.notes.create("아직 안 올린 메모")
    assert live.to_put() == 1


def test_a_new_name_goes_with_the_next_round_and_does_not_wait_for_one(
    bed: Bed, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = bed.first.live
    assert live is not None
    told: list[bool] = []
    monkeypatch.setattr(live.runner, "edited", lambda big=False: told.append(big))
    live.rename("거실 PC")
    assert told == [False]


def test_a_stopped_kind_this_version_does_not_know_is_not_told_of(bed: Bed, qtbot: QtBot) -> None:
    """Left by a later version: not a reason for this one not to start."""
    sync_connection.set_stopped(bed.first.db, "something new")
    assert stopped_as_kept("something new") is None
    assert stopped_as_kept(Stopped.SIGN_IN.value) is Stopped.SIGN_IN
    assert stopped_as_kept(None) is None


# A note not in the cloud yet


def unsent_of(live: Syncing) -> list[frozenset[str]]:
    told: list[frozenset[str]] = []
    live.unsent_changed.connect(told.append)
    return told


def test_a_note_going_up_as_usual_is_never_shown_as_not_there(bed: Bed, qtbot: QtBot) -> None:
    live = bed.first.live
    assert live is not None
    told = unsent_of(live)
    note = bed.first.pc.notes.create("곧 올라갈 메모")
    assert [each for each, _at in sync_state.changed_at(bed.first.db, bed.login.vault)] == [note.id]
    bed.first.round_over(qtbot)
    assert sync_state.changed_at(bed.first.db, bed.login.vault) == []
    assert told == []
    assert not live._next_unsent.isActive()  # pyright: ignore[reportPrivateUsage]


def test_a_minute_on_it_is_shown_and_once_up_it_is_not(
    bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = bed.first.live
    assert live is not None
    told = unsent_of(live)
    monkeypatch.setattr("stickle.app.syncing.UNSENT_AFTER_MS", 150)
    note = bed.first.pc.notes.create("못 올린 메모")
    qtbot.wait(20)  # told of the change: it is looked at when it can be that old
    qtbot.waitUntil(lambda: told != [], timeout=MS)
    assert told == [frozenset({note.id})]

    bed.first.round_over(qtbot)
    assert told == [frozenset({note.id}), frozenset()]
    assert not live._next_unsent.isActive()  # pyright: ignore[reportPrivateUsage]


def test_it_is_looked_at_when_the_next_will_be_that_old(
    bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = bed.first.live
    assert live is not None
    told = unsent_of(live)
    monkeypatch.setattr("stickle.app.syncing.UNSENT_AFTER_MS", 30_000)
    bed.first.pc.notes.create("메모")
    live._look_for_unsent()  # pyright: ignore[reportPrivateUsage]
    timer = live._next_unsent  # pyright: ignore[reportPrivateUsage]
    assert timer.isActive()
    assert 25_000 < timer.interval() <= 30_000  # its time left is rounded, up as well as down
    assert told == []  # not that old yet


def test_before_a_first_round_nothing_is_shown(
    qtbot: QtBot, tmp_path: Path, share: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first upload does not put a cloud on every note."""
    monkeypatch.setattr("stickle.app.syncing.UNSENT_AFTER_MS", 0)
    computer = Computer(make_pc(tmp_path, "pc"), Keys())
    computer.pc.notes.create("이미 있던 메모")
    with webdav(share, PASSWORD) as server:
        target = target_of(server)
        made = make(target, PAIRS)
        target.close()
        login = Login(
            made.vault, WEBDAV, on_server(server), "stickle", PASSWORD, dict(made.keys.keys)
        )
        started = SyncRun(
            computer.db,
            computer.pc.path,
            DB_KEY,
            login,
            cloud_time(computer.db, computer.pc.clocks),
            computer.pc.manager,
        )
        told = unsent_of(started)
        started._look_for_unsent()  # pyright: ignore[reportPrivateUsage]
        started._noticing = True  # pyright: ignore[reportPrivateUsage]
        started._notice()  # pyright: ignore[reportPrivateUsage]
        assert told == []
        assert not started._next_unsent.isActive()  # pyright: ignore[reportPrivateUsage]
        started.runner.finish(0.0)
        computer.end()


def test_a_time_it_cannot_read_is_passed_over(
    bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = bed.first.live
    assert live is not None
    told = unsent_of(live)

    def not_a_time(_db: object, _vault: str) -> list[tuple[str, str]]:
        return [("x", "not a time")]

    monkeypatch.setattr("stickle.app.syncing.sync_state.changed_at", not_a_time)
    live._look_for_unsent()  # pyright: ignore[reportPrivateUsage]
    assert told == []


def test_a_secret_note_is_not_counted_among_them(bed: Bed, qtbot: QtBot) -> None:
    pc = bed.first.pc
    note = pc.notes.create("비밀로 할 메모")
    bed.first.round_over(qtbot)
    pc.notes.seal_in_place(note.id, "제목", b"sealed")
    assert sync_state.changed_at(bed.first.db, bed.login.vault) == []


def test_leaving_stops_looking(bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    live = bed.first.live
    assert live is not None
    monkeypatch.setattr("stickle.app.syncing.UNSENT_AFTER_MS", 30_000)
    bed.first.pc.notes.create("메모")
    live._look_for_unsent()  # pyright: ignore[reportPrivateUsage]
    assert live._next_unsent.isActive()  # pyright: ignore[reportPrivateUsage]
    live.leaving()
    assert not live._next_unsent.isActive()  # pyright: ignore[reportPrivateUsage]
    live._look_for_unsent()  # pyright: ignore[reportPrivateUsage]
    assert not live._next_unsent.isActive()  # pyright: ignore[reportPrivateUsage]
    live.runner.finish(0.0)


def test_ending_stops_looking(bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    live = bed.first.live
    assert live is not None
    monkeypatch.setattr("stickle.app.syncing.UNSENT_AFTER_MS", 30_000)
    bed.first.pc.notes.create("메모")
    live._look_for_unsent()  # pyright: ignore[reportPrivateUsage]
    live.finish(0.0)
    assert not live._next_unsent.isActive()  # pyright: ignore[reportPrivateUsage]


# Coming back


def test_the_others_stop_telling_of_it_and_it_keeps_its_name(bed: Bed, qtbot: QtBot) -> None:
    second = join(bed, qtbot)
    first_id = bed.first.device
    assert [o.name for o in others(second.db, bed.login.vault, second.device)] != []
    window = bed.first.page(qtbot)

    disconnect(window, qtbot, bed.first)
    second.round_over(qtbot)

    assert others(second.db, bed.login.vault, second.device) == []
    assert record_state.device_name(second.db, first_id) == this_device().name


def test_turned_on_again_nothing_is_doubled_and_what_it_missed_comes(
    bed: Bed, qtbot: QtBot
) -> None:
    second = join(bed, qtbot)
    pc = bed.first.pc
    both = pc.notes.create("둘 다 아는 메모")
    bed.first.round_over(qtbot)
    second.round_over(qtbot)
    window = bed.first.page(qtbot)
    disconnect(window, qtbot, bed.first)

    pc.notes.update_body(both.id, "끊은 동안 여기서 고침", keep_earlier=True)
    theirs = second.pc.notes.create("끊은 동안 저쪽에서 만듦")
    second.round_over(qtbot)

    bed.first.connect(bed.login, True)
    qtbot.waitUntil(lambda: pc.notes.get(theirs.id) is not None, timeout=MS)
    second.round_over(qtbot)

    assert len(pc.notes.all()) == 2
    got = second.pc.notes.get(both.id)
    assert got is not None and got.body == "끊은 동안 여기서 고침"
    assert bed.own_file(bed.first) in bed.files()
    assert len(second.pc.notes.all()) == 2
    assert [o.name for o in others(second.db, bed.login.vault, second.device)] != []


def test_what_shows_how_sync_stands_is_told_as_it_goes_on_and_off(bed: Bed, qtbot: QtBot) -> None:
    followed: list[object] = []

    class Display:
        def follow(self, syncing: object) -> None:
            followed.append(syncing)

    display = cast(SyncDisplay, Display())
    live = bed.first.live
    assert live is not None
    live.runner.finish(0.0)
    disconnect_sync(bed.first.db, live, bed.first.pc.clocks, display)
    assert followed == [None]
    pc = bed.first.pc
    started = connect_sync(
        pc.db, pc.path, DB_KEY, bed.login, True, pc.clocks, pc.manager, None, display
    )
    pc.started.append(started)
    assert followed == [None, started]


def test_turned_on_again_the_page_is_a_new_one(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    old = window.sync_panel
    disconnect(window, qtbot, bed.first)
    window.refresh()
    assert window.sync_panel is None

    bed.first.connect(bed.login, True)
    window.refresh()
    assert window.sync_panel is not None
    assert window.sync_panel is not old
    assert window.sync_panel.syncing is bed.first.live


def test_sync_replaced_by_another_has_a_page_of_its_own(bed: Bed, qtbot: QtBot) -> None:
    window = bed.first.page(qtbot)
    old = window.sync_panel
    live = bed.first.live
    assert live is not None
    live.runner.finish(0.0)
    bed.first.connect(bed.login, True)
    window.refresh()
    assert window.sync_panel is not old
    assert window.sync_panel is not None
    assert window.sync_panel.syncing is bed.first.live


def test_leaving_cut_short_leaves_it_on_at_the_next_start(bed: Bed, qtbot: QtBot) -> None:
    """Its own file gone, the connection kept: on again, and the file written anew."""
    live = bed.first.live
    assert live is not None
    live.leaving()
    live.leave()
    assert bed.own_file(bed.first) not in bed.files()
    assert sync_connection.current(bed.first.db) is not None
    bed.first.connect(bed.login, False)
    qtbot.waitUntil(lambda: bed.own_file(bed.first) in bed.files(), timeout=MS)


def test_its_own_file_goes_with_one_request_and_no_condition(bed: Bed, qtbot: QtBot) -> None:
    """A condition on a server's weak tag would never be met."""
    before = len(bed.server.asked)
    live = bed.first.live
    assert live is not None
    live.leaving()
    live.leave()
    asked = [(a.method, a.path) for a in bed.server.asked[before:]]
    own = "/" + "share/Stickle/" + bed.own_file(bed.first)
    assert [method for method, path in asked if path == own] == ["DELETE"]


def test_it_gives_up_as_soon_as_quitting_does(
    bed: Bed, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rounds are given as long as when the user quits, no longer."""
    live = bed.first.live
    assert live is not None
    asked: list[float] = []
    real = live.runner.finish

    def noted(seconds: float = 0.0) -> None:
        asked.append(seconds)
        real(seconds)

    monkeypatch.setattr(live.runner, "finish", noted)
    live.leaving()
    live.leave()
    assert asked == [QUIT_WAIT]


# The whole way through its windows


def test_turned_on_in_its_window_the_page_shows_it(
    qtbot: QtBot, tmp_path: Path, share: Path
) -> None:
    computer = Computer(make_pc(tmp_path, "pc"), Keys())
    window = computer.page(qtbot)
    assert window.sync_panel is None
    with webdav(share, PASSWORD) as server:
        window.turn_on_sync_button.click()
        opened = window.findChild(TurnOnSync)
        assert isinstance(opened, TurnOnSync)
        bench = Bench(opened, qtbot, [], FakeSyncing())
        bench.connect(on_server(server))
        bench.page(opened.absent_page)
        bench.idle()
        bench.press()
        bench.page(opened.last_page)
        qtbot.waitUntil(lambda: "On your other" in opened.last_line.text(), timeout=MS)
        opened.reject()
        qtbot.waitUntil(lambda: window.sync_panel is not None, timeout=MS)
        qtbot.waitUntil(panel_of(window).isVisible, timeout=MS)
        assert not window.turn_on_sync_button.isVisible()
        computer.end()


def test_a_job_lets_go_of_its_thread_before_it_says_so(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever that thread still held is let go of on it, before anything here goes."""
    let_go = threading.Event()
    joined: list[bool] = []
    seen: list[list[bool]] = []
    job = Job(lambda: let_go.wait(MS / 1000), lambda _outcome: seen.append(list(joined)))
    thread = job._thread  # pyright: ignore[reportPrivateUsage]
    real = thread.join

    def noted(timeout: float | None = None) -> None:
        joined.append(True)
        real(timeout)

    monkeypatch.setattr(thread, "join", noted)
    let_go.set()
    qtbot.waitUntil(lambda: seen != [], timeout=MS)
    assert seen == [[True]]
    assert not thread.is_alive()


REAL_MS = 60_000


@pytest.mark.skipif(not account_given(), reason="needs a real WebDAV server")
def test_on_a_real_server_it_shows_renames_leaves_and_comes_back(
    qtbot: QtBot, tmp_path: Path
) -> None:
    given = account()
    assert given is not None
    first = Computer(make_pc(tmp_path, "first"), Keys())
    second = Computer(make_pc(tmp_path, "second"), Keys())
    note = first.pc.notes.create("실제 서버의 메모")

    def asked_round(computer: Computer) -> None:
        computer.round_over(qtbot, REAL_MS)

    with folder_of_its_own() as target:
        address = target._address.text  # pyright: ignore[reportPrivateUsage]
        made = make(target, PAIRS)
        login = Login(made.vault, WEBDAV, address, given.user, given.password, dict(made.keys.keys))
        first.connect(login, False)
        qtbot.waitUntil(lambda: _synced(first), timeout=REAL_MS)
        second.connect(login, True)
        qtbot.waitUntil(lambda: _synced(second), timeout=REAL_MS)
        asked_round(first)

        window = first.page(qtbot)
        assert panel_of(window).status.text().startswith("Synced with WebDAV")
        assert panel_of(window).computers.count() == 2
        qtbot.wait(1100)  # a server's tags may be made of the second and the size
        dialog = rename_dialog(window)
        dialog.name.setText("거실 PC")
        dialog.ok_button.click()
        asked_round(first)
        asked_round(second)
        assert [o.name for o in others(second.db, made.vault, second.device)] == ["거실 PC"]

        pressed = press_disconnect(window)
        pressed.disconnect_button.click()
        qtbot.waitUntil(lambda: first.live is None, timeout=REAL_MS)
        assert sync_connection.current(first.db) is None
        asked_round(second)
        assert others(second.db, made.vault, second.device) == []
        got = second.pc.notes.get(note.id)
        assert got is not None and got.body == "실제 서버의 메모"

        first.connect(login, True)
        qtbot.waitUntil(lambda: _synced(first), timeout=REAL_MS)
        asked_round(first)
        asked_round(second)
        assert len(first.pc.notes.all()) == 1
        assert [o.name for o in others(second.db, made.vault, second.device)] == ["거실 PC"]
        first.end()
        second.end()
