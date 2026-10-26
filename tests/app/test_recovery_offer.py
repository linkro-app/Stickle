"""The recovery key, offered once more to whoever skipped it, and only once."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from pytestqt.qtbot import QtBot

from stickle.app.recovery_key_dialog import RecoveryKeyDialog
from stickle.app.recovery_offer import NOTES_WORTH_KEEPING, RecoveryOffer
from stickle.crypto.recovery import generate
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import RECOVERY_KEY_KEPT, RECOVERY_KEY_OFFERED_AGAIN, Settings

KEY = secrets.token_bytes(32)


class Offer:
    def __init__(self, connection: apsw.Connection, answer: bool) -> None:
        self.notes = NoteRepository(connection)
        self.settings = Settings(connection)
        self.shown: list[RecoveryKeyDialog] = []
        self.made: list[str] = []
        self._answer = answer
        self.offer = RecoveryOffer(self.settings, self.notes, self.make, self.show)

    def make(self) -> str:
        self.made.append(generate())
        return self.made[-1]

    def show(self, dialog: RecoveryKeyDialog) -> bool:
        self.shown.append(dialog)
        return self._answer


@pytest.fixture
def connection(qtbot: QtBot, tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


def notes(offer: Offer, count: int) -> None:
    for number in range(count):
        offer.notes.create(f"메모 {number}")


def test_not_before_there_is_something_to_lose(connection: apsw.Connection) -> None:
    offer = Offer(connection, answer=True)
    notes(offer, NOTES_WORTH_KEEPING - 1)

    offer.offer.check()

    assert offer.shown == []
    assert not offer.settings.get(RECOVERY_KEY_OFFERED_AGAIN)


def test_offered_at_five_notes_with_the_reason(connection: apsw.Connection) -> None:
    offer = Offer(connection, answer=True)
    notes(offer, NOTES_WORTH_KEEPING)

    offer.offer.check()

    assert len(offer.shown) == 1
    dialog = offer.shown[0]
    assert dialog.panel.recovery_key == offer.made[0]
    assert "will not ask again" in dialog.reason.text()
    assert not dialog.reason.isHidden()
    assert offer.settings.get(RECOVERY_KEY_KEPT)


def test_skipped_again_it_is_never_offered_again(connection: apsw.Connection) -> None:
    offer = Offer(connection, answer=False)
    notes(offer, NOTES_WORTH_KEEPING)

    offer.offer.check()
    notes(offer, 10)
    offer.offer.check()

    assert len(offer.shown) == 1
    assert not offer.settings.get(RECOVERY_KEY_KEPT)


def test_kept_at_first_start_means_never_offered(connection: apsw.Connection) -> None:
    offer = Offer(connection, answer=True)
    offer.settings.set(RECOVERY_KEY_KEPT, True)
    notes(offer, NOTES_WORTH_KEEPING)

    offer.offer.check()

    assert offer.shown == []
    assert offer.made == []


def test_deleted_notes_do_not_count(connection: apsw.Connection) -> None:
    offer = Offer(connection, answer=True)
    notes(offer, NOTES_WORTH_KEEPING)
    first = offer.notes.all()[0]
    offer.notes.delete(first.id)

    offer.offer.check()

    assert offer.shown == []


def test_a_note_stored_for_the_first_time_is_announced(connection: apsw.Connection) -> None:
    from stickle.app.notes import NoteManager

    manager = NoteManager(NoteRepository(connection))
    created: list[None] = []
    manager.note_created.connect(lambda: created.append(None))
    window = manager.new_note()

    window.editor.insertPlainText("첫 저장")
    manager.save(window)
    window.editor.insertPlainText(" 그리고 수정")
    manager.save(window)

    assert created == [None]
    window.release()


def test_not_offered_once_there_are_secret_notes(connection: apsw.Connection) -> None:
    # Their key is not at hand to seal for a new one, and the one it is sealed
    # for would stop working: a new key here would leave them to their password.
    offer = Offer(connection, answer=True)
    offer.offer = RecoveryOffer(
        offer.settings, offer.notes, offer.make, offer.show, secret_notes_on=lambda: True
    )
    notes(offer, NOTES_WORTH_KEEPING)

    offer.offer.check()

    assert offer.made == [] and offer.shown == []
