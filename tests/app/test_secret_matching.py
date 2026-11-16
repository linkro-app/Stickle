"""Matching this computer's secret notes' password with the others': the one
typed is tried on both keys; both open, they are matched at once, one only,
the other is asked for. Matched, this computer's secret notes are sealed again
with the others' key, every open one holds it, and it is told once at the top
of Stickle Manager. Later leaves both as they are, the note opening with the
key that opened (if it is sealed with it), and asks again next time; a note
sealed with the other key then stays locked, unsaid. Forgotten, the recovery
key that key is sealed for opens it: this computer's sets no new password (its
notes switch to the others'), the others' sets one, as when it is forgotten
alone. With nothing sealed with this computer's key, theirs is taken at once."""

import secrets
import uuid
from collections.abc import Callable

import apsw
import pytest
import test_secret_note_windows as windows
from PySide6.QtWidgets import QDialog, QMessageBox
from pytestqt.qtbot import QtBot
from test_secret_note_windows import (
    SECRET,
    Answers,
    accepted,
    cancel,
    kept_new_recovery_key,
    new_password,
    password,
    recovery_key_typed,
    secret_note,
    stored,
    type_into,
)
from test_startup_dialogs import FAST

from stickle.app import application, secret_dialogs
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.protection_changes import ProtectionChanges
from stickle.app.recovery_key_dialog import EnterRecoveryKeyDialog
from stickle.app.secret_dialogs import MatchSecretDialog
from stickle.core.note import SealedText
from stickle.crypto.keyfile import encode_slot, wrap_with_password
from stickle.crypto.recovery import read_recovery
from stickle.crypto.sealed import SealedSlot, seal
from stickle.crypto.secret_notes import PASSWORD_SLOT, open_sealed, seal_text
from stickle.data.notes import NoteRepository
from stickle.data.settings import PROTECTION_CHANGES, Settings
from stickle.unlock import Unlock

# The secret notes' windows' fixtures, as they are there.
connection, unlock = windows.connection, windows.unlock
answers, manager = windows.answers, windows.manager

THEIRS = "the others' password 7"  # of the computers this one syncs with
THEIR_KEY = secrets.token_bytes(32)
THEIR_NUMBER = str(uuid.uuid4())

type Told = list[tuple[int, bool]]


def their_slots(
    unlock: Unlock, password: str = THEIRS, key: bytes = THEIR_KEY
) -> tuple[dict[str, object], dict[str, object]]:
    """The slots of the others' key, as the vault has them (sealed for the
    recovery key these notes have: synced, it is theirs too)."""
    slot = wrap_with_password(key, password, *FAST, PASSWORD_SLOT, "2026-11-15T09:00:00.000Z")
    pair = read_recovery(unlock.files)
    assert isinstance(pair, SealedSlot)
    import base64

    recovery: dict[str, object] = {
        "public": base64.b64encode(pair.public).decode(),
        "sealed": base64.b64encode(seal(pair.public, key)).decode(),
    }
    return encode_slot(slot), recovery


def matching_on(
    manager: NoteManager, unlock: Unlock, connection: apsw.Connection, vault: str | None = None
) -> Told:
    """The secret notes' windows as Stickle has them on a computer that syncs
    (vault: the number of the vault's key); what was told once matched."""
    told: Told = []
    manager.secrets = application.secret_passwords(
        unlock,
        application.master_passwords(unlock),
        ProtectionChanges(Settings(connection)),
        application.recovery_keys(unlock, Settings(connection)),
        manager.secret_key_fits,
        matched=application.MatchedHere(
            lambda: vault, manager.sealed_here, manager.match_secret_keys, manager.adopt_apart_key
        ),
    )
    manager.secrets_matched = lambda count, old_key: told.append((count, old_key))
    return told


def theirs_kept(unlock: Unlock, password: str = THEIRS) -> None:
    """A round brought the vault's key, another than this computer's."""
    assert unlock.keep_other_secret_key(THEIR_NUMBER, *their_slots(unlock, password))


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch, answers: Answers) -> Answers:
    """The windows matching asks with answered as the others are."""
    monkeypatch.setattr(secret_dialogs, "ask_match", answers)
    monkeypatch.setattr(secret_dialogs, "ask_recovery_key", answers)
    monkeypatch.setattr(secret_dialogs, "ask_new_password", answers)
    return answers


def switch(*typed: str, said: list[str] | None = None) -> Callable[[QDialog], bool]:
    """Each typed in turn until one is taken (said: what it said of the others)."""

    def answer(dialog: QDialog) -> bool:
        assert isinstance(dialog, MatchSecretDialog)
        for each in typed:
            dialog.password.setText(each)
            if accepted(dialog):
                return True
            if said is not None:
                said.append(dialog.message.text())
        return False

    return answer


def recovery_typed(recovery_key: str) -> Callable[[QDialog], bool]:
    typed = recovery_key_typed(recovery_key)

    def answer(dialog: QDialog) -> bool:
        assert isinstance(dialog, EnterRecoveryKeyDialog)
        return typed(dialog)

    return answer


def new_one(typed: str) -> Callable[[QDialog], bool]:
    chosen = new_password(typed)

    def answer(dialog: QDialog) -> bool:
        assert isinstance(dialog, secret_dialogs.NewSecretPasswordDialog)
        return chosen(dialog)

    return answer


def forgot(dialog: QDialog) -> bool:
    assert isinstance(dialog, MatchSecretDialog)
    dialog.forgot_button.click()
    return dialog.result() == QDialog.DialogCode.Accepted.value


def own_note(
    qtbot: QtBot, manager: NoteManager, answers: Answers, text: str, first: bool = True
) -> NoteWindow:
    """A secret note of this computer's, made before sync brought the others' key, locked."""
    window = secret_note(qtbot, manager, answers, text, first)
    assert manager.lock_secret(window)
    return window


def their_note(
    manager: NoteManager,
    connection: apsw.Connection,
    text: str,
    key: bytes = THEIR_KEY,
    number: str = THEIR_NUMBER,
) -> NoteWindow:
    """A secret note sync brought, sealed with the others' key: shown locked."""
    note_id = str(uuid.uuid4())
    sealed = SealedText(seal_text(key, note_id, text), note_id, number)
    NoteRepository(connection).take_in(note_id, sealed, None, {"secret_title": "그쪽"}, {}, {})
    manager.take_in([note_id])
    window = manager.window_for(note_id)
    assert window is not None and window.secret_locked
    return window


def opens(window: NoteWindow) -> None:
    window.open_secret_requested.emit()


def sealed_with_theirs(connection: apsw.Connection, window: NoteWindow) -> str:
    note = stored(connection, window)
    sealed = note.sealed_text
    assert sealed is not None
    return open_sealed(THEIR_KEY, note.id, sealed)


def test_one_password_for_both_matches_them_at_once(
    qtbot: QtBot, manager: NoteManager, asked: Answers, unlock: Unlock, connection: apsw.Connection
) -> None:
    told = matching_on(manager, unlock, connection, THEIR_NUMBER)
    mine = own_note(qtbot, manager, asked, "이 PC의 비밀")
    theirs_kept(unlock, SECRET)  # the same password there

    asked.queue += [password(SECRET)]
    opens(mine)

    assert not any(isinstance(each, MatchSecretDialog) for each in asked.asked)
    assert mine.text == "이 PC의 비밀"
    assert unlock.secret_notes_number == THEIR_NUMBER and unlock.other_secret_keys() == []
    assert sealed_with_theirs(connection, mine) == "이 PC의 비밀"
    assert stored(connection, mine).key_number == THEIR_NUMBER
    assert told == [(1, False)]


def test_this_computers_password_asks_for_theirs_and_switches_to_it(
    qtbot: QtBot, manager: NoteManager, asked: Answers, unlock: Unlock, connection: apsw.Connection
) -> None:
    told = matching_on(manager, unlock, connection, THEIR_NUMBER)
    mine = own_note(qtbot, manager, asked, "이 PC의 비밀")
    theirs_kept(unlock)

    said: list[str] = []
    asked.queue += [password(SECRET), switch("not theirs", THEIRS, said=said)]
    opens(mine)

    [dialog] = [each for each in asked.asked if isinstance(each, MatchSecretDialog)]
    assert dialog.kind == "here"
    assert said == ["The password is not correct."]
    assert mine.text == "이 PC의 비밀"
    assert unlock.open_secret_notes(THEIRS) == THEIR_KEY
    assert sealed_with_theirs(connection, mine) == "이 PC의 비밀"
    assert told == [(1, False)]
    # Edited, it is saved sealed with the key of now.
    type_into(mine, " 고침")
    assert manager.flush(mine)
    assert sealed_with_theirs(connection, mine).endswith(" 고침")


def test_later_opens_with_this_computers_and_asks_again_next_time(
    qtbot: QtBot, manager: NoteManager, asked: Answers, unlock: Unlock, connection: apsw.Connection
) -> None:
    told = matching_on(manager, unlock, connection, THEIR_NUMBER)
    mine = own_note(qtbot, manager, asked, "이 PC의 비밀")
    theirs_kept(unlock)
    own_number = unlock.secret_notes_number

    asked.queue += [password(SECRET), cancel]
    opens(mine)

    assert mine.text == "이 PC의 비밀" and not mine.secret_locked
    assert unlock.secret_notes_number == own_number and told == []
    assert manager.lock_secret(mine)
    asked.queue += [password(SECRET), cancel]
    opens(mine)
    assert sum(isinstance(each, MatchSecretDialog) for each in asked.asked) == 2


def test_their_password_asks_for_this_ones_and_later_opens_only_theirs(
    qtbot: QtBot,
    manager: NoteManager,
    asked: Answers,
    unlock: Unlock,
    connection: apsw.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warned: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warned.append(str(args[2])))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    matching_on(manager, unlock, connection, THEIR_NUMBER)
    mine = own_note(qtbot, manager, asked, "이 PC의 비밀")
    theirs_kept(unlock)
    there = their_note(manager, connection, "그쪽 비밀")

    asked.queue += [password(THEIRS), cancel]
    opens(there)

    [dialog] = [each for each in asked.asked if isinstance(each, MatchSecretDialog)]
    assert dialog.kind == "there"
    assert there.text == "그쪽 비밀"
    type_into(there, " 고침")
    assert manager.flush(there)
    assert stored(connection, there).key_number == THEIR_NUMBER  # saved with their key
    assert sealed_with_theirs(connection, there) == "그쪽 비밀 고침"

    asked.queue += [password(THEIRS), cancel]
    opens(mine)  # sealed with this computer's: it stays locked, unsaid
    assert mine.secret_locked and warned == []


def test_this_computers_forgotten_its_recovery_key_opens_it_and_no_password_is_set(
    qtbot: QtBot, manager: NoteManager, asked: Answers, unlock: Unlock, connection: apsw.Connection
) -> None:
    recovery_key = kept_new_recovery_key(unlock, connection)
    told = matching_on(manager, unlock, connection, THEIR_NUMBER)
    mine = own_note(qtbot, manager, asked, "이 PC의 비밀")
    theirs_kept(unlock)
    there = their_note(manager, connection, "그쪽 비밀")

    asked.queue += [password(THEIRS), forgot, recovery_typed(recovery_key)]
    opens(there)

    assert there.text == "그쪽 비밀"
    assert not any(isinstance(each, secret_dialogs.NewSecretPasswordDialog) for each in asked.asked)
    assert unlock.open_secret_notes(THEIRS) == THEIR_KEY
    assert sealed_with_theirs(connection, mine) == "이 PC의 비밀"
    assert told == [(1, False)]


def test_theirs_forgotten_the_recovery_key_opens_it_and_a_new_password_is_set(
    qtbot: QtBot, manager: NoteManager, asked: Answers, unlock: Unlock, connection: apsw.Connection
) -> None:
    recovery_key = kept_new_recovery_key(unlock, connection)
    matching_on(manager, unlock, connection, THEIR_NUMBER)
    mine = own_note(qtbot, manager, asked, "이 PC의 비밀")
    theirs_kept(unlock)

    asked.queue += [
        password(SECRET),
        forgot,
        recovery_typed(recovery_key),
        new_one("everyone's new one 9"),
    ]
    opens(mine)

    assert mine.text == "이 PC의 비밀"
    assert unlock.open_secret_notes("everyone's new one 9") == THEIR_KEY
    changes = Settings(connection).get(PROTECTION_CHANGES)
    assert [entry["what"] for entry in changes] == ["secret_password_reset"]


def test_with_nothing_sealed_with_its_own_theirs_is_taken_without_asking(
    manager: NoteManager, asked: Answers, unlock: Unlock, connection: apsw.Connection
) -> None:
    told = matching_on(manager, unlock, connection, THEIR_NUMBER)
    unlock.turn_on_secret_notes(SECRET)
    theirs_kept(unlock)
    there = their_note(manager, connection, "그쪽 비밀")

    asked.queue += [password(THEIRS)]
    opens(there)

    assert there.text == "그쪽 비밀"
    assert not any(isinstance(each, MatchSecretDialog) for each in asked.asked)
    assert unlock.secret_notes_number == THEIR_NUMBER and told == []


def test_one_waiting_to_be_matched_says_so_at_its_corner(
    qtbot: QtBot, manager: NoteManager, asked: Answers, unlock: Unlock, connection: apsw.Connection
) -> None:
    matching_on(manager, unlock, connection, THEIR_NUMBER)
    mine = own_note(qtbot, manager, asked, "이 PC의 비밀")
    assert mine.note_id is not None

    manager.show_unmatched({mine.note_id})

    tag = mine.title_bar.category_tag
    assert tag.unmatched
    assert tag.toolTip().endswith(
        "Only on this computer. It won't go to your other computers until its password is matched."
    )
    manager.show_unmatched(frozenset())
    assert not tag.unmatched


def test_the_match_window_says_which_password_it_asks_for(qtbot: QtBot) -> None:
    def never(_typed: str) -> bytes:
        raise AssertionError

    here = MatchSecretDialog("here", 3, never, never)
    there = MatchSecretDialog("there", 3, never, never)
    none_here = MatchSecretDialog("here", 0, never, never)
    for dialog in (here, there, none_here):
        qtbot.addWidget(dialog)
        assert dialog.windowTitle() == "Match secret note passwords"
        assert dialog.ok_button.text() == "&Switch" and dialog.cancel_button.text() == "&Later"

    assert "the 3 secret note" in here.intro.text()
    assert here.password.accessibleName() == (
        "Secret notes' password of the computers that already sync"
    )
    assert there.intro.text().startswith("3 secret note")
    assert "on this computer are locked with a different password" in there.intro.text()
    assert there.password.accessibleName() == "The password this computer used for them"
    assert "switch" not in none_here.intro.text()


def test_the_recovery_key_window_says_which_old_one(qtbot: QtBot) -> None:
    def submit(_typed: str) -> str | None:
        return None

    before_sync = EnterRecoveryKeyDialog(submit)
    before_new = EnterRecoveryKeyDialog(submit)
    for dialog in (before_sync, before_new):
        qtbot.addWidget(dialog)
    secret_dialogs.ask_for_the_old_one(before_sync, True)
    secret_dialogs.ask_for_the_old_one(before_new, False)

    assert before_sync.note.text() == (
        "Enter the recovery key this computer used before you turned on sync."
    )
    assert before_new.note.text() == (
        "Enter the recovery key you used before a new one was made on another computer."
    )


def test_matched_a_note_open_already_is_saved_with_the_key_of_now(
    qtbot: QtBot, manager: NoteManager, asked: Answers, unlock: Unlock, connection: apsw.Connection
) -> None:
    matching_on(manager, unlock, connection, THEIR_NUMBER)
    first = own_note(qtbot, manager, asked, "첫째 비밀")
    second = own_note(qtbot, manager, asked, "둘째 비밀", first=False)
    theirs_kept(unlock)
    asked.queue += [password(SECRET), cancel]
    opens(first)  # open with this computer's key, not matched yet
    assert first.text == "첫째 비밀"

    asked.queue += [password(SECRET), switch(THEIRS)]
    opens(second)  # matched now

    type_into(first, " 고침")
    assert manager.flush(first)
    assert sealed_with_theirs(connection, first) == "첫째 비밀 고침"
    assert stored(connection, first).key_number is None  # this computer's key, theirs now


# A key another computer used apart


APART = "the laptop's own password"
APART_KEY = secrets.token_bytes(32)
APART_NUMBER = str(uuid.uuid4())


def apart_kept(unlock: Unlock, password: str = APART, by: str | None = "노트북") -> None:
    """A round brought a key another computer used apart, which a note here is sealed with."""
    slots = their_slots(unlock, password, APART_KEY)
    assert unlock.keep_other_secret_key(APART_NUMBER, *slots, vault=False, by=by)


def apart_note(manager: NoteManager, connection: apsw.Connection) -> NoteWindow:
    return their_note(manager, connection, "노트북의 비밀", APART_KEY, APART_NUMBER)


def sealed_here(unlock: Unlock, connection: apsw.Connection, window: NoteWindow) -> str:
    note = stored(connection, window)
    assert note.sealed_text is not None and note.key_number is None
    return open_sealed(unlock.open_secret_notes(SECRET), note.id, note.sealed_text)


@pytest.fixture
def warned(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    said: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: said.append(str(args[2])))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    return said


def test_its_password_moves_its_notes_to_this_computers_key(
    manager: NoteManager, asked: Answers, unlock: Unlock, connection: apsw.Connection
) -> None:
    unlock.turn_on_secret_notes(SECRET)
    matching_on(manager, unlock, connection, unlock.secret_notes_number)
    apart_kept(unlock)
    window = apart_note(manager, connection)

    asked.queue += [password(SECRET), switch("not it", APART)]
    window.open_secret_requested.emit()

    [dialog] = [each for each in asked.asked if isinstance(each, MatchSecretDialog)]
    assert dialog.kind == "apart"
    assert dialog.intro.text().startswith(
        "This secret note is locked with the password 노트북 used for its own secret notes."
    )
    assert window.text == "노트북의 비밀"
    assert sealed_here(unlock, connection, window) == "노트북의 비밀"
    assert unlock.apart_secret_keys() == {}
    assert unlock.secret_old_keys(unlock.open_secret_notes(SECRET)) == {APART_NUMBER: APART_KEY}


def test_later_it_stays_locked_unsaid_and_is_asked_again(
    manager: NoteManager,
    asked: Answers,
    unlock: Unlock,
    connection: apsw.Connection,
    warned: list[str],
) -> None:
    unlock.turn_on_secret_notes(SECRET)
    matching_on(manager, unlock, connection, unlock.secret_notes_number)
    apart_kept(unlock, by=None)
    window = apart_note(manager, connection)

    asked.queue += [password(SECRET), cancel, password(SECRET), cancel]
    window.open_secret_requested.emit()
    window.open_secret_requested.emit()

    dialogs = [each for each in asked.asked if isinstance(each, MatchSecretDialog)]
    assert len(dialogs) == 2 and "another computer" in dialogs[0].intro.text()
    assert window.secret_locked and warned == []
    assert stored(connection, window).key_number == APART_NUMBER


def test_its_password_alone_opens_the_note_and_moves_nothing(
    manager: NoteManager, asked: Answers, unlock: Unlock, connection: apsw.Connection
) -> None:
    unlock.turn_on_secret_notes(SECRET)
    matching_on(manager, unlock, connection, unlock.secret_notes_number)
    apart_kept(unlock)
    window = apart_note(manager, connection)

    asked.queue += [password(APART)]
    window.open_secret_requested.emit()

    assert window.text == "노트북의 비밀"
    assert not any(isinstance(each, MatchSecretDialog) for each in asked.asked)
    assert stored(connection, window).key_number == APART_NUMBER
    assert APART_NUMBER in unlock.apart_secret_keys()


def test_one_password_for_both_moves_them_at_once(
    manager: NoteManager, asked: Answers, unlock: Unlock, connection: apsw.Connection
) -> None:
    unlock.turn_on_secret_notes(SECRET)
    matching_on(manager, unlock, connection, unlock.secret_notes_number)
    apart_kept(unlock, SECRET)
    window = apart_note(manager, connection)

    asked.queue += [password(SECRET)]
    window.open_secret_requested.emit()

    assert not any(isinstance(each, MatchSecretDialog) for each in asked.asked)
    assert sealed_here(unlock, connection, window) == "노트북의 비밀"


def test_forgotten_the_recovery_key_opens_it_and_no_password_is_set(
    manager: NoteManager, asked: Answers, unlock: Unlock, connection: apsw.Connection
) -> None:
    recovery_key = kept_new_recovery_key(unlock, connection)
    unlock.turn_on_secret_notes(SECRET)
    matching_on(manager, unlock, connection, unlock.secret_notes_number)
    apart_kept(unlock)
    window = apart_note(manager, connection)

    asked.queue += [password(SECRET), forgot, recovery_typed(recovery_key)]
    window.open_secret_requested.emit()

    assert window.text == "노트북의 비밀"
    assert not any(isinstance(each, secret_dialogs.NewSecretPasswordDialog) for each in asked.asked)
    assert sealed_here(unlock, connection, window) == "노트북의 비밀"
