"""A whole vault in a fake cloud, against the rules every file keeps.

Nothing the user wrote is in any file or file name as text: not a note, a
title, a category's or a setting's name or value, nor a device's name, written
as it is, as UTF-16, escaped as JSON, in base64 at any alignment or in hex.
What a newer version wrote comes back as it was once this version has changed
an item, and a field it must not change keeps only that item from changing."""

import base64
import json

import pytest
from fake_cloud import DROPBOX, FakeCloud
from peek import describe_target

from stickle.core.hlc import Hlc
from stickle.crypto import master, recovery
from stickle.crypto.sealed import make_sealed_slot
from stickle.crypto.vault_keys import MASTER_PASSWORD, RECOVERY_KEY, DataKeys, key_file
from stickle.sync.contents import Contents, ReadOnlyItemError
from stickle.sync.format import (
    DEVICES,
    FOLDERS,
    KEYS,
    NOTES,
    RECORDS,
    VAULT_FILE,
    Json,
    VaultInfo,
    item_at,
    item_path,
    open_envelope,
    seal,
    vault_file,
)
from stickle.sync.targets import base
from stickle.sync.targets.base import Absent, AsRead, Target

VAULT = "6f1c2a5e-8d3b-4c7a-9e21-0b4d5f6a7c8e"
NOTE = "0f8e6f3c-1d9b-4a63-9c38-3b2a4f2f7d10"
OTHER_NOTE = "5b6c7d8e-9f0a-4b1c-8d2e-3f4a5b6c7d8e"
CATEGORY = "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f"
DEVICE = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
KEYS_OF_VAULT = DataKeys({1: bytes(range(32))})
RECOVERY = recovery.generate()
WHAT_THE_USER_WROTE = {
    "note": "MARKER-BODY the first line is mine 첫 줄은 내 것",
    "title": "MARKER-TITLE 회의록 제목",
    "category": "MARKER-CATEGORY 업무 카테고리",
    "setting name": "marker_setting_name_snooze",
    "setting value": "MARKER-SETTING-VALUE 다시 알림",
    "device": "MARKER-DEVICE 김씨의 노트북",
}
KNOWN = ("text", "title", "colour", "category", "name", "value")


def field(value: Json, hlc: int = 1_762_500_000_000) -> dict[str, Json]:
    return {"device": DEVICE, "hlc": Hlc(hlc).text, "value": value}


def put_item(target: Target, kind: str, item: str, contents: Json) -> None:
    target.put(item_path(kind, item), seal(KEYS_OF_VAULT, VAULT, kind, item, contents), Absent())


def a_vault() -> FakeCloud:
    cloud = FakeCloud(DROPBOX)
    target = cloud.target("laptop")
    words = WHAT_THE_USER_WROTE
    target.put(VAULT_FILE, vault_file(VaultInfo(VAULT)), Absent())
    database_key = bytes([9]) * 32
    pairs = {
        RECOVERY_KEY: recovery.make_recovery_slot(database_key, RECOVERY, None),
        MASTER_PASSWORD: make_sealed_slot(database_key, "pw", 1, 8192, master.SLOT, None),
    }
    for kind, pair in pairs.items():
        file_id, data = key_file(KEYS_OF_VAULT, kind, pair)
        target.put(item_path(KEYS, file_id), data, Absent())
    note: dict[str, Json] = {
        "text": field(words["note"]),
        "title": field(words["title"]),
        "category": field(CATEGORY),
    }
    put_item(target, NOTES, NOTE, {"fields": note})
    put_item(
        target,
        RECORDS,
        CATEGORY,
        {"type": "category", "fields": {"name": field(words["category"])}},
    )
    setting = KEYS_OF_VAULT.record_id("setting", words["setting name"])
    put_item(
        target,
        RECORDS,
        setting,
        {"type": "setting", "fields": {"value": field(words["setting value"])}},
    )
    put_item(target, DEVICES, DEVICE, {"fields": {"name": field(words["device"])}})
    return cloud


def forms(text: str) -> list[bytes]:
    """text as it could show in a file without being locked."""
    raw = text.encode()
    found = [
        raw,
        text.encode("utf-16-le"),
        text.encode("utf-16-be"),
        json.dumps(text)[1:-1].encode(),
        raw.hex().encode(),
        raw.hex().upper().encode(),
    ]
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        for shift, start in ((0, 0), (1, 2), (2, 3)):  # past the characters the shift touches
            written = encode(b"\0" * shift + raw)
            found.append(written[start : len(written) - 4])  # short of a partial end
    return found


def plain_in(files: dict[str, bytes], texts: dict[str, str]) -> list[str]:
    names = "\n".join(files).encode()
    return [
        f"{what} in {where}"
        for what, text in texts.items()
        for form in forms(text)
        for where, data in [("the file names", names), *files.items()]
        if form in data
    ]


def test_the_search_finds_what_is_written_in_any_of_its_forms() -> None:
    text = WHAT_THE_USER_WROTE["note"]
    for written in forms(text):
        assert plain_in({"x": b"..." + written + b"..."}, {"note": text})
    shifted = base64.b64encode(b"ab" + text.encode())  # as part of a longer text
    assert plain_in({"x": shifted}, {"note": text})


def test_nothing_the_user_wrote_is_in_the_vault_as_text() -> None:
    files = a_vault().files()

    assert {path.partition("/")[0] for path in files} == {VAULT_FILE, *FOLDERS}
    assert plain_in(files, WHAT_THE_USER_WROTE) == []


def test_a_look_into_the_vault_opens_it_with_the_recovery_key() -> None:
    target = a_vault().target("desktop")

    shown = describe_target(target, recovery_key=RECOVERY)

    assert WHAT_THE_USER_WROTE["note"] in shown
    assert f"notes/{NOTE}.json: notes {NOTE}, data key 1" in shown
    assert "recovery_key, data key 1, trusted" in shown
    assert "master_password, data key 1, trusted" in shown
    assert "(no data key" in describe_target(target, recovery_key=recovery.generate())


def test_what_a_newer_version_wrote_comes_back_whole() -> None:
    cloud = a_vault()
    newer, this = cloud.target("newer"), cloud.target("this")
    later_fields: dict[str, Json] = {
        "text": {**field("text"), "lang": "ko"},  # a name within a field
        "checklist": field([{"done": False, "item": "milk"}]),  # a field
    }
    links: Json = [{"to": NOTE}]  # a part beside the fields
    put_item(newer, NOTES, OTHER_NOTE, {"fields": later_fields, "links": links})
    put_item(
        newer, RECORDS, "7d8e9f0a-1b2c-4d3e-8f4a-5b6c7d8e9f0a", {"type": "tag-group", "fields": {}}
    )

    read = this.get(item_path(NOTES, OTHER_NOTE))
    contents = Contents.from_json(open_envelope(KEYS_OF_VAULT, read.data, VAULT, NOTES)[1])
    contents.check_writable(KNOWN)
    changed = contents.changed("colour", "#FFF2A8", Hlc(1_762_600_000_000), DEVICE)
    this.put(
        item_path(NOTES, OTHER_NOTE),
        seal(KEYS_OF_VAULT, VAULT, NOTES, OTHER_NOTE, changed.to_json()),
        AsRead(read.tag),
    )

    again = newer.get(item_path(NOTES, OTHER_NOTE)).data
    after = open_envelope(KEYS_OF_VAULT, again, VAULT, NOTES)[1]
    assert isinstance(after, dict)
    fields = after["fields"]
    assert isinstance(fields, dict)
    assert after["links"] == links
    assert {name: fields[name] for name in later_fields} == later_fields
    assert fields["colour"] == field("#FFF2A8", 1_762_600_000_000)


def test_a_field_this_version_must_not_change_keeps_only_its_item() -> None:
    cloud = a_vault()
    newer = cloud.target("newer")
    locked_newer_way = {**field("locked some newer way"), "readonly": True}
    put_item(newer, NOTES, OTHER_NOTE, {"fields": {"text": field(""), "body2": locked_newer_way}})

    def contents_of(item: str) -> Contents:
        data = cloud.target("this").get(item_path(NOTES, item)).data
        return Contents.from_json(open_envelope(KEYS_OF_VAULT, data, VAULT, NOTES)[1])

    with pytest.raises(ReadOnlyItemError):
        contents_of(OTHER_NOTE).check_writable(KNOWN)
    contents_of(NOTE).check_writable(KNOWN)  # the other notes change as ever


def test_a_folder_this_version_does_not_know_is_not_one_of_its_own() -> None:
    assert FOLDERS == base.FOLDERS
    assert item_at(f"blobs/{NOTE}.json") is None
