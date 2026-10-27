"""Notes' reminders in the local database; stickle.core.reminders works out when.

A note has one reminder of its own at most. Dealing with a reminder that
repeats moves it on to its first day after now, so the times it rang (or would
have) before are dealt with together; one that does not repeat keeps its row,
dealt with for good (no next day), so that sync can quiet it on other devices.
Turning one off marks its row deleted, for the same reason.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, tzinfo

import apsw

from stickle.core.clock import Clock, moment_of, stamp_of, utc_now
from stickle.core.reminders import Rule, first_after, is_known_zone, is_repeat, moment

NOTE = "note"  # kind: a note's own reminder
GREGORIAN = "gregorian"

_COLUMNS = (
    "id, note_id, calendar, start_day, at_time, time_zone, repeat, weekdays, next_day,"
    " snoozed_until, hide_note"
)


class ReminderPastError(ValueError):
    """A reminder that does not repeat, set for a time already past."""


@dataclass(frozen=True)
class Reminder:
    id: str
    note_id: str
    rule: Rule
    next_day: date | None  # None: dealt with, for good
    snoozed_until: str | None  # it rings then instead
    hide_note: bool  # the note is out of sight until it rings

    def rings_at(self, here: tzinfo | None = None) -> datetime | None:
        """When it rings next (UTC), None if never again; here: this computer's
        time zone, for a reminder that rings wherever it is."""
        if self.next_day is None:
            return None
        if self.snoozed_until is not None:
            return moment_of(self.snoozed_until)
        return moment(self.rule, self.next_day, here)


def _weekdays(text: str) -> frozenset[int]:
    return frozenset(int(part) for part in text.split(",") if part)


def _reminder(row: apsw.SQLiteValues) -> Reminder | None:
    """A row as a reminder; None for one this version cannot read (a calendar or
    repeat a later version added), which it leaves as it is."""
    reminder_id, note_id, calendar, start, at, zone, repeat, weekdays, day, snoozed, hide = row
    if calendar != GREGORIAN or not is_repeat(repeat):
        return None
    try:
        rule = Rule(
            date.fromisoformat(str(start)),
            time.fromisoformat(str(at)),
            None if zone is None else str(zone),
            repeat,
            _weekdays(str(weekdays)),
        )
        next_day = None if day is None else date.fromisoformat(str(day))
    except ValueError:
        return None
    return Reminder(
        str(reminder_id),
        str(note_id),
        rule,
        next_day,
        None if snoozed is None else str(snoozed),
        bool(hide),
    )


class ReminderRepository:
    def __init__(
        self, connection: apsw.Connection, clock: Clock = utc_now, device: str | None = None
    ) -> None:
        self._db = connection
        self._clock = clock
        self._device = device  # this device's id, recorded when one is dealt with

    def _rows(self, where: str, params: tuple[str, ...] = ()) -> list[Reminder]:
        rows = self._db.execute(f"SELECT {_COLUMNS} FROM reminders WHERE {where}", params)
        return [reminder for row in rows if (reminder := _reminder(row)) is not None]

    def of_note(self, note_id: str) -> Reminder | None:
        """The note's own reminder, dealt with or not."""
        found = self._rows("note_id = ? AND kind = ? AND deleted_at IS NULL", (note_id, NOTE))
        return found[0] if found else None

    def get(self, reminder_id: str) -> Reminder | None:
        found = self._rows("id = ? AND deleted_at IS NULL", (reminder_id,))
        return found[0] if found else None

    def pending(self) -> list[Reminder]:
        """Reminders still to ring, of notes not in the trash."""
        return self._rows(
            "deleted_at IS NULL AND next_day IS NOT NULL"
            " AND note_id IN (SELECT id FROM notes WHERE deleted_at IS NULL)"
        )

    def set(
        self, note_id: str, rule: Rule, hide_note: bool = False, here: tzinfo | None = None
    ) -> Reminder:
        """Set the note's own reminder, in place of the one it had."""
        if rule.zone is not None and not is_known_zone(rule.zone):
            raise ValueError(f"unknown time zone {rule.zone!r}")
        now = self._clock()
        first = first_after(rule, moment_of(now), here)
        if first is None:
            raise ReminderPastError(note_id)
        values = (
            rule.start.isoformat(),
            rule.at.strftime("%H:%M"),
            rule.zone,
            rule.repeat,
            ",".join(str(day) for day in sorted(rule.weekdays)),
            first.isoformat(),
            int(hide_note),
            now,
        )
        # The row it had, even one of a later version's calendar: still one a note.
        old = self._db.execute(
            "SELECT id FROM reminders WHERE note_id = ? AND kind = ? AND deleted_at IS NULL",
            (note_id, NOTE),
        ).fetchall()
        reminder_id = str(old[0][0]) if old else str(uuid.uuid4())
        if old:
            self._db.execute(
                "UPDATE reminders SET start_day = ?, at_time = ?, time_zone = ?, repeat = ?,"
                " weekdays = ?, next_day = ?, hide_note = ?, updated_at = ?,"
                " calendar = 'gregorian', snoozed_until = NULL WHERE id = ?",
                (*values, reminder_id),
            )
        else:
            self._db.execute(
                "INSERT INTO reminders (start_day, at_time, time_zone, repeat, weekdays,"
                " next_day, hide_note, updated_at, id, note_id, kind, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*values, reminder_id, note_id, NOTE, now),
            )
        return self._require(reminder_id)

    def _require(self, reminder_id: str) -> Reminder:
        reminder = self.get(reminder_id)
        if reminder is None:
            raise KeyError(reminder_id)
        return reminder

    def turn_off(self, note_id: str) -> None:
        """The note's own reminder, gone."""
        now = self._clock()
        self._db.execute(
            "UPDATE reminders SET deleted_at = ?, updated_at = ?"
            " WHERE note_id = ? AND kind = ? AND deleted_at IS NULL",
            (now, now, note_id, NOTE),
        )

    def dealt_with(self, reminder_id: str, here: tzinfo | None = None) -> Reminder:
        """It rang and was seen: on to its first day after now, if it repeats."""
        reminder = self._require(reminder_id)
        now = self._clock()
        after = moment_of(now)
        if reminder.next_day is not None:
            after = max(after, moment(reminder.rule, reminder.next_day, here))
        repeats = reminder.rule.repeat != "none"
        following = first_after(reminder.rule, after, here) if repeats else None
        self._db.execute(
            "UPDATE reminders SET next_day = ?, snoozed_until = NULL, acked_at = ?,"
            " acked_device = ?, updated_at = ? WHERE id = ?",
            (
                None if following is None else following.isoformat(),
                now,
                self._device,
                now,
                reminder_id,
            ),
        )
        return self._require(reminder_id)

    def snooze(self, reminder_id: str, until: datetime) -> Reminder:
        """Ring again at until, in place of its time."""
        self._db.execute(
            "UPDATE reminders SET snoozed_until = ?, updated_at = ? WHERE id = ?",
            (stamp_of(until), self._clock(), reminder_id),
        )
        return self._require(reminder_id)
