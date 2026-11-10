"""A note being edited on screen, as a round finds it: what would change what is
typed, or end its edit, waits for the edit to end (its text, its lock, the
trash, emptying it, a conflict copy), and nothing of it is put meanwhile; the
rest of what came is taken in. Once the edit is over, it is merged as any other.
A note edited that nothing came for goes up as usual."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, FakeCloud
from sync_bed import Clock, Device, cloud_notes, settle

from stickle.core.being_edited import Editing


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud(DROPBOX)


@pytest.fixture
def pair(tmp_path: Path, cloud: FakeCloud) -> Iterator[tuple[Device, Device]]:
    clock = Clock()
    devices = (Device("a", tmp_path, cloud, clock, True), Device("b", tmp_path, cloud, clock, True))
    yield devices
    for device in devices:
        device.close()


def shared(a: Device, b: Device, text: str = "회의 9시") -> str:
    note = a.notes.create(text)
    settle([a, b])
    a.clock.tick()
    return note.id


def test_a_text_that_comes_while_the_note_is_edited_waits(
    pair: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = pair
    note_id = shared(a, b)
    b.notes.update_body(note_id, "회의 10시", keep_earlier=True)
    b.notes.set_color(note_id, "blue")
    b.sync()
    a.editing.begin(note_id)
    before = cloud.files()

    report = a.sync()

    note = a.notes.get(note_id)
    assert note is not None and note.body == "회의 9시"  # what is typed on stays
    assert note.color == "blue"  # what does not touch it comes at once
    assert report.held == [note_id] and report.put == []
    assert {p: d for p, d in cloud.files().items() if p.startswith("notes/")} == {
        p: d for p, d in before.items() if p.startswith("notes/")
    }
    again = a.sync()  # still being edited: still waiting, read again
    assert again.held == [note_id] and a.notes.get(note_id).body == "회의 9시"  # pyright: ignore[reportOptionalMemberAccess]

    a.editing.end(note_id)
    a.sync()
    assert a.notes.get(note_id).body == "회의 10시"  # pyright: ignore[reportOptionalMemberAccess]
    assert len(a.texts()) == 1  # nothing changed here: no copy


def test_changed_on_both_while_edited_both_texts_are_kept_once_it_is_over(
    pair: tuple[Device, Device],
) -> None:
    a, b = pair
    note_id = shared(a, b)
    b.notes.update_body(note_id, "회의 10시", keep_earlier=True)
    b.sync()
    a.editing.begin(note_id)
    a.clock.tick()
    a.notes.update_body(note_id, "회의 11시", keep_earlier=True)  # typed on, saved
    assert a.sync().held == [note_id]
    assert a.notes.get(note_id).body == "회의 11시"  # pyright: ignore[reportOptionalMemberAccess]
    a.clock.tick()
    a.notes.update_body(note_id, "회의 11시 3층", keep_earlier=False)

    a.editing.end(note_id)
    settle([a, b])

    texts = sorted(a.texts().values())
    assert texts == sorted(b.texts().values()) == ["회의 10시", "회의 11시 3층"]
    assert a.notes.get(note_id).body == "회의 11시 3층"  # pyright: ignore[reportOptionalMemberAccess]  # edited later


def test_what_is_typed_goes_up_as_usual_when_nothing_came(
    pair: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = pair
    note_id = shared(a, b)
    a.editing.begin(note_id)
    a.notes.update_body(note_id, "회의 9시 반", keep_earlier=True)

    report = a.sync()

    assert report.held == [] and report.put == [note_id]
    assert cloud_notes(cloud)[note_id].body.text == "회의 9시 반"
    b.sync()
    assert b.notes.get(note_id).body == "회의 9시 반"  # pyright: ignore[reportOptionalMemberAccess]


@pytest.mark.parametrize("what", ["lock", "trash", "empty"])
def test_what_would_end_the_edit_waits_too(pair: tuple[Device, Device], what: str) -> None:
    a, b = pair
    note_id = shared(a, b)
    if what == "lock":
        b.notes.set_locked(note_id, True)
    else:
        b.notes.delete(note_id)
        if what == "empty":
            b.notes.purge(note_id)
    b.sync()
    a.editing.begin(note_id)

    assert a.sync().held == [note_id]
    note = a.notes.get(note_id)
    assert note is not None and not note.locked and note.deleted_at is None

    a.editing.end(note_id)
    a.sync()
    note = a.notes.get(note_id)
    if what == "lock":
        assert note is not None and note.locked
    elif what == "trash":
        assert note is not None and note.deleted_at is not None
    else:
        assert note is None


def test_hidden_or_folded_comes_at_once_the_window_waits_on_its_own(
    pair: tuple[Device, Device],
) -> None:
    a, b = pair
    note_id = shared(a, b)
    b.notes.set_hidden(note_id, True)
    b.notes.set_collapsed(note_id, True)
    b.sync()
    a.editing.begin(note_id)

    assert a.sync().held == []
    note = a.notes.get(note_id)
    assert note is not None and note.hidden and note.collapsed


def test_a_note_held_is_read_again_though_no_device_tells_of_it(
    pair: tuple[Device, Device],
) -> None:
    a, b = pair
    note_id = shared(a, b)
    b.notes.update_body(note_id, "회의 10시", keep_earlier=True)
    b.sync()
    a.editing.begin(note_id)
    a.full = False
    held = a.sync().held
    assert held == [note_id]
    a.editing.end(note_id)

    a.sync(again=held)  # its rounds told of nothing new since

    assert a.notes.get(note_id).body == "회의 10시"  # pyright: ignore[reportOptionalMemberAccess]


def test_an_edit_begins_only_once_a_note_being_taken_in_is_in() -> None:
    editing = Editing()
    begun = threading.Event()

    def begin() -> None:
        editing.begin("note")
        begun.set()

    with editing.taking() as edited:
        assert not edited("note")
        thread = threading.Thread(target=begin)
        thread.start()
        assert not begun.wait(0.2)  # waits for the taking in to be over
        assert not edited("note")
    thread.join(5)
    assert begun.is_set()
    with editing.taking() as edited:
        assert edited("note")
    editing.end("note")
    with editing.taking() as edited:
        assert not edited("note")


def test_what_was_told_of_a_note_held_is_told_again(pair: tuple[Device, Device]) -> None:
    """With no one asking for it again, a round that only reads what the other
    devices told of still gets to a note it held (what was told is not taken
    as read)."""
    a, b = pair
    note_id = shared(a, b)
    a.full = b.full = False
    b.notes.update_body(note_id, "회의 10시", keep_earlier=True)
    b.sync()
    a.editing.begin(note_id)
    assert a.sync().held == [note_id]
    a.editing.end(note_id)

    a.sync()

    assert a.notes.get(note_id).body == "회의 10시"  # pyright: ignore[reportOptionalMemberAccess]
