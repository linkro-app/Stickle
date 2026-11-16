"""A computer that had secret notes of its own before it synced with others
that have theirs: its notes stay there until the two passwords are matched,
the others' key kept beside its own meanwhile (their notes open there with
their password). Matched, its notes are sealed again with the others' key and
go to them, open there with their password, and its own key is kept among the
old ones, so that its earlier texts and a backup from before open still with
the password of now; a note of such a backup, gone back to, goes nowhere until
it is sealed again with the key of now. Stopped halfway, every note opens.

Two computers that sync already and each turn secret notes on while offline
come to the same: the first to put its key makes the vault's, the other
matches. On a server that does not keep to conditional writes both may make
it, the notes of the one that lost going to the vault sealed with its own key
all the same: that one puts its key's slots in the vault (a computer whose
notes never went there does not), so that the others keep it beside theirs
and open those notes with its password, or with the recovery key they share
once it is gone; its password typed, they seal its notes again with theirs.
The cloud and the logs never hold a secret note's text in the clear."""

import logging
from collections.abc import Iterator
from functools import partial
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, FakeCloud
from sync_bed import KEY, KEYS, VAULT, Clock, Device, cloud_notes

from stickle.core.note import Note
from stickle.crypto import recovery, secret_notes
from stickle.crypto.key_files import C, KeyFiles
from stickle.crypto.keyfile import WrongPasswordError
from stickle.crypto.sealed import SealedSlot
from stickle.crypto.secret_notes import open_sealed, open_with_any, seal_text
from stickle.data import backups, sync_state
from stickle.data.database import open_database
from stickle.data.key_copies import KeyCopies
from stickle.data.notes import NoteRepository, adopt_sealed, match_sealed
from stickle.data.versions import VersionRepository
from stickle.sync.format import NOTES, RECORDS, item_at, item_path, json_bytes, open_envelope
from stickle.sync.note_file import Secret
from stickle.sync.record_file import RecordFile
from stickle.sync.secret_key import record_of

FAST = (1, 8192)
THEIRS = "the secret notes' password"  # of the computers that sync
OWN = "a password of its own"  # of the computer that joins them
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
    turn_on(device, THEIRS)
    device.sync()
    yield device
    device.close()


@pytest.fixture
def c(tmp_path: Path, cloud: FakeCloud, clock: Clock, a: Device) -> Iterator[Device]:
    """A computer with secret notes of its own, about to join a (not synced yet)."""
    device = Device("c", tmp_path, cloud, clock, whole=True, keyed=True)
    files(device).keep_in(KeyCopies(device.db))  # as the app does: a copy in the database too
    turn_on(device, OWN)
    yield device
    device.close()


def files(device: Device) -> KeyFiles:
    assert device.files is not None
    return device.files


def turn_on(device: Device, password: str) -> bytes:
    pair = recovery.read_recovery(files(device))
    assert isinstance(pair, SealedSlot)
    device.clock.tick()
    return secret_notes.create(files(device), password, pair.public, device.stamp(), FAST)


def key_of(device: Device, password: str) -> bytes:
    return secret_notes.open_with_password(files(device), password)


def number_of(device: Device) -> str | None:
    return secret_notes.key_number(files(device).peek(C))


def made_secret(device: Device, text: str, password: str) -> str:
    device.clock.tick()
    note = device.notes.create(text)
    device.notes.make_secret(note.id, "은행", seal_text(key_of(device, password), note.id, text))
    return note.id


def edited(device: Device, note_id: str, text: str, password: str) -> None:
    """Opened and saved again (the first save of an edit: the text it had kept)."""
    device.clock.tick()
    sealed = seal_text(key_of(device, password), note_id, text)
    device.notes.update_secret_body(note_id, sealed, keep_earlier=True)


def note_of(device: Device, note_id: str) -> Note:
    note = device.notes.get(note_id)
    assert note is not None
    return note


def opened_with(key: bytes, device: Device, note_id: str) -> str:
    sealed = note_of(device, note_id).sealed_text
    assert sealed is not None
    return open_sealed(key, note_id, sealed)


def settle(*devices: Device) -> None:
    for _ in range(2):
        for device in devices:
            device.clock.tick()
            device.sync()


def their_number(device: Device) -> str:
    """The others' key kept beside the device's own: there is one."""
    [number] = secret_notes.others(files(device))
    return number


def matched(device: Device, own: bytes | None, theirs: bytes) -> int:
    """The passwords matched, as the app matches them (stickle.app.notes.NoteManager.
    match_secret_keys): its notes sealed again with their key first, then C."""
    return match_sealed(
        device.notes,
        own,
        their_number(device),
        theirs,
        partial(secret_notes.old_keys, files(device)),
        partial(secret_notes.match, files(device)),
    )


def record_fields(cloud: FakeCloud) -> dict[str, object]:
    """The fields of the vault's record of the secret notes' key."""
    data = cloud.files()[item_path(RECORDS, record_of(KEYS))]
    _head, value = open_envelope(KEYS, data, VAULT, RECORDS)
    return dict(RecordFile.from_json(value).contents.fields)


def cloud_holds(cloud: FakeCloud, word: str) -> bool:
    """Whether a vault's file holds word, as it is or opened with the data key."""
    needle = word.encode("utf-8")
    for path, data in cloud.files().items():
        opened = b""
        at = item_at(path)
        if at is not None and at[0] in (NOTES, RECORDS):
            opened = json_bytes(open_envelope(KEYS, data, VAULT, at[0])[1])
        if needle in data or needle in opened:
            return True
    return False


# Before the passwords are matched


def test_its_own_notes_stay_and_the_others_key_waits_beside_its_own(a: Device, c: Device) -> None:
    from_a = made_secret(a, "PC의 비밀", THEIRS)
    own = made_secret(c, SECRET, OWN)

    settle(a, c, a)

    assert number_of(c) != number_of(a)
    assert their_number(c) == number_of(a)  # their key, kept beside, as the vault has it
    assert f"password.{number_of(c)}" not in record_fields(a.cloud)  # none of c's went: not put
    assert sync_state.sealed_here(c.db) == [own]  # what stays here
    assert own not in cloud_notes(a.cloud) and a.notes.get(own) is None
    # Their notes open here with their password, by their key kept beside.
    theirs = secret_notes.open_other_with_password(files(c), number_of(a) or "", THEIRS)
    assert opened_with(theirs, c, from_a) == "PC의 비밀"
    with pytest.raises(WrongPasswordError):
        secret_notes.open_other_with_password(files(c), number_of(a) or "", OWN)
    # Their recovery key (this computer's too, synced) opens their key too.
    pair = recovery.read_recovery(files(c))
    assert isinstance(pair, SealedSlot)
    secret = recovery.open_recovery_pair(pair, a.recovery_key)
    assert secret_notes.open_other_with_pair(files(c), number_of(a) or "", secret, pair.public) == (
        theirs
    )


def test_a_password_changed_on_the_others_is_kept_beside_as_changed(a: Device, c: Device) -> None:
    settle(a, c)
    a.clock.tick()
    key = key_of(a, THEIRS)
    secret_notes.change_password(files(a), key, "their new password", a.stamp(), FAST)

    settle(a, c)

    assert (
        secret_notes.open_other_with_password(files(c), their_number(c), "their new password")
        == key
    )


# Matched


def test_matched_its_notes_go_to_the_others_and_open_there_with_their_password(
    a: Device, c: Device
) -> None:
    own = made_secret(c, SECRET, OWN)
    trashed = made_secret(c, "휴지통의 비밀", OWN)
    c.notes.delete(trashed)
    settle(a, c)
    own_key = key_of(c, OWN)
    theirs = secret_notes.open_other_with_password(files(c), their_number(c), THEIRS)

    assert matched(c, own_key, theirs) == 2
    settle(c, a)

    assert number_of(c) == number_of(a)
    assert secret_notes.others(files(c)) == {}
    assert key_of(c, THEIRS) == key_of(a, THEIRS)  # one password for every computer now
    with pytest.raises(WrongPasswordError):
        key_of(c, OWN)
    assert opened_with(key_of(a, THEIRS), a, own) == SECRET
    assert note_of(a, trashed).deleted
    assert opened_with(key_of(a, THEIRS), a, trashed) == "휴지통의 비밀"
    assert sync_state.sealed_here(c.db) == []
    assert not cloud_holds(a.cloud, SECRET)
    # Edited after, it goes on going, sealed with the key of now.
    edited(c, own, f"{SECRET} 고침", THEIRS)
    settle(c, a)
    assert opened_with(key_of(a, THEIRS), a, own) == f"{SECRET} 고침"


def test_its_earlier_texts_open_with_the_password_of_now(a: Device, c: Device) -> None:
    own = made_secret(c, "처음 글", OWN)
    edited(c, own, "고친 글", OWN)
    settle(a, c)
    own_key = key_of(c, OWN)
    matched(c, own_key, secret_notes.open_other_with_password(files(c), their_number(c), THEIRS))

    versions = VersionRepository(c.db, clock=c.stamp)
    [earlier] = versions.listed(own)
    sealed = versions.text(own, earlier.id)
    assert not isinstance(sealed, str) and sealed is not None
    now = key_of(c, THEIRS)
    old = secret_notes.old_keys(files(c), now)
    assert list(old.values()) == [own_key]
    assert open_with_any([now, *old.values()], own, sealed) == "처음 글"


def test_a_backup_from_before_goes_nowhere_until_sealed_again_with_the_key_of_now(
    a: Device, c: Device
) -> None:
    own = made_secret(c, SECRET, OWN)
    settle(a, c)
    backup = backups.take(c.db, c.path)
    own_number = number_of(c)
    own_key = key_of(c, OWN)
    matched(c, own_key, secret_notes.open_other_with_password(files(c), their_number(c), THEIRS))
    settle(c, a)

    backups.stage_restore(c.path, backup)
    backups.keep_key_state(c.path, KEY, c.db)
    staged = open_database(c.path.with_name(c.path.name + backups.RESTORING), KEY)
    try:
        came_back = NoteRepository(staged).get(own)
        assert came_back is not None and came_back.key_number == own_number  # said so
        sealed = came_back.sealed_text
        assert sealed is not None
        now = key_of(c, THEIRS)
        assert open_with_any([now, *secret_notes.old_keys(files(c), now).values()], own, sealed)
    finally:
        staged.close()
    backups.cancel_restore(c.path)

    # Such a note goes nowhere until it is sealed again with the key of now.
    edited(c, own, f"{SECRET} 백업 전 고침", THEIRS)
    settle(c, a)
    with c.db:
        c.db.execute(
            "UPDATE notes SET secret_body = ?, secret_key_number = ? WHERE id = ?",
            (seal_text(own_key, own, "되돌린 글"), own_number, own),
        )
    c.db.execute("UPDATE notes SET change_seq = change_seq + 1000 WHERE id = ?", (own,))
    settle(c, a)
    assert opened_with(key_of(a, THEIRS), a, own) == f"{SECRET} 백업 전 고침"
    assert own in sync_state.sealed_here(c.db, secret_notes.old_numbers(files(c).peek(C)))

    now = key_of(c, THEIRS)
    old = secret_notes.old_keys(files(c), now)
    resealed = c.notes.reseal_old(
        old, partial(open_with_any, [*old.values(), now]), partial(seal_text, now)
    )
    assert resealed == 1
    settle(c, a)
    assert opened_with(key_of(a, THEIRS), a, own) == "되돌린 글"


def test_stopped_halfway_every_note_opens(a: Device, c: Device) -> None:
    own = made_secret(c, SECRET, OWN)
    settle(a, c)
    own_key = key_of(c, OWN)
    number = their_number(c)
    theirs = secret_notes.open_other_with_password(files(c), number, THEIRS)
    # Sealed again, then the computer went off before C was written.
    c.notes.reseal_here(partial(open_with_any, [own_key]), partial(seal_text, theirs), number)

    assert note_of(c, own).key_number == number  # sealed with theirs, and saying so
    assert opened_with(theirs, c, own) == SECRET  # their password opens it, by their key beside
    assert key_of(c, OWN) == own_key  # this computer's still opens with its own
    settle(c, a)
    assert opened_with(key_of(a, THEIRS), a, own) == SECRET  # and it went: it is theirs

    matched(c, key_of(c, OWN), theirs)  # done next time
    assert number_of(c) == number_of(a)
    now = key_of(c, THEIRS)
    assert own_key in secret_notes.old_keys(files(c), now).values()


def test_nothing_sealed_with_its_own_key_takes_theirs_as_it_is(a: Device, c: Device) -> None:
    settle(a, c)
    theirs = secret_notes.open_other_with_password(files(c), their_number(c), THEIRS)

    assert matched(c, None, theirs) == 0

    assert number_of(c) == number_of(a)
    assert secret_notes.old_keys(files(c), key_of(c, THEIRS)) == {}


# Two computers that sync turn secret notes on apart, offline


def test_two_computers_turning_them_on_offline_match_like_any_other(
    tmp_path: Path, cloud: FakeCloud, clock: Clock
) -> None:
    x = Device("x", tmp_path, cloud, clock, whole=True, keyed=True)
    x.made_the_vault()
    x.sync()
    y = Device("y", tmp_path, cloud, clock, whole=True, keyed=True)
    try:
        settle(x, y)
        assert x.files is not None and y.files is not None
        turn_on(x, THEIRS)
        turn_on(y, OWN)  # each on its own, offline
        from_x = made_secret(x, "x의 비밀", THEIRS)
        from_y = made_secret(y, SECRET, OWN)

        settle(x, y, x)  # x put its key first: the vault's

        assert their_number(y) == number_of(x)
        assert from_y not in cloud_notes(cloud)
        theirs = secret_notes.open_other_with_password(files(y), their_number(y), THEIRS)
        assert opened_with(theirs, y, from_x) == "x의 비밀"

        matched(y, key_of(y, OWN), theirs)
        settle(y, x)
        assert opened_with(key_of(x, THEIRS), x, from_y) == SECRET
        assert not cloud_holds(cloud, SECRET)
    finally:
        y.close()
        x.close()


# A key used apart: two computers made the vault's key at once


APART = "x's own password"


@pytest.fixture
def apart(tmp_path: Path, cloud: FakeCloud, clock: Clock) -> Iterator[tuple[Device, Device, str]]:
    """x and y sync; each turned secret notes on offline; x's note went to the
    vault sealed with its key, then y made the vault's key over x's (as on a
    server that does not keep to conditional writes). x's note's id."""
    x = Device("x", tmp_path, cloud, clock, whole=True, keyed=True)
    x.made_the_vault()
    x.sync()
    y = Device("y", tmp_path, cloud, clock, whole=True, keyed=True)
    try:
        settle(x, y)
        turn_on(x, APART)
        turn_on(y, THEIRS)
        from_x = made_secret(x, SECRET, APART)
        x.sync()  # the vault's key x's, as x saw it: its note goes
        assert from_x in cloud_notes(cloud)
        cloud.remove_by_hand(item_path(RECORDS, record_of(KEYS)))
        made_secret(y, "y의 비밀", THEIRS)
        y.sync()  # y's key made the vault's, over x's
        settle(x, y)
        yield x, y, from_x
    finally:
        y.close()
        x.close()


def test_a_key_used_apart_is_put_in_the_vault_and_kept_beside_by_who_holds_its_notes(
    apart: tuple[Device, Device, str],
) -> None:
    x, y, from_x = apart
    x_number = number_of(x)
    assert x_number is not None and x_number != number_of(y)

    fields = record_fields(x.cloud)
    assert f"password.{x_number}" in fields and f"recovery.{x_number}" in fields
    assert note_of(y, from_x).key_number == x_number  # came as it went, sealed with x's key
    kept = secret_notes.others(files(y))[x_number]
    assert not secret_notes.is_the_vaults(kept)
    x_key = key_of(x, APART)
    assert secret_notes.open_other_with_password(files(y), x_number, APART) == x_key
    assert opened_with(x_key, y, from_x) == SECRET
    assert their_number(x) == number_of(y)  # and x keeps y's, the vault's, to match with


def test_without_that_computer_the_recovery_key_opens_its_notes_and_they_move(
    apart: tuple[Device, Device, str],
) -> None:
    x, y, from_x = apart
    x_number = number_of(x) or ""
    x_key = key_of(x, APART)
    x.close()  # lost

    pair = recovery.read_recovery(files(y))
    assert isinstance(pair, SealedSlot)
    secret = recovery.open_recovery_pair(pair, x.recovery_key)  # the one they share
    found = secret_notes.open_other_with_pair(files(y), x_number, secret, pair.public)
    assert found == x_key

    own = key_of(y, THEIRS)
    moved = adopt_sealed(y.notes, own, x_number, found, partial(secret_notes.adopt, files(y)))
    assert moved == 1
    assert note_of(y, from_x).key_number is None and opened_with(own, y, from_x) == SECRET
    assert x_number not in secret_notes.others(files(y))
    assert secret_notes.old_keys(files(y), own) == {x_number: x_key}
    y.clock.tick()
    y.sync()
    [file] = [each for path, each in cloud_notes(y.cloud).items() if path == from_x]
    assert isinstance(file.body.text, Secret) and file.body.text.key == number_of(y)
    assert not cloud_holds(y.cloud, SECRET)


def test_moved_on_one_and_matched_on_the_other_no_text_is_lost(
    apart: tuple[Device, Device, str],
) -> None:
    x, y, from_x = apart
    x_number = number_of(x) or ""
    own = key_of(y, THEIRS)
    x_key = secret_notes.open_other_with_password(files(y), x_number, APART)
    adopt_sealed(y.notes, own, x_number, x_key, partial(secret_notes.adopt, files(y)))
    matched(
        x,
        key_of(x, APART),
        secret_notes.open_other_with_password(files(x), their_number(x), THEIRS),
    )

    settle(x, y, x, y)

    for device in (x, y):
        assert opened_with(key_of(device, THEIRS), device, from_x) == SECRET
        copies = [each for each in device.notes.copies() if each.conflict.note == from_x]
        for copy in copies:  # sealed again on both: a copy may come, the same text
            assert opened_with(key_of(device, THEIRS), device, copy.id) == SECRET
    assert number_of(x) == number_of(y)


# Nothing in the clear


def test_no_log_tells_a_secret_note_or_a_key(
    a: Device, c: Device, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    own = made_secret(c, SECRET, OWN)
    settle(a, c)
    matched(
        c, key_of(c, OWN), secret_notes.open_other_with_password(files(c), their_number(c), THEIRS)
    )
    settle(c, a)

    assert opened_with(key_of(a, THEIRS), a, own) == SECRET
    assert SECRET not in caplog.text
    assert "은행" not in caplog.text
    assert not cloud_holds(a.cloud, SECRET)
