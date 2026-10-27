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

Those gone by unrung are listed, one line each however many times a repeating
one went by, until they are dealt with there or from their note: the list is
shown once as Stickle starts with some, and once as the computer wakes with
new ones (stickle.app.missed_reminders).
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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

from stickle.app.calendar_names import change_text, written_note
from stickle.app.note_view import ReminderLook
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.reminder_bar import TOMORROW_MORNING
from stickle.app.reminder_dialog import ReminderDialog, when_text
from stickle.app.titles import secret_name, shown_title
from stickle.core.calendars import GREGORIAN
from stickle.core.clock import Clock, moment_of, stamp_of, utc_now
from stickle.core.markdown import note_title
from stickle.core.reminders import Rule, changes_on, is_known_zone, moment, time_zone
from stickle.core.text_reminders import codes, due, with_code
from stickle.data.reminders import Reminder, ReminderPastError, ReminderRepository
from stickle.data.settings import (
    MORNING,
    RECENT_CALENDARS,
    RECENT_CALENDARS_KEPT,
    SNOOZE_MINUTES,
    Settings,
)

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


@dataclass(frozen=True)
class Missed:
    """A reminder gone by unrung, as the list of them shows it."""

    reminder_id: str
    note_id: str
    title: str  # its note's (a secret note's own title)
    first: datetime  # when it first went by (UTC)
    times: int  # how many times it has by now
    reason: str = ""  # why not on the day picked in its calendar, if so


class ReminderKeeper(QObject):
    # Set, changed, turned off or dealt with: what shows the times refreshes.
    changed = Signal()
    # Reminders gone by while Stickle was not running were found, or are no longer.
    missed_changed = Signal()
    # Some went by as Stickle was not running, or the computer slept: listed for
    # the user once, now.
    missed_found = Signal()

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
        # Note id -> why its reminder ringing now is not on the day picked, if so.
        self._reasons: dict[str, str] = {}
        self._checked_at: datetime | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.check)
        notes.window_opened.connect(self._dress)
        notes.reminders = self

    def now(self) -> datetime:
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

    @property
    def recent_calendars(self) -> list[str]:
        """The calendars other than the Gregorian picked lately, the latest first."""
        return self._settings.get(RECENT_CALENDARS) if self._settings else []

    def _picked_in(self, calendar: str) -> None:
        """A reminder was set in this calendar: first among those picked lately."""
        if self._settings is None or calendar == GREGORIAN:
            return
        recent = [calendar, *(c for c in self.recent_calendars if c != calendar)]
        try:
            self._settings.set(RECENT_CALENDARS, recent[:RECENT_CALENDARS_KEPT])
        except apsw.Error as error:
            log.warning("could not keep the calendar picked: %s", type(error).__name__)

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
            self.now().astimezone(self.here).replace(tzinfo=None),
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
            reason = self._reasons.get(window.note_id, "")
            window.ring(list(ringing.values())[-1][0], self.snooze_minutes, reason)

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
            self.now(),
            None,
            self.here,
            self.morning,
            Rule(current.date(), current.time(), None) if current is not None else None,
            parent=window,
            in_text=True,
            recent_calendars=self.recent_calendars,
        )
        if not ask_reminder(dialog):
            return
        rule = dialog.choice().rule
        if rule is None:
            return
        note = written_note(rule) if rule.picked is not None else None
        window.write_in_line(with_code(line, datetime.combine(rule.start, rule.at), note))
        self._picked_in(rule.calendar)
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
            self.now(),
            self.zone,
            self.here,
            self.morning,
            current.rule if current is not None else None,
            current.hide_note if current is not None else False,
            window,
            recent_calendars=self.recent_calendars,
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
        if choice.rule is not None:
            self._picked_in(choice.rule.calendar)
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
        if self.missed:
            self.missed_found.emit()

    def check(self) -> None:
        """Ring what is due, and wait for the next."""
        self._timer.stop()
        now = self.now()
        # Not looked at for longer than the timer waits: the computer slept (or
        # its clock was put forward), and what went by meanwhile is told.
        woke = (
            self._checked_at is not None
            and now - self._checked_at > timedelta(milliseconds=CHECK_AT_MOST_MS) + LATE
        )
        self._checked_at = now
        soonest: datetime | None = None
        try:
            pending = self._repository.pending()
        except apsw.Error as error:
            log.error("could not read reminders: %s", type(error).__name__)
            self._timer.start(CHECK_AT_MOST_MS)
            return
        ringing = {reminder_id for rung in self._ringing.values() for reminder_id in rung}
        # Counted again each time: one set anew, dealt with or gone is no longer.
        missed: set[str] = set()
        for reminder in pending:
            at = reminder.rings_at(self.here)
            if at is None:
                continue
            if at > now:
                soonest = at if soonest is None else min(soonest, at)
            elif reminder.id in ringing:
                continue
            elif reminder.id in self.missed:
                missed.add(reminder.id)
            elif now - at > LATE:
                missed.add(reminder.id)
                log.info("a reminder went by unrung")
            else:
                self._ring(reminder, at)
        if missed != self.missed:
            new = bool(missed - self.missed)
            self.missed = missed
            self.missed_changed.emit()
            if new and woke:
                self.missed_found.emit()
        wait = CHECK_AT_MOST_MS
        if soonest is not None:
            wait = min(wait, max(0, int((soonest - now).total_seconds() * 1000) + 1))
        self._timer.start(wait)

    def reason(self, reminder: Reminder) -> str:
        """Why a reminder rings on its next day rather than the day picked in its
        calendar, as said under its time; "" for one that rings as picked."""
        if reminder.next_day is None:
            return ""
        changes = changes_on(reminder.rule, reminder.next_day)
        calendar = reminder.rule.calendar
        return " ".join(change_text(calendar, c) for c in sorted(changes, key=lambda c: c.value))

    def _time_text(self, at: datetime) -> str:
        """When a reminder was for: the time alone if today, else the day too."""
        today = self.now().astimezone(self.here).date()
        return time_of_day(at) if at.astimezone(self.here).date() == today else when_text(at)

    def _ring(self, reminder: Reminder, at: datetime, tell: bool = True) -> NoteWindow | None:
        """Its note in front with the strip; tell: with the system's notification."""
        window = self._notes.come_forward(reminder.note_id)
        if window is None:
            return None
        when = self._time_text(at)
        self._ringing.setdefault(reminder.note_id, {})[reminder.id] = (when, reminder.written)
        self._reasons[reminder.note_id] = self.reason(reminder)
        window.ring(when, self.snooze_minutes, self._reasons[reminder.note_id])
        if reminder.written is not None:
            window.refresh_formatted()  # its line stands out
        if not tell:
            return window
        note = self._notes.note(reminder.note_id)
        title = shown_title(note) if note is not None else ""
        log.info("a reminder rang")
        if self._notify is not None:
            text = QCoreApplication.translate("Reminders", "Reminder, %1").replace("%1", when)
            self._notify(title or "Stickle", text)
        return window

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

    def _until(self, minutes: int) -> datetime:
        """When one put off by that many minutes (or till tomorrow morning) rings."""
        now = self.now()
        if minutes == TOMORROW_MORNING:
            tomorrow = now.astimezone(self.here).date() + timedelta(days=1)
            return moment(Rule(tomorrow, self.morning, self.zone), tomorrow, self.here)
        return (now + timedelta(minutes=minutes)).astimezone(UTC)

    def snooze(self, window: NoteWindow, minutes: int) -> None:
        """[Remind me again]: in that many minutes, or tomorrow morning; every
        reminder ringing in the note."""
        if window.note_id is None:
            return
        until = self._until(minutes)
        for reminder_id in self._quiet(window.note_id):
            try:
                self._repository.snooze(reminder_id, until)
                log.info("a reminder was snoozed")
            except (apsw.Error, KeyError) as error:
                log.error("could not snooze a reminder: %s", type(error).__name__)
        self._show_next(window)
        self.changed.emit()
        self.check()

    # Those gone by unrung

    def missed_list(self) -> list[Missed]:
        """The reminders gone by unrung, the earliest first."""
        now = self.now()
        found: list[Missed] = []
        try:
            for reminder_id in self.missed:
                reminder = self._repository.get(reminder_id)
                first = reminder.rings_at(self.here) if reminder is not None else None
                if reminder is None or first is None:
                    continue
                note = self._notes.note(reminder.note_id)
                found.append(
                    Missed(
                        reminder.id,
                        reminder.note_id,
                        shown_title(note) if note is not None else "",
                        first,
                        reminder.gone_by(now, self.here),
                        self.reason(reminder),
                    )
                )
        except apsw.Error as error:
            log.error("could not read reminders: %s", type(error).__name__)
        return sorted(found, key=lambda missed: (missed.first, missed.reminder_id))

    def open_missed(self, reminder_id: str) -> None:
        """One gone by, chosen in the list: its note in front with its strip, the
        keyboard on Done (its line standing out, for one written in the text)."""
        if reminder_id not in self.missed:
            return
        reminder = self._repository.get(reminder_id)
        at = reminder.rings_at(self.here) if reminder is not None else None
        if reminder is None or at is None:
            return
        window = self._ring(reminder, at, tell=False)
        if window is None:
            return
        log.info("a reminder gone by was opened")
        self.missed.discard(reminder_id)
        self.missed_changed.emit()
        # As a note chosen from a list is: GNOME left it under the others otherwise.
        window.bring_to_front()
        window.reminder_bar.done_button.setFocus()

    def _done_in_list(self, reminders: list[Reminder]) -> None:
        """Those gone by, dealt with in the list: out of it, and their notes'
        times shown anew."""
        for reminder in reminders:
            self.missed.discard(reminder.id)
        for note_id in {reminder.note_id for reminder in reminders}:
            window = self._notes.window_for(note_id)
            if window is not None:
                self._show_next(window)
        self.missed_changed.emit()
        self.changed.emit()
        self.check()

    def deal_with_missed(self, reminder_ids: Iterable[str]) -> None:
        """[Done] in the list: seen, every time it went by (one that repeats, on
        to its next time)."""
        dealt: list[Reminder] = []
        for reminder_id in reminder_ids:
            if reminder_id not in self.missed:
                continue
            try:
                dealt.append(self._repository.dealt_with(reminder_id, self.here))
                log.info("a reminder gone by was dealt with")
            except (apsw.Error, KeyError) as error:
                log.error("could not deal with a reminder: %s", type(error).__name__)
        self._done_in_list(dealt)

    def snooze_missed(self, reminder_id: str, minutes: int) -> None:
        """[Remind me again] in the list: it rings in that many minutes, or
        tomorrow morning."""
        if reminder_id not in self.missed:
            return
        try:
            snoozed = self._repository.snooze(reminder_id, self._until(minutes))
            log.info("a reminder gone by was snoozed")
        except (apsw.Error, KeyError) as error:
            log.error("could not snooze a reminder: %s", type(error).__name__)
            return
        self._done_in_list([snoozed])
