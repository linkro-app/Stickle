"""Turning sync on with Dropbox: signed in to in the browser, or with the code
Dropbox shows pasted, as a test plays it against a Dropbox of its own
(dropbox_server)."""

# pyright: reportPrivateUsage=false
import gc
import http.client
import re
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest
from dropbox_server import (
    ACCOUNT,
    API,
    APP_KEY,
    AUTHORIZE,
    FIRST_REFRESH,
    HOST,
    Answer,
    DropboxState,
    dropbox_server,
)
from PySide6.QtCore import QCoreApplication, QEvent, QLocale, QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from stickle.app.recovery_key_dialog import RecoveryKeys
from stickle.app.sync_setup import SyncSetup
from stickle.app.syncing import Syncing
from stickle.app.turn_on_sync import TurnOnSync
from stickle.crypto import master, recovery
from stickle.crypto.sealed import make_sealed_slot
from stickle.crypto.vault_keys import MASTER_PASSWORD, RECOVERY_KEY
from stickle.data.sync_connection import DROPBOX, Login
from stickle.sync.targets.dropbox import App
from stickle.sync.targets.onedrive import App as OneDriveApp
from stickle.sync.vault import Vault, make, there

RECOVERY = recovery.generate()
DATABASE_KEY = bytes([7]) * 32
PAIRS = {
    RECOVERY_KEY: recovery.make_recovery_slot(DATABASE_KEY, RECOVERY, "2026-11-25T00:00:00Z"),
    MASTER_PASSWORD: make_sealed_slot(DATABASE_KEY, "a spare", 1, 8192, master.SLOT, None),
}
MS = 20_000
WRONG_CODE = "That code didn't sign you in. Check it, or choose Sign in with your browser… again."


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
    QLocale.setDefault(QLocale("en"))
    yield
    QLocale.setDefault(QLocale.system())


@pytest.fixture
def box() -> Iterator[DropboxState]:
    with dropbox_server() as serving:
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


@pytest.fixture
def held() -> Iterator[int]:
    """A port another program listens on, as when every registered one is taken."""
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        yield taken.getsockname()[1]


class FakeSyncing(QObject):
    moving = Signal(str, int, int)
    round_shown = Signal(object)


def ready_keys() -> RecoveryKeys:
    return RecoveryKeys(
        exists=lambda: True,
        make=lambda: RECOVERY,
        kept=lambda: None,
        was_kept=lambda: True,
        is_old=lambda: False,
        upgrade=lambda _typed: None,
        confirm=lambda _typed: None,
    )


def app_of(box: DropboxState) -> App:
    return App(APP_KEY, box.place, box.context)


def vault_there(box: DropboxState) -> Vault:
    """A Stickle folder in the app's folder, as another computer made it."""
    target = app_of(box).target(FIRST_REFRESH, lambda _token: None)
    try:
        return make(target, PAIRS)
    finally:
        target.close()


def allow_page(box: DropboxState, address: str) -> tuple[int, str, str]:
    """As a browser at the page to allow Stickle, the user allowing it: its
    status, where it sends the browser back to, and the page."""
    parts = urlsplit(address)
    assert parts.path == AUTHORIZE
    connection = http.client.HTTPSConnection(HOST, parts.port, context=box.context(), timeout=10)
    try:
        connection.request("GET", f"{parts.path}?{parts.query}")
        response = connection.getresponse()
        return response.status, response.getheader("Location") or "", response.read().decode()
    finally:
        connection.close()


def back(location: str) -> bytes:
    """As the browser sent back to the listener: the page it shows."""
    parts = urlsplit(location)
    assert parts.hostname == "127.0.0.1" and parts.port
    connection = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=10)
    try:
        connection.request("GET", f"/?{parts.query}", headers={"Host": parts.netloc})
        return connection.getresponse().read()
    finally:
        connection.close()


def shown_code(box: DropboxState, address: str) -> str:
    status, location, page = allow_page(box, address)
    assert status == 200 and not location
    found = re.search(r"<code>([^<]+)</code>", page)
    assert found is not None
    return found.group(1)


@dataclass
class Bench:
    window: TurnOnSync
    qtbot: QtBot
    box: DropboxState
    opened: list[str]
    logins: list[tuple[Login, bool]] = field(default_factory=list[tuple[Login, bool]])

    def press(self) -> None:
        assert self.window.next_button.isEnabled()
        self.window.next_button.click()

    def page(self, page: QWidget) -> None:
        self.qtbot.waitUntil(lambda: self.window.pages.current() is page, timeout=MS)

    def to_sign_in(self) -> None:
        self.press()
        self.window.dropbox_choice.setChecked(True)
        self.press()
        assert self.window.pages.current() is self.window.sign_in_page

    def sign_in(self) -> str:
        """[Sign in with your browser…]: where the browser was sent."""
        before = len(self.opened)
        self.press()
        assert len(self.opened) == before + 1
        return self.opened[-1]

    def signed_in(self) -> bytes:
        status, location, _page = allow_page(self.box, self.sign_in())
        assert status == 302
        return back(location)

    def entered(self, code: str) -> None:
        """Pasted, and [Connect]: its exchange over, and the window told."""
        self.window.code.setText(code)
        self.window.code_button.click()
        self.qtbot.waitUntil(lambda: self.window._entering is None, timeout=MS)

    def said(self) -> str:
        return self.window.message.text()


def make_bench(
    qtbot: QtBot,
    box: DropboxState,
    opened: list[str],
    *,
    offered: bool = True,
    onedrive: Callable[[], OneDriveApp | None] = lambda: None,
    notes_here: int = 3,
    again: Login | None = None,
    signed_out: bool = False,
) -> Bench:
    syncing = FakeSyncing()
    logins: list[tuple[Login, bool]] = []

    def connected(login: Login, joining: bool) -> Syncing:
        logins.append((login, joining))
        return cast(Syncing, syncing)

    app = app_of(box)
    setup = SyncSetup(
        lambda: None,
        ready_keys(),
        lambda: notes_here,
        lambda: PAIRS,
        connected,
        lambda: None,
        onedrive=onedrive,
        dropbox=(lambda: app) if offered else (lambda: None),
    )
    window = TurnOnSync(setup, None, again, signed_out)
    if again is None:  # else it may close by itself, and goes once closed
        qtbot.addWidget(window)
    window.show()
    return Bench(window, qtbot, box, opened, logins)


@pytest.fixture
def bench(qtbot: QtBot, box: DropboxState, opened: list[str]) -> Bench:
    return make_bench(qtbot, box, opened)


def kept_login(box: DropboxState, vault: Vault, refresh: str = FIRST_REFRESH) -> Login:
    return Login(vault.vault, DROPBOX, "", ACCOUNT, refresh, vault.keys.keys)


# Choosing it


def test_dropbox_comes_between_onedrive_and_webdav(
    qtbot: QtBot, box: DropboxState, opened: list[str]
) -> None:
    onedrive = OneDriveApp("a-client-of-a-test")
    bench = make_bench(qtbot, box, opened, onedrive=lambda: onedrive)
    window = bench.window
    bench.press()
    layout = window.choose_page.layout()
    assert layout is not None
    order = [layout.indexOf(each) for each in window._choices()]
    assert order == sorted(order)
    assert window.onedrive_choice.isChecked() and window.dropbox_choice.isVisibleTo(window)
    assert window.dropbox_choice.text() == "&Dropbox"
    assert window.own_folder_note.text() == (
        "In OneDrive and Dropbox, Stickle uses only its own folder and can't see your other files."
    )
    window.dropbox_choice.setChecked(True)
    bench.press()
    assert window.pages.current() is window.sign_in_page
    assert window.sign_in_heading.text() == "Dropbox"


def test_alone_it_is_chosen_first(bench: Bench) -> None:
    window = bench.window
    bench.press()
    assert not window.onedrive_choice.isVisibleTo(window)
    assert window.dropbox_choice.isChecked()
    assert window.own_folder_note.text() == (
        "In Dropbox, Stickle uses only its own folder and can't see your other files."
    )
    bench.press()
    assert window.pages.current() is window.sign_in_page


def test_without_stickle_registered_it_is_not_offered(
    qtbot: QtBot, box: DropboxState, opened: list[str]
) -> None:
    bench = make_bench(qtbot, box, opened, offered=False)
    window = bench.window
    bench.press()
    assert not window.dropbox_choice.isVisibleTo(window)
    assert not window.own_folder_note.isVisibleTo(window)
    assert window.webdav_choice.isChecked()


# Signing in


def test_the_browser_goes_to_dropbox_in_the_apps_language(bench: Bench) -> None:
    bench.to_sign_in()
    address = bench.sign_in()
    asked = {name: values[0] for name, values in parse_qs(urlsplit(address).query).items()}
    assert urlsplit(address).path == AUTHORIZE
    assert asked["client_id"] == APP_KEY and asked["locale"] == "en"
    assert asked["force_reapprove"] == "true" and "force_reauthentication" not in asked
    window = bench.window
    for part in (window.sign_in_waiting, window.other_account_note, window.copy_button):
        assert part.isVisibleTo(window)
    assert window.other_account_note.text() == (
        "If the page shows a different Dropbox account, sign out on the Dropbox website"
        " first, then choose Sign in with your browser… again."
    )
    assert not window.code.isVisibleTo(window)  # the browser brings the code back
    assert not bench.box.requests(API)  # nothing asked of Dropbox before signing in
    assert not bench.logins


def test_signed_in_it_makes_the_folder_and_keeps_the_refresh_token(bench: Bench) -> None:
    bench.to_sign_in()
    page = bench.signed_in()
    assert b"signed in" in page and b'lang="en"' in page
    window = bench.window
    bench.page(window.absent_page)
    assert "Dropbox" in window.absent_text.text()
    assert window.sign_in_waiting.isHidden() and window.other_account_note.isHidden()
    bench.press()
    bench.qtbot.waitUntil(lambda: bool(bench.logins), timeout=MS)
    login, joining = bench.logins[0]
    assert not joining
    assert login.kind == DROPBOX and login.address == "" and login.user == ACCOUNT
    assert login.secret in bench.box.refresh_tokens and login.secret != FIRST_REFRESH
    target = app_of(bench.box).target(login.secret, lambda _token: None)
    try:
        assert there(target) == login.vault
    finally:
        target.close()


def test_a_folder_there_is_joined_with_the_recovery_key(
    qtbot: QtBot, box: DropboxState, opened: list[str]
) -> None:
    vault = vault_there(box)
    bench = make_bench(qtbot, box, opened, notes_here=0)
    window = bench.window
    bench.to_sign_in()
    bench.signed_in()
    bench.page(window.present_page)
    assert "Dropbox" in window.present_text.text()
    window.way_in.setText(RECOVERY)
    bench.press()
    qtbot.waitUntil(lambda: bool(bench.logins), timeout=MS)
    login, joining = bench.logins[0]
    assert joining and login.vault == vault.vault and login.kind == DROPBOX


def test_said_no_to_on_the_page_it_says_so(bench: Bench) -> None:
    bench.box.declining = True
    bench.to_sign_in()
    bench.signed_in()
    bench.qtbot.waitUntil(lambda: bool(bench.said()), timeout=MS)
    assert bench.said() == (
        "Stickle wasn't allowed, so sign-in didn't finish. To sync, sign in again and allow"
        " Stickle."
    )
    assert bench.window.pages.current() is bench.window.sign_in_page
    assert not bench.logins


@pytest.mark.parametrize("declining", [False, True], ids=["allowed", "said-no"])
def test_back_from_the_browser_the_window_is_brought_forward(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, declining: bool
) -> None:
    # Its page says to go back to Stickle, whose window was behind the browser.
    asked: list[str] = []
    monkeypatch.setattr(bench.window, "activateWindow", lambda: asked.append("activate"))
    bench.box.declining = declining
    bench.to_sign_in()
    bench.sign_in()
    assert not asked  # not while the browser is in use

    status, location, _page = allow_page(bench.box, bench.opened[-1])
    assert status == 302
    back(location)
    bench.qtbot.waitUntil(lambda: bool(asked), timeout=MS)
    assert asked == ["activate"]


def test_dropbox_down_once_signed_in_is_said_by_name(bench: Bench) -> None:
    bench.box.answers += [Answer(500, body=b"down") for _ in range(6)]
    bench.to_sign_in()
    bench.signed_in()
    bench.qtbot.waitUntil(
        lambda: bench.window._job is None and bench.window._signing is None and bool(bench.said()),
        timeout=MS,
    )
    assert bench.said() == "Couldn't reach Dropbox. Check your network and try again."


# Every registered port taken: the code pasted


def test_with_every_port_taken_the_code_shown_is_pasted(
    qtbot: QtBot, box: DropboxState, opened: list[str], held: int
) -> None:
    box.redirect_ports = (held,)
    bench = make_bench(qtbot, box, opened)
    window = bench.window
    bench.to_sign_in()
    address = bench.sign_in()
    assert "redirect_uri" not in parse_qs(urlsplit(address).query)
    for part in (window.paste_line, window.code_label, window.code, window.code_button):
        assert part.isVisibleTo(window)
    assert window.paste_line.text() == "Paste the code shown on the Dropbox page."
    assert window.focusWidget() is window.code
    assert not window.code_button.isEnabled()  # nothing pasted yet
    code = shown_code(box, address)

    bench.entered("not a code")  # sent nowhere
    assert bench.said() == WRONG_CODE and window.focusWidget() is window.code
    bench.entered("a-code-dropbox-never-gave")
    assert bench.said() == WRONG_CODE
    assert window._signing is not None and window.code.isVisibleTo(window)  # it goes on

    window.code.setText(code)
    QTest.keyClick(window.code, Qt.Key.Key_Return)  # Enter there enters it
    bench.page(window.absent_page)
    assert len(opened) == 1  # and did not sign in anew
    assert not window.code.text()  # not left about
    bench.press()
    qtbot.waitUntil(lambda: bool(bench.logins), timeout=MS)
    assert bench.logins[0][0].kind == DROPBOX


def test_a_code_dropbox_will_not_take_from_stickle_says_it_is_blocked(
    qtbot: QtBot, box: DropboxState, opened: list[str], held: int
) -> None:
    box.redirect_ports = (held,)
    bench = make_bench(qtbot, box, opened)
    bench.to_sign_in()
    code = shown_code(box, bench.sign_in())
    box.app_turned_away = True
    bench.entered(code)
    assert bench.said() == (
        "Dropbox has blocked Stickle. Until a new version fixes this, you can use another cloud."
    )


def test_signing_in_anew_lets_the_code_go(
    qtbot: QtBot, box: DropboxState, opened: list[str], held: int
) -> None:
    box.redirect_ports = (held,)
    bench = make_bench(qtbot, box, opened)
    window = bench.window
    bench.to_sign_in()
    first = bench.sign_in()
    window.code.setText("typed, then let go of")
    window.back_button.click()
    assert not window.code.text() and window._signing is None
    window.next_button.click()
    assert window.pages.current() is window.sign_in_page
    assert bench.sign_in() != first


# Going on again


def test_signed_out_it_starts_at_signing_in_and_sends_nothing(
    qtbot: QtBot, box: DropboxState, opened: list[str]
) -> None:
    vault = vault_there(box)
    asked = len(box.asked)
    bench = make_bench(qtbot, box, opened, again=kept_login(box, vault), signed_out=True)
    window = bench.window
    assert window.pages.current() is window.sign_in_page
    assert window.sign_in_heading.text() == "Dropbox"
    assert not window.back_button.isVisible()
    assert len(box.asked) == asked


def test_as_kept_it_goes_on_with_the_same_folder(
    qtbot: QtBot, box: DropboxState, opened: list[str]
) -> None:
    vault = vault_there(box)
    bench = make_bench(qtbot, box, opened, again=kept_login(box, vault))
    qtbot.waitUntil(lambda: bool(bench.logins), timeout=MS)
    login, joining = bench.logins[0]
    assert not joining and login.vault == vault.vault and login.secret == FIRST_REFRESH


def test_turned_down_on_the_way_it_signs_in_anew(
    qtbot: QtBot, box: DropboxState, opened: list[str]
) -> None:
    vault = vault_there(box)
    box.refusing_refresh = True
    bench = make_bench(qtbot, box, opened, again=kept_login(box, vault))
    window = bench.window
    qtbot.waitUntil(lambda: window._job is None and bool(bench.said()), timeout=MS)
    assert window.pages.current() is window.sign_in_page
    assert bench.said() == "Sign-in didn't finish. Try again."


def test_a_build_without_stickle_registered_cannot_sign_in_again(
    qtbot: QtBot, box: DropboxState, opened: list[str]
) -> None:
    vault = vault_there(box)
    bench = make_bench(
        qtbot, box, opened, offered=False, again=kept_login(box, vault), signed_out=True
    )
    assert bench.window.pages.current() is bench.window.sign_in_page
    assert not bench.window.next_button.isEnabled()
