"""Notes that line up: where a note dropped near another, or near the edge of the
screen, settles, and which notes touch one another, to move together.

Only geometry, in screen pixels: what a note is next to is worked out when it
is needed and never stored.
"""

from collections.abc import Mapping, Sequence

from stickle.core.layout import Rect

SNAP_DISTANCE = 12  # dropped this close to an edge, a note goes to it
GAP = 6  # between notes side by side: their rounded corners do not touch
TOUCHING = GAP + SNAP_DISTANCE  # this close, two notes count as together


def _nearest(value: int, targets: Sequence[int]) -> int | None:
    """The target within SNAP_DISTANCE of value that is nearest to it, if any."""
    close = [t for t in targets if abs(t - value) <= SNAP_DISTANCE]
    return min(close, key=lambda t: abs(t - value)) if close else None


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Whether two spans share more than a few pixels (side by side, not corner to corner)."""
    return min(a_end, b_end) - max(a_start, b_start) > 0


def snapped(window: Rect, others: Sequence[Rect], areas: Sequence[Rect]) -> Rect:
    """Where window settles when dropped: beside or in line with a note near it
    (GAP apart), or against an edge of the area it is on. Unchanged when nothing
    is near."""
    xs: list[int] = []  # where the left edge could go
    ys: list[int] = []  # where the top edge could go
    for other in others:
        near_vertically = _overlap(
            window.y, window.bottom, other.y - TOUCHING, other.bottom + TOUCHING
        )
        near_horizontally = _overlap(
            window.x, window.right, other.x - TOUCHING, other.right + TOUCHING
        )
        if near_vertically:
            # Side by side: just left or right of it, tops or bottoms in line.
            xs += [other.x - GAP - window.width, other.right + GAP]
            ys += [other.y, other.bottom - window.height]
        if near_horizontally:
            # One above the other: just above or below it, left or right sides in line.
            ys += [other.y - GAP - window.height, other.bottom + GAP]
            xs += [other.x, other.right - window.width]
    for area in areas:
        if _overlap(window.x, window.right, area.x, area.right) and _overlap(
            window.y, window.bottom, area.y, area.bottom
        ):
            xs += [area.x, area.right - window.width]
            ys += [area.y, area.bottom - window.height]
    x = _nearest(window.x, xs)
    y = _nearest(window.y, ys)
    return Rect(
        window.x if x is None else x, window.y if y is None else y, window.width, window.height
    )


def resized(
    window: Rect, others: Sequence[Rect], areas: Sequence[Rect], height_too: bool = True
) -> Rect:
    """The size window settles at when let go after resizing (by its bottom right
    corner, so only the right and bottom edges moved): each edge near the same
    edge of a note in line with it, near the gap before a note beside or below
    it, or near an edge of the area it is on, goes there. height_too False keeps
    the height (a folded note)."""
    rights: list[int] = []
    bottoms: list[int] = []
    for other in others:
        near_vertically = _overlap(
            window.y, window.bottom, other.y - TOUCHING, other.bottom + TOUCHING
        )
        near_horizontally = _overlap(
            window.x, window.right, other.x - TOUCHING, other.right + TOUCHING
        )
        if near_vertically:
            # Side by side: bottoms in line, or up to the note on its right.
            bottoms.append(other.bottom)
            if other.x > window.x:
                rights.append(other.x - GAP)
        if near_horizontally:
            # One above the other: right sides in line (the same width when the
            # left sides are), or down to the note below.
            rights.append(other.right)
            if other.y > window.y:
                bottoms.append(other.y - GAP)
    for area in areas:
        if _overlap(window.x, window.right, area.x, area.right) and _overlap(
            window.y, window.bottom, area.y, area.bottom
        ):
            rights.append(area.right)
            bottoms.append(area.bottom)
    right = _nearest(window.right, rights)
    bottom = _nearest(window.bottom, bottoms) if height_too else None
    return Rect(
        window.x,
        window.y,
        window.width if right is None else right - window.x,
        window.height if bottom is None else bottom - window.y,
    )


def touching(a: Rect, b: Rect) -> bool:
    """Whether two notes are side by side or one above the other, at most
    TOUCHING apart (as snapping leaves them), or overlap."""
    apart_x = max(a.x - b.right, b.x - a.right)
    apart_y = max(a.y - b.bottom, b.y - a.bottom)
    side_by_side = apart_x <= TOUCHING and apart_y < 0
    stacked = apart_y <= TOUCHING and apart_x < 0
    return side_by_side or stacked


def together[K](start: K, notes: Mapping[K, Rect]) -> set[K]:
    """The notes that move with start: those touching it, and those touching
    them in turn."""
    found = {start}
    waiting = [start]
    while waiting:
        current = notes[waiting.pop()]
        for key, other in notes.items():
            if key not in found and touching(current, other):
                found.add(key)
                waiting.append(key)
    return found
