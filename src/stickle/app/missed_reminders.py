"""The window listing the reminders gone by unrung, while Stickle was not
running or the computer slept: one line each, however many times a repeating
one went by, saying how long ago it was or how many times since when, with
[Done] and [Remind me again] for it, and [Done] for all below.

Choosing a line (a click, or Enter) brings its note forward with its strip,
the keyboard on Done. The window is shown once as Stickle starts with some
(or the computer wakes with new ones), and from the tray and Stickle Manager
while any is left; it closes once none is.
"""

import itertools
import logging
from datetime import datetime
from typing import override

from PySide6.QtCore import QDate, QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QHideEvent, QKeyEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QCommandLinkButton,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from stickle.app.in_use import OnTopWhileInUse
from stickle.app.note_window import RAISE_AGAIN_MS
from stickle.app.reminder_bar import SnoozeButton
from stickle.app.reminder_dialog import when_text
from stickle.app.reminders import Missed, ReminderKeeper
from stickle.app.times import day_text
from stickle.app.window_flags import set_stays_on_top
from stickle.platform.linux.x11 import activate

log = logging.getLogger(__name__)

REFRESH_MS = 60_000  # "3 minutes ago" kept true while the window is open
TITLE_LENGTH = 50  # a longer one is cut short, not the window made wider


class MissedRow(QFrame):
    """One reminder gone by: its note, how long ago, [Done], [Remind me again]."""

    def __init__(self, missed: Missed, parent: QWidget) -> None:
        super().__init__(parent)
        self.missed = missed
        self.open_button = QCommandLinkButton(self)
        self.done_button = QPushButton(self)
        self.again_button = SnoozeButton(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.open_button, 1)
        layout.addWidget(self.done_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.again_button, 0, Qt.AlignmentFlag.AlignVCenter)

    def parts(self) -> list[QWidget]:
        """What the keyboard goes to, left to right."""
        return [self.open_button, self.done_button, self.again_button]


class MissedDialog(QDialog):
    def __init__(self, keeper: ReminderKeeper) -> None:
        # A window of its own, not a dialog: X11 window managers keep a dialog
        # whose window is not shown (the Stickle window, or none) above all the
        # app's windows, and the note brought forward from it came up below it.
        super().__init__(None, Qt.WindowType.Window)
        self._keeper = keeper
        # Shown by itself or from the tray: kept above pinned notes until the
        # user moves on from it.
        self._on_top = OnTopWhileInUse(self)
        self.intro = QLabel(self)
        self.intro.setWordWrap(True)
        self.rows: list[MissedRow] = []
        self._rows_box = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_box)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.addStretch(1)
        self.area = QScrollArea(self)
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.Shape.NoFrame)
        # Not a stop of its own: the keyboard goes to its lines, which it follows.
        self.area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.area.setWidget(self._rows_box)
        self.all_button = QPushButton(self)
        self.all_button.clicked.connect(self.deal_with_all)
        self.close_button = QPushButton(self)
        self.close_button.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.all_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro)
        layout.addWidget(self.area, 1)
        layout.addLayout(buttons)
        self.resize(480, 360)
        self._ticker = QTimer(self)
        self._ticker.setInterval(REFRESH_MS)
        self._ticker.timeout.connect(self.refresh_texts)
        keeper.missed_changed.connect(self.refresh)
        self.retranslate()

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Missed reminders"))
        self.intro.setText(
            self.tr(
                "These reminders came while Stickle was not running or the computer was "
                "asleep. Choose one to go to its note."
            )
        )
        self.all_button.setText(self.tr("&All done"))
        self.close_button.setText(self.tr("Close"))
        self.refresh_texts()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    # What each line says

    def gone_text(self, missed: Missed, now: datetime) -> str:
        """How long ago it went by, or how many times since when."""
        if missed.times > 1:
            local = missed.first.astimezone(self._keeper.here)
            return self.tr("%n times since %1", "", missed.times).replace(
                "%1", day_text(QDate(local.year, local.month, local.day))
            )
        minutes = max(0, int((now - missed.first).total_seconds()) // 60)
        if minutes < 60:
            return self.tr("%n minute(s) ago", "", minutes)
        if minutes < 48 * 60:
            return self.tr("%n hour(s) ago", "", minutes // 60)
        return self.tr("%n day(s) ago", "", minutes // (24 * 60))

    def _say(self, row: MissedRow, now: datetime) -> None:
        missed = row.missed
        title = missed.title or self.tr("(empty note)")
        gone = self.gone_text(missed, now)
        shown = title if len(title) <= TITLE_LENGTH else title[: TITLE_LENGTH - 1] + "…"
        row.open_button.setText(shown.replace("&", "&&"))  # & kept, not a shortcut mark
        row.open_button.setDescription(gone)
        row.open_button.setAccessibleName(f"{title}, {gone}")
        row.open_button.setToolTip(when_text(missed.first))
        row.done_button.setText(self.tr("Done"))
        row.done_button.setAccessibleName(self.tr("Done: %1").replace("%1", title))
        row.again_button.set_minutes(self._keeper.snooze_minutes)

    def refresh_texts(self) -> None:
        now = self._keeper.now()
        for row in self.rows:
            self._say(row, now)

    # The lines

    def refresh(self) -> None:
        """The lines as the reminders gone by are now; closed once there are none."""
        focus = self._focus_place()
        for row in self.rows:
            # Gone at once, though its button may be the one being pressed.
            row.hide()
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self.rows = []
        for missed in self._keeper.missed_list():
            row = MissedRow(missed, self._rows_box)
            self._connect(row, missed.reminder_id)
            for part in row.parts():
                part.installEventFilter(self)
            self._rows_layout.insertWidget(len(self.rows), row)
            self.rows.append(row)
        self.refresh_texts()
        self._set_tab_order()
        if not self.rows and self.isVisible():
            self.close()
        elif focus is not None:
            self._focus(*focus)

    def _connect(self, row: MissedRow, reminder_id: str) -> None:
        keeper = self._keeper

        def snoozed(minutes: int) -> None:
            keeper.snooze_missed(reminder_id, minutes)

        row.open_button.clicked.connect(lambda: self.open_one(reminder_id))
        row.done_button.clicked.connect(lambda: keeper.deal_with_missed([reminder_id]))
        row.again_button.snoozed.connect(snoozed)

    def _set_tab_order(self) -> None:
        order: list[QWidget] = [part for row in self.rows for part in row.parts()]
        order += [self.all_button, self.close_button]
        for before, after in itertools.pairwise(order):
            QWidget.setTabOrder(before, after)

    def _focus_place(self) -> tuple[str, int, int] | None:
        """Where the keyboard is among the lines: the reminder, its line, the part."""
        focus = QApplication.focusWidget()
        for number, row in enumerate(self.rows):
            parts = row.parts()
            if focus in parts:
                return row.missed.reminder_id, number, parts.index(focus)
        return None

    def _focus(self, reminder_id: str, number: int, part: int) -> None:
        """The keyboard back on the line it was on, or the one now in its place."""
        if not self.rows:
            return
        row = next((r for r in self.rows if r.missed.reminder_id == reminder_id), None)
        if row is None:
            row = self.rows[min(number, len(self.rows) - 1)]
        row.parts()[part].setFocus()

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Up and down go from line to line, staying on the same part.
        if isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
            step = {Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1}.get(Qt.Key(event.key()))
            if step is not None and event.modifiers() == Qt.KeyboardModifier.NoModifier:
                for number, row in enumerate(self.rows):
                    for index, part in enumerate(row.parts()):
                        if part is not watched:
                            continue
                        to = number + step
                        if 0 <= to < len(self.rows):
                            target = self.rows[to].parts()[index]
                            target.setFocus()
                            self.area.ensureWidgetVisible(target)
                        return True
        return super().eventFilter(watched, event)

    # Acting on them

    def open_one(self, reminder_id: str) -> None:
        # No longer above the others first: kept there, it covered the note it
        # brings forward (GNOME).
        set_stays_on_top(self, False)
        self._keeper.open_missed(reminder_id)

    def deal_with_all(self) -> None:
        self._keeper.deal_with_missed([row.missed.reminder_id for row in self.rows])

    def present(self) -> None:
        """Shown, or brought forward if it already is, the keyboard on its first
        line; above pinned notes until the user moves on from it."""
        self.refresh()
        if not self.rows:
            return
        log.info("%d reminder(s) gone by listed", len(self.rows))
        set_stays_on_top(self, True)
        self.show()
        self._come_forward()
        if QGuiApplication.platformName() == "xcb":
            # Shown as Stickle starts, it came up without the keyboard on Ubuntu:
            # asked for again once it is surely on screen.
            QTimer.singleShot(RAISE_AGAIN_MS, self, self._come_forward)
        self.rows[0].open_button.setFocus()

    def _come_forward(self) -> None:
        if not self.isVisible() or self.isActiveWindow():
            return
        self.raise_()
        self.activateWindow()
        if QGuiApplication.platformName() == "xcb":
            # Asked for as a taskbar would; the application's own request above
            # may be refused.
            activate(int(self.winId()))

    @override
    def showEvent(self, event: QShowEvent) -> None:
        self._ticker.start()
        super().showEvent(event)

    @override
    def hideEvent(self, event: QHideEvent) -> None:
        self._ticker.stop()
        super().hideEvent(event)
