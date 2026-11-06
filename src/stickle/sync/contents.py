"""What an item holds, in a shape every version reads alike.

    {"fields": {"<name>": {"device": "<id>", "hlc": "<time>", "value": <JSON>}}}

Every field has the same shape: its value, when it was changed (a hybrid
logical clock's time, stickle.sync.hlc) and by which device. So a field this
version does not know merges as any other, the later change winning. Fields,
names within a field, and parts beside "fields" that this version does not
know are kept as they are when the item is written again.

A field may say "readonly": true. A version that does not know such a field
must neither show nor change its item, only keep it as it is (a body locked
some newer way, say), and says it opens after an update. A version that knows
the field reads it as any other.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from typing import cast

from stickle.sync.format import Json, UnreadableError, is_id
from stickle.sync.hlc import Hlc

FIELDS = "fields"
_FIELD_NAMES = ("device", "hlc", "readonly", "value")


class ReadOnlyItemError(Exception):
    """The item has a field this version does not know and may not change."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(f"fields this version may not change: {', '.join(names)}")
        self.names = names


@dataclass(frozen=True)
class Field:
    value: Json
    hlc: Hlc
    device: str  # the id of the device that changed it
    readonly: bool = False
    others: Mapping[str, Json] = field(default_factory=dict[str, Json])  # kept as they are

    @property
    def stamp(self) -> tuple[Hlc, str]:
        """The order of changes: the later time, or the same time by device."""
        return (self.hlc, self.device)

    def to_json(self) -> dict[str, Json]:
        written: dict[str, Json] = {
            **self.others,
            "device": self.device,
            "hlc": self.hlc.text,
            "value": self.value,
        }
        if self.readonly:
            written["readonly"] = True
        return written

    @classmethod
    def from_json(cls, value: Json) -> Field:
        if not isinstance(value, dict):
            raise UnreadableError("a field is not an object")
        device, hlc, readonly = value.get("device"), value.get("hlc"), value.get("readonly", False)
        if "value" not in value or not isinstance(device, str) or not is_id(device):
            raise UnreadableError("a field without its value or device")
        if not isinstance(hlc, str) or not isinstance(readonly, bool):
            raise UnreadableError("a field without its time")
        try:
            when = Hlc.parse(hlc)
        except ValueError as error:
            raise UnreadableError("a field's time is not one") from error
        others = {name: each for name, each in value.items() if name not in _FIELD_NAMES}
        return cls(value["value"], when, device, readonly, others)


@dataclass(frozen=True)
class Contents:
    fields: Mapping[str, Field] = field(default_factory=dict[str, Field])
    others: Mapping[str, Json] = field(default_factory=dict[str, Json])  # kept as they are

    def to_json(self) -> dict[str, Json]:
        fields: dict[str, Json] = {name: each.to_json() for name, each in self.fields.items()}
        return {**self.others, FIELDS: fields}

    @classmethod
    def from_json(cls, value: Json) -> Contents:
        if not isinstance(value, dict):
            raise UnreadableError("an item is not an object")
        fields = value.get(FIELDS, {})
        if not isinstance(fields, dict):
            raise UnreadableError("an item's fields are not an object")
        others = {name: each for name, each in value.items() if name != FIELDS}
        read = {name: Field.from_json(each) for name, each in cast(dict[str, Json], fields).items()}
        return cls(read, others)

    def changed(self, name: str, value: Json, hlc: Hlc, device: str) -> Contents:
        """With name's value changed, at hlc by device; whatever else the field
        holds is kept."""
        before = self.fields.get(name)
        after = (
            Field(value, hlc, device)
            if before is None
            else replace(before, value=value, hlc=hlc, device=device)
        )
        return replace(self, fields={**self.fields, name: after})

    def not_to_change(self, known: Collection[str]) -> list[str]:
        """The fields, among those this version does not know, that it may not change."""
        return sorted(
            name for name, each in self.fields.items() if each.readonly and name not in known
        )

    def check_writable(self, known: Collection[str]) -> None:
        names = self.not_to_change(known)
        if names:
            raise ReadOnlyItemError(names)
