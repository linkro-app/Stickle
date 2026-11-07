"""The "Earlier versions" window: newest first, each with when and where from
and its first line; the one picked shown formatted; going back to it as a new
edit (what the note held is a version then); deleting one or all; from the
note's menu while it has some, and from the Stickle window's list."""

import logging
import secrets
import tracemalloc
from collections.abc import Callable, Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QWidget
from pytestqt.qtbot import QtBot

from stickle.app import notes as notes_module
from stickle.app import versions_window
from stickle.app.note_list import NoteList
from stickle.app.notes import NoteManager
from stickle.app.times import local_time
from stickle.app.versions_window import EarlierVersionsDialog
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.versions import VersionRepository

KEY = secrets.token_bytes(32)

type Answer = Callable[[EarlierVersionsDialog], None]


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def repository(connection: apsw.Connection) -> NoteRepository:
    return NoteRepository(connection)


@pytest.fixture
def manager(qtbot: QtBot, connection: apsw.Connection) -> Iterator[NoteManager]:
    manager = NoteManager(
        NoteRepository(connection), idle_ms=20, max_ms=200, versions=VersionRepository(connection)
    )
    yield manager
    for window in manager.windows:
        window.release()


class Shown:
    """What the window does once shown, in place of the user (exec waits)."""

    def __init__(self) -> None:
        self.dialogs: list[EarlierVersionsDialog] = []
        self.answer: Answer = lambda _dialog: None

    def __call__(self, dialog: QDialog) -> bool:
        assert isinstance(dialog, EarlierVersionsDialog)
        self.dialogs.append(dialog)
        self.answer(dialog)
        return dialog.result() == QDialog.DialogCode.Accepted.value


@pytest.fixture
def shown(monkeypatch: pytest.MonkeyPatch) -> Shown:
    shown = Shown()
    monkeypatch.setattr(notes_module, "ask_versions", shown)
    return shown


@pytest.fixture
def confirmed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []

    def confirm(_parent: QWidget, question: str, _go: str) -> bool:
        asked.append(question)
        return True

    monkeypatch.setattr(versions_window, "confirm", confirm)
    return asked


def with_versions(repository: NoteRepository, *texts: str) -> str:
    """A note that had these texts, one edit each: the last is its text now."""
    note = repository.create(texts[0])
    for text in texts[1:]:
        repository.update_body(note.id, text, keep_earlier=True)
    return note.id


def rows(dialog: EarlierVersionsDialog) -> list[str]:
    return [dialog.list.item(row).text() for row in range(dialog.list.count())]


def test_newest_first_with_when_and_where_from_and_the_first_line(
    qtbot: QtBot, repository: NoteRepository, connection: apsw.Connection
) -> None:
    note_id = with_versions(repository, "# 장보기\n우유", "# 장보기\n우유, 빵", "# 장보기 끝")
    repository.replace_text(note_id, "노트북에서 고친 글", came_from="Laptop")
    kept = VersionRepository(connection)
    listed = kept.listed(note_id)

    dialog = EarlierVersionsDialog(kept, note_id, "장보기", "yellow")
    qtbot.addWidget(dialog)

    assert dialog.windowTitle() == "Earlier versions — 장보기"
    assert rows(dialog) == [
        f"{local_time(listed[0].at)} · Changed here · 장보기 끝",
        f"{local_time(listed[1].at)} · Changed here · 장보기",
        f"{local_time(listed[2].at)} · Changed here · 장보기",
    ]
    assert dialog.list.currentRow() == 0
    assert dialog.view.toPlainText() == "장보기 끝"  # formatted: the heading's text
    dialog.list.setCurrentRow(2)
    assert "우유" in dialog.view.toPlainText() and dialog.restore_button.isEnabled()
    assert "this computer only" in dialog.note.text()


def test_a_text_from_another_computer_says_where_from(
    qtbot: QtBot, repository: NoteRepository, connection: apsw.Connection
) -> None:
    note = repository.create("처음")
    repository.replace_text(note.id, "노트북에서 쓴 글", came_from="laptop-id")
    repository.update_body(note.id, "다시 여기서", keep_earlier=True)

    dialog = EarlierVersionsDialog(
        VersionRepository(connection),
        note.id,
        "처음",
        "yellow",
        device_name=lambda device: {"laptop-id": "노트북"}[device],
    )
    qtbot.addWidget(dialog)

    assert [r.split(" · ")[1:] for r in rows(dialog)] == [
        ["From 노트북", "노트북에서 쓴 글"],
        ["Changed here", "처음"],
    ]


def test_a_text_from_a_computer_not_known_yet_says_another(
    qtbot: QtBot, repository: NoteRepository, connection: apsw.Connection
) -> None:
    note = repository.create("처음")
    repository.replace_text(note.id, "어디선가 쓴 글", came_from="laptop-id")
    repository.update_body(note.id, "다시 여기서", keep_earlier=True)

    dialog = EarlierVersionsDialog(VersionRepository(connection), note.id, "처음", "yellow")
    qtbot.addWidget(dialog)

    assert rows(dialog)[0].split(" · ")[1] == "From another PC"


def test_going_back_from_the_notes_menu_is_a_new_edit_shown_in_the_note(
    qtbot: QtBot,
    manager: NoteManager,
    repository: NoteRepository,
    connection: apsw.Connection,
    shown: Shown,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    note_id = with_versions(repository, "처음 글", "둘째 글")
    manager.open_stored()
    window = manager.window_for(note_id)
    assert window is not None
    qtbot.waitExposed(window)
    window.make_menu()
    window._menu.aboutToShow.emit()  # pyright: ignore[reportPrivateUsage]
    assert window.versions_action.isVisible()

    def go_back_to_the_first(dialog: EarlierVersionsDialog) -> None:
        dialog.list.setCurrentRow(0)
        dialog.restore_chosen()

    shown.answer = go_back_to_the_first
    window.versions_action.trigger()

    assert window.text == "처음 글"
    note = repository.get(note_id)
    assert note is not None and note.body == "처음 글"
    kept = VersionRepository(connection)
    top = kept.listed(note_id)[0]
    assert top.restored and kept.text(note_id, top.id) == "둘째 글"
    logged = [r.getMessage() for r in caplog.records]
    assert "a note went back to an earlier version" in logged
    assert not any("처음" in line or "둘째" in line for line in logged)  # no text in the log


def test_what_the_note_held_unsaved_is_a_version_before_going_back(
    qtbot: QtBot, manager: NoteManager, repository: NoteRepository, shown: Shown
) -> None:
    note_id = with_versions(repository, "처음 글", "둘째 글")
    manager.open_stored()
    window = manager.window_for(note_id)
    assert window is not None
    window.edit()
    window.editor.insertPlainText(" 그리고 저장 전")  # not saved yet

    shown.answer = lambda dialog: dialog.list.setCurrentRow(0)  # only looked
    manager.show_versions(window)

    note = repository.get(note_id)
    assert note is not None and note.body == window.text  # saved first
    assert [rows(shown.dialogs[0])[0].split(" · ")[-1]] == ["둘째 글"]


def test_deleting_one_or_all_asks_and_the_window_closes_when_none_are_left(
    qtbot: QtBot, repository: NoteRepository, connection: apsw.Connection, confirmed: list[str]
) -> None:
    note_id = with_versions(repository, "하나", "둘", "셋", "넷")
    kept = VersionRepository(connection)
    dialog = EarlierVersionsDialog(kept, note_id, "하나", "yellow")
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.list.setCurrentRow(0)
    dialog.list.setFocus()
    QTest.keyClick(dialog.list, Qt.Key.Key_Delete)  # the key, as the button
    assert confirmed == ["Delete this version? This cannot be undone."]
    assert [r.split(" · ")[-1] for r in rows(dialog)] == ["둘", "하나"]

    dialog.delete_all()
    assert confirmed[-1] == "Delete all 2 earlier versions of this note? This cannot be undone."
    assert kept.listed(note_id) == [] and not dialog.isVisible()


def test_nothing_is_deleted_when_the_user_says_no(
    qtbot: QtBot,
    repository: NoteRepository,
    connection: apsw.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_parent: QWidget, _question: str, _go: str) -> bool:
        return False

    monkeypatch.setattr(versions_window, "confirm", refuse)
    note_id = with_versions(repository, "하나", "둘", "셋")
    kept = VersionRepository(connection)
    dialog = EarlierVersionsDialog(kept, note_id, "하나", "yellow")
    qtbot.addWidget(dialog)

    dialog.delete_chosen()
    dialog.delete_all()

    assert len(kept.listed(note_id)) == 2


def test_a_note_in_the_trash_shows_them_to_read_and_delete_but_not_go_back(
    qtbot: QtBot, manager: NoteManager, repository: NoteRepository, shown: Shown
) -> None:
    note_id = with_versions(repository, "버린 글", "버린 글, 고침")
    repository.delete(note_id)

    manager.show_versions_of(note_id)

    [dialog] = shown.dialogs
    assert rows(dialog) and not dialog.restore_button.isEnabled()
    assert dialog.delete_button.isEnabled()


def test_the_menu_item_is_there_only_while_it_has_some(
    qtbot: QtBot, manager: NoteManager, repository: NoteRepository
) -> None:
    note_id = with_versions(repository, "처음")
    manager.open_stored()
    window = manager.window_for(note_id)
    assert window is not None
    window.make_menu()
    window._menu.aboutToShow.emit()  # pyright: ignore[reportPrivateUsage]
    assert not window.versions_action.isVisible()

    repository.update_body(note_id, "고침", keep_earlier=True)
    window._menu.aboutToShow.emit()  # pyright: ignore[reportPrivateUsage]
    assert window.versions_action.isVisible()


def test_the_stickle_windows_list_offers_them_for_a_note_that_has_some(
    qtbot: QtBot, manager: NoteManager, repository: NoteRepository
) -> None:
    some = with_versions(repository, "있음", "있음, 고침")
    none = with_versions(repository, "없음")
    listing = NoteList(manager)
    qtbot.addWidget(listing)
    listing.refresh()

    def items(note_id: str) -> list[str]:
        for row in range(listing.list.count()):
            item = listing.list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == note_id:
                return [a.text() for a in listing.menu_for(item).actions() if a.text()]
        raise AssertionError(note_id)

    assert "Earlier versions…" in items(some)
    assert "Earlier versions…" not in items(none)


def test_the_list_reads_first_lines_and_only_the_one_picked_whole(
    qtbot: QtBot,
    repository: NoteRepository,
    connection: apsw.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    big = "줄 하나\n" + "가나다라마바사 " * 20_000  # some 300 KB each
    note_id = with_versions(repository, *(f"{number} {big}" for number in range(12)))
    kept = VersionRepository(connection)

    tracemalloc.start()
    try:
        listed = kept.listed(note_id)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(listed) == 11
    assert peak < 100_000  # their first lines only, none of them whole
    read: list[int] = []
    whole = kept.text

    def reading(owner: str, version_id: int) -> str | bytes | None:
        read.append(version_id)
        return whole(owner, version_id)

    monkeypatch.setattr(kept, "text", reading)
    dialog = EarlierVersionsDialog(kept, note_id, "큰 메모", "yellow")
    qtbot.addWidget(dialog)

    assert dialog.list.count() == 11 and read == [listed[0].id]  # the one picked, once
    dialog.list.setCurrentRow(3)
    assert read == [listed[0].id, listed[3].id]
