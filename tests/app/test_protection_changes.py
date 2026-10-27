"""Changes to what protects the notes, told to their user afterwards."""

import secrets
import sys
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from pytestqt.qtbot import QtBot

from stickle.app import application
from stickle.app.application import tell_changes
from stickle.app.i18n import Translations
from stickle.app.master_password import MasterPasswords
from stickle.app.notes import NoteManager
from stickle.app.protection_changes import Change, ProtectionChanges
from stickle.app.settings_window import RecoveryKeys, SettingsWindow
from stickle.app.stickle_window import StickleWindow
from stickle.app.tray import Tray
from stickle.data.schema import open_store
from stickle.data.settings import PROTECTION_CHANGES, InvalidSettingError, Settings
from stickle.platform.linux.session import ScreenSaverWatcher

KEY = secrets.token_bytes(32)


class Clock:
    def __init__(self) -> None:
        self.minute = 0

    def __call__(self) -> str:
        self.minute += 1
        return f"2026-10-26T09:{self.minute:02d}:00.000Z"


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def changes(connection: apsw.Connection) -> ProtectionChanges:
    return ProtectionChanges(Settings(connection), Clock())


def test_a_change_is_told_later_not_at_once(changes: ProtectionChanges) -> None:
    changes.note("master_password_set")
    assert changes.to_tell() == []  # whoever is at the screen made it

    assert changes.due()  # the next start, or an unlock
    assert changes.to_tell() == [Change("master_password_set", "2026-10-26T09:01:00.000Z")]
    assert not changes.due()  # nothing more to tell


def test_the_latest_of_each_kind_is_told_and_seen_clears_them(changes: ProtectionChanges) -> None:
    changes.note("recovery_key")
    changes.note("master_password_set")
    changes.note("master_password_changed")
    changes.note("recovery_key")
    changes.due()
    changes.note("master_password_changed")  # after: told at the next start or unlock

    told = changes.to_tell()
    assert [c.what for c in told] == [
        "master_password_set",
        "master_password_changed",
        "recovery_key",
    ]
    assert told[-1].at == "2026-10-26T09:04:00.000Z"

    changes.seen()
    assert changes.to_tell() == []
    assert changes.due()
    assert [c.what for c in changes.to_tell()] == ["master_password_changed"]


def test_only_well_formed_changes_are_kept(connection: apsw.Connection) -> None:
    settings = Settings(connection)
    with pytest.raises(InvalidSettingError):
        settings.set(PROTECTION_CHANGES, [{"what": "anything", "at": "now", "told": False}])


def window_with(changes: ProtectionChanges) -> StickleWindow:
    recovery = RecoveryKeys(lambda: True, lambda: "", lambda: None)
    return StickleWindow(
        NoteManager(), Translations(), lambda: None, recovery=recovery, changes=changes
    )


def close(window: StickleWindow) -> None:
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_stickle_manager_says_so_at_the_top_until_the_user_has_seen_it(
    qtbot: QtBot, changes: ProtectionChanges
) -> None:
    window = window_with(changes)
    try:
        window.open()
        assert not window.changes_label.isVisibleTo(window)

        changes.note("master_password_set")
        changes.due()
        window.refresh_changes()
        assert window.changes_label.isVisibleTo(window)
        assert "spare password was set on this computer" in window.changes_label.text()
        assert "If it was not you" in window.changes_label.text()

        window.changes_again_button.click()  # to the place to set it again
        assert window.settings.isVisible()
        assert window.settings.tabs.currentWidget() is window.settings.protection
        window.settings.close()

        window.changes_seen_button.click()
        assert not window.changes_label.isVisibleTo(window)
        assert changes.to_tell() == []
    finally:
        close(window)


@pytest.mark.parametrize("open_window", [True, False])
def test_with_stickle_manager_closed_the_system_tells_it_once(
    qtbot: QtBot, changes: ProtectionChanges, monkeypatch: pytest.MonkeyPatch, open_window: bool
) -> None:
    shown: list[str] = []
    window = window_with(changes)
    tray = Tray(lambda: None, lambda: None, Translations())

    def show_message(_title: str, text: str) -> None:
        shown.append(text)

    monkeypatch.setattr(tray, "showMessage", show_message)
    try:
        if open_window:
            window.open()
        changes.note("recovery_key")

        tell_changes(changes, window, tray)
        tell_changes(changes, window, tray)  # an unlock later: nothing new

        assert len(shown) == (0 if open_window else 1)
        assert window.changes_told()
        assert all("new recovery key" in text for text in shown)
    finally:
        tray.hide()
        close(window)


def test_the_settings_say_when_each_was_made_or_set(
    qtbot: QtBot, translations: Translations
) -> None:
    # In English (the fixture's), whatever this computer's own language.
    recovery = RecoveryKeys(
        lambda: True, lambda: "", lambda: None, lambda: "2026-10-03T05:00:00.000Z"
    )
    master = MasterPasswords(
        exists=lambda: True,
        set=lambda _: None,
        change=lambda _a, _b: None,
        check=lambda _: True,
        opens_notes=lambda _: False,
        recovery_key_opens=lambda _: True,
        set_at=lambda: "2026-10-25T05:03:00.000Z",
    )
    window = SettingsWindow(NoteManager(), Translations(), recovery=recovery, master=master)
    qtbot.addWidget(window)

    text = window.protection_dates.text()
    assert (
        text.startswith("Recovery key: made October 3") and "Spare password: set October 25" in text
    )

    old = SettingsWindow(
        NoteManager(), Translations(), recovery=RecoveryKeys(lambda: True, lambda: "", lambda: None)
    )
    qtbot.addWidget(old)
    assert old.protection_dates.text() == "Recovery key: made with an earlier Stickle"


def test_changes_made_in_the_settings_are_noted_but_not_at_the_first_start(
    connection: apsw.Connection, tmp_path: Path
) -> None:
    from test_startup_dialogs import FAST, Backend, store_of

    from stickle.unlock import Unlock

    unlock = Unlock(tmp_path / "data", store_of(Backend()), FAST, master_strength=FAST)
    (tmp_path / "data").mkdir()
    unlock.opened_key = KEY
    changes = ProtectionChanges(Settings(connection), Clock())

    first_start = application.master_passwords(unlock)
    assert first_start is not None
    first_start.set("correct horse battery staple")
    settings_page = application.master_passwords(unlock, changes)
    recovery = application.recovery_keys(unlock, Settings(connection), changes)
    assert settings_page is not None and recovery is not None
    settings_page.change("correct horse battery staple", "another phrase to remember well")
    recovery.make()

    changes.due()
    assert [c.what for c in changes.to_tell()] == ["master_password_changed", "recovery_key"]
    assert settings_page.set_at() is not None and recovery.made_at() is not None


def test_the_screen_saver_starting_is_a_lock_and_ending_an_unlock() -> None:
    heard: list[str] = []
    watcher = ScreenSaverWatcher(lambda: heard.append("unlock"), lambda: heard.append("lock"))

    watcher.active_changed(True)  # it starts: the screen locks
    assert heard == ["lock"]
    watcher.active_changed(False)
    assert heard == ["lock", "unlock"]


class Bus:
    """The session bus, as far as the watcher uses it: what it listens to."""

    def __init__(self) -> None:
        self.heard: list[tuple[str, str, str, str]] = []

    def isConnected(self) -> bool:  # noqa: N802 - Qt's name
        return True

    def connect(self, service: str, path: str, interface: str, name: str, *_: object) -> bool:
        self.heard.append((service, path, interface, name))
        return True


@pytest.mark.parametrize(
    "service",
    [
        "org.gnome.ScreenSaver",  # GNOME
        "org.freedesktop.ScreenSaver",  # KDE and others
        "org.cinnamon.ScreenSaver",  # Linux Mint's Cinnamon
        "org.mate.ScreenSaver",  # MATE
        "org.xfce.ScreenSaver",  # Xfce
    ],
)
def test_each_desktops_screen_saver_is_listened_to(
    monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    bus = Bus()
    monkeypatch.setattr(
        "stickle.platform.linux.session.QDBusConnection.sessionBus", staticmethod(lambda: bus)
    )

    ScreenSaverWatcher(lambda: None, lambda: None)

    assert (service, "ActiveChanged") in {(s, name) for s, _p, _i, name in bus.heard}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_tells_of_the_lock_and_the_unlock() -> None:
    if sys.platform != "win32":  # also tells the type checker this is Windows
        return
    import ctypes

    from stickle.platform.windows.power import MSG
    from stickle.platform.windows.session import (
        WM_WTSSESSION_CHANGE,
        WTS_SESSION_LOCK,
        WTS_SESSION_UNLOCK,
        SessionFilter,
        screen_saver_running,
    )

    heard: list[str] = []
    session_filter = SessionFilter(lambda: heard.append("unlock"), lambda: heard.append("lock"))
    for change in (WTS_SESSION_LOCK, WTS_SESSION_UNLOCK, 0x5):  # 0x5: a logon, neither
        message = MSG(None, WM_WTSSESSION_CHANGE, change, 0, 0)
        session_filter.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message))
    assert heard == ["lock", "unlock"]
    assert screen_saver_running() is False  # asked of Windows: not while tests run
