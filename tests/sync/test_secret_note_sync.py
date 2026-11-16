"""Secret notes synced: each goes as it is sealed, and comes to the user's
other computers locked, by its title, to open with the same password there;
its reminders written in it ring there all the same. Edited on one, it changes
on the others; edited apart, both keep the note and a conflict copy alike,
which opens as the note did and is sealed for itself once saved, and whose
text can change places with the note's while both are locked. Made ordinary
or secret on one, it is so on the others, and one made secret leaves none of
its text in the clear anywhere: what it held in the clear on another computer
is sealed there into its key's box, which needs no key, its earlier texts kept
so till the key is held there (sealed again as any other then), and waits for
a box that is not there yet; made ordinary, its earlier texts sealed open once
the key is held. The trash, emptying it, a year in it and a big
change count secret notes too. One sealed with a key of a computer's own
stays there until its key is matched with the others'.

The cloud never holds a secret note's text in the clear, nor does a computer
it is locked on. Sync never needs the key: a computer the note is locked on
merges what one it is open on merges (each device here merges with no key at
hand, and opens with it only to check)."""

import logging
from collections.abc import Iterator
from datetime import datetime, time
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, FakeCloud
from sync_bed import KEYS, VAULT, Clock, Device, cloud_notes

from stickle.core.note import Note, SealedText
from stickle.core.text_reminders import due
from stickle.crypto import recovery, secret_notes
from stickle.crypto.key_files import C, KeyFiles
from stickle.crypto.sealed import SealedSlot
from stickle.crypto.secret_notes import BOX, SecretNoteError, open_sealed, seal_text
from stickle.data import sync_state, versions
from stickle.data.notes import NoteIsSecretError
from stickle.sync.contents import Contents
from stickle.sync.engine import BigChangeError, cloud_only, first_look, joined
from stickle.sync.format import (
    NOTES,
    RECORDS,
    item_at,
    item_path,
    json_bytes,
    open_envelope,
    seal,
)
from stickle.sync.merge import YEAR
from stickle.sync.note_file import Secret
from stickle.sync.record_file import RecordFile
from stickle.sync.secret_key import BOXES, record_of

FAST = (1, 8192)
PASSWORD = "the secret notes' password"
MORNING = time(9)
SECRET = "통장 비밀번호 4821"  # what must never be found in the clear


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud(DROPBOX)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def a(tmp_path: Path, cloud: FakeCloud, clock: Clock) -> Iterator[Device]:
    """The device that made the vault, its secret notes turned on."""
    device = Device("a", tmp_path, cloud, clock, whole=True, keyed=True)
    device.made_the_vault()
    turn_on(device)
    device.sync()
    yield device
    device.close()


@pytest.fixture
def b(tmp_path: Path, cloud: FakeCloud, clock: Clock, a: Device) -> Iterator[Device]:
    """Another computer, without secret notes: it takes the vault's key."""
    device = Device("b", tmp_path, cloud, clock, whole=True, keyed=True)
    device.sync()
    assert key_of(device) == key_of(a)
    yield device
    device.close()


def files(device: Device) -> KeyFiles:
    assert device.files is not None
    return device.files


def turn_on(device: Device, password: str = PASSWORD) -> bytes:
    pair = recovery.read_recovery(files(device))
    assert isinstance(pair, SealedSlot)
    device.clock.tick()
    return secret_notes.create(files(device), password, pair.public, device.stamp(), FAST)


def key_of(device: Device, password: str = PASSWORD) -> bytes:
    return secret_notes.open_with_password(files(device), password)


def number_of(device: Device) -> str | None:
    return secret_notes.key_number(files(device).peek(C))


def made_secret(device: Device, text: str, title: str = "은행") -> str:
    """A note written and made secret, its reminders following its text (as the app does)."""
    device.clock.tick()
    note = device.notes.create(text)
    key = key_of(device)
    device.notes.make_secret(note.id, title, seal_text(key, note.id, text))
    device.reminders.match_text(note.id, due(text, MORNING).keys())
    return note.id


def edited(device: Device, note_id: str, text: str) -> None:
    """Opened, edited and saved: sealed for the note itself, here."""
    device.clock.tick()
    device.notes.update_secret_body(note_id, seal_text(key_of(device), note_id, text))
    device.reminders.match_text(note_id, due(text, MORNING).keys())


def note_of(device: Device, note_id: str) -> Note:
    note = device.notes.get(note_id)
    assert note is not None
    return note


def opened(device: Device, note_id: str) -> str:
    """The note's text, opened with the password on that computer."""
    sealed = note_of(device, note_id).sealed_text
    assert sealed is not None
    return open_sealed(key_of(device), note_id, sealed)


def in_the_clear(device: Device, word: str) -> list[str]:
    """The tables of the device's database any value of which holds word."""
    found: list[str] = []
    needle = word.encode("utf-8")
    for (table,) in device.db.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        for row in device.db.execute(f'SELECT * FROM "{table}"').fetchall():
            for value in row:
                data = value if isinstance(value, bytes) else str(value).encode("utf-8")
                if needle in data:
                    found.append(str(table))
    return found


def cloud_holds(cloud: FakeCloud, word: str) -> list[str]:
    """The vault's files that hold word, as they are and opened with the data key."""
    needle = word.encode("utf-8")
    found: list[str] = []
    for path, data in cloud.files().items():
        opened_too = b""
        at = item_at(path)
        if at is not None and at[0] in (NOTES, RECORDS):
            _, value = open_envelope(KEYS, data, VAULT, at[0])
            opened_too = json_bytes(value)
        if needle in data or needle in opened_too:
            found.append(path)
    return found


def earlier_texts(device: Device, note_id: str, key: bytes) -> list[str]:
    """The note's earlier texts on that computer, each opened with key
    (sealed as any other, or into the key's box), oldest first; none in the clear."""
    rows = device.db.execute(
        "SELECT sealed, boxed FROM note_versions WHERE note_id = ? ORDER BY kept_at, id",
        (note_id,),
    ).fetchall()
    texts: list[str] = []
    for sealed, boxed in rows:
        assert sealed is not None, "an earlier text in the clear"
        held = SealedText(bytes(sealed), boxed=bool(boxed))  # pyright: ignore[reportArgumentType]
        texts.append(open_sealed(key, note_id, held))
    return texts


def boxed_count(device: Device, note_id: str) -> int:
    rows = device.db.execute(
        "SELECT count(*) FROM note_versions WHERE note_id = ? AND boxed", (note_id,)
    ).fetchall()
    return int(rows[0][0])  # pyright: ignore[reportArgumentType]


def as_0_6_made_it(*devices: Device) -> None:
    """The secret notes' key without its box, here and in the vault (a key
    0.6 made, which no computer opened since)."""
    for device in devices:
        slots = dict(files(device).load(C) or {})
        slots.pop(BOX, None)
        files(device).save(C, slots)
    cloud = devices[0].cloud
    path = item_path(RECORDS, record_of(KEYS))
    _head, value = open_envelope(KEYS, cloud.files()[path], VAULT, RECORDS)
    record = RecordFile.from_json(value)
    fields = {n: f for n, f in record.contents.fields.items() if not n.startswith(BOXES)}
    kept = RecordFile(record.kind, record.item, Contents(fields, record.contents.others))
    cloud.put_by_hand(path, seal(KEYS, VAULT, RECORDS, record_of(KEYS), kept.to_json()))


def settle(*devices: Device) -> None:
    for _ in range(2):
        for device in devices:
            device.clock.tick()
            device.sync()


# Coming to the other computer


def test_a_secret_note_comes_locked_by_its_title_and_opens_with_the_password(
    a: Device, b: Device
) -> None:
    note_id = made_secret(a, f"{SECRET}\n⏰2025-11-20 18:30 은행 가기")

    settle(a, b)

    there = note_of(b, note_id)
    assert there.secret and there.body == "" and there.secret_title == "은행"
    assert there.sealed == note_of(a, note_id).sealed  # as it was sealed, never opened on the way
    assert (there.bound_to, there.key_number) == (None, number_of(a))
    assert opened(b, note_id) == f"{SECRET}\n⏰2025-11-20 18:30 은행 가기"
    # Its reminder rings there, locked as it is.
    assert b.reminders.text_times(note_id) == [datetime(2025, 11, 20, 18, 30)]
    assert in_the_clear(b, SECRET) == []
    assert cloud_holds(a.cloud, SECRET) == []
    [file] = [each for path, each in cloud_notes(a.cloud).items() if path == note_id]
    assert isinstance(file.body.text, Secret)
    assert file.body.text.reminders == ("2025-11-20 18:30",)
    assert file.contents.fields["secret_title"].value == "은행"


def test_edits_go_both_ways(a: Device, b: Device) -> None:
    note_id = made_secret(a, "처음 글")
    settle(a, b)

    edited(b, note_id, "노트북에서 고침")
    settle(b, a)
    assert opened(a, note_id) == "노트북에서 고침"
    assert note_of(a, note_id).bound_to is None

    edited(a, note_id, "PC에서 다시 고침")
    a.notes.set_secret_title(note_id, "새 제목")
    settle(a, b)
    assert opened(b, note_id) == "PC에서 다시 고침"
    assert note_of(b, note_id).secret_title == "새 제목"
    assert not b.notes.copies()


# Edited apart


def test_edited_apart_both_have_the_same_copy_which_opens_as_the_note_did(
    a: Device, b: Device
) -> None:
    note_id = made_secret(a, "처음 글")
    settle(a, b)
    edited(a, note_id, "PC의 글")
    edited(b, note_id, "노트북의 글")

    settle(a, b, a)

    [copy] = a.notes.copies()
    assert [each.id for each in b.notes.copies()] == [copy.id]
    assert copy.swappable and copy.conflict.note == note_id
    for device in (a, b):
        kept = note_of(device, copy.id)
        assert kept.secret and kept.bound_to == note_id  # sealed for the note it copies
        assert kept.sealed == note_of(a, copy.id).sealed
        assert note_of(device, note_id).sealed == note_of(a, note_id).sealed
    texts = {opened(a, note_id), opened(a, copy.id)}
    assert texts == {opened(b, note_id), opened(b, copy.id)} == {"PC의 글", "노트북의 글"}
    assert in_the_clear(a, "노트북의 글") == in_the_clear(b, "PC의 글") == []


def test_a_copy_saved_is_sealed_for_itself(a: Device, b: Device) -> None:
    note_id = made_secret(a, "처음 글")
    settle(a, b)
    edited(a, note_id, "PC의 글")
    edited(b, note_id, "노트북의 글")
    settle(a, b, a)
    [copy] = b.notes.copies()

    edited(b, copy.id, opened(b, copy.id) + " 그리고 더")
    settle(b, a)

    for device in (a, b):
        assert note_of(device, copy.id).bound_to is None
        assert opened(device, copy.id).endswith(" 그리고 더")


def test_a_copy_changes_places_with_its_note_while_both_are_locked(a: Device, b: Device) -> None:
    note_id = made_secret(a, "처음 글")
    settle(a, b)
    edited(a, note_id, "PC의 글 ⏰2025-11-21 08:00")
    edited(b, note_id, "노트북의 글 ⏰2025-11-22 07:00")
    settle(a, b, a)
    [copy] = b.notes.copies()
    copy_text, note_text = opened(b, copy.id), opened(b, note_id)
    copy_times = b.reminders.text_times(copy.id)

    b.notes.use_copy(copy.id)  # no key at hand
    settle(b, a)

    for device in (a, b):
        assert opened(device, note_id) == copy_text
        assert opened(device, copy.id) == note_text
        assert note_of(device, copy.id).deleted_at is not None
        assert device.reminders.text_times(note_id) == copy_times
    assert not a.notes.copies()


# Made ordinary, made secret


def test_made_ordinary_on_one_is_ordinary_on_the_other(a: Device, b: Device) -> None:
    note_id = made_secret(a, "비밀이던 글")
    settle(a, b)
    a.clock.tick()
    a.notes.make_ordinary(note_id, "비밀이던 글", None)

    settle(a, b)

    there = note_of(b, note_id)
    assert (there.secret, there.body, there.secret_title) == (False, "비밀이던 글", None)
    # Its text sealed stays among its earlier ones, sealed (opened once it is opened there).
    listed = versions.VersionRepository(b.db, clock=b.stamp).listed(note_id)
    assert [v.sealed for v in listed] == [True]

    assert b.notes.sealed_earlier(note_id)
    assert b.notes.open_earlier({None: key_of(b)}) == 1  # the key held there, at last
    assert versions.plain_count(b.db, note_id) == 1  # the text it has now: not listed
    assert not b.notes.sealed_earlier(note_id)


def test_made_secret_on_one_leaves_none_of_its_text_in_the_clear_on_the_other(
    a: Device, b: Device
) -> None:
    note = a.notes.create(f"{SECRET} 첫 글")
    settle(a, b)
    b.clock.tick()
    b.notes.update_body(note.id, f"{SECRET} 둘째 글", keep_earlier=True)
    settle(b, a)
    assert versions.plain_count(b.db, note.id) == 1  # its earlier text, in the clear
    key = key_of(a)
    a.clock.tick()
    a.notes.make_secret(
        note.id,
        "통장",
        seal_text(key, note.id, f"{SECRET} 둘째 글"),
        lambda text: seal_text(key, note.id, text),
    )

    settle(a, b)

    there = note_of(b, note.id)
    assert there.secret and there.secret_title == "통장"
    assert opened(b, note.id) == f"{SECRET} 둘째 글"
    assert versions.plain_count(b.db, note.id) == 0
    assert in_the_clear(b, SECRET) == in_the_clear(a, SECRET) == []
    assert b.notes.matching(SECRET) == set()
    assert cloud_holds(a.cloud, SECRET) == []
    # Its earlier text there, and the one it had, sealed into its key's box: kept, not lost.
    assert boxed_count(b, note.id) == 2
    assert earlier_texts(b, note.id, key_of(b)) == [f"{SECRET} 첫 글", f"{SECRET} 둘째 글"]


def test_made_secret_elsewhere_its_earlier_texts_are_sealed_as_any_other_once_the_key_is_held(
    a: Device, b: Device
) -> None:
    note = a.notes.create(f"{SECRET} 첫 글")
    settle(a, b)
    b.clock.tick()
    b.notes.update_body(note.id, f"{SECRET} 둘째 글", keep_earlier=True)
    settle(b, a)
    key = key_of(a)
    a.clock.tick()
    a.notes.make_secret(
        note.id,
        "통장",
        seal_text(key, note.id, f"{SECRET} 둘째 글"),
        lambda text: seal_text(key, note.id, text),
    )
    settle(a, b)
    assert boxed_count(b, note.id) == 2

    assert b.notes.open_earlier({None: key_of(b)}) == 2

    assert boxed_count(b, note.id) == 0
    assert earlier_texts(b, note.id, key_of(b)) == [f"{SECRET} 첫 글", f"{SECRET} 둘째 글"]
    assert in_the_clear(b, SECRET) == []
    earlier = versions.VersionRepository(b.db, clock=b.stamp).listed(note.id)[-1]  # oldest
    b.clock.tick()
    assert opened(b, b.notes.restore_version(note.id, earlier.id).id) == f"{SECRET} 첫 글"


def test_a_box_sealed_text_goes_back_only_once_sealed_as_any_other(a: Device, b: Device) -> None:
    note = a.notes.create("첫 글")
    settle(a, b)
    key = key_of(a)
    a.clock.tick()
    a.notes.make_secret(note.id, "통장", seal_text(key, note.id, "첫 글 고침"))
    settle(a, b)
    earlier = versions.VersionRepository(b.db, clock=b.stamp).listed(note.id)[0]

    with pytest.raises(NoteIsSecretError):  # it would go to the vault sealed so
        b.notes.restore_version(note.id, earlier.id)
    assert opened(b, note.id) == "첫 글 고침"


def test_made_secret_with_a_key_0_6_made_it_waits_for_the_keys_box(
    a: Device, b: Device, caplog: pytest.LogCaptureFixture
) -> None:
    as_0_6_made_it(a, b)
    note = a.notes.create(f"{SECRET} 첫 글")
    settle(a, b)
    b.clock.tick()
    b.notes.update_body(note.id, f"{SECRET} 둘째 글", keep_earlier=True)
    settle(b, a)
    key = key_of(a)
    a.clock.tick()
    a.notes.make_secret(
        note.id,
        "통장",
        seal_text(key, note.id, f"{SECRET} 둘째 글"),
        lambda text: seal_text(key, note.id, text),
    )
    a.sync()

    caplog.set_level(logging.INFO)
    b.clock.tick()
    report = b.sync()

    assert note.id in report.left  # nothing of it lost, nothing new shown
    there = note_of(b, note.id)
    assert (there.secret, there.body) == (False, f"{SECRET} 둘째 글")
    assert versions.plain_count(b.db, note.id) == 1
    assert "waits for its key's box" in caplog.text
    assert SECRET not in caplog.text

    assert secret_notes.keep_box(files(a), key)  # opened there in 0.7
    settle(a, b)

    there = note_of(b, note.id)
    assert there.secret and opened(b, note.id) == f"{SECRET} 둘째 글"
    assert earlier_texts(b, note.id, key_of(b)) == [f"{SECRET} 첫 글", f"{SECRET} 둘째 글"]
    assert in_the_clear(b, SECRET) == []
    assert secret_notes.box_of(files(b).load(C)) == secret_notes.box_public(key)  # taken


# Made secret on one computer while its text was edited on another in the clear


def made_secret_meanwhile(a: Device, note_id: str, text: str) -> None:
    """Made secret on a, offline (its earlier ones sealed too, as the app does)."""
    key = key_of(a)
    a.clock.tick()
    a.notes.make_secret(
        note_id, "통장", seal_text(key, note_id, text), lambda each: seal_text(key, note_id, each)
    )


def copies_of(device: Device) -> list[Note]:
    return [note for note in device.notes.all() if note.conflict is not None]


def test_made_secret_while_edited_in_the_clear_the_secret_one_stays_and_no_copy_is_made(
    a: Device, b: Device
) -> None:
    note = a.notes.create(f"{SECRET} 첫 글")
    settle(a, b)
    made_secret_meanwhile(a, note.id, f"{SECRET} 첫 글")
    b.clock.tick()
    b.notes.update_body(note.id, f"{SECRET} 데스크톱에서 고침", keep_earlier=True)

    told_a = a.sync()
    told_b = b.sync()  # b merges: the secret one stays
    settle(a, b)

    assert note_of(a, note.id).secret and note_of(b, note.id).secret
    assert opened(a, note.id) == opened(b, note.id) == f"{SECRET} 첫 글"
    assert copies_of(a) == copies_of(b) == []
    assert told_a.let_go == []
    assert [(each.note, each.by) for each in told_b.let_go] == [(note.id, "A-PC 표식")]
    # What b wrote stays on b only, among its earlier texts, sealed.
    assert f"{SECRET} 데스크톱에서 고침" in earlier_texts(b, note.id, key_of(b))
    assert f"{SECRET} 데스크톱에서 고침" not in earlier_texts(a, note.id, key_of(a))
    assert in_the_clear(a, SECRET) == in_the_clear(b, SECRET) == []
    assert cloud_holds(a.cloud, SECRET) == []
    assert sync_state.get(a.db, VAULT, note.id) is not None and a.notes.get(note.id) is not None


def test_the_edit_in_the_clear_put_first_is_let_go_of_all_the_same_and_its_writer_told(
    a: Device, b: Device, tmp_path: Path, cloud: FakeCloud, clock: Clock
) -> None:
    c = Device("c", tmp_path, cloud, clock, whole=True, keyed=True)
    try:
        note = a.notes.create(f"{SECRET} 첫 글")
        settle(a, b, c)
        made_secret_meanwhile(a, note.id, f"{SECRET} 첫 글")
        b.clock.tick()
        b.notes.update_body(note.id, f"{SECRET} 데스크톱에서 고침", keep_earlier=True)
        b.sync()  # its edit in the vault first
        c.clock.tick()
        c.sync()  # and on another computer, in the clear

        told_a = a.sync()  # a merges: the secret one stays
        told_b = b.sync()
        told_c = c.sync()
        settle(a, b, c)

        for device in (a, b, c):
            assert note_of(device, note.id).secret
            assert opened(device, note.id) == f"{SECRET} 첫 글"
            assert copies_of(device) == []
            assert in_the_clear(device, SECRET) == []
        assert told_a.let_go == told_c.let_go == []
        assert [(each.note, each.by) for each in told_b.let_go] == [(note.id, "A-PC 표식")]
        assert f"{SECRET} 데스크톱에서 고침" in earlier_texts(b, note.id, key_of(b))
        assert f"{SECRET} 데스크톱에서 고침" in earlier_texts(c, note.id, key_of(c))
        assert cloud_holds(a.cloud, SECRET) == []
    finally:
        c.close()


def test_made_ordinary_on_one_while_edited_secret_on_the_other_both_are_kept(
    a: Device, b: Device
) -> None:
    note_id = made_secret(a, "비밀 글")
    settle(a, b)
    a.clock.tick()
    a.notes.make_ordinary(note_id, "이제 보통 글", None)
    edited(b, note_id, "비밀 글 고침")

    a.sync()
    told = b.sync()
    settle(a, b)

    assert told.let_go == []
    assert len(copies_of(a)) == len(copies_of(b)) == 1  # as any conflict: nothing let go of


# The trash


def test_the_trash_and_emptying_it_count_secret_notes_too(a: Device, b: Device) -> None:
    note_id = made_secret(a, "지울 글")
    settle(a, b)

    a.clock.tick()
    a.notes.delete(note_id)
    settle(a, b)
    assert note_of(b, note_id).deleted_at is not None

    a.clock.tick()
    a.notes.purge(note_id)
    settle(a, b)
    assert b.notes.get(note_id) is None


def test_a_year_in_the_trash_empties_a_secret_note_everywhere(a: Device, b: Device) -> None:
    note_id = made_secret(a, "오래된 글")
    settle(a, b)
    a.clock.tick()
    a.notes.delete(note_id)
    settle(a, b)

    a.clock.tick(YEAR + 60_000)
    settle(a, b)

    assert a.notes.get(note_id) is None and b.notes.get(note_id) is None


def test_secret_notes_count_toward_a_big_change(a: Device, b: Device) -> None:
    made = [made_secret(a, f"비밀 {n}") for n in range(10)]
    settle(a, b)
    for note_id in made:
        a.clock.tick()
        a.notes.delete(note_id)
    settle(a)
    b.accepts = False

    with pytest.raises(BigChangeError):
        b.sync()
    assert all(note_of(b, note_id).deleted_at is None for note_id in made)


# A key of a computer's own


def test_a_note_sealed_with_a_key_of_its_own_stays_on_its_computer(
    tmp_path: Path, cloud: FakeCloud, clock: Clock, a: Device
) -> None:
    from_a = made_secret(a, "PC의 비밀")
    settle(a)
    c = Device("c", tmp_path, cloud, clock, whole=True, keyed=True)
    try:
        turn_on(c, "another password of its own")
        own = c.notes.create("노트북의 비밀")
        own_key = secret_notes.open_with_password(files(c), "another password of its own")
        c.notes.make_secret(own.id, "따로", seal_text(own_key, own.id, "노트북의 비밀"))

        settle(c, a)

        assert own.id not in cloud_notes(cloud)  # matched with the vault's key first
        assert own.id in sync_state.changed(c.db, VAULT)  # and told as not gone yet
        assert a.notes.get(own.id) is None
        came = note_of(c, from_a)  # the vault's comes, sealed with its key
        assert came.secret and came.key_number == number_of(a) != number_of(c)
        sealed = came.sealed_text
        assert sealed is not None
        with pytest.raises(SecretNoteError):
            open_sealed(own_key, from_a, sealed)
    finally:
        c.close()


# Joining


def test_joining_folds_no_secret_note_and_keeping_the_clouds_drops_them(
    tmp_path: Path, cloud: FakeCloud, clock: Clock, a: Device
) -> None:
    made_secret(a, "같은 글")
    a.notes.create("보통 글")
    settle(a)
    d = Device("d", tmp_path, cloud, clock, whole=True, keyed=True)
    try:
        turn_on(d, "its own password")
        own = d.notes.create("같은 글")
        own_key = secret_notes.open_with_password(files(d), "its own password")
        d.notes.make_secret(own.id, "은행", seal_text(own_key, own.id, "같은 글"))
        joined(d.db, VAULT)

        look = first_look(d.db, d.place, clock, d.notes)
        assert (look.here, look.cloud, look.alike) == (1, 2, 0)
        assert None in look.recent  # a secret note's first line is not shown

        cloud_only(d.db, d.notes, VAULT, lambda: None)
        assert d.notes.get(own.id) is None
    finally:
        d.close()


# Nothing in the clear


def test_no_log_tells_a_secret_note(a: Device, b: Device, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    note_id = made_secret(a, SECRET)
    settle(a, b)
    edited(a, note_id, f"{SECRET} 고침")
    edited(b, note_id, f"{SECRET} 따로 고침")
    settle(a, b, a)

    assert SECRET not in caplog.text
    assert "은행" not in caplog.text
    assert cloud_holds(a.cloud, SECRET) == []
