"""The settings that follow the user to every computer that syncs: said so
beside each, once sync is on (to a screen reader too), and shown at once as
another computer changed them."""

import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import apsw
import pytest
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot
from sync_screens import FakeSyncing

from stickle.app.i18n import Translations
from stickle.app.notes import NoteManager
from stickle.app.settings_window import Everywhere, RecoveryKeys, SettingsWindow
from stickle.app.sync_setup import SyncSetup
from stickle.app.syncing import Syncing
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import MAGNET, RANDOM_COLOR, RELOCK_MINUTES, Settings
from stickle.data.sync_connection import Login

KEY = secrets.token_bytes(32)
SAID = "Changes on all your syncing computers."


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


class Sync:
    """Sync on or off, as the settings window asks."""

    def __init__(self) -> None:
        self.syncing: FakeSyncing | None = None

    def setup(self) -> SyncSetup:
        def connect(_login: Login, _joining: bool) -> Syncing:
            raise AssertionError("nothing connects here")

        def current() -> Syncing | None:
            return cast("Syncing | None", self.syncing)

        keys = RecoveryKeys(exists=lambda: True, make=lambda: "key", kept=lambda: None)
        return SyncSetup(current, keys, lambda: 0, lambda: {}, connect, lambda: None)


def window_of(
    qtbot: QtBot, connection: apsw.Connection, sync: Sync
) -> tuple[SettingsWindow, NoteManager]:
    notes = NoteManager(NoteRepository(connection), settings=Settings(connection))
    window = SettingsWindow(notes, Translations(), sync_setup=sync.setup())
    qtbot.addWidget(window)
    return window, notes


def shown(window: SettingsWindow) -> set[str]:
    """The controls beside which "All computers" shows, by their names here."""
    names: dict[QWidget, str] = {
        window.magnet_box: "magnet",
        window.random_box: "random",
        window.own_list: "own colours",
        window.morning_edit: "morning",
        window.snooze_box: "snooze",
        window.relock_box: "relock",
        window.leave_box: "leave",
    }
    marks = window._everywhere  # pyright: ignore[reportPrivateUsage]
    assert {names[control] for _, control in marks} == set(names.values())
    assert len(window.findChildren(Everywhere)) == len(names)
    return {names[control] for label, control in marks if not label.isHidden()}


def test_said_beside_the_settings_that_follow_only_with_sync_on(
    qtbot: QtBot, connection: apsw.Connection
) -> None:
    sync = Sync()
    window, _ = window_of(qtbot, connection, sync)
    magnet_said = window.magnet_box.accessibleDescription()
    assert shown(window) == set()

    sync.syncing = FakeSyncing()
    window.refresh()

    # Those of secret notes only once there are secret notes to cover.
    assert shown(window) == {"magnet", "random", "own colours", "morning", "snooze"}
    for label in window.findChildren(Everywhere):
        assert (label.text(), label.toolTip(), label.accessibleName()) == (
            "All computers",
            SAID,
            SAID,
        )
    assert window.magnet_box.accessibleDescription() == f"{magnet_said} {SAID}"
    assert window.snooze_box.accessibleDescription() == SAID

    sync.syncing = None
    window.refresh()
    assert shown(window) == set()
    assert window.magnet_box.accessibleDescription() == magnet_said


def test_a_setting_changed_on_another_computer_shows_at_once(
    qtbot: QtBot, connection: apsw.Connection
) -> None:
    window, notes = window_of(qtbot, connection, Sync())
    assert window.magnet_box.isChecked() and not window.random_box.isChecked()
    settings = Settings(connection)  # as a round takes them in
    settings.set(MAGNET, False)
    settings.set(RANDOM_COLOR, True)
    settings.set(RELOCK_MINUTES, 20)

    with qtbot.waitSignals([notes.magnet_changed, notes.settings_taken]):
        notes.take_settings([MAGNET.key, RANDOM_COLOR.key, RELOCK_MINUTES.key])

    assert not window.magnet_box.isChecked()
    assert window.random_box.isChecked()
    assert window.relock_box.value() == 20
