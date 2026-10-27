"""The keys at a glance: a panel over the screen listing Stickle's shortcuts.

F1 or Ctrl+/ in a note or the Stickle window opens it, and so do the menus.
It closes with Esc, the same key again, or a click elsewhere, and the keyboard
goes back to where it was. The shortcuts from anywhere show the keys they have
now: changed by the user, given by the desktop, or none. They are changed in
the settings, which a link leads to; here they are only shown.

What it lists is written here once (GUIDE); tests check that the keys it
names within the app are the keys the app answers to.
"""

from typing import override

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QCursor,
    QGuiApplication,
    QKeyEvent,
    QPainter,
    QPaintEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stickle.app.link_label import LinkLabel
from stickle.app.shortcuts import GlobalShortcuts, State
from stickle.data.settings import SHORTCUT_ACTIONS
from stickle.platform.linux.x11 import activate

KEYS = ("F1", "Ctrl+/")  # open and close the guide
MARGIN = 18
RADIUS = 12


def _global_names() -> dict[str, str]:
    return {
        "new-note": QCoreApplication.translate("ShortcutGuide", "New note"),
        "show": QCoreApplication.translate("ShortcutGuide", "Open Stickle Manager"),
        "hide-all": QCoreApplication.translate(
            "ShortcutGuide", "Hide all notes for now, or show them again"
        ),
        "next-category": QCoreApplication.translate(
            "ShortcutGuide", "Show only the notes of the next category"
        ),
        "find": QCoreApplication.translate("ShortcutGuide", "Find notes on the desktop"),
    }


# (group, [(what, keys)]) within the app; the keys as Qt writes them.
def guide() -> list[tuple[str, list[tuple[str, str]]]]:
    return [
        (
            QCoreApplication.translate("ShortcutGuide", "In a note"),
            [
                (QCoreApplication.translate("ShortcutGuide", "New note"), "Ctrl+N"),
                (QCoreApplication.translate("ShortcutGuide", "Hide the note"), "Ctrl+W"),
                (QCoreApplication.translate("ShortcutGuide", "Note menu"), "F10"),
                (
                    QCoreApplication.translate(
                        "ShortcutGuide", "To the next note, or the one before"
                    ),
                    "Ctrl+Tab, Ctrl+Shift+Tab",
                ),
                (
                    QCoreApplication.translate(
                        "ShortcutGuide", "The next colour, or the one before"
                    ),
                    "Ctrl+Shift+], Ctrl+Shift+[",
                ),
                (QCoreApplication.translate("ShortcutGuide", "The note's reminder"), "Ctrl+R"),
                (QCoreApplication.translate("ShortcutGuide", "Edit the text"), "Enter, F2"),
                (QCoreApplication.translate("ShortcutGuide", "Back to the formatted note"), "Esc"),
                (
                    QCoreApplication.translate(
                        "ShortcutGuide", "Choose a checkbox, link or code block"
                    ),
                    "Tab, Shift+Tab",
                ),
                (
                    QCoreApplication.translate(
                        "ShortcutGuide", "Check, open or copy what is chosen"
                    ),
                    "Space, Enter",
                ),
                (QCoreApplication.translate("ShortcutGuide", "These keys"), "F1, Ctrl+/"),
            ],
        ),
        (
            QCoreApplication.translate("ShortcutGuide", "Editing a note's text"),
            [
                (
                    QCoreApplication.translate("ShortcutGuide", "Next list item, or end the list"),
                    "Enter",
                ),
                (
                    QCoreApplication.translate("ShortcutGuide", "Move a list item in or out"),
                    "Tab, Shift+Tab",
                ),
                (QCoreApplication.translate("ShortcutGuide", "A new line only"), "Shift+Enter"),
                (QCoreApplication.translate("ShortcutGuide", "A checkbox"), "[ ]"),
                (QCoreApplication.translate("ShortcutGuide", "A code block"), "``` Enter"),
            ],
        ),
        (
            QCoreApplication.translate("ShortcutGuide", "In Stickle Manager"),
            [
                (QCoreApplication.translate("ShortcutGuide", "Search the notes"), "Ctrl+F"),
                (
                    QCoreApplication.translate(
                        "ShortcutGuide", "Only one category's notes, or all"
                    ),
                    "Ctrl+1 … Ctrl+9, Ctrl+0",
                ),
                (QCoreApplication.translate("ShortcutGuide", "Open the note"), "Enter"),
                (QCoreApplication.translate("ShortcutGuide", "Delete the note"), "Del"),
                (QCoreApplication.translate("ShortcutGuide", "Rename a category or mark"), "F2"),
                (
                    QCoreApplication.translate(
                        "ShortcutGuide", "Move a category or mark up or down"
                    ),
                    "Alt+Up, Alt+Down",
                ),
                (QCoreApplication.translate("ShortcutGuide", "Open the settings"), "Ctrl+,"),
                (QCoreApplication.translate("ShortcutGuide", "Close the window"), "Esc"),
            ],
        ),
    ]


def _open_link(href: str) -> None:
    # Here, not at the top: the note window imports this module's keys.
    from stickle.app import note_window

    note_window.open_link(href)


class ShortcutGuide(QWidget):
    settings_requested = Signal()  # the link to where the shortcuts from anywhere are changed

    def __init__(self, shortcuts: GlobalShortcuts | None) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._shortcuts = shortcuts
        self._came_from: QWidget | None = None
        self.title = QLabel(self)
        font = self.title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.3)
        self.title.setFont(font)
        self.list = QTreeWidget(self)
        self.list.setColumnCount(2)
        self.list.setHeaderHidden(True)
        self.list.setRootIsDecorated(False)
        self.list.setFrameShape(QTreeWidget.Shape.NoFrame)
        self.list.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.list.setStyleSheet("QTreeWidget { background: transparent; }")
        self.list.installEventFilter(self)
        self.hint = QLabel(self)
        self.change = LinkLabel(self)
        self.change.linkActivated.connect(self._to_settings)
        self.change.setVisible(shortcuts is not None)
        # Everything else Stickle does, on the website (opened in the browser).
        self.more = LinkLabel(self)
        self.more.linkActivated.connect(_open_link)
        self._bottom = bottom = QHBoxLayout()
        bottom.addWidget(self.hint, 1)
        bottom.addWidget(self.change)
        bottom.addSpacing(MARGIN)
        bottom.addWidget(self.more)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.addWidget(self.title)
        layout.addWidget(self.list, 1)
        layout.addLayout(bottom)
        self.retranslate()

    def retranslate(self) -> None:
        self.title.setText(self.tr("Keyboard shortcuts"))
        self.setWindowTitle(self.tr("Keyboard shortcuts"))
        self.list.setAccessibleName(self.tr("Keyboard shortcuts"))
        self.hint.setText(self.tr("Esc closes this."))
        # Here, not at the top: stickle.app.support leads back to the note window,
        # which imports this module's keys.
        from stickle.app.support import site_page

        # Translated outside the f-string: lupdate does not read calls inside one.
        every_feature = self.tr("Every feature, explained")
        self.more.setText(f'<a href="{site_page("guide")}">{every_feature}</a>')
        self.more.setAccessibleName(every_feature)
        change = self.tr("Change shortcuts in Settings")
        self.change.setText(f'<a href="settings">{change}</a>')
        self.change.setAccessibleName(change)
        if self.isVisible():
            self.fill()  # otherwise filled as it opens

    def global_keys(self) -> list[tuple[str, str]]:
        """The shortcuts from anywhere, with the keys they have now."""
        shortcuts = self._shortcuts
        if shortcuts is None or not shortcuts.available:
            return []
        names = _global_names()
        rows: list[tuple[str, str]] = []
        for action in SHORTCUT_ACTIONS:
            on = shortcuts.state(action) in (State.ON, State.WAITING)
            keys = shortcuts.combo(action) if on else ""
            rows.append((names[action], keys or self.tr("none", "no keys")))
        return rows

    def fill(self) -> None:
        self.list.clear()
        groups = [(self.tr("From anywhere"), self.global_keys()), *guide()]
        for title, rows in groups:
            if not rows:
                continue
            heading = QTreeWidgetItem(self.list, [title, ""])
            font = heading.font(0)
            font.setBold(True)
            heading.setFont(0, font)
            heading.setFlags(Qt.ItemFlag.ItemIsEnabled)
            # Room above each group but the first.
            if self.list.topLevelItemCount() > 1:
                heading.setSizeHint(0, QSize(0, self.list.fontMetrics().height() * 2))
                bottom = Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft
                heading.setTextAlignment(0, bottom)
            for what, keys in rows:
                row = QTreeWidgetItem(self.list, [what, keys])
                row.setData(0, Qt.ItemDataRole.AccessibleTextRole, f"{what}, {keys}")
        self.list.resizeColumnToContents(0)
        # Room between what a row does and its keys: sized to the longest text
        # alone, the longest rows ran into their keys.
        self.list.setColumnWidth(0, self.list.columnWidth(0) + self.key_gap())
        self.list.resizeColumnToContents(1)

    def key_gap(self) -> int:
        return 2 * self.list.fontMetrics().horizontalAdvance("M")

    def rows(self) -> list[tuple[str, str]]:
        """What it lists, as (what, keys); headings have no keys."""
        items = (self.list.topLevelItem(i) for i in range(self.list.topLevelItemCount()))
        return [(item.text(0), item.text(1)) for item in items if item is not None]

    def _to_settings(self) -> None:
        """Closed for the settings, which take the keyboard instead."""
        self._came_from = None
        self.close()
        self.settings_requested.emit()

    def toggle(self) -> None:
        if self.isVisible():
            self.close()
        else:
            self.open()

    def open(self) -> None:
        """Over the middle of the screen the pointer is on, with the keyboard."""
        self._came_from = QApplication.focusWidget()
        self.fill()  # keys may have changed since
        width = self.list.columnWidth(0) + self.list.columnWidth(1) + 2 * MARGIN + 40
        content = sum(
            self.list.visualItemRect(item).height()
            for i in range(self.list.topLevelItemCount())
            if (item := self.list.topLevelItem(i)) is not None
        )
        chrome = self.title.sizeHint().height() + self.hint.sizeHint().height() + 2 * MARGIN
        # As wide as the line of links below the list, too.
        width = max(width, self._bottom.sizeHint().width() + 2 * MARGIN, 420)
        self.resize(width, min(content + chrome + 24, 760))
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.move(area.center() - QPoint(self.width() // 2, self.height() // 2))
        self.show()
        self.raise_()
        self.activateWindow()
        self.list.setFocus()
        first = self.list.topLevelItem(1)
        if first is not None:
            self.list.setCurrentItem(first)

    @override
    def closeEvent(self, event: QCloseEvent) -> None:
        came_from, self._came_from = self._came_from, None
        super().closeEvent(event)
        if came_from is not None and came_from.isVisible():
            window = came_from.window()
            window.activateWindow()
            if QGuiApplication.platformName() == "xcb":
                # As a taskbar asks: the window manager refuses the app's own request.
                activate(int(window.winId()))
            came_from.setFocus()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        elif event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            self.close()  # a click elsewhere
        super().changeEvent(event)

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
            closing = event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_F1) or (
                event.key() == Qt.Key.Key_Slash
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            )
            if closing:
                self.close()
                return True
        return super().eventFilter(watched, event)

    @override
    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        background = self.palette().window().color()
        panel = QColor(background)
        panel.setAlpha(240)
        painter.setPen(QColor(0, 0, 0, 60))
        painter.setBrush(panel)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)
        painter.end()
