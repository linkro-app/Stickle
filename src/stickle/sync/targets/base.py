"""What sync asks of a cloud, and what a cloud can answer.

A target (OneDrive, Dropbox, WebDAV) does four things to the files in its
Stickle folder: lists a folder, reads a file, writes one and deletes one. A
file it makes is written only if none is there yet; one it replaces or deletes,
only if it is still as it was read. Some targets can also tell what changed
since a cursor, and wait for a change; others leave sync to compare lists.

Nothing else is asked of them: no locks, no renames, and nothing is read into
the time a file was changed. A target that cannot keep a condition (some
WebDAV servers ignore them) says so, and writes anyway: sync keeps what it
wrote until it sees it there.

Whatever a cloud answers comes up as one of the errors below, never as the
cloud's own status codes.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

# The folders in the Stickle folder; vault.json is beside them.
FOLDERS = ("notes", "records", "devices", "keys")


@dataclass(frozen=True)
class Entry:
    """A file as a folder lists it."""

    path: str  # in the Stickle folder, "/" between its parts: "notes/<id>.json"
    tag: str | None  # what tells this content from another; None if the target gives none
    size: int


@dataclass(frozen=True)
class Read:
    data: bytes
    tag: str | None


@dataclass(frozen=True)
class Absent:
    """Written only if no file is there."""


@dataclass(frozen=True)
class AsRead:
    """Written over, or deleted, only if the file is still as read: its tag."""

    tag: str | None  # None: the target gives no tags, so this cannot be checked


type Condition = Absent | AsRead


@dataclass(frozen=True)
class Changes:
    """One page of what changed since a cursor."""

    changed: tuple[Entry, ...]  # made or written over
    gone: tuple[str, ...]  # deleted (paths)
    cursor: str  # where the next look starts: kept once the page is dealt with


class Target(Protocol):
    @property
    def keeps_conditions(self) -> bool:
        """False: Absent and AsRead are not checked, and every write goes through."""
        ...

    def list(self, folder: str) -> Iterator[Entry]:
        """The files right in folder ("notes"), as they come: not all at once."""
        ...

    def get(self, path: str) -> Read:
        """NotThereError if there is none."""
        ...

    def put(self, path: str, data: bytes, condition: Condition) -> str | None:
        """The file written whole; its new tag. RefusedError if the condition does not hold."""
        ...

    def delete(self, path: str, condition: AsRead) -> None:
        """NotThereError if there is none; RefusedError if it changed since read."""
        ...

    def changes(self, cursor: str | None) -> Iterator[Changes] | None:
        """What changed since cursor, a page at a time (None for a cursor: every
        file, as made). None if the target cannot tell: sync compares lists.
        CursorExpiredError if the cursor is too old: start again without one."""
        ...

    def wait_for_change(self, cursor: str, seconds: float) -> bool | None:
        """Whether something changed since cursor, waiting up to seconds for it;
        None if the target cannot wait (sync looks every so often instead)."""
        ...


class TargetError(Exception):
    """What a cloud answered, as sync deals with it."""


class NotThereError(TargetError):
    """No such file (404)."""


class RefusedError(TargetError):
    """A condition did not hold: a file is there already, or it changed since read
    (409, 412). Read again, merge, and write again."""


class UnavailableError(TargetError):
    """No answer, or the cloud failing for now (the network, 5xx): try again later.
    The request may have gone through all the same, its answer lost."""


class TooBusyError(TargetError):
    """Too many requests (429, Dropbox's too_many_write_operations): wait."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__(retry_after)
        self.retry_after = retry_after  # seconds, as the cloud asked; None: as sync sees fit


class TokenExpiredError(TargetError):
    """The access token is out of date: get another with the refresh token."""


class SignInNeededError(TargetError):
    """RefusedError for good until the user signs in again: the refresh token was turned
    down, or a WebDAV password is wrong (401). Stop; never try again by itself."""


class FullError(TargetError):
    """No room left in the cloud: stop writing, go on reading."""


class NoAccessError(TargetError):
    """The app may not use the cloud any more (its access withdrawn): stop."""


class CursorExpiredError(TargetError):
    """The cursor is too old to follow (OneDrive's 410): start again without one."""
