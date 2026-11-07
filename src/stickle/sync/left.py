"""What a round does with a file it cannot take in as it is, and remembers of
it (stickle.data.sync_left).

- One that does not open is read again the next round: a file half written,
  or half arrived, opens once it is all there. The same bytes failing three
  rounds running, ten minutes or more from the first, are taken for damaged:
  set aside (their bytes kept in this device's data folder for 30 days,
  Place.aside) and the file written over with what this device has of the
  item. If it has nothing of it, the file is passed over until something else
  is written there. Bytes fewer than the folder listed are not counted: half
  arrived. Without a folder to keep them in, nothing is set aside.
- One with a field this version may not change, and one that is not this
  vault's or not where its item's own file is, is passed over until its tag
  changes (a cloud with no tags: until its bytes do, read each round).
"""

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime

import apsw

from stickle.data import sync_left
from stickle.data.sync_left import NOT_OURS, READ_ONLY, SET_ASIDE, UNREADABLE, Left
from stickle.sync.place import Place, writing
from stickle.sync.targets.base import Entry, Read

FAILS = 3  # rounds running
WAIT = 10 * 60 * 1000  # from the first
KEEP = 30 * 24 * 60 * 60 * 1000  # how long bytes set aside are kept
PASSED = (SET_ASIDE, READ_ONLY, NOT_OURS)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_STAMP = "%Y%m%dT%H%M%S%fZ"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Leaving:
    """One round's dealings with the files of one folder it leaves as they are."""

    def __init__(self, connection: apsw.Connection, place: Place, cloud_now: Callable[[], int]):
        self._db = connection
        self._place = place
        self._now = cloud_now
        self._left = sync_left.every(connection, place.vault)
        self._sizes: dict[str, int] = {}
        self._passed: dict[str, Left] = {}

    def listed(self, entry: Entry) -> bool:
        """The file as the folder lists it; True: passed over, still as it was left."""
        self._sizes[entry.path] = entry.size
        left = self._left.get(entry.path)
        if left is None or left.why not in PASSED or not _same_tag(left, entry.tag):
            return False
        self._passed[entry.path] = left
        return True

    def passed(self, path: str) -> Left | None:
        """How the file was left, if it was passed over this round, as listed."""
        return self._passed.get(path)

    def why(self, path: str, read: Read) -> str | None:
        """Why a file read is left, if it is still as it was left; else None."""
        left = self._left.get(path)
        if left is None or left.why not in PASSED or left.digest != digest(read.data):
            return None
        return left.why

    def leave(self, path: str, read: Read, why: str) -> None:
        left = Left(read.tag, digest(read.data), why)
        if self._left.get(path) == left:
            return
        with writing(self._db):
            sync_left.put(self._db, self._place.vault, path, left)
        self._left[path] = left

    def opened(self, path: str) -> None:
        """The file opened as it is: nothing left of it."""
        if self._left.pop(path, None) is not None:
            with writing(self._db):
                sync_left.drop(self._db, self._place.vault, path)

    def unreadable(self, path: str, read: Read) -> bool:
        """The file did not open: counted. True: set aside now."""
        size = self._sizes.get(path)
        if size is None or len(read.data) != size:
            return False  # half arrived, or not listed this round: later
        now, hashed = self._now(), digest(read.data)
        left = self._left.get(path)
        again = left is not None and left.why == UNREADABLE and left.digest == hashed
        fails, first = (left.fails + 1, left.first_at) if again and left else (1, now)
        aside = self._place.aside
        due = aside is not None and fails >= FAILS and now - first >= WAIT
        if due:
            self._keep(path, read.data, now)
        row = Left(read.tag, hashed, SET_ASIDE if due else UNREADABLE, fails, first)
        with writing(self._db):
            sync_left.put(self._db, self._place.vault, path, row)
        self._left[path] = row
        return due

    def _keep(self, path: str, data: bytes, now: int) -> None:
        aside = self._place.aside
        assert aside is not None
        folder = aside / self._place.vault
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.iterdir():
            at = _stamp_of(old.name)
            if at is not None and now - at >= KEEP:
                old.unlink(missing_ok=True)
        stamp = datetime.fromtimestamp(now / 1000, UTC).strftime(_STAMP)
        (folder / f"{stamp}-{_UNSAFE.sub('_', path)}").write_bytes(data)


def _same_tag(left: Left, tag: str | None) -> bool:
    return tag is not None and left.tag == tag


def _stamp_of(name: str) -> int | None:
    try:
        at = datetime.strptime(name.split("-", 1)[0], _STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None
    return round(at.timestamp() * 1000)


def set_aside(place: Place) -> int:
    """How many files of the vault are set aside here (the last 30 days)."""
    if place.aside is None:
        return 0
    folder = place.aside / place.vault
    return sum(1 for each in folder.iterdir() if each.is_file()) if folder.is_dir() else 0
