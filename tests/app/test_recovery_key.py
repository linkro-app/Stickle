"""Getting back to the notes with the recovery key, and making a new one."""

import logging
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QMessageBox
from pytestqt.qtbot import QtBot
from test_startup_dialogs import (
    FAST,
    PASSWORD,
    Backend,
    Recovery,
    files,
    never,
    store_of,
    type_and_accept,
)

from stickle.app import settings_window
from stickle.app.i18n import Translations
from stickle.app.notes import NoteManager
from stickle.app.password_dialog import PasswordDialog
from stickle.app.recovery_dialog import Choice, RecoveryDialog
from stickle.app.recovery_key_dialog import EnterRecoveryKeyDialog, KeptBox, RecoveryKeyDialog
from stickle.app.settings_window import RecoveryKeys, SettingsWindow
from stickle.app.startup import open_notes
from stickle.crypto.recovery import generate
from stickle.data.notes import NoteRepository
from stickle.platform.credentials import DATABASE_KEY, SERVICE
from stickle.unlock import Unlock


class Keys:
    """Types into the recovery key dialog, in turn, recording what it said."""

    def __init__(self, *typed: str | None) -> None:
        self.typed = list(typed)
        self.errors: list[str] = []

    def __call__(self, dialog: EnterRecoveryKeyDialog) -> bool:
        while self.typed:
            text = self.typed.pop(0)
            if text is None:
                return False  # Back
            dialog.key.setText(text)
            dialog.accept()
            if dialog.result() == EnterRecoveryKeyDialog.DialogCode.Accepted:
                return True
            self.errors.append(dialog.error.text())
        return False


def notes_with_recovery_key(folder: Path, backend: Backend | None) -> tuple[str, str]:
    """Notes with one note and a recovery key; returns the note text and the key."""

    def create_password(dialog: PasswordDialog) -> bool:
        type_and_accept(dialog, PASSWORD)
        return True

    ask = create_password if backend is None else never
    unlock = Unlock(folder, store_of(backend), FAST)
    connection = open_notes(unlock, ask, never)
    assert connection is not None
    assert unlock.opened_key is not None
    NoteRepository(connection).create("잃어버리면 안 되는 메모")
    connection.close()
    return "잃어버리면 안 되는 메모", unlock.make_recovery_key(unlock.opened_key)


def only_note(connection: object) -> str:
    import apsw

    assert isinstance(connection, apsw.Connection)
    (note,) = NoteRepository(connection).all()
    connection.close()
    return note.body


def test_a_key_lost_from_the_store_comes_back_with_the_recovery_key(
    qtbot: QtBot, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    backend = Backend()
    text, recovery_key = notes_with_recovery_key(tmp_path, backend)
    stored = dict(backend.items)
    backend.items.clear()  # a reset keychain

    def offer_recovery(dialog: RecoveryDialog) -> None:
        assert dialog.problem.kind == "key_missing"
        assert dialog.recover_button.isVisibleTo(dialog)

    recovery = Recovery(Choice.RECOVER, before=offer_recovery)
    keys = Keys(recovery_key.lower())
    connection = open_notes(Unlock(tmp_path, store_of(backend), FAST), never, recovery, keys)

    assert only_note(connection) == text
    assert backend.items == stored  # the same key, back in the store
    # And the next start needs nothing.
    assert only_note(open_notes(Unlock(tmp_path, store_of(backend), FAST), never, never)) == text
    assert recovery_key not in caplog.text
    assert recovery_key.replace("-", "") not in caplog.text


def test_mistyped_and_wrong_keys_are_explained_and_change_nothing(
    qtbot: QtBot, tmp_path: Path
) -> None:
    backend = Backend()
    text, recovery_key = notes_with_recovery_key(tmp_path, backend)
    backend.items.clear()
    before = files(tmp_path)

    keys = Keys(recovery_key[:-1], generate(), recovery_key)
    connection = open_notes(
        Unlock(tmp_path, store_of(backend), FAST), never, Recovery(Choice.RECOVER), keys
    )

    dialog = EnterRecoveryKeyDialog(lambda _: None)
    assert keys.errors == [dialog.typo(), dialog.wrong_key()]
    assert only_note(connection) == text
    assert {name: files(tmp_path)[name] for name in before if name != "notes.db"} == {
        name: content for name, content in before.items() if name != "notes.db"
    }


def test_an_explanation_is_shown_whole(qtbot: QtBot, translations: Translations) -> None:
    # A wrapped message was cut off at the bottom (Korean wraps to more lines).
    translations.apply("ko")
    dialog = EnterRecoveryKeyDialog(lambda _: dialog.typo())
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    dialog.key.setText("abc")
    dialog.ok_button.click()

    error = dialog.error
    assert error.isVisible()
    assert error.height() >= error.heightForWidth(error.width())
    assert error.geometry().bottom() < dialog.buttons.geometry().top()
    assert dialog.isVisible()


def test_going_back_from_the_recovery_key_changes_nothing(qtbot: QtBot, tmp_path: Path) -> None:
    backend = Backend()
    notes_with_recovery_key(tmp_path, backend)
    backend.items.clear()
    before = files(tmp_path)

    recovery = Recovery(Choice.RECOVER, Choice.QUIT)
    result = open_notes(Unlock(tmp_path, store_of(backend), FAST), never, recovery, Keys(None))

    assert result is None
    assert len(recovery.shown) == 2  # back to the explanation, then quit
    assert files(tmp_path) == before
    assert backend.items == {}


def test_without_a_recovery_key_it_is_not_offered(qtbot: QtBot, tmp_path: Path) -> None:
    backend = Backend()
    unlock = Unlock(tmp_path, store_of(backend), FAST)
    connection = open_notes(unlock, never, never)
    assert connection is not None
    connection.close()
    backend.items.clear()

    def no_recovery(dialog: RecoveryDialog) -> None:
        assert not dialog.recover_button.isVisibleTo(dialog)

    open_notes(
        Unlock(tmp_path, store_of(backend), FAST), never, Recovery(Choice.QUIT, before=no_recovery)
    )


def test_a_forgotten_password_is_replaced_after_the_recovery_key(
    qtbot: QtBot, tmp_path: Path
) -> None:
    text, recovery_key = notes_with_recovery_key(tmp_path, None)
    asked: list[str] = []

    def answer(dialog: PasswordDialog) -> bool:
        if not dialog.creating:
            asked.append("unlock")
            assert dialog.forgot_button.isVisibleTo(dialog)
            dialog.forgot_button.click()
            return False
        asked.append("new")
        assert dialog.after_recovery
        type_and_accept(dialog, "a brand new password")
        return dialog.result() == PasswordDialog.DialogCode.Accepted

    connection = open_notes(
        Unlock(tmp_path, store_of(None), FAST), answer, never, Keys(recovery_key)
    )

    assert asked == ["unlock", "new"]
    assert only_note(connection) == text

    def new_password(dialog: PasswordDialog) -> bool:
        type_and_accept(dialog, "a brand new password")
        return True

    again = open_notes(Unlock(tmp_path, store_of(None), FAST), new_password, never)
    assert only_note(again) == text


def test_a_damaged_key_file_is_set_aside_not_lost(qtbot: QtBot, tmp_path: Path) -> None:
    text, recovery_key = notes_with_recovery_key(tmp_path, None)
    (tmp_path / "keys.json").write_text("{ damaged")

    def new_password(dialog: PasswordDialog) -> bool:
        assert dialog.after_recovery
        type_and_accept(dialog, "a brand new password")
        return True

    connection = open_notes(
        Unlock(tmp_path, store_of(None), FAST),
        new_password,
        Recovery(Choice.RECOVER),
        Keys(recovery_key),
    )

    assert only_note(connection) == text
    assert (tmp_path / "keys.json.damaged").read_text() == "{ damaged"


def test_the_store_key_is_not_touched_by_a_new_recovery_key(qtbot: QtBot, tmp_path: Path) -> None:
    backend = Backend()
    notes_with_recovery_key(tmp_path, backend)
    key_before = backend.items[(SERVICE, DATABASE_KEY)]
    unlock = Unlock(tmp_path, store_of(backend), FAST)
    connection = open_notes(unlock, never, never)
    assert connection is not None and unlock.opened_key is not None
    connection.close()

    unlock.make_recovery_key(unlock.opened_key)

    assert backend.items[(SERVICE, DATABASE_KEY)] == key_before


# Making a new one in the settings


def test_a_new_recovery_key_replaces_the_old_one_after_asking(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = Backend()
    text, old_key = notes_with_recovery_key(tmp_path, backend)
    unlock = Unlock(tmp_path, store_of(backend), FAST)
    connection = open_notes(unlock, never, never)
    assert connection is not None and unlock.opened_key is not None
    key = unlock.opened_key
    connection.close()
    shown: list[str] = []
    asked: list[str] = []

    def question(*arguments: object) -> QMessageBox.StandardButton:
        asked.append(str(arguments[2]))
        return QMessageBox.StandardButton.Yes

    kept: list[None] = []

    def show(dialog: RecoveryKeyDialog) -> bool:
        shown.append(dialog.panel.recovery_key)
        return True  # Done

    monkeypatch.setattr(QMessageBox, "question", question)
    monkeypatch.setattr(settings_window, "show_recovery_key", show)
    window = SettingsWindow(
        NoteManager(),
        Translations(),
        recovery=RecoveryKeys(
            exists=lambda: unlock.has_recovery_key,
            make=lambda: unlock.make_recovery_key(key),
            kept=lambda: kept.append(None),
        ),
    )
    qtbot.addWidget(window)

    window.recovery_button.click()

    assert len(asked) == 1 and "no longer open" in asked[0]
    assert len(shown) == 1 and shown[0] != old_key
    assert kept == [None]  # Done counts as kept
    backend.items.clear()
    old = open_notes(
        Unlock(tmp_path, store_of(backend), FAST),
        never,
        Recovery(Choice.RECOVER, Choice.QUIT),
        Keys(old_key, None),
    )
    assert old is None  # the old key no longer opens them
    new = open_notes(
        Unlock(tmp_path, store_of(backend), FAST), never, Recovery(Choice.RECOVER), Keys(shown[0])
    )
    assert only_note(new) == text


def test_the_recovery_key_window_waits_until_it_is_kept(qtbot: QtBot) -> None:
    recovery_key = generate()
    dialog = RecoveryKeyDialog(recovery_key)
    qtbot.addWidget(dialog)

    assert not dialog.done_button.isEnabled()
    assert dialog.later_button.isEnabled()
    dialog.panel.copy_button.click()
    assert QGuiApplication.clipboard().text() == recovery_key
    dialog.panel.kept.setChecked(True)
    assert dialog.done_button.isEnabled()
    assert dialog.panel.key.text() == recovery_key
    assert dialog.panel.key.accessibleName()


def test_once_kept_enter_is_done_wherever_the_buttons_are(qtbot: QtBot) -> None:
    dialog = RecoveryKeyDialog(generate())
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    dialog.panel.kept.setChecked(True)
    assert dialog.done_button.isDefault()
    dialog.panel.kept.setFocus()
    QTest.keyClick(dialog.panel.kept, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_enter_on_the_kept_box_never_unticks_it(qtbot: QtBot) -> None:
    # Some desktops have Enter tick a check box: here Enter goes on to the window.
    box = KeptBox()
    qtbot.addWidget(box)
    box.setChecked(True)
    for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        press = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
        release = QKeyEvent(QEvent.Type.KeyRelease, key, Qt.KeyboardModifier.NoModifier)
        box.keyPressEvent(press)
        box.keyReleaseEvent(release)
        assert not press.isAccepted() and not release.isAccepted()
    assert box.isChecked()
