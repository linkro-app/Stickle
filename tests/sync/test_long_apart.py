"""A note changed on one device more times than a version's history holds, while
another stayed away: one that left the note as it was takes the text it is
now, with no conflict copy (nothing of its own to keep). One that changed it
meanwhile still keeps both."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_cloud import DROPBOX
from test_records import KEYS, VAULT, Device, Times, vault_cloud

from stickle.data import sync_state
from stickle.data.sync_state import NoteState
from stickle.sync import engine, local
from stickle.sync.contents import Contents
from stickle.sync.merge import merge
from stickle.sync.note_file import HISTORY, NoteFile, Step, Version, new_version

DESKTOP = "a-0000-4000-8000-000000000000"


@pytest.fixture
def times() -> Times:
    return Times()


@pytest.fixture
def devices(tmp_path: Path, times: Times) -> Iterator[tuple[Device, Device]]:
    cloud = vault_cloud(DROPBOX)
    desktop, laptop = Device("a", tmp_path, cloud, times), Device("b", tmp_path, cloud, times)
    yield desktop, laptop
    for device in (desktop, laptop):
        device.db.close()


def settle(*devices: Device) -> None:
    for _ in range(3):
        for device in devices:
            device.sync()
    for device in devices:
        assert not sync_state.unconfirmed(device.db, VAULT)


def written_on(device: Device, note_id: str, times: int) -> str:
    """The note's text changed times, each put in a round of its own."""
    text = ""
    for count in range(times):
        text = f"오래 쓴 글 {count}"
        device.notes.update_body(note_id, text)
        device.sync()
    return text


def copies(device: Device) -> list[str]:
    return [note.body for note in device.notes.all() if note.conflict is not None]


@pytest.mark.parametrize("times_changed", [HISTORY, HISTORY + 1, HISTORY + 8])
def test_left_as_it_was_it_takes_the_text_without_a_copy(
    devices: tuple[Device, Device], times_changed: int
) -> None:
    desktop, laptop = devices
    note_id = desktop.notes.create("처음 글").id
    settle(desktop, laptop)

    last = written_on(desktop, note_id, times_changed)  # the laptop shut meanwhile
    settle(laptop, desktop)

    for device in devices:
        note = device.notes.get(note_id)
        assert note is not None and note.body == last
        assert copies(device) == []


def test_changed_meanwhile_it_still_keeps_both(devices: tuple[Device, Device]) -> None:
    desktop, laptop = devices
    note_id = desktop.notes.create("처음 글").id
    settle(desktop, laptop)
    laptop.notes.update_body(note_id, "노트북에서 고친 글")  # not put yet

    last = written_on(desktop, note_id, HISTORY + 8)
    settle(laptop, desktop)

    bodies = sorted([desktop.notes.get(note_id).body, *copies(desktop)])  # pyright: ignore[reportOptionalMemberAccess]
    assert bodies == sorted([last, "노트북에서 고친 글"])


def test_left_as_it_was_what_it_changed_besides_the_text_stays(
    devices: tuple[Device, Device],
) -> None:
    desktop, laptop = devices
    note_id = desktop.notes.create("처음 글").id
    settle(desktop, laptop)
    laptop.notes.set_color(note_id, "blue")  # its colour only

    last = written_on(desktop, note_id, HISTORY + 8)
    settle(laptop, desktop)

    for device in devices:
        note = device.notes.get(note_id)
        assert note is not None and (note.body, note.color) == (last, "blue")
        assert copies(device) == []


# As merged


def line(start: Version, length: int) -> Version:
    """start, changed length times on from it."""
    version = start
    for count in range(length):
        version = new_version(KEYS, [version], f"글 {count}", count, DESKTOP)
    return version


def kept(body: Version) -> NoteFile:
    return NoteFile(Contents(), body)


def test_left_as_it_was_and_far_behind_the_cloud_s_line_it_takes_it() -> None:
    start = new_version(KEYS, [], "처음 글", 0, DESKTOP)
    cloud = line(start, HISTORY + 8)
    assert not cloud.holds(start.id)  # further on than its history reaches back

    merged = merge(KEYS, "note", kept(start), kept(cloud), untouched=True)

    assert merged.step is Step.RECEIVE and merged.copy is None
    assert merged.note.body == cloud


def test_left_as_it_was_with_the_vault_gone_back_further_still_both_are_kept() -> None:
    # Its line far on here; the vault's, gone back to another one long before.
    start = new_version(KEYS, [], "처음 글", 0, DESKTOP)
    here = line(start, HISTORY + 8)
    gone_back = new_version(KEYS, [start], "옛 갈래", 1, DESKTOP)

    merged = merge(KEYS, "note", kept(here), kept(gone_back), untouched=True)

    assert merged.step is Step.CONFLICT and merged.copy is not None


def test_changed_here_far_behind_both_are_kept() -> None:
    start = new_version(KEYS, [], "처음 글", 0, DESKTOP)
    changed = new_version(KEYS, [start], "여기서 고침", 1, DESKTOP)

    merged = merge(KEYS, "note", kept(changed), kept(line(start, HISTORY + 8)))

    assert merged.step is Step.CONFLICT and merged.copy is not None


def test_it_is_left_as_it_was_only_with_its_text_as_seen_and_nothing_put_unseen() -> None:
    seen = kept(new_version(KEYS, [], "처음 글", 0, DESKTOP))
    put = kept(new_version(KEYS, [seen.body], "고친 글", 1, DESKTOP))
    untouched = engine._untouched  # pyright: ignore[reportPrivateUsage]

    assert untouched(NoteState(seen=local.as_kept(seen)), seen)
    assert not untouched(NoteState(seen=local.as_kept(seen)), put)  # changed here
    assert not untouched(NoteState(seen=local.as_kept(seen), outbox=local.as_kept(put)), put)
    assert not untouched(None, seen)
