"""The start-up path through its dialogs, with the user's answers scripted."""

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import apsw
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from stickle.app.password_dialog import PasswordDialog
from stickle.app.recovery_dialog import Choice, Problem, RecoveryDialog, diagnostics
from stickle.app.startup import open_notes
from stickle.crypto.key_files import A
from stickle.data import schema
from stickle.data.database import open_database
from stickle.data.notes import NoteRepository
from stickle.data.schema import NotesDiff, open_store, schema_version
from stickle.platform.credentials import CredentialStore, CredentialStoreUnavailableError
from stickle.unlock import Unlock

FAST = (1, 8192)
PASSWORD = "correct horse"


class Backend:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.locked = False
        self.writes = 0

    def get_password(self, service: str, username: str) -> str | None:
        if self.locked:
            raise RuntimeError("locked")
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.writes += 1
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.items[(service, username)]


def store_of(backend: Backend | None) -> Callable[[], CredentialStore]:
    def make() -> CredentialStore:
        if backend is None:
            raise CredentialStoreUnavailableError("none on this system")
        return CredentialStore(backend)

    return make


def never(_: object) -> NoReturn:
    raise AssertionError("no dialog expected")


class Recovery:
    """Answers the recovery dialog, recording what it showed."""

    def __init__(self, *answers: Choice, before: Callable[[RecoveryDialog], None] | None = None):
        self.answers = list(answers)
        self.shown: list[RecoveryDialog] = []
        self.before = before

    def __call__(self, dialog: RecoveryDialog) -> Choice:
        self.shown.append(dialog)
        if self.before:
            self.before(dialog)
        return self.answers.pop(0)


def type_and_accept(dialog: PasswordDialog, password: str, confirm: str | None = None) -> None:
    dialog.password.setText(password)
    dialog.confirm.setText(password if confirm is None else confirm)
    dialog.accept()


def notes_folder(tmp_path: Path, backend: Backend) -> Path:
    """A folder with one note, its key in the backend."""
    unlock = Unlock(tmp_path, store_of(backend), FAST)
    connection = open_notes(unlock, never, never)
    assert connection is not None
    NoteRepository(connection).create("회의록을 내일까지")
    connection.close()
    return tmp_path


def files(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}


# A. First start with a credential store: no question at all.
def test_first_start_asks_nothing(qtbot: QtBot, tmp_path: Path) -> None:
    backend = Backend()
    connection = open_notes(Unlock(tmp_path, store_of(backend), FAST), never, never)
    assert connection is not None
    assert schema_version(connection) == schema.latest_version()
    connection.close()
    assert backend.writes == 1
    assert not (tmp_path / A).exists()


# C. Locked store with notes present: explained, then retried after unlocking.
def test_locked_store_is_explained_and_retried(qtbot: QtBot, tmp_path: Path) -> None:
    backend = Backend()
    notes_folder(tmp_path, backend)
    backend.locked = True

    def unlock_store(dialog: RecoveryDialog) -> None:
        assert dialog.problem.kind == "store_unavailable"
        assert dialog.retry_button.isVisibleTo(dialog)
        backend.locked = False

    recovery = Recovery(Choice.RETRY, before=unlock_store)
    connection = open_notes(Unlock(tmp_path, store_of(backend), FAST), never, recovery)

    assert connection is not None
    assert len(recovery.shown) == 1
    connection.close()
    assert backend.writes == 1  # the key made at the very start, never again


# D. Notes present but their key gone: nothing is created, nothing changes.
def test_missing_key_changes_nothing(qtbot: QtBot, tmp_path: Path) -> None:
    backend = Backend()
    notes_folder(tmp_path, backend)
    backend.items.clear()
    before = files(tmp_path)

    recovery = Recovery(Choice.QUIT)
    result = open_notes(Unlock(tmp_path, store_of(backend), FAST), never, recovery)

    assert result is None
    assert recovery.shown[0].problem.kind == "key_missing"
    assert backend.items == {}
    assert files(tmp_path) == before


# E. First start without a credential store: a password is created.
def test_first_start_without_a_store_creates_a_password(qtbot: QtBot, tmp_path: Path) -> None:
    def answer(dialog: PasswordDialog) -> bool:
        assert dialog.creating
        type_and_accept(dialog, "short")
        assert dialog.error.isVisibleTo(dialog)  # too short: stays open
        type_and_accept(dialog, PASSWORD, confirm="different")
        assert dialog.result() != PasswordDialog.DialogCode.Accepted
        type_and_accept(dialog, PASSWORD)
        return dialog.result() == PasswordDialog.DialogCode.Accepted

    connection = open_notes(Unlock(tmp_path, store_of(None), FAST), answer, never)

    assert connection is not None
    connection.close()
    assert (tmp_path / A).exists()


def test_quitting_at_the_first_password_leaves_no_files(qtbot: QtBot, tmp_path: Path) -> None:
    result = open_notes(Unlock(tmp_path, store_of(None), FAST), lambda _: False, never)
    assert result is None
    assert list(tmp_path.iterdir()) == []


# F. Later starts in password mode: wrong passwords change nothing, the right one opens.
def test_password_mode_later_starts(qtbot: QtBot, tmp_path: Path) -> None:
    first = open_notes(
        Unlock(tmp_path, store_of(None), FAST),
        lambda dialog: (type_and_accept(dialog, PASSWORD), True)[1],
        never,
    )
    assert first is not None
    note = NoteRepository(first).create("장보기")
    first.close()
    before = files(tmp_path)
    store_opened: list[int] = []

    def counting_store() -> CredentialStore:
        store_opened.append(1)
        raise CredentialStoreUnavailableError("should not be asked")

    def answer(dialog: PasswordDialog) -> bool:
        assert not dialog.creating
        type_and_accept(dialog, "wrong password")
        assert dialog.error.text() == dialog.wrong_password()
        assert dialog.password.text() == ""  # cleared for the next try
        assert files(tmp_path) == before
        type_and_accept(dialog, PASSWORD)
        return dialog.result() == PasswordDialog.DialogCode.Accepted

    connection = open_notes(Unlock(tmp_path, counting_store, FAST), answer, never)
    assert connection is not None
    assert NoteRepository(connection).get(note.id) == note
    connection.close()
    assert store_opened == []


# H. Notes saved by a newer version: explained, exportable, untouched.
def test_newer_notes_can_be_exported_and_stay_untouched(qtbot: QtBot, tmp_path: Path) -> None:
    backend = Backend()
    notes_folder(tmp_path, backend)
    key = CredentialStore(backend).read("local-database-key")
    assert key is not None
    connection = open_database(tmp_path / "notes.db", key)
    connection.pragma("user_version", 99)
    connection.close()
    before = files(tmp_path)
    exported: list[Path] = []

    def export(dialog: RecoveryDialog) -> None:
        assert dialog.problem.kind == "newer_version"
        assert not dialog.retry_button.isVisibleTo(dialog)
        assert dialog.export_button.isVisibleTo(dialog)
        exported.append(dialog._export(tmp_path / "out").folder)  # pyright: ignore[reportPrivateUsage, reportOptionalCall]

    result = open_notes(
        Unlock(tmp_path, store_of(backend), FAST),
        never,
        Recovery(Choice.QUIT, before=export),
    )

    assert result is None
    assert [p.read_text(encoding="utf-8") for p in exported[0].glob("*.md")] == [
        "회의록을 내일까지"
    ]
    assert files(tmp_path) == before


# I. A failed upgrade: the notes it would have changed are listed; a fixed app continues.
def test_failed_upgrade_lists_the_notes_then_continues(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    backend = Backend()
    current = list(schema.MIGRATIONS)  # the notes as this version leaves them
    notes_folder(tmp_path, backend)
    monkeypatch.setattr(schema, "MIGRATIONS", [*current, "DELETE FROM notes"])

    def fix_the_app(dialog: RecoveryDialog) -> None:
        assert dialog.problem.kind == "upgrade_failed"
        items = [dialog.notes.item(i).text() for i in range(dialog.notes.count())]
        assert items == ["Missing: 회의록을 내일까지"]
        assert "회의록" not in diagnostics(dialog.problem)
        monkeypatch.setattr(schema, "MIGRATIONS", [*current, "CREATE TABLE extra (x)"])

    recovery = Recovery(Choice.RETRY, before=fix_the_app)
    connection = open_notes(Unlock(tmp_path, store_of(backend), FAST), never, recovery)

    assert connection is not None
    assert schema_version(connection) == len(current) + 1
    assert [n.body for n in NoteRepository(connection).all()] == ["회의록을 내일까지"]
    connection.close()
    # J. The log tells what happened, without the note's text.
    assert "notes could not be opened" in caplog.text
    assert "회의록" not in caplog.text


def test_notes_from_another_key_cannot_be_retried(qtbot: QtBot, tmp_path: Path) -> None:
    open_store(tmp_path / "notes.db", secrets.token_bytes(32)).close()
    backend = Backend()
    CredentialStore(backend).get_or_create_key()

    recovery = Recovery(Choice.QUIT)
    assert open_notes(Unlock(tmp_path, store_of(backend), FAST), never, recovery) is None
    dialog = recovery.shown[0]
    assert dialog.problem.kind == "wrong_key"
    assert not dialog.retry_button.isVisibleTo(dialog)
    assert not dialog.export_button.isVisibleTo(dialog)


# K. Keyboard and screen readers.
def test_password_dialog_works_from_the_keyboard(qtbot: QtBot) -> None:
    submitted: list[str] = []
    dialog = PasswordDialog(False, lambda p: submitted.append(p))
    qtbot.addWidget(dialog)
    dialog.show()
    QTest.keyClicks(dialog.password, PASSWORD)
    QTest.keyClick(dialog.password, Qt.Key.Key_Return)

    assert submitted == [PASSWORD]
    assert dialog.result() == PasswordDialog.DialogCode.Accepted
    assert dialog.password.accessibleName() and dialog.confirm.accessibleName()

    other = PasswordDialog(False, lambda _: None)
    qtbot.addWidget(other)
    other.show()
    QTest.keyClick(other.password, Qt.Key.Key_Escape)
    assert other.result() == PasswordDialog.DialogCode.Rejected


def test_copied_details_hold_no_note_text(qtbot: QtBot, tmp_path: Path) -> None:
    diff = NotesDiff(missing=["비밀 메모"], changed=["다른 메모"])
    dialog = RecoveryDialog(
        Problem("upgrade_failed", "MigrationError: notes differ", diff), tmp_path
    )
    qtbot.addWidget(dialog)

    dialog.copy_button.click()

    from PySide6.QtGui import QGuiApplication

    copied = QGuiApplication.clipboard().text()
    assert "upgrade_failed" in copied and "1 missing, 1 changed" in copied
    assert "메모" not in copied


def test_korean_texts(qtbot: QtBot, translations: object, tmp_path: Path) -> None:
    from stickle.app.i18n import Translations

    assert isinstance(translations, Translations)
    translations.apply("ko")
    dialog = PasswordDialog(True, lambda _: None)
    recovery = RecoveryDialog(Problem("store_unavailable"), tmp_path)
    qtbot.addWidget(dialog)
    qtbot.addWidget(recovery)

    assert dialog.windowTitle() == "암호로 메모 보호하기"
    assert dialog.ok_button.text() == "암호 만들기"
    assert recovery.heading.text() == "시스템 키 저장소가 응답하지 않습니다"
    dialog.password.setText("짧음")
    dialog.accept()
    assert dialog.error.text() == "8자 이상 입력하세요."


def test_a_failing_database_open_is_reported_not_raised(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = Backend()
    notes_folder(tmp_path, backend)

    def broken(*_: object) -> apsw.Connection:
        raise apsw.IOError("disk I/O error")

    monkeypatch.setattr("stickle.app.startup.open_store", broken)
    recovery = Recovery(Choice.QUIT)
    assert open_notes(Unlock(tmp_path, store_of(backend), FAST), never, recovery) is None
    assert recovery.shown[0].problem.kind == "open_failed"


def test_log_folder_link_opens_the_logs(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    (tmp_path / "logs").mkdir()
    opened: list[str] = []

    def open_url(url: QUrl) -> bool:
        opened.append(url.toLocalFile())
        return True

    monkeypatch.setattr(QDesktopServices, "openUrl", open_url)
    dialog = RecoveryDialog(Problem("store_unavailable"), tmp_path)
    qtbot.addWidget(dialog)

    dialog.log_button.click()

    assert [Path(p) for p in opened] == [tmp_path / "logs"]
