"""One round of sync for a vault's records (stickle.sync.record_file): what
changed in the vault is taken in, what changed here is put there, each field's
later change kept (stickle.sync.merge.merge_fields).

Record by record, as notes are (stickle.sync.engine): read, those the vault
lists with a tag other than the one last seen and those put but not seen there
yet; merged, those and the items changed here since they were last merged;
taken in, here, with the file to put kept as sent until it is seen there, in
one transaction, only if the item did not change here meanwhile; put, made if
the vault has none, else written over only if it is as read.

A record of a kind this version does not know, or with a field it may not
change, is left as it is. One whose file does not open whole, or that is not
where its own id says, is left for a later round.
"""

from collections.abc import Callable
from dataclasses import dataclass, field, replace

import apsw

from stickle.core.clock import Clock, utc_now
from stickle.data import record_state
from stickle.data.record_state import RecordState
from stickle.sync import local_records
from stickle.sync.contents import Contents, ReadOnlyItemError
from stickle.sync.format import (
    RECORDS,
    NotOursError,
    UnreadableError,
    item_at,
    item_path,
    json_bytes,
    open_envelope,
    parse_json,
    seal,
)
from stickle.sync.merge import merge_fields
from stickle.sync.place import Place, writing
from stickle.sync.record_file import RecordFile, record_id
from stickle.sync.targets.base import Absent, AsRead, NotThereError, RefusedError

TRIES = 5


@dataclass
class RecordReport:
    changed: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])  # here
    put: list[str] = field(default_factory=list[str])  # records put in the vault
    left: list[str] = field(default_factory=list[str])  # left for a later round


@dataclass(frozen=True)
class _Read:
    record: RecordFile | None  # None: no file
    tag: str | None


class _LaterError(Exception):
    """The record is left for a later round."""


class _LeftError(Exception):
    """The record is not this version's to change."""


def kept(text: str | None) -> RecordFile | None:
    return None if text is None else RecordFile.from_json(parse_json(text.encode("utf-8")))


def as_kept(record: RecordFile) -> str:
    return json_bytes(record.to_json()).decode("utf-8")


def _same(a: RecordFile, b: RecordFile) -> bool:
    return json_bytes(a.to_json()) == json_bytes(b.to_json())


def _as_sent(contents: Contents, cloud_now: int) -> Contents:
    fields = {
        name: each if each.sent is not None else replace(each, sent=cloud_now)
        for name, each in contents.fields.items()
    }
    return replace(contents, fields=fields)


class RecordRound:
    def __init__(
        self,
        connection: apsw.Connection,
        place: Place,
        cloud_now: Callable[[], int],
        clock: Clock = utc_now,
        shown: local_records.Shown = local_records.own_name,
    ) -> None:
        """shown: the name a mark shows here (a built-in one's, in this language)."""
        self._db = connection
        self._place = place
        self._now = cloud_now
        self._clock = clock
        self._shown = shown
        self._report = RecordReport()
        self._trusted = place.target.keeps_conditions and place.target.unique_tags

    def run(self, also: list[tuple[str, str]] | None = None) -> RecordReport:
        """also: items changed here in this round already, merged again."""
        vault, keys = self._place.vault, self._place.keys
        known = record_state.known(self._db, vault)
        to_read: set[str] = set(record_state.unconfirmed(self._db, vault))
        for entry in self._place.target.list(RECORDS):
            found = item_at(entry.path)
            if found is None or found[0] != RECORDS:
                continue
            state = known.get(found[1])
            if entry.tag is None or state is None or state.seen is None or state.tag != entry.tag:
                to_read.add(found[1])
        here = {
            record_id(keys, kind, item): (kind, item)
            for kind, item in [*record_state.changed(self._db, vault), *(also or [])]
        }
        for record in sorted(to_read | here.keys()):
            self._try(record, record in to_read, here.get(record))
        with writing(self._db):
            alike = local_records.merge_alike(self._db, self._now(), self._clock(), self._shown)
        for kind, item in alike:
            self._report.changed.append((kind, item))
            self._try(record_id(keys, kind, item), False, (kind, item))
        return self._report

    def _try(self, record: str, read: bool, item: tuple[str, str] | None) -> None:
        try:
            self.sync(record, read, item)
        except _LaterError:
            self._report.left.append(record)
        except _LeftError:
            pass

    def _read(self, record: str) -> _Read:
        place = self._place
        try:
            read = place.target.get(item_path(RECORDS, record))
        except NotThereError:
            return _Read(None, None)
        try:
            head, value = open_envelope(place.keys, read.data, place.vault, RECORDS)
            if head.item != record:
                raise _LaterError()  # another record's file in its place
            found = RecordFile.from_json(value)
        except (UnreadableError, NotOursError) as error:
            raise _LaterError() from error
        if found.known and record_id(place.keys, found.kind, found.item) != record:
            raise _LaterError()  # not where its id says
        return _Read(found, read.tag)

    def sync(self, record: str, read: bool, item: tuple[str, str] | None) -> None:
        """Merged, taken in, put."""
        for _ in range(TRIES):
            state = record_state.get(self._db, self._place.vault, record)
            seen = kept(None if state is None else state.seen)
            if read or not self._place.target.keeps_conditions:
                cloud = self._read(record)
                if cloud.record is None and seen is not None:
                    raise _LaterError()  # gone from the vault: nothing to make of it yet
            else:
                cloud = _Read(seen, None if state is None else state.tag)
            put = self._merge(record, state, cloud, item)
            if put is None:
                return
            try:
                tag = self._place.target.put(
                    item_path(RECORDS, record),
                    seal(self._place.keys, self._place.vault, RECORDS, record, put.to_json()),
                    Absent() if cloud.record is None else AsRead(cloud.tag),
                )
            except RefusedError:
                read = True
                continue
            self._sent(record, put, tag)
            return
        raise _LaterError()

    def _merge(
        self, record: str, state: RecordState | None, cloud: _Read, item: tuple[str, str] | None
    ) -> RecordFile | None:
        """The record merged and taken in: the file to put (None: the vault has it)."""
        if cloud.record is not None:
            if not cloud.record.known:
                raise _LeftError()
            try:
                cloud.record.contents.check_writable(
                    local_records.COLUMNS.get(cloud.record.kind, {})
                )
            except ReadOnlyItemError as error:
                raise _LeftError() from error
            kind, name = cloud.record.kind, cloud.record.item
        elif item is not None:
            kind, name = item
        elif state is not None:
            kind, name = state.kind, state.item
        else:
            return None
        head = kept(state.outbox if state is not None and state.outbox else None) or kept(
            None if state is None else state.seen
        )
        device = self._place.device
        for _ in range(TRIES):
            at = local_records.here(self._db, kind, name)
            mine = local_records.mine(device, at, kind, name, head)
            if mine is None and cloud.record is None:
                return None
            if mine is not None and cloud.record is not None:
                merged = RecordFile(kind, name, merge_fields(mine.contents, cloud.record.contents))
            else:
                merged = mine if mine is not None else cloud.record
                assert merged is not None
            put = None
            if cloud.record is None or not _same(merged, cloud.record):
                put = replace(merged, contents=_as_sent(merged.contents, self._now()))
            with writing(self._db):
                if local_records.here(self._db, kind, name) != at:
                    continue  # changed here meanwhile: merged again
                kept_file = put or merged
                seq = local_records.take(self._db, device, kept_file, at, self._clock())
                record_state.put(
                    self._db,
                    self._place.vault,
                    record,
                    RecordState(
                        kind,
                        name,
                        None if cloud.record is None else as_kept(cloud.record),
                        cloud.tag,
                        None if put is None else as_kept(put),
                        at.seq if seq is None else seq,
                    ),
                )
            if seq is not None and seq != at.seq:
                self._report.changed.append((kind, name))
            return put
        raise _LaterError()

    def _sent(self, record: str, put: RecordFile, tag: str | None) -> None:
        vault = self._place.vault
        with writing(self._db):
            state = record_state.get(self._db, vault, record)
            if state is None or state.outbox != as_kept(put):
                return
            outbox = None if self._trusted else state.outbox
            record_state.put(
                self._db, vault, record, replace(state, seen=state.outbox, tag=tag, outbox=outbox)
            )
        self._report.put.append(record)
