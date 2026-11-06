"""Where a note goes on screen, remembered per monitor rather than per desktop.

Monitors are numbered: the main (primary) monitor is 1, the others follow
from left to right, then top to bottom, so the numbers stay put as long as
the monitors stay where they are. A place is the number of a note's
monitor, that monitor's name as a hint (a model or connector name, never a
serial number), and where the note sits on it as fractions of the monitor,
so a change of resolution or scaling leaves it in the same place.

A note has a place for each number of monitors it was moved with: one for a
laptop's screen alone, another for it with a monitor plugged in. Moving it
changes only the place for as many monitors as there are now, and computers
with as many monitors share a place. With no place yet for that many, the
one for the nearest number (the larger, between two) shows it until it is
moved. Its size is one, whatever the monitors. Whatever is remembered, a
note is always put entirely inside a monitor's work area.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

MIN_WIDTH = 120
MIN_HEIGHT = 60
MAX_SIZE = 20_000
TITLE_BAR = 28  # where a note is held: which monitor it is on follows this


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom

    def distance(self, x: float, y: float) -> float:
        dx = max(self.x - x, 0, x - self.right)
        dy = max(self.y - y, 0, y - self.bottom)
        return math.hypot(dx, dy)


@dataclass(frozen=True)
class Monitor:
    name: str  # a hint to find the same monitor again; may be empty
    geometry: Rect
    available: Rect  # without taskbars and docks
    primary: bool = False


@dataclass(frozen=True)
class Place:
    """Where a note sits: on monitor number `monitor`, as fractions of it."""

    monitor: int
    name: str
    rel_x: float
    rel_y: float
    # The names of every monitor connected at the time: seeing any of them
    # again means the same computer, where the note's own monitor is missing.
    setup: tuple[str, ...] = ()

    def is_valid(self) -> bool:
        return self.monitor >= 1 and all(
            math.isfinite(v) and -1 <= v <= 2 for v in (self.rel_x, self.rel_y)
        )


@dataclass(frozen=True)
class Size:
    width: int
    height: int

    def is_valid(self) -> bool:
        return MIN_WIDTH <= self.width <= MAX_SIZE and MIN_HEIGHT <= self.height <= MAX_SIZE


def size_of(window: Rect) -> Size:
    return Size(window.width, window.height)


def numbered(monitors: Sequence[Monitor]) -> list[Monitor]:
    """Monitors in number order: the main one first, then left to right, top to bottom."""
    primary = [m for m in monitors if m.primary][:1] or list(monitors[:1])
    others = sorted(
        (m for m in monitors if m not in primary), key=lambda m: (m.geometry.x, m.geometry.y)
    )
    return primary + others


def monitor_at(window: Rect, monitors: Sequence[Monitor]) -> tuple[int, Monitor]:
    """The monitor a note is on (by its title bar), with its number."""
    ordered = numbered(monitors)
    x, y = window.x + window.width / 2, window.y + min(TITLE_BAR, window.height) / 2
    for number, monitor in enumerate(ordered, 1):
        if monitor.geometry.contains(x, y):
            return number, monitor
    number, monitor = min(enumerate(ordered, 1), key=lambda item: item[1].geometry.distance(x, y))
    return number, monitor


def place_of(window: Rect, monitors: Sequence[Monitor]) -> Place:
    number, monitor = monitor_at(window, monitors)
    area = monitor.geometry
    return Place(
        monitor=number,
        name=monitor.name,
        rel_x=(window.x - area.x) / area.width,
        rel_y=(window.y - area.y) / area.height,
        setup=tuple(m.name for m in numbered(monitors)),
    )


def find_monitor(place: Place, monitors: Sequence[Monitor], same_count: bool) -> Monitor | None:
    """The monitor a place belongs to, or None if it is not connected.

    By name when exactly one connected monitor has it. same_count: the place
    is for as many monitors as there are now, so another monitor stands at its
    number (one at work, another at home; the same one on another connector):
    by number. Otherwise, on the same computer (a monitor of the remembered
    setup is here) a name not found means its monitor is missing: going by
    number would put it on whichever monitor moved up into that number. On
    another computer, by number.
    """
    ordered = numbered(monitors)
    named = [m for m in ordered if place.name and m.name == place.name]
    if len(named) == 1:
        return named[0]
    same_computer = any(m.name and m.name in place.setup for m in ordered)
    if not same_count and same_computer and place.name and not named:
        return None
    if 1 <= place.monitor <= len(ordered):
        return ordered[place.monitor - 1]
    return None


def fit(window: Rect, area: Rect) -> Rect:
    """window moved (and shrunk if need be) to lie entirely inside area."""
    width = max(min(window.width, area.width), min(MIN_WIDTH, area.width))
    height = max(min(window.height, area.height), min(MIN_HEIGHT, area.height))
    x = min(max(window.x, area.x), area.right - width)
    y = min(max(window.y, area.y), area.bottom - height)
    return Rect(x, y, width, height)


def on_monitor(place: Place, size: Size, monitor: Monitor) -> Rect:
    area = monitor.geometry
    window = Rect(
        area.x + round(place.rel_x * area.width),
        area.y + round(place.rel_y * area.height),
        size.width,
        size.height,
    )
    return fit(window, monitor.available)


def chosen(places: Mapping[int, Place], count: int) -> tuple[int, Place] | None:
    """The place shown with count monitors, and the number it is for: its own,
    else the nearest number's (the larger, between two); None without any."""
    if not places:
        return None
    number = min(places, key=lambda n: (abs(n - count), -n))
    return number, places[number]


def restore(places: Mapping[int, Place], size: Size, monitors: Sequence[Monitor]) -> Rect | None:
    """Where to show a note of that size now, or None if no place is remembered.
    A place whose monitor is missing shows it on monitor 1, as far in."""
    picked = chosen(places, len(monitors))
    if picked is None:
        return None
    count, place = picked
    monitor = find_monitor(place, monitors, same_count=count == len(monitors))
    return on_monitor(place, size, monitor or numbered(monitors)[0])


def remember(
    places: Mapping[int, Place], window: Rect, monitors: Sequence[Monitor]
) -> dict[int, Place]:
    """The places after the note was moved to window: only the one for as many
    monitors as there are now changes."""
    return {**places, len(monitors): place_of(window, monitors)}
