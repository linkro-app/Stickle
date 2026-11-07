"""Clouds in memory, for sync's tests.

Each answers as a real one does: Dropbox (a rev for each file, kept even when the
same content is written again; conditions kept; a long poll), OneDrive (an eTag
changed by any write; conditions kept; changes shown late) and WebDAV servers of
several kinds (as the one tried answers, or with no tags, weak tags, conditions
ignored, or a file half there while it is written). The test says where things
go wrong, request by request (Trouble), and does to the folder what the user
or the cloud may (it vanishes, goes back to an earlier state, a copy appears
under another name).

Devices reach it through their own FakeTarget, one request at a time: with
Turns, each waits at the door and the test lets them in one by one, in the
order it chose, so that one device's reads and writes fall between another's.
The same order and troubles always give the same requests and answers.
"""

import hashlib
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Literal

from stickle.sync.targets.base import (
    Absent,
    AsRead,
    Changes,
    Condition,
    CursorExpiredError,
    Entry,
    FullError,
    NotThereError,
    Read,
    RefusedError,
    SignInNeededError,
    TokenExpiredError,
    TooBusyError,
    UnavailableError,
)

# Requests in one "second": a weak WebDAV tag is given for a file written within it.
TICKS_PER_SECOND = 4

# rev: Dropbox's; etag: OneDrive's; strong: changed by every write; seconds: as
# wsgidav makes them, from the second it was written and its size (two writes
# of one size within a second keep the tag); weak: as Apache gives for a file
# written within the second; none: no tags.
type TagStyle = Literal["rev", "etag", "strong", "seconds", "weak", "none"]


@dataclass(frozen=True)
class Kind:
    """How a cloud answers."""

    name: str
    tags: TagStyle
    keeps_conditions: bool = True
    same_content_new_tag: bool = True  # written again as it was: a new tag
    follows: bool = False  # tells what changed since a cursor
    waits: bool = False  # waits for a change (a long poll)
    changes_late: int = 0  # requests before a change shows among the changes
    page: int = 3  # changes on a page
    half_while_written: bool = False  # read right after a write: half of it, once
    # Written as read, a file deleted since is made again (wsgidav), not refused.
    makes_gone_as_read: bool = False


DROPBOX = Kind("dropbox", "rev", same_content_new_tag=False, follows=True, waits=True)
ONEDRIVE = Kind("onedrive", "etag", follows=True, changes_late=2)
# As the WebDAV server tried answers (test_wsgidav_facts checks it does).
WEBDAV = Kind("webdav", "seconds", makes_gone_as_read=True)
WEBDAV_NO_TAGS = Kind("webdav-no-tags", "none", keeps_conditions=False)
WEBDAV_WEAK = Kind("webdav-weak", "weak")  # Apache, a file written within the second
WEBDAV_IGNORING = Kind("webdav-ignoring", "strong", keeps_conditions=False)
WEBDAV_HALF = Kind("webdav-half", "strong", half_while_written=True)
KINDS = (DROPBOX, ONEDRIVE, WEBDAV, WEBDAV_NO_TAGS, WEBDAV_WEAK, WEBDAV_IGNORING, WEBDAV_HALF)


class Trouble(Enum):
    """What goes wrong with one request."""

    UNAVAILABLE = auto()  # no answer, nothing done
    LOST = auto()  # done, then its answer lost
    TOO_BUSY = auto()  # 429, nothing done
    HALF = auto()  # a read gets half the file
    LATE = auto()  # a write shows in lists and changes only some requests later


LATE_BY = 5  # requests a LATE write waits before lists and changes show it


@dataclass(frozen=True)
class Request:
    device: str
    op: str  # list, get, put, delete, changes, wait
    path: str
    number: int  # the cloud's count of requests, from 1


@dataclass
class _File:
    data: bytes
    version: int  # its own count of writes, for its tag
    written: int  # the request that wrote it
    listed_from: int  # the request from which lists show it
    path_hash: str = ""  # a part of a "seconds" tag, as wsgidav's


@dataclass(frozen=True)
class _Event:
    path: str
    gone: bool
    entry: Entry | None
    shown_from: int  # the request from which changes show it


@dataclass
class FakeCloud:
    kind: Kind
    # Chooses the trouble for each request (None: none); set by the test.
    trouble: Callable[[Request], Trouble | None] = lambda _request: None
    turns: Turns | None = None
    requests: int = 0
    log: list[str] = field(default_factory=list[str])
    _files: dict[str, _File] = field(default_factory=dict[str, _File])
    _events: list[_Event] = field(default_factory=list[_Event])
    # The files after each write, by the request that made it (see go_back_to).
    _states: list[tuple[int, dict[str, _File]]] = field(
        default_factory=list[tuple[int, dict[str, _File]]]
    )
    _written: int = 0
    _expired: set[str] = field(default_factory=set[str])
    _signed_out: set[str] = field(default_factory=set[str])
    _cursor_age: int = 0  # cursors of an earlier age no longer work (see expire_cursors)
    _halves: set[str] = field(default_factory=set[str])
    full: bool = False

    def target(self, device: str) -> FakeTarget:
        return FakeTarget(self, device)

    # What the test does to it, as the user or the cloud would (not requests)

    def expire_token(self, device: str) -> None:
        self._expired.add(device)

    def refuse_refresh(self, device: str) -> None:
        """The app's access withdrawn by the user: refreshing is turned down."""
        self._signed_out.add(device)

    def expire_cursors(self) -> None:
        """Every cursor given so far too old to follow, as a cloud does after days."""
        self._cursor_age += 1

    def put_by_hand(self, path: str, data: bytes, long_ago: bool = False) -> None:
        """Written by something other than Stickle (or another vault's device);
        long_ago: there a while, so no read finds it half written."""
        self._write(path, data, late=False)
        if long_ago:
            self._halves.discard(path)

    def copy_by_hand(self, path: str, copy: str) -> None:
        """A copy under another name, as a cloud's web page or app makes one."""
        self._write(copy, self._files[path].data, late=False)

    def vanish(self) -> None:
        """The Stickle folder deleted (by the user, from the cloud's web page)."""
        for path in sorted(self._files):
            self._remove(path)

    def go_back_to(self, number: int) -> None:
        """The folder as it was after that request (Dropbox Rewind, a restore):
        the files written again with their contents then, under new tags."""
        then = next(
            (files for at, files in reversed(self._states) if at <= number), dict[str, _File]()
        )
        for path in sorted(set(self._files) - set(then)):
            self._remove(path)
        for path, old in sorted(then.items()):
            if path not in self._files or self._files[path].data != old.data:
                self._write(path, old.data, late=False)

    # What it holds

    def files(self) -> dict[str, bytes]:
        return {path: stored.data for path, stored in sorted(self._files.items())}

    def describe(self, last: int = 40) -> str:
        """Its files (tag, size, when written) and its last requests, for a failed test."""
        lines = [f"{self.kind.name} after {self.requests} requests:"]
        for path, stored in sorted(self._files.items()):
            when = f"written at {stored.written}"
            if stored.listed_from > self.requests:
                when += f", listed from {stored.listed_from}"
            lines.append(f"  {path}  {self._tag_of(stored)}  {len(stored.data)} bytes, {when}")
        lines.append("requests:")
        lines += [f"  {line}" for line in self.log[-last:]]
        return "\n".join(lines)

    # Requests

    def request[T](self, device: str, op: str, path: str, act: Callable[[Trouble | None], T]) -> T:
        if self.turns is not None:
            self.turns.turn(device)
        self.requests += 1
        number = self.requests
        trouble = self.trouble(Request(device, op, path, number))
        try:
            if device in self._signed_out:
                raise SignInNeededError()
            if device in self._expired:
                raise TokenExpiredError()
            if trouble is Trouble.UNAVAILABLE:
                raise UnavailableError()
            if trouble is Trouble.TOO_BUSY:
                raise TooBusyError(retry_after=1.0)
            result = act(trouble)
            if trouble is Trouble.LOST:
                raise UnavailableError("its answer was lost")
        except Exception as error:
            self.log.append(f"{number} {device} {op} {path}: {type(error).__name__}")
            raise
        self.log.append(f"{number} {device} {op} {path}: ok")
        return result

    def refresh(self, device: str) -> None:
        """A new access token for the device, unless its refresh is turned down."""
        if device in self._signed_out:
            raise SignInNeededError()
        self._expired.discard(device)

    # Inside

    def _tag_of(self, stored: _File) -> str | None:
        match self.kind.tags:
            case "none":
                return None
            case "rev":
                return f"{stored.version:09x}"
            case "etag":
                return f'"{{F1}},{stored.version}"'
            case "strong":
                return f'"{stored.version}"'
            case "seconds":
                return (
                    f'"{stored.path_hash}-{stored.written // TICKS_PER_SECOND}-{len(stored.data)}"'
                )
            case "weak":
                same_second = (
                    stored.written // TICKS_PER_SECOND == self.requests // TICKS_PER_SECOND
                )
                return f'W/"{stored.version}"' if same_second else f'"{stored.version}"'

    def _entry(self, path: str, stored: _File) -> Entry:
        return Entry(path, self._tag_of(stored), len(stored.data))

    def _holds(self, stored: _File, tag: str | None) -> bool:
        """Whether the file is still as read with that tag (a weak tag never is)."""
        if tag is None or tag.startswith("W/"):
            return False
        return tag == self._tag_of(stored)

    def _write(self, path: str, data: bytes, late: bool) -> _File:
        old = self._files.get(path)
        if old is not None and old.data == data and not self.kind.same_content_new_tag:
            return old
        self._written += 1
        shown = self.requests + (LATE_BY if late else 0)
        path_hash = hashlib.md5(path.encode()).hexdigest()[:8]
        stored = _File(data, self._written, self.requests, shown, path_hash)
        self._files[path] = stored
        self._events.append(
            _Event(path, False, self._entry(path, stored), shown + self.kind.changes_late)
        )
        self._states.append((self.requests, dict(self._files)))
        if self.kind.half_while_written:
            self._halves.add(path)
        return stored

    def _remove(self, path: str) -> None:
        del self._files[path]
        self._halves.discard(path)
        self._events.append(_Event(path, True, None, self.requests + self.kind.changes_late))
        self._states.append((self.requests, dict(self._files)))

    def _check(self, path: str, condition: Condition) -> None:
        if not self.kind.keeps_conditions:
            return
        stored = self._files.get(path)
        if isinstance(condition, Absent):
            if stored is not None:
                raise RefusedError(path)
        elif stored is None:
            if not self.kind.makes_gone_as_read:
                raise RefusedError(path)  # gone since it was read counts as changed (412)
        elif not self._holds(stored, condition.tag):
            raise RefusedError(path)

    # The requests' answers: through request, as a FakeTarget asks them

    def listing(self, folder: str) -> list[Entry]:
        prefix = f"{folder}/" if folder else ""
        return [
            self._entry(path, stored)
            for path, stored in sorted(self._files.items())
            if path.startswith(prefix)
            and "/" not in path[len(prefix) :]
            and stored.listed_from <= self.requests
        ]

    def reading(self, path: str, trouble: Trouble | None) -> Read:
        stored = self._files.get(path)
        if stored is None:
            raise NotThereError(path)
        data = stored.data
        if trouble is Trouble.HALF or path in self._halves:
            self._halves.discard(path)
            data = data[: len(data) // 2]
        return Read(data, self._tag_of(stored))

    def writing(
        self, path: str, data: bytes, condition: Condition, trouble: Trouble | None
    ) -> str | None:
        if self.full:
            raise FullError()
        self._check(path, condition)
        return self._tag_of(self._write(path, data, late=trouble is Trouble.LATE))

    def deleting(self, path: str, condition: AsRead) -> None:
        if path not in self._files:
            raise NotThereError(path)
        self._check(path, condition)
        self._remove(path)

    def _shown(self) -> int:
        """How many events changes show now: in order, up to the first not shown yet."""
        count = 0
        for event in self._events:
            if event.shown_from > self.requests:
                break
            count += 1
        return count

    def _cursor(self, at: int) -> str:
        return f"{self._cursor_age}:{at}"

    def _from(self, cursor: str) -> int:
        """Where a cursor starts; CursorExpiredError if it is of an earlier age."""
        age, _, at = cursor.partition(":")
        if int(age) != self._cursor_age:
            raise CursorExpiredError()
        return int(at)

    def page(self, cursor: str | None) -> tuple[Changes, bool]:
        """One page of changes since cursor, and whether more follow now."""
        if cursor is None:
            entries = self._list_all()
            return Changes(tuple(entries), (), self._cursor(self._shown())), False
        start = self._from(cursor)
        end = min(self._shown(), start + self.kind.page)
        latest: dict[str, _Event] = {}
        for event in self._events[start:end]:
            latest[event.path] = event
        changed = tuple(e.entry for e in latest.values() if e.entry is not None)
        gone = tuple(e.path for e in latest.values() if e.gone)
        return Changes(changed, gone, self._cursor(end)), end < self._shown()

    def _list_all(self) -> list[Entry]:
        return [
            self._entry(path, stored)
            for path, stored in sorted(self._files.items())
            if stored.listed_from <= self.requests
        ]

    def changed_since(self, cursor: str) -> bool:
        return self._shown() > self._from(cursor)


@dataclass
class FakeTarget:
    """One device's way into a FakeCloud (stickle.sync.targets.base.Target)."""

    cloud: FakeCloud
    device: str

    @property
    def keeps_conditions(self) -> bool:
        return self.cloud.kind.keeps_conditions

    @property
    def unique_tags(self) -> bool:
        return self.cloud.kind.tags in ("rev", "etag", "strong")

    def list(self, folder: str) -> Iterator[Entry]:
        return iter(
            self.cloud.request(self.device, "list", folder, lambda _t: self.cloud.listing(folder))
        )

    def get(self, path: str) -> Read:
        return self.cloud.request(self.device, "get", path, lambda t: self.cloud.reading(path, t))

    def put(self, path: str, data: bytes, condition: Condition) -> str | None:
        return self.cloud.request(
            self.device,
            "put",
            path,
            lambda t: self.cloud.writing(path, data, condition, t),
        )

    def delete(self, path: str, condition: AsRead) -> None:
        self.cloud.request(
            self.device, "delete", path, lambda _t: self.cloud.deleting(path, condition)
        )

    def changes(self, cursor: str | None) -> Iterator[Changes] | None:
        if not self.cloud.kind.follows:
            return None
        return self._pages(cursor)

    def _pages(self, cursor: str | None) -> Iterator[Changes]:
        more = True
        while more:
            page, more = self.cloud.request(
                self.device,
                "changes",
                cursor or "",
                lambda _t, c=cursor: self.cloud.page(c),
            )
            cursor = page.cursor
            yield page

    def wait_for_change(self, cursor: str, seconds: float) -> bool | None:
        if not self.cloud.kind.waits:
            return None
        return self.cloud.request(
            self.device,
            "wait",
            cursor,
            lambda _t: self.cloud.changed_since(cursor),
        )

    def refresh(self) -> None:
        self.cloud.refresh(self.device)


class Turns:
    """Devices, each on its own thread, let into the cloud one request at a time
    in the order chosen: order[i] picks among those waiting (in name order) for
    the i-th request, and the first of them once the order runs out. Only one
    device runs at a time, so the same order gives the same run."""

    WAIT = 10.0  # seconds before a device that never comes back is taken for stuck

    def __init__(self, order: Sequence[int] = ()) -> None:
        self._order = list(order)
        self._lock = threading.Condition()
        self._waiting: set[str] = set()
        self._done: set[str] = set()
        self._let_in: str | None = None
        self.picked: list[str] = []

    def turn(self, device: str) -> None:
        """At the door: back once this device is let in."""
        with self._lock:
            self._waiting.add(device)
            self._let_in = None if self._let_in == device else self._let_in
            self._lock.notify_all()
            if not self._lock.wait_for(lambda: self._let_in == device, self.WAIT):
                raise RuntimeError(f"{device} was never let in")
            self._waiting.discard(device)

    def run(self, devices: dict[str, Callable[[], None]]) -> dict[str, BaseException]:
        """Every device's work to its end; what any of them raised, by device."""
        failed: dict[str, BaseException] = {}

        def work(name: str, job: Callable[[], None]) -> None:
            try:
                job()
            except BaseException as error:  # reported to the test, not lost in a thread
                failed[name] = error
            finally:
                with self._lock:
                    self._done.add(name)
                    if self._let_in == name:
                        self._let_in = None
                    self._lock.notify_all()

        threads = [
            threading.Thread(target=work, args=(name, job), daemon=True)
            for name, job in devices.items()
        ]
        for thread in threads:
            thread.start()
        names = set(devices)
        with self._lock:
            while self._done != names:
                settled = self._lock.wait_for(
                    lambda: self._let_in is None and self._waiting | self._done == names, self.WAIT
                )
                if not settled:
                    raise RuntimeError("a device neither came to the door nor ended")
                if self._done == names:
                    break
                waiting = sorted(self._waiting)
                step = len(self.picked)
                choice = self._order[step] if step < len(self._order) else 0
                self._let_in = waiting[choice % len(waiting)]
                self.picked.append(self._let_in)
                self._lock.notify_all()
        for thread in threads:
            thread.join(self.WAIT)
        return failed


def by_number(troubles: Sequence[Trouble | None]) -> Callable[[Request], Trouble | None]:
    """Troubles for the first requests, in order (None: none for that one)."""

    def chosen(request: Request) -> Trouble | None:
        index = request.number - 1
        return troubles[index] if index < len(troubles) else None

    return chosen
