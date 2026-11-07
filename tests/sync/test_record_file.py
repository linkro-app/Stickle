"""A record file holds what it is and which one beside its fields, all locked;
its name tells neither."""

import pytest

from stickle.core.hlc import Hlc
from stickle.crypto.vault_keys import DataKeys
from stickle.sync.contents import Contents, Field
from stickle.sync.format import Json, UnreadableError, is_id, json_bytes
from stickle.sync.record_file import RecordFile, record_id

KEYS = DataKeys({1: bytes(range(32))})
DEVICE = "0b6f3c2e-8f14-4d52-9a57-2f3c8d9e1a40"
CATEGORY_ID = "5a1c7e9b-3d2f-4a6b-8c0d-1e2f3a4b5c6d"


def test_a_category_goes_and_comes_back_as_it_was() -> None:
    contents = Contents({"name": Field("일", Hlc(5), DEVICE)}, {"later": [1]})
    record = RecordFile("category", CATEGORY_ID, contents)

    written = record.to_json()

    assert written["record"] == "category"
    assert written["item"] == CATEGORY_ID
    assert written["later"] == [1]
    assert RecordFile.from_json(written) == record


def test_a_note_brought_in_is_its_app_and_its_id_there() -> None:
    record = RecordFile("import", "sticky_notes\nab-12", Contents())

    written = record.to_json()

    assert written["item"] == ["sticky_notes", "ab-12"]
    assert RecordFile.from_json(written) == record


def test_a_kind_this_version_does_not_know_is_kept_as_it_is() -> None:
    value: dict[str, Json] = {
        "record": "folder",
        "item": {"path": [1, 2]},
        "fields": {"x": {"device": DEVICE, "hlc": Hlc(1).text, "value": 3}},
    }
    record = RecordFile.from_json(value)

    assert not record.known
    assert json_bytes(record.to_json()) == json_bytes(value)


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"item": "x", "fields": {}},
        {"record": "category", "fields": {}},
        {"record": "category", "item": "", "fields": {}},
        {"record": "import", "item": "sticky\nx", "fields": {}},
        {"record": "import", "item": ["sticky"], "fields": {}},
        {"record": "import", "item": ["a\nb", "x"], "fields": {}},
    ],
)
def test_one_without_what_or_which_it_is_is_not_read(value: Json) -> None:
    with pytest.raises(UnreadableError):
        RecordFile.from_json(value)


def test_a_category_mark_or_colour_is_named_by_its_own_id() -> None:
    for kind in ("category", "mark", "color"):
        assert record_id(KEYS, kind, CATEGORY_ID) == CATEGORY_ID


def test_a_built_in_mark_and_a_note_brought_in_by_a_keyed_hash() -> None:
    todo, brought = record_id(KEYS, "mark", "todo"), record_id(KEYS, "import", "sticky\nab")

    assert is_id(todo) and is_id(brought)
    assert "todo" not in todo
    assert todo == record_id(KEYS, "mark", "todo")
    assert todo != record_id(DataKeys({1: bytes(32)}), "mark", "todo")
    assert len({todo, brought, record_id(KEYS, "mark", "urgent")}) == 3
