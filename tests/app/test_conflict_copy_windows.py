"""A conflict copy on screen: its band (where and when its text was written,
[Show the original], [Use this text], [Keep both], or [Resolve] where they do
not fit), the dark cloud on it and on its original, the menu's way to each,
and what each does; reached with Tab too. The Stickle window says which notes
are copies."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from stickle.app.i18n import Translations
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.stickle_window import StickleWindow
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.versions import VersionRepository

KEY = secrets.token_bytes(32)
LAPTOP = "1a2b3c4d-0000-4000-8000-000000000000"
EDITED = 1_762_483_200_000
FIRST = "c1c1c1c1-0000-4000-8000-000000000000"
SECOND = "c2c2c2c2-0000-4000-8000-000000000000"


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def notes(connection: apsw.Connection) -> NoteRepository:
    return NoteRepository(connection)


def copy_of(
    notes: NoteRepository, original: str, text: str, copy_id: str = FIRST, here: bool = False
) -> str:
    """A copy as sync takes one in: of original, holding the laptop's text (here:
    this computer's)."""
    columns = {
        "conflict_of": original,
        "conflict_device": None if here else LAPTOP,
        "conflict_edited": EDITED,
    }
    notes.take_in(copy_id, text, None if here else LAPTOP, columns, {}, {})
    return copy_id


@pytest.fixture
def manager(
    qtbot: QtBot, connection: apsw.Connection, notes: NoteRepository
) -> Iterator[NoteManager]:
    manager = NoteManager(
        notes,
        idle_ms=20,
        max_ms=200,
        versions=VersionRepository(connection),
        device_names=lambda device: "Laptop" if device == LAPTOP else None,
    )
    yield manager
    for window in manager.windows:
        window.release()


def fronting(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """The notes brought to the front, by id, from now on."""
    fronted: list[str | None] = []

    def record(window: NoteWindow) -> None:
        fronted.append(window.note_id)

    monkeypatch.setattr(NoteWindow, "bring_to_front", record)
    return fronted


def opened(manager: NoteManager, *note_ids: str) -> list[NoteWindow]:
    manager.open_stored()
    found = [manager.window_for(note_id) for note_id in note_ids]
    assert all(each is not None for each in found)
    return [each for each in found if each is not None]


def test_a_copy_has_its_band_and_both_a_dark_cloud(
    manager: NoteManager, notes: NoteRepository
) -> None:
    original_id = notes.create("회의 11시").id
    original, copy = opened(manager, original_id, copy_of(notes, original_id, "회의 10시"))

    bar = copy.conflict_bar
    assert bar.isVisibleTo(copy)
    assert bar.line.text().startswith("Changed separately on Laptop · ")
    assert "the text from Laptop was kept in this copy" in bar.toolTip()
    assert copy.title_bar.category_tag.conflict == bar.toolTip()
    said = original.title_bar.category_tag.conflict
    assert said == "Text changed separately is in a copy. Click to show it."
    assert said in original.title_bar.category_tag.toolTip()
    assert original._conflict_bar is None  # pyright: ignore[reportPrivateUsage]  # never made
    for button in bar.shown():
        assert button.isEnabled() and button.text()


def test_who_wrote_the_copys_text_is_said_as_it_is_known(
    manager: NoteManager, notes: NoteRepository
) -> None:
    original_id = notes.create("원본").id
    mine = copy_of(notes, original_id, "이 PC의 글", FIRST, here=True)
    stranger = "9f9f9f9f-0000-4000-8000-000000000000"
    notes.take_in(
        SECOND,
        "모르는 PC의 글",
        stranger,
        {"conflict_of": original_id, "conflict_device": stranger, "conflict_edited": EDITED},
        {},
        {},
    )
    _, here, there = opened(manager, original_id, mine, SECOND)

    assert here.conflict_bar.line.text().startswith("Changed separately on this computer · ")
    assert there.conflict_bar.line.text().startswith("Changed separately on another computer · ")
    original = manager.window_for(original_id)
    assert original is not None
    assert original.title_bar.category_tag.conflict == (
        "Text changed separately is in 2 copies. Click to show them."
    )


def test_a_computers_name_shows_as_it_is_written(qtbot: QtBot, notes: NoteRepository) -> None:
    manager = NoteManager(notes, device_names=lambda _: "<b>노트북</b>")
    original_id = notes.create("원본").id
    [copy] = opened(manager, copy_of(notes, original_id, "사본"))
    try:
        assert copy.conflict_bar.line.textFormat() == Qt.TextFormat.PlainText
        assert copy.conflict_bar.line.text().startswith("Changed separately on <b>노트북</b> · ")
    finally:
        copy.release()


def test_show_the_original_brings_it_forward(
    manager: NoteManager, notes: NoteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    fronted = fronting(monkeypatch)
    original_id = notes.create("원본").id
    _, copy = opened(manager, original_id, copy_of(notes, original_id, "사본"))

    copy.conflict_bar.original_button.click()
    assert fronted == [original_id]
    copy.show_original_action.trigger()
    assert fronted == [original_id, original_id]


def test_the_originals_cloud_and_menu_show_each_copy_in_turn(
    manager: NoteManager, notes: NoteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    fronted = fronting(monkeypatch)
    original_id = notes.create("원본").id
    first = copy_of(notes, original_id, "첫 사본", FIRST)
    second = copy_of(notes, original_id, "둘째 사본", SECOND)
    [original] = opened(manager, original_id)

    original.show_copy_action.trigger()
    tag = original.title_bar.category_tag
    centre = tag.geometry().center()
    QTest.mouseClick(original.title_bar, Qt.MouseButton.LeftButton, pos=centre)
    original.show_copy_action.trigger()
    assert fronted == [first, second, first]

    QTest.mouseClick(original.title_bar, Qt.MouseButton.LeftButton, pos=QPoint(140, 10))
    assert len(fronted) == 3  # elsewhere on the bar: not the cloud


def test_use_this_text_trades_them_and_the_copy_goes(
    manager: NoteManager, notes: NoteRepository
) -> None:
    original_id = notes.create("회의 11시").id
    copy_id = copy_of(notes, original_id, "회의 10시")
    original, copy = opened(manager, original_id, copy_id)

    copy.conflict_bar.use_button.click()

    assert original.text == "회의 10시"
    assert manager.window_for(copy_id) is None
    moved = notes.get(copy_id)
    assert moved is not None and moved.body == "회의 11시" and moved.deleted_at is not None
    assert original.title_bar.category_tag.conflict is None
    assert notes.get(original_id).body == "회의 10시"  # pyright: ignore[reportOptionalMemberAccess]

    manager.restore_last_deleted()  # back: and the two can change places again
    back = manager.window_for(copy_id)
    assert back is not None and back.copy is not None and back.copy.here
    back.conflict_bar.use_button.click()
    assert original.text == "회의 11시"


def test_what_was_being_typed_is_saved_before_the_texts_change_places(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    original_id = notes.create("회의 11시").id
    copy_id = copy_of(notes, original_id, "회의 10시")
    original, copy = opened(manager, original_id, copy_id)
    original.edit()
    original.editor.moveCursor(QTextCursor.MoveOperation.End)
    original.editor.insertPlainText(" 3층")  # not saved yet
    copy.edit()
    copy.editor.moveCursor(QTextCursor.MoveOperation.End)
    copy.editor.insertPlainText(" 2층")

    copy.use_copy_action.trigger()

    assert original.text == "회의 10시 2층"
    moved = notes.get(copy_id)
    assert moved is not None and moved.body == "회의 11시 3층"


def test_keep_both_takes_the_band_and_the_clouds_away(
    manager: NoteManager, notes: NoteRepository
) -> None:
    original_id = notes.create("원본").id
    copy_id = copy_of(notes, original_id, "사본")
    original, copy = opened(manager, original_id, copy_id)

    copy.conflict_bar.keep_button.click()

    assert not copy.conflict_bar.isVisibleTo(copy)
    assert copy.title_bar.category_tag.conflict is None
    assert original.title_bar.category_tag.conflict is None
    copy.make_menu()
    for action in (copy.show_original_action, copy.use_copy_action, copy.keep_both_action):
        assert not action.isVisible()
    assert not original.show_copy_action.isVisible()
    assert notes.get(copy_id).conflict is None  # pyright: ignore[reportOptionalMemberAccess]


def test_the_copy_in_the_trash_takes_the_originals_cloud_away(
    manager: NoteManager, notes: NoteRepository
) -> None:
    original_id = notes.create("원본").id
    copy_id = copy_of(notes, original_id, "사본")
    original, copy = opened(manager, original_id, copy_id)

    manager.delete(copy)
    assert original.title_bar.category_tag.conflict is None
    manager.restore_note(copy_id)
    assert original.title_bar.category_tag.conflict is not None


def test_with_its_original_gone_only_keep_both_is_offered(
    manager: NoteManager, notes: NoteRepository
) -> None:
    original_id = notes.create("원본").id
    copy_id = copy_of(notes, original_id, "사본")
    notes.delete(original_id)
    [copy] = opened(manager, copy_id)

    assert copy.conflict_bar.shown() == [copy.conflict_bar.keep_button]
    copy.make_menu()
    copy.menu.aboutToShow.emit()
    assert copy.keep_both_action.isVisible()
    assert not copy.show_original_action.isVisible()
    assert not copy.use_copy_action.isVisible()


def test_a_narrow_note_offers_the_three_under_resolve(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    original_id = notes.create("원본").id
    [copy] = opened(manager, copy_of(notes, original_id, "사본"))
    bar = copy.conflict_bar
    needed = bar.needed_width()

    copy.resize(needed - 1, 240)
    qtbot.waitUntil(lambda: bar.shown() == [bar.resolve_button], timeout=2000)
    actions = [a for a in bar.resolve_menu.actions() if a.isVisible()]
    assert [a.text() for a in actions] == ["Show the original", "Use this text", "Keep both"]

    copy.resize(needed, 240)
    qtbot.waitUntil(lambda: len(bar.shown()) == 3, timeout=2000)


def test_tab_reaches_the_band_from_the_note_and_goes_back(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    original_id = notes.create("원본").id
    [copy] = opened(manager, copy_of(notes, original_id, "사본 [링크](https://example.com)"))
    copy.resize(copy.conflict_bar.needed_width() + 40, 240)
    copy.show()
    copy.activateWindow()
    qtbot.waitUntil(copy.isActiveWindow, timeout=3000)
    copy.view.setFocus()
    bar = copy.conflict_bar

    def focused() -> object:
        return QApplication.focusWidget()

    QTest.keyClick(copy.view, Qt.Key.Key_Tab)  # the link first
    assert focused() is copy.view and copy.view.stop == 0
    QTest.keyClick(copy.view, Qt.Key.Key_Tab)
    assert focused() is bar.original_button
    QTest.keyClick(bar.original_button, Qt.Key.Key_Tab)
    QTest.keyClick(bar.use_button, Qt.Key.Key_Tab)
    assert focused() is bar.keep_button
    QTest.keyClick(bar.keep_button, Qt.Key.Key_Tab)
    assert focused() is copy.view
    QTest.keyClick(copy.view, Qt.Key.Key_Backtab)  # back: the link, then the band
    assert focused() is copy.view and copy.view.stop == 0
    QTest.keyClick(copy.view, Qt.Key.Key_Backtab)
    assert focused() is bar.keep_button


def test_the_stickle_window_says_which_notes_are_copies(
    qtbot: QtBot, manager: NoteManager, notes: NoteRepository
) -> None:
    original_id = notes.create("원본").id
    copy_of(notes, original_id, "사본")
    window = StickleWindow(manager, Translations(), lambda: None)
    try:
        window.open()
        rows = window.note_list.list
        texts = sorted(rows.item(row).text() for row in range(rows.count()))
        assert texts == ["사본 · conflict copy", "원본"]
    finally:
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
