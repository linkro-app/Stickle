"""A sticky note as stored on this device."""

import hashlib
from dataclasses import dataclass

from stickle.core.colors import DEFAULT_COLOR

__all__ = ["DEFAULT_COLOR", "Note", "content_hash"]


def content_hash(body: str) -> str:
    """Local content fingerprint. (Encrypted vaults will use a keyed hash instead.)"""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def secret(self) -> bool:
        return self.sealed is not None
