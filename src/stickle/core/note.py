"""A sticky note as stored on this device."""

import hashlib
from dataclasses import dataclass

from stickle.core.colors import DEFAULT_COLOR

__all__ = ["DEFAULT_COLOR", "Conflict", "Note", "SealedText", "content_hash"]


def content_hash(body: str) -> str:
    """Local content fingerprint. (Encrypted vaults will use a keyed hash instead.)"""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SealedText:
    """A secret note's text, encrypted (stickle.crypto.secret_notes), as it is
    kept here: bound to the note it was sealed for (None: the one that has it
    — a conflict copy's opens as its original's) and sealed with the key of
    that number (None: this computer's)."""

    data: bytes
    bound_to: str | None = None
    key_number: str | None = None

    def opens_as(self, note_id: str) -> str:
        """The id it opens with, for the note note_id that has it."""
        return self.bound_to or note_id

    @property
    def hash(self) -> str:
        return content_hash(self.data.hex())


@dataclass(frozen=True)
class Conflict:
    """What a conflict copy is: one of two texts written apart on two devices,
    kept beside the note (its original) that kept the other."""

    note: str  # the original's id
    device: str | None  # the id of the device whose text it keeps; None: this one
    edited: int  # when that text was edited there, in milliseconds


@dataclass(frozen=True)
class Note:
    id: str  # random UUID v4: reveals nothing, not even when it was made
    body: str  # Markdown
    color: str
    hidden: bool
    always_on_top: bool
    created_at: str  # ISO 8601 UTC
    updated_at: str
    content_hash: str
    deleted_at: str | None  # deleting only marks the note; it is kept for 365 days
    change_seq: int  # this device's change counter when the note last changed
    # Kept in the model now so later features need no schema change.
    opacity: float = 1.0
    status: str = "active"
    label: str | None = None  # the id of its category, if any
    marks: frozenset[str] = frozenset()  # the ids of its marks
    locked: bool = False
    collapsed: bool = False
    auto_height: bool = False
    zoom: float = 1.0
    sleep_until: str | None = None
    style_id: str | None = None
    # A secret note: its text encrypted (stickle.crypto.secret_notes) in sealed,
    # body left empty, and a title of its own that shows while it is locked.
    secret_title: str | None = None
    sealed: bytes | None = None
    conflict: Conflict | None = None  # a conflict copy's: what it is a copy of
    # What sealed is bound to and sealed with (SealedText; None: its own, this computer's).
    bound_to: str | None = None
    key_number: str | None = None

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def secret(self) -> bool:
        return self.sealed is not None

    @property
    def sealed_text(self) -> SealedText | None:
        if self.sealed is None:
            return None
        return SealedText(self.sealed, self.bound_to, self.key_number)
