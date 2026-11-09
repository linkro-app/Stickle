"""Turning sync on, in its window, against a WebDAV server of the test's own: from
the first step to the first round, and every way it can stop on the way. Nothing
is kept before the Stickle folder is made or opened, and a password goes only to
a server the user chose to trust."""

import gc
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtWidgets import QLineEdit, QWidget
from pytestqt.qtbot import QtBot
from real_server import account, account_given, folder_of_its_own
from webdav_server import HOST, Server, self_signed, webdav

from stickle.app.application import connect_sync
from stickle.app.background import Job
from stickle.app.i18n import Translations
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.recovery_key_dialog import EnterRecoveryKeyDialog, RecoveryKeyDialog, RecoveryKeys
from stickle.app.settings_window import SettingsWindow
from stickle.app.support import site_language
from stickle.app.sync_setup import SyncGuideLine, SyncSetup
from stickle.app.syncing import Ended, Syncing
from stickle.app.turn_on_sync import TurnOnSync, shown_fingerprint
from stickle.core.clock import Clocks
from stickle.crypto import master, recovery
from stickle.crypto.recovery import RecoveryKeyTypoError, WrongRecoveryKeyError
from stickle.crypto.sealed import SealedSlot, make_sealed_slot
from stickle.crypto.vault_keys import MASTER_PASSWORD, RECOVERY_KEY
from stickle.data import sync_connection, sync_state
from stickle.data.colors import ColorRepository
from stickle.data.labels import LabelRepository
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import Settings
from stickle.data.sync_connection import WEBDAV, Login
from stickle.data.versions import VersionRepository
from stickle.sync.engine import GETTING, PUTTING, Report
from stickle.sync.format import VAULT_FILE, VaultInfo, json_bytes, read_vault_file, vault_file
from stickle.sync.outcome import Outcome, Stopped
from stickle.sync.targets.webdav import WebDav, parse_address
from stickle.sync.vault import make

PASSWORD = "correct horse battery staple"
SPARE = "a spare password"
RECOVERY = recovery.generate()
DATABASE_KEY = bytes([7]) * 32
PAIRS = {
    RECOVERY_KEY: recovery.make_recovery_slot(DATABASE_KEY, RECOVERY, "2026-11-09T00:00:00Z"),
    MASTER_PASSWORD: make_sealed_slot(DATABASE_KEY, SPARE, 1, 8192, master.SLOT, None),
}
DB_KEY = bytes([9]) * 32
VAULT_ID = "0a1b2c3d-1111-4222-8333-444455556677"
MS = 20_000


@pytest.fixture(autouse=True)
def windows_deleted() -> Iterator[None]:
    yield
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.enable()
    gc.collect()  # here, and not on some thread of the next tests' servers


@pytest.fixture
def share(tmp_path: Path) -> Path:
    root = tmp_path / "dav"
    (root / "share").mkdir(parents=True)
    return root


class FakeSyncing(QObject):
    moving = Signal(str, int, int)
    round_shown = Signal(object)


@dataclass
class Keys:
    """A recovery key as the window finds it."""

    there: bool = True
    old: bool = False
    held: bool = True
    made: int = 0
    typed: str | None = None

    def exists(self) -> bool:
        return self.there

    def is_old(self) -> bool:
        return self.old

    def was_kept(self) -> bool:
        return self.held

    def make(self) -> str:
        self.made += 1
        self.there, self.old, self.held = True, False, False
        return RECOVERY

    def kept(self) -> None:
        self.held = True

    def type_in(self, typed: str) -> None:
        if typed == "typo":
            raise RecoveryKeyTypoError("not as written")
        if typed != RECOVERY:
            raise WrongRecoveryKeyError("not this one")
        self.typed, self.old, self.held = typed, False, True

    def keys(self) -> RecoveryKeys:
        return RecoveryKeys(
            exists=self.exists,
            make=self.make,
            kept=self.kept,
            was_kept=self.was_kept,
            is_old=self.is_old,
            upgrade=self.type_in,
            confirm=self.type_in,
        )


@dataclass
class Bench:
    window: TurnOnSync
    qtbot: QtBot
    logins: list[tuple[Login, bool]]
    syncing: FakeSyncing

    def page(self, page: QWidget) -> None:
        self.qtbot.waitUntil(lambda: self.window.pages.current() is page, timeout=MS)

    def idle(self) -> None:
        self.qtbot.waitUntil(lambda: self.window.back_button.isEnabled(), timeout=MS)

    def press(self) -> None:
        assert self.window.next_button.isEnabled()
        self.window.next_button.click()

    def to_server_page(self) -> None:
        self.press()
        self.press()
        assert self.window.pages.current() is self.window.server_page

    def fill(self, address: str, password: str = PASSWORD, user: str = "stickle") -> None:
        self.window.address.setText(address)
        self.window.user.setText(user)
        self.window.password.setText(password)

    def connect(self, address: str, password: str = PASSWORD) -> None:
        self.to_server_page()
        self.fill(address, password)
        self.press()

    def said(self) -> str:
        return self.window.message.text()


@pytest.fixture
def keys() -> Keys:
    return Keys()


@pytest.fixture
def bench(qtbot: QtBot, keys: Keys) -> Bench:
    return make_bench(qtbot, keys)


def make_bench(
    qtbot: QtBot,
    keys: Keys,
    connect: Callable[[Login, bool], object] | None = None,
    sealed_for: Mapping[str, SealedSlot] = PAIRS,
) -> Bench:
    syncing = FakeSyncing()
    logins: list[tuple[Login, bool]] = []

    def connected(login: Login, joining: bool) -> Syncing:
        logins.append((login, joining))
        if connect is not None:
            connect(login, joining)
        return cast(Syncing, syncing)

    setup = SyncSetup(lambda: False, keys.keys(), lambda: 3, lambda: sealed_for, connected)
    window = TurnOnSync(setup)
    qtbot.addWidget(window)
    window.show()
    return Bench(window, qtbot, logins, syncing)


def on_server(server: Server, place: str = "share") -> str:
    return f"http://{HOST}:{server.port}/{place}"


def target_of(server: Server, password: str = PASSWORD) -> WebDav:
    return WebDav(parse_address(on_server(server)), "stickle", password)


# Where it is turned on


def settings_with(qtbot: QtBot, setup: SyncSetup | None) -> SettingsWindow:
    window = SettingsWindow(NoteManager(), Translations(), sync_setup=setup)
    qtbot.addWidget(window)
    window.show()
    window.tabs.setCurrentWidget(window.connections_page)
    return window


def test_turning_on_is_offered_until_it_is_on(qtbot: QtBot, keys: Keys) -> None:
    on = [False]

    def connect(_login: Login, _joining: bool) -> Syncing:
        raise AssertionError("not connected in this test")

    window = settings_with(
        qtbot, SyncSetup(lambda: on[0], keys.keys(), lambda: 3, lambda: PAIRS, connect)
    )
    assert window.turn_on_sync_button.isVisible()
    assert window.sync_guide.isVisible()
    on[0] = True
    window.refresh()
    assert not window.turn_on_sync_button.isVisible()
    assert window.sync_guide.isVisible()


def test_without_a_way_to_turn_it_on_nothing_is_offered(qtbot: QtBot) -> None:
    window = settings_with(qtbot, None)
    assert not window.turn_on_sync_button.isVisible()
    assert not window.sync_guide.isVisible()


def test_the_button_opens_its_window_once(qtbot: QtBot, keys: Keys) -> None:
    def connect(_login: Login, _joining: bool) -> Syncing:
        raise AssertionError("not connected in this test")

    window = settings_with(
        qtbot, SyncSetup(lambda: False, keys.keys(), lambda: 3, lambda: PAIRS, connect)
    )
    window.turn_on_sync_button.click()
    opened = window.findChild(TurnOnSync)
    assert isinstance(opened, TurnOnSync)
    assert opened.isVisible() and opened.isModal()
    opened.reject()


def test_the_guide_goes_to_its_own_page(qtbot: QtBot) -> None:
    line = SyncGuideLine()
    qtbot.addWidget(line)
    assert f"https://stickle.linkro.co/{site_language()}/guide/sync" in line.link.text()


# The first steps


def test_it_goes_forward_and_back_through_the_first_steps(bench: Bench) -> None:
    window = bench.window
    assert window.pages.current() is window.intro_page
    assert not window.back_button.isVisible()
    bench.press()
    assert window.pages.current() is window.choose_page
    assert window.webdav_choice.isChecked()
    bench.press()
    assert window.pages.current() is window.server_page
    window.back_button.click()
    assert window.pages.current() is window.choose_page
    window.back_button.click()
    assert window.pages.current() is window.intro_page


def test_connecting_waits_for_all_three_fields(bench: Bench) -> None:
    bench.to_server_page()
    assert not bench.window.next_button.isEnabled()
    bench.window.address.setText("nas.example")
    bench.window.user.setText("me")
    assert not bench.window.next_button.isEnabled()
    bench.window.password.setText("p")
    assert bench.window.next_button.isEnabled()


# The address


def test_http_at_home_is_warned_of(bench: Bench) -> None:
    bench.to_server_page()
    bench.window.address.setText("http://192.168.0.10:5005/dav")
    assert bench.window.address_note.isVisible()
    assert "isn't encrypted" in bench.window.address_note.text()


def test_https_has_no_warning(bench: Bench) -> None:
    bench.to_server_page()
    bench.window.address.setText("https://192.168.0.10:5006/dav")
    assert not bench.window.address_note.isVisible()


def test_http_away_from_home_cannot_connect(bench: Bench) -> None:
    bench.to_server_page()
    bench.fill("http://nas.example.com/dav")
    assert "https" in bench.window.address_note.text()
    assert not bench.window.next_button.isEnabled()


def test_an_address_that_cannot_be_read_is_said(bench: Bench) -> None:
    bench.to_server_page()
    bench.fill("https://nas.example/a?b=1")
    bench.press()
    assert "can't be read" in bench.said()
    assert bench.window.pages.current() is bench.window.server_page


# What a server may answer


def test_a_wrong_password_is_said_and_asked_once(bench: Bench, share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        bench.connect(on_server(server), "wrong")
        bench.idle()
        assert "didn't accept" in bench.said()
        assert bench.window.pages.current() is bench.window.server_page
        assert len(server.asked) == 1
        assert bench.logins == []


def test_no_webdav_folder_at_the_address_is_said(bench: Bench, share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        bench.connect(on_server(server, "nowhere"))
        bench.idle()
        assert "no WebDAV folder" in bench.said()
    assert not (share / "nowhere").exists()


def test_a_server_gone_is_said_and_the_step_stays(bench: Bench, share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        address = on_server(server)
    bench.connect(address)
    bench.idle()
    assert "Couldn't reach" in bench.said()
    assert bench.window.pages.current() is bench.window.server_page


def test_a_server_gone_after_the_check_is_said_and_starting_can_be_tried_again(
    bench: Bench, share: Path
) -> None:
    with webdav(share, PASSWORD) as server:
        bench.connect(on_server(server))
        bench.page(bench.window.absent_page)
    bench.idle()
    bench.press()
    bench.idle()
    assert "Couldn't reach" in bench.said()
    assert bench.window.pages.current() is bench.window.absent_page
    assert bench.logins == []


# A certificate no public authority issued


def test_no_password_goes_before_the_certificate_is_trusted(bench: Bench, share: Path) -> None:
    own = self_signed()
    with webdav(share, PASSWORD, own=own) as server:
        bench.connect(f"https://{HOST}:{server.port}/share")
        bench.page(bench.window.certificate_page)
        bench.idle()
        assert server.asked == []
        assert server.certificate is not None
        assert bench.window.fingerprint.text() == shown_fingerprint(server.certificate)
        bench.press()
        bench.page(bench.window.absent_page)
        bench.idle()
        bench.press()
        bench.page(bench.window.last_page)
        assert bench.logins[0][0].certificate == server.certificate


def test_going_back_from_the_certificate_trusts_nothing(bench: Bench, share: Path) -> None:
    with webdav(share, PASSWORD, own=self_signed()) as server:
        bench.connect(f"https://{HOST}:{server.port}/share")
        bench.page(bench.window.certificate_page)
        bench.idle()
        bench.window.back_button.click()
        assert bench.window.pages.current() is bench.window.server_page
        bench.press()
        bench.page(bench.window.certificate_page)
        bench.idle()
        assert server.asked == []


def test_a_certificate_is_trusted_for_the_address_it_was_shown_at(
    bench: Bench, share: Path
) -> None:
    with webdav(share, PASSWORD, own=self_signed()) as server:
        bench.connect(f"https://{HOST}:{server.port}/share")
        bench.page(bench.window.certificate_page)
        bench.idle()
        bench.press()
        bench.page(bench.window.absent_page)
        bench.idle()
        bench.window.back_button.click()
        bench.window.address.setText(f"https://{HOST}:{server.port}/share/")
        assert bench.window._certificate is None  # pyright: ignore[reportPrivateUsage]


def test_a_wrong_password_after_trusting_needs_no_second_trust(bench: Bench, share: Path) -> None:
    with webdav(share, PASSWORD, own=self_signed()) as server:
        bench.connect(f"https://{HOST}:{server.port}/share", "wrong")
        bench.page(bench.window.certificate_page)
        bench.idle()
        bench.press()
        bench.page(bench.window.server_page)
        bench.idle()
        assert "didn't accept" in bench.said()
        bench.window.password.setText(PASSWORD)
        bench.press()
        bench.page(bench.window.absent_page)


def test_the_fingerprint_is_shown_as_servers_show_it() -> None:
    assert shown_fingerprint(b"x").splitlines() == [
        "2D:71:16:42:B7:26:B0:44:01:62:7C:A9:FB:AC:32:F5",
        "C8:53:0F:B1:90:3C:C4:DB:02:25:87:17:92:1A:48:81",
    ]


# No Stickle folder: made


def test_starting_makes_the_folder_and_keeps_what_it_takes(bench: Bench, share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        address = on_server(server)
        bench.connect(address)
        bench.page(bench.window.absent_page)
        bench.idle()
        assert "the 3 note" in bench.window.absent_text.text()
        bench.press()
        bench.page(bench.window.last_page)
    login, joining = bench.logins[0]
    assert not joining
    assert login.kind == WEBDAV
    assert login.address == address
    assert login.user == "stickle"
    assert login.secret == PASSWORD
    assert login.certificate is None
    assert login.stopped is None
    folder = share / "share" / "Stickle"
    assert read_vault_file((folder / VAULT_FILE).read_bytes()).vault == login.vault
    assert len(list((folder / "keys").iterdir())) == 2
    assert bench.window.password.text() == ""


@pytest.mark.parametrize(
    ("state", "check_shown"),
    [
        pytest.param(Keys(there=False), False, id="none"),
        pytest.param(Keys(held=False), True, id="not kept"),
        pytest.param(Keys(old=True), True, id="from before"),
    ],
)
def test_without_a_recovery_key_kept_nothing_starts(
    qtbot: QtBot, share: Path, state: Keys, check_shown: bool
) -> None:
    bench = make_bench(qtbot, state)
    with webdav(share, PASSWORD) as server:
        bench.connect(on_server(server))
        bench.page(bench.window.absent_page)
        bench.idle()
    assert not bench.window.next_button.isEnabled()
    assert bench.window.recovery_line.isVisible()
    assert bench.window.check_key_button.isVisible() == check_shown
    assert bench.window.make_key_button.isVisible()


def at_start_page(bench: Bench, server: Server) -> None:
    bench.connect(on_server(server))
    bench.page(bench.window.absent_page)
    bench.idle()


def test_a_recovery_key_typed_counts_as_kept(
    qtbot: QtBot, share: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = Keys(held=False)
    bench = make_bench(qtbot, state)

    def type_it(dialog: EnterRecoveryKeyDialog) -> bool:
        dialog.key.setText(RECOVERY)
        dialog.accept()
        return True

    monkeypatch.setattr("stickle.app.turn_on_sync.ask_recovery_key", type_it)
    with webdav(share, PASSWORD) as server:
        at_start_page(bench, server)
        assert not bench.window.next_button.isEnabled()
        bench.window.check_key_button.click()
    assert state.typed == RECOVERY
    assert bench.window.next_button.isEnabled()
    assert not bench.window.recovery_line.isVisible()


def test_a_wrong_recovery_key_typed_changes_nothing(
    qtbot: QtBot, share: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = Keys(held=False)
    bench = make_bench(qtbot, state)
    errors: list[str] = []

    def type_it(dialog: EnterRecoveryKeyDialog) -> bool:
        dialog.key.setText("not it")
        dialog.accept()
        errors.append(dialog.error.text())
        dialog.reject()
        return False

    monkeypatch.setattr("stickle.app.turn_on_sync.ask_recovery_key", type_it)
    with webdav(share, PASSWORD) as server:
        at_start_page(bench, server)
        bench.window.check_key_button.click()
    assert errors and errors[0]
    assert not bench.window.next_button.isEnabled()


def test_a_new_recovery_key_kept_lets_it_start(
    qtbot: QtBot, share: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = Keys(there=False)
    bench = make_bench(qtbot, state)

    def shown(_dialog: RecoveryKeyDialog) -> bool:
        return True

    monkeypatch.setattr("stickle.app.turn_on_sync.show_recovery_key", shown)
    with webdav(share, PASSWORD) as server:
        at_start_page(bench, server)
        bench.window.make_key_button.click()
    assert state.made == 1 and state.held
    assert bench.window.next_button.isEnabled()


def test_a_new_recovery_key_not_kept_does_not_let_it_start(
    qtbot: QtBot, share: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = Keys(there=False)
    bench = make_bench(qtbot, state)

    def shown(_dialog: RecoveryKeyDialog) -> bool:
        return False

    monkeypatch.setattr("stickle.app.turn_on_sync.show_recovery_key", shown)
    with webdav(share, PASSWORD) as server:
        at_start_page(bench, server)
        bench.window.make_key_button.click()
    assert state.made == 1
    assert not bench.window.next_button.isEnabled()


def test_a_new_recovery_key_is_asked_about_when_there_is_one(
    qtbot: QtBot, share: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = Keys(held=False)
    bench = make_bench(qtbot, state)
    asked: list[object] = []

    def refused(parent: object) -> bool:
        asked.append(parent)
        return False

    monkeypatch.setattr("stickle.app.turn_on_sync.confirm_new_recovery_key", refused)
    with webdav(share, PASSWORD) as server:
        at_start_page(bench, server)
        bench.window.make_key_button.click()
    assert asked == [bench.window] and state.made == 0


def test_no_folder_is_made_without_a_recovery_key_to_seal_it_for(
    qtbot: QtBot, keys: Keys, share: Path
) -> None:
    bench = make_bench(qtbot, keys, sealed_for={})
    with webdav(share, PASSWORD) as server:
        at_start_page(bench, server)
        bench.press()
        assert "could not be saved" in bench.said()
    assert list((share / "share").iterdir()) == []


def test_a_making_cut_short_is_made_over(bench: Bench, share: Path) -> None:
    folder = share / "share" / "Stickle"
    folder.mkdir()
    left = "0a1b2c3d-1111-4222-8333-444455556666"
    (folder / VAULT_FILE).write_bytes(vault_file(VaultInfo(left)))
    with webdav(share, PASSWORD) as server:
        bench.connect(on_server(server))
        bench.page(bench.window.absent_page)
        bench.idle()
        bench.press()
        bench.page(bench.window.last_page)
    made = bench.logins[0][0].vault
    assert made != left
    assert read_vault_file((folder / VAULT_FILE).read_bytes()).vault == made


def test_another_computer_making_it_first_leads_to_opening_that_one(
    bench: Bench, share: Path
) -> None:
    with webdav(share, PASSWORD) as server:
        bench.connect(on_server(server))
        bench.page(bench.window.absent_page)
        bench.idle()
        other = target_of(server)
        make(other, PAIRS)
        other.close()
        bench.press()
        bench.page(bench.window.present_page)
    assert bench.logins == []


# A Stickle folder there: opened


def folder_there(server: Server) -> str:
    target = target_of(server)
    made = make(target, PAIRS)
    target.close()
    return made.vault


def test_a_folder_there_is_opened_with_the_recovery_key(bench: Bench, share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        vault = folder_there(server)
        bench.connect(on_server(server))
        bench.page(bench.window.present_page)
        bench.idle()
        assert not bench.window.next_button.isEnabled()
        bench.window.way_in.setText(recovery.format_key(RECOVERY).lower())
        bench.press()
        bench.page(bench.window.last_page)
    login, joining = bench.logins[0]
    assert joining and login.vault == vault
    assert bench.window.way_in.text() == ""


def test_a_folder_there_is_opened_with_the_spare_password(bench: Bench, share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        vault = folder_there(server)
        bench.connect(on_server(server))
        bench.page(bench.window.present_page)
        bench.idle()
        assert bench.window.way_in.echoMode() == QLineEdit.EchoMode.Normal
        bench.window.by_password.click()
        assert bench.window.way_in.echoMode() == QLineEdit.EchoMode.Password
        bench.window.way_in.setText(SPARE)
        bench.press()
        bench.page(bench.window.last_page)
    assert bench.logins[0][0].vault == vault


def test_changing_the_way_in_forgets_what_was_typed(bench: Bench, share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        folder_there(server)
        bench.connect(on_server(server))
        bench.page(bench.window.present_page)
        bench.idle()
    bench.window.way_in.setText("half of it")
    bench.window.by_password.click()
    assert bench.window.way_in.text() == ""


@pytest.mark.parametrize(
    ("typed", "password", "said"),
    [
        pytest.param(recovery.generate(), False, "doesn't open", id="another key"),
        pytest.param("abc", False, "as written", id="not a key"),
        pytest.param("wrong", True, "doesn't open", id="wrong password"),
    ],
)
def test_what_does_not_open_it_is_said_and_can_be_tried_again(
    bench: Bench, share: Path, typed: str, password: bool, said: str
) -> None:
    with webdav(share, PASSWORD) as server:
        folder_there(server)
        bench.connect(on_server(server))
        bench.page(bench.window.present_page)
        bench.idle()
        if password:
            bench.window.by_password.click()
        bench.window.way_in.setText(typed)
        bench.press()
        bench.idle()
        assert said in bench.said()
        assert bench.window.pages.current() is bench.window.present_page
        assert bench.window.next_button.isEnabled()
        assert bench.logins == []


def test_a_folder_of_a_newer_format_is_said(bench: Bench, share: Path) -> None:
    folder = share / "share" / "Stickle"
    folder.mkdir()
    (folder / VAULT_FILE).write_bytes(
        json_bytes({"format": 2, "kind": "personal", "vault": "a-vault"})
    )
    with webdav(share, PASSWORD) as server:
        bench.connect(on_server(server))
        bench.idle()
        assert "newer version" in bench.said()
    assert bench.logins == []


# Cancelled


def test_cancelled_before_the_end_nothing_is_kept_or_made(
    qtbot: QtBot, keys: Keys, share: Path
) -> None:
    bench = make_bench(qtbot, keys)
    with webdav(share, PASSWORD) as server:
        bench.connect(on_server(server))
        bench.page(bench.window.absent_page)
        bench.idle()
        bench.window.cancel_button.click()
    assert bench.logins == []
    assert list((share / "share").iterdir()) == []


def test_what_was_typed_is_forgotten_when_the_window_closes(
    qtbot: QtBot, keys: Keys, share: Path
) -> None:
    bench = make_bench(qtbot, keys)
    with webdav(share, PASSWORD) as server:
        folder_there(server)
        bench.connect(on_server(server))
        bench.page(bench.window.present_page)
        bench.idle()
    bench.window.way_in.setText(RECOVERY)
    bench.window.reject()
    assert bench.window.password.text() == "" and bench.window.way_in.text() == ""


def test_cancelled_while_it_talks_to_the_server_comes_to_nothing(
    qtbot: QtBot, keys: Keys, share: Path
) -> None:
    bench = make_bench(qtbot, keys)
    with webdav(share, PASSWORD) as server:
        bench.connect(on_server(server))
        bench.window.cancel_button.click()
        qtbot.waitUntil(gc.isenabled, timeout=MS)
    assert bench.logins == []


# The first round


@pytest.fixture
def at_last(bench: Bench, share: Path) -> Bench:
    with webdav(share, PASSWORD) as server:
        bench.connect(on_server(server))
        bench.page(bench.window.absent_page)
        bench.idle()
        bench.press()
        bench.page(bench.window.last_page)
    return bench


def test_the_last_step_says_how_far_the_notes_have_got(at_last: Bench) -> None:
    window = at_last.window
    assert "Uploading" in window.last_line.text()
    assert not window.next_button.isVisible() and not window.back_button.isVisible()
    assert window.cancel_button.text() == "Close"
    at_last.syncing.moving.emit(PUTTING, 340, 1000)
    assert "340 of 1,000" in window.last_line.text()
    at_last.syncing.moving.emit(GETTING, 1, 2)
    assert "Downloading your notes · 1 of 2" in window.last_line.text()


def test_the_end_says_what_to_do_on_the_other_computers(at_last: Bench) -> None:
    at_last.syncing.round_shown.emit(Ended(Report(), Outcome()))
    assert "On your other computers" in at_last.window.last_line.text()
    at_last.syncing.moving.emit(PUTTING, 1, 1)
    assert "On your other computers" in at_last.window.last_line.text()


def test_a_round_failed_for_now_is_tried_again_and_says_so_not(at_last: Bench) -> None:
    before = at_last.window.last_line.text()
    at_last.syncing.round_shown.emit(Ended(Report(), Outcome(wait=15.0, failures=1)))
    assert at_last.window.last_line.text() == before
    at_last.syncing.round_shown.emit(Ended(Report(), Outcome()))
    assert "On your other computers" in at_last.window.last_line.text()


@pytest.mark.parametrize(
    ("outcome", "said"),
    [
        (Outcome(stopped=Stopped.SIGN_IN), "server password"),
        (Outcome(stopped=Stopped.NO_ACCESS), "permissions"),
        (Outcome(stopped=Stopped.BIG_CHANGE), "big change"),
        (Outcome(stopped=Stopped.VAULT_GONE), "gone from WebDAV"),
        (Outcome(stopped=Stopped.VAULT_CHANGED), "made anew"),
        (Outcome(stopped=Stopped.CERTIFICATE), "certificate changed"),
        (Outcome(stopped=Stopped.NEWER), "update Stickle"),
        (Outcome(full=True), "your WebDAV is full"),
    ],
)
def test_a_first_round_that_stops_says_why(at_last: Bench, outcome: Outcome, said: str) -> None:
    at_last.syncing.round_shown.emit(Ended(Report(), outcome))
    assert said in at_last.window.last_line.text()


def test_joining_says_notes_are_coming_and_then_that_it_is_done(bench: Bench, share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        folder_there(server)
        bench.connect(on_server(server))
        bench.page(bench.window.present_page)
        bench.idle()
        bench.window.way_in.setText(RECOVERY)
        bench.press()
        bench.page(bench.window.last_page)
    assert "Downloading" in bench.window.last_line.text()
    bench.syncing.round_shown.emit(Ended(Report(), Outcome()))
    assert "now sync too" in bench.window.last_line.text()


def test_what_goes_wrong_keeping_it_is_said(qtbot: QtBot, keys: Keys, share: Path) -> None:
    def fails(_login: Login, _joining: bool) -> None:
        raise apsw.BusyError("busy")

    bench = make_bench(qtbot, keys, fails)
    with webdav(share, PASSWORD) as server:
        bench.connect(on_server(server))
        bench.page(bench.window.absent_page)
        bench.idle()
        bench.press()
        bench.idle()
        assert "Couldn't reach" in bench.said()
    assert bench.window.pages.current() is bench.window.absent_page


# Work off the app's thread


def test_work_is_done_elsewhere_and_its_outcome_comes_back_here(qtbot: QtBot) -> None:
    here = threading.current_thread()
    seen: list[object] = []
    ran_on: list[threading.Thread] = []
    came_on: list[threading.Thread] = []

    def work() -> int:
        ran_on.append(threading.current_thread())
        return 7

    def then(outcome: object) -> None:
        came_on.append(threading.current_thread())
        seen.append(outcome)

    Job(work, then)
    qtbot.waitUntil(lambda: seen != [], timeout=MS)
    assert seen == [7] and came_on == [here] and ran_on[0] is not here


def test_an_exception_comes_back_as_the_outcome(qtbot: QtBot) -> None:
    seen: list[object] = []

    def work() -> None:
        raise ValueError("no")

    Job(work, seen.append)
    qtbot.waitUntil(lambda: seen != [], timeout=MS)
    assert isinstance(seen[0], ValueError)


def test_nothing_is_collected_while_work_goes_on(qtbot: QtBot) -> None:
    let_go = threading.Event()
    seen: list[object] = []
    Job(lambda: let_go.wait(10), seen.append)
    assert not gc.isenabled()
    let_go.set()
    qtbot.waitUntil(lambda: seen != [], timeout=MS)
    assert gc.isenabled()


def test_collecting_stays_off_while_sync_or_other_work_goes_on(qtbot: QtBot) -> None:
    let_go, sync_goes_on = threading.Event(), threading.Event()
    seen: list[object] = []
    sync = threading.Thread(target=sync_goes_on.wait, args=(10,), name="sync", daemon=True)
    sync.start()
    Job(lambda: let_go.wait(10), seen.append)
    let_go.set()
    qtbot.waitUntil(lambda: seen != [], timeout=MS)
    assert not gc.isenabled()
    sync_goes_on.set()
    sync.join(10)


def test_work_whose_window_went_comes_to_what_it_was_told(qtbot: QtBot) -> None:
    let_go = threading.Event()
    seen: list[object] = []
    instead: list[bool] = []
    job = Job(lambda: let_go.wait(10), seen.append)

    def dropped() -> None:
        instead.append(True)

    job.drop(dropped)
    let_go.set()
    qtbot.waitUntil(lambda: instead != [], timeout=MS)
    assert seen == []


# The whole way, with the app's own ways of keeping and starting


@dataclass
class Pc:
    db: apsw.Connection
    notes: NoteRepository
    manager: NoteManager
    clocks: Clocks
    path: Path
    started: list[Syncing]

    def connect(self, login: Login, joining: bool) -> Syncing:
        started = connect_sync(
            self.db, self.path, DB_KEY, login, joining, self.clocks, self.manager, None
        )
        self.started.append(started)
        return started

    def setup(self, keys: Keys) -> SyncSetup:
        return SyncSetup(
            lambda: bool(self.started),
            keys.keys(),
            lambda: len(self.manager.listed_notes()),
            lambda: PAIRS,
            self.connect,
        )

    def end(self) -> None:
        for each in self.started:
            each.finish(0.0)
            self_wait_for_thread(each)
        for window in self.manager.windows:
            window.release()
        self.db.close()


def first_login(pc: Pc) -> Login:
    kept = sync_connection.current(pc.db)
    assert kept is not None
    return kept


def self_wait_for_thread(syncing: Syncing) -> None:
    deadline = threading.Event()
    for _ in range(100):
        if not syncing.runner.running:
            return
        deadline.wait(0.1)


def make_pc(tmp_path: Path, name: str) -> Pc:
    path = tmp_path / f"{name}.db"
    db = open_store(path, DB_KEY)
    clocks = Clocks()
    notes = NoteRepository(db, clocks.stamp, clocks.millis)
    manager = NoteManager(
        notes,
        idle_ms=10,
        max_ms=50,
        settings=Settings(db),
        layouts=LayoutRepository(db),
        labels=LabelRepository(db, clocks.stamp, clocks.millis),
        colors=ColorRepository(db, clocks.stamp, clocks.millis),
        versions=VersionRepository(db),
    )
    return Pc(db, notes, manager, clocks, path, [])


@pytest.mark.parametrize("joining", [True, False])
def test_connecting_keeps_the_login_and_starts_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, joining: bool
) -> None:
    calls: list[str] = []

    class Started:
        def __init__(self, *_args: object) -> None:
            calls.append("made")

        def start(self) -> None:
            calls.append("start")

        def sync_now(self) -> None:
            calls.append("now")

    monkeypatch.setattr("stickle.app.syncing.Syncing", Started)
    pc = make_pc(tmp_path, "pc")
    login = Login(VAULT_ID, WEBDAV, "https://nas.example/dav", "u", "p", {1: bytes(32)})
    connect_sync(pc.db, pc.path, DB_KEY, login, joining, pc.clocks, pc.manager, None)
    assert sync_connection.current(pc.db) == login
    assert sync_state.joining(pc.db, VAULT_ID) is joining
    assert calls == ["made", "start", "now"]
    assert pc.clocks.cloud is not None
    pc.db.close()


def test_one_computer_turns_sync_on_and_another_joins_and_gets_its_note(
    qtbot: QtBot, tmp_path: Path, share: Path, keys: Keys
) -> None:
    first, second = make_pc(tmp_path, "first"), make_pc(tmp_path, "second")
    made = first.notes.create("made on the first computer")
    first.manager.open_stored()
    second.manager.open_stored()
    with webdav(share, PASSWORD) as server:
        one = TurnOnSync(first.setup(keys))
        one.show()
        bench = Bench(one, qtbot, [], FakeSyncing())
        bench.connect(on_server(server))
        bench.page(one.absent_page)
        bench.idle()
        assert first.clocks.cloud is None
        bench.press()
        bench.page(one.last_page)
        assert first.clocks.cloud is not None  # stamped by the cloud's time from now on
        qtbot.waitUntil(lambda: "On your other" in one.last_line.text(), timeout=MS)
        assert sync_connection.current(first.db) is not None
        one.reject()
        assert sync_state.joining(first.db, first_login(first).vault) is False

        other = TurnOnSync(second.setup(keys))
        other.show()
        bench = Bench(other, qtbot, [], FakeSyncing())
        bench.connect(on_server(server))
        bench.page(other.present_page)
        bench.idle()
        other.way_in.setText(RECOVERY)
        bench.press()
        bench.page(other.last_page)
        qtbot.waitUntil(lambda: "now sync too" in other.last_line.text(), timeout=MS)
        other.reject()
        got = second.notes.get(made.id)
        assert got is not None and got.body == "made on the first computer"
        assert first_login(second).vault == first_login(first).vault
        window = second.manager.window_for(made.id)
        assert isinstance(window, NoteWindow)
        first.end()
        second.end()


REAL_MS = 60_000


@pytest.mark.skipif(not account_given(), reason="needs a real WebDAV server")
def test_on_a_real_server_one_computer_turns_it_on_and_another_joins(
    qtbot: QtBot, tmp_path: Path, keys: Keys
) -> None:
    given = account()
    assert given is not None
    first, second = make_pc(tmp_path, "first"), make_pc(tmp_path, "second")
    made = first.notes.create("made on the first computer")

    def up_to_the_folder(pc: Pc, address: str) -> TurnOnSync:
        window = TurnOnSync(pc.setup(keys))
        window.show()
        bench = Bench(window, qtbot, [], FakeSyncing())
        bench.to_server_page()
        bench.fill(address, given.password, given.user)
        bench.press()
        qtbot.waitUntil(lambda: window.back_button.isEnabled(), timeout=REAL_MS)
        if window.pages.current() is window.certificate_page:
            bench.press()
            qtbot.waitUntil(lambda: window.back_button.isEnabled(), timeout=REAL_MS)
        return window

    with folder_of_its_own() as target:
        address = target._address.text  # pyright: ignore[reportPrivateUsage]
        one = up_to_the_folder(first, address)
        assert one.pages.current() is one.absent_page
        one.next_button.click()
        qtbot.waitUntil(lambda: one.pages.current() is one.last_page, timeout=REAL_MS)
        qtbot.waitUntil(lambda: "On your other" in one.last_line.text(), timeout=REAL_MS)
        one.reject()

        other = up_to_the_folder(second, address)
        assert other.pages.current() is other.present_page
        other.way_in.setText(RECOVERY)
        other.next_button.click()
        qtbot.waitUntil(lambda: other.pages.current() is other.last_page, timeout=REAL_MS)
        qtbot.waitUntil(lambda: "now sync too" in other.last_line.text(), timeout=REAL_MS)
        other.reject()
        got = second.notes.get(made.id)
        assert got is not None and got.body == "made on the first computer"
        first.end()
        second.end()
