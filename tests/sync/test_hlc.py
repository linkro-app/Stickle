"""The clock never goes back, whatever the wall clock does and whatever times
come from other devices; written as text, its times sort as they are."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from stickle.sync.hlc import LARGEST_COUNTER, LARGEST_MILLIS, Clock, Hlc

TIMES = st.builds(Hlc, st.integers(0, 10**13 - 1), st.integers(0, LARGEST_COUNTER))
END = Hlc(LARGEST_MILLIS, LARGEST_COUNTER)
# digits of another script, which a regular expression's \d would take
OTHER_DIGITS = "".join(chr(0x0660 + int(c)) if c.isdigit() else c for c in "1762483200000-0000")


@given(a=TIMES, b=TIMES)
def test_the_text_sorts_as_the_times_do(a: Hlc, b: Hlc) -> None:
    assert (a.text < b.text) == (a < b)
    assert len(a.text) == len(b.text) == 18
    assert Hlc.parse(a.text) == a


@pytest.mark.parametrize(
    "text",
    ["", "1762483200000-000", "1762483200000_0000", "176248320000a-0000", OTHER_DIGITS],
)
def test_only_its_own_text_is_a_time(text: str) -> None:
    with pytest.raises(ValueError):
        Hlc.parse(text)


@given(
    steps=st.lists(
        st.one_of(
            st.tuples(st.just("wall"), st.integers(-1000, 10**13 + 1000)),
            st.tuples(st.just("now"), st.just(0)),
            st.tuples(st.just("saw"), st.integers(0, 10**13 - 1)),
        ),
        max_size=60,
    )
)
def test_it_never_goes_back(steps: list[tuple[str, int]]) -> None:
    wall = [1_762_483_200_000]
    clock = Clock(lambda: wall[0])
    latest: Hlc | None = None
    for step, number in steps:
        if step == "wall":
            wall[0] = number  # forward, back, before 1970, after 2286
        elif step == "now":
            if latest == END:  # seen from a clock wrong past 2286: no later time
                with pytest.raises(OverflowError):
                    clock.now()
                continue
            made = clock.now()
            assert latest is None or made > latest
            latest = made
        else:
            seen = Hlc(number, LARGEST_COUNTER)
            clock.saw(seen)
            latest = seen if latest is None or seen > latest else latest
        assert clock.last == latest


def test_a_time_is_its_wall_clock_while_that_goes_ahead() -> None:
    wall = [1000]
    clock = Clock(lambda: wall[0])

    first = clock.now()
    second = clock.now()  # the same millisecond
    wall[0] = 2000

    assert (first, second, clock.now()) == (Hlc(1000), Hlc(1000, 1), Hlc(2000))


def test_its_counter_full_it_takes_the_next_millisecond() -> None:
    clock = Clock(lambda: 5, last=Hlc(5, LARGEST_COUNTER))

    assert clock.now() == Hlc(6)


def test_kept_and_given_back_it_goes_on_from_there() -> None:
    clock = Clock(lambda: 10, last=Hlc(500, 7))  # the wall clock was set back meanwhile

    assert clock.now() == Hlc(500, 8)
