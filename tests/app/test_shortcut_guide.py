"""The keys at a glance."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot
from test_shortcuts import System

from stickle.app.about_dialog import AboutDialog
from stickle.app.i18n import Translations
from stickle.app.note_list import NoteList
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.shortcut_guide import ShortcutGuide, guide
from stickle.app.shortcuts import GlobalShortcuts
from stickle.app.stickle_window import StickleWindow
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import SHORTCUTS, Settings

KEY = secrets.token_bytes(32)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


def keys_of(text: str) -> list[str]:
    """The key combinations a guide row names (as Qt writes them)."""
    return [part.strip() for part in text.split(",") if "+" in part or part.strip()[:1] == "F"]


def bound(widget: object) -> set[str]:
    """The keys a window answers to, by its actions and shortcuts."""
    found: set[str] = set()
    for action in widget.actions():  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownVariableType]
        for sequence in action.shortcuts():  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
            found.add(sequence.toString())  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
    for shortcut in widget.findChildren(QShortcut):  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownVariableType]
        found.add(shortcut.key().toString())  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
    return found


def test_the_note_keys_it_lists_are_the_notes_own(qtbot: QtBot) -> None:
    note = NoteWindow(text="x")
    qtbot.addWidget(note)
    rows = dict(guide()[0][1])
    answered = bound(note)
    for name in ("New note", "Hide the note", "Note menu", "These keys"):
        for keys in keys_of(rows[name]):
            assert keys in answered, (name, keys, answered)
    for keys in ("Ctrl+Tab", "Ctrl+Shift+Tab"):
        assert QKeySequence(keys).toString() in answered or "Ctrl+Backtab" in answered
    note.release()


def test_the_stickle_window_keys_it_lists_are_its_own(
    qtbot: QtBot, connection: apsw.Connection
) -> None:
    window = StickleWindow(NoteManager(NoteRepository(connection)), Translations(), lambda: None)
    rows = dict(guide()[2][1])
    answered = bound(window) | bound(window.findChild(NoteList))
    assert QKeySequence(QKeySequence.StandardKey.Find).toString() in answered
    assert "Ctrl+F" in rows["Search the notes"]
    for digit in range(10):
        assert f"Ctrl+{digit}" in answered
    assert {"F1", "Ctrl+/"} <= answered
    assert rows["Open the settings"] == "Ctrl+," and "Ctrl+," in answered
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_the_shortcuts_from_anywhere_show_their_keys_now(
    qtbot: QtBot, connection: apsw.Connection
) -> None:
    settings = Settings(connection)
    settings.set(SHORTCUTS, {"new-note": "Ctrl+Alt+M"})
    shortcuts = GlobalShortcuts(settings, System())
    shown = dict(ShortcutGuide(shortcuts).global_keys())
    assert shown["New note"] == "Ctrl+Alt+M"
    assert shown["Hide all notes for now, or show them again"] == "Ctrl+Alt+H"
    assert shown["Show only the notes of the next category"] == "none"


def test_without_shortcuts_from_anywhere_that_part_is_left_out(qtbot: QtBot) -> None:
    guide_window = ShortcutGuide(None)
    guide_window.fill()
    assert guide_window.rows()[0][0] == "In a note"


def test_f1_in_a_note_opens_it_and_esc_gives_the_keyboard_back(
    qtbot: QtBot, connection: apsw.Connection
) -> None:
    manager = NoteManager(NoteRepository(connection))
    window = StickleWindow(manager, Translations(), lambda: None)
    note = manager.new_note()
    qtbot.waitExposed(note)
    note.editor.setFocus()

    note.guide_action.trigger()  # F1
    assert window.guide.isVisible()
    QTest.keyClick(window.guide.list, Qt.Key.Key_Escape)

    assert not window.guide.isVisible()
    assert note.editor.hasFocus() or not note.isActiveWindow()  # back where it was
    QCoreApplication.processEvents()  # the focus change the guide caused, handled
    assert note.editing  # still open to type in
    note.release()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_the_same_key_closes_it(qtbot: QtBot) -> None:
    guide_window = ShortcutGuide(None)
    guide_window.open()
    QTest.keyClick(guide_window.list, Qt.Key.Key_F1)
    assert not guide_window.isVisible()
    guide_window.open()
    QTest.keyClick(guide_window.list, Qt.Key.Key_Slash, Qt.KeyboardModifier.ControlModifier)
    assert not guide_window.isVisible()


@pytest.mark.parametrize("language", ["en", "ko"])
def test_the_longest_row_keeps_clear_of_its_keys(
    qtbot: QtBot, translations: Translations, language: str
) -> None:
    del qtbot
    translations.apply(language)
    guide_window = ShortcutGuide(None)
    guide_window.open()
    metrics = guide_window.list.fontMetrics()
    widest = max(metrics.horizontalAdvance(what) for what, _ in guide_window.rows())

    # The keys start at the second column: clear of the widest text by the gap.
    assert guide_window.list.columnWidth(0) >= widest + guide_window.key_gap()
    guide_window.close()


def test_it_leads_to_the_guide_on_the_website(qtbot: QtBot, translations: Translations) -> None:
    del qtbot
    guide_window = ShortcutGuide(None)
    about = AboutDialog()

    assert 'href="https://stickle.linkro.co/en/guide"' in guide_window.more.text()
    assert 'href="https://stickle.linkro.co/en/guide"' in about.links.text()
    translations.apply("ko")
    assert 'href="https://stickle.linkro.co/ko/guide"' in guide_window.more.text()
    guide_window.deleteLater()
    about.deleteLater()


def test_it_follows_the_language(qtbot: QtBot, translations: Translations) -> None:
    guide_window = ShortcutGuide(None)
    guide_window.open()
    translations.apply("ko")
    assert guide_window.title.text() == "단축키"
    assert guide_window.rows()[0][0] == "메모에서"
    guide_window.close()
