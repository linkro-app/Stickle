"""The formatted view of a note, drawn from its Markdown text.

The text itself lives only in the note's plain-text editor; this view reads
it and draws it, so switching between the two never changes the text. The
document is built directly from the parsed Markdown rather than through
HTML, which keeps every format under test and means nothing is ever loaded:
images show their description instead.

A line break inside a paragraph shows as a line break, and blank lines show
as the space they take in the text, so a note looks laid out as it was typed.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import override

from markdown_it.tree import SyntaxTreeNode
from PySide6.QtCore import (
    QDate,
    QDateTime,
    QEvent,
    QPoint,
    QRect,
    QRectF,
    Qt,
    QTime,
    Signal,
)
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QFont,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QTextBlock,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextLength,
    QTextList,
    QTextListFormat,
)
from PySide6.QtWidgets import QApplication, QTextEdit, QToolButton, QToolTip, QWidget

from stickle.app.fonts import code_family
from stickle.app.palette import qcolor
from stickle.app.times import moment_text
from stickle.core.colors import DEFAULT_COLOR, NoteColors, note_colors
from stickle.core.markdown import LINE_SEPARATOR, parse, source_position
from stickle.core.text_reminders import MARK, Code, codes

INDENT_WIDTH = 20
QUOTE_MARGIN = 14
# Font size steps above normal (Qt's scale, as for HTML headings), by heading level.
HEADING_SIZE = {1: 2, 2: 1}
BULLETS = (
    QTextListFormat.Style.ListDisc,
    QTextListFormat.Style.ListCircle,
    QTextListFormat.Style.ListSquare,
)


@dataclass(frozen=True)
class ReminderLook:
    """What the reminders written in a note need to be shown: a small "⏰ day,
    time" in place of each code, quieter once past (or on a checked line), and
    standing out while it rings."""

    now: datetime  # on this computer's clock
    morning: time  # when a day written without a time rings
    ringing: frozenset[datetime]  # the times written of those ringing now


def reminder_text(code: Code, morning: time) -> str:
    """A reminder written in a note, as the formatted note shows it."""
    when = code.when(morning)
    moment = QDateTime(QDate(when.year, when.month, when.day), QTime(when.hour, when.minute))
    shown = moment_text(moment)
    # A day picked in another calendar first, as written, then the day it rings.
    return f"{MARK} {code.note} ({shown})" if code.note else f"{MARK} {shown}"


@dataclass(frozen=True)
class BlockSource:
    """The lines of text a shown block was drawn from (inclusive)."""

    first_line: int
    last_line: int
    checkbox_line: int | None = None  # a task item: the line with its "[ ]"
    code: str | None = None  # a code block: its code, without the fences


@dataclass(frozen=True)
class Stop:
    """Something the keyboard can reach in the formatted note: a task item's
    checkbox (line set), a link (href set) or a code block (code set), between
    two positions of the view."""

    start: int
    end: int
    checkbox_line: int | None = None
    href: str = ""
    code: str | None = None


@dataclass
class _ListLevel:
    format: QTextListFormat
    items: QTextList | None = None
    item_starts: bool = False  # the next block is the first of a new item
    checked: bool | None = None  # that item's checkbox, if it is a task


def utf16_length(text: str) -> int:
    """Length as Qt counts positions: characters outside the BMP count twice."""
    return len(text.encode("utf-16-le")) // 2


def from_utf16(text: str, position: int) -> int:
    """The index into text of a Qt position within it."""
    count = 0
    for index, char in enumerate(text):
        if count >= position:
            return index
        count += 2 if ord(char) > 0xFFFF else 1
    return len(text)


class _Builder:
    def __init__(
        self, document: QTextDocument, colors: NoteColors, look: ReminderLook | None = None
    ) -> None:
        document.clear()
        document.setIndentWidth(INDENT_WIDTH)
        self._document = document
        self._highlight_color = qcolor(colors.highlight)
        self._code_color = qcolor(colors.code_background)
        self._quiet_color = qcolor(colors.title_icon)
        self._look = look
        self._cursor = QTextCursor(document)
        self._fresh = True  # the document's first block has not been used yet
        self._next_line = 0  # the first line not yet shown
        self.sources: list[BlockSource] = []
        self._lists: list[_ListLevel] = []
        self._quotes = 0
        self._heading = 0
        self._bold = 0
        self._italic = 0
        self._highlight = 0
        self._struck = 0
        self._code = False
        self._links: list[str] = []

    # Blocks

    def blocks(self, node: SyntaxTreeNode) -> None:
        for child in node.children:
            self._block(child)

    def _block(self, node: SyntaxTreeNode) -> None:
        first, end = node.map or (self._next_line, self._next_line + 1)
        # Blank lines before a list or quote belong outside it.
        self._blank_lines(first)
        match node.type:
            case "paragraph":
                self._start_block(first, end)
                self._inline_children(node)
            case "heading":
                level = int(node.tag[1:])
                block = QTextBlockFormat()
                block.setHeadingLevel(level)
                self._start_block(first, end, block)
                self._heading = level
                self._inline_children(node)
                self._heading = 0
            case "code_block" | "fence":
                block = QTextBlockFormat()
                block.setBackground(self._code_color)
                self._start_block(first, end, block)
                code = node.content.removesuffix("\n")
                self.sources[-1] = replace(self.sources[-1], code=code)
                self._code = True
                self._text(code.replace("\n", LINE_SEPARATOR))
                self._code = False
            case "hr":
                block = QTextBlockFormat()
                block.setProperty(
                    QTextFormat.Property.BlockTrailingHorizontalRulerWidth,
                    QTextLength(QTextLength.Type.PercentageLength, 100),
                )
                self._start_block(first, end, block)
            case "blockquote":
                self._quotes += 1
                self.blocks(node)
                self._quotes -= 1
            case "bullet_list" | "ordered_list":
                self._list(node)
            case "list_item":
                level = self._lists[-1]
                level.item_starts = True
                checked = node.meta.get("checked")
                level.checked = checked if isinstance(checked, bool) else None
                self.blocks(node)
                # An item with nothing in it still shows its bullet.
                if level.item_starts:
                    self._start_block(first, first + 1)
            case _:
                # Nothing else is produced; show its text rather than lose it.
                self._start_block(first, end)
                self._text(node.content)

    def _list(self, node: SyntaxTreeNode) -> None:
        depth = len(self._lists)
        list_format = QTextListFormat()
        list_format.setIndent(depth + 1)
        if node.type == "ordered_list":
            list_format.setStyle(QTextListFormat.Style.ListDecimal)
            start = node.attrs.get("start", 1)
            list_format.setStart(start if isinstance(start, int) else 1)
            item = node.children[0] if node.children else None
            list_format.setNumberSuffix(item.markup if item is not None else ".")
        else:
            list_format.setStyle(BULLETS[depth % len(BULLETS)])
        self._lists.append(_ListLevel(list_format))
        self.blocks(node)
        self._lists.pop()

    def _start_block(self, first: int, end: int, block: QTextBlockFormat | None = None) -> None:
        """Begin a block drawn from lines first..end-1, after any blank lines before it."""
        self._blank_lines(first)
        self._next_line = max(self._next_line, end)
        level = self._lists[-1] if self._lists else None
        starts_item = level is not None and level.item_starts
        checkbox_line = None
        block = block or QTextBlockFormat()
        if starts_item and level is not None and level.checked is not None:
            block.setMarker(
                QTextBlockFormat.MarkerType.Checked
                if level.checked
                else QTextBlockFormat.MarkerType.Unchecked
            )
            checkbox_line = first
        self._insert_block(block, BlockSource(first, end - 1, checkbox_line), starts_item)

    def _blank_lines(self, until: int) -> None:
        for line in range(self._next_line, until):
            self._insert_block(QTextBlockFormat(), BlockSource(line, line), in_item=False)
        self._next_line = max(self._next_line, until)

    def _insert_block(self, block: QTextBlockFormat, source: BlockSource, in_item: bool) -> None:
        block.setLeftMargin(self._quotes * QUOTE_MARGIN)
        level = self._lists[-1] if self._lists else None
        if level is not None and not in_item:
            # Further paragraphs of an item line up with its text.
            block.setIndent(level.format.indent())
        if self._fresh:
            self._cursor.setBlockFormat(block)
            self._fresh = False
        else:
            self._cursor.insertBlock(block, QTextCharFormat())
        if level is not None and in_item:
            if level.items is None:
                level.items = self._cursor.createList(level.format)
            else:
                level.items.add(self._cursor.block())
            level.item_starts = False
        self.sources.append(source)

    # Text

    def _inline_children(self, node: SyntaxTreeNode) -> None:
        for child in node.children:
            self._inline(child)

    def _inline(self, node: SyntaxTreeNode) -> None:
        match node.type:
            case "inline":
                self._inline_children(node)
            case "text":
                self._text(node.content)
            case "softbreak" | "hardbreak":
                self._text(LINE_SEPARATOR)
            case "code_inline":
                self._code = True
                self._text(node.content)
                self._code = False
            case "strong":
                self._bold += 1
                self._inline_children(node)
                self._bold -= 1
            case "em":
                self._italic += 1
                self._inline_children(node)
                self._italic -= 1
            case "mark":
                self._highlight += 1
                self._inline_children(node)
                self._highlight -= 1
            case "s":
                self._struck += 1
                self._inline_children(node)
                self._struck -= 1
            case "link":
                href = node.attrs.get("href", "")
                self._links.append(href if isinstance(href, str) else "")
                self._inline_children(node)
                self._links.pop()
            case "image":
                # Never loaded: its description stands in for it.
                self._italic += 1
                self._inline_children(node)
                self._italic -= 1
            case _:
                self._text(node.content)

    def _text(self, text: str) -> None:
        if not text:
            return
        look = self._look
        if look is None or self._code or MARK not in text:
            self._cursor.insertText(text, self._char_format())
            return
        written = 0
        for code in codes(text):
            if code.start > written:
                self._cursor.insertText(text[written : code.start], self._char_format())
            self._cursor.insertText(reminder_text(code, look.morning), self._reminder_format(code))
            written = code.end
        if written < len(text):
            self._cursor.insertText(text[written:], self._char_format())

    def _reminder_format(self, code: Code) -> QTextCharFormat:
        """A reminder written in the note: small; quiet once past or checked; bold
        on the note's highlight while it rings."""
        assert self._look is not None
        char = self._char_format()
        if not self._heading:
            char.setProperty(QTextFormat.Property.FontSizeAdjustment, -1)
        when = code.when(self._look.morning)
        checked = bool(self._lists) and self._lists[-1].checked is True
        if when in self._look.ringing and not checked:
            char.setFontWeight(QFont.Weight.Bold)
            char.setBackground(self._highlight_color)
        elif checked or when <= self._look.now:
            char.setForeground(self._quiet_color)
        return char

    def _char_format(self) -> QTextCharFormat:
        char = QTextCharFormat()
        if self._bold or self._heading:
            char.setFontWeight(QFont.Weight.Bold)
        if self._heading in HEADING_SIZE:
            char.setProperty(QTextFormat.Property.FontSizeAdjustment, HEADING_SIZE[self._heading])
        if self._italic:
            char.setFontItalic(True)
        if self._code:
            char.setFontFamilies([code_family()])
            # A code block has the background on the whole block already.
            if not self._cursor.blockFormat().hasProperty(QTextFormat.Property.BackgroundBrush):
                char.setBackground(self._code_color)
        if self._highlight:
            char.setBackground(self._highlight_color)
        if self._struck:
            char.setFontStrikeOut(True)
        if self._links:
            char.setAnchor(True)
            char.setAnchorHref(self._links[-1])
            char.setFontUnderline(True)
        return char


def render(
    text: str, document: QTextDocument, colors: NoteColors, look: ReminderLook | None = None
) -> list[BlockSource]:
    """Draw a note's Markdown into document; returns where each block came from.
    With look, the reminders written in it show as their day and time."""
    builder = _Builder(document, colors, look)
    if text.strip():
        builder.blocks(parse(text))
    # Nothing shown (such as an empty quote): the one empty block stands for all of it.
    return builder.sources or [BlockSource(0, text.count("\n"))]


class NoteView(QTextEdit):
    """Read-only formatted note. A double-click asks to edit, a drag selects text.

    A single click only gives the note the focus, so clicking a note to read
    or scroll it does not start editing (a note with nothing to read is never
    shown formatted). Clicking a task item's checkbox asks to toggle it; the
    note changes its text, and the view is drawn again from it. Clicking a
    link asks to open it.

    The keyboard reaches the same things: Tab and Shift+Tab go from checkbox
    to link in the order shown, selecting each; Space or Enter then checks the
    box or opens the link, and Esc lets go. Enter with nothing selected, and
    F2 always, ask to edit.
    """

    edit_requested = Signal(int)  # a position in the text, or -1 for its end
    checkbox_clicked = Signal(int)  # the line of the checkbox
    link_clicked = Signal(str)  # its address

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setFrameShape(QTextEdit.Shape.NoFrame)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self._source = ""
        self._blocks: list[BlockSource] = []
        self.stops: list[Stop] = []
        self.stop: int | None = None  # which of stops the keyboard is on
        self._press: QPoint | None = None
        self.colors = note_colors(DEFAULT_COLOR)
        # An arrow, not the text cursor: the note is read here, not typed in.
        # A hand over what a click acts on, links and checkboxes.
        self.viewport().setMouseTracking(True)
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        # Over a code block, a button to copy its code (the keyboard: Tab, then Enter).
        self.copy_button = QToolButton(self.viewport())
        self.copy_button.setAutoRaise(False)
        self.copy_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.copy_button.clicked.connect(self._copy_from_button)
        self.copy_button.hide()
        self._copy_code = ""
        # How its reminders show (stickle.app.reminders); without, as written.
        self.reminder_look: Callable[[], ReminderLook] | None = None
        self.retranslate()

    def retranslate(self) -> None:
        self.copy_button.setText(self.tr("Copy"))
        self.copy_button.setToolTip(self.tr("Copy the code"))
        self.copy_button.setAccessibleName(self.tr("Copy the code"))

    def show_markdown(self, text: str) -> None:
        self._source = text
        # Drawn again when a checkbox is toggled: stay where the reader was.
        scrolled = self.verticalScrollBar().value()
        look = self.reminder_look() if self.reminder_look is not None else None
        self._blocks = render(text, self.document(), self.colors, look)
        self.stops = self._find_stops()
        # The (hidden) cursor is left at the end by the drawing; the view would
        # scroll there the moment it is shown or given the keyboard.
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.setTextCursor(cursor)
        self.verticalScrollBar().setValue(scrolled)
        if self.stop is not None:  # the box just checked stays selected
            self._go_to(self.stop if self.stop < len(self.stops) else None)

    def _find_stops(self) -> list[Stop]:
        stops: list[Stop] = []
        block = self.document().begin()
        while block.isValid():
            source = self.block_source(block)
            end = block.position() + max(block.length() - 1, 0)
            if source is not None and source.checkbox_line is not None:
                stops.append(Stop(block.position(), end, checkbox_line=source.checkbox_line))
            if source is not None and source.code is not None:
                stops.append(Stop(block.position(), end, code=source.code))
            fragments = block.begin()
            while not fragments.atEnd():
                fragment = fragments.fragment()
                href = fragment.charFormat().anchorHref()
                if fragment.isValid() and href:
                    start, end = fragment.position(), fragment.position() + fragment.length()
                    if stops and stops[-1].href == href and stops[-1].end == start:
                        start = stops.pop().start  # one link drawn in several formats
                    stops.append(Stop(start, end, href=href))
                fragments += 1
            block = block.next()
        return stops

    def _go_to(self, stop: int | None) -> None:
        """Select a stop (None: none), as the keyboard's place in the note."""
        self.stop = stop
        cursor = self.textCursor()
        if stop is None:
            cursor.clearSelection()
        else:
            cursor.setPosition(self.stops[stop].start)
            cursor.setPosition(self.stops[stop].end, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def _act(self, stop: Stop) -> None:
        if stop.checkbox_line is not None:
            self.checkbox_clicked.emit(stop.checkbox_line)
        elif stop.code is not None:
            self.copy_code(stop.code, self.cursorRect().bottomRight())
        else:
            self.link_clicked.emit(stop.href)

    def copy_code(self, code: str, near: QPoint) -> None:
        """A code block's code onto the clipboard, said for a moment near it."""
        QGuiApplication.clipboard().setText(code)
        QToolTip.showText(self.viewport().mapToGlobal(near), self.tr("Code copied"), self)

    def code_at(self, point: QPoint) -> tuple[str, QRect] | None:
        """The code block under point (viewport coordinates): its code and where
        it is drawn, in the same coordinates."""
        block = self.cursorForPosition(point).block()
        source = self.block_source(block)
        if source is None or source.code is None:
            return None
        drawn = self.document().documentLayout().blockBoundingRect(block).toRect()
        drawn.translate(-self.horizontalScrollBar().value(), -self.verticalScrollBar().value())
        return (source.code, drawn) if drawn.contains(point) else None

    def _place_copy_button(self, point: QPoint | None) -> None:
        """Over a code block, its copy button at its top right; elsewhere none."""
        found = self.code_at(point) if point is not None else None
        if found is None:
            self.copy_button.hide()
            return
        code, drawn = found
        self._copy_code = code
        size = self.copy_button.sizeHint()
        self.copy_button.move(drawn.right() - size.width() - 2, drawn.top() + 2)
        self.copy_button.show()
        self.copy_button.raise_()

    def _copy_from_button(self) -> None:
        self.copy_code(self._copy_code, self.copy_button.geometry().bottomLeft())

    def set_colors(self, colors: NoteColors) -> None:
        self.colors = colors
        if self._source:
            self.show_markdown(self._source)

    def block_source(self, block: QTextBlock) -> BlockSource | None:
        number = block.blockNumber()
        return self._blocks[number] if 0 <= number < len(self._blocks) else None

    def checkbox_at(self, point: QPoint) -> int | None:
        """The checkbox line if point (in viewport coordinates) is on a task item's box."""
        block = self.cursorForPosition(point).block()
        source = self.block_source(block)
        if source is None or source.checkbox_line is None:
            return None
        layout = block.layout()
        if layout.lineCount() == 0:
            return None
        line = layout.lineAt(0)
        x = point.x() + self.horizontalScrollBar().value()
        y = point.y() + self.verticalScrollBar().value()
        text_left = layout.position().x() + line.x()
        top = layout.position().y() + line.y()
        # The box is drawn in the indent to the left of the item's text.
        on_box = text_left - self.document().indentWidth() <= x < text_left
        return source.checkbox_line if on_box and top <= y < top + line.height() else None

    def source_position_at(self, point: QPoint) -> int:
        """The position in the text (as Qt counts) that point shows."""
        cursor = self.cursorForPosition(point)
        block = cursor.block()
        source = self.block_source(block)
        if source is None:
            return -1
        shown = block.text()
        split = from_utf16(shown, cursor.position() - block.position())
        index = source_position(
            self._source, source.first_line, source.last_line, shown[:split], shown[split:]
        )
        return utf16_length(self._source[:index])

    @override
    def paintEvent(self, e: QPaintEvent) -> None:
        # Drawn as QTextEdit draws it, but with no caret: one made the formatted note
        # look as if it were being edited. (A caret width of 0 is not enough: at
        # display scales such as 150% Qt still draws a one-pixel line.)
        dx = self.horizontalScrollBar().value()
        dy = self.verticalScrollBar().value()
        clip = QRectF(e.rect().translated(dx, dy))
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette = self.palette()
        context.cursorPosition = -1
        context.clip = clip
        cursor = self.textCursor()
        if cursor.hasSelection():
            group = QPalette.ColorGroup.Active if self.hasFocus() else QPalette.ColorGroup.Inactive
            selection = QAbstractTextDocumentLayout.Selection()
            selection.cursor = cursor
            selection.format.setBackground(
                self.palette().brush(group, QPalette.ColorRole.Highlight)
            )
            selection.format.setForeground(
                self.palette().brush(group, QPalette.ColorRole.HighlightedText)
            )
            context.selections = [selection]
        painter = QPainter(self.viewport())
        painter.translate(-dx, -dy)
        painter.setClipRect(clip)
        self.document().documentLayout().draw(painter, context)

    @override
    def mousePressEvent(self, e: QMouseEvent) -> None:
        self._press = e.position().toPoint() if e.button() == Qt.MouseButton.LeftButton else None
        self.stop = None  # the mouse takes over from the keyboard
        super().mousePressEvent(e)

    @override
    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        super().mouseMoveEvent(e)
        point = e.position().toPoint()
        clickable = bool(self.anchorAt(point)) or self.checkbox_at(point) is not None
        shape = Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.ArrowCursor
        self.viewport().setCursor(shape)
        self._place_copy_button(e.position().toPoint())

    @override
    def leaveEvent(self, event: QEvent) -> None:
        if not self.copy_button.underMouse():
            self.copy_button.hide()
        super().leaveEvent(event)

    @override
    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        super().mouseReleaseEvent(e)
        press, self._press = self._press, None
        point = e.position().toPoint()
        if (
            press is None
            or e.button() != Qt.MouseButton.LeftButton
            or (point - press).manhattanLength() >= QApplication.startDragDistance()
            or self.textCursor().hasSelection()
        ):
            return
        line = self.checkbox_at(point)
        if line is not None:
            self.checkbox_clicked.emit(line)
        elif href := self.anchorAt(point):
            self.link_clicked.emit(href)

    @override
    def mouseDoubleClickEvent(self, e: QMouseEvent) -> None:
        # Not passed on: a double-click here edits rather than selects a word.
        point = e.position().toPoint()
        if e.button() == Qt.MouseButton.LeftButton and self.checkbox_at(point) is None:
            self.edit_requested.emit(self.source_position_at(point))

    @override
    def event(self, e: QEvent) -> bool:
        # Tab moves between the note's checkboxes and links, not out of the note
        # (a note has nothing else to go to); Ctrl+Tab is left to the window.
        if (
            isinstance(e, QKeyEvent)
            and e.type() == QEvent.Type.KeyPress
            and e.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)
            and not e.modifiers() & Qt.KeyboardModifier.ControlModifier
            and self.stops
        ):
            back = e.key() == Qt.Key.Key_Backtab
            if self.stop is None:
                self._go_to(len(self.stops) - 1 if back else 0)
            else:
                self._go_to((self.stop + (-1 if back else 1)) % len(self.stops))
            return True
        return super().event(e)

    @override
    def keyPressEvent(self, e: QKeyEvent) -> None:
        key = e.key()
        if self.stop is not None and key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._act(self.stops[self.stop])
            return
        if self.stop is not None and key == Qt.Key.Key_Escape:
            self._go_to(None)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_F2):
            self.edit_requested.emit(-1)
            return
        super().keyPressEvent(e)
