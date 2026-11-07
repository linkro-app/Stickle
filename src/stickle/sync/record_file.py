"""A record in the vault (records/<id>.json): a category, a mark, a colour of
the user's own, or a note of another app brought in, with its fields
(stickle.sync.contents).

    {"record": "category", "item": "<its id here>", "fields": {...}}

What it is and which one it is here never change, so they are beside its
fields, not among them; both are inside the locked file, never in its name.
The item is a category's, a mark's or a colour's id; for a note brought in,
its app and its id there, as ["<app>", "<id>"].

The file's name is the item's id where every device has the same random one
(a category, a mark the user made, a colour); else (a built-in mark, a note
brought in) a hash of what it is, keyed with the vault's key for ids, the same
on every device and telling nothing. So a file name does not tell a built-in
mark from a category.

A record of a kind this version does not know is read as it is, and left so.
"""

from dataclasses import dataclass, replace
from typing import cast

from stickle.core.labels import BUILT_IN_MARKS
from stickle.crypto.vault_keys import DataKeys
from stickle.data.record_stamps import CATEGORY, COLOR, IMPORT, KINDS, MARK, import_item
from stickle.sync.contents import Contents
from stickle.sync.format import Json, UnreadableError, is_id

RECORD = "record"
ITEM = "item"


@dataclass(frozen=True)
class RecordFile:
    kind: str  # one of stickle.data.record_stamps.KINDS, or one this version does not know
    # Its id here (stickle.data.record_stamps): a note brought in, "<app>\n<id>".
    # One of a kind not known: "", the item kept among the others, as it is.
    item: str
    contents: Contents

    @property
    def known(self) -> bool:
        return self.kind in KINDS

    def to_json(self) -> dict[str, Json]:
        written = {**self.contents.to_json(), RECORD: self.kind}
        if self.known:
            written[ITEM] = _item_json(self.kind, self.item)
        return written

    @classmethod
    def from_json(cls, value: Json) -> RecordFile:
        if not isinstance(value, dict):
            raise UnreadableError("a record is not an object")
        kind = value.get(RECORD)
        if not isinstance(kind, str):
            raise UnreadableError("a record without its kind")
        contents = Contents.from_json(value)
        if kind not in KINDS:
            others = {name: each for name, each in contents.others.items() if name != RECORD}
            return cls(kind, "", replace(contents, others=others))
        others = {
            name: each for name, each in contents.others.items() if name not in (RECORD, ITEM)
        }
        return cls(kind, _item_of(kind, value.get(ITEM)), replace(contents, others=others))


def _item_json(kind: str, item: str) -> Json:
    if kind == IMPORT:
        source, _, source_id = item.partition("\n")
        return [source, source_id]
    return item


def _item_of(kind: str, value: Json) -> str:
    if kind == IMPORT:
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(isinstance(part, str) for part in value)
        ):
            raise UnreadableError("a note brought in without its app and id")
        source, source_id = cast(list[str], value)
        if "\n" in source:
            raise UnreadableError("an app's name with a line break")
        return import_item(source, source_id)
    if not isinstance(value, str) or not value:
        raise UnreadableError("a record without its item")
    return value


def record_id(keys: DataKeys, kind: str, item: str) -> str:
    """The id of the item's file in the vault."""
    shared = kind in (CATEGORY, COLOR) or (kind == MARK and item not in BUILT_IN_MARKS)
    if shared and is_id(item):
        return item
    return keys.record_id(kind, item)
