"""The colours the user added to the palette ("my colours").

Each is a row of its own, so that colours added on two devices are both kept.
A removed colour keeps its row, marked deleted. At most MAX_OWN_COLORS are
shown, in their order; the same colour twice shows once. Notes store the
colour itself, so nothing here changes how a note looks. Each change stamps
the fields it changes (stickle.data.record_stamps), for sync.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

import apsw

from stickle.core.clock import Clock, utc_now
from stickle.core.colors import is_hex
from stickle.core.hlc import wall_millis
from stickle.data import record_stamps
from stickle.data.record_stamps import COLOR

MAX_OWN_COLORS = 12


class OwnColorsFullError(ValueError):
    """MAX_OWN_COLORS are shown already."""


@dataclass(frozen=True)
class OwnColor:
    id: str
    hex: str  # #rrggbb, lower case
    position: int


class ColorRepository:
    def __init__(
        self,
        connection: apsw.Connection,
        clock: Clock = utc_now,
        millis: Callable[[], int] = wall_millis,
    ) -> None:
        """millis: the time fields are stamped with (as NoteRepository's)."""
        self._db = connection
        self._clock = clock
        self._millis = millis

    def _live(self) -> list[OwnColor]:
        rows = self._db.execute(
            "SELECT id, hex, position FROM custom_colors"
            " WHERE deleted_at IS NULL ORDER BY position, id"
        )
        return [
            OwnColor(str(i), str(h), int(p))  # pyright: ignore[reportArgumentType]
            for i, h, p in rows
            if is_hex(str(h))  # one written wrong elsewhere is left out, not shown
        ]

    def colors(self) -> list[OwnColor]:
        """Those shown, in their order: each colour once, MAX_OWN_COLORS at most."""
        shown: list[OwnColor] = []
        for color in self._live():
            if color.hex not in {c.hex for c in shown}:
                shown.append(color)
        return shown[:MAX_OWN_COLORS]

    def add(self, hex_value: str) -> OwnColor:
        """Last in the order; the one there already, for a colour shown already.
        OwnColorsFullError when MAX_OWN_COLORS are shown."""
        if not is_hex(hex_value):
            raise ValueError(f"colour {hex_value}")
        shown = self.colors()
        existing = next((c for c in shown if c.hex == hex_value), None)
        if existing is not None:
            return existing
        if len(shown) >= MAX_OWN_COLORS:
            raise OwnColorsFullError(hex_value)
        color_id = str(uuid.uuid4())
        now = self._clock()
        with self._db:
            self._db.execute(
                "INSERT INTO custom_colors (id, hex, position, created_at, updated_at) VALUES"
                " (?, ?, (SELECT coalesce(max(position), 0) + 1 FROM custom_colors), ?, ?)",
                (color_id, hex_value, now, now),
            )
            record_stamps.touched(self._db, COLOR, color_id)
        return next(c for c in self._live() if c.id == color_id)

    def remove(self, color_id: str) -> None:
        """Out of the palette; notes of the colour stay as they are."""
        now = self._clock()
        with self._db:
            self._db.execute(
                "UPDATE custom_colors SET deleted_at = ?, updated_at = ? WHERE id = ?",
                (now, now, color_id),
            )
            record_stamps.changed(self._db, COLOR, color_id, ["deleted_at"], self._millis())

    def move(self, color_id: str, step: int) -> list[OwnColor]:
        """Earlier (step -1) or later (1) in the order; the colours as they then
        are. At either end it stays where it is."""
        order = self.colors()
        index = next((i for i, c in enumerate(order) if c.id == color_id), None)
        if index is None:
            raise KeyError(color_id)
        target = index + step
        if not 0 <= target < len(order):
            return order
        order.insert(target, order.pop(index))
        now = self._clock()
        with self._db:
            for position, color in enumerate(order, start=1):
                if color.position != position:
                    self._db.execute(
                        "UPDATE custom_colors SET position = ?, updated_at = ? WHERE id = ?",
                        (position, now, color.id),
                    )
                    record_stamps.changed(self._db, COLOR, color.id, ["position"], self._millis())
        return self.colors()
