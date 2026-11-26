"""Where the app sends what waits to go up, or asks sync for a round, of its own:
as the computer goes to sleep (what was typed just before is not left only
here), and once what protects the notes changes (the other computers have the
new password or recovery key before it is typed there)."""

import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import apsw
import pytest

from stickle.app.application import SyncSwitch, before_sleep, round_for_a_change
from stickle.app.notes import NoteManager
from stickle.app.protection_changes import ProtectionChanges
from stickle.app.syncing import Syncing
from stickle.data.schema import open_store
from stickle.data.settings import Settings

KEY = secrets.token_bytes(32)


class Told:
    """What the app was asked to do, in turn."""

    def __init__(self) -> None:
        self.done: list[str] = []

    # As a note manager
    def save_all(self) -> None:
        self.done.append("saved")

    def lock_secret_notes(self) -> None:
        self.done.append("locked")

    # As sync running
    def hurry(self) -> None:
        self.done.append("sent")

    def sync_now(self) -> None:
        self.done.append("round")


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


def test_going_to_sleep_what_waits_to_go_up_is_sent_once_saved() -> None:
    told = Told()

    before_sleep(cast(NoteManager, told), cast(Syncing, told))

    assert told.done == ["saved", "locked", "sent"]


def test_going_to_sleep_with_sync_off_it_is_saved_and_locked() -> None:
    told = Told()

    before_sleep(cast(NoteManager, told), None)

    assert told.done == ["saved", "locked"]


def test_a_change_of_what_protects_the_notes_asks_a_round_once_noted(
    connection: apsw.Connection,
) -> None:
    told = Told()
    switch = SyncSwitch()
    switch.syncing = cast(Syncing, told)
    changes = ProtectionChanges(Settings(connection), made=lambda: round_for_a_change(switch))

    for what in ("secret_password_changed", "master_password_set", "recovery_key"):
        changes.note(what)

    assert told.done == ["round", "round", "round"]
    assert changes.due()  # noted all the same, to be told


def test_with_sync_off_a_change_asks_nothing(connection: apsw.Connection) -> None:
    changes = ProtectionChanges(Settings(connection), made=lambda: round_for_a_change(SyncSwitch()))

    changes.note("recovery_key")  # nothing to ask, nothing raised

    assert changes.due()
