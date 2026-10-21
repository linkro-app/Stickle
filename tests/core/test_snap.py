from hypothesis import given
from hypothesis import strategies as st

from stickle.core.layout import Rect
from stickle.core.snap import GAP, SNAP_DISTANCE, resized, snapped, together

SCREEN = Rect(0, 0, 1920, 1040)
A = Rect(100, 100, 260, 300)


def test_a_note_dropped_near_another_goes_beside_it_tops_in_line() -> None:
    dropped = Rect(A.right + 10, A.y + 5, 260, 200)
    assert snapped(dropped, [A], [SCREEN]) == Rect(A.right + GAP, A.y, 260, 200)


def test_on_the_left_and_below_too() -> None:
    left = Rect(A.x - 260 - 2, A.y + 40, 260, 200)
    assert snapped(left, [A], [SCREEN]).x == A.x - GAP - 260
    below = Rect(A.x + 4, A.bottom + 15, 200, 100)
    assert snapped(below, [A], [SCREEN]) == Rect(A.x, A.bottom + GAP, 200, 100)


def test_a_note_dropped_near_the_edge_of_the_screen_goes_to_it() -> None:
    dropped = Rect(8, 500, 260, 200)
    assert snapped(dropped, [], [SCREEN]) == Rect(0, 500, 260, 200)
    corner = Rect(SCREEN.right - 260 - 5, SCREEN.bottom - 200 - 9, 260, 200)
    assert snapped(corner, [], [SCREEN]) == Rect(SCREEN.right - 260, SCREEN.bottom - 200, 260, 200)


def test_far_from_everything_it_stays() -> None:
    dropped = Rect(800, 500, 260, 200)
    assert snapped(dropped, [A], [SCREEN]) == dropped


def test_a_note_far_below_is_not_pulled_sideways() -> None:
    # In line with A's right edge, but 400 px below it: not near it at all.
    dropped = Rect(A.right + 8, A.bottom + 400, 260, 200)
    assert snapped(dropped, [A], [SCREEN]).x == dropped.x


def test_notes_side_by_side_move_together_and_on_down_the_chain() -> None:
    b = Rect(A.right + GAP, A.y, 260, 300)
    c = Rect(b.x, b.bottom + GAP, 260, 100)
    d = Rect(1500, 700, 260, 200)  # on its own
    notes = {"a": A, "b": b, "c": c, "d": d}
    assert together("a", notes) == {"a", "b", "c"}
    assert together("d", notes) == {"d"}


def test_corner_to_corner_is_not_together() -> None:
    diagonal = Rect(A.right + GAP, A.bottom + GAP, 100, 100)
    assert together("a", {"a": A, "x": diagonal}) == {"a"}


rects = st.builds(
    Rect,
    st.integers(-200, 2000),
    st.integers(-200, 1200),
    st.integers(60, 600),
    st.integers(22, 600),
)


@given(rects, st.lists(rects, max_size=6))
def test_snapping_moves_a_note_little_and_keeps_its_size(window: Rect, others: list[Rect]) -> None:
    result = snapped(window, others, [SCREEN])
    assert (result.width, result.height) == (window.width, window.height)
    assert abs(result.x - window.x) <= SNAP_DISTANCE
    assert abs(result.y - window.y) <= SNAP_DISTANCE


# Resizing (by the bottom right corner)


def test_a_note_below_another_takes_its_width() -> None:
    below = Rect(A.x, A.bottom + GAP, A.width - 9, 200)
    assert resized(below, [A], [SCREEN]) == Rect(A.x, below.y, A.width, 200)
    wider = Rect(A.x, A.bottom + GAP, A.width + 7, 200)
    assert resized(wider, [A], [SCREEN]).width == A.width


def test_a_note_beside_another_takes_its_height() -> None:
    beside = Rect(A.right + GAP, A.y, 260, A.height + 10)
    assert resized(beside, [A], [SCREEN]) == Rect(beside.x, A.y, 260, A.height)


def test_it_stops_short_of_the_note_on_its_right_or_below() -> None:
    right = Rect(A.right + GAP, A.y, 260, 300)
    left = Rect(A.x - 200 - GAP - 8, A.y, 200, 300)  # 8 short of the gap before A
    assert resized(left, [A, right], [SCREEN]).right == A.x - GAP
    above = Rect(A.x, A.y - 150 - GAP - 5, 260, 150)
    assert resized(above, [A], [SCREEN]).bottom == A.y - GAP


def test_it_goes_to_the_edge_of_the_screen() -> None:
    near = Rect(SCREEN.right - 300, SCREEN.bottom - 400, 290, 392)
    assert resized(near, [], [SCREEN]) == Rect(near.x, near.y, 300, 400)


def test_a_folded_note_keeps_its_height() -> None:
    beside = Rect(A.right + GAP, A.y, 260, 30)
    assert resized(beside, [A], [SCREEN], height_too=False).height == 30
    below = Rect(A.x, A.bottom + GAP, A.width - 5, 30)
    assert resized(below, [A], [SCREEN], height_too=False) == Rect(A.x, below.y, A.width, 30)


def test_far_from_every_edge_the_size_stays() -> None:
    alone = Rect(900, 400, 250, 250)
    assert resized(alone, [A], [SCREEN]) == alone


@given(rects, st.lists(rects, max_size=6))
def test_resizing_keeps_the_place_and_changes_the_size_little(
    window: Rect, others: list[Rect]
) -> None:
    result = resized(window, others, [SCREEN])
    assert (result.x, result.y) == (window.x, window.y)
    assert abs(result.right - window.right) <= SNAP_DISTANCE
    assert abs(result.bottom - window.bottom) <= SNAP_DISTANCE
