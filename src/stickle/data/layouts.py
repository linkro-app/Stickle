"""Where each note was on screen, in the note_layouts table, and its size, in
note_sizes.

A note has a row for each number of monitors it was moved with (see
stickle.core.layout): config_key holds that number, and monitor_key, as
JSON, the monitor's number and name and the names of the monitors connected
then. Its size is one row of its own. A row changes, and its time with it,
only when what it holds does: moving a note leaves its size as it was, and
resizing it its places. Rows that do not make sense (edited by hand, written
by a broken version, a kind a later version added) are ignored on reading.

Each is a field sync takes to the other devices (stickle.sync.local_layouts):
"place:<n>" and "size", stamped when they change here (stickle.data.stamps),
the note counting as changed then (its change number, not when it was last
changed as shown). A place the app only gave a note never placed, so that it
stays where it was put at the next start, is kept unstamped (arrange): never
changed here, it goes nowhere, and any other device's place comes in its
stead. Which places another device set is known by their stamps (elsewhere):
those are found by their monitors' numbers alone.
"""

import json
from collections.abc import Callable
from typing import cast

import apsw

from stickle.core.clock import Clock, millis_at, utc_now
from stickle.core.layout import Place, Size
from stickle.data import stamps

PLACE = "place:"  # a place's field: "place:2" for two monitors
SIZE = "size"


def place_field(count: int) -> str:
    return f"{PLACE}{count}"


def count_of(field: str) -> int | None:
    """The number of monitors a place's field is for, written as such ("2", not
    "02"); None for another field."""
    if not field.startswith(PLACE):
        return None
    count = _count(field.removeprefix(PLACE))
    return count if count is not None and field == place_field(count) else None


def _monitor_key(place: Place) -> str:
    return json.dumps(
        {"number": place.monitor, "name": place.name, "setup": list(place.setup)},
        ensure_ascii=False,
    )


def _count(config_key: object) -> int | None:
    """The number of monitors a row is for; None for a kind of row not known here."""
    text = str(config_key)
    return int(text) if text.isascii() and text.isdigit() and int(text) >= 1 else None


def _place(monitor_key: object, rel_x: object, rel_y: object) -> Place | None:
    try:
        decoded: object = json.loads(str(monitor_key))
    except ValueError:
        return None
    if not isinstance(decoded, dict):
        return None
    monitor = cast(dict[str, object], decoded)
    number, name, setup = monitor.get("number"), monitor.get("name"), monitor.get("setup")
    if (
        not isinstance(number, int)
        or isinstance(number, bool)
        or not isinstance(name, str)
        or not isinstance(setup, list)
    ):
        return None
    names = [item for item in cast(list[object], setup) if isinstance(item, str)]
    if len(names) != len(cast(list[object], setup)) or not all(
        isinstance(value, int | float) for value in (rel_x, rel_y)
    ):
        return None
    place = Place(
        monitor=number,
        name=name,
        rel_x=float(cast(float, rel_x)),
        rel_y=float(cast(float, rel_y)),
        setup=tuple(names),
    )
    return place if place.is_valid() else None


_UPSERT_SIZE = (
    "INSERT INTO note_sizes (note_id, width, height, updated_at) VALUES (?, ?, ?, ?)"
    " ON CONFLICT (note_id) DO UPDATE SET width = excluded.width,"
    " height = excluded.height, updated_at = excluded.updated_at"
    " WHERE width IS NOT excluded.width OR height IS NOT excluded.height"
)
_UPSERT_PLACE = (
    "INSERT INTO note_layouts (note_id, config_key, monitor_key, rel_x, rel_y,"
    " updated_at) VALUES (?, ?, ?, ?, ?, ?)"
    " ON CONFLICT (note_id, config_key) DO UPDATE SET"
    " monitor_key = excluded.monitor_key, rel_x = excluded.rel_x,"
    " rel_y = excluded.rel_y, updated_at = excluded.updated_at"
    " WHERE monitor_key IS NOT excluded.monitor_key OR rel_x IS NOT excluded.rel_x"
    " OR rel_y IS NOT excluded.rel_y"
)


class LayoutRepository:
    def __init__(
        self,
        connection: apsw.Connection,
        clock: Clock = utc_now,
        millis: Callable[[], int] | None = None,
    ) -> None:
        """millis: the time a place or size changed is stamped with (as notes
        are: the cloud's, while syncing); by default, the clock's."""
        self._db = connection
        self._clock = clock
        self._millis = millis or (lambda: millis_at(self._clock()))

    def places(self, note_id: str) -> dict[int, Place]:
        """The note's places, by the number of monitors each is for."""
        rows = self._db.execute(
            "SELECT config_key, monitor_key, rel_x, rel_y FROM note_layouts WHERE note_id = ?",
            (note_id,),
        ).fetchall()
        found: dict[int, Place] = {}
        for config_key, *values in rows:
            count = _count(config_key)
            if count is not None and (place := _place(*values)) is not None:
                found[count] = place
        return found

    def elsewhere(self, note_id: str) -> set[int]:
        """The numbers of monitors whose place for the note another device set."""
        rows = self._db.execute(
            "SELECT field FROM note_stamps WHERE note_id = ? AND device IS NOT NULL"
            " AND field LIKE 'place:%'",
            (note_id,),
        )
        return {count for (field,) in rows if (count := count_of(str(field))) is not None}

    def size(self, note_id: str) -> Size | None:
        rows = self._db.execute(
            "SELECT width, height FROM note_sizes WHERE note_id = ?", (note_id,)
        ).fetchall()
        if not rows:
            return None
        width, height = rows[0]
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (width, height)):
            return None
        size = Size(cast(int, width), cast(int, height))
        return size if size.is_valid() else None

    def save(self, note_id: str, size: Size, placed: tuple[int, Place] | None = None) -> None:
        """The note's size and, if given, its place for that number of monitors:
        each a change for sync, if it is one."""
        now = self._clock()
        changed: list[str] = []
        with self._db:
            self._db.execute(_UPSERT_SIZE, (note_id, size.width, size.height, now))
            if self._db.changes():
                changed.append(SIZE)
            if placed is not None:
                count, place = placed
                values = (str(count), _monitor_key(place), place.rel_x, place.rel_y, now)
                self._db.execute(_UPSERT_PLACE, (note_id, *values))
                if self._db.changes():
                    changed.append(place_field(count))
            if changed:
                stamps.changed(self._db, note_id, changed, self._millis())
                stamps.touch(self._db, note_id)

    def arrange(self, note_id: str, count: int, place: Place, size: Size) -> None:
        """A note never placed, put at place with that many monitors: kept there,
        and its size, where it has none, as never changed here (nothing for sync)."""
        now = self._clock()
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO note_sizes (note_id, width, height, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (note_id, size.width, size.height, now),
            )
            self._db.execute(
                "INSERT OR IGNORE INTO note_layouts (note_id, config_key, monitor_key, rel_x,"
                " rel_y, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (note_id, str(count), _monitor_key(place), place.rel_x, place.rel_y, now),
            )

    def take_place(self, note_id: str, count: int, place: Place) -> None:
        """A place as sync brought it (the caller stamps it)."""
        values = (str(count), _monitor_key(place), place.rel_x, place.rel_y, self._clock())
        self._db.execute(_UPSERT_PLACE, (note_id, *values))

    def take_size(self, note_id: str, size: Size) -> None:
        """A size as sync brought it (the caller stamps it)."""
        self._db.execute(_UPSERT_SIZE, (note_id, size.width, size.height, self._clock()))
