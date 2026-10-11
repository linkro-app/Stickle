"""A single sticky note window."""

import logging
import math
import sys
import time
from collections.abc import Callable
from typing import override

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QEventLoop,
    QLocale,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QColor,
    QContextMenuEvent,
    QDesktopServices,
    QFocusEvent,
    QGuiApplication,
    QIcon,
    QInputMethodEvent,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QMoveEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QScreen,
    QShowEvent,
    QTextCursor,
    QWindow,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QSizeGrip,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import Shiboken

from stickle.app.labels import CategoryTag, MarksBadge, mark_name
from stickle.app.note_highlight import MarkdownHighlighter
from stickle.app.note_view import NoteView, from_utf16, utf16_length
from stickle.app.palette import color_name, qcolor, swatch_icon
from stickle.app.shortcut_guide import KEYS as GUIDE_KEYS
from stickle.app.window_flags import keep_stays_on_top, set_stays_on_top, stays_on_top
from stickle.core.colors import DARK_TEXT, DEFAULT_COLOR, PALETTE, note_colors
from stickle.core.editing import on_close_bracket, on_enter, on_tab
from stickle.core.labels import Category, Mark
from stickle.core.markdown import note_title, task_box
from stickle.platform.linux.x11 import activate, keep_off_taskbar

log = logging.getLogger(__name__)

CORNER_RADIUS = 6
TITLE_BAR_HEIGHT = 22  # also the height of a folded note
BUTTON_SIZE = 20
ICON_SIZE = 12
STROKE = 1.1  # line width of drawn icons, in logical pixels
# Screen scales drawn for, so icons stay crisp at 125 %, 150 %, 175 % too.
ICON_SCALES = (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)
MAX_HEIGHT = 16_777_215  # Qt's QWIDGETSIZE_MAX: no limit
DEFAULT_SIZE = (260, 240)
# How long an input method has to commit a character after the focus left.
DROP_CHECK_MS = 150
# Where an input method may drop the character being composed when the focus
# leaves (ibus on Linux). Elsewhere they commit it, sometimes a moment later: a
# character put in for them there came out twice.
DROPPING_INPUT_METHODS = sys.platform.startswith("linux")
# A composition that ended this shortly before the focus left was dropped by the
# input method, not erased by the user (too quick for Backspace and a click).
JUST_DROPPED_S = 0.3
# Moving and resizing report a stream of positions: the place is kept once they stop.
SETTLE_MS = 500
DRAG_WATCH_MS = 30  # how often the button and Alt are looked at during a drag
AFTER_DROP_MS = 300  # and for how long Alt still counts once the button is up
# Brought forward on X11: raised again this long after, once the keyboard is there.
RAISE_AGAIN_MS = 150
# Moving and resizing with the keyboard: how far each arrow press goes (Shift: 1 pixel).
MOVE, RESIZE = "move", "resize"
KEYBOARD_STEP = 10
FOUND_BORDER = 3  # the border of a note chosen among those found
ARROWS: dict[int, tuple[int, int]] = {
    Qt.Key.Key_Left.value: (-1, 0),
    Qt.Key.Key_Right.value: (1, 0),
    Qt.Key.Key_Up.value: (0, -1),
    Qt.Key.Key_Down.value: (0, 1),
}
# How see-through a note may be while another window is in use (1.0: not at all).
OPACITIES = (1.0, 0.9, 0.8, 0.7, 0.6)


# Links a note may open: web pages and mail. A note from someone else must
# not be able to open a program or file on this computer.
OPENABLE_SCHEMES = ("http", "https", "mailto")


def _open_url(url: QUrl) -> bool:
    return QDesktopServices.openUrl(url)


open_url: Callable[[QUrl], bool] = _open_url  # replaced in tests


def open_link(href: str) -> bool:
    """Open a link from a note with the system's browser or mail app, if it is one of
    OPENABLE_SCHEMES; False (and nothing opened) otherwise."""
    url = QUrl(href)
    if not url.isValid() or url.scheme().lower() not in OPENABLE_SCHEMES:
        log.info("a link of another kind was not opened")
        return False
    return open_url(url)


def _main_button_down() -> bool:
    """Whether the mouse button a drag is made with is still held. Asked of the
    system: Qt's own idea of it is stale while the window system moves a window."""
    if sys.platform == "win32":
        from stickle.platform.windows.pointer import main_button_down

        return main_button_down()
    if QGuiApplication.platformName() == "xcb":
        from stickle.platform.linux.x11 import main_button_down

        held = main_button_down()
        if held is not None:
            return held
    return bool(QGuiApplication.mouseButtons() & Qt.MouseButton.LeftButton)


def _alt_held() -> bool:
    return bool(QGuiApplication.queryKeyboardModifiers() & Qt.KeyboardModifier.AltModifier)


def drawn_icon(
    draw: Callable[[QPainter, float], None], color: QColor, quiet: QColor | None = None
) -> QIcon:
    """An icon drawn in color, at every usual screen scale.

    With quiet, the icon is drawn in that colour at rest and in color when
    the pointer is on it. Drawn rather than symbol characters: finding a
    font with such a glyph made showing the first note a third of a second
    slower. Drawn per scale rather than scaled, so lines stay sharp.
    """
    icon = QIcon()
    modes = [(QIcon.Mode.Normal, quiet or color), (QIcon.Mode.Active, color)]
    for mode, ink in modes:
        for scale in ICON_SCALES:
            pixels = round(ICON_SIZE * scale)
            pixmap = QPixmap(pixels, pixels)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(ink, STROKE * scale)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            draw(painter, pixels)
            painter.end()
            pixmap.setDevicePixelRatio(scale)
            icon.addPixmap(pixmap, mode)
    return icon


def _cross(painter: QPainter, size: float) -> None:
    inset = size * 0.2
    painter.drawLine(QPointF(inset, inset), QPointF(size - inset, size - inset))
    painter.drawLine(QPointF(size - inset, inset), QPointF(inset, size - inset))


def _dots(painter: QPainter, size: float) -> None:
    """Three dots in a row: the menu."""
    painter.setBrush(painter.pen().color())
    radius = size * 0.085
    for x in (0.18, 0.5, 0.82):
        painter.drawEllipse(QPointF(size * x, size * 0.5), radius, radius)


def _bin(painter: QPainter, size: float) -> None:
    painter.drawLine(QPointF(size * 0.1, size * 0.22), QPointF(size * 0.9, size * 0.22))
    painter.drawLine(QPointF(size * 0.38, size * 0.08), QPointF(size * 0.62, size * 0.08))
    painter.drawRoundedRect(QRectF(size * 0.2, size * 0.22, size * 0.6, size * 0.7), 1, 1)


def _warning(painter: QPainter, size: float) -> None:
    painter.drawEllipse(QRectF(size * 0.08, size * 0.08, size * 0.84, size * 0.84))
    painter.drawLine(QPointF(size * 0.5, size * 0.28), QPointF(size * 0.5, size * 0.55))
    painter.drawPoint(QPointF(size * 0.5, size * 0.72))


def _pin(painter: QPainter, size: float, pinned: bool) -> None:
    """A push pin: a head, a collar and a point. Pushed in (upright, head
    filled) while the note stays on top; lying on its side and hollow when not,
    so the state shows in its shape, not only its colour."""
    if not pinned:
        painter.translate(size / 2, size / 2)
        painter.rotate(45)
        painter.translate(-size / 2, -size / 2)
    if pinned:
        painter.setBrush(painter.pen().color())
    painter.drawRoundedRect(
        QRectF(size * 0.34, size * 0.08, size * 0.32, size * 0.34), size * 0.08, size * 0.08
    )
    painter.drawLine(QPointF(size * 0.24, size * 0.48), QPointF(size * 0.76, size * 0.48))
    painter.drawLine(QPointF(size * 0.5, size * 0.48), QPointF(size * 0.5, size * 0.92))


def _lock(painter: QPainter, size: float) -> None:
    """A padlock: a shackle over a filled body."""
    painter.drawArc(QRectF(size * 0.3, size * 0.1, size * 0.4, size * 0.5), 0, 180 * 16)
    painter.drawLine(QPointF(size * 0.3, size * 0.35), QPointF(size * 0.3, size * 0.48))
    painter.drawLine(QPointF(size * 0.7, size * 0.35), QPointF(size * 0.7, size * 0.48))
    painter.setBrush(painter.pen().color())
    painter.drawRoundedRect(
        QRectF(size * 0.18, size * 0.48, size * 0.64, size * 0.44), size * 0.06, size * 0.06
    )


def make_lock_icon(color: QColor, quiet: QColor | None = None) -> QIcon:
    return drawn_icon(_lock, color, quiet)


def make_pin_icon(color: QColor, pinned: bool, quiet: QColor | None = None) -> QIcon:
    return drawn_icon(lambda painter, size: _pin(painter, size, pinned), color, quiet)


def make_close_icon(color: QColor, quiet: QColor | None = None) -> QIcon:
    return drawn_icon(_cross, color, quiet)


def make_menu_icon(color: QColor, quiet: QColor | None = None) -> QIcon:
    return drawn_icon(_dots, color, quiet)


def make_delete_icon(color: QColor) -> QIcon:
    return drawn_icon(_bin, color)


def make_unsaved_icon(color: QColor) -> QIcon:
    return drawn_icon(_warning, color)


class TitleBar(QWidget):
    """Drag handle with the menu and hide buttons. Right-clicking it opens the menu,
    double-clicking it folds or unfolds the note, and a folded note shows its title."""

    menu_requested = Signal(QPoint)  # where, on the screen
    double_clicked = Signal()
    drag_started = Signal(bool)  # True: with Shift, the notes beside it along
    dragged_by = Signal(QPoint)  # moved by Stickle itself, this far from where it started

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedHeight(TITLE_BAR_HEIGHT)
        self._drag_offset: QPoint | None = None
        self._drag_from = QPoint()  # where the window was as Stickle began moving it
        self._press: QPoint | None = None  # a press that may yet become a drag

        self.title = QLabel(self)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.title.setMinimumWidth(0)
        self.title.hide()
        self._full_title = ""

        # Always on top: a pin that shows at a glance whether the note stays on top.
        self.pin_button = self._button()
        self.pin_button.setCheckable(True)
        self.pin_button.setChecked(True)
        self._icon_color = qcolor(DARK_TEXT)
        self._quiet_color = qcolor(DARK_TEXT)
        self.pin_button.toggled.connect(self._show_pin)
        self.menu_button = self._button()
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.close_button = self._button()
        # Shown only while the note could not be saved; clicking tries again at once.
        self.unsaved_button = self._button()
        self.unsaved_button.hide()
        # Shown while the note is locked; clicking opens the menu, where it is unlocked.
        self.lock_button = self._button()
        self.lock_button.hide()
        self.lock_button.clicked.connect(
            lambda: self.menu_requested.emit(
                self.lock_button.mapToGlobal(QPoint(0, self.lock_button.height()))
            )
        )
        self.movable = True  # False while the note is locked
        # What the note is about, and how it is to be dealt with; clicking either
        # opens the menu, where they are changed.
        self.category_tag = CategoryTag(self)
        self.marks_badge = MarksBadge(self)
        for part in (self.category_tag, self.marks_badge):
            part.clicked.connect(
                lambda _=False, part=part: self.menu_requested.emit(
                    part.mapToGlobal(QPoint(0, part.height()))
                )
            )
        self.set_icon_color(qcolor(DARK_TEXT))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 1, 1, 1)
        layout.setSpacing(1)
        layout.addWidget(self.unsaved_button)
        layout.addWidget(self.lock_button)
        layout.addWidget(self.category_tag)
        layout.addWidget(self.title, 1)
        layout.addStretch()
        layout.addWidget(self.marks_badge)
        # Kept away from the hide button, so it is not hit by mistake.
        layout.addWidget(self.pin_button)
        layout.addWidget(self.menu_button)
        layout.addWidget(self.close_button)

    def _button(self) -> QToolButton:
        """A small flat button; its look comes from the note's style sheet."""
        button = QToolButton(self)
        button.setAutoRaise(True)
        button.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        button.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        return button

    def set_icon_color(self, color: QColor, quiet: QColor | None = None) -> None:
        """Icons in color when pointed at, and quiet (quieter) otherwise."""
        self._icon_color = color
        self._quiet_color = quiet or color
        self.menu_button.setIcon(make_menu_icon(color, self._quiet_color))
        self.close_button.setIcon(make_close_icon(color, self._quiet_color))
        # A warning should not be quiet.
        self.unsaved_button.setIcon(make_unsaved_icon(color))
        self.lock_button.setIcon(make_lock_icon(color, self._quiet_color))
        self._show_pin(self.pin_button.isChecked())
        self.category_tag.set_color(color)
        self.marks_badge.set_color(color)

    def _show_pin(self, pinned: bool) -> None:
        self.pin_button.setIcon(make_pin_icon(self._icon_color, pinned, self._quiet_color))

    def set_title(self, title: str) -> None:
        """Shown while the note is folded, shortened to the width it has."""
        self._full_title = title
        self._elide()

    def _elide(self) -> None:
        metrics = self.title.fontMetrics()
        self.title.setText(
            metrics.elidedText(self._full_title, Qt.TextElideMode.ElideRight, self.title.width())
        )

    @override
    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()

    @override
    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        self.menu_requested.emit(event.globalPos())
        event.accept()

    @override
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        # The window starts moving only once the mouse moves: a click or a
        # double-click (to fold the note) must reach the note first.
        self._press = event.globalPosition().toPoint()
        event.accept()

    @override
    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        point = event.globalPosition().toPoint()
        if not self.movable:
            self._press = None  # locked: stays where it is
        if self._press is not None and self._drag_offset is None:
            if (point - self._press).manhattanLength() < QApplication.startDragDistance():
                return
            start = self._press
            self._press = None
            # Shift: the notes beside it come along, so Stickle moves them all
            # itself (not possible under Wayland, where only the system moves windows).
            group = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier) and (
                QGuiApplication.platformName() != "wayland"
            )
            self.drag_started.emit(group)
            # Otherwise the window system moves it: the only way that also works on Wayland.
            if group or not self.window().windowHandle().startSystemMove():
                self._drag_from = self.window().pos()
                self._drag_offset = start - self._drag_from
        if self._drag_offset is None:
            super().mouseMoveEvent(event)
            return
        # From where it started rather than from pos(): under X11 pos() changes
        # only once the window system has moved the window.
        target = point - self._drag_offset
        self.window().move(target)
        self.dragged_by.emit(target - self._drag_from)
        event.accept()

    @override
    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._press = None
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    @override
    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = None
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class NoteWindow(QWidget):
    """Frameless, always-on-top note with rounded corners that stays off the taskbar.

    Its colours all follow from one palette key (see stickle.core.colors); the
    background is opaque so the text always contrasts as intended.

    The window only asks: hiding and deleting are decided (and stored) by its
    owner, which then closes it with release(). Any other close, such as Alt+F4,
    asks to hide too, so a note never leaves the screen without being stored.
    """

    closed = Signal()
    new_note_requested = Signal()
    hide_requested = Signal()
    delete_requested = Signal()
    editing_finished = Signal()  # the text lost focus: a moment to save
    text_changed = Signal()
    retry_requested = Signal()
    color_requested = Signal(str)  # a palette key
    collapse_requested = Signal(bool)  # True: fold to the title bar, False: unfold
    on_top_requested = Signal(bool)  # True: stay above other windows
    opacity_requested = Signal(float)  # one of OPACITIES
    lock_requested = Signal(bool)  # True: keep it where it is, as it is
    category_requested = Signal(object)  # a category id, or None for none
    new_category_requested = Signal()
    mark_requested = Signal(str, bool)  # a mark id; True: put it on
    guide_requested = Signal()  # the keys at a glance
    drag_started = Signal(bool)  # by its title bar; True: with Shift, the notes beside it along
    dragged_by = Signal(QPoint)  # moved by Stickle itself (with Shift), this far from the start
    switch_requested = Signal(int)  # 1: to the next note on screen, -1: the one before
    geometry_settled = Signal()  # moved or resized, and then left alone for a moment

    def __init__(
        self,
        note_id: str | None = None,
        text: str = "",
        color: str = DEFAULT_COLOR,
        always_on_top: bool = True,
        opacity: float = 1.0,
        locked: bool = False,
    ) -> None:
        # A tool window stays out of the taskbar and window switcher. Under X11 an
        # ordinary window is asked to do the same instead (see showEvent): window
        # managers lift all of an app's tool windows to the layer of the highest,
        # so a note kept on top would keep every note on top.
        self._x11 = QGuiApplication.platformName() == "xcb"
        kind = Qt.WindowType.Window if self._x11 else Qt.WindowType.Tool
        super().__init__(
            None, kind | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        # Only so the rounded corners are see-through; the note itself is opaque.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # macOS hides tool windows while the app is inactive unless told otherwise.
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.resize(*DEFAULT_SIZE)

        self.note_id = note_id  # None until the note is first stored
        self._released = False
        # An input method is composing a character (Hangul syllables, for example).
        # The editor's text holds only what is committed, so saving never catches
        # half a character.
        self.composing = False
        self._preedit = ""
        self._commit_seen = False
        self._commits = 0  # committed texts seen, to tell a drop from a commit
        # A composition that just ended with nothing committed: (character, when,
        # commits then). ibus drops it just before the focus leaves for another app.
        self._ended_unfinished: tuple[str, float, int] | None = None
        # Still being edited while the shortcuts it asked for are shown over it.
        self._keep_editing = False
        self.found = False  # chosen among the notes found on the desktop

        # Where the app last put the note; anything else is the user's doing.
        self._placed: QRect | None = None
        self._scale_watched: QWindow | None = None
        self._screen_watched: QScreen | None = None
        # False while shown in its spare place because its own monitor is missing.
        self.own_monitor = True
        self.collapsed = False
        self._expanded_height = 0  # the height to unfold to, while folded
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self._settled)

        self.color = color
        self.colors = note_colors(color)

        self.title_bar = TitleBar(self)
        self.title_bar.close_button.clicked.connect(self.hide_requested)
        self.title_bar.menu_requested.connect(self.open_menu_at)
        # Dragged by its title bar: its owner may line it up where it is dropped.
        self.dragging = False
        self.alt_at_drop = False  # Alt held as it was let go: left where it is
        self._after_drop = 0  # looks for Alt since the button went up
        self._drag_watch = QTimer(self)
        self._drag_watch.setInterval(DRAG_WATCH_MS)
        self._drag_watch.timeout.connect(self._watch_drag)
        self.title_bar.drag_started.connect(self._drag_started)
        self.title_bar.dragged_by.connect(self.dragged_by)
        self.title_bar.double_clicked.connect(
            lambda: self.collapse_requested.emit(not self.collapsed)
        )
        self.menu = QMenu(self)
        self.color_menu = self.menu.addMenu("")
        self.color_actions: dict[str, QAction] = {}
        colors = QActionGroup(self)
        for key in PALETTE:
            action = self.color_menu.addAction(swatch_icon(key), "")
            action.setCheckable(True)
            action.triggered.connect(lambda _=False, key=key: self.color_requested.emit(key))
            colors.addAction(action)
            self.color_actions[key] = action
        # Filled each time the menu opens, from what exists then (see label_choices).
        self.category: Category | None = None
        self.marks: list[Mark] = []
        self.label_choices: Callable[[], tuple[list[Category], list[Mark]]] = lambda: ([], [])
        self.category_menu = self.menu.addMenu("")
        self.marks_menu = self.menu.addMenu("")
        self.menu.aboutToShow.connect(self._fill_label_menus)
        # See-through only while another window is in use: read and written, the
        # note is opaque, so its text keeps its contrast.
        self.opacity = 1.0
        self.opacity_menu = self.menu.addMenu("")
        self.opacity_actions: dict[float, QAction] = {}
        opacities = QActionGroup(self)
        for level in OPACITIES:
            action = self.opacity_menu.addAction("")
            action.setCheckable(True)
            action.triggered.connect(
                lambda _=False, level=level: self.opacity_requested.emit(level)
            )
            opacities.addAction(action)
            self.opacity_actions[level] = action
        self.collapse_action = self.menu.addAction("")
        self.collapse_action.triggered.connect(
            lambda: self.collapse_requested.emit(not self.collapsed)
        )
        # The same as the pin, for the keyboard and screen readers.
        self.on_top_action = self.menu.addAction("")
        self.on_top_action.setCheckable(True)
        self.on_top_action.setChecked(True)
        self.on_top_action.triggered.connect(self.on_top_requested)
        self.title_bar.pin_button.clicked.connect(self.on_top_requested)
        # Kept where it is, as it is: no moving, resizing or editing (hiding and
        # deleting still work: the trash keeps a deleted note).
        self.locked = False
        self.lock_action = self.menu.addAction("")
        self.lock_action.setCheckable(True)
        self.lock_action.triggered.connect(self.lock_requested)
        # Moving and resizing without a mouse: the arrow keys, then Enter (or Esc
        # to put it back). A frameless note has no window menu of the system's.
        self.move_action = self.menu.addAction("")
        self.move_action.triggered.connect(lambda: self.start_keyboard(MOVE))
        self.resize_action = self.menu.addAction("")
        self.resize_action.triggered.connect(lambda: self.start_keyboard(RESIZE))
        self._keyboard: str | None = None  # MOVE or RESIZE while the arrow keys do that
        self._keyboard_from = QRect()
        # The keys at a glance (F1, Ctrl+/), shown by the app (stickle.app.shortcut_guide).
        self.guide_action = self.menu.addAction("")
        self.guide_action.setShortcuts([QKeySequence(keys) for keys in GUIDE_KEYS])
        self.guide_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.guide_action.triggered.connect(self._ask_for_guide)
        self.addAction(self.guide_action)
        self.menu.addSeparator()
        # The menu has the system's colours, which are the note's only by chance.
        self.delete_action = self.menu.addAction(make_delete_icon(qcolor(DARK_TEXT)), "")
        self.delete_action.triggered.connect(self.delete_requested)
        self.title_bar.menu_button.setMenu(self.menu)

        self.editor = QPlainTextEdit(self)
        self.editor.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.editor.setPlainText(text)
        self.highlighter = MarkdownHighlighter(self.editor.document(), self.colors)
        self.editor.installEventFilter(self)
        # The editor also reports a change when only the colouring changed.
        self._last_text = self.text
        self.editor.textChanged.connect(self._text_changed)
        self.title_bar.unsaved_button.clicked.connect(self.retry_requested)
        # The formatted note, drawn from the editor's text; a click edits it.
        self.view = NoteView(self)
        self.view.edit_requested.connect(self._edit_asked)
        self.view.checkbox_clicked.connect(self._checkbox_clicked)
        self.view.link_clicked.connect(open_link)  # locked or not: the text stays as it is
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.editor)
        self.stack.setCurrentWidget(self.editor)

        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch()
        self.size_grip = QSizeGrip(self)
        grip_row.addWidget(self.size_grip)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.title_bar)
        layout.addWidget(self.stack)
        layout.addLayout(grip_row)

        self.new_note_action = self._add_action(QKeySequence.StandardKey.New)
        self.new_note_action.triggered.connect(self.new_note_requested)
        self.close_action = self._add_action(QKeySequence.StandardKey.Close)
        self.close_action.triggered.connect(self.hide_requested)
        # Tab types a tab in the text, so the menu has a key of its own.
        self.menu_action = QAction(self)
        self.menu_action.setShortcut(QKeySequence(Qt.Key.Key_F10))
        self.menu_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.menu_action.triggered.connect(self.open_menu)
        self.addAction(self.menu_action)
        # To the next note on screen, or the one before: notes are not in Alt+Tab.
        for keys, step in ((Qt.Key.Key_Tab, 1), (Qt.Key.Key_Backtab, -1)):
            action = QAction(self)
            action.setShortcut(QKeySequence(Qt.KeyboardModifier.ControlModifier | keys))
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            action.triggered.connect(lambda _=False, step=step: self.switch_requested.emit(step))
            self.addAction(action)

        self.set_color(color)
        self.set_always_on_top(always_on_top)
        self.set_opacity(opacity)
        self.retranslate()
        if text.strip():
            self.show_formatted()
        else:
            self.edit()
        self.set_locked(locked)

    def retranslate(self) -> None:
        """Apply every visible text; runs again when the UI language changes."""
        self.setWindowTitle(self.tr("Note"))
        self.setAccessibleName(self.tr("Note"))
        self.editor.setAccessibleName(self.tr("Note text"))
        self.view.setAccessibleName(self.tr("Note text"))
        self.view.setAccessibleDescription(
            self.tr(
                "Tab moves between checkboxes, links and code blocks; Space or Enter checks,"
                " opens or copies one. F2, or Enter with none chosen, edits the note."
            )
        )
        self.view.retranslate()
        hide_note = self.tr("Hide note")
        self.title_bar.close_button.setAccessibleName(hide_note)
        self.title_bar.close_button.setToolTip(hide_note)
        self.close_action.setText(hide_note)
        note_menu = self.tr("Note menu")
        self.title_bar.menu_button.setAccessibleName(note_menu)
        self.title_bar.menu_button.setToolTip(note_menu)
        on_top = self.tr("Always on top")
        self.on_top_action.setText(on_top)
        self.title_bar.pin_button.setAccessibleName(on_top)
        self.title_bar.pin_button.setToolTip(on_top)
        self.menu_action.setText(note_menu)
        self.color_menu.setTitle(self.tr("Color"))
        self.category_menu.setTitle(self.tr("Category"))
        self.marks_menu.setTitle(self.tr("Marks"))
        self.title_bar.category_tag.retranslate()
        self.title_bar.marks_badge.retranslate()
        self.opacity_menu.setTitle(self.tr("Opacity when not in use"))
        self.lock_action.setText(self.tr("Lock note"))
        self.move_action.setText(self.tr("Move with the arrow keys"))
        self.resize_action.setText(self.tr("Resize with the arrow keys"))
        locked = self.tr("Locked: it cannot be moved or changed. Unlock it in the note menu.")
        self.title_bar.lock_button.setAccessibleName(self.tr("Locked"))
        self.title_bar.lock_button.setToolTip(locked)
        self.title_bar.lock_button.setAccessibleDescription(locked)
        for level, action in self.opacity_actions.items():
            # Some languages put the sign first.
            percent = QLocale().toString(round(level * 100))
            action.setText(self.tr("%1%", "a percentage").replace("%1", percent))
        self.collapse_action.setText(
            self.tr("Expand note") if self.collapsed else self.tr("Collapse note")
        )
        self._update_title()
        for key, action in self.color_actions.items():
            action.setText(color_name(key))
        self.delete_action.setText(self.tr("Delete note"))
        self.guide_action.setText(self.tr("Keyboard shortcuts"))
        self.new_note_action.setText(self.tr("New note"))
        self.title_bar.unsaved_button.setAccessibleName(self.tr("Not saved"))
        self.title_bar.unsaved_button.setToolTip(
            self.tr("This note could not be saved. Trying again; click to try now.")
        )

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        elif event.type() == QEvent.Type.ActivationChange:
            self._show_opacity()
            if self._keyboard is not None and not self.isActiveWindow():
                # Left for another window: kept where it got to.
                self._end_keyboard()
                self.geometry_settled.emit()
        super().changeEvent(event)

    def set_locked(self, locked: bool) -> None:
        """Keep the note where it is, as it is, or let it be moved and edited again.

        Locking while editing leaves the editor (its owner saves first).
        """
        self.locked = locked
        self.lock_action.setChecked(locked)
        self.title_bar.lock_button.setVisible(locked)
        self.title_bar.movable = not locked
        self.size_grip.setVisible(not locked and not self.collapsed)
        self.move_action.setEnabled(not locked)
        self.resize_action.setEnabled(not locked)
        self.editor.setReadOnly(locked)
        if locked:
            self.show_formatted()

    def _edit_asked(self, position: int) -> None:
        if not self.locked:
            self.edit(position)

    def _checkbox_clicked(self, line: int) -> None:
        if not self.locked:  # checking a box changes the text
            self.toggle_checkbox(line)

    def set_opacity(self, opacity: float) -> None:
        """How see-through the note is while another window is in use."""
        self.opacity = min(OPACITIES, key=lambda level: abs(level - opacity))
        self.opacity_actions[self.opacity].setChecked(True)
        self._show_opacity()

    def _show_opacity(self) -> None:
        # Pointing at the note changes nothing: only using it (or another window) does.
        self.setWindowOpacity(1.0 if self.isActiveWindow() else self.opacity)

    def set_labels(self, category: Category | None, marks: list[Mark]) -> None:
        """Show the note's category and marks (marks in their order)."""
        self.category = category
        self.marks = marks
        self.title_bar.category_tag.set_category(category)
        self.title_bar.marks_badge.set_marks(marks)

    def _fill_label_menus(self) -> None:
        categories, marks = self.label_choices()
        self.category_menu.clear()
        chosen = self.category.id if self.category is not None else None
        group = QActionGroup(self.category_menu)
        for category_id, name in [(None, self.tr("None", "no category"))] + [
            (category.id, category.name) for category in categories
        ]:
            action = self.category_menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(category_id == chosen)
            action.triggered.connect(
                lambda _=False, category_id=category_id: self.category_requested.emit(category_id)
            )
            group.addAction(action)
        self.category_menu.addSeparator()
        new = self.category_menu.addAction(self.tr("New category…"))
        new.triggered.connect(self.new_category_requested)

        self.marks_menu.clear()
        on = {mark.id for mark in self.marks}
        for mark in marks:
            action = self.marks_menu.addAction(mark_name(mark))
            action.setCheckable(True)
            action.setChecked(mark.id in on)
            action.triggered.connect(
                lambda checked=False, mark_id=mark.id: self.mark_requested.emit(mark_id, checked)
            )

    def _add_action(self, key: QKeySequence.StandardKey) -> QAction:
        action = QAction(self)
        action.setShortcuts(key)
        action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.addAction(action)
        return action

    def bring_to_front(self) -> None:
        """In front of other windows, with the keyboard (asked for from the list of notes)."""
        self.show()
        self.raise_()
        self.activateWindow()
        if self._x11:
            # Asked for as a taskbar would; the application's own request may be refused.
            activate(int(self.winId()))
            # GNOME under Wayland gave a note asked for by a shortcut the keyboard
            # but left it under the others; raised again once it has the keyboard.
            QTimer.singleShot(RAISE_AGAIN_MS, self, self.raise_)
        self.setFocus()

    def fit_to_text(self, most: int) -> None:
        """Tall enough to show the formatted text whole, but no taller than most.

        The width stays; a note is never made smaller than it is.
        """
        if self.editing:
            return
        document = self.view.document().clone(self)
        document.setTextWidth(self.width())
        frame = TITLE_BAR_HEIGHT + self.size_grip.sizeHint().height()
        needed = math.ceil(document.size().height()) + frame
        document.deleteLater()
        self.resize(self.width(), max(self.height(), min(needed, most)))

    def place(self, geometry: QRect, own_monitor: bool = True) -> None:
        """Put the note somewhere as the app, not the user, decided.

        geometry has the unfolded height; a folded note keeps it for later.
        """
        if self.collapsed:
            self._expanded_height = geometry.height()
            geometry = QRect(geometry.topLeft(), QSize(geometry.width(), TITLE_BAR_HEIGHT))
        self.setGeometry(geometry)
        self._placed = self.geometry()
        self.own_monitor = own_monitor

    def expanded_geometry(self) -> QRect:
        """Where the note is, with the height it has when unfolded."""
        geometry = self.geometry()
        if self.collapsed:
            geometry.setHeight(self._expanded_height)
        return geometry

    @property
    def always_on_top(self) -> bool:
        return stays_on_top(self)

    def set_always_on_top(self, on_top: bool) -> None:
        """Keep the note above other windows, or let them cover it."""
        self.title_bar.pin_button.setChecked(on_top)
        self.on_top_action.setChecked(on_top)
        if on_top == self.always_on_top:
            return
        set_stays_on_top(self, on_top)
        if on_top:
            self.raise_()

    def set_collapsed(self, collapsed: bool) -> None:
        """Fold the note to its title bar, keeping its top edge where it is, or unfold it."""
        if collapsed == self.collapsed:
            return
        top_left = self.pos()
        if collapsed:
            self._expanded_height = self.height()
            self.collapsed = True
            self.stack.hide()
            self.size_grip.hide()
            self.setFixedHeight(TITLE_BAR_HEIGHT)
            # The keyboard stays with the note (on its title bar): Enter unfolds it.
            self.title_bar.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.setFocusProxy(self.title_bar)
            self.title_bar.setFocus()
        else:
            self.collapsed = False
            self.setMinimumHeight(0)
            self.setMaximumHeight(MAX_HEIGHT)
            self.stack.show()
            self.size_grip.setVisible(not self.locked)
            self.resize(self.width(), self._expanded_height)
            self.title_bar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            focus = self.editor if self.editing else self.view
            self.setFocusProxy(focus)
            focus.setFocus()
        self.move(top_left)
        self.title_bar.title.setVisible(collapsed)
        self.retranslate()

    def _update_title(self) -> None:
        if not self.collapsed:
            self.title_bar.set_title("")  # only a folded note shows it
            self.title_bar.setAccessibleName("")
            return
        title = note_title(self.text) or self.tr("Empty note")
        self.title_bar.set_title(title)
        # Folded, the title bar has the keyboard: it is what a screen reader reads.
        self.title_bar.setAccessibleName(self.tr("Folded note: %1").replace("%1", title))
        self.title_bar.setAccessibleDescription(self.tr("Enter unfolds it."))

    @override
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._keyboard is not None:
            self._keyboard_key(event)
            return
        if self.collapsed and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.collapse_requested.emit(False)
            return
        super().keyPressEvent(event)

    # Moving and resizing with the keyboard

    @property
    def keyboard_mode(self) -> str | None:
        """MOVE or RESIZE while the arrow keys move or resize the note."""
        return self._keyboard

    def start_keyboard(self, mode: str) -> None:
        if self.locked:
            return
        self._keyboard = mode
        self._keyboard_from = self.geometry()
        # Every key comes here, not to the text, until Enter or Esc.
        self.grabKeyboard()
        self.title_bar.set_title(
            self.tr("Moving: arrow keys, then Enter (Esc puts it back)")
            if mode == MOVE
            else self.tr("Resizing: arrow keys, then Enter (Esc puts it back)")
        )
        self.title_bar.title.show()

    def _keyboard_key(self, event: QKeyEvent) -> None:
        key = event.key()
        step = 1 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else KEYBOARD_STEP
        dx, dy = ARROWS.get(key, (0, 0))
        if dx or dy:
            if self._keyboard == MOVE:
                self.move(self.pos() + QPoint(dx * step, dy * step))
            else:
                # Qt keeps it no smaller than the note allows (its buttons need the room).
                self.resize(self.size() + QSize(dx * step, dy * step))
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._end_keyboard()
            self.geometry_settled.emit()  # kept at once, as after the mouse lets go
        elif key == Qt.Key.Key_Escape:
            self.setGeometry(self._keyboard_from)
            self._end_keyboard()

    def _end_keyboard(self) -> None:
        self._keyboard = None
        self.releaseKeyboard()
        self.title_bar.title.setVisible(self.collapsed)
        self._update_title()

    @property
    def moved_by_user(self) -> bool:
        """Moved or resized since the app last placed it."""
        return self.geometry() != self._placed

    def mark_placed(self) -> None:
        """Where the note is now has been remembered."""
        self._placed = self.geometry()

    @override
    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not event.spontaneous():
            # Once shown: Qt may have put back an on-top state since changed.
            QTimer.singleShot(0, self, lambda: keep_stays_on_top(self))
            self._watch_scale()
        if self._x11 and not event.spontaneous():
            # Before the window is mapped, and again once it is.
            QGuiApplication.sync()  # the window exists on the X server
            keep_off_taskbar(int(self.winId()), mapped=False)
            QTimer.singleShot(0, self, self._keep_off_taskbar_mapped)

    def _watch_scale(self) -> None:
        """Follow the display scale of the monitor the note is on."""
        window = self.windowHandle()
        if self._scale_watched is not window:
            self._scale_watched = window
            window.screenChanged.connect(self._watch_screen)
            self._watch_screen()

    def _watch_screen(self, _moved_to: QScreen | None = None) -> None:
        screen = self.screen()
        if screen is not self._screen_watched:
            if self._screen_watched is not None:
                self._screen_watched.logicalDotsPerInchChanged.disconnect(self._scale_changed)
            self._screen_watched = screen
            screen.logicalDotsPerInchChanged.connect(self._scale_changed)

    def _scale_changed(self) -> None:
        # Changing the display scale while a note was open left its right end,
        # icons and all, outside what was drawn (a see-through window on a
        # Windows VM). Setting its size again and drawing it whole fixes that.
        QTimer.singleShot(0, self, self._redo_size)

    def _redo_size(self) -> None:
        geometry = self.geometry()
        self.setGeometry(geometry.adjusted(0, 0, 1, 0))
        self.setGeometry(geometry)
        self.update()

    def _keep_off_taskbar_mapped(self) -> None:
        if self.isVisible():
            QGuiApplication.sync()
            # Waiting for the window system lets pending events run: a note hidden
            # at once (and so deleted) may be gone by now.
            if Shiboken.isValid(self) and self.isVisible():
                keep_off_taskbar(int(self.winId()), mapped=True)

    @override
    def moveEvent(self, event: QMoveEvent) -> None:
        super().moveEvent(event)
        if self.isVisible():
            self._settle.start()

    def _settled(self) -> None:
        if self.dragging and (_main_button_down() or self._drag_watch.isActive()):
            self._settle.start()  # held still in the middle of a drag: not dropped yet
            return
        self.geometry_settled.emit()

    def _drag_started(self, group: bool) -> None:
        self.dragging = True
        self.alt_at_drop = False
        self._after_drop = 0
        self._drag_watch.start()
        self.drag_started.emit(group)

    def _watch_drag(self) -> None:
        """Whether Alt is held as the note is let go. While the desktop moves a
        window under Wayland (through XWayland) it keeps the keys to itself, and
        tells of Alt only once the button is up: so a few looks after that too."""
        alt = _alt_held()
        if _main_button_down():
            self.alt_at_drop = alt
            return
        self.alt_at_drop = self.alt_at_drop or alt
        self._after_drop += 1
        if self._after_drop * DRAG_WATCH_MS >= AFTER_DROP_MS:
            self._drag_watch.stop()

    @override
    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self.isVisible():
            self._settle.start()

    def set_color(self, color: str) -> None:
        """Show the note in a palette colour (an unknown key shows the default)."""
        self.color = color
        self.colors = note_colors(color)
        text = qcolor(self.colors.text).name()
        title = self.colors.title_text
        shade = f"{title.red}, {title.green}, {title.blue}"
        # Style sheets rather than a palette: native styles ignore palette text colours.
        # Pinned so a dark system theme does not paint light text on a light note.
        # The buttons are flat: only a faint rounded shade where the pointer is.
        self.setStyleSheet(
            f"QPlainTextEdit, QTextEdit {{ background: transparent; color: {text}; }}"
            f"TitleBar QLabel {{ color: {qcolor(title).name()}; }}"
            "TitleBar QToolButton { border: none; border-radius: 4px; padding: 0;"
            " background: transparent; }"
            f"TitleBar QToolButton:hover {{ background: rgba({shade}, 0.12); }}"
            f"TitleBar QToolButton:pressed {{ background: rgba({shade}, 0.22); }}"
            "TitleBar QToolButton::menu-indicator { image: none; width: 0; }"
        )
        self.title_bar.set_icon_color(qcolor(title), qcolor(self.colors.title_icon))
        self.view.set_colors(self.colors)
        self.highlighter.set_colors(self.colors)
        for key, action in self.color_actions.items():
            action.setChecked(key == color)
        self.update()

    @override
    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shape = QPainterPath()
        outline = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)  # the border on whole pixels
        shape.addRoundedRect(outline, CORNER_RADIUS, CORNER_RADIUS)
        painter.fillPath(shape, qcolor(self.colors.background))
        painter.setClipPath(shape)
        painter.fillRect(0, 0, self.width(), self.title_bar.height(), qcolor(self.colors.title_bar))
        painter.setClipping(False)
        if self.found:
            # Chosen among the notes found on the desktop: a border that stands out.
            painter.setPen(QPen(self.palette().highlight().color(), FOUND_BORDER))
            inner = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
            painter.drawRoundedRect(inner, CORNER_RADIUS, CORNER_RADIUS)
        else:
            painter.setPen(QPen(qcolor(self.colors.border), 1))
            painter.drawPath(shape)

    def set_found(self, found: bool) -> None:
        """Mark the note as the one chosen among those found (see the desktop finder)."""
        self.found = found
        self.update()

    @property
    def text(self) -> str:
        return self.editor.toPlainText()

    @property
    def editing(self) -> bool:
        return self.stack.currentWidget() is self.editor

    def edit(self, position: int = -1) -> None:
        """Show the text to edit, with the cursor at position (-1: the end)."""
        self.stack.setCurrentWidget(self.editor)
        self.setFocusProxy(self.editor)
        cursor = self.editor.textCursor()
        if 0 <= position <= self.editor.document().characterCount() - 1:
            cursor.setPosition(position)
        else:
            cursor.movePosition(QTextCursor.MoveOperation.End)
        self.editor.setTextCursor(cursor)
        self.editor.setFocus()
        self.editor.ensureCursorVisible()

    def show_formatted(self) -> None:
        """Show the note formatted; an empty note stays ready to type in."""
        if not self.editing or not self.text.strip():
            return
        self.view.show_markdown(self.text)
        self.stack.setCurrentWidget(self.view)
        if self.collapsed:
            return  # the folded note keeps the keyboard itself
        self.setFocusProxy(self.view)
        # Given at once if the note is active, or when it next becomes active.
        self.view.setFocus()

    def toggle_checkbox(self, line: int) -> None:
        """Check or uncheck the task item on that line of the text.

        Only the mark inside its brackets changes, through the editor, so it
        can be undone and is saved like any other change.
        """
        block = self.editor.document().findBlockByNumber(line)
        found = task_box(block.text()) if block.isValid() else None
        if found is None:
            return
        column, mark = found
        cursor = QTextCursor(block)
        cursor.setPosition(block.position() + utf16_length(block.text()[:column]))
        cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(mark)

    def _text_changed(self) -> None:
        text = self.text
        if text == self._last_text:
            return
        self._last_text = text
        self.text_changed.emit()
        # The text can change while shown formatted: a checkbox was toggled, or
        # an input method committed a character after the focus left.
        if not self.editing:
            self.view.show_markdown(self.text)
        if self.collapsed:
            self._update_title()

    def _ask_for_guide(self) -> None:
        # The shortcuts take the keyboard for a moment; the text stays open to
        # type on in when they give it back.
        self._keep_editing = self.editing
        self.guide_requested.emit()

    def _leave_editing(self) -> None:
        if self._keep_editing:
            return
        if not self._released and self.editing and not self.editor.hasFocus():
            self.show_formatted()

    def finish_composition(self, closing: bool) -> None:
        """Make sure the character being composed ends up in the text.

        Input methods differ: some commit when asked to, fcitx5 commits when
        reset, and some answer only after the request returns. When the note
        is closing, a character that was still not committed after all that
        is put into the text as the input method would have. While the note
        stays open (logout, sleep) it is only asked to commit, so that nothing
        can be typed twice when the user goes on typing.
        """
        if not self.composing:
            return
        preedit = self._preedit
        self._commit_seen = False
        if self.editor.hasFocus():
            self._request_commit()
            if closing and self.composing:
                self._request_reset()
            # Answers that arrive as posted events; the user's input waits.
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        if closing and not self._commit_seen:
            event = QInputMethodEvent("", [])
            event.setCommitString(preedit)
            QCoreApplication.sendEvent(self.editor, event)

    # Separate so tests can play the part of different input methods.
    def _request_commit(self) -> None:
        QGuiApplication.inputMethod().commit()

    def _request_reset(self) -> None:
        QGuiApplication.inputMethod().reset()

    def set_unsaved(self, unsaved: bool) -> None:
        self.title_bar.unsaved_button.setVisible(unsaved)

    @property
    def unsaved(self) -> bool:
        return self.title_bar.unsaved_button.isVisibleTo(self)

    def open_menu(self) -> None:
        button = self.title_bar.menu_button
        self.open_menu_at(button.mapToGlobal(button.rect().bottomLeft()))

    def open_menu_at(self, position: QPoint) -> None:
        # No item is chosen beforehand, so Enter alone does nothing (Down picks the
        # first). Choosing the Color item would open its submenu at once, and a
        # submenu opened that way misses its first click.
        self.menu.popup(position)

    def allow_close(self) -> None:
        """Let the next close through without asking to hide (the app is quitting)."""
        self._released = True

    def release(self) -> None:
        """Close for real: the owner has stored what it needed."""
        self.allow_close()
        self.close()

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Once released the note has been stored (or deleted): closing moves the focus
        # away, and that must not store it again.
        if (
            watched is self.editor
            and isinstance(event, QFocusEvent)
            and event.type() == QEvent.Type.FocusOut
            and not self._released
        ):
            if not DROPPING_INPUT_METHODS:
                pass  # input methods here commit on focus loss, if late (Windows)
            elif self.composing:
                self._watch_for_drop(self._preedit, self._commits)
            elif (ended := self._ended_unfinished) is not None:
                # Already dropped: leaving for another application, ibus ends the
                # composition before this window hears that the focus is going.
                character, when, commits = ended
                if time.monotonic() - when <= JUST_DROPPED_S:
                    self._watch_for_drop(character, commits)
            self._ended_unfinished = None
            self.editing_finished.emit()
            # A menu opened from the note leaves it being edited.
            if event.reason() != Qt.FocusReason.PopupFocusReason:
                QTimer.singleShot(0, self, self._leave_editing)
        if (
            watched is self.editor
            and isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            # Finished while the editor still has the keyboard, as when the note
            # closes (nothing more is typed there after Esc): left to the focus
            # change, a Windows input method committed it after Stickle had put
            # it in itself, and it was typed twice.
            self.finish_composition(closing=True)
            self.show_formatted()
            return True
        if watched is self.editor and event.type() == QEvent.Type.FocusIn:
            self._keep_editing = False  # back from the shortcuts, if it was there
        if (
            watched is self.editor
            and isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and self._help_with_lists(event)
        ):
            return True
        if watched is self.editor and isinstance(event, QInputMethodEvent):
            previous = self._preedit
            self._preedit = event.preeditString()
            self.composing = bool(self._preedit)
            if event.commitString():
                self._commit_seen = True
                self._commits += 1
                self._ended_unfinished = None
            elif previous and not self._preedit:
                self._ended_unfinished = (previous, time.monotonic(), self._commits)
        return super().eventFilter(watched, event)

    def _help_with_lists(self, event: QKeyEvent) -> bool:
        """Enter, Tab, Shift+Tab and "]" on a list line (see stickle.core.editing);
        whether the key was dealt with. Shift+Enter is an ordinary new line, and a
        character still being composed is left to the input method and the editor."""
        editor = self.editor
        if editor.isReadOnly() or self.composing or editor.textCursor().hasSelection():
            return False
        key, modifiers = event.key(), event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        plain = modifiers == Qt.KeyboardModifier.NoModifier
        cursor = editor.textCursor()
        block = cursor.block()
        line = block.text()
        column = from_utf16(line, cursor.positionInBlock())
        lines = editor.toPlainText().split("\n")
        lines_before = lines[: block.blockNumber()]
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and plain:
            change = on_enter(lines_before, line, column, lines[block.blockNumber() + 1 :])
        elif key == Qt.Key.Key_Tab and plain:
            change = on_tab(lines_before, line, column, back=False)
        elif key == Qt.Key.Key_Backtab:
            change = on_tab(lines_before, line, column, back=True)
        elif event.text() == "]":
            change = on_close_bracket(lines_before, line, column)
        else:
            return False
        if change is None:
            return False
        # One step to undo: the key as it was before the help.
        edit = QTextCursor(block)
        edit.beginEditBlock()
        edit.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        edit.insertText(change.text)
        edit.endEditBlock()
        cursor.setPosition(block.position() + utf16_length(change.text[: change.cursor]))
        editor.setTextCursor(cursor)
        return True

    def _watch_for_drop(self, preedit: str, commits: int) -> None:
        """Keep the character being composed if the input method drops it on focus loss.

        Some input methods (ibus on GNOME) throw away the character being composed
        when the focus moves elsewhere, in every application. Most commit it
        instead. If, shortly after the focus left, the composition has ended
        without anything being committed, the character is put in the text.
        """

        def check() -> None:
            if self._released or self.composing or self._commits != commits:
                return
            event = QInputMethodEvent("", [])
            event.setCommitString(preedit)
            QCoreApplication.sendEvent(self.editor, event)

        QTimer.singleShot(DROP_CHECK_MS, check)

    @override
    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._released:
            event.ignore()
            # After this close has finished: Qt ignores a close started during another.
            QTimer.singleShot(0, self.hide_requested.emit)
            return
        self.closed.emit()
        super().closeEvent(event)
