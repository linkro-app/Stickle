"""Bringing notes in from another app, through the window, into notes on screen."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from stickle.app import import_dialog
from stickle.app.application import data_access
from stickle.app.import_dialog import ImportDialog
from stickle.app.notes import NoteManager
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.importers import sources

KEY = secrets.token_bytes(32)
T = ""


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


def xpad_home(home: Path) -> Path:
    folder = home / ".config" / "xpad"
    folder.mkdir(parents=True)
    for number, (text, hidden) in enumerate([(f"{T}bold{T}장보기{T}/bold{T}", 0), ("숨김", 1)]):
        (folder / f"info-{number}").write_text(
            f"width 200\nheight 150\nx 99999\ny 99999\nfollow_font 1\nfollow_color 0\n"
            f"hidden {hidden}\nback rgb(255,192,203)\ncontent content-{number}\n",
            "utf-8",
        )
        (folder / f"content-{number}").write_text(text, "utf-8")
    return folder


def window_for(
    connection: apsw.Connection,
    manager: NoteManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ImportDialog:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    access = data_access(manager, connection, tmp_path / "notes.db", KEY, lambda: None)
    dialog = ImportDialog(access.importing)
    dialog.refresh()
    return dialog


def test_found_notes_are_shown_before_anything_comes(
    qtbot: QtBot,
    connection: apsw.Connection,
    manager: NoteManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xpad_home(tmp_path)
    dialog = window_for(connection, manager, tmp_path, monkeypatch)
    qtbot.addWidget(dialog)

    assert dialog.sources.count() == 1
    assert dialog.sources.item(0).text().startswith("xpad — 2")
    assert dialog.preview.count() == 2
    assert dialog.bring_button.isEnabled()
    assert NoteRepository(connection).all() == []  # nothing yet


def test_bringing_in_shows_the_notes_and_only_once(
    qtbot: QtBot,
    connection: apsw.Connection,
    manager: NoteManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = xpad_home(tmp_path)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    dialog = window_for(connection, manager, tmp_path, monkeypatch)
    qtbot.addWidget(dialog)

    dialog.bring_in()

    notes = NoteRepository(connection).all()
    assert sorted(n.body for n in notes) == ["**장보기**", "숨김"]
    assert {n.color for n in notes} == {"pink"}
    assert [w.text for w in manager.windows] == ["**장보기**"]  # the hidden one stays hidden
    assert not dialog.bring_button.isEnabled()
    assert "every note brought in already" in dialog.sources.item(0).text()
    assert not dialog.outcome.isHidden()
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before  # xpad's files untouched
    dialog.refresh()
    dialog.bring_in()
    assert len(NoteRepository(connection).all()) == 2


def test_nothing_found_says_so_and_offers_a_file(
    qtbot: QtBot,
    connection: apsw.Connection,
    manager: NoteManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = window_for(connection, manager, tmp_path, monkeypatch)
    qtbot.addWidget(dialog)

    assert not dialog.none_found.isHidden()
    assert dialog.sources.isHidden()
    assert not dialog.bring_button.isEnabled()

    copied = tmp_path / "elsewhere" / "indicator-stickynotes"
    copied.parent.mkdir()
    copied.write_text('{"notes": [{"uuid": "u1", "body": "옮겨 온 메모"}]}', "utf-8")

    def choose(_parent: QWidget, _title: str) -> Path:
        return copied

    monkeypatch.setattr(import_dialog, "choose_file", choose)
    dialog.choose()

    assert dialog.sources.count() == 1
    assert dialog.preview.count() == 1
    dialog.bring_in()
    assert [n.body for n in NoteRepository(connection).all()] == ["옮겨 온 메모"]


def test_an_unknown_file_is_said_not_read(
    qtbot: QtBot,
    connection: apsw.Connection,
    manager: NoteManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = tmp_path / "notes.txt"
    other.write_text("plain", "utf-8")

    def choose(_parent: QWidget, _title: str) -> Path:
        return other

    monkeypatch.setattr(import_dialog, "choose_file", choose)
    dialog = window_for(connection, manager, tmp_path, monkeypatch)
    qtbot.addWidget(dialog)

    dialog.choose()

    assert not dialog.outcome.isHidden()
    assert dialog.sources.count() == 0


def test_xpad_files_are_recognised_by_folder_or_file(tmp_path: Path) -> None:
    folder = xpad_home(tmp_path)

    by_folder = sources.recognise(folder)
    by_file = sources.recognise(folder / "content-0")

    assert by_folder is not None and by_folder[0].name == "xpad"
    assert by_file is not None and by_file[1].path == folder
