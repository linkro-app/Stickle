"""Setting a note's reminder: when, how often, and whether the note waits out
of sight until then.

The quick choices (in an hour, this evening, tomorrow morning, next Monday)
only fill in the day and time, so that a repeat or waiting out of sight can go
with them; the day and time can be chosen too. Below, the next times it rings.
One that does not repeat cannot be set for a time already past.

The same dialog chooses the time of a reminder written in the text (in_text):
then without repeating, waiting out of sight or turning off, which are a
note's own reminder's only (taking the code out of the text turns it off).

The day can be picked in another calendar (stickle.core.calendars): the
Gregorian one first, then those picked lately, then the rest. The day it
falls on is said below, and why, when it rings on another day.
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
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from stickle.app.calendar_names import calendar_name, change_text, month_name, named_months
from stickle.app.times import moment_text
from stickle.core.calendars import CALENDARS, GREGORIAN, CalendarDate, resolve
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


def _picked_on(calendar: str, day: date) -> CalendarDate:
    """The day to pick in the calendar so that it rings on day: in one whose days
    may come early, a reminder rings a day before the day picked. Beyond the
    years it is worked out for, its last day."""
    chosen = CALENDARS[calendar]
    if chosen.early:
        day += timedelta(days=1)
    try:
        return chosen.from_gregorian(day)
    except ValueError:
        month, leap = chosen.months(chosen.last_year)[-1]
        last = chosen.days_in_month(chosen.last_year, month, leap)
        return CalendarDate(chosen.last_year, month, last, leap)


def _last_day(calendar: str) -> date | None:
    """The last Gregorian day a calendar is worked out for, if its data ends
    within this century or two; None for one worked out by rules alone."""
    chosen = CALENDARS[calendar]
    if chosen.last_year >= 5000:  # by rules: as far as anyone could ask
        return None
    month, leap = chosen.months(chosen.last_year)[-1]
    days = chosen.days_in_month(chosen.last_year, month, leap)
    return chosen.to_gregorian(CalendarDate(chosen.last_year, month, days, leap))


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
        recent_calendars: list[str] | None = None,
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

        # The calendar: the Gregorian one, those picked lately, then the rest.
        self.calendar_label = QLabel(self)
        self.calendar_box = QComboBox(self)
        self.calendar_label.setBuddy(self.calendar_box)
        recent = [c for c in recent_calendars or [] if c in CALENDARS]
        self._calendars = [GREGORIAN, *recent]
        self._calendars += sorted(set(CALENDARS) - set(recent), key=calendar_name)
        for calendar in self._calendars:
            self.calendar_box.addItem("", calendar)
        if recent and len(self._calendars) > len(recent) + 1:
            self.calendar_box.insertSeparator(len(recent) + 1)
        calendar_row = QHBoxLayout()
        calendar_row.addWidget(self.calendar_label)
        calendar_row.addWidget(self.calendar_box)
        calendar_row.addStretch(1)
        # A day of another calendar: its year, month (leap ones too) and day.
        self.year_box = QSpinBox(self)
        self.year_box.setRange(1, 9999)
        self.month_box = QComboBox(self)
        self.day_box = QComboBox(self)
        self.gregorian_label = QLabel(self)
        self.changes_label = QLabel(self)
        self.changes_label.setWordWrap(True)
        self._calendar_parts: tuple[QWidget, ...] = (
            self.year_box,
            self.month_box,
            self.day_box,
            self.gregorian_label,
        )
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
        when.addWidget(self.year_box)
        when.addWidget(self.month_box)
        when.addWidget(self.day_box)
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
        layout.addLayout(calendar_row)
        layout.addLayout(when)
        layout.addWidget(self.gregorian_label)
        layout.addWidget(self.changes_label)
        layout.addLayout(repeat_row)
        layout.addLayout(days_row)
        layout.addWidget(self.hide_box)
        layout.addWidget(self.coming_label)
        layout.addWidget(self.past_label)
        layout.addWidget(self.buttons)

        if current is None:
            self._fill(self._tomorrow_morning())
        else:
            if current.picked is not None and current.calendar in CALENDARS:
                self.calendar_box.setCurrentIndex(self._calendars.index(current.calendar))
            self._fill(datetime.combine(current.start, current.at))
            if current.picked is not None:
                self._fill_picked(current.picked)
            self.repeat_box.setCurrentIndex(REPEATS.index(current.repeat))
            for day, box in enumerate(self.day_boxes):
                box.setChecked(day in current.weekdays)
        self._show_calendar()
        self.calendar_box.currentIndexChanged.connect(self._calendar_changed)
        self.year_box.valueChanged.connect(self._year_changed)
        self.month_box.currentIndexChanged.connect(self._month_changed)
        self.day_box.currentIndexChanged.connect(self._show_coming)
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
        calendar = self.calendar()
        if calendar != GREGORIAN:
            self._fill_picked(_picked_on(calendar, when.date()))

    # Another calendar

    def calendar(self) -> str:
        chosen = self.calendar_box.currentData()
        return chosen if isinstance(chosen, str) else GREGORIAN

    def _show_calendar(self) -> None:
        other = self.calendar() != GREGORIAN
        self.day_edit.setVisible(not other)
        self.day_label.setBuddy(self.year_box if other else self.day_edit)
        for part in self._calendar_parts:
            part.setVisible(other)
        if not other:
            self.changes_label.setVisible(False)

    def _calendar_changed(self) -> None:
        """The day shown in the calendar now chosen: the same Gregorian day."""
        rule = self.rule()  # as it was, before this calendar was chosen
        calendar = self.calendar()
        if calendar != GREGORIAN:
            self._fill_picked(_picked_on(calendar, rule.start))
        else:
            self.day_edit.setDate(QDate(rule.start.year, rule.start.month, rule.start.day))
        self._show_calendar()
        self._show_coming()

    def _fill_picked(self, picked: CalendarDate) -> None:
        for box in (self.year_box, self.month_box, self.day_box):
            box.blockSignals(True)
        chosen = CALENDARS[self.calendar()]
        self.year_box.setRange(chosen.first_year, chosen.last_year)
        self.year_box.setValue(picked.year)
        self._fill_months(picked.year, (picked.month, picked.leap))
        self._fill_days(picked.day)
        for box in (self.year_box, self.month_box, self.day_box):
            box.blockSignals(False)
        self._show_coming()

    def _fill_months(self, year: int, keep: tuple[int, bool]) -> None:
        """The year's months; the one kept chosen (the ordinary month of a leap
        month the year lacks)."""
        months = CALENDARS[self.calendar()].months(year)
        self.month_box.clear()
        for month, leap in months:
            self.month_box.addItem(self._month_item(year, month, leap), (month, leap))
        if keep not in months:
            keep = (keep[0], False)
        self.month_box.setCurrentIndex(months.index(keep) if keep in months else 0)

    def _fill_days(self, keep: int) -> None:
        """The month's days; the one kept chosen, or the last if it is shorter."""
        month, leap = self._month()
        length = CALENDARS[self.calendar()].days_in_month(self.year_box.value(), month, leap)
        self.day_box.clear()
        for day in range(1, length + 1):
            self.day_box.addItem(self._day_item(day), day)
        self.day_box.setCurrentIndex(min(keep, length) - 1)

    def _month(self) -> tuple[int, bool]:
        chosen: object = self.month_box.currentData()
        if isinstance(chosen, tuple) and len(chosen) == 2:  # pyright: ignore[reportUnknownArgumentType]
            month, leap = chosen  # pyright: ignore[reportUnknownVariableType]
            return int(month), bool(leap)  # pyright: ignore[reportUnknownArgumentType]
        return 1, False

    def _day(self) -> int:
        chosen = self.day_box.currentData()
        return chosen if isinstance(chosen, int) else 1

    def _month_item(self, year: int, month: int, leap: bool) -> str:
        calendar = self.calendar()
        name = month_name(calendar, year, month, leap)
        if named_months(calendar):
            return name
        return self.tr("%1", "a month of another calendar, by its number").replace("%1", name)

    def _day_item(self, day: int) -> str:
        return self.tr("%1", "a day of the month, by its number").replace("%1", str(day))

    def _year_changed(self) -> None:
        day = self._day()
        for box in (self.month_box, self.day_box):
            box.blockSignals(True)
        self._fill_months(self.year_box.value(), self._month())
        self._fill_days(day)
        for box in (self.month_box, self.day_box):
            box.blockSignals(False)
        self._show_coming()

    def _month_changed(self) -> None:
        day = self._day()
        self.day_box.blockSignals(True)
        self._fill_days(day)
        self.day_box.blockSignals(False)
        self._show_coming()

    def picked(self) -> CalendarDate | None:
        """The day picked in another calendar; None in the Gregorian one."""
        if self.calendar() == GREGORIAN or self.month_box.count() == 0:
            return None
        month, leap = self._month()
        return CalendarDate(self.year_box.value(), month, self._day(), leap)

    # What is chosen

    def rule(self) -> Rule:
        at = self.time_edit.time()
        repeat: Repeat = self.repeat_box.currentData()
        weekdays = frozenset(n for n, box in enumerate(self.day_boxes) if box.isChecked())
        picked = self.picked()
        resolved = resolve(CALENDARS[self.calendar()], picked) if picked is not None else None
        if picked is not None and resolved is not None:
            start, calendar = resolved.day, self.calendar()
        else:
            day = self.day_edit.date()
            start, calendar, picked = date(day.year(), day.month(), day.day()), GREGORIAN, None
        return Rule(
            start,
            time(at.hour(), at.minute()),
            self._zone,
            repeat,
            weekdays if repeat == "weekly" else frozenset(),
            calendar,
            picked,
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
        self._show_gregorian(rule)
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

    def _show_gregorian(self, rule: Rule) -> None:
        """For a day picked in another calendar: the Gregorian day it rings on, and
        why if that is not the day picked."""
        if rule.picked is None:
            self.changes_label.setVisible(False)
            return
        shown = QDate(rule.start.year, rule.start.month, rule.start.day)
        self.gregorian_label.setText(
            self.tr("= %1 %2")
            .replace("%1", calendar_name(GREGORIAN))
            .replace("%2", QLocale().toString(shown, QLocale.FormatType.LongFormat))
        )
        calendar = CALENDARS[rule.calendar]
        resolved = resolve(calendar, rule.picked)
        changes = sorted(resolved.changes, key=lambda c: c.value) if resolved else []
        lines = [change_text(rule.calendar, c) for c in changes]
        last = _last_day(rule.calendar)
        if rule.repeat in ("monthly", "yearly") and last is not None:
            lines.append(
                self.tr("%1 is worked out until %2: it rings no more after that.")
                .replace("%1", calendar_name(rule.calendar))
                .replace("%2", str(last.year))
            )
        self.changes_label.setText(" ".join(lines))
        self.changes_label.setVisible(bool(lines))

    def retranslate(self) -> None:
        self.setWindowTitle(
            self.tr("Reminder in this line") if self.in_text else self.tr("Reminder")
        )
        self.hour_button.setText(self.tr("In an hour"))
        self.evening_button.setText(self.tr("This evening"))
        self.morning_button.setText(self.tr("Tomorrow morning"))
        self.monday_button.setText(self.tr("Next Monday"))
        self.day_label.setText(self.tr("&Day"))
        self.calendar_label.setText(self.tr("&Calendar"))
        for item, calendar in enumerate(self._calendars):
            index = self.calendar_box.findData(calendar)
            if index >= 0:
                self.calendar_box.setItemText(index, calendar_name(calendar))
            del item
        self.year_box.setAccessibleName(self.tr("Year"))
        self.month_box.setAccessibleName(self.tr("Month"))
        self.day_box.setAccessibleName(self.tr("Day of the month"))
        picked = self.picked()
        if picked is not None:
            self._fill_picked(picked)  # the months and days, named anew
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
