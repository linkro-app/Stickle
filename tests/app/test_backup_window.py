"""The daily backup while Stickle runs, and going back to a backup."""

import logging
import secrets
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTranslator
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot
from update_translations import TS_DIR, compile_to

from stickle.app import application
from stickle.app import backups as app_backups
from stickle.app.application import backup_access, go_back_to
from stickle.app.backups import BackupAccess, DailyBackups, RestoreDialog
from stickle.app.i18n import Translations
from stickle.app.notes import NoteManager
from stickle.app.stickle_window import StickleWindow
from stickle.data import backups
from stickle.data.backups import Backup, Contents
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store

KEY = secrets.token_bytes(32)


class FakeClock:
    def __init__(self, now: str = "2026-10-18T12:00:00.000Z") -> None:
        self.now = now

    def __call__(self) -> str:
        return self.now


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "notes.db"


@pytest.fixture
def connection(database: Path) -> Iterator[apsw.Connection]:
    connection = open_store(database, KEY)
    yield connection
    connection.close()


def test_the_days_backup_is_taken_once(
    qtbot: QtBot, connection: apsw.Connection, database: Path
) -> None:
    del qtbot
    daily = DailyBackups(connection, database, FakeClock())

    assert daily.run_now() is not None
    assert daily.run_now() is None
    assert len(backups.backups(database)) == 1


def test_a_failed_backup_is_logged_and_nothing_pops_up(
    qtbot: QtBot,
    connection: apsw.Connection,
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    del qtbot

    def full_disk(*_: object) -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(app_backups, "take_daily", full_disk)

    with caplog.at_level(logging.WARNING):
        assert DailyBackups(connection, database).run_now() is None

    assert "could not back up the notes: OSError" in caplog.text
    assert str(database.parent) not in caplog.text


def at(hour: int) -> datetime:
    return datetime(2026, 10, 18, hour, tzinfo=UTC)


def access_for(
    found: Sequence[tuple[Backup, Contents | None]], restored: list[Path], works: bool = True
) -> BackupAccess:
    def restore(path: Path) -> bool:
        restored.append(path)
        return works

    return BackupAccess(
        available=lambda: [backup for backup, _ in found],
        contents=lambda path: dict((b.path, c) for b, c in found)[path],
        restore=restore,
    )


def yes(_parent: QWidget, _title: str, _text: str, _go: str) -> bool:
    return True


def rows(dialog: RestoreDialog) -> list[tuple[str, bool]]:
    return [
        (
            dialog.backups.item(row).text(),
            bool(dialog.backups.item(row).flags() & Qt.ItemFlag.ItemIsEnabled),
        )
        for row in range(dialog.backups.count())
    ]


def test_the_backups_are_listed_with_their_notes(qtbot: QtBot, tmp_path: Path) -> None:
    found = [
        (Backup(tmp_path / "c.db", at(12), False), Contents(3, newer=False)),
        (Backup(tmp_path / "b.db", at(11), True), Contents(1, newer=False)),
        (Backup(tmp_path / "a.db", at(10), False), None),
        (Backup(tmp_path / "z.db", at(9), False), Contents(0, newer=True)),
    ]
    english = QTranslator()
    compile_to(TS_DIR / "stickle_en.ts", tmp_path / "stickle_en.qm")
    assert english.load(str(tmp_path / "stickle_en.qm"))
    QCoreApplication.installTranslator(english)
    try:
        dialog = RestoreDialog(access_for(found, []))
        qtbot.addWidget(dialog)
        listed = rows(dialog)
    finally:
        QCoreApplication.removeTranslator(english)

    assert [enabled for _, enabled in listed] == [True, True, False, False]
    assert listed[0][0].endswith("— 3 notes")
    assert listed[1][0].endswith("— 1 note, from before going back")
    assert listed[2][0].endswith("— cannot be opened")
    assert listed[3][0].endswith("— made by a newer Stickle")
    assert dialog.backups.currentRow() == 0
    assert dialog.empty.isHidden()


def test_the_rows_read_in_korean(qtbot: QtBot, tmp_path: Path, translations: Translations) -> None:
    translations.apply("ko")
    found = [(Backup(tmp_path / "c.db", at(12), False), Contents(3, newer=False))]
    dialog = RestoreDialog(access_for(found, []))
    qtbot.addWidget(dialog)

    assert rows(dialog)[0][0].endswith("— 메모 3개")
    assert dialog.windowTitle() == "백업에서 되돌리기"


def test_with_no_backup_yet_it_says_so(qtbot: QtBot) -> None:
    dialog = RestoreDialog(access_for([], []))
    qtbot.addWidget(dialog)

    assert not dialog.empty.isHidden()
    assert not dialog.restore_button.isEnabled()


def test_going_back_is_asked_first(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = [(Backup(tmp_path / "c.db", at(12), False), Contents(3, newer=False))]
    restored: list[Path] = []
    asked: list[str] = []
    answer = False

    def confirm(_parent: QWidget, _title: str, text: str, _go: str) -> bool:
        asked.append(text)
        return answer

    monkeypatch.setattr(app_backups, "confirm", confirm)
    dialog = RestoreDialog(access_for(found, restored))
    qtbot.addWidget(dialog)

    dialog.restore_chosen()
    assert restored == []
    assert "backed up first" in asked[0]

    answer = True
    dialog.restore_chosen()
    assert restored == [tmp_path / "c.db"]
    assert dialog.problem.isHidden()


def test_a_failed_restore_says_nothing_changed(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = [(Backup(tmp_path / "c.db", at(12), False), Contents(3, newer=False))]
    monkeypatch.setattr(app_backups, "confirm", yes)
    dialog = RestoreDialog(access_for(found, [], works=False))
    qtbot.addWidget(dialog)

    dialog.restore_chosen()

    assert not dialog.problem.isHidden()
    assert "Nothing was changed" in dialog.problem.text()


def test_going_back_backs_up_first_then_quits(
    qtbot: QtBot, connection: apsw.Connection, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(application, "_restore_staged", False)
    notes = NoteRepository(connection)
    notes.create("그때")
    chosen = backups.take(connection, database, FakeClock("2026-10-17T12:00:00.000Z"))
    notes.create("지금")
    quits: list[bool] = []

    assert go_back_to(
        chosen, NoteManager(notes), connection, database, lambda: quits.append(True), FakeClock()
    )

    qtbot.waitUntil(lambda: quits == [True])
    assert application.restore_staged()
    before = [b for b in backups.backups(database) if b.before_restore]
    assert len(before) == 1
    assert database.with_name("notes.db.restoring").exists()


def test_a_restore_that_cannot_be_staged_changes_nothing(
    qtbot: QtBot,
    connection: apsw.Connection,
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del qtbot
    monkeypatch.setattr(application, "_restore_staged", False)
    quits: list[bool] = []

    assert not go_back_to(
        tmp_path / "gone.db",
        NoteManager(NoteRepository(connection)),
        connection,
        database,
        lambda: quits.append(True),
    )

    assert not application.restore_staged()
    assert not database.with_name("notes.db.restoring").exists()
    assert quits == []


def test_the_stickle_window_offers_backups_only_with_them(
    qtbot: QtBot, connection: apsw.Connection, database: Path
) -> None:
    del qtbot
    manager = NoteManager(NoteRepository(connection))
    without = StickleWindow(manager, Translations(), lambda: None)
    access = backup_access(manager, connection, database, KEY, lambda: None)
    with_them = StickleWindow(manager, Translations(), lambda: None, backups=access)
    try:
        assert without.data_button.isHidden()
        assert not with_them.data_button.isHidden()
        with_them.restore_backup_action.trigger()
        assert with_them.restore_dialog is not None
        assert with_them.restore_dialog.isVisible()
    finally:  # left open, a later language switch would reach it after its database closed
        for window in (without, with_them):
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
