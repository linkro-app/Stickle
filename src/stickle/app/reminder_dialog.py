"""Setting a note's reminder: when, how often, and whether the note waits out
of sight until then.

The quick choices (in an hour, this evening, tomorrow morning, next Monday)
only fill in the day and time, so that a repeat or waiting out of sight can go
with them; the day and time can be chosen too. Below, the next times it rings.
One that does not repeat cannot be set for a time already past.

The same dialog chooses the time of a reminder written in the text (in_text):
then without repeating, waiting out of sight or turning off, which are a
note's own reminder's only (taking the code out of the text turns it off).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import override

from PySide6.QtCore import QCoreApplication, QDate, QDateTime, QEvent, QLocale, Qt, QTime
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from stickle.app.times import moment_text
from stickle.core.clock import stamp_of
from stickle.core.reminders import REPEATS, Repeat, Rule, coming, first_after, moment

EVENING = time(18, 0)
COMING = 5


@dataclass(frozen=True)
class ReminderChoice:
    rule: Rule | None  # None: turned off
    hide_note: bool = False


def when_text(moment_utc: datetime) -> str:
    """A day and time (given in UTC) with its day of the week, as the user reads
    it here."""
    shown = QDateTime.fromString(stamp_of(moment_utc), Qt.DateFormat.ISODateWithMs)
    return moment_text(shown.toLocalTime(), weekday=True)


def repeat_name(repeat: Repeat) -> str:
    match repeat:
        case "none":
            return QCoreApplication.translate("ReminderDialog", "Does not repeat")
        case "daily":
            return QCoreApplication.translate("ReminderDialog", "Every day")
        case "weekdays":
            return QCoreApplication.translate("ReminderDialog", "Every weekday (Monday to Friday)")
        case "weekly":
            return QCoreApplication.translate("ReminderDialog", "Every week")
        case "monthly":
            return QCoreApplication.translate("ReminderDialog", "Every month")
        case "yearly":
            return QCoreApplication.translate("ReminderDialog", "Every year")


class ReminderDialog(QDialog):
    def __init__(
        self,
        title: str,
        now: datetime,
        zone: str | None,
        here: tzinfo,
        morning: time,
        current: Rule | None = None,
        hidden_until: bool = False,
        parent: QWidget | None = None,
        in_text: bool = False,
    ) -> None:
        super().__init__(parent)
        self._now = now
        self._zone = zone
        self._here = here
        self._morning = morning
        self.in_text = in_text
        self.turned_off = False

        self.note_title = QLabel(title, self)
        self.note_title.setWordWrap(True)
        self.note_title.setTextFormat(Qt.TextFormat.PlainText)
        font = self.note_title.font()
        font.setBold(True)
        self.note_title.setFont(font)

        self.hour_button = QPushButton(self)
        self.evening_button = QPushButton(self)
        self.morning_button = QPushButton(self)
        self.monday_button = QPushButton(self)
        quick = QHBoxLayout()
        picks: list[tuple[QPushButton, Callable[[], datetime]]] = [
            (self.hour_button, self._in_an_hour),
            (self.evening_button, self._this_evening),
            (self.morning_button, self._tomorrow_morning),
            (self.monday_button, self._next_monday),
        ]
        for button, pick in picks:
            button.setAutoDefault(False)
            button.clicked.connect(self._filler(pick))
            quick.addWidget(button)
        quick.addStretch(1)
        self.evening_button.setEnabled(self._local_now().time() < EVENING)

        self.day_label = QLabel(self)
        self.day_edit = QDateEdit(self)
        self.day_edit.setCalendarPopup(True)
        self.day_edit.setDisplayFormat(QLocale().dateFormat(QLocale.FormatType.ShortFormat))
        self.day_label.setBuddy(self.day_edit)
        self.time_label = QLabel(self)
        self.time_edit = QTimeEdit(self)
        self.time_edit.setDisplayFormat(QLocale().timeFormat(QLocale.FormatType.ShortFormat))
        self.time_label.setBuddy(self.time_edit)
        when = QHBoxLayout()
        when.addWidget(self.day_label)
        when.addWidget(self.day_edit)
        when.addSpacing(12)
        when.addWidget(self.time_label)
        when.addWidget(self.time_edit)
        when.addStretch(1)

        self.repeat_label = QLabel(self)
        self.repeat_box = QComboBox(self)
        for repeat in REPEATS:
            self.repeat_box.addItem("", repeat)
        self.repeat_label.setBuddy(self.repeat_box)
        repeat_row = QHBoxLayout()
        repeat_row.addWidget(self.repeat_label)
        repeat_row.addWidget(self.repeat_box)
        repeat_row.addStretch(1)
        # Every week: on these days (Monday first, as stored).
        self.day_boxes = [QCheckBox(self) for _ in range(7)]
        days_row = QHBoxLayout()
        for box in self.day_boxes:
            days_row.addWidget(box)
            box.toggled.connect(self._show_coming)
        days_row.addStretch(1)

        self.hide_box = QCheckBox(self)
        self.hide_box.setChecked(hidden_until)
        self.coming_label = QLabel(self)
        self.coming_label.setWordWrap(True)
        self.coming_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.past_label = QLabel(self)
        self.past_label.setWordWrap(True)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.off_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.DestructiveRole)
        self.off_button.setAutoDefault(False)
        self.off_button.setVisible(current is not None and not in_text)
        self.off_button.clicked.connect(self._turn_off)
        for part in (self.repeat_label, self.repeat_box, self.hide_box):
            part.setVisible(not in_text)

        layout = QVBoxLayout(self)
        layout.addWidget(self.note_title)
        layout.addLayout(quick)
        layout.addLayout(when)
        layout.addLayout(repeat_row)
        layout.addLayout(days_row)
        layout.addWidget(self.hide_box)
        layout.addWidget(self.coming_label)
        layout.addWidget(self.past_label)
        layout.addWidget(self.buttons)

        if current is None:
            self._fill(self._tomorrow_morning())
        else:
            self._fill(datetime.combine(current.start, current.at))
            self.repeat_box.setCurrentIndex(REPEATS.index(current.repeat))
            for day, box in enumerate(self.day_boxes):
                box.setChecked(day in current.weekdays)
        self.day_edit.dateChanged.connect(self._show_coming)
        self.time_edit.timeChanged.connect(self._show_coming)
        self.repeat_box.currentIndexChanged.connect(self._show_coming)
        self.retranslate()
        self.hour_button.setFocus()

    # The quick choices, here and now

    def _local_now(self) -> datetime:
        return self._now.astimezone(self._here).replace(tzinfo=None, second=0, microsecond=0)

    def _in_an_hour(self) -> datetime:
        return self._local_now() + timedelta(hours=1)

    def _this_evening(self) -> datetime:
        return datetime.combine(self._local_now().date(), EVENING)

    def _tomorrow_morning(self) -> datetime:
        return datetime.combine(self._local_now().date() + timedelta(days=1), self._morning)

    def _next_monday(self) -> datetime:
        today = self._local_now().date()
        return datetime.combine(today + timedelta(days=7 - today.weekday()), self._morning)

    def _filler(self, pick: Callable[[], datetime]) -> Callable[[], None]:
        return lambda: self._fill(pick())

    def _fill(self, when: datetime) -> None:
        self.day_edit.setDate(QDate(when.year, when.month, when.day))
        self.time_edit.setTime(QTime(when.hour, when.minute))

    # What is chosen

    def rule(self) -> Rule:
        day = self.day_edit.date()
        at = self.time_edit.time()
        repeat: Repeat = self.repeat_box.currentData()
        weekdays = frozenset(n for n, box in enumerate(self.day_boxes) if box.isChecked())
        return Rule(
            date(day.year(), day.month(), day.day()),
            time(at.hour(), at.minute()),
            self._zone,
            repeat,
            weekdays if repeat == "weekly" else frozenset(),
        )

    def choice(self) -> ReminderChoice:
        if self.turned_off:
            return ReminderChoice(None)
        return ReminderChoice(self.rule(), self.hide_box.isChecked())

    def _turn_off(self) -> None:
        self.turned_off = True
        self.accept()

    def _show_coming(self) -> None:
        rule = self.rule()
        weekly = rule.repeat == "weekly"
        for box in self.day_boxes:
            box.setVisible(weekly)
        if weekly and not any(box.isChecked() for box in self.day_boxes):
            # None chosen: the start's day, which is shown chosen.
            self.day_boxes[rule.start.weekday()].setChecked(True)
            return  # shown again by the box's change
        first = first_after(rule, self._now, self._here)
        days = coming(rule, first, COMING) if first is not None else []
        if days:
            times = [when_text(moment(rule, day, self._here)) for day in days]
            self.coming_label.setText(self.tr("Coming: %1").replace("%1", "\n".join(times)))
        self.coming_label.setVisible(bool(days))
        self.past_label.setText(self.tr("That time has already passed. Choose a later one."))
        self.past_label.setVisible(not days)
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(bool(days))

    def retranslate(self) -> None:
        self.setWindowTitle(
            self.tr("Reminder in this line") if self.in_text else self.tr("Reminder")
        )
        self.hour_button.setText(self.tr("In an hour"))
        self.evening_button.setText(self.tr("This evening"))
        self.morning_button.setText(self.tr("Tomorrow morning"))
        self.monday_button.setText(self.tr("Next Monday"))
        self.day_label.setText(self.tr("&Day"))
        self.time_label.setText(self.tr("&Time"))
        self.repeat_label.setText(self.tr("&Repeat"))
        for index, repeat in enumerate(REPEATS):
            self.repeat_box.setItemText(index, repeat_name(repeat))
        for day, box in enumerate(self.day_boxes):
            box.setText(QLocale().dayName(day + 1, QLocale.FormatType.ShortFormat))
            box.setAccessibleName(QLocale().dayName(day + 1, QLocale.FormatType.LongFormat))
        self.hide_box.setText(self.tr("&Keep the note out of sight until then"))
        self.off_button.setText(self.tr("Turn &off"))
        self._show_coming()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)
