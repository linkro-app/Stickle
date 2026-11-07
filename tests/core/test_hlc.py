"""A field's new time is always after the one it replaces, whatever the clock
does; written as text, times sort as they are."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from stickle.core.hlc import END, LARGEST_COUNTER, LARGEST_MILLIS, Hlc, after

TIMES = st.builds(Hlc, st.integers(0, LARGEST_MILLIS), st.integers(0, LARGEST_COUNTER))
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


@given(millis=st.integers(-1000, LARGEST_MILLIS + 1000), before=st.none() | TIMES)
def test_a_change_is_after_the_value_it_replaces(millis: int, before: Hlc | None) -> None:
    made = after(millis, before)

    if before is not None and before != END:
        assert made > before
    if before is None or Hlc(min(max(millis, 0), LARGEST_MILLIS)) > before:
        assert made == Hlc(min(max(millis, 0), LARGEST_MILLIS))  # the clock, when it is later


@given(steps=st.lists(st.integers(-1000, LARGEST_MILLIS + 1000), max_size=40))
def test_changing_a_field_again_and_again_never_goes_back(steps: list[int]) -> None:
    stamp: Hlc | None = None
    for millis in steps:  # the clock forward, back, before 1970, after 2286
        made = after(millis, stamp)
        assert stamp is None or made > stamp
        stamp = made


def test_the_clock_when_it_is_ahead_of_the_field() -> None:
    first = after(1000, None)
    second = after(1000, first)  # the same millisecond

    assert (first, second, after(2000, second)) == (Hlc(1000), Hlc(1000, 1), Hlc(2000))


def test_a_field_from_a_clock_far_ahead_is_still_beaten_by_the_next_change() -> None:
    from_ahead = Hlc(1_762_483_200_000 + 9 * 3_600_000)  # nine hours ahead

    assert after(1_762_483_200_000, from_ahead) == Hlc(from_ahead.millis, 1)


def test_its_counter_full_it_takes_the_next_millisecond() -> None:
    assert after(5, Hlc(5, LARGEST_COUNTER)) == Hlc(6)


def test_the_last_time_of_all_stays_the_last() -> None:
    with pytest.raises(OverflowError):
        END.following()
    assert after(0, END) == END  # a change to a broken file's field does not fail
