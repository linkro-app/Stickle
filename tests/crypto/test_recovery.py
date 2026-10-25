"""The recovery key: its form, typing it, and what it unlocks."""

import re
import secrets
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from stickle.crypto.key_files import B, KeyFiles
from stickle.crypto.keyfile import PasswordSlot, encode_slot, wrap_with_password, write_slots
from stickle.crypto.recovery import (
    ALPHABET,
    OLD_SLOT,
    RecoveryKeyTypoError,
    WrongRecoveryKeyError,
    canonical,
    generate,
    read_recovery,
    unwrap,
    write_recovery,
)
from stickle.crypto.sealed import SealedSlot, make_sealed_slot

KEY = secrets.token_bytes(32)
NOW = "2026-10-26T01:02:03.000Z"


def test_it_reads_as_eight_groups_of_four() -> None:
    key = generate()

    assert re.fullmatch(r"([0-9A-Z]{4}-){7}[0-9A-Z]{4}", key)
    assert not set(key.replace("-", "")) & set("ILOU")


def test_every_key_is_different() -> None:
    assert len({generate() for _ in range(200)}) == 200


@given(st.data())
def test_how_it_is_typed_does_not_matter(data: st.DataObject) -> None:
    key = generate()
    typed = "".join(
        data.draw(st.sampled_from([c, c.lower(), c + " "]))
        if c != "-"
        else data.draw(st.sampled_from(["-", "", " ", " - "]))
        for c in key
    )
    typed = typed.replace("0", data.draw(st.sampled_from(["0", "O", "o"])))
    typed = typed.replace("1", data.draw(st.sampled_from(["1", "I", "l"])))

    assert canonical(typed) == key.replace("-", "")


def missing(key: str) -> str:
    return key[:-1]


def extra(key: str) -> str:
    return key + "A"


def last_mistyped(key: str) -> str:
    return key[:-1] + ALPHABET[(ALPHABET.index(key[-1]) + 1) % 32]


def not_a_key_character(key: str) -> str:
    return "!" + key[1:]


def empty(key: str) -> str:
    return ""


@pytest.mark.parametrize("mangle", [missing, extra, last_mistyped, not_a_key_character, empty])
def test_mistyped_keys_are_told_apart(mangle: Callable[[str], str]) -> None:
    with pytest.raises(RecoveryKeyTypoError):
        canonical(mangle(generate()))


# What it opens, in the key file B


def files_in(folder: Path) -> KeyFiles:
    return KeyFiles(folder)


def test_the_right_key_opens_and_another_does_not(tmp_path: Path) -> None:
    files = files_in(tmp_path)
    recovery_key = generate()
    write_recovery(files, KEY, recovery_key, NOW)
    slot = read_recovery(files)
    assert isinstance(slot, SealedSlot)

    assert unwrap(slot, recovery_key.lower()) == KEY
    with pytest.raises(WrongRecoveryKeyError):
        unwrap(slot, generate())
    assert slot.set_at == NOW


def test_a_new_key_replaces_the_old_one(tmp_path: Path) -> None:
    files = files_in(tmp_path)
    old, new = generate(), generate()
    write_recovery(files, KEY, old, NOW)
    first = read_recovery(files)
    write_recovery(files, KEY, new, NOW)
    slot = read_recovery(files)
    assert isinstance(first, SealedSlot) and isinstance(slot, SealedSlot)

    assert unwrap(slot, new) == KEY
    with pytest.raises(WrongRecoveryKeyError):
        unwrap(slot, old)
    assert slot.public != first.public  # a new key pair: the old one opens nothing


def test_a_recovery_key_from_before_0_6_still_opens_until_replaced(tmp_path: Path) -> None:
    # Until 0.5 the database key was wrapped with the recovery key directly.
    files = files_in(tmp_path)
    old = generate()
    wrapped = wrap_with_password(KEY, canonical(old), 2, 8192, slot_name=OLD_SLOT)
    write_slots(tmp_path / "recovery.json", {OLD_SLOT: encode_slot(wrapped)})
    files.prepare()

    slot = read_recovery(files)
    assert isinstance(slot, PasswordSlot)
    assert unwrap(slot, old) == KEY

    new = generate()
    write_recovery(files, KEY, new, NOW)
    slots = files.load(B)
    assert slots is not None and OLD_SLOT not in slots
    replaced = read_recovery(files)
    assert replaced is not None
    with pytest.raises(WrongRecoveryKeyError):
        unwrap(replaced, old)
    assert unwrap(replaced, new) == KEY


def test_a_new_key_keeps_the_files_other_slots(tmp_path: Path) -> None:
    # The master password will keep its own slot in the same file.
    files = files_in(tmp_path)
    files.save(B, {"later": {"x": 1}})

    write_recovery(files, KEY, generate(), NOW)

    slots = files.load(B)
    assert slots is not None and slots["later"] == {"x": 1}


def test_a_public_key_swapped_in_the_file_opens_nothing(tmp_path: Path) -> None:
    # Whoever can write the file could seal a key of their own for a pair of their own.
    files = files_in(tmp_path)
    recovery_key = generate()
    write_recovery(files, KEY, recovery_key, NOW)
    slot = read_recovery(files)
    assert isinstance(slot, SealedSlot)
    other = make_sealed_slot(secrets.token_bytes(32), canonical(generate()), 2, 8192, "x", NOW)

    swapped = SealedSlot(slot.secret, other.public, other.sealed)
    with pytest.raises(WrongRecoveryKeyError):
        unwrap(swapped, recovery_key)


def test_the_recovery_key_itself_is_never_written(tmp_path: Path) -> None:
    files = files_in(tmp_path)
    recovery_key = generate()
    write_recovery(files, KEY, recovery_key, NOW)

    written = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    for form in (recovery_key, recovery_key.replace("-", ""), recovery_key.lower()):
        assert form.encode() not in written
    assert KEY not in written


def test_no_file_means_no_recovery_key(tmp_path: Path) -> None:
    assert read_recovery(files_in(tmp_path)) is None


@settings(max_examples=20, deadline=None)
@given(st.binary(min_size=32, max_size=32))
def test_any_key_comes_back_intact(key: bytes) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        files = files_in(Path(folder))
        recovery_key = generate()
        write_recovery(files, key, recovery_key, NOW)
        slot = read_recovery(files)
        assert slot is not None
        assert unwrap(slot, recovery_key) == key
