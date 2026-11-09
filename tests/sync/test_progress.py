"""A round tells how far it has got, note by note, so that a long first one can
be shown going on."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_cloud import WEBDAV, FakeCloud
from sync_bed import VAULT, Clock, Device

from stickle.sync.engine import GETTING, PUTTING, joined, sync_vault


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud(WEBDAV)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def a(tmp_path: Path, cloud: FakeCloud, clock: Clock) -> Iterator[Device]:
    device = Device("a", tmp_path, cloud, clock, whole=True)
    yield device
    device.close()


@pytest.fixture
def b(tmp_path: Path, cloud: FakeCloud, clock: Clock) -> Iterator[Device]:
    device = Device("b", tmp_path, cloud, clock, whole=True)
    yield device
    device.close()


def told_of(device: Device) -> list[tuple[str, int, int]]:
    told: list[tuple[str, int, int]] = []

    def progress(doing: str, done: int, of: int) -> None:
        told.append((doing, done, of))

    sync_vault(device.db, device.place, device.clock, device.notes, progress=progress)
    return told


def test_notes_going_up_are_counted_one_by_one(a: Device) -> None:
    for text in ("one", "two", "three"):
        a.notes.create(text)
    assert told_of(a) == [(PUTTING, 1, 3), (PUTTING, 2, 3), (PUTTING, 3, 3)]


def test_notes_coming_down_are_counted_one_by_one(a: Device, b: Device) -> None:
    for text in ("one", "two", "three"):
        a.notes.create(text)
    a.sync()
    assert told_of(b) == [(GETTING, 1, 3), (GETTING, 2, 3), (GETTING, 3, 3)]


def test_nothing_to_do_tells_nothing(a: Device) -> None:
    a.notes.create("one")
    a.sync()
    assert told_of(a) == []


def test_reading_back_what_it_put_is_not_getting_notes(a: Device) -> None:
    for text in ("one", "two"):
        a.notes.create(text)
    a.sync()
    before = len(a.cloud.log)
    assert told_of(a) == []
    assert sum(" get notes/" in line for line in a.cloud.log[before:]) == 2


def test_a_note_folded_into_the_vaults_is_not_counted_as_going_up(a: Device, b: Device) -> None:
    a.notes.create("alike")
    a.sync()
    b.notes.create("alike")
    b.notes.create("only here")
    joined(b.db, VAULT)
    told = told_of(b)
    assert told[0] == (GETTING, 1, 1)
    assert told[1:] == [(PUTTING, 1, 1)]
