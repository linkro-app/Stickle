"""Settings, in two kinds.

Device settings belong to this computer only (autostart, ...; the language,
needed before the database opens, is in stickle.data.startup).
Shared settings will follow the user to every device once sync exists
(default colour, categories, ...); each records when and on which device it
last changed, so sync can later pick the newest per setting.

Every setting is declared once here, with its kind, default and allowed
values; reads and writes are checked against that declaration.
"""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeGuard, cast

import apsw

from stickle.core.clock import Clock, utc_now
from stickle.core.colors import is_hex
from stickle.core.note import DEFAULT_COLOR
from stickle.core.updates import version_key

type Scope = Literal["device", "shared"]


@dataclass(frozen=True)
class Setting[T]:
    key: str
    scope: Scope
    default: T
    check: Callable[[object], TypeGuard[T]]


class InvalidSettingError(ValueError):
    pass


def _flag(value: object) -> TypeGuard[bool]:
    return isinstance(value, bool)


THIS_DEVICE = "this_device"
SEVERAL_DEVICES = "several_devices"


def _usage(value: object) -> TypeGuard[str | None]:
    return value in (None, THIS_DEVICE, SEVERAL_DEVICES)


def _color_key(value: object) -> TypeGuard[str]:
    """A palette key (one a newer version added, too) or a colour of the user's own."""
    return isinstance(value, str) and (value.isidentifier() or is_hex(value))


def _size_or_none(value: object) -> TypeGuard[list[int] | None]:
    """[width, height] in pixels, within what any screen could show."""
    if value is None:
        return True
    if not isinstance(value, list) or len(cast(list[object], value)) != 2:
        return False
    return all(
        isinstance(n, int) and not isinstance(n, bool) and 100 <= n <= 20000
        for n in cast(list[object], value)
    )


SHORTCUT_ACTIONS = ("new-note", "show", "hide-all", "next-category", "find")


def _shortcuts(value: object) -> TypeGuard[dict[str, str]]:
    """Action: its key combination as Qt writes it ("Ctrl+Alt+N"), "" for none."""
    if not isinstance(value, dict):
        return False
    items = cast(dict[object, object], value).items()
    return all(
        key in SHORTCUT_ACTIONS and isinstance(text, str) and len(text) <= 40 for key, text in items
    )


def _uuid_or_none(value: object) -> TypeGuard[str | None]:
    if value is None:
        return True
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


# The interface language is not here: it is needed before the database is
# open (see stickle.data.startup).
DEVICE_ID = Setting[str | None]("device_id", "device", None, _uuid_or_none)
DEFAULT_NOTE_COLOR = Setting[str]("default_color", "shared", DEFAULT_COLOR, _color_key)
# Asked once whether to add the AppImage to the application list (whatever the answer).
APP_MENU_ASKED = Setting[bool]("app_menu_asked", "device", False, _flag)
# Asked at first start: notes on this computer only, or on several (sync comes later).
USAGE = Setting[str | None]("usage", "device", None, _usage)
# The user said they kept the recovery key (Done, not Later).
RECOVERY_KEY_KEPT = Setting[bool]("recovery_key_kept", "device", False, _flag)
# Skipped at first start, the recovery key is offered once more, never again after.
RECOVERY_KEY_OFFERED_AGAIN = Setting[bool]("recovery_key_offered_again", "device", False, _flag)
# The Stickle window's size as the user left it (screens differ between devices).
LIST_WINDOW_SIZE = Setting[list[int] | None]("list_window_size", "device", None, _size_or_none)
# Shortcuts the user changed on this computer (keyboards and other apps differ).
SHORTCUTS = Setting[dict[str, str]]("shortcuts", "device", {}, _shortcuts)


def _stamp_or_none(value: object) -> TypeGuard[str | None]:
    """A time as stickle.core.clock writes it, or None."""
    return value is None or (
        isinstance(value, str) and len(value) == 24 and value[10] == "T" and value[-1] == "Z"
    )


# The daily check for a newer Stickle, on by default; off, nothing is ever sent.
UPDATE_CHECK = Setting[bool]("update_check", "device", True, _flag)
# When it last asked, so that a restart does not ask again the same day.
UPDATE_CHECKED_AT = Setting[str | None]("update_checked_at", "device", None, _stamp_or_none)


def _version_or_none(value: object) -> TypeGuard[str | None]:
    return value is None or (isinstance(value, str) and version_key(value) is not None)


# The version whose "what's new" note was last given. Shared: seen on one device,
# it does not come up again on another.
NEWS_SEEN = Setting[str | None]("news_seen", "shared", None, _version_or_none)
# Notes line up with the notes and screen edges near them when moved or resized.
MAGNET = Setting[bool]("magnet", "shared", True, _flag)


def _minutes(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 60


def _color_or_none(value: object) -> TypeGuard[str | None]:
    return value is None or _color_key(value)


# New notes take a colour at random (from the palette and the user's own),
# never the one used just before, which is kept too.
RANDOM_COLOR = Setting[bool]("random_color", "shared", False, _flag)
LAST_USED_COLOR = Setting[str | None]("last_used_color", "shared", None, _color_or_none)

# An open secret note locks again once left alone this long (minutes).
RELOCK_MINUTES = Setting[int]("relock_minutes", "shared", 5, _minutes)
# It also locks as soon as another window is used, if chosen.
LOCK_ON_LEAVE = Setting[bool]("lock_on_leave", "shared", False, _flag)


def _time_of_day(value: object) -> TypeGuard[str]:
    """Hours and minutes, as "09:00"."""
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    hours, minutes = value[:2], value[3:]
    return hours.isdigit() and minutes.isdigit() and int(hours) < 24 and int(minutes) < 60


SNOOZE_CHOICES = (5, 10, 30, 60)


def _snooze(value: object) -> TypeGuard[int]:
    return not isinstance(value, bool) and value in SNOOZE_CHOICES


# Reminders: the morning ("tomorrow morning", and a reminder with a date only),
# and how long "remind me again" waits unless another time is chosen (minutes).
MORNING = Setting[str]("morning", "shared", "09:00", _time_of_day)
SNOOZE_MINUTES = Setting[int]("snooze_minutes", "shared", 10, _snooze)
RECENT_CALENDARS_KEPT = 3


def _calendar_ids(value: object) -> TypeGuard[list[str]]:
    # Ids this version may not know are kept: a later one put them there.
    return (
        isinstance(value, list)
        and len(cast(list[object], value)) <= RECENT_CALENDARS_KEPT
        and all(isinstance(item, str) and item for item in cast(list[object], value))
    )


# The calendars other than the Gregorian picked lately for reminders, the latest
# first: offered first, below the Gregorian one.
RECENT_CALENDARS = Setting[list[str]]("recent_calendars", "shared", [], _calendar_ids)

CHANGE_KINDS = (
    "recovery_key",
    "master_password_set",
    "master_password_changed",
    "secret_password_changed",
    "secret_password_reset",  # set anew with the recovery key, the old one forgotten
)


def _protection_changes(value: object) -> TypeGuard[list[dict[str, object]]]:
    """Each {"what": a kind, "at": a time, "told": whether it is to be told now}."""
    if not isinstance(value, list):
        return False
    for entry in cast(list[object], value):
        if not isinstance(entry, dict):
            return False
        fields = cast(dict[str, object], entry)
        if (
            set(fields) != {"what", "at", "told"}
            or fields["what"] not in CHANGE_KINDS
            or not _stamp_or_none(fields["at"])
            or fields["at"] is None
            or not isinstance(fields["told"], bool)
        ):
            return False
    return True


# What protects the notes changed on this computer (a new recovery key, the
# master password), told to its user later: at the next start, or once the
# computer is unlocked, in case someone else made the change at it.
PROTECTION_CHANGES = Setting[list[dict[str, object]]](
    "protection_changes", "device", [], _protection_changes
)

SETTINGS: dict[str, Setting[object]] = {
    s.key: s  # pyright: ignore[reportAssignmentType]
    for s in (
        DEVICE_ID,
        DEFAULT_NOTE_COLOR,
        APP_MENU_ASKED,
        USAGE,
        RECOVERY_KEY_KEPT,
        RECOVERY_KEY_OFFERED_AGAIN,
        LIST_WINDOW_SIZE,
        SHORTCUTS,
        UPDATE_CHECK,
        UPDATE_CHECKED_AT,
        NEWS_SEEN,
        MAGNET,
        RELOCK_MINUTES,
        LOCK_ON_LEAVE,
        RANDOM_COLOR,
        LAST_USED_COLOR,
        PROTECTION_CHANGES,
        MORNING,
        SNOOZE_MINUTES,
    )
}


class Settings:
    def __init__(self, connection: apsw.Connection, clock: Clock = utc_now) -> None:
        self._db = connection
        self._clock = clock

    def get[T](self, setting: Setting[T]) -> T:
        table = "device_settings" if setting.scope == "device" else "shared_settings"
        rows = self._db.execute(
            f"SELECT value FROM {table} WHERE key = ?", (setting.key,)
        ).fetchall()
        if not rows:
            return setting.default
        value: object = json.loads(str(rows[0][0]))
        # A value that no longer fits (edited by hand, older app) falls back to the default.
        return value if setting.check(value) else setting.default

    def set[T](self, setting: Setting[T], value: T) -> None:
        if not setting.check(value):
            raise InvalidSettingError(f"{setting.key}: {value!r}")
        encoded = json.dumps(value)
        with self._db:
            if setting.scope == "device":
                self._db.execute(
                    "INSERT INTO device_settings (key, value) VALUES (?, ?)"
                    " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (setting.key, encoded),
                )
            else:
                self._db.execute(
                    "INSERT INTO shared_settings (key, value, updated_at, updated_by)"
                    " VALUES (?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET"
                    " value = excluded.value, updated_at = excluded.updated_at,"
                    " updated_by = excluded.updated_by",
                    (setting.key, encoded, self._clock(), self.device_id()),
                )

    def device_id(self) -> str:
        """This device's identity, created on first use."""
        current = self.get(DEVICE_ID)
        if current is None:
            current = str(uuid.uuid4())
            self.set(DEVICE_ID, current)
        return current
