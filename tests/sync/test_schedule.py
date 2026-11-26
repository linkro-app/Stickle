"""When rounds of sync are due."""

import pytest

from stickle.sync.schedule import (
    BIG_EDIT_WAIT,
    DAY,
    EDIT_WAIT,
    EVERY,
    FOCUS_AT_LEAST,
    LOOK_AGAIN,
    OFFLINE_AGAIN,
    START_WAIT,
    WOKE_WAIT,
    Schedule,
)


def after_first(now: float = 0.0, **given: object) -> Schedule:
    """As it is once the first round, a full look, went through at now."""
    schedule = Schedule(now - START_WAIT, **given)  # pyright: ignore[reportArgumentType]
    assert schedule.begin(now) is True
    schedule.ended(now)
    return schedule


def due(schedule: Schedule) -> float:
    at = schedule.next_at()
    assert at is not None
    return at


def test_the_first_round_comes_soon_after_the_start_and_looks_at_everything() -> None:
    schedule = Schedule(100.0)
    assert due(schedule) == 100.0 + START_WAIT
    assert schedule.begin(due(schedule)) is True


def test_nothing_changed_a_look_every_five_minutes() -> None:
    schedule = after_first()
    assert due(schedule) == EVERY
    assert schedule.begin(EVERY) is False


def test_the_look_is_as_often_as_set() -> None:
    assert due(after_first(every=30.0)) == 30.0


def test_an_edit_goes_five_seconds_after_the_last_change() -> None:
    schedule = after_first()
    schedule.edited(10.0)
    assert due(schedule) == 10.0 + EDIT_WAIT
    schedule.edited(13.0)
    assert due(schedule) == 13.0 + EDIT_WAIT


def test_a_big_note_waits_a_minute() -> None:
    schedule = after_first()
    schedule.edited(10.0, big=True)
    assert due(schedule) == 10.0 + BIG_EDIT_WAIT


def test_a_big_note_goes_when_it_is_left() -> None:
    schedule = after_first()
    schedule.edited(10.0, big=True)
    schedule.left(12.0)
    assert due(schedule) == 12.0


def test_leaving_a_note_starts_nothing_if_no_big_one_waits() -> None:
    schedule = after_first()
    schedule.left(12.0)
    assert due(schedule) == EVERY


def test_a_small_edit_does_not_wait_for_a_big_one() -> None:
    schedule = after_first()
    schedule.edited(10.0, big=True)
    schedule.edited(11.0)
    assert due(schedule) == 11.0 + EDIT_WAIT


def test_coming_to_the_front_sends_what_waits_at_once() -> None:
    schedule = after_first()
    schedule.edited(10.0)
    schedule.focused(11.0)
    assert due(schedule) == 11.0


def test_coming_to_the_front_looks_only_if_the_last_look_is_old_enough() -> None:
    schedule = after_first(100.0)
    schedule.focused(100.0 + FOCUS_AT_LEAST - 1)
    assert due(schedule) == 100.0 + EVERY
    schedule.focused(100.0 + FOCUS_AT_LEAST)
    assert due(schedule) == 100.0 + FOCUS_AT_LEAST


def test_waking_looks_once_the_network_may_be_back() -> None:
    schedule = after_first()
    schedule.woke(50.0)
    assert due(schedule) == 50.0 + WOKE_WAIT


def test_an_edit_over_that_held_what_came_looks_at_once() -> None:
    schedule = after_first()
    schedule.let_go(50.0)
    assert due(schedule) == 50.0


def test_a_change_the_cloud_tells_of_looks_at_once() -> None:
    schedule = after_first()
    schedule.changed_elsewhere(50.0)
    assert due(schedule) == 50.0
    assert schedule.begin(50.0) is False


def test_a_change_told_while_a_round_runs_brings_another() -> None:
    schedule = after_first()
    schedule.woke(50.0)
    schedule.begin(50.0)
    schedule.changed_elsewhere(51.0)
    schedule.ended(52.0)
    assert due(schedule) == 51.0


def test_asking_is_a_full_look_at_once() -> None:
    schedule = after_first()
    schedule.asked(50.0)
    assert due(schedule) == 50.0
    assert schedule.begin(50.0) is True


def test_a_full_look_a_day_after_the_last() -> None:
    schedule = after_first()
    schedule.woke(DAY - 1)
    assert schedule.begin(DAY - 1) is False
    schedule.ended(DAY - 1)
    schedule.woke(DAY)
    assert schedule.begin(DAY) is True


def test_a_full_look_that_failed_is_done_again() -> None:
    schedule = after_first()
    schedule.asked(50.0)
    assert schedule.begin(50.0) is True
    schedule.ended(60.0, wait=15.0)
    assert schedule.begin(due(schedule)) is True


def test_a_failed_first_round_is_still_a_full_look_next() -> None:
    schedule = Schedule(0.0)
    assert schedule.begin(START_WAIT) is True
    schedule.ended(START_WAIT, wait=15.0)
    assert schedule.begin(due(schedule)) is True


def test_after_a_failure_it_tries_again_as_told() -> None:
    schedule = after_first()
    schedule.woke(50.0)
    schedule.begin(50.0)
    schedule.ended(51.0, wait=15.0)
    assert due(schedule) == 66.0


@pytest.mark.parametrize("event", ["edit", "focus", "wake", "left", "let go", "told"])
def test_while_it_fails_nothing_starts_a_round_sooner(event: str) -> None:
    schedule = after_first()
    schedule.woke(50.0)
    schedule.begin(50.0)
    schedule.ended(51.0, wait=120.0)
    if event == "edit":
        schedule.edited(52.0)
    elif event == "focus":
        schedule.focused(52.0)
    elif event == "wake":
        schedule.woke(52.0)
    elif event == "let go":
        schedule.let_go(52.0)
    elif event == "told":
        schedule.changed_elsewhere(52.0)
    else:
        schedule.edited(52.0, big=True)
        schedule.left(52.0)
    assert due(schedule) == 171.0


def test_asking_does_not_wait_out_a_failure() -> None:
    schedule = after_first()
    schedule.woke(50.0)
    schedule.begin(50.0)
    schedule.ended(51.0, wait=120.0)
    schedule.asked(52.0)
    assert due(schedule) == 52.0


def test_asking_during_a_round_that_fails_is_not_held_back_by_its_failing() -> None:
    schedule = after_first()
    schedule.woke(50.0)
    schedule.begin(50.0)
    schedule.asked(50.5)
    schedule.ended(51.0, wait=120.0)
    assert due(schedule) == 50.5
    assert schedule.held


def test_what_changed_during_a_round_is_not_forgotten() -> None:
    schedule = after_first()
    schedule.woke(50.0)
    schedule.begin(50.0)
    schedule.edited(51.0)
    schedule.ended(52.0)
    assert due(schedule) == 51.0 + EDIT_WAIT
    assert schedule.waiting_to_put


def test_a_round_takes_what_waited_with_it() -> None:
    schedule = after_first()
    schedule.edited(10.0)
    assert schedule.waiting_to_put
    schedule.begin(15.0)
    schedule.ended(16.0)
    assert not schedule.waiting_to_put
    assert due(schedule) == 16.0 + EVERY


@pytest.mark.parametrize("event", ["edit", "focus", "wake", "time"])
def test_stopped_nothing_starts_a_round_by_itself(event: str) -> None:
    schedule = after_first()
    schedule.woke(50.0)
    schedule.begin(50.0)
    schedule.ended(51.0, stopped=True)
    if event == "edit":
        schedule.edited(52.0)
    elif event == "focus":
        schedule.focused(100.0)
    elif event == "wake":
        schedule.woke(52.0)
    assert schedule.next_at() is None
    assert schedule.held


def test_stopped_when_it_starts_it_waits_for_the_user() -> None:
    schedule = Schedule(0.0, stopped=True)
    assert schedule.next_at() is None
    schedule.asked(10.0)
    assert due(schedule) == 10.0


def test_a_round_through_after_being_asked_goes_on_as_usual() -> None:
    schedule = Schedule(0.0, stopped=True)
    schedule.asked(10.0)
    schedule.begin(10.0)
    schedule.ended(11.0)
    assert due(schedule) == 11.0 + EVERY
    assert not schedule.held


def test_setting_the_interval_shorter_applies_at_once() -> None:
    schedule = after_first(100.0)
    schedule.set_every(30.0)
    assert due(schedule) == 130.0


def test_setting_the_interval_longer_does_not_put_off_what_is_due() -> None:
    schedule = after_first(100.0)
    schedule.set_every(3600.0)
    assert due(schedule) == 100.0 + EVERY


def test_a_failing_round_is_held() -> None:
    schedule = after_first()
    schedule.woke(50.0)
    schedule.begin(50.0)
    schedule.ended(51.0, wait=15.0)
    assert schedule.held
    schedule.begin(66.0)
    schedule.ended(67.0)
    assert not schedule.held


def test_hurrying_sends_what_waits_now() -> None:
    schedule = after_first()
    schedule.edited(10.0)
    schedule.edited(11.0, big=True)
    schedule.hurry(12.0)
    assert due(schedule) == 12.0
    schedule.begin(12.0)
    assert not schedule.waiting_to_put


def test_hurrying_with_nothing_waiting_starts_nothing() -> None:
    schedule = after_first()
    schedule.hurry(12.0)
    assert due(schedule) == EVERY


# With no answer at all (no network), what happens here tries again soon


def failed_offline(at: float = 51.0, wait: float = 120.0) -> Schedule:
    schedule = after_first()
    schedule.woke(40.0)
    schedule.begin(50.0)
    schedule.ended(at, wait=wait, offline=True)
    return schedule


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("edit", 51.0 + OFFLINE_AGAIN),
        ("focus", 51.0 + OFFLINE_AGAIN),
        ("wake", 51.0 + OFFLINE_AGAIN),
        ("told", 51.0 + OFFLINE_AGAIN),
        ("late edit", 90.0 + EDIT_WAIT),
    ],
)
def test_offline_something_happening_tries_again_soon_but_not_at_once(
    event: str, expected: float
) -> None:
    schedule = failed_offline()
    match event:
        case "edit":
            schedule.edited(52.0)
        case "focus":
            schedule.focused(52.0)
        case "wake":
            schedule.woke(52.0)
        case "told":
            schedule.changed_elsewhere(52.0)
        case _:
            schedule.edited(90.0)
    assert due(schedule) == expected


def test_offline_with_nothing_happening_it_waits_as_told() -> None:
    assert due(failed_offline()) == 51.0 + 120.0


def test_failing_with_an_answer_nothing_pulls_it_sooner() -> None:
    schedule = after_first()
    schedule.begin(50.0)
    schedule.ended(51.0, wait=120.0)  # busy, or a server error: as it says
    schedule.edited(52.0)
    assert due(schedule) == 171.0


def test_through_again_it_is_no_longer_offline() -> None:
    schedule = failed_offline()
    schedule.begin(66.0)
    schedule.ended(67.0)
    schedule.begin(68.0)
    schedule.ended(69.0, wait=120.0)  # failing again, with an answer
    schedule.edited(70.0)
    assert due(schedule) == 189.0


# Stopped for what may be dealt with elsewhere, looked at again now and then


def stopped_looking_again() -> Schedule:
    schedule = after_first()
    schedule.begin(50.0)
    schedule.ended(51.0, stopped=True, looks_again=True)
    return schedule


def test_stopped_for_a_big_change_it_looks_again_in_an_hour() -> None:
    schedule = stopped_looking_again()
    assert due(schedule) == 51.0 + LOOK_AGAIN
    assert schedule.held


@pytest.mark.parametrize(
    ("event", "expected"), [("focus", 100.0), ("told", 60.0), ("wake", 60.0 + WOKE_WAIT)]
)
def test_stopped_for_a_big_change_the_cloud_the_front_or_waking_looks_again_sooner(
    event: str, expected: float
) -> None:
    schedule = stopped_looking_again()
    match event:
        case "focus":
            schedule.focused(100.0)
        case "told":
            schedule.changed_elsewhere(60.0)
        case _:
            schedule.woke(60.0)
    assert due(schedule) == expected


def test_stopped_for_a_big_change_an_edit_here_looks_at_nothing() -> None:
    schedule = stopped_looking_again()
    schedule.edited(60.0)
    assert due(schedule) == 51.0 + LOOK_AGAIN


def test_still_stopped_after_looking_again_it_looks_again_an_hour_on() -> None:
    schedule = stopped_looking_again()
    schedule.begin(51.0 + LOOK_AGAIN)
    schedule.ended(52.0 + LOOK_AGAIN, stopped=True, looks_again=True)
    assert due(schedule) == 52.0 + 2 * LOOK_AGAIN
