"""Finding notes on the desktop by their text.

A small box at the top of the screen the pointer is on. As the text in it
changes (once typing pauses, and only what an input method has committed),
the notes on the desktop that do not hold it go out of sight; nothing is
stored. Up and Down go from one note found to the next, bringing it forward;
Enter gives it the keyboard; Esc or a click elsewhere puts every note back.
Notes found among the hidden ones or in the trash are counted, and Ctrl+Enter
opens the Stickle window searching for the same text.
"""

from collections.abc import Callable
from typing import override

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QCursor,
    QGuiApplication,
    QKeyEvent,
    QPainter,
    QPaintEvent,
)
from PySide6.QtWidgets import QLabel, QLineEdit, QVBoxLayout, QWidget

from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.platform.linux.x11 import activate

SEARCH_DELAY_MS = 150  # as the Stickle window's search
WIDTH = 460
TOP = 80  # from the top of the screen
MARGIN = 12
RADIUS = 10


class DesktopFinder(QWidget):
    def __init__(self, notes: NoteManager, search_elsewhere: Callable[[str], object]) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._notes = notes
        self._search_elsewhere = search_elsewhere
        self.found: list[NoteWindow] = []
        self.chosen = 0  # which of found is chosen
        self._ending = False

        self.box = QLineEdit(self)
        self.box.setClearButtonEnabled(True)
        self.box.installEventFilter(self)
        self.count = QLabel(self)
        self.elsewhere = QLabel(self)
        self.elsewhere.setWordWrap(True)
        self._soon = QTimer(self)
        self._soon.setSingleShot(True)
        self._soon.setInterval(SEARCH_DELAY_MS)
        self._soon.timeout.connect(self.search)
        self.box.textChanged.connect(self._soon.start)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.addWidget(self.box)
        layout.addWidget(self.count)
        layout.addWidget(self.elsewhere)
        self.retranslate()

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Find on the desktop"))
        self.box.setPlaceholderText(self.tr("Find notes on the desktop"))
        self.box.setAccessibleName(self.tr("Find notes on the desktop"))
        self.box.setAccessibleDescription(
            self.tr(
                "Up and Down go from one note found to the next, Enter goes to it, "
                "Esc shows every note again."
            )
        )
        if self.isVisible():
            self.search()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        elif event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            QTimer.singleShot(0, self, self._lost_keyboard)
        super().changeEvent(event)

    def _lost_keyboard(self) -> None:
        if self.isVisible() and not self.isActiveWindow() and not self._ending:
            self.end()  # a click elsewhere: every note as it was

    def open(self) -> None:
        """At the top of the screen the pointer is on, with the keyboard."""
        if self.isVisible():
            self.activateWindow()
            return
        self._notes.begin_find()
        self.box.clear()
        self.found = []
        self.chosen = 0
        self.search()
        self.resize(WIDTH, self.sizeHint().height())
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.move(QPoint(area.center().x() - WIDTH // 2, area.top() + TOP))
        self.show()
        self.raise_()
        self.activateWindow()
        if QGuiApplication.platformName() == "xcb":
            activate(int(self.winId()))  # asked for from another app: as a taskbar would
        self.box.setFocus()

    def search(self) -> None:
        self._soon.stop()
        term = self.box.text()
        self.found = self._notes.find(term)
        self.chosen = 0
        if term.strip() and self.found:
            self.found[0].set_found(True)
        if not term.strip():
            self.count.setText(self.tr("Type to find notes on the desktop."))
        elif self.found:
            self.count.setText(self.tr("%n note(s)", "", len(self.found)))
        else:
            self.count.setText(self.tr("No note on the desktop matches."))
        hidden, trashed = self._notes.found_elsewhere(term)
        self.elsewhere.setVisible(bool(hidden or trashed))
        if hidden or trashed:
            self.elsewhere.setText(
                self.tr(
                    "Also in %1 hidden note(s) and %2 in the trash: Ctrl+Enter shows them "
                    "in Stickle Manager."
                )
                .replace("%1", str(hidden))
                .replace("%2", str(trashed))
            )
        self.resize(WIDTH, self.sizeHint().height())  # as wide as ever, as tall as needed

    def move_choice(self, step: int) -> None:
        if not self.found:
            return
        self.found[self.chosen].set_found(False)
        self.chosen = (self.chosen + step) % len(self.found)
        chosen = self.found[self.chosen]
        chosen.set_found(True)
        chosen.raise_()  # in front, while the keyboard stays here
        self.raise_()

    def end(self, choose: bool = False) -> None:
        """Every note back; the one chosen to the front with the keyboard (choose)."""
        if self._ending:
            return
        self._ending = True
        self._soon.stop()
        chosen = self.found[self.chosen] if choose and self.found else None
        self.hide()
        self._notes.end_find(chosen)
        self._ending = False

    def _search_elsewhere_now(self) -> None:
        term = self.box.text().strip()
        self.end()
        self._search_elsewhere(term)

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.box
            and isinstance(event, QKeyEvent)
            and (event.type() == QEvent.Type.KeyPress)
        ):
            key = event.key()
            control = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if key == Qt.Key.Key_Escape:
                self.end()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self._soon.isActive():
                    self.search()  # typed too quickly for the notes to have caught up
                if control:
                    self._search_elsewhere_now()
                else:
                    self.end(choose=True)
                return True
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                if self._soon.isActive():
                    self.search()
                self.move_choice(1 if key == Qt.Key.Key_Down else -1)
                return True
        return super().eventFilter(watched, event)

    @override
    def closeEvent(self, event: QCloseEvent) -> None:
        self.end()
        super().closeEvent(event)

    @override
    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        panel = QColor(self.palette().window().color())
        panel.setAlpha(245)
        painter.setPen(QColor(0, 0, 0, 70))
        painter.setBrush(panel)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)
        painter.end()
