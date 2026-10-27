"""The strip at the top of a note while its reminder rings: the time it was for,
[Done] and [Remind me again], whose arrow offers other times than the usual.

It is reached with the keyboard too: a note that rings while another window
is in use keeps its keyboard for that one, and the strip has it once the note
is used.
"""

from typing import override

from PySide6.QtCore import QCoreApplication, QEvent, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QToolButton, QWidget

TOMORROW_MORNING = -1  # in place of minutes: the morning time, tomorrow
SNOOZES = (5, 10, 30, 60, TOMORROW_MORNING)


class SnoozeButton(QToolButton):
    """[Remind me again]: after the usual wait, its arrow offering the others."""

    snoozed = Signal(int)  # minutes, or TOMORROW_MORNING

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.minutes = 10  # the usual wait
        self.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.clicked.connect(lambda: self.snoozed.emit(self.minutes))
        self.again_menu = QMenu(self)
        self.snooze_actions: dict[int, QAction] = {}
        for minutes in SNOOZES:
            action = self.again_menu.addAction("")
            action.triggered.connect(lambda _=False, minutes=minutes: self.snoozed.emit(minutes))
            self.snooze_actions[minutes] = action
        self.setMenu(self.again_menu)
        self.retranslate()

    def set_minutes(self, minutes: int) -> None:
        self.minutes = minutes
        self.retranslate()

    def retranslate(self) -> None:
        self.setText(self.tr("Remind me again"))
        tip = self.tr("Remind me again: %1").replace("%1", self.snooze_name(self.minutes).lower())
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        for minutes, action in self.snooze_actions.items():
            action.setText(self.snooze_name(minutes))

    def snooze_name(self, minutes: int) -> str:
        if minutes == TOMORROW_MORNING:
            return self.tr("Tomorrow morning")
        if minutes % 60 == 0:
            return self.tr("In %n hour(s)", "", minutes // 60)
        return self.tr("In %n minute(s)", "", minutes)

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)


class ReminderBar(QFrame):
    dealt_with = Signal()
    snoozed = Signal(int)  # minutes, or TOMORROW_MORNING

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("reminderBar")
        self.when = ""
        self.time_label = QLabel(self)
        self.done_button = QPushButton(self)
        # Enter presses it too, as Space does (a note is not a dialog, where
        # buttons take Enter by themselves).
        self.done_button.setAutoDefault(True)
        self.done_button.clicked.connect(self.dealt_with)
        self.again_button = SnoozeButton(self)
        self.again_button.snoozed.connect(self.snoozed)
        self.snooze_actions = self.again_button.snooze_actions
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 6, 4)
        layout.setSpacing(6)
        layout.addWidget(self.time_label, 1)
        layout.addWidget(self.done_button)
        layout.addWidget(self.again_button)
        self.setTabOrder(self.done_button, self.again_button)
        self.hide()
        self.retranslate()

    @property
    def snooze_minutes(self) -> int:
        return self.again_button.minutes

    def ring(self, when: str, snooze_minutes: int) -> None:
        """Shown for a reminder at when (a time as the user reads it)."""
        self.when = when
        self.again_button.set_minutes(snooze_minutes)
        self.retranslate()
        self.show()

    def retranslate(self) -> None:
        self.time_label.setText("⏰ " + self.when if self.when else "")
        name = QCoreApplication.translate("ReminderBar", "Reminder, %1").replace("%1", self.when)
        self.setAccessibleName(name)
        self.time_label.setAccessibleName(name)
        self.done_button.setText(QCoreApplication.translate("ReminderBar", "Done"))
        self.done_button.setAccessibleDescription(name)

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)
