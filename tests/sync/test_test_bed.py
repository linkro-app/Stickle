"""The test bed tests itself: devices let in one request at a time really do
fall between each other's reads and writes, so that a careless way of writing
loses another device's write and the test finds it, while a careful one (a
condition, then read again and merge when refused) loses nothing whatever the
order and the troubles. The same order and troubles give the same run."""

import json
from collections.abc import Callable

from fake_cloud import (
    DROPBOX,
    ONEDRIVE,
    WEBDAV,
    WEBDAV_HALF,
    WEBDAV_IGNORING,
    WEBDAV_NO_TAGS,
    WEBDAV_WEAK,
    FakeCloud,
    Kind,
    Trouble,
    Turns,
    by_number,
)
from hypothesis import find, given, settings
from hypothesis import strategies as st

from stickle.sync.targets.base import (
    Absent,
    AsRead,
    Condition,
    NotThereError,
    RefusedError,
    Target,
    TooBusyError,
    UnavailableError,
)

SHARED = "records/shared.json"
TRIES = 60


def add_items(target: Target, items: list[str]) -> None:
    """Each item into the shared list: read it, add the item, write it only if
    it is still as read; refused or unanswered, read again (an item found there
    already was written, its answer lost). A file that will not read whole is
    not all there yet: read again."""
    for item in items:
        for _ in range(TRIES):
            try:
                condition: Condition
                try:
                    read = target.get(SHARED)
                    held: list[str] = json.loads(read.data)
                    condition = AsRead(read.tag)
                except NotThereError:
                    held, condition = [], Absent()
                if item in held:
                    break
                target.put(SHARED, json.dumps([*held, item]).encode(), condition)
                break
            except RefusedError, UnavailableError, TooBusyError, ValueError:
                continue
        else:
            raise AssertionError(f"{item} never got in")


def run(
    kind: Kind,
    lists: list[list[str]],
    order: list[int],
    troubles: list[Trouble | None],
) -> FakeCloud:
    cloud = FakeCloud(kind, trouble=by_number(troubles))
    turns = Turns(order)
    cloud.turns = turns

    def job(target: Target, items: list[str]) -> Callable[[], None]:
        return lambda: add_items(target, items)

    devices = {
        f"device{number}": job(cloud.target(f"device{number}"), items)
        for number, items in enumerate(lists)
    }
    failed = turns.run(devices)
    assert not failed, failed
    return cloud


def held(cloud: FakeCloud) -> list[str]:
    data = cloud.files().get(SHARED)
    return [] if data is None else json.loads(data)


ITEMS = st.integers(1, 3)
DEVICES = st.integers(2, 3)
ORDER = st.lists(st.integers(0, 2), max_size=80)
# Troubles that leave a careful writer whole: a lost answer, none, too many, half a file.
TROUBLES = st.lists(
    st.sampled_from(
        [None, None, None, Trouble.LOST, Trouble.UNAVAILABLE, Trouble.TOO_BUSY, Trouble.HALF]
    ),
    max_size=80,
)


def lists_of(devices: int, items: int) -> list[list[str]]:
    return [[f"{device}-{n}" for n in range(items)] for device in range(devices)]


@settings(max_examples=60, deadline=None)
@given(
    kind=st.sampled_from([DROPBOX, ONEDRIVE, WEBDAV, WEBDAV_WEAK, WEBDAV_HALF]),
    devices=DEVICES,
    items=ITEMS,
    order=ORDER,
    troubles=TROUBLES,
)
def test_with_conditions_kept_no_write_is_lost_or_made_twice(
    kind: Kind, devices: int, items: int, order: list[int], troubles: list[Trouble | None]
) -> None:
    lists = lists_of(devices, items)

    cloud = run(kind, lists, order, troubles)

    every = [item for items_of in lists for item in items_of]
    assert sorted(held(cloud)) == sorted(every), cloud.describe()


def test_where_conditions_are_ignored_the_test_finds_a_write_lost() -> None:
    # The careful writer, on a server that writes whatever the condition: an
    # order where one device's write is lost exists, and the test bed finds it
    # (sync keeps what it wrote until it sees it there, for these servers).
    for kind in (WEBDAV_IGNORING, WEBDAV_NO_TAGS):
        found = find(
            st.tuples(ORDER, st.just(kind)),
            lambda case: len(held(run(case[1], lists_of(2, 1), case[0], []))) < 2,
            settings=settings(max_examples=200, deadline=None, database=None),
        )
        assert found[1] is kind


def test_turns_do_mix_two_devices_requests() -> None:
    cloud = run(DROPBOX, lists_of(2, 2), [0, 1, 0, 1, 1, 0, 0, 1], [])
    devices = [line.split()[1] for line in cloud.log]
    assert devices[:4] == ["device0", "device1", "device0", "device1"]


def test_the_same_order_and_troubles_give_the_same_run() -> None:
    order = [1, 0, 0, 1, 1, 1, 0, 2, 0, 1]
    troubles = [None, Trouble.LOST, None, Trouble.TOO_BUSY, None, Trouble.HALF]
    first = run(WEBDAV_WEAK, lists_of(3, 2), order, troubles)
    again = run(WEBDAV_WEAK, lists_of(3, 2), order, troubles)
    assert first.log == again.log and first.files() == again.files()
