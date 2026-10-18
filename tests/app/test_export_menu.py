"""Exporting everything from the Data menu."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from stickle.app import data_menu, file_dialogs
from stickle.app.application import data_access
from stickle.app.data_menu import export_everything
from stickle.app.notes import NoteManager
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store

KEY = secrets.token_bytes(32)


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


class Told:
    def __init__(self) -> None:
        self.exported: list[tuple[str, Path]] = []
        self.failed: list[str] = []

    def tell_exported(self, _parent: QWidget, _title: str, text: str, folder: Path) -> None:
        self.exported.append((text, folder))

    def tell_failed(self, _parent: QWidget, _title: str, text: str) -> None:
        self.failed.append(text)


@pytest.fixture
def told(monkeypatch: pytest.MonkeyPatch) -> Told:
    told = Told()
    monkeypatch.setattr(data_menu, "tell_exported", told.tell_exported)
    monkeypatch.setattr(data_menu, "tell_failed", told.tell_failed)
    return told


def choose(monkeypatch: pytest.MonkeyPatch, folder: Path | None) -> None:
    def existing_folder(_parent: QWidget, _title: str) -> Path | None:
        return folder

    monkeypatch.setattr(file_dialogs, "existing_folder", existing_folder)


def test_what_is_being_typed_goes_out_too(
    qtbot: QtBot,
    manager: NoteManager,
    connection: apsw.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    told: Told,
) -> None:
    window = manager.new_note()
    window.editor.insertPlainText("아직 저장 전인 글")
    choose(monkeypatch, tmp_path / "out")
    (tmp_path / "out").mkdir()
    parent = QWidget()
    qtbot.addWidget(parent)

    result = export_everything(
        parent, data_access(manager, connection, tmp_path / "notes.db", KEY, lambda: None)
    )

    assert result is not None
    texts = [p.read_text(encoding="utf-8") for p in result.folder.glob("*.md")]
    assert texts == ["아직 저장 전인 글"]
    ((text, folder),) = told.exported
    assert folder == result.folder
    assert "1 note" in text or "note(s)" in text
    assert "not encrypted" in text
    assert told.failed == []


def test_cancelling_the_folder_does_nothing(
    qtbot: QtBot,
    manager: NoteManager,
    connection: apsw.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    told: Told,
) -> None:
    choose(monkeypatch, None)
    parent = QWidget()
    qtbot.addWidget(parent)

    access = data_access(manager, connection, tmp_path / "notes.db", KEY, lambda: None)
    assert export_everything(parent, access) is None

    assert told.exported == [] and told.failed == []


def test_a_folder_that_cannot_be_written_is_said(
    qtbot: QtBot,
    manager: NoteManager,
    connection: apsw.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    told: Told,
) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    choose(monkeypatch, blocked)
    parent = QWidget()
    qtbot.addWidget(parent)

    access = data_access(manager, connection, tmp_path / "notes.db", KEY, lambda: None)
    assert export_everything(parent, access) is None

    assert told.exported == []
    assert len(told.failed) == 1
