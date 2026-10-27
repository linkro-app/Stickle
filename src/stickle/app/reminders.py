"""Setting notes' reminders, and ringing them (stickle.data.reminders keeps
them, stickle.core.reminders works out when).

A reminder rings while Stickle runs: its note comes in front, shown again if
it was hidden, with a strip at its top to say it is done or to be reminded
again, and the system shows a notification once (a secret note's by its title
only). The keyboard stays with what is in use. One that should have rung while
Stickle was not running, or while the computer slept, does not ring late: it
waits among the reminders gone by.

Reminders written in a note's text (stickle.core.text_reminders) ring the same
way, the time written standing out in the formatted note; they follow the text
each time it is saved, and a secret note's ring while it is covered, by the
times saved while it was open. Every reminder ringing in a note shares its
strip: Done and Remind me again are for all of them.

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

from stickle.app.note_view import ReminderLook
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.reminder_bar import TOMORROW_MORNING
from stickle.app.reminder_dialog import ReminderDialog, when_text
from stickle.app.titles import secret_name, shown_title
from stickle.core.clock import Clock, moment_of, stamp_of, utc_now
from stickle.core.markdown import note_title
from stickle.core.reminders import Rule, is_known_zone, moment, time_zone
from stickle.core.text_reminders import codes, due, with_code
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
        # Note id -> what rings in it: reminder id -> its time, and the time written
        # in the text for one written there.
        self._ringing: dict[str, dict[str, tuple[str, datetime | None]]] = {}
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
            self.follow_texts()  # a day written without a time rings then

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
        """When each note rings next (UTC), its own reminder or one in its text, by
        note id."""
        times: dict[str, datetime] = {}
        for reminder in self._repository.pending():
            at = reminder.rings_at(self.here)
            if at is not None and (reminder.note_id not in times or at < times[reminder.note_id]):
                times[reminder.note_id] = at
        return times

    def look(self, note_id: str | None) -> ReminderLook:
        """How the reminders written in a note's text show in its formatted view."""
        ringing = self._ringing.get(note_id or "", {})
        return ReminderLook(
            self._now().astimezone(self.here).replace(tzinfo=None),
            self.morning,
            frozenset(written for _, written in ringing.values() if written is not None),
        )

    def _show_next(self, window: NoteWindow) -> None:
        reminder = self._repository.of_note(window.note_id) if window.note_id else None
        at = reminder.rings_at(self.here) if reminder is not None else None
        window.set_next_reminder(None if at is None else when_text(at))

    def _dress(self, window: NoteWindow) -> None:
        """A note's window, opened: its reminders' menu, strip and times."""
        window.reminder_requested.connect(lambda: self.ask(window))
        window.text_reminder_requested.connect(lambda: self.write(window))
        window.reminder_dealt_with.connect(lambda: self.deal_with(window))

        def snoozed(minutes: int) -> None:
            self.snooze(window, minutes)

        window.reminder_snoozed.connect(snoozed)
        window.view.reminder_look = lambda: self.look(window.note_id)
        window.refresh_formatted()  # drawn before, with its codes as written
        if window.note_id is None:
            return
        self._show_next(window)
        ringing = self._ringing.get(window.note_id)
        if ringing:
            window.ring(list(ringing.values())[-1][0], self.snooze_minutes)

    # Those written in the text

    def text_saved(self, note_id: str, text: str) -> None:
        """A note's text was saved: its reminders written there follow it."""
        try:
            changed = self._repository.match_text(note_id, due(text, self.morning).keys())
        except apsw.Error as error:
            log.error("could not follow a note's reminders: %s", type(error).__name__)
            return
        if not changed:
            return
        log.info("the reminders written in a note changed")
        # One taken out of the text while it rang rings no more.
        ringing = self._ringing.get(note_id, {})
        gone = [reminder_id for reminder_id in ringing if self._repository.get(reminder_id) is None]
        if gone:
            self._quiet(note_id, gone)
        self.changed.emit()
        self.check()

    def follow_texts(self) -> None:
        """Every note's reminders written in its text, as stored now (a secret
        note's are as last saved open)."""
        morning = self.morning
        try:
            for note in self._notes.listed_notes():
                if not note.secret:
                    self._repository.match_text(note.id, due(note.body, morning).keys())
        except apsw.Error as error:
            log.error("could not follow the notes' reminders: %s", type(error).__name__)
        self.changed.emit()
        self.check()

    def write(self, window: NoteWindow) -> None:
        """A reminder written at the end of the line being edited (in place of
        the one it has), at a time chosen in the dialog."""
        if not window.editing:
            return
        line = window.editor.textCursor().block().text()
        on_line = codes(line)
        current = on_line[0].when(self.morning) if on_line else None
        title = secret_name(window.secret_title) if window.secret else note_title(window.text)
        dialog = ReminderDialog(
            title,
            self._now(),
            None,
            self.here,
            self.morning,
            Rule(current.date(), current.time(), None) if current is not None else None,
            parent=window,
            in_text=True,
        )
        if not ask_reminder(dialog):
            return
        rule = dialog.choice().rule
        if rule is None:
            return
        window.write_in_line(with_code(line, datetime.combine(rule.start, rule.at)))
        log.info("a reminder was written in a note")

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
        if current is not None:
            self._quiet(note_id, [current.id])
        if self._notes.window_for(note_id) is window:
            self._show_next(window)
        if choice.rule is not None and choice.hide_note:
            self._notes.hide_note(note_id)
        self.changed.emit()
        self.check()

    # Ringing

    def start(self) -> None:
        self.follow_texts()  # notes brought in, or changed by an earlier version

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
        ringing = {reminder_id for rung in self._ringing.values() for reminder_id in rung}
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
        self._ringing.setdefault(reminder.note_id, {})[reminder.id] = (when, reminder.written)
        window.ring(when, self.snooze_minutes)
        if reminder.written is not None:
            window.refresh_formatted()  # its line stands out
        note = self._notes.note(reminder.note_id)
        title = shown_title(note) if note is not None else ""
        log.info("a reminder rang")
        if self._notify is not None:
            text = QCoreApplication.translate("Reminders", "Reminder, %1").replace("%1", when)
            self._notify(title or "Stickle", text)

    def _quiet(self, note_id: str, reminder_ids: list[str] | None = None) -> list[str]:
        """Those of the note's reminders (all, for None) stop ringing: the ids of
        the ones that rang. The strip goes once none rings."""
        ringing = self._ringing.get(note_id, {})
        quieted = [
            i for i in (list(ringing) if reminder_ids is None else reminder_ids) if i in ringing
        ]
        for reminder_id in quieted:
            del ringing[reminder_id]
        if not ringing:
            self._ringing.pop(note_id, None)
        window = self._notes.window_for(note_id)
        if window is not None and quieted:
            if ringing:
                window.ring(list(ringing.values())[-1][0], self.snooze_minutes)
            else:
                window.stop_ringing()
            window.refresh_formatted()
        return quieted

    def deal_with(self, window: NoteWindow) -> None:
        """[Done]: what rang was seen; each on to its next time, if it repeats.
        Every reminder ringing in the note goes together (one strip)."""
        if window.note_id is None:
            return
        for reminder_id in self._quiet(window.note_id):
            try:
                self._repository.dealt_with(reminder_id, self.here)
                log.info("a reminder was dealt with")
            except (apsw.Error, KeyError) as error:
                log.error("could not deal with a reminder: %s", type(error).__name__)
        self._show_next(window)
        self.changed.emit()
        self.check()

    def snooze(self, window: NoteWindow, minutes: int) -> None:
        """[Remind me again]: in that many minutes, or tomorrow morning; every
        reminder ringing in the note."""
        if window.note_id is None:
            return
        now = self._now()
        if minutes == TOMORROW_MORNING:
            tomorrow = now.astimezone(self.here).date() + timedelta(days=1)
            until = moment(Rule(tomorrow, self.morning, self.zone), tomorrow, self.here)
        else:
            until = (now + timedelta(minutes=minutes)).astimezone(UTC)
        for reminder_id in self._quiet(window.note_id):
            try:
                self._repository.snooze(reminder_id, until)
                log.info("a reminder was snoozed")
            except (apsw.Error, KeyError) as error:
                log.error("could not snooze a reminder: %s", type(error).__name__)
        self._show_next(window)
        self.changed.emit()
        self.check()
