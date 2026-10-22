"""The formatted note: how Markdown looks, and switching to the text and back."""

from collections.abc import Iterator

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import (
    QFont,
    QImage,
    QInputMethodEvent,
    QMouseEvent,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextListFormat,
)
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from stickle.app import note_window
from stickle.app.note_view import LINE_SEPARATOR, render
from stickle.app.note_window import NoteWindow
from stickle.app.palette import qcolor
from stickle.core.colors import DEFAULT_COLOR, note_colors

DEFAULT = note_colors(DEFAULT_COLOR)
HIGHLIGHT = qcolor(DEFAULT.highlight)
CODE_BACKGROUND = qcolor(DEFAULT.code_background)

# Characters that make Markdown structure, so generated notes are more than plain text.
MARKDOWN = st.text(alphabet=st.sampled_from([*"-*_=#>`[]x ().1\n\t!한글abc😀", LINE_SEPARATOR]))


@pytest.fixture
def document(qtbot: QtBot) -> QTextDocument:
    return QTextDocument()


def blocks(document: QTextDocument) -> list[str]:
    found: list[str] = []
    block = document.begin()
    while block.isValid():
        found.append(block.text())
        block = block.next()
    return found


def char_format(document: QTextDocument, text: str) -> QTextCharFormat:
    """The format of the first character of text where it is shown."""
    found = document.find(text)
    assert not found.isNull(), text
    cursor = QTextCursor(document)
    cursor.setPosition(found.selectionStart() + 1)
    return cursor.charFormat()


def block_format(document: QTextDocument, text: str) -> QTextBlockFormat:
    found = document.find(text)
    assert not found.isNull(), text
    return found.block().blockFormat()


# What Markdown looks like


def test_inline_formats(document: QTextDocument) -> None:
    text = (
        "**굵게** *기울임* 한글 _밑줄 기울임_ ==형광== ~~취소~~ `코드` [링크](https://example.com)"
    )
    render(text, document, DEFAULT)

    assert blocks(document) == ["굵게 기울임 한글 밑줄 기울임 형광 취소 코드 링크"]
    assert char_format(document, "취소").fontStrikeOut()
    assert not char_format(document, "형광").fontStrikeOut()
    assert char_format(document, "굵게").fontWeight() == QFont.Weight.Bold
    assert char_format(document, "기울임").fontItalic()
    assert char_format(document, "밑줄 기울임").fontItalic()
    assert char_format(document, "형광").background().color() == HIGHLIGHT
    code = char_format(document, "코드")
    assert code.background().color() == CODE_BACKGROUND
    assert code.fontFamilies()
    link = char_format(document, "링크")
    assert link.isAnchor()
    assert link.anchorHref() == "https://example.com"
    assert link.fontUnderline()
    plain = char_format(document, "한글")
    assert plain.fontWeight() != QFont.Weight.Bold
    assert not plain.fontItalic()


def test_headings_are_bold_and_the_first_two_larger(document: QTextDocument) -> None:
    render("# One\n## Two\n### Three", document, DEFAULT)

    for text, level in (("One", 1), ("Two", 2), ("Three", 3)):
        assert block_format(document, text).headingLevel() == level
        assert char_format(document, text).fontWeight() == QFont.Weight.Bold
    size = QTextFormat.Property.FontSizeAdjustment
    assert char_format(document, "One").intProperty(size) == 2
    assert char_format(document, "Two").intProperty(size) == 1
    assert char_format(document, "Three").intProperty(size) == 0


def test_lists_keep_their_kind_numbering_and_depth(document: QTextDocument) -> None:
    render("- top\n  - inner\n\n3) three\n4) four", document, DEFAULT)

    top = document.find("top").block().textList()
    inner = document.find("inner").block().textList()
    ordered = document.find("three").block().textList()
    assert top is not None and inner is not None and ordered is not None
    assert top.format().style() == QTextListFormat.Style.ListDisc
    assert inner.format().indent() == top.format().indent() + 1
    assert ordered.format().style() == QTextListFormat.Style.ListDecimal
    assert ordered.format().start() == 3
    assert ordered.format().numberSuffix() == ")"
    assert document.find("four").block().textList() == ordered


def test_task_items_show_boxes_instead_of_brackets(document: QTextDocument) -> None:
    render("- [ ] to do\n- [x] done\n- plain", document, DEFAULT)

    assert blocks(document) == ["to do", "done", "plain"]
    assert block_format(document, "to do").marker() == QTextBlockFormat.MarkerType.Unchecked
    assert block_format(document, "done").marker() == QTextBlockFormat.MarkerType.Checked
    assert block_format(document, "plain").marker() == QTextBlockFormat.MarkerType.NoMarker


def test_quote_code_block_and_rule(document: QTextDocument) -> None:
    render("> quoted\n\n```\ncode\n  indented\n```\n\n---", document, DEFAULT)

    assert block_format(document, "quoted").leftMargin() > 0
    code = document.find("code").block()
    assert code.text() == f"code{LINE_SEPARATOR}  indented"
    assert code.blockFormat().background().color() == CODE_BACKGROUND
    assert char_format(document, "code").fontFamilies()
    # Only the block is shaded, not each line of text again.
    assert not char_format(document, "code").hasProperty(QTextFormat.Property.BackgroundBrush)
    rule = document.lastBlock().blockFormat()
    assert rule.hasProperty(QTextFormat.Property.BlockTrailingHorizontalRulerWidth)


def test_lines_show_as_they_were_typed(document: QTextDocument) -> None:
    render("one\ntwo\n\n\n\nthree", document, DEFAULT)

    assert blocks(document) == [f"one{LINE_SEPARATOR}two", "", "", "", "three"]


def test_leading_blank_lines_are_kept(document: QTextDocument) -> None:
    render("\n\ntext", document, DEFAULT)

    assert blocks(document) == ["", "", "text"]


def test_html_shows_as_typed_and_images_are_never_loaded(document: QTextDocument) -> None:
    text = '<img src="https://example.com/a.png">\n\n![a cat](https://example.com/cat.png)'
    render(text, document, DEFAULT)

    assert blocks(document) == ['<img src="https://example.com/a.png">', "", "a cat"]
    block = document.begin()
    while block.isValid():
        iterator = block.begin()
        while not iterator.atEnd():
            assert not iterator.fragment().charFormat().isImageFormat()
            iterator += 1
        block = block.next()


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@given(MARKDOWN)
def test_any_text_can_be_drawn(document: QTextDocument, text: str) -> None:
    sources = render(text, document, DEFAULT)

    lines = text.count("\n") + 1
    assert len(sources) == document.blockCount()
    assert all(0 <= s.first_line <= s.last_line < lines for s in sources)


# Switching between the formatted note and its text


@pytest.fixture
def window(qtbot: QtBot) -> Iterator[NoteWindow]:
    window = NoteWindow(text="hello **world**\n\nsecond")
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    yield window
    window.release()


def point_of(window: NoteWindow, text: str, offset: int) -> QPoint:
    """Where, in the view, the character offset characters into text is shown."""
    found = window.view.document().find(text)
    cursor = QTextCursor(window.view.document())
    cursor.setPosition(found.selectionStart() + offset)
    rect = window.view.cursorRect(cursor)
    return QPoint(rect.left() + 1, rect.center().y())


def click(window: NoteWindow, point: QPoint) -> None:
    QTest.mouseClick(window.view.viewport(), Qt.MouseButton.LeftButton, pos=point)


def double_click(window: NoteWindow, point: QPoint) -> None:
    viewport = window.view.viewport()
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseDClick(viewport, Qt.MouseButton.LeftButton, pos=point)
    # Let go as a person does: QTest leaves the button held after a double-click,
    # and every later mouse move in the tests would drag. On the note, as the
    # formatted view is gone once it is being edited.
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, pos=viewport.mapTo(window, point))


def test_note_with_text_opens_formatted(window: NoteWindow) -> None:
    assert not window.editing
    assert window.view.toPlainText() == "hello world\n\nsecond"


def test_empty_note_opens_ready_to_type(qtbot: QtBot) -> None:
    window = NoteWindow()
    qtbot.addWidget(window)

    assert window.editing
    window.release()


def test_the_formatted_note_shows_no_caret_but_still_copies(
    qtbot: QtBot, window: NoteWindow
) -> None:
    window.view.setFocus()
    window.view.selectAll()

    QTest.keyClick(window.view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)

    assert QApplication.clipboard().text() == "hello world\n\nsecond"
    assert not window.editing


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5])
def test_no_caret_is_left_where_the_note_was_clicked(
    qtbot: QtBot, window: NoteWindow, scale: float
) -> None:
    # A caret made the note look as if it were being edited. At scales such as
    # 150% a caret 0 pixels wide was still drawn as a line.
    view = window.view
    window.activateWindow()
    view.setFocus()
    qtbot.waitUntil(view.hasFocus)

    def drawn() -> QImage:
        size = view.viewport().size() * scale
        image = QImage(size, QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(scale)
        # Opaque: Qt draws a caret by inverting what is under it, and the view
        # itself has no background.
        image.fill(Qt.GlobalColor.white)
        view.viewport().render(image)
        return image

    before = drawn()
    click(window, point_of(window, "world", 2))
    assert drawn() == before
    QTest.keyClick(view, Qt.Key.Key_Right)  # the keyboard moves it, still unseen
    assert drawn() == before


def test_a_single_click_does_not_edit(window: NoteWindow) -> None:
    # Clicking to read or scroll a note must not start editing it.
    click(window, point_of(window, "world", 2))

    assert not window.editing


def test_double_click_edits_where_it_was_clicked(qtbot: QtBot, window: NoteWindow) -> None:
    double_click(window, point_of(window, "world", 2))

    assert window.editing
    qtbot.waitUntil(window.editor.hasFocus)
    assert window.editor.textCursor().position() == len("hello **wo")


def test_double_click_on_a_later_block_edits_there(window: NoteWindow) -> None:
    double_click(window, point_of(window, "second", 3))

    assert window.editor.textCursor().position() == len("hello **world**\n\nsec")


def test_dragging_selects_without_editing(window: NoteWindow) -> None:
    viewport = window.view.viewport()
    start, end = point_of(window, "hello", 0), point_of(window, "world", 3)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(viewport, end)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)

    assert not window.editing
    assert window.view.textCursor().hasSelection()


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_F2])
def test_enter_or_f2_edits_at_the_end(window: NoteWindow, key: Qt.Key) -> None:
    QTest.keyClick(window.view, key)

    assert window.editing
    assert window.editor.textCursor().position() == len(window.text)


def test_escape_shows_the_note_formatted_and_saves(qtbot: QtBot, window: NoteWindow) -> None:
    window.edit()
    qtbot.waitUntil(window.editor.hasFocus)

    with qtbot.waitSignal(window.editing_finished):
        QTest.keyClick(window.editor, Qt.Key.Key_Escape)

    assert not window.editing


def test_escape_leaves_an_empty_note_ready_to_type(qtbot: QtBot) -> None:
    window = NoteWindow()
    qtbot.addWidget(window)
    window.show()

    QTest.keyClick(window.editor, Qt.Key.Key_Escape)

    assert window.editing
    window.release()


def test_leaving_the_note_shows_it_formatted(qtbot: QtBot, window: NoteWindow) -> None:
    window.edit()
    qtbot.waitUntil(window.editor.hasFocus)
    window.editor.insertPlainText(" more")

    window.editor.clearFocus()

    qtbot.waitUntil(lambda: not window.editing)
    assert window.view.toPlainText().endswith("second more")


def test_opening_the_note_menu_keeps_editing(qtbot: QtBot, window: NoteWindow) -> None:
    window.edit()
    qtbot.waitUntil(window.editor.hasFocus)

    window.open_menu()
    qtbot.waitUntil(window.menu.isVisible)
    qtbot.wait(20)

    assert window.editing
    window.menu.close()


def test_character_committed_late_shows_formatted(
    qtbot: QtBot, window: NoteWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ibus on GNOME drops the character being composed when the focus leaves;
    # the note keeps it, and the formatted view shows it too.
    monkeypatch.setattr(note_window, "DROPPING_INPUT_METHODS", True)
    window.edit()
    qtbot.waitUntil(window.editor.hasFocus)
    QApplication.sendEvent(window.editor, QInputMethodEvent("한", []))

    window.editor.clearFocus()
    qtbot.waitUntil(lambda: not window.editing)
    QApplication.sendEvent(window.editor, QInputMethodEvent("", []))

    qtbot.waitUntil(lambda: window.view.toPlainText().endswith("second한"))


def test_both_views_have_accessible_names(window: NoteWindow) -> None:
    assert window.view.accessibleName()
    assert window.view.accessibleDescription()
    assert window.editor.accessibleName()


@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None, max_examples=40
)
@given(MARKDOWN, st.integers(1, 4))
def test_switching_never_changes_the_text(qtbot: QtBot, text: str, switches: int) -> None:
    window = NoteWindow(text=text)
    original = window.text
    changes: list[None] = []
    window.text_changed.connect(lambda: changes.append(None))

    for _ in range(switches):
        window.edit()
        window.show_formatted()

    assert window.text == original
    assert changes == []
    window.release()


# Checking a task item without editing


def box_of(window: NoteWindow, text: str) -> QPoint:
    """The middle of the checkbox drawn before the task item showing text."""
    view = window.view
    block = view.document().find(text).block()
    layout = block.layout()
    line = layout.lineAt(0)
    x = layout.position().x() + line.x() - view.document().indentWidth() / 2
    y = layout.position().y() + line.y() + line.height() / 2
    return QPoint(
        round(x) - view.horizontalScrollBar().value(), round(y) - view.verticalScrollBar().value()
    )


TASKS = "# 할 일 😀\n- [ ] 🍎 사과\n- [x] 우유\n> - [ ] 인용 안"


@pytest.fixture
def tasks(qtbot: QtBot) -> Iterator[NoteWindow]:
    window = NoteWindow(text=TASKS)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    yield window
    window.release()


@pytest.mark.parametrize(
    ("shown", "expected"),
    [
        ("🍎 사과", "# 할 일 😀\n- [x] 🍎 사과\n- [x] 우유\n> - [ ] 인용 안"),
        ("우유", "# 할 일 😀\n- [ ] 🍎 사과\n- [ ] 우유\n> - [ ] 인용 안"),
        ("인용 안", "# 할 일 😀\n- [ ] 🍎 사과\n- [x] 우유\n> - [x] 인용 안"),
    ],
)
def test_clicking_a_box_toggles_only_its_mark(tasks: NoteWindow, shown: str, expected: str) -> None:
    changes: list[None] = []
    tasks.text_changed.connect(lambda: changes.append(None))

    click(tasks, box_of(tasks, shown))

    assert tasks.text == expected
    assert changes == [None]  # saved like any other change
    assert not tasks.editing


def test_the_box_shows_its_new_state(tasks: NoteWindow) -> None:
    click(tasks, box_of(tasks, "🍎 사과"))

    block = tasks.view.document().find("🍎 사과").block()
    assert block.blockFormat().marker() == QTextBlockFormat.MarkerType.Checked


def test_the_pointer_is_an_arrow_and_a_hand_over_what_a_click_acts_on(qtbot: QtBot) -> None:
    # The text cursor over a formatted note looked as if it could be typed in.
    window = NoteWindow(text="- [ ] 우유 [문서](https://x.org)")
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    viewport = window.view.viewport()

    def pointer_over(point: QPoint) -> Qt.CursorShape:
        move = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(point),
            QPointF(viewport.mapToGlobal(point)),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(viewport, move)
        return viewport.cursor().shape()

    assert viewport.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert pointer_over(point_of(window, "우유", 1)) == Qt.CursorShape.ArrowCursor
    assert pointer_over(box_of(window, "우유")) == Qt.CursorShape.PointingHandCursor
    assert pointer_over(point_of(window, "문서", 1)) == Qt.CursorShape.PointingHandCursor
    assert pointer_over(point_of(window, "우유", 1)) == Qt.CursorShape.ArrowCursor

    window.edit()
    assert window.editor.viewport().cursor().shape() == Qt.CursorShape.IBeamCursor
    window.release()


def test_double_clicking_a_box_neither_edits_nor_toggles_twice(tasks: NoteWindow) -> None:
    double_click(tasks, box_of(tasks, "우유"))

    assert not tasks.editing
    assert tasks.text == TASKS.replace("[x] 우유", "[ ] 우유")  # toggled once


def test_a_toggle_can_be_undone(tasks: NoteWindow) -> None:
    click(tasks, box_of(tasks, "우유"))

    tasks.editor.undo()

    assert tasks.text == TASKS


def test_double_clicking_the_item_text_edits_instead(tasks: NoteWindow) -> None:
    double_click(tasks, point_of(tasks, "우유", 1))

    assert tasks.editing
    assert tasks.text == TASKS


def test_double_clicking_beside_a_plain_list_item_edits(qtbot: QtBot) -> None:
    window = NoteWindow(text="- plain\n- [ ] task")
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    double_click(window, box_of(window, "plain"))

    assert window.text == "- plain\n- [ ] task"
    assert window.editing
    window.release()


def test_a_long_note_opens_at_its_top(qtbot: QtBot) -> None:
    window = NoteWindow(text="# Title\n" + "\n".join(f"line {n}" for n in range(60)))
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    qtbot.wait(50)

    scroll = window.view.verticalScrollBar()
    assert scroll.maximum() > 0
    assert scroll.value() == 0
    window.release()


# Copying a code block

CODE_NOTE = "before\n\n```python\nprint('우유')\nx = 1\n```\n\nafter"


@pytest.fixture
def code_window(qtbot: QtBot) -> Iterator[NoteWindow]:
    window = NoteWindow(text=CODE_NOTE)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    yield window
    window.release()


def test_the_copy_button_shows_over_a_code_block_and_copies_its_code(
    code_window: NoteWindow,
) -> None:
    view = code_window.view
    QTest.mouseMove(view.viewport(), point_of(code_window, "print", 2))
    assert view.copy_button.isVisibleTo(view)
    QApplication.clipboard().clear()
    view.copy_button.click()
    assert QApplication.clipboard().text() == "print('우유')\nx = 1"
    assert code_window.text == CODE_NOTE  # nothing changes

    QTest.mouseMove(view.viewport(), point_of(code_window, "after", 2))
    assert not view.copy_button.isVisibleTo(view)


def test_the_keyboard_reaches_a_code_block_and_copies_it(code_window: NoteWindow) -> None:
    view = code_window.view
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Tab)
    assert view.stop is not None and view.stops[view.stop].code == "print('우유')\nx = 1"
    QApplication.clipboard().clear()
    QTest.keyClick(view, Qt.Key.Key_Return)
    assert QApplication.clipboard().text() == "print('우유')\nx = 1"
    assert not code_window.editing  # Enter copied, it did not edit
