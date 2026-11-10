"""A conflict copy as every device keeps it: which note it copies, whose text it
holds and when that was edited, the same on each; dealt with on one device
(both kept, its text put in the original, put in the trash), so on all. A
note that is no copy goes as it always went."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, FakeCloud
from sync_bed import KEYS, START, VAULT, Clock, Device, cloud_notes, settle

from stickle.data import schema
from stickle.data.versions import VersionRepository
from stickle.sync.contents import Contents, Field
from stickle.sync.format import NOTES, item_path, seal
from stickle.sync.merge import COPY_OF
from stickle.sync.note_file import NoteFile


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


def apart(a: Device, b: Device) -> tuple[str, str]:
    """One note changed on both, b's later: (the note, its copy holding a's text)."""
    note = a.notes.create("회의 9시")
    settle([a, b])
    a.clock.tick()
    a.notes.update_body(note.id, "회의 10시", keep_earlier=True)
    a.clock.tick()
    b.notes.update_body(note.id, "회의 11시", keep_earlier=True)
    settle([a, b])
    copies = [each for each in a.texts() if each != note.id]
    assert len(copies) == 1
    return note.id, copies[0]


def test_both_devices_have_the_copy_as_one(pair: tuple[Device, Device]) -> None:
    a, b = pair
    note_id, copy_id = apart(a, b)

    assert a.texts() == b.texts() == {note_id: "회의 11시", copy_id: "회의 10시"}
    there = a.notes.get(copy_id)
    assert there is not None and there.conflict is not None
    assert there.conflict.note == note_id
    assert there.conflict.device is None  # a's own text
    assert there.conflict.edited == START + 1_000
    assert b.copy_of(copy_id) == a.copy_of(copy_id) == (note_id, a.id, START + 1_000)
    for device in pair:
        [copy] = device.notes.copies()
        assert (copy.id, copy.original, copy.plain) == (copy_id, True, True)
    assert a.notes.get(note_id).conflict is None  # pyright: ignore[reportOptionalMemberAccess]


def test_keeping_both_makes_it_an_ordinary_note_everywhere(
    pair: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = pair
    note_id, copy_id = apart(a, b)
    a.clock.tick()

    b.notes.keep_both(copy_id)
    settle([a, b])

    for device in pair:
        assert device.notes.copies() == []
        assert device.copy_of(copy_id) is None
    assert a.texts() == {note_id: "회의 11시", copy_id: "회의 10시"}
    mark = cloud_notes(cloud)[copy_id].contents.fields[COPY_OF]
    assert mark.value is None and mark.device == b.id


def test_using_the_copy_trades_the_texts_everywhere(
    pair: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = pair
    note_id, copy_id = apart(a, b)
    a.clock.tick()

    original = b.notes.use_copy(copy_id)
    assert original.body == "회의 10시"
    settle([a, b])

    for device in pair:
        note, copy = device.notes.get(note_id), device.notes.get(copy_id)
        assert note is not None and copy is not None
        assert (note.body, note.deleted_at) == ("회의 10시", None)
        assert copy.body == "회의 11시" and copy.deleted_at is not None
        assert device.notes.copies() == []  # in the trash: its original unmarked
        # The text it holds now, b's, edited when b wrote it.
        assert device.copy_of(copy_id) == (note_id, b.id, START + 2_000)
    assert a.shown() == b.shown()
    assert cloud_notes(cloud)[note_id].body.text == "회의 10시"
    earlier = VersionRepository(b.db, clock=b.stamp).listed(note_id)
    assert earlier[0].first_line == "회의 11시" and earlier[0].came_from is None
    rows = b.db.execute("SELECT text_from FROM notes WHERE id = ?", (note_id,)).fetchall()
    assert rows == [(a.id,)]  # the text a wrote

    a.clock.tick()
    taken_in = a.clock.now - 1_000  # when a took in what b put in the original
    a.notes.restore(copy_id)  # taken out of the trash: it can change them back
    a.notes.use_copy(copy_id)
    settle([a, b])
    for device in pair:
        assert device.texts() == {note_id: "회의 11시", copy_id: "회의 10시"}
        # The text the original had on a: as b put it there, when it came.
        assert device.copy_of(copy_id) == (note_id, b.id, taken_in)


def test_the_copy_in_the_trash_takes_the_mark_off_and_back(pair: tuple[Device, Device]) -> None:
    a, b = pair
    note_id, copy_id = apart(a, b)
    a.clock.tick()

    a.notes.delete(copy_id)
    settle([a, b])
    assert a.notes.copies() == b.notes.copies() == []

    a.clock.tick()
    b.notes.restore(copy_id)
    settle([a, b])
    for device in pair:
        assert [each.id for each in device.notes.copies()] == [copy_id]
        assert device.copy_of(copy_id) == (note_id, a.id, START + 1_000)


def test_with_its_original_gone_the_copy_says_so(pair: tuple[Device, Device]) -> None:
    a, b = pair
    note_id, copy_id = apart(a, b)
    a.clock.tick()

    a.notes.delete(note_id)
    settle([a, b])
    for device in pair:
        [copy] = device.notes.copies()
        assert (copy.id, copy.original) == (copy_id, False)

    a.clock.tick()
    a.notes.purge(note_id)
    settle([a, b])
    for device in pair:
        [copy] = device.notes.copies()
        assert (copy.id, copy.original, copy.plain) == (copy_id, False, False)


def test_notes_that_are_no_copies_go_as_they_always_went(
    pair: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = pair
    note_id, copy_id = apart(a, b)
    others = [a.notes.create(f"보통 메모 {i}").id for i in range(3)]
    a.notes.set_color(others[0], "blue")
    settle([a, b])
    files = cloud_notes(cloud)
    for each in [note_id, *others]:
        assert COPY_OF not in files[each].contents.fields
    before = cloud.files()

    for device in pair:  # started again, every folder listed: nothing to put
        device.reopen()
        device.clock.tick()
        report = device.sync()
        assert report.put == [] and report.changed == []
    assert {p: d for p, d in cloud.files().items() if p.startswith("notes/")} == {
        p: d for p, d in before.items() if p.startswith("notes/")
    }
    assert copy_id in files


def test_a_copy_made_before_this_was_kept_is_marked_at_the_next_round(
    pair: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = pair
    _, copy_id = apart(a, b)
    expected = a.copy_of(copy_id)
    # As the version before left it: the mark in its file only.
    a.db.execute(
        "UPDATE notes SET conflict_of = NULL, conflict_device = NULL, conflict_edited = NULL"
    )
    a.db.execute("DELETE FROM note_stamps WHERE field = ?", (COPY_OF,))
    upgrade = schema.V25[schema.V25.index("UPDATE sync_notes") :]
    a.db.execute(upgrade)
    before = cloud.files()
    a.clock.tick()

    report = a.sync()

    assert a.copy_of(copy_id) == expected
    assert report.put == []
    assert {p: d for p, d in cloud.files().items() if p.startswith("notes/")} == {
        p: d for p, d in before.items() if p.startswith("notes/")
    }


def test_a_mark_of_another_shape_stays_in_the_file_as_it_is(
    pair: tuple[Device, Device], cloud: FakeCloud
) -> None:
    a, b = pair
    note_id, copy_id = apart(a, b)
    copy = cloud_notes(cloud)[copy_id]
    mark = copy.contents.fields[COPY_OF]
    assert isinstance(mark.value, dict)
    newer = Field({**mark.value, "why": "a newer version's"}, mark.hlc.following(), b.id)
    fields = {**copy.contents.fields, COPY_OF: newer}
    file = NoteFile(Contents(fields, copy.contents.others), copy.body)
    cloud.put_by_hand(item_path(NOTES, copy_id), seal(KEYS, VAULT, NOTES, copy_id, file.to_json()))
    a.clock.tick()

    a.sync()
    a.clock.tick()
    a.notes.set_color(copy_id, "pink")  # written again from here
    a.sync()

    assert a.copy_of(copy_id) == (note_id, a.id, START + 1_000)  # as it was: not taken
    assert cloud_notes(cloud)[copy_id].contents.fields[COPY_OF].value == newer.value
