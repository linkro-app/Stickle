"""Stickle's own export, brought in through the window for bringing notes in."""

import secrets
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import apsw
import pytest
from PySide6.QtWidgets import QDialog, QWidget
from pytestqt.qtbot import QtBot

from stickle.app import import_dialog
from stickle.app.application import data_access
from stickle.app.import_dialog import ImportDialog
from stickle.app.notes import NoteManager
from stickle.app.secret_dialogs import ExportedSecretsDialog
from stickle.crypto.key_files import C, KeyFiles
from stickle.crypto.keyfile import parse_document
from stickle.crypto.recovery import generate, pair_for_export, read_recovery, write_recovery
from stickle.crypto.sealed import SealedSlot
from stickle.crypto.secret_notes import create, open_text, seal_text
from stickle.data.export import SecretExport, export_all
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store

KEY = secrets.token_bytes(32)
FAST = (1, 8192)
NOW = "2026-10-28T09:00:00.000Z"
PASSWORD = "secret notes 1"
HERE_KEY = secrets.token_bytes(32)  # the secret notes' key on this computer


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def manager(qtbot: QtBot, connection: apsw.Connection) -> Iterator[NoteManager]:
    del qtbot
    manager = NoteManager(NoteRepository(connection))
    yield manager
    for window in manager.windows:
        window.release()


@pytest.fixture
def export(tmp_path: Path) -> Path:
    """Another computer's export: a note, one in the trash, a secret note locked."""
    folder = tmp_path / "other"
    folder.mkdir()
    other = open_store(folder / "notes.db", KEY)
    notes = NoteRepository(other)
    notes.create("회의록", "blue", note_id="note-1")
    notes.create("지운 것", note_id="note-2")
    notes.delete("note-2")
    files = KeyFiles(folder)
    write_recovery(files, KEY, generate(), NOW)
    slot = read_recovery(files)
    assert isinstance(slot, SealedSlot)
    key = create(files, PASSWORD, slot.public, NOW, FAST)
    notes.create("", note_id="note-3")
    notes.make_secret("note-3", "금고", seal_text(key, "note-3", "금고 번호"))
    raw = files.raw(C)
    assert raw is not None
    copies = SecretExport(key_copies=parse_document(raw), recovery_pair=pair_for_export(files))
    out = tmp_path / "out"
    out.mkdir()
    exported = export_all(other, out, "0.6.0", secret=copies).folder
    other.close()
    return exported


def window_for(
    connection: apsw.Connection,
    manager: NoteManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chosen: Path,
) -> ImportDialog:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nobody")  # no other app's notes
    access = data_access(manager, connection, tmp_path / "notes.db", KEY, lambda: None)

    def key_here(_parent: QWidget, _typed: str | None, _count: int) -> bytes:
        return HERE_KEY

    importing = replace(access.importing, secret_key_here=key_here)
    dialog = ImportDialog(importing)
    dialog.refresh()

    def choose(_parent: QWidget, _title: str) -> Path:
        return chosen

    monkeypatch.setattr(import_dialog, "choose_folder", choose)
    dialog.choose_export()
    return dialog


def answering(monkeypatch: pytest.MonkeyPatch, password: str | None) -> list[str]:
    """The window for the export's secret notes answered with password, or with
    Bring in without them (None); what it said when refused."""
    said: list[str] = []

    def ask(dialog: QDialog) -> bool:
        assert isinstance(dialog, ExportedSecretsDialog)
        if password is None:
            dialog.go_on_without()
            return True
        dialog.password.setText(password)
        dialog.accept()
        if dialog.result() != QDialog.DialogCode.Accepted:
            said.append(dialog.message.text())
            return False
        return True

    monkeypatch.setattr(import_dialog, "ask_exported", ask)
    return said


def test_an_export_is_previewed_as_what_it_brings(
    qtbot: QtBot,
    connection: apsw.Connection,
    manager: NoteManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    export: Path,
) -> None:
    dialog = window_for(connection, manager, tmp_path, monkeypatch, export)
    qtbot.addWidget(dialog)

    row = dialog.sources.item(dialog.sources.currentRow()).text()
    assert row.startswith("Stickle export of") and "3 new note" in row  # (no plurals here)
    assert "go into the trash" in dialog.summary.text()
    assert "went out locked" in dialog.summary.text()
    assert dialog.source_box.isHidden()  # its own notes: no line saying where from
    assert dialog.preview.count() == 3
    assert NoteRepository(connection).all() == []


def test_with_the_password_of_the_day_the_secret_note_comes_locked_with_the_key_here(
    qtbot: QtBot,
    connection: apsw.Connection,
    manager: NoteManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    export: Path,
) -> None:
    dialog = window_for(connection, manager, tmp_path, monkeypatch, export)
    qtbot.addWidget(dialog)
    refused = answering(monkeypatch, "not the password")
    dialog.bring_in()
    assert refused == ["The password is not correct."]
    assert NoteRepository(connection).all() == []  # nothing came

    answering(monkeypatch, PASSWORD)
    dialog.bring_in()

    secret = NoteRepository(connection).get("note-3")
    assert secret is not None and secret.secret and secret.sealed is not None
    assert open_text(HERE_KEY, "note-3", secret.sealed) == "금고 번호"
    trashed = NoteRepository(connection).get("note-2")
    assert trashed is not None and trashed.deleted
    assert sorted(w.text for w in manager.windows if not w.secret) == ["회의록"]
    assert "locked with this computer's password" in dialog.outcome.text()
    assert not dialog.bring_button.isEnabled()


def test_without_them_the_secret_notes_wait_for_another_time(
    qtbot: QtBot,
    connection: apsw.Connection,
    manager: NoteManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    export: Path,
) -> None:
    dialog = window_for(connection, manager, tmp_path, monkeypatch, export)
    qtbot.addWidget(dialog)
    answering(monkeypatch, None)

    dialog.bring_in()

    assert NoteRepository(connection).get("note-3") is None
    assert "were left out" in dialog.outcome.text()
    assert "1 new note" in dialog.sources.item(dialog.sources.currentRow()).text()
    answering(monkeypatch, PASSWORD)
    dialog.bring_in()
    assert NoteRepository(connection).get("note-3") is not None


def test_a_folder_without_an_export_is_said_so(
    qtbot: QtBot,
    connection: apsw.Connection,
    manager: NoteManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    dialog = window_for(connection, manager, tmp_path, monkeypatch, empty)
    qtbot.addWidget(dialog)

    assert "no Stickle export" in dialog.outcome.text()
    assert dialog.sources.count() == 0
