"""A note's reminders as fields of its file (stickle.sync.local), and those
fields taken in.

- "reminder": the note's own reminder as set: {"calendar", "start_day",
  "at_time", "time_zone", "repeat", "weekdays", "hide_note", "first_day"} as
  stickle.data.reminders keeps them; null once it is turned off.
- "reminder_state": how it was dealt with: {"for", "next_day",
  "snoozed_until", "acked_at", "acked_device"}, "for" naming the setting it
  was dealt with for (that field's time and device, "<hlc> <device>"). For
  another setting it counts for nothing, and the reminder rings from its first
  day: a reminder set anew and dealt with elsewhere as it was before rings.
- "text_reminder:<day> <time>": how one written in its text was dealt with,
  {"next_day", "snoozed_until", "acked_at", "acked_device"}. Each device makes
  the reminders written in a text from the text itself.

A field this device has not changed since it was taken in goes as it came.
How one was dealt with goes once it was, or once it is not as it starts.
"""

import re
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, time
from typing import cast

import apsw

from stickle.core.calendars import CALENDARS
from stickle.core.reminders import is_known_zone, is_repeat
from stickle.core.text_reminders import due
from stickle.data import stamps
from stickle.data.reminders import (
    GREGORIAN,
    NOTE,
    RULE_FIELD,
    STATE_FIELD,
    TEXT,
    TEXT_FIELD,
    ReminderRepository,
    text_field,
)
from stickle.data.settings import MORNING, Settings
from stickle.data.stamps import Stamp
from stickle.sync.contents import Contents, Field
from stickle.sync.format import Json, is_id

RULE_KEYS = (
    "calendar",
    "start_day",
    "at_time",
    "time_zone",
    "repeat",
    "weekdays",
    "hide_note",
    "first_day",
)
STATE_KEYS = ("next_day", "snoozed_until", "acked_at", "acked_device")
FOR = "for"
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_TIME = re.compile(r"[0-2][0-9]:[0-5][0-9]")
_WEEKDAYS = re.compile(r"([0-6](,[0-6])*)?")
_DAY_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}(L)?-[0-9]{2}")


def is_reminder_field(name: str) -> bool:
    return name in (RULE_FIELD, STATE_FIELD) or name.startswith(TEXT_FIELD)


def values(connection: apsw.Connection, note_id: str) -> dict[str, Json]:
    """The note's reminders here, as the values of their fields (a state
    without "for", which goes by the setting's stamp)."""
    found: dict[str, Json] = {}
    rows = connection.execute(
        "SELECT kind, calendar, start_day, at_time, time_zone, repeat, weekdays, hide_note,"
        " first_day, next_day, snoozed_until, acked_at, acked_device, deleted_at"
        " FROM reminders WHERE note_id = ? ORDER BY deleted_at IS NULL, updated_at, id",
        (note_id,),
    ).fetchall()
    for row in rows:
        kind, deleted = row[0], row[-1]
        rule = dict(zip(RULE_KEYS, row[1:9], strict=True))
        rule["hide_note"] = bool(rule["hide_note"])
        state = dict(zip(STATE_KEYS, row[9:13], strict=True))
        if kind == NOTE:
            if deleted is None:
                found[RULE_FIELD] = cast(Json, rule)
                found[STATE_FIELD] = cast(Json, state)
            elif RULE_FIELD not in found:
                found[RULE_FIELD] = None
        elif kind == TEXT and deleted is None:
            found[text_field(str(rule["start_day"]), str(rule["at_time"]))] = cast(Json, state)
    if found.get(RULE_FIELD) is None:
        found.pop(STATE_FIELD, None)
    return found


def _as_it_starts(name: str, value: Json) -> bool:
    """How one is dealt with when nothing has been done with it yet."""
    if not isinstance(value, dict):
        return True
    if value.get("snoozed_until") is not None or value.get("acked_at") is not None:
        return False
    return name == STATE_FIELD or value.get("next_day") == name.removeprefix(TEXT_FIELD)[:10]


def fields(
    device: str,
    here: Mapping[str, Json],
    stamped: Mapping[str, Stamp],
    sent: Mapping[str, int],
    before: Contents,
    earliest: Field,
) -> dict[str, Field]:
    """The note's reminder fields as this device has them; before: the file as
    last seen or put; earliest: a field never changed (its time and device)."""
    made: dict[str, Field] = {}
    rule_when, rule_by = stamped.get(RULE_FIELD, (earliest.hlc, None))
    for name, value in here.items():
        when, by = stamped.get(name, (earliest.hlc, None))
        old = before.fields.get(name)
        if old is not None and (old.hlc, old.device) == (when, by or device):
            continue  # as it came
        if (
            old is None
            and name != RULE_FIELD
            and name not in stamped
            and _as_it_starts(name, value)
        ):
            continue  # nothing to tell
        if name == STATE_FIELD and isinstance(value, dict):
            value = {**value, FOR: f"{rule_when.text} {rule_by or device}"}
        made[name] = Field(value, when, by or device, sent=sent.get(name))
    return made


# Taking them in


def _day(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _stamp_or_none(value: object) -> bool:
    return value is None or (isinstance(value, str) and _TIMESTAMP.fullmatch(value) is not None)


def _rule(value: Json) -> dict[str, object] | None:
    """A setting this version can take, or None."""
    if not isinstance(value, dict):
        return None
    calendar, start, at, zone = (value.get(key) for key in RULE_KEYS[:4])
    repeat, weekdays, hide, first = (value.get(key) for key in RULE_KEYS[4:])
    known = calendar == GREGORIAN or calendar in CALENDARS
    if not (isinstance(calendar, str) and known and isinstance(repeat, str) and is_repeat(repeat)):
        return None
    if not (isinstance(start, str) and _DAY_TEXT.fullmatch(start)):
        return None
    if not (isinstance(at, str) and _TIME.fullmatch(at)):
        return None
    if zone is not None and not (isinstance(zone, str) and is_known_zone(zone)):
        return None
    if not (isinstance(weekdays, str) and _WEEKDAYS.fullmatch(weekdays)):
        return None
    if not isinstance(hide, bool) or not (first is None or _day(first)):
        return None
    taken: dict[str, object] = {key: value[key] for key in RULE_KEYS}
    taken["hide_note"] = int(hide)
    return taken


def _state(value: Json) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    day, snoozed, acked, by = (value.get(key) for key in STATE_KEYS)
    if not (day is None or _day(day)) or not _stamp_or_none(snoozed) or not _stamp_or_none(acked):
        return None
    if not (by is None or (isinstance(by, str) and is_id(by))):
        return None
    return dict(zip(STATE_KEYS, (day, snoozed, acked, by), strict=True))


def _of(each: Field, device: str) -> Stamp:
    return (each.hlc, None if each.device == device else each.device)


def _for(stamp: Stamp, device: str) -> str:
    return f"{stamp[0].text} {stamp[1] or device}"


def take(
    connection: apsw.Connection,
    device: str,
    note_id: str,
    contents: Contents,
    here: Mapping[str, Json],
    stamped: Mapping[str, Stamp],
    copy_of: str | None,
) -> bool:
    """The note's reminders as the file has them, here (its text taken in
    already); copy_of: the note it is a conflict copy of, made here just now.
    Whether anything changed."""
    changed = False
    now = stamped.get(RULE_FIELD)
    rule = contents.fields.get(RULE_FIELD)
    if rule is not None and (_of(rule, device) != now or rule.value != here.get(RULE_FIELD)):
        taken = None if rule.value is None else _rule(rule.value)
        if rule.value is None:
            connection.execute(
                "UPDATE reminders SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ'),"
                " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ')"
                " WHERE note_id = ? AND kind = ? AND deleted_at IS NULL",
                (note_id, NOTE),
            )
        if rule.value is None or taken is not None:
            if taken is not None:
                _set_rule(connection, note_id, taken)
            stamps.put(connection, note_id, RULE_FIELD, _of(rule, device), rule.sent)
            now = _of(rule, device)
            changed = True
    state = contents.fields.get(STATE_FIELD)
    if state is not None and _of(state, device) != stamped.get(STATE_FIELD):
        stamps.put(connection, note_id, STATE_FIELD, _of(state, device), state.sent)
        changed = True
    changed |= _deal(connection, note_id, state, None if now is None else _for(now, device))
    body = connection.execute("SELECT body FROM notes WHERE id = ?", (note_id,)).fetchall()
    if body:
        morning = time.fromisoformat(Settings(connection).get(MORNING))
        reminders = ReminderRepository(connection)
        changed |= reminders.match_text(note_id, due(str(body[0][0]), morning).keys(), fresh=False)
        if copy_of is not None:
            changed |= _quiet_in_copy(connection, note_id, copy_of)
    for name, each in sorted(contents.fields.items()):
        taken = _state(each.value) if name.startswith(TEXT_FIELD) else None
        if taken is None:
            continue
        day, _, at = name.removeprefix(TEXT_FIELD).partition(" ")
        connection.execute(
            f"UPDATE reminders SET {_SET_STATE} WHERE note_id = ? AND kind = ? AND start_day = ?"
            f" AND at_time = ? AND deleted_at IS NULL AND NOT ({_SAME_STATE})",
            (*taken.values(), note_id, TEXT, day, at, *taken.values()),  # pyright: ignore[reportArgumentType]
        )
        changed |= bool(connection.changes())
        if _of(each, device) != stamped.get(name):
            stamps.put(connection, note_id, name, _of(each, device), each.sent)
            changed = True
    return changed


_SET_STATE = "next_day = ?, snoozed_until = ?, acked_at = ?, acked_device = ?"
_SAME_STATE = "next_day IS ? AND snoozed_until IS ? AND acked_at IS ? AND acked_device IS ?"


def _set_rule(connection: apsw.Connection, note_id: str, rule: Mapping[str, object]) -> None:
    columns = ", ".join(f"{key} = ?" for key in rule)
    live = connection.execute(
        "SELECT id FROM reminders WHERE note_id = ? AND kind = ? AND deleted_at IS NULL",
        (note_id, NOTE),
    ).fetchall()
    if live:
        connection.execute(
            f"UPDATE reminders SET {columns}, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ')"
            " WHERE id = ?",
            (*rule.values(), live[0][0]),  # pyright: ignore[reportArgumentType]
        )
        return
    names = ", ".join(rule)
    marks = ", ".join("?" for _ in rule)
    connection.execute(
        f"INSERT INTO reminders (id, note_id, kind, {names}, created_at, updated_at)"
        f" VALUES (?, ?, ?, {marks}, strftime('%Y-%m-%dT%H:%M:%fZ'),"
        " strftime('%Y-%m-%dT%H:%M:%fZ'))",
        (str(uuid.uuid4()), note_id, NOTE, *rule.values()),  # pyright: ignore[reportArgumentType]
    )


def _deal(connection: apsw.Connection, note_id: str, state: Field | None, rule: str | None) -> bool:
    """The note's own reminder dealt with as state says, if it is for the
    setting it has now; else as it starts, from its first day. Whether it changed."""
    taken = None
    if state is not None and isinstance(state.value, dict) and state.value.get(FOR) == rule:
        taken = _state(state.value)
    if taken is None:
        connection.execute(
            "UPDATE reminders SET next_day = first_day, snoozed_until = NULL, acked_at = NULL,"
            " acked_device = NULL WHERE note_id = ? AND kind = ? AND deleted_at IS NULL"
            " AND NOT (next_day IS first_day AND snoozed_until IS NULL AND acked_at IS NULL"
            " AND acked_device IS NULL)",
            (note_id, NOTE),
        )
        return bool(connection.changes())
    connection.execute(
        f"UPDATE reminders SET {_SET_STATE} WHERE note_id = ? AND kind = ?"
        f" AND deleted_at IS NULL AND NOT ({_SAME_STATE})",
        (*taken.values(), note_id, NOTE, *taken.values()),  # pyright: ignore[reportArgumentType]
    )
    return bool(connection.changes())


def for_this_setting(merged: Contents, mine: Contents, cloud: Contents) -> Contents:
    """The merged note with, of two ways its own reminder was dealt with, one
    for the setting it has if the later is for another: an earlier setting's,
    dealt with on a device that had not seen it change, does not undo one
    dealt with for the setting as it is. Every device merges it alike."""
    rule, state = merged.fields.get(RULE_FIELD), merged.fields.get(STATE_FIELD)
    if rule is None or state is None:
        return merged
    wanted = f"{rule.hlc.text} {rule.device}"

    def fits(each: Field | None) -> bool:
        return each is not None and isinstance(each.value, dict) and each.value.get(FOR) == wanted

    if fits(state):
        return merged
    for side in (mine, cloud):
        other = side.fields.get(STATE_FIELD)
        if other is not None and fits(other):
            return replace(merged, fields={**merged.fields, STATE_FIELD: other})
    return merged


def _quiet_in_copy(connection: apsw.Connection, note_id: str, original: str) -> bool:
    """A conflict copy's reminders written in its text that its note has too:
    dealt with in the copy, here only (the note rings them)."""
    connection.execute(
        "UPDATE reminders SET next_day = NULL WHERE note_id = ? AND kind = ?"
        " AND deleted_at IS NULL AND next_day IS NOT NULL AND (start_day, at_time) IN"
        " (SELECT start_day, at_time FROM reminders WHERE note_id = ? AND kind = ?"
        " AND deleted_at IS NULL)",
        (note_id, TEXT, original, TEXT),
    )
    return bool(connection.changes())
