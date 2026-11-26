"""Turning sync on with OneDrive: signed in to in the browser, as a test plays it
against a Graph and a sign-in of its own (graph_server)."""

# pyright: reportPrivateUsage=false
import gc
import socket
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest
from graph_server import (
    ACCOUNT,
    AUTHORIZE_PATH,
    CLIENT_ID,
    FIRST_REFRESH,
    Answer,
    Graph,
    back,
    graph_server,
    to_sign_in_page,
)
from PySide6.QtCore import QCoreApplication, QEvent, QLocale, QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot
from update_translations import TS_DIR, compile_to

from stickle.app.i18n import Translations
from stickle.app.recovery_key_dialog import RecoveryKeys
from stickle.app.sync_setup import SyncSetup
from stickle.app.syncing import Syncing
from stickle.app.turn_on_sync import TurnOnSync
from stickle.crypto import master, recovery
from stickle.crypto.sealed import make_sealed_slot
from stickle.crypto.vault_keys import MASTER_PASSWORD, RECOVERY_KEY
from stickle.data.sync_connection import ONEDRIVE, Login
from stickle.sync.targets import sign_in as sign_in_module
from stickle.sync.targets.onedrive import App
from stickle.sync.vault import Vault, make, there

RECOVERY = recovery.generate()
DATABASE_KEY = bytes([7]) * 32
PAIRS = {
    RECOVERY_KEY: recovery.make_recovery_slot(DATABASE_KEY, RECOVERY, "2026-11-22T00:00:00Z"),
    MASTER_PASSWORD: make_sealed_slot(DATABASE_KEY, "a spare", 1, 8192, master.SLOT, None),
}
MS = 20_000
DECLINED = (
    "Stickle wasn't allowed, so sign-in didn't finish. To sync, sign in again and allow Stickle."
)


@pytest.fixture(autouse=True)
def windows_deleted() -> Iterator[None]:
    yield
    for window in QApplication.topLevelWidgets():
        if isinstance(window, TurnOnSync) and window.isVisible():
            window.reject()  # one going on again, left open
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.enable()
    gc.collect()


@pytest.fixture(autouse=True)
def in_english() -> Iterator[None]:
    """The app in English, as the window's words are without a translation."""
    QLocale.setDefault(QLocale("en"))
    yield
    QLocale.setDefault(QLocale.system())


@pytest.fixture
def graph() -> Iterator[Graph]:
    with graph_server() as serving:
        yield serving


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What the browser was sent to, the browser left alone."""
    sent: list[str] = []

    def open_url(url: QUrl) -> bool:
        sent.append(bytes(url.toEncoded().data()).decode("ascii"))
        return True

    monkeypatch.setattr(QDesktopServices, "openUrl", open_url)
    return sent


class FakeSyncing(QObject):
    moving = Signal(str, int, int)
    round_shown = Signal(object)


def ready_keys() -> RecoveryKeys:
    """A recovery key made and kept: nothing in the way of starting."""
    return RecoveryKeys(
        exists=lambda: True,
        make=lambda: RECOVERY,
        kept=lambda: None,
        was_kept=lambda: True,
        is_old=lambda: False,
        upgrade=lambda _typed: None,
        confirm=lambda _typed: None,
    )


def port_of(address: str) -> int:
    """The listener's port, as the browser is told to come back to it."""
    redirect = parse_qs(urlsplit(address).query)["redirect_uri"][0]
    port = urlsplit(redirect).port
    assert port is not None
    return port


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def app_of(graph: Graph) -> App:
    return App(CLIENT_ID, graph.place, graph.context)


def vault_there(graph: Graph) -> Vault:
    """A Stickle folder in the app's folder, as another computer made it."""
    target = app_of(graph).target(FIRST_REFRESH, lambda _token: None)
    try:
        return make(target, PAIRS)
    finally:
        target.close()


@dataclass
class Bench:
    window: TurnOnSync
    qtbot: QtBot
    graph: Graph
    opened: list[str]
    logins: list[tuple[Login, bool]] = field(default_factory=list[tuple[Login, bool]])

    def press(self) -> None:
        assert self.window.next_button.isEnabled()
        self.window.next_button.click()

    def page(self, page: QWidget) -> None:
        self.qtbot.waitUntil(lambda: self.window.pages.current() is page, timeout=MS)

    def to_sign_in(self) -> None:
        self.press()
        self.press()
        assert self.window.pages.current() is self.window.sign_in_page

    def sign_in(self) -> str:
        """[Sign in with your browser…]: where the browser was sent."""
        before = len(self.opened)
        self.press()
        assert len(self.opened) == before + 1
        return self.opened[-1]

    def come_back(self, address: str) -> bytes:
        """As the browser at Microsoft's page, sent back to Stickle: the page it shows."""
        _status, _headers, page = back(to_sign_in_page(self.graph, address))
        return page

    def signed_in(self) -> bytes:
        return self.come_back(self.sign_in())

    def ended(self) -> None:
        """The sign-in waited for is over, and the window has heard of it."""
        self.qtbot.waitUntil(lambda: self.window._signing is None, timeout=MS)

    def idle(self) -> None:
        self.qtbot.waitUntil(lambda: self.window._job is None, timeout=MS)

    def said(self) -> str:
        return self.window.message.text()


def make_bench(
    qtbot: QtBot,
    graph: Graph,
    opened: list[str],
    *,
    offered: bool = True,
    notes_here: int = 3,
    again: Login | None = None,
    signed_out: bool = False,
) -> Bench:
    syncing = FakeSyncing()
    logins: list[tuple[Login, bool]] = []

    def connected(login: Login, joining: bool) -> Syncing:
        logins.append((login, joining))
        return cast(Syncing, syncing)

    app = app_of(graph)
    setup = SyncSetup(
        lambda: None,
        ready_keys(),
        lambda: notes_here,
        lambda: PAIRS,
        connected,
        lambda _left: None,
        onedrive=(lambda: app) if offered else (lambda: None),
    )
    window = TurnOnSync(setup, None, again, signed_out)
    if again is None:  # else it may close by itself, and goes once closed
        qtbot.addWidget(window)
    window.show()
    return Bench(window, qtbot, graph, opened, logins)


@pytest.fixture
def bench(qtbot: QtBot, graph: Graph, opened: list[str]) -> Bench:
    return make_bench(qtbot, graph, opened)


# Choosing it


def test_onedrive_is_offered_first_and_said_to_see_only_its_folder(bench: Bench) -> None:
    window = bench.window
    bench.press()
    assert window.pages.current() is window.choose_page
    assert window.onedrive_choice.isVisibleTo(window) and window.onedrive_choice.isChecked()
    assert window.own_folder_note.isVisibleTo(window)
    assert window.own_folder_note.text() == (
        "In OneDrive, Stickle uses only its own folder and can't see your other files."
    )
    bench.press()
    assert window.pages.current() is window.sign_in_page
    assert window.next_button.text() == "&Sign in with your browser…"
    assert window.sign_in_waiting.isHidden() and window.copy_button.isHidden()
    window.back_button.click()
    window.webdav_choice.setChecked(True)
    bench.press()
    assert window.pages.current() is window.server_page


def test_without_stickle_registered_only_webdav_is_offered(
    qtbot: QtBot, graph: Graph, opened: list[str]
) -> None:
    bench = make_bench(qtbot, graph, opened, offered=False)
    window = bench.window
    bench.press()
    assert not window.onedrive_choice.isVisibleTo(window)
    assert not window.own_folder_note.isVisibleTo(window)
    assert window.webdav_choice.isChecked()
    bench.press()
    assert window.pages.current() is window.server_page


# Signing in


def test_the_browser_goes_to_microsoft_and_the_window_waits(bench: Bench) -> None:
    bench.to_sign_in()
    address = bench.sign_in()
    parts = urlsplit(address)
    asked = parse_qs(parts.query)
    assert parts.path == AUTHORIZE_PATH
    assert asked["client_id"] == [CLIENT_ID] and asked["prompt"] == ["select_account"]
    window = bench.window
    for part in (window.sign_in_waiting, window.copy_line, window.copy_button):
        assert part.isVisibleTo(window)
    assert window.sign_in_waiting.text() == (
        "Sign in on the page that opened in your browser and allow Stickle. When you're"
        " done, you'll continue in this window."
    )
    assert window.back_button.isEnabled() and window.copy_button.isEnabled()
    assert listening(port_of(address))
    assert not bench.graph.graph_requests()  # nothing asked of Graph before signing in
    assert not bench.logins


def test_signed_in_it_makes_the_folder_and_keeps_the_newest_refresh_token(
    bench: Bench,
) -> None:
    bench.to_sign_in()
    address = bench.sign_in()
    page = bench.come_back(address)
    assert b"You&#x27;re signed in." in page or b"You're signed in." in page
    assert b'lang="en"' in page
    window = bench.window
    bench.page(window.absent_page)
    assert not listening(port_of(address))
    assert "OneDrive" in window.absent_text.text()
    assert window.sign_in_waiting.isHidden()
    bench.press()
    bench.qtbot.waitUntil(lambda: bool(bench.logins), timeout=MS)
    login, joining = bench.logins[0]
    assert not joining
    assert login.kind == ONEDRIVE and login.address == "" and login.user == ACCOUNT
    # Renewed on the way (the window's first request to Graph): the last is kept.
    assert len(bench.graph.issued_refresh) >= 2
    assert login.secret == bench.graph.issued_refresh[-1]
    target = app_of(bench.graph).target(login.secret, lambda _token: None)
    try:
        assert there(target) == login.vault
    finally:
        target.close()


def test_a_folder_there_is_joined_with_the_recovery_key(
    qtbot: QtBot, graph: Graph, opened: list[str]
) -> None:
    vault = vault_there(graph)
    bench = make_bench(qtbot, graph, opened, notes_here=0)
    window = bench.window
    bench.to_sign_in()
    bench.signed_in()
    bench.page(window.present_page)
    assert "OneDrive" in window.present_text.text()
    window.way_in.setText(RECOVERY)
    bench.press()
    qtbot.waitUntil(lambda: bool(bench.logins), timeout=MS)
    login, joining = bench.logins[0]
    assert joining and login.vault == vault.vault and login.kind == ONEDRIVE
    assert login.secret == graph.issued_refresh[-1]


def test_an_account_unnamed_is_kept_without_a_name(
    qtbot: QtBot, graph: Graph, opened: list[str]
) -> None:
    graph.claims = None  # no ID token
    bench = make_bench(qtbot, graph, opened)
    bench.to_sign_in()
    bench.signed_in()
    bench.page(bench.window.absent_page)
    bench.press()
    qtbot.waitUntil(lambda: bool(bench.logins), timeout=MS)
    assert bench.logins[0][0].user == ""


def test_saying_no_to_stickle_is_said_and_keeps_nothing(bench: Bench) -> None:
    bench.graph.declining = True
    bench.to_sign_in()
    address = bench.sign_in()
    page = bench.come_back(address)
    assert b"Sign-in didn" in page
    bench.ended()
    window = bench.window
    assert window.pages.current() is window.sign_in_page
    assert bench.said() == DECLINED
    assert window.sign_in_waiting.isHidden() and window.copy_button.isHidden()
    assert not listening(port_of(address))
    assert not bench.logins and not bench.graph.graph_requests()
    again = bench.sign_in()  # and once more, anew
    assert parse_qs(urlsplit(again).query)["state"] != parse_qs(urlsplit(address).query)["state"]
    assert bench.said() == ""


def test_signing_in_again_lets_the_first_one_go(bench: Bench) -> None:
    bench.to_sign_in()
    first = bench.sign_in()
    second = bench.sign_in()  # the first tab closed, say
    assert not listening(port_of(first))
    with pytest.raises(OSError):
        back(to_sign_in_page(bench.graph, first))
    bench.come_back(second)
    bench.page(bench.window.absent_page)


@pytest.mark.parametrize("leave", ["back", "cancel"])
def test_leaving_the_step_while_waiting_closes_the_listener(bench: Bench, leave: str) -> None:
    bench.to_sign_in()
    address = bench.sign_in()
    window = bench.window
    if leave == "back":
        window.back_button.click()
        assert window.pages.current() is window.choose_page
    else:
        window.reject()
    assert not listening(port_of(address))
    with pytest.raises(OSError):
        back(to_sign_in_page(bench.graph, address))
    assert not bench.logins


def test_the_address_can_be_copied_for_a_browser_that_did_not_open(bench: Bench) -> None:
    bench.to_sign_in()
    address = bench.sign_in()
    bench.window.copy_button.click()
    clipboard = QGuiApplication.clipboard()
    assert clipboard.text() == address
    clipboard.clear()


def test_microsoft_blocking_stickle_is_said(bench: Bench) -> None:
    bench.graph.app_turned_away = True
    bench.to_sign_in()
    bench.signed_in()
    bench.ended()
    assert bench.said() == (
        "Microsoft has blocked Stickle. Until a new version fixes this, you can use another cloud."
    )
    assert not bench.logins


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (
            Answer(429, {"Retry-After": "30"}),
            "Microsoft isn't taking requests right now. Try again in a little while.",
        ),
        (Answer(502), "Couldn't reach Microsoft. Check your network and try again."),
        (Answer(400, body={"error": "invalid_grant"}), "Sign-in didn't finish. Try again."),
    ],
)
def test_what_went_wrong_at_the_exchange_is_said(bench: Bench, answer: Answer, said: str) -> None:
    bench.graph.token_answers.append(answer)
    bench.to_sign_in()
    bench.signed_in()
    bench.ended()
    assert bench.said() == said
    assert bench.window.pages.current() is bench.window.sign_in_page
    assert not bench.logins


def test_no_listener_to_open_is_said(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    def refused() -> list[object]:
        raise OSError("no port")

    monkeypatch.setattr(sign_in_module, "listen", refused)
    bench.to_sign_in()
    bench.press()
    assert not bench.opened
    assert bench.said() == "Couldn't start signing in. Try again."
    assert bench.window.sign_in_waiting.isHidden()


def test_onedrive_not_answering_after_signing_in_is_said(bench: Bench) -> None:
    bench.to_sign_in()
    address = bench.sign_in()
    bench.graph.answers.extend([Answer(500)] * 8)
    bench.come_back(address)
    bench.ended()
    bench.idle()
    assert bench.said() == "Couldn't reach OneDrive. Check your network and try again."
    assert not bench.logins


def test_a_refresh_token_turned_down_after_signing_in_leads_back_to_signing_in(
    bench: Bench,
) -> None:
    bench.to_sign_in()
    address = bench.sign_in()
    bench.graph.refusing_refresh = True  # the code is still exchanged; its token, not
    bench.come_back(address)
    bench.ended()
    bench.idle()
    assert bench.window.pages.current() is bench.window.sign_in_page
    assert bench.said() == "Sign-in didn't finish. Try again."
    assert not bench.logins


# Going on again, as kept


def kept_login(vault: Vault, secret: str = FIRST_REFRESH) -> Login:
    return Login(vault.vault, ONEDRIVE, "", ACCOUNT, secret, vault.keys.keys)


def test_going_on_again_looks_with_the_token_kept(
    qtbot: QtBot, graph: Graph, opened: list[str]
) -> None:
    vault = vault_there(graph)
    graph.issued_refresh.clear()
    bench = make_bench(qtbot, graph, opened, again=kept_login(vault))
    qtbot.waitUntil(lambda: bool(bench.logins), timeout=MS)
    login, joining = bench.logins[0]
    assert not joining and login.vault == vault.vault and login.user == ACCOUNT
    assert login.secret == graph.issued_refresh[-1]  # renewed on the way
    assert not opened  # no sign-in


def test_going_on_again_with_the_folder_gone_makes_a_new_one(
    qtbot: QtBot, graph: Graph, opened: list[str]
) -> None:
    vault = vault_there(graph)
    graph.files.clear()
    bench = make_bench(qtbot, graph, opened, again=kept_login(vault))
    window = bench.window
    bench.page(window.absent_page)
    assert "OneDrive" in window.absent_text.text()
    assert window.absent_text.text().startswith("Stickle will make a new Stickle folder")
    assert not opened  # no sign-in


def test_signed_out_it_signs_in_anew_sending_nothing_before(
    qtbot: QtBot, graph: Graph, opened: list[str]
) -> None:
    vault = vault_there(graph)
    graph.asked.clear()
    bench = make_bench(
        qtbot, graph, opened, again=kept_login(vault, "turned down"), signed_out=True
    )
    window = bench.window
    assert window.pages.current() is window.sign_in_page
    assert not window.back_button.isVisible()
    assert not graph.asked  # the token turned down is not sent again
    bench.signed_in()
    qtbot.waitUntil(lambda: bool(bench.logins), timeout=MS)
    login, _joining = bench.logins[0]
    assert login.vault == vault.vault and login.secret == graph.issued_refresh[-1]
    assert all(b"turned down" not in asked.body for asked in graph.asked)


def test_the_browser_is_told_in_the_apps_language(
    qtbot: QtBot, graph: Graph, opened: list[str], tmp_path: Path
) -> None:
    compile_to(TS_DIR / "stickle_ko.ts", tmp_path / "stickle_ko.qm")
    translations = Translations(tmp_path)
    translations.apply("ko")
    try:
        bench = make_bench(qtbot, graph, opened)
        bench.to_sign_in()
        page = bench.signed_in().decode("utf-8")
        assert '<html lang="ko">' in page
        assert "로그인이 끝났습니다. 이 탭을 닫고 Stickle로 돌아가세요." in page
        graph.declining = True
        bench.page(bench.window.absent_page)
        bench.window.back_button.click()
        page = bench.signed_in().decode("utf-8")
        assert "로그인을 마치지 못했습니다. Stickle로 돌아가 다시 해 보세요." in page
    finally:
        translations.apply("en")
