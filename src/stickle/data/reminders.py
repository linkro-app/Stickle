"""Notes' reminders in the local database; stickle.core.reminders works out when.

A note has one reminder of its own at most, and one for each time written in
its text (stickle.core.text_reminders): those follow the text each time it is
saved, matched by the time written, never by a number hidden in the text.
Dealing with a reminder that repeats moves it on to its first day after now,
so the times it rang (or would have) before are dealt with together; one that
does not repeat keeps its row, dealt with for good (no next day), so that sync
can quiet it on other devices. Turning one off, or taking it out of the text,
marks its row deleted, for the same reason.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, tzinfo

import apsw

from stickle.core.clock import Clock, moment_of, stamp_of, utc_now
from stickle.core.reminders import Rule, first_after, is_known_zone, is_repeat, moment

NOTE = "note"  # kind: a note's own reminder
TEXT = "text"  # kind: one written in its text
GREGORIAN = "gregorian"

_COLUMNS = (
    "id, note_id, calendar, start_day, at_time, time_zone, repeat, weekdays, next_day,"
    " snoozed_until, hide_note, kind"
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
    kind: str = NOTE

    @property
    def written(self) -> datetime | None:
        """One written in the text: the time written there."""
        return datetime.combine(self.rule.start, self.rule.at) if self.kind == TEXT else None

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
    reminder_id, note_id, calendar, start, at, zone, repeat, weekdays, day, snoozed, hide, kind = (
        row
    )
    if calendar != GREGORIAN or not is_repeat(repeat) or kind not in (NOTE, TEXT):
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
        str(kind),
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

    def match_text(self, note_id: str, times: Iterable[datetime]) -> bool:
        """The reminders written in the note's text, as it was saved: one for each
        time written (on this computer's clock). A time still written keeps its
        reminder as it is (rung, snoozed or not); a new one gets a reminder; one
        no longer written loses it. Whether anything changed."""
        wanted = {(when.date().isoformat(), when.strftime("%H:%M")) for when in times}
        now = self._clock()
        rows = self._db.execute(
            "SELECT id, start_day, at_time FROM reminders"
            " WHERE note_id = ? AND kind = ? AND deleted_at IS NULL",
            (note_id, TEXT),
        ).fetchall()
        kept = {(str(day), str(at)): str(row_id) for row_id, day, at in rows}
        gone = [row_id for key, row_id in kept.items() if key not in wanted]
        new = wanted - kept.keys()
        if not (gone or new):
            return False
        with self._db:
            self._db.executemany(
                "UPDATE reminders SET deleted_at = ?, updated_at = ? WHERE id = ?",
                [(now, now, row_id) for row_id in gone],
            )
            self._db.executemany(
                "INSERT INTO reminders (id, note_id, kind, start_day, at_time, next_day,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(str(uuid.uuid4()), note_id, TEXT, day, at, day, now, now) for day, at in new],
            )
        return True

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
