"""The vault's files: names, vault.json, and envelopes that open only as written
and only in their place, whatever byte is changed. An envelope made by hand,
from what the format says, opens as one the app made."""

import base64
import json

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from nacl import secret

from stickle.crypto.vault_keys import DataKeys, derive
from stickle.sync.format import (
    AAD,
    DEVICES,
    LARGEST,
    NOTES,
    RECORDS,
    FormatError,
    Json,
    NewerFormatError,
    NotOursError,
    UnknownKeyError,
    UnreadableError,
    VaultInfo,
    item_at,
    item_path,
    open_envelope,
    read_head,
    read_vault_file,
    seal,
    vault_file,
)

VAULT = "6f1c2a5e-8d3b-4c7a-9e21-0b4d5f6a7c8e"
OTHER_VAULT = "0a9b8c7d-6e5f-4a3b-8c2d-1e0f9a8b7c6d"
NOTE = "0f8e6f3c-1d9b-4a63-9c38-3b2a4f2f7d10"
OTHER_NOTE = "5b6c7d8e-9f0a-4b1c-8d2e-3f4a5b6c7d8e"
KEY = bytes(range(32))
KEYS = DataKeys({1: KEY})
SEALED = seal(KEYS, VAULT, NOTES, NOTE, {"fields": {"text": "a note"}})

JSON: st.SearchStrategy[Json] = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-LARGEST, LARGEST)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=20,
)


def by_hand(head: dict[str, int | str], contents: bytes, key: bytes = KEY) -> bytes:
    """An envelope as the format describes it, made without the app's code."""
    head_json = json.dumps(dict(sorted(head.items())), separators=(",", ":"))
    box = secret.Aead(derive(key, b"stickle-items")).encrypt(contents, AAD + head_json.encode())
    written = {**dict(sorted(head.items())), "box": base64.b64encode(bytes(box)).decode()}
    return json.dumps(written, separators=(",", ":")).encode()


def head(**changes: int | str) -> dict[str, int | str]:
    return {"alg": 1, "format": 1, "id": NOTE, "key": 1, "kind": NOTES, "vault": VAULT, **changes}


def test_names_are_ids_alone() -> None:
    assert item_path(NOTES, NOTE) == f"notes/{NOTE}.json"
    assert item_at(f"notes/{NOTE}.json") == (NOTES, NOTE)
    assert item_at(f"keys/{NOTE}.json") == ("keys", NOTE)
    for other in (
        f"notes/{NOTE} (1).json",
        f"notes/{NOTE.upper()}.json",
        f"blobs/{NOTE}.json",
        f"notes/sub/{NOTE}.json",
        f"notes/{NOTE}.txt",
        "vault.json",
        "notes/.json",
    ):
        assert item_at(other) is None, other
    with pytest.raises(ValueError):
        item_path(NOTES, "Not-An-Id")


def test_a_new_vault_json() -> None:
    written = vault_file(VaultInfo(VAULT))

    assert written == b'{"format":1,"kind":"personal","vault":"' + VAULT.encode() + b'"}'
    assert read_vault_file(written) == VaultInfo(VAULT)


def test_vault_json_keeps_what_it_does_not_know() -> None:
    written = (
        b'{"format":1,"kind":"personal","later":{"a":[1,2]},"vault":"' + VAULT.encode() + b'"}'
    )

    info = read_vault_file(written)

    assert (info.vault, info.kind, info.others) == (VAULT, "personal", {"later": {"a": [1, 2]}})
    assert vault_file(info) == written


@pytest.mark.parametrize(
    ("written", "error"),
    [
        (b'{"format":2,"kind":"personal","vault":"' + VAULT.encode() + b'"}', NewerFormatError),
        (b'{"format":1,"kind":"shared","vault":"' + VAULT.encode() + b'"}', NewerFormatError),
        (b'{"format":1,"kind":"personal","vault":"NOT AN ID"}', UnreadableError),
        (b'{"kind":"personal","vault":"' + VAULT.encode() + b'"}', UnreadableError),
        (b'{"format":1,"kind":"perso', UnreadableError),
        (b"[]", UnreadableError),
    ],
)
def test_vault_json_from_a_newer_version_or_damaged(written: bytes, error: type[Exception]) -> None:
    with pytest.raises(error):
        read_vault_file(written)


@settings(max_examples=200)
@given(contents=JSON, folder=st.sampled_from([NOTES, RECORDS, DEVICES]))
def test_what_is_sealed_opens_as_it_was(contents: Json, folder: str) -> None:
    sealed = seal(KEYS, VAULT, folder, NOTE, contents)

    opened_head, opened = open_envelope(KEYS, sealed, VAULT, folder)

    assert opened == contents
    assert (opened_head.vault, opened_head.kind, opened_head.item, opened_head.key) == (
        VAULT,
        folder,
        NOTE,
        1,
    )


def test_an_envelope_made_as_the_format_says_opens() -> None:
    made = by_hand(head(), b'{"fields":{"text":"a note"}}')

    assert open_envelope(KEYS, made, VAULT, NOTES)[1] == {"fields": {"text": "a note"}}
    assert SEALED.startswith(b'{"alg":1,"format":1,"id":"' + NOTE.encode() + b'","key":1,')
    assert json.loads(SEALED)["box"]  # the head, readable; the box last


@settings(max_examples=300)
@given(position=st.integers(min_value=0), byte=st.integers(0, 255))
def test_a_file_with_any_byte_changed_does_not_open(position: int, byte: int) -> None:
    changed = bytearray(SEALED)
    at = position % len(changed)
    assume(changed[at] != byte)
    changed[at] = byte

    with pytest.raises(FormatError):
        open_envelope(KEYS, bytes(changed), VAULT, NOTES)


@given(position=st.integers(min_value=0), byte=st.integers(0, 255), drop=st.booleans())
def test_a_file_with_a_byte_more_or_less_does_not_open(
    position: int, byte: int, drop: bool
) -> None:
    at = position % len(SEALED)
    changed = SEALED[:at] + SEALED[at + 1 :] if drop else SEALED[:at] + bytes([byte]) + SEALED[at:]

    with pytest.raises(FormatError):
        open_envelope(KEYS, changed, VAULT, NOTES)


def test_written_another_way_it_does_not_open() -> None:
    spaced = json.dumps(json.loads(SEALED)).encode()  # the same, with spaces
    for other in (spaced, SEALED + b"\n", b" " + SEALED):
        with pytest.raises(UnreadableError):
            open_envelope(KEYS, other, VAULT, NOTES)


def test_base64_with_its_spare_bits_changed_does_not_open() -> None:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    for length in (1, 2):  # the box ends in "==" and in "=": its last character has spare bits
        sealed = seal(KEYS, VAULT, NOTES, NOTE, "x" * length)
        box: str = json.loads(sealed)["box"]
        assert box.endswith("=")
        bare = box.rstrip("=")
        other = alphabet[alphabet.index(bare[-1]) ^ 1]  # the lowest bit is a spare one
        changed_box = bare[:-1] + other + box[len(bare) :]
        assert base64.b64decode(changed_box) == base64.b64decode(box)
        changed = sealed.replace(box.encode(), changed_box.encode())
        with pytest.raises(UnreadableError):
            open_envelope(KEYS, changed, VAULT, NOTES)


def test_in_another_vaults_or_another_folders_place_it_does_not_open() -> None:
    with pytest.raises(NotOursError):
        open_envelope(KEYS, SEALED, OTHER_VAULT, NOTES)
    with pytest.raises(NotOursError):
        open_envelope(KEYS, SEALED, VAULT, RECORDS)
    # its head made to say so: bound to the box, so it does not open there either
    for moved in (head(vault=OTHER_VAULT), head(kind=RECORDS), head(id=OTHER_NOTE)):
        relabelled = json.loads(SEALED)
        relabelled.update(moved)
        written = json.dumps(
            {**dict(sorted(moved.items())), "box": relabelled["box"]}, separators=(",", ":")
        ).encode()
        with pytest.raises(UnreadableError):
            open_envelope(KEYS, written, str(moved["vault"]), str(moved["kind"]))


def test_copied_under_another_name_it_is_still_the_item_its_head_names() -> None:
    # read at notes/<OTHER_NOTE>.json, it says it is NOTE: never OTHER_NOTE
    assert open_envelope(KEYS, SEALED, VAULT, NOTES)[0].item == NOTE


def test_a_newer_format_or_algorithm_stops_and_another_vaults_file_does_not() -> None:
    newer = by_hand(head(format=2), b"{}")
    with pytest.raises(NewerFormatError):
        open_envelope(KEYS, newer, VAULT, NOTES)
    with pytest.raises(NewerFormatError):
        open_envelope(KEYS, by_hand(head(alg=2), b"{}"), VAULT, NOTES)
    newer_elsewhere = by_hand(head(format=2, vault=OTHER_VAULT), b"{}")
    with pytest.raises(NotOursError):
        open_envelope(KEYS, newer_elsewhere, VAULT, NOTES)
    # written by a newer version some other way, its format still says so
    other_way = b'{"format": 2, "vault": "' + VAULT.encode() + b'", "x": [1]}'
    with pytest.raises(NewerFormatError):
        open_envelope(KEYS, other_way, VAULT, NOTES)


def test_a_data_key_this_device_does_not_have_stops() -> None:
    later = DataKeys({1: KEY, 2: bytes(32)})
    sealed = seal(later, VAULT, NOTES, NOTE, "after the keys changed")

    with pytest.raises(UnknownKeyError):
        open_envelope(KEYS, sealed, VAULT, NOTES)
    assert open_envelope(later, sealed, VAULT, NOTES)[1] == "after the keys changed"
    assert open_envelope(later, SEALED, VAULT, NOTES)[0].key == 1  # older files still open


def test_head_names_a_newer_version_added_are_bound_and_let_go() -> None:
    made = by_hand(head(level=3, note="kept in the head"), b'"contents"')

    opened_head, contents = open_envelope(KEYS, made, VAULT, NOTES)

    assert opened_head.others == {"level": 3, "note": "kept in the head"}
    assert contents == "contents"
    unbound = json.loads(made)
    unbound["level"] = 4
    with pytest.raises(UnreadableError):
        open_envelope(KEYS, json.dumps(unbound, separators=(",", ":")).encode(), VAULT, NOTES)


@pytest.mark.parametrize(
    "written",
    [
        b"",
        b'{"alg":1,"format":1,"id":"' + NOTE.encode(),  # half written
        b"\xff\xfe",
        b"[]",
        by_hand(head(), b"not json"),
        by_hand(head(), b"{}", key=bytes(32)),  # locked with another key
        by_hand(head(id="NOT AN ID"), b"{}"),
        by_hand(head(key=0), b"{}"),
    ],
)
def test_damaged_or_half_written_does_not_open(written: bytes) -> None:
    with pytest.raises(UnreadableError):
        open_envelope(KEYS, written, VAULT, NOTES)


def test_a_head_reads_without_a_key() -> None:
    read = read_head(SEALED)

    assert (read.vault, read.kind, read.item, read.key) == (VAULT, NOTES, NOTE, 1)


@pytest.mark.parametrize("value", [2**53, -(2**53), {"a": [2**60]}, float("nan"), float("inf")])
def test_what_javascript_would_not_read_back_is_not_written(value: Json) -> None:
    with pytest.raises(ValueError):
        seal(KEYS, VAULT, NOTES, NOTE, value)
    assert open_envelope(KEYS, seal(KEYS, VAULT, NOTES, NOTE, str(2**60)), VAULT, NOTES)[1] == str(
        2**60
    )
