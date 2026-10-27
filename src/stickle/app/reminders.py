"""Setting notes' reminders, and ringing them (stickle.data.reminders keeps
them, stickle.core.reminders works out when).

A reminder rings while Stickle runs: its note comes in front, shown again if
it was hidden, with a strip at its top to say it is done or to be reminded
again, and the system shows a notification once (a secret note's by its title
only). The keyboard stays with what is in use. One that should have rung while
Stickle was not running, or while the computer slept, does not ring late: it
waits among the reminders gone by.

A timer wakes for the next one, and at least once a minute, so that a clock
set otherwise or a computer woken is soon noticed.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta, tzinfo

import apsw
from PySide6.QtCore import (
    QCoreApplication,
    QDateTime,
    QLocale,
    QObject,
    Qt,
    QTimer,
    QTimeZone,
    Signal,
)
from PySide6.QtWidgets import QDialog

from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.reminder_bar import TOMORROW_MORNING
from stickle.app.reminder_dialog import ReminderDialog, when_text
from stickle.app.titles import secret_name, shown_title
from stickle.core.clock import Clock, moment_of, stamp_of, utc_now
from stickle.core.markdown import note_title
from stickle.core.reminders import Rule, is_known_zone, moment, time_zone
from stickle.data.reminders import Reminder, ReminderPastError, ReminderRepository
from stickle.data.settings import MORNING, SNOOZE_MINUTES, Settings

log = logging.getLogger(__name__)

# Later than this, a reminder did not ring on time: Stickle was not running, or
# the computer slept. It is not rung late.
LATE = timedelta(minutes=2)
CHECK_AT_MOST_MS = 60_000


def _ask(dialog: QDialog) -> bool:
    return dialog.exec() == QDialog.DialogCode.Accepted


# Shows the reminder dialog and waits: whether accepted.
ask_reminder: Callable[[QDialog], bool] = _ask  # replaced in tests


def system_zone() -> str | None:
    """This computer's time zone as an IANA name, if the app's data knows it."""
    name = bytes(QTimeZone.systemTimeZoneId().data()).decode("ascii", "replace")
    return name if is_known_zone(name) else None


def time_of_day(moment_utc: datetime) -> str:
    """The time alone (given in UTC), as the user reads it here."""
    shown = QDateTime.fromString(stamp_of(moment_utc), Qt.DateFormat.ISODateWithMs)
    return QLocale().toString(shown.toLocalTime().time(), QLocale.FormatType.ShortFormat)


class ReminderKeeper(QObject):
    # Set, changed, turned off or dealt with: what shows the times refreshes.
    changed = Signal()
    # Reminders gone by while Stickle was not running were found.
    missed_changed = Signal()

    def __init__(
        self,
        repository: ReminderRepository,
        notes: NoteManager,
        settings: Settings | None = None,
        notify: Callable[[str, str], object] | None = None,
        clock: Clock = utc_now,
        zone: str | None = None,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._notes = notes
        self._settings = settings
        self._notify = notify
        self._clock = clock
        # Where reminders are set (stored with each); unknown, wherever they ring.
        self.zone = zone if zone is not None and is_known_zone(zone) else None
        self.here: tzinfo = time_zone(self.zone, datetime.now().astimezone().tzinfo)
        self._ringing: dict[str, tuple[str, str]] = {}  # note id -> (reminder id, its time)
        self.missed: set[str] = set()  # reminder ids
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.check)
        notes.window_opened.connect(self._dress)
        notes.reminders = self

    def _now(self) -> datetime:
        return moment_of(self._clock())

    # Settings

    @property
    def morning(self) -> time:
        text = self._settings.get(MORNING) if self._settings else MORNING.default
        return time.fromisoformat(text)

    def set_morning(self, at: time) -> None:
        if self._settings is not None:
            self._settings.set(MORNING, at.strftime("%H:%M"))

    @property
    def snooze_minutes(self) -> int:
        return self._settings.get(SNOOZE_MINUTES) if self._settings else SNOOZE_MINUTES.default

    def set_snooze_minutes(self, minutes: int) -> None:
        if self._settings is not None:
            self._settings.set(SNOOZE_MINUTES, minutes)

    # What shows the times

    def has(self, note_id: str) -> bool:
        """Whether the note has a reminder still to ring."""
        reminder = self._repository.of_note(note_id)
        return reminder is not None and reminder.next_day is not None

    def coming(self) -> dict[str, datetime]:
        """When each note's reminder rings next (UTC), by note id."""
        times: dict[str, datetime] = {}
        for reminder in self._repository.pending():
            at = reminder.rings_at(self.here)
            if at is not None:
                times[reminder.note_id] = at
        return times

    def _show_next(self, window: NoteWindow) -> None:
        reminder = self._repository.of_note(window.note_id) if window.note_id else None
        at = reminder.rings_at(self.here) if reminder is not None else None
        window.set_next_reminder(None if at is None else when_text(at))

    def _dress(self, window: NoteWindow) -> None:
        """A note's window, opened: its reminder's menu, strip and time."""
        window.reminder_requested.connect(lambda: self.ask(window))
        window.reminder_dealt_with.connect(lambda: self.deal_with(window))

        def snoozed(minutes: int) -> None:
            self.snooze(window, minutes)

        window.reminder_snoozed.connect(snoozed)
        if window.note_id is None:
            return
        self._show_next(window)
        ringing = self._ringing.get(window.note_id)
        if ringing is not None:
            window.ring(ringing[1], self.snooze_minutes)

    # Setting one

    def ask(self, window: NoteWindow) -> None:
        """The note's reminder, set, changed or turned off in the dialog."""
        note_id = self._notes.stored_id(window)
        if note_id is None:
            return
        current = self._repository.of_note(note_id)
        if current is not None and current.next_day is None:
            current = None  # rang and was seen, for good: as if none
        title = secret_name(window.secret_title) if window.secret else note_title(window.text)
        dialog = ReminderDialog(
            title,
            self._now(),
            self.zone,
            self.here,
            self.morning,
            current.rule if current is not None else None,
            current.hide_note if current is not None else False,
            window,
        )
        if not ask_reminder(dialog):
            return
        choice = dialog.choice()
        try:
            if choice.rule is None:
                self._repository.turn_off(note_id)
            else:
                self._repository.set(note_id, choice.rule, choice.hide_note, self.here)
        except ReminderPastError:
            log.info("a reminder for a time already past was not set")
            return
        except apsw.Error as error:
            log.error("could not set a reminder: %s", type(error).__name__)
            return
        log.info("a reminder was %s", "turned off" if choice.rule is None else "set")
        self._quiet(note_id)
        if self._notes.window_for(note_id) is window:
            self._show_next(window)
        if choice.rule is not None and choice.hide_note:
            self._notes.hide_note(note_id)
        self.changed.emit()
        self.check()

    # Ringing

    def start(self) -> None:
        self.check()

    def check(self) -> None:
        """Ring what is due, and wait for the next."""
        self._timer.stop()
        now = self._now()
        soonest: datetime | None = None
        try:
            pending = self._repository.pending()
        except apsw.Error as error:
            log.error("could not read reminders: %s", type(error).__name__)
            pending = []
        ringing = {reminder_id for reminder_id, _ in self._ringing.values()}
        for reminder in pending:
            at = reminder.rings_at(self.here)
            if at is None:
                continue
            if at > now:
                soonest = at if soonest is None else min(soonest, at)
            elif reminder.id in ringing or reminder.id in self.missed:
                continue
            elif now - at > LATE:
                self.missed.add(reminder.id)
                log.info("a reminder went by unrung")
                self.missed_changed.emit()
            else:
                self._ring(reminder, at)
        wait = CHECK_AT_MOST_MS
        if soonest is not None:
            wait = min(wait, max(0, int((soonest - now).total_seconds() * 1000) + 1))
        self._timer.start(wait)

    def _ring(self, reminder: Reminder, at: datetime) -> None:
        window = self._notes.come_forward(reminder.note_id)
        if window is None:
            return
        when = time_of_day(at)
        self._ringing[reminder.note_id] = (reminder.id, when)
        window.ring(when, self.snooze_minutes)
        note = self._notes.note(reminder.note_id)
        title = shown_title(note) if note is not None else ""
        log.info("a reminder rang")
        if self._notify is not None:
            text = QCoreApplication.translate("Reminders", "Reminder, %1").replace("%1", when)
            self._notify(title or "Stickle", text)

    def _quiet(self, note_id: str) -> str | None:
        """The note's reminder stops ringing; its id, if it was."""
        ringing = self._ringing.pop(note_id, None)
        window = self._notes.window_for(note_id)
        if window is not None:
            window.stop_ringing()
        return ringing[0] if ringing is not None else None

    def deal_with(self, window: NoteWindow) -> None:
        """[Done]: it rang and was seen; on to its next time, if it repeats."""
        if window.note_id is None:
            return
        reminder_id = self._quiet(window.note_id)
        if reminder_id is None:
            return
        try:
            self._repository.dealt_with(reminder_id, self.here)
            log.info("a reminder was dealt with")
        except (apsw.Error, KeyError) as error:
            log.error("could not deal with a reminder: %s", type(error).__name__)
        self._show_next(window)
        self.changed.emit()
        self.check()

    def snooze(self, window: NoteWindow, minutes: int) -> None:
        """[Remind me again]: in that many minutes, or tomorrow morning."""
        if window.note_id is None:
            return
        reminder_id = self._quiet(window.note_id)
        if reminder_id is None:
            return
        now = self._now()
        if minutes == TOMORROW_MORNING:
            tomorrow = now.astimezone(self.here).date() + timedelta(days=1)
            until = moment(Rule(tomorrow, self.morning, self.zone), tomorrow, self.here)
        else:
            until = (now + timedelta(minutes=minutes)).astimezone(UTC)
        try:
            self._repository.snooze(reminder_id, until)
            log.info("a reminder was snoozed")
        except (apsw.Error, KeyError) as error:
            log.error("could not snooze a reminder: %s", type(error).__name__)
        self._show_next(window)
        self.changed.emit()
        self.check()
