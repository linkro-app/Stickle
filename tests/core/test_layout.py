"""Where notes go on screen: numbered monitors, a place for each number of them."""

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from stickle.core.layout import (
    Monitor,
    Place,
    Rect,
    Size,
    chosen,
    fit,
    monitor_at,
    numbered,
    on_monitor,
    place_of,
    remember,
    restore,
    size_of,
)

TASKBAR = 40


def monitor(
    name: str, x: int, width: int = 1920, height: int = 1080, primary: bool = False
) -> Monitor:
    return Monitor(
        name=name,
        geometry=Rect(x, 0, width, height),
        available=Rect(x, 0, width, height - TASKBAR),
        primary=primary,
    )


def inside(window: Rect, area: Rect) -> bool:
    return (
        area.x <= window.x
        and area.y <= window.y
        and window.right <= area.right
        and window.bottom <= area.bottom
    )


NOTE = Rect(0, 0, 260, 240)
SIZE = size_of(NOTE)


def at(x: int, y: int) -> Rect:
    return Rect(x, y, NOTE.width, NOTE.height)


def shown(places: dict[int, Place], monitors: list[Monitor]) -> Rect:
    where = restore(places, SIZE, monitors)
    assert where is not None
    return where


def on(where: Rect, monitors: list[Monitor]) -> str:
    """The name of the monitor a note is on."""
    return monitor_at(where, monitors)[1].name


# Numbering


@pytest.mark.parametrize(
    "arrangement",
    [
        [monitor("P", 0, primary=True), monitor("A", 1920), monitor("B", 3840)],  # main on the left
        [monitor("A", 0), monitor("P", 1920, primary=True), monitor("B", 3840)],  # in the middle
        [monitor("A", 0), monitor("B", 1920), monitor("P", 3840, primary=True)],  # on the right
    ],
)
def test_main_monitor_is_one_then_left_to_right(arrangement: list[Monitor]) -> None:
    assert [m.name for m in numbered(arrangement)] == ["P", "A", "B"]


def test_numbering_ignores_the_order_the_system_lists_them_in() -> None:
    listed = [monitor("B", 3840), monitor("P", 0, primary=True), monitor("A", 1920)]

    assert [m.name for m in numbered(listed)] == ["P", "A", "B"]


# Coming back to the same place


def test_same_place_after_reopening() -> None:
    screens = [monitor("P", 0, primary=True), monitor("A", 1920)]
    window = at(2500, 300)

    assert shown(remember({}, window, screens), screens) == window


def test_same_proportional_place_after_a_resolution_change() -> None:
    before = [monitor("P", 0, 1920, 1080, primary=True)]
    after = [monitor("P", 0, 3840, 2160, primary=True)]

    where = shown(remember({}, at(960, 540), before), after)

    assert (where.x, where.y) == (1920, 1080)


def test_the_size_is_the_one_given() -> None:
    screens = [monitor("P", 0, primary=True)]
    places = remember({}, at(100, 100), screens)

    where = restore(places, Size(400, 300), screens)

    assert where == Rect(100, 100, 400, 300)


# A place for each number of monitors


def test_note_of_a_missing_monitor_shows_on_monitor_one_and_goes_back() -> None:
    both = [monitor("P", 0, primary=True), monitor("B", 1920)]
    alone = [monitor("P", 0, primary=True)]
    places = remember({}, at(2500, 300), both)

    where = shown(places, alone)
    assert inside(where, alone[0].available)
    assert (where.x, where.y) == (2500 - 1920, 300)  # as far in on monitor 1

    assert shown(places, both) == at(2500, 300)


def test_moving_it_with_fewer_monitors_changes_only_their_place() -> None:
    both = [monitor("P", 0, primary=True), monitor("B", 1920)]
    alone = [monitor("P", 0, primary=True)]
    places = remember({}, at(2500, 300), both)

    places = remember(places, at(100, 700), alone)

    assert set(places) == {1, 2}
    assert shown(places, alone) == at(100, 700)
    assert shown(places, both) == at(2500, 300)


def test_moving_it_with_more_monitors_leaves_the_place_for_fewer() -> None:
    both = [monitor("P", 0, primary=True), monitor("B", 1920)]
    alone = [monitor("P", 0, primary=True)]
    places = remember({}, at(100, 700), alone)

    places = remember(places, at(2500, 300), both)

    assert shown(places, alone) == at(100, 700)
    assert shown(places, both) == at(2500, 300)


def test_with_no_place_for_as_many_the_nearest_number_shows_it() -> None:
    one = Place(1, "P", 0.1, 0.1, ("P",))
    three = Place(1, "P", 0.5, 0.5, ("P", "A", "B"))

    assert chosen({1: one, 3: three}, 2) == (3, three)  # the larger, between two
    assert chosen({1: one, 3: three}, 4) == (3, three)
    assert chosen({3: three}, 1) == (3, three)
    assert chosen({1: one, 3: three}, 1) == (1, one)
    assert chosen({}, 2) is None


def test_moving_to_another_monitor_while_all_are_there_moves_it_for_good() -> None:
    both = [monitor("P", 0, primary=True), monitor("B", 1920)]
    places = remember({}, at(2500, 300), both)

    places = remember(places, at(300, 300), both)

    assert places[2].monitor == 1


def test_removing_the_middle_of_three_keeps_the_others_in_place() -> None:
    three = [monitor("P", 0, primary=True), monitor("A", 1920), monitor("B", 3840)]
    without_a = [monitor("P", 0, primary=True), monitor("B", 1920)]
    on_a = remember({}, at(2500, 300), three)
    on_b = remember({}, at(4500, 300), three)

    assert on(shown(on_b, without_a), without_a) == "B"  # still on B, now number 2
    assert on(shown(on_a, without_a), without_a) == "P"  # A is gone: monitor 1


def test_another_monitor_in_its_number_takes_its_notes() -> None:
    # A laptop with a monitor at work and another at home: as many monitors,
    # so the same place, on whichever monitor stands at its number.
    work = [monitor("Laptop", 0, primary=True), monitor("DELL", 1920)]
    home = [monitor("Laptop", 0, primary=True), monitor("BenQ", 1920, 2560, 1440)]
    places = remember({}, at(1920 + 960, 540), work)

    where = shown(places, home)
    assert on(where, home) == "BenQ"
    assert (where.x, where.y) == (1920 + 1280, 720)

    places = remember(places, at(1920 + 100, 100), home)
    assert on(shown(places, work), work) == "DELL"


def test_the_same_monitor_on_another_connector_keeps_its_notes() -> None:
    # Linux names a monitor by its connector.
    before = [monitor("eDP-1", 0, primary=True), monitor("HDMI-1", 1920)]
    after = [monitor("eDP-1", 0, primary=True), monitor("DP-1", 1920)]

    assert shown(remember({}, at(2500, 300), before), after) == at(2500, 300)


def test_changing_the_main_monitor_keeps_notes_on_their_monitor() -> None:
    before = [monitor("L", 0, primary=True), monitor("R", 1920)]
    after = [monitor("L", 0), monitor("R", 1920, primary=True)]

    assert shown(remember({}, at(2500, 300), before), after) == at(2500, 300)


def test_another_computer_with_as_many_monitors_uses_the_numbers() -> None:
    here = [monitor("DELL", 0, primary=True), monitor("LG", 1920)]
    there = [monitor("HP", 0, 2560, 1440, primary=True), monitor("ASUS", 2560, 2560, 1440)]

    where = shown(remember({}, at(1920 + 960, 540), here), there)

    assert (where.x, where.y) == (2560 + 1280, 720)


def test_another_computer_with_fewer_monitors_uses_the_numbers_it_has() -> None:
    here = [monitor("DELL", 0, primary=True), monitor("LG", 1920)]
    there = [monitor("HP", 0, primary=True)]

    where = shown(remember({}, at(1920 + 960, 540), here), there)

    assert on(where, there) == "HP"


def test_twin_monitors_with_the_same_name_go_by_number() -> None:
    twins = [monitor("U2720Q", 0, primary=True), monitor("U2720Q", 1920)]

    assert shown(remember({}, at(2500, 300), twins), twins) == at(2500, 300)


# Always on screen


def test_a_note_off_the_edge_is_brought_inside_the_work_area() -> None:
    screen = monitor("P", 0, primary=True)

    where = shown({1: Place(1, "P", 0.99, 0.99)}, [screen])

    assert inside(where, screen.available)


def test_a_note_bigger_than_the_screen_is_shrunk() -> None:
    area = Rect(0, 0, 800, 560)

    assert inside(fit(Rect(-50, -50, 2000, 2000), area), area)


def test_nothing_remembered_means_nothing_to_restore() -> None:
    assert restore({}, SIZE, [monitor("P", 0, primary=True)]) is None


@pytest.mark.parametrize(
    "place",
    [
        Place(0, "", 0.1, 0.1),
        Place(1, "", math.nan, 0.1),
        Place(1, "", 0.1, math.inf),
        Place(1, "", 3, 0.1),
    ],
)
def test_nonsense_places_are_not_valid(place: Place) -> None:
    assert not place.is_valid()


@pytest.mark.parametrize("size", [Size(-5, 240), Size(260, 10**9), Size(100, 240), Size(260, 50)])
def test_nonsense_sizes_are_not_valid(size: Size) -> None:
    assert not size.is_valid()


def any_monitor(name: str, x: int, y: int, width: int, height: int, taskbar: int) -> Monitor:
    return Monitor(name, Rect(x, y, width, height), Rect(x, y, width, max(height - taskbar, 200)))


screens = st.lists(
    st.builds(
        any_monitor,
        st.sampled_from(["", "P", "A", "B"]),
        st.integers(-5000, 5000),
        st.integers(-3000, 3000),
        st.integers(640, 7680),
        st.integers(480, 4320),
        st.integers(0, 80),
    ),
    min_size=1,
    max_size=4,
)
places = st.builds(
    Place,
    st.integers(1, 5),
    st.sampled_from(["", "P", "A"]),
    st.floats(-1, 2),
    st.floats(-1, 2),
    st.lists(st.sampled_from(["", "P", "A", "C"]), max_size=3).map(tuple),
)
sizes = st.builds(Size, st.integers(120, 3000), st.integers(60, 3000))


@given(screens, st.dictionaries(st.integers(1, 5), places, min_size=1), sizes)
def test_a_restored_note_is_always_inside_a_monitor(
    monitors: list[Monitor], remembered: dict[int, Place], size: Size
) -> None:
    where = restore(remembered, size, monitors)

    assert where is not None
    assert any(inside(where, m.available) for m in monitors)


side_by_side = st.lists(
    st.tuples(st.integers(640, 7680), st.integers(480, 4320)), min_size=1, max_size=4
).map(
    lambda sizes: [
        monitor(f"M{i}", sum(w for w, _ in sizes[:i]), w, h, primary=i == 0)
        for i, (w, h) in enumerate(sizes)
    ]
)


@given(side_by_side, st.data())
def test_where_it_is_can_always_be_remembered_and_found_again(
    monitors: list[Monitor], data: st.DataObject
) -> None:
    area = data.draw(st.sampled_from(monitors)).available
    x = data.draw(st.integers(area.x, area.right - NOTE.width))
    y = data.draw(st.integers(area.y, area.bottom - NOTE.height))

    where = shown(remember({}, at(x, y), monitors), monitors)

    assert abs(where.x - x) <= 1 and abs(where.y - y) <= 1


# Monitors plugged in, taken out, moved around and changed, the note moved in
# between: each step either changes the monitors or moves the note.
POOL = ["Laptop", "DELL", "LG", "BenQ"]
setups = st.tuples(
    st.permutations(POOL).flatmap(
        lambda names: st.integers(1, len(names)).map(lambda k: names[:k])
    ),
    st.integers(0, 3),  # which of them is the main monitor
    st.lists(st.sampled_from([(1920, 1080), (2560, 1440), (3840, 2160)]), min_size=4, max_size=4),
).map(
    lambda drawn: [
        monitor(
            name,
            sum(w for w, _ in drawn[2][:i]),
            *drawn[2][i],
            primary=i == drawn[1] % len(drawn[0]),
        )
        for i, name in enumerate(drawn[0])
    ]
)


@given(
    st.lists(
        st.one_of(setups, st.tuples(st.integers(0, 3), st.floats(0, 1), st.floats(0, 1))),
        max_size=25,
    ),
    setups,
)
def test_monitors_changing_and_the_note_moving_in_between(
    steps: list[list[Monitor] | tuple[int, float, float]], first: list[Monitor]
) -> None:
    current = first
    remembered: dict[int, Place] = {}
    moved_onto: dict[int, tuple[str, Place]] = {}  # by number of monitors: the last move
    for step in steps:
        if isinstance(step, list):
            current = step
        else:
            index, fx, fy = step
            area = current[index % len(current)].available
            window = at(
                area.x + round(fx * (area.width - NOTE.width)),
                area.y + round(fy * (area.height - NOTE.height)),
            )
            before = dict(remembered)
            remembered = remember(remembered, window, current)
            count = len(current)
            # Only the place for as many monitors as now changed.
            assert {n: p for n, p in remembered.items() if n != count} == {
                n: p for n, p in before.items() if n != count
            }
            moved_onto[count] = (on(window, current), remembered[count])
        where = restore(remembered, SIZE, current)
        if not remembered:
            assert where is None
            continue
        assert where is not None
        assert any(inside(where, m.available) for m in current)
        # With as many monitors as at the last move made with that many, and the
        # monitor it was moved onto among them: there, as far in as it was put.
        last = moved_onto.get(len(current))
        if last is not None and (target := [m for m in current if m.name == last[0]]):
            assert where == on_monitor(last[1], SIZE, target[0])


def test_where_a_window_is_is_worked_out_from_its_title_bar() -> None:
    screens = [monitor("P", 0, primary=True), monitor("B", 1920)]
    # Mostly on B, but held by a title bar that is still on P.
    window = Rect(1900, 100, 400, 300)

    assert place_of(Rect(1700, 100, 400, 300), screens).monitor == 1
    assert place_of(window, screens).monitor == 2
