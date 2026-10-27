"""Reminders written in a note's text: ⏰ and a day, with a time or not, as in
"- [ ] Send the report ⏰2026-10-20 15:00".

The text is where they are kept; the reminders table follows it each time the
note is saved (stickle.data.reminders). The form is strict, so that ordinary
text never rings by chance: ⏰ then YYYY-MM-DD, then a space and HH:MM (24
hours) or nothing (the morning time). A day picked in another calendar comes
first, in brackets, for people to read: only the day after it is read here.
One a line (the first); on a checked line, or in a code block, it does not
ring. The time is this computer's, wherever the note goes: it is read as it is
written.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time

from stickle.core.markdown import task_box

MARK = "⏰"
# A day, then a time (followed by no more digits) or nothing that could be one
# gone wrong: "⏰2026-10-20 25:00" and "⏰2026-10-20 9:00" are not reminders.
_CODE = re.compile(
    MARK
    + r"(?:\[([^\]\n]{1,80})\] ?)?(\d{4})-(\d{2})-(\d{2})"
    + r"(?: ([01]\d|2[0-3]):([0-5]\d)(?![\d:])|(?![\d:]| \d))"
)
_FENCE = re.compile(r" {0,3}(```|~~~)")


@dataclass(frozen=True)
class Code:
    """A reminder as written: where in its line or piece of text, and when."""

    start: int
    end: int
    day: date
    at: time | None  # None: a day only, which rings in the morning
    note: str | None  # what the brackets say, if any (another calendar's day)

    def when(self, morning: time) -> datetime:
        """When it rings, as a time on this computer's clock."""
        return datetime.combine(self.day, self.at or morning)


@dataclass(frozen=True)
class Written:
    """The reminder written on a line of the text."""

    line: int
    code: Code
    checked: bool  # on a checked line: it does not ring


def codes(text: str) -> list[Code]:
    """The reminders written in a line or piece of text: those whose day exists."""
    found: list[Code] = []
    for match in _CODE.finditer(text):
        try:
            day = date(int(match[2]), int(match[3]), int(match[4]))
        except ValueError:
            continue
        at = None if match[5] is None else time(int(match[5]), int(match[6]))
        found.append(Code(match.start(), match.end(), day, at, match[1]))
    return found


def written(text: str) -> list[Written]:
    """The reminders written in text, top to bottom: one a line."""
    found: list[Written] = []
    fenced = False
    for number, line in enumerate(text.split("\n")):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced or MARK not in line:
            continue
        on_line = codes(line)
        if not on_line:
            continue
        box = task_box(line)
        found.append(Written(number, on_line[0], box is not None and line[box[0]] in "xX"))
    return found


def due(text: str, morning: time) -> dict[datetime, list[int]]:
    """When the text's reminders ring, each with its lines: those on lines not
    checked, the same time once (one reminder, however many lines say it)."""
    times: dict[datetime, list[int]] = {}
    for reminder in written(text):
        if not reminder.checked:
            times.setdefault(reminder.code.when(morning), []).append(reminder.line)
    return times


def code(when: datetime) -> str:
    """How a reminder at when is written."""
    return f"{MARK}{when:%Y-%m-%d %H:%M}"


def with_code(line: str, when: datetime) -> str:
    """The line with a reminder at when: in place of the one it has, or at its end."""
    on_line = codes(line)
    if on_line:
        first = on_line[0]
        return line[: first.start] + code(when) + line[first.end :]
    stripped = line.rstrip()
    return f"{stripped} {code(when)}" if stripped else code(when)
