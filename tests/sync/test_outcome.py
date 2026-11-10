"""What the end of a round tells whoever runs sync: no answer is tried again,
waiting twice as long each time up to five minutes; too many requests, after
as long as the cloud asked; a full cloud goes on reading and puts nothing, and
what was not put is put once there is room; sign-in needed or access withdrawn
stops that cloud; a vault gone, made again or newer stops that vault."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, WEBDAV_IGNORING, FakeCloud, Kind
from sync_bed import Clock, Device, settle

from stickle.sync.format import NewerFormatError, UnknownKeyError, UnreadableError
from stickle.sync.outcome import LONGEST_WAIT, Outcome, Stopped, after
from stickle.sync.targets.base import (
    NoAccessError,
    RefusedError,
    SignInNeededError,
    TokenExpiredError,
    TooBusyError,
    UnavailableError,
)
from stickle.sync.vault import VaultChangedError, VaultGoneError


def test_a_round_that_went_through_waits_as_usual() -> None:
    assert after(None, failures=4) == Outcome()


@pytest.mark.parametrize(
    "error", [UnavailableError(), TooBusyError(), TokenExpiredError(), UnreadableError("half")]
)
def test_failing_for_now_waits_twice_as_long_each_time_up_to_five_minutes(
    error: Exception,
) -> None:
    waits: list[float] = []
    failures = 0
    for _ in range(8):
        outcome = after(error, failures)
        assert outcome.stopped is None
        assert outcome.wait is not None
        waits.append(outcome.wait)
        failures = outcome.failures
    assert waits == [15, 30, 60, 120, 240, LONGEST_WAIT, LONGEST_WAIT, LONGEST_WAIT]


def test_no_answer_at_all_is_told_from_a_cloud_failing() -> None:
    assert after(UnavailableError()).offline
    assert not after(UnavailableError(answered=True)).offline
    assert not after(TooBusyError()).offline
    assert not after(TooBusyError(9.0)).offline
    assert not after(TokenExpiredError()).offline
    assert not after(UnreadableError("half")).offline
    assert not after(None).offline
    assert after(UnavailableError(answered=True)).wait == 15


@pytest.mark.parametrize("asked", [1.0, 90.0, 900.0])
def test_too_many_requests_waits_as_long_as_the_cloud_asked(asked: float) -> None:
    assert after(TooBusyError(asked), failures=3).wait == asked


@pytest.mark.parametrize(
    ("error", "stopped"),
    [
        (SignInNeededError(), Stopped.SIGN_IN),
        (NoAccessError(), Stopped.NO_ACCESS),
        (VaultGoneError(), Stopped.VAULT_GONE),
        (VaultChangedError(), Stopped.VAULT_CHANGED),
        (NewerFormatError("format 2"), Stopped.NEWER),
        (UnknownKeyError("data key 2"), Stopped.NEWER),
    ],
)
def test_what_wants_the_user_stops_and_is_not_tried_again_by_itself(
    error: Exception, stopped: Stopped
) -> None:
    outcome = after(error, failures=2)
    assert outcome.stopped is stopped and outcome.wait is None


def test_an_error_it_does_not_know_of_is_raised_again() -> None:
    with pytest.raises(RefusedError):
        after(RefusedError())
    with pytest.raises(ZeroDivisionError):
        after(ZeroDivisionError())


# A full cloud


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(params=[DROPBOX, WEBDAV_IGNORING], ids=lambda k: k.name)
def kind(request: pytest.FixtureRequest) -> Kind:
    return request.param


@pytest.fixture
def two(tmp_path: Path, kind: Kind, clock: Clock) -> Iterator[tuple[FakeCloud, Device, Device]]:
    cloud = FakeCloud(kind)
    a = Device("a", tmp_path, cloud, clock, whole=True)
    b = Device("b", tmp_path, cloud, clock, whole=True)
    yield cloud, a, b
    a.close()
    b.close()


def puts(cloud: FakeCloud, device: str, since: int) -> int:
    """Puts of notes and records (its own file aside)."""
    return len(
        [
            line
            for line in cloud.log[since:]
            if f" {device} put " in line and f" {device} put devices/" not in line
        ]
    )


def test_a_full_cloud_goes_on_reading_and_puts_once_a_round(
    two: tuple[FakeCloud, Device, Device], clock: Clock
) -> None:
    cloud, a, b = two
    first = a.notes.create("처음")
    settle([a, b])
    b.notes.update_body(first.id, "b가 고침")
    b.sync()
    cloud.full = True
    mine = [a.notes.create(f"올리지 못한 메모 {i}") for i in range(3)]
    a.labels.create_category("일", "blue")
    before = len(cloud.log)
    report = a.sync()
    assert report.full
    assert a.texts()[first.id] == "b가 고침"  # still taken in
    assert puts(cloud, "a", before) == 1  # one try, turned down
    clock.tick()
    before = len(cloud.log)
    assert a.sync().full
    assert puts(cloud, "a", before) == 1
    assert after(None, full=True) == Outcome(full=True)
    cloud.full = False
    settle([a, b])
    assert set(b.texts()) == {first.id, *(note.id for note in mine)}
    assert [c.name for c in b.labels.categories()] == ["일"]
