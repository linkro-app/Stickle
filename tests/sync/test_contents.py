"""An item's fields, all of one shape: what this version does not know (a field,
a name within a field, a part beside the fields) is kept as it was when the item
is written again, and a field marked read-only that it does not know keeps it
from changing the item."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from stickle.core.hlc import Hlc
from stickle.crypto.vault_keys import DataKeys
from stickle.sync.contents import Contents, Field, ReadOnlyItemError
from stickle.sync.format import LARGEST, NOTES, Json, UnreadableError, open_envelope, seal

VAULT = "6f1c2a5e-8d3b-4c7a-9e21-0b4d5f6a7c8e"
NOTE = "0f8e6f3c-1d9b-4a63-9c38-3b2a4f2f7d10"
LAPTOP = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
DESKTOP = "9f8e7d6c-5b4a-4c3d-8e2f-1a0b9c8d7e6f"
KEYS = DataKeys({1: bytes(range(32))})
KNOWN = ("colour", "text")

VALUES: st.SearchStrategy[Json] = st.recursive(
    st.none() | st.booleans() | st.integers(-LARGEST, LARGEST) | st.text(),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=6), inner, max_size=3)
    ),
    max_leaves=10,
)
FIELDS = st.builds(
    Field,
    VALUES,
    st.builds(Hlc, st.integers(0, 10**13 - 1), st.integers(0, 9999)),
    st.sampled_from([LAPTOP, DESKTOP]),
    st.booleans(),
    st.dictionaries(
        st.text(max_size=6).filter(
            lambda n: n not in ("device", "hlc", "readonly", "sent", "value")
        ),
        VALUES,
        max_size=2,
    ),
)
CONTENTS = st.builds(
    Contents,
    st.dictionaries(st.text(max_size=8), FIELDS, max_size=5),
    st.dictionaries(st.text(max_size=8).filter(lambda n: n != "fields"), VALUES, max_size=3),
)


@given(contents=CONTENTS)
def test_an_item_written_and_read_is_as_it_was(contents: Contents) -> None:
    sealed = seal(KEYS, VAULT, NOTES, NOTE, contents.to_json())

    assert Contents.from_json(open_envelope(KEYS, sealed, VAULT, NOTES)[1]) == contents


@given(contents=CONTENTS, value=VALUES)
def test_changing_a_known_field_keeps_all_it_does_not_know(contents: Contents, value: Json) -> None:
    changed = Contents.from_json(contents.changed("text", value, Hlc(1, 2), LAPTOP).to_json())

    assert changed.fields["text"].value == value
    assert changed.fields["text"].stamp == (Hlc(1, 2), LAPTOP)
    assert changed.others == contents.others
    for name, before in contents.fields.items():
        if name != "text":
            assert changed.fields[name] == before
    if "text" in contents.fields:
        assert changed.fields["text"].others == contents.fields["text"].others


def test_what_a_newer_version_wrote_comes_back_as_it_was() -> None:
    text: Json = {"value": "hello", "hlc": "1762483200000-0001", "device": LAPTOP, "lang": "en"}
    tags: Json = {"value": ["a", "b"], "hlc": "1762483200000-0000", "device": DESKTOP}
    written: Json = {
        "fields": {"text": text, "tags": tags},
        "type": "note-of-a-later-kind",
        "links": [{"to": NOTE}],
    }
    read = Contents.from_json(written)

    again = read.changed("colour", "yellow", Hlc(1_762_483_300_000), DESKTOP).to_json()

    assert again["type"] == "note-of-a-later-kind" and again["links"] == [{"to": NOTE}]
    fields = again["fields"]
    assert isinstance(fields, dict)
    assert (fields["tags"], fields["text"]) == (tags, text)


def test_a_field_a_version_must_not_change_keeps_it_from_changing_the_item() -> None:
    read = Contents.from_json(
        {
            "fields": {
                "text": {"value": "x", "hlc": "1762483200000-0000", "device": LAPTOP},
                "body2": {
                    "value": "locked a newer way",
                    "hlc": "1762483200000-0000",
                    "device": LAPTOP,
                    "readonly": True,
                },
            }
        }
    )

    assert read.not_to_change(KNOWN) == ["body2"]
    with pytest.raises(ReadOnlyItemError) as raised:
        read.check_writable(KNOWN)
    assert raised.value.names == ["body2"]
    read.check_writable((*KNOWN, "body2"))  # a version that knows the field
    assert read.fields["body2"].to_json()["readonly"] is True  # and the mark is kept


@pytest.mark.parametrize(
    "written",
    [
        [],
        {"fields": []},
        {"fields": {"text": "not a field"}},
        {"fields": {"text": {"hlc": "1762483200000-0000", "device": LAPTOP}}},
        {"fields": {"text": {"value": 1, "hlc": "yesterday", "device": LAPTOP}}},
        {"fields": {"text": {"value": 1, "hlc": "1762483200000-0000", "device": "Laptop"}}},
        {
            "fields": {
                "text": {"value": 1, "hlc": "1762483200000-0000", "device": LAPTOP, "readonly": 1}
            }
        },
    ],
)
def test_an_item_of_another_shape_is_not_read(written: Json) -> None:
    with pytest.raises(UnreadableError):
        Contents.from_json(written)
