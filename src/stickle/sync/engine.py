"""One round of sync for a vault's notes: what changed in the vault is taken
in, what changed here is put there, and a note changed on both sides is merged
as every device merges it (stickle.sync.merge).

Note by note:

- read: the notes the vault lists with a tag other than the one last seen (or
  none), and those put there but not seen there yet;
- merged: those, and the notes changed or emptied here since they were last
  merged, this device's (stickle.sync.local) with the vault's;
- taken in: what the merge makes, here, together with the file to put kept as
  sent but not seen yet (the outbox), in one transaction, and only if nothing
  changed here meanwhile (else merged again). So the text here is at the
  version put, and a change made here after is made on it;
- put: made if the vault has none, else written over only if it is as read;
  refused, read again and merged again.

The file put stays the outbox until it is seen in the vault: the next round
reads it back, finds it there and lets it go, or puts it again. An answer lost
on the way is so taken care of. With a cloud whose tags tell every write apart
and that keeps conditions, the put going through is enough to know it is there.
With one that does not keep them, a note is read before it is put.

A file that does not open whole is left for a later round, and set aside
if the same bytes keep failing (stickle.sync.left); one written by a newer
version, or with a key this device does not have, stops the round
(stickle.sync.format). Another vault's file, or another note's, in a note's
place is neither read nor written over, unless this device has that note: its
own is put there. A note with a field this version may not change is neither
taken in nor written over, and passed over until it changes. A copy of a
note's file under another name (as a cloud's web page makes one) is merged
into the note, then deleted. Secret notes are not synced yet.

A note the vault had and has no file for any more (read, and none there: a
list may show a file late) is taken for one emptied elsewhere and its
deletion cleared a year after only if this device last went through a round
with the vault more than a year ago: it goes to the trash here, not emptied,
and is put with its text if it was edited here since, else kept here only.
Otherwise it cannot have been cleared: the vault went back to an earlier day,
or the file was deleted, and this device's is put back. One this device
emptied too is let go of if its deletion was a year in the vault, else put
back as it is.

By the cloud's time: notes a year in the trash since that reached the cloud
are emptied, and a deletion a year in the vault is removed from it.

A cloud full turns a put down: the round goes on taking in what changed
there, and puts nothing more; what was not put stays in the outbox.
"""

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace

import apsw

from stickle.data import sync_state
from stickle.data.notes import NoteRepository
from stickle.data.record_stamps import CATEGORY, MARK
from stickle.data.sync_left import NOT_OURS, READ_ONLY
from stickle.data.sync_state import NoteState
from stickle.sync import local, local_reminders
from stickle.sync.contents import ReadOnlyItemError
from stickle.sync.devices import About, sync_devices
from stickle.sync.format import (
    NOTES,
    FormatError,
    NotOursError,
    UnreadableError,
    item_at,
    item_path,
    json_bytes,
    open_envelope,
    seal,
)
from stickle.sync.left import Leaving
from stickle.sync.local_records import Shown, own_name
from stickle.sync.merge import TRASH, YEAR, Merged, as_sent, deletion_due, merge, trash_due
from stickle.sync.note_file import NoteFile, Step
from stickle.sync.place import Place, writing
from stickle.sync.records import RecordReport, RecordRound
from stickle.sync.targets.base import (
    Absent,
    AsRead,
    Entry,
    FullError,
    NotThereError,
    Read,
    RefusedError,
)
from stickle.sync.vault import confirm

TRIES = 5  # merges of one note in a round, refused each time, before it is left
__all__ = ["Place", "Report", "sync_notes", "sync_vault"]


@dataclass
class Report:
    changed: list[str] = field(default_factory=list[str])  # notes changed here
    put: list[str] = field(default_factory=list[str])  # notes put in the vault
    left: list[str] = field(default_factory=list[str])  # notes left for a later round
    # Notes put in the trash here, their files gone from the vault while this
    # device was away for more than a year (said once: emptied elsewhere).
    trashed: list[str] = field(default_factory=list[str])
    full: bool = False  # a put turned down, the cloud full: nothing more put
    records: RecordReport = field(default_factory=RecordReport)
    device: str = ""  # the id the device went on as (a new one if its old one was taken)


@dataclass(frozen=True)
class _Read:
    note: NoteFile | None  # None: no file
    tag: str | None
    kept: str | None = None  # the file as kept here, when it is not read but taken as last seen
    foreign: bool = False  # a file there that is not the note's: another vault's, say
    raw: Read | None = None  # as read


@dataclass(frozen=True)
class _Copy:
    """A copy of a note's file under another name, to merge into the note."""

    path: str
    read: Read
    note: NoteFile


class _LaterError(Exception):
    """The note is left for a later round."""


class _PassedError(Exception):
    """The note is not this version's to change: passed over."""


def _same(a: NoteFile, b: NoteFile) -> bool:
    return json_bytes(a.to_json()) == json_bytes(b.to_json())


def sync_notes(
    connection: apsw.Connection,
    place: Place,
    cloud_now: Callable[[], int],
    notes: NoteRepository | None = None,
) -> Report:
    """One round; cloud_now: the time by the cloud's clock (stickle.core.cloud_time)."""
    return _Round(connection, notes or NoteRepository(connection), place, cloud_now).run()


def sync_vault(
    connection: apsw.Connection,
    place: Place,
    cloud_now: Callable[[], int],
    notes: NoteRepository | None = None,
    shown: Shown = own_name,
    about: About | None = None,
    full: bool = True,
) -> Report:
    """One round of the whole vault: vault.json first, the vault still the one
    it was (stickle.sync.vault.confirm: VaultError, else nothing is done); this
    device's own file (about: what it says of itself; None: not now) and the
    others'; the records, so that the categories and marks notes name are here
    when the notes come; then the notes, and again those that name a category
    or mark that came, a field of theirs perhaps not taken before for want of
    it. full: a full look (at start, once a day), with what only that reads."""
    confirm(place.target, place.vault)
    if about is not None:
        place = replace(place, device=sync_devices(connection, place, cloud_now, about, full))
    records = RecordRound(connection, place, cloud_now, shown=shown).run()
    came = [item for kind, item in records.changed if kind in (CATEGORY, MARK)]
    also = sync_state.mentioning(connection, place.vault, came)
    notes_round = _Round(connection, notes or NoteRepository(connection), place, cloud_now)
    notes_round.full = records.full
    report = notes_round.run(also)
    report.full = report.full or records.full
    report.records = records
    return report


class _Round:
    def __init__(
        self,
        connection: apsw.Connection,
        notes: NoteRepository,
        place: Place,
        cloud_now: Callable[[], int],
    ) -> None:
        self._db = connection
        self._notes = notes
        self._place = place
        self._now = cloud_now
        self._report = Report(device=place.device)
        self._trusted = place.target.keeps_conditions and place.target.unique_tags
        self._redirects = local.Redirects({}, {})
        self._leaving = Leaving(connection, place, cloud_now)
        self._copies: dict[str, list[_Copy]] = {}
        self._last = sync_state.synced_at(connection, place.vault)
        self.full = False  # the cloud full: nothing put

    def run(self, also: list[str] | None = None) -> Report:
        """also: notes merged whether or not they changed."""
        vault, target = self._place.vault, self._place.target
        started = self._now()
        self._empty_old_trash()
        known = sync_state.known(self._db, vault)
        to_read: set[str] = set(sync_state.unconfirmed(self._db, vault))
        listed: set[str] = set()
        for entry in target.list(NOTES):
            passed = self._leaving.listed(entry)
            found = item_at(entry.path)
            if found is None or found[0] != NOTES:
                copy_of = None if passed else self._copy_of(entry)
                if copy_of is not None:
                    to_read.add(copy_of)
                continue
            note_id = found[1]
            listed.add(note_id)
            if passed:
                left = self._leaving.passed(entry.path)
                mine = note_id in known or self._notes.get(note_id) is not None
                if left is not None and left.why != READ_ONLY and mine:
                    to_read.add(note_id)  # this device's own to be put in its place
                continue
            state = known.get(note_id)
            if entry.tag is None or state is None or state.seen is None or state.tag != entry.tag:
                to_read.add(note_id)
        for note_id, state in known.items():  # not listed: looked for, a list may be late
            if note_id not in listed and state.seen is not None and not state.gone:
                to_read.add(note_id)
        here = set(sync_state.changed(self._db, vault)) | set(sync_state.emptied(self._db, vault))
        here |= set(also or [])
        self._redirects = local.Redirects.here(self._db)
        queue = sorted(to_read | here)
        done: set[str] = set()
        while queue:
            note_id = queue.pop(0)
            if note_id in done:
                continue
            done.add(note_id)
            try:
                queue += self._sync(note_id, note_id in to_read)
            except _LaterError:
                self._report.left.append(note_id)
            except _PassedError:
                pass
        self._remove_old_deletions()
        with writing(self._db):
            sync_state.set_synced_at(self._db, vault, started)
        self._report.full = self.full
        return self._report

    def _copy_of(self, entry: Entry) -> str | None:
        """A file named otherwise in notes/: the note it is a copy of, if it opens
        as one of this vault's, kept to merge into it; else it is left as it is."""
        place = self._place
        try:
            read = place.target.get(entry.path)
        except NotThereError:
            return None
        try:
            head, value = open_envelope(place.keys, read.data, place.vault, NOTES)
            note = NoteFile.from_json(place.keys, value)
        except FormatError:
            self._leaving.leave(entry.path, read, NOT_OURS)
            return None
        self._copies.setdefault(head.item, []).append(_Copy(entry.path, read, note))
        return head.item

    def _read(self, note_id: str) -> _Read:
        place, path = self._place, item_path(NOTES, note_id)
        left = self._leaving.passed(path)
        if left is not None:  # listed as it was left: not read again
            if left.why == READ_ONLY:
                raise _PassedError()
            return _Read(None, left.tag, foreign=True)
        try:
            read = place.target.get(path)
        except NotThereError:
            return _Read(None, None)
        why = self._leaving.why(path, read)
        if why == READ_ONLY:
            raise _PassedError()
        if why is not None:
            return _Read(None, read.tag, foreign=True)
        try:
            head, value = open_envelope(place.keys, read.data, place.vault, NOTES)
            note = NoteFile.from_json(place.keys, value)
        except NotOursError:
            self._leaving.leave(path, read, NOT_OURS)
            return _Read(None, read.tag, foreign=True)
        except UnreadableError as error:
            if self._leaving.unreadable(path, read):
                return _Read(None, read.tag, foreign=True)  # set aside: written over
            raise _LaterError() from error
        if head.item != note_id:  # another note's file in its place
            self._leaving.leave(path, read, NOT_OURS)
            return _Read(None, read.tag, foreign=True)
        self._leaving.opened(path)
        return _Read(note, read.tag, raw=read)

    def _sync(self, note_id: str, read: bool) -> list[str]:
        """Merged (with its copies under other names first), taken in, put; the
        conflict copies it made, to sync after it. Its copies, then deleted."""
        left = self._leaving.passed(item_path(NOTES, note_id))
        if left is not None and left.why == READ_ONLY:
            raise _PassedError()
        made: list[str] = []
        folded: list[_Copy] = []
        for copy in self._copies.pop(note_id, []):
            copies = self._fold(note_id, copy)
            if copies is not None:
                made += copies
                folded.append(copy)
        made += self._sync_note(note_id, read or bool(folded), bool(folded))
        for copy in folded:
            with suppress(NotThereError, RefusedError):  # gone, or changed since: next round
                self._place.target.delete(copy.path, AsRead(copy.read.tag))
        return made

    def _fold(self, note_id: str, copy: _Copy) -> list[str] | None:
        """A copy merged into the note here, as if the vault had it: the conflict
        copies made. One this version may not change is left as it is (None)."""
        state = sync_state.get(self._db, self._place.vault, note_id)
        try:
            outcome = self._merge(note_id, state, _Read(copy.note, None))
        except _PassedError:
            self._leaving.leave(copy.path, copy.read, NOT_OURS)
            return None
        return [] if outcome is None else outcome[1]

    def _sync_note(self, note_id: str, read: bool, folded: bool) -> list[str]:
        for _ in range(TRIES):
            state = sync_state.get(self._db, self._place.vault, note_id)
            seen = local.kept(None if state is None else state.seen)
            # Its text taken out here, the note emptied (or made secret): what is
            # kept is the version a deletion is made on, not the vault's text.
            taken_out = seen is not None and seen.body.text is None and not seen.body.deleted
            if taken_out and self._notes.get(note_id) is not None:
                read = True
            if read or not self._place.target.keeps_conditions:
                cloud = self._read(note_id)
                if cloud.note is None and not cloud.foreign and seen is not None and not folded:
                    assert state is not None
                    if not self._lost(note_id, state, seen):
                        return []
            else:
                tag, kept = (None, None) if state is None else (state.tag, state.seen)
                cloud = _Read(seen, tag, kept)
            outcome = self._merge(note_id, state, cloud)
            if outcome is None:
                return []
            put, copies = outcome
            if put is None:
                return copies
            if self.full:
                raise _LaterError()  # kept in the outbox, put once there is room
            try:
                tag = self._place.target.put(
                    item_path(NOTES, note_id),
                    seal(self._place.keys, self._place.vault, NOTES, note_id, put.to_json()),
                    Absent() if cloud.note is None and not cloud.foreign else AsRead(cloud.tag),
                )
            except RefusedError:
                read = True
                continue
            except FullError as error:
                self.full = True
                raise _LaterError() from error
            self._sent(note_id, put, tag)
            return copies
        raise _LaterError()

    def _merge(
        self, note_id: str, state: NoteState | None, cloud: _Read
    ) -> tuple[NoteFile | None, list[str]] | None:
        """The note merged and taken in: the file to put (None: the vault has it)
        and the conflict copies made. None: nothing to do with it."""
        place = self._place
        for _ in range(TRIES):
            at = local.here(self._db, self._notes, note_id)
            if at.note is not None and at.note.secret:
                return None
            mine = local.mine(place.keys, place.device, at, state)
            if cloud.note is not None:
                try:
                    cloud.note.contents.check_writable(local.known(cloud.note.contents))
                except ReadOnlyItemError as error:
                    if cloud.raw is not None:
                        self._leaving.leave(item_path(NOTES, note_id), cloud.raw, READ_ONLY)
                    raise _PassedError() from error
            if mine is None and cloud.note is None:
                return None
            if mine is not None and cloud.note is not None:
                merged = merge(place.keys, note_id, mine, cloud.note)
            elif mine is not None:
                merged = Merged(mine, Step.SEND)
            else:
                assert cloud.note is not None
                merged = Merged(cloud.note, Step.RECEIVE)
            if mine is not None and cloud.note is not None:
                fitting = local_reminders.for_this_setting(
                    merged.note.contents, mine.contents, cloud.note.contents
                )
                merged = replace(merged, note=replace(merged.note, contents=fitting))
            merged = self._redirected(merged)
            now = self._now()
            put = None
            if cloud.note is None or not _same(merged.note, cloud.note):
                put = as_sent(merged.note, now)
            with writing(self._db):
                if local.here(self._db, self._notes, note_id) != at:
                    continue  # changed here meanwhile: merged again
                before = None if at.note is None else at.note.change_seq
                kept = put or merged.note
                seq = local.take(self._db, self._notes, place.device, note_id, kept, at)
                seen = cloud.kept
                if seen is None and cloud.note is not None:
                    # Emptied here: no text of it kept, the vault's neither.
                    seen = local.as_kept(cloud.note, text=not kept.body.deleted)
                sync_state.put(
                    self._db,
                    place.vault,
                    note_id,
                    NoteState(
                        seen,
                        cloud.tag,
                        None if put is None else local.as_kept(put),
                        seq if seq is not None else (0 if state is None else state.merged_seq),
                        kept.body.deleted,
                    ),
                )
                copies = self._copy(merged, now)
            if seq != before:
                self._report.changed.append(note_id)
            return put, copies
        raise _LaterError()

    def _lost(self, note_id: str, state: NoteState, seen: NoteFile) -> bool:
        """Its file read, and none there: True to put this device's back; False,
        dealt with here."""
        at = local.here(self._db, self._notes, note_id)
        if at.note is not None and at.note.secret:
            return False  # not synced yet
        vault, now = self._place.vault, self._now()
        if state.gone:  # kept here only: put back if changed here since
            if at.note is None:
                with writing(self._db):
                    sync_state.drop(self._db, vault, note_id)
                return False
            return at.note.change_seq > state.merged_seq
        if state.deleted:
            if not deletion_due(seen, now):
                return True
            with writing(self._db):
                sync_state.drop(self._db, vault, note_id)
                self._notes.forget_deletion(note_id)
            return False
        if self._last is None or now - self._last <= YEAR:
            return True  # it cannot have been cleared: the vault went back, or it was deleted
        mine = local.mine(self._place.keys, self._place.device, at, state)
        if at.note is not None and at.note.deleted_at is None:
            self._notes.delete(note_id)
            self._report.trashed.append(note_id)
            self._report.changed.append(note_id)
        if mine is not None and mine.body.id != seen.body.id:
            return True  # edited here since: put in the trash, with its text
        with writing(self._db):
            here = local.here(self._db, self._notes, note_id)
            seq = state.merged_seq if here.note is None else here.note.change_seq
            sync_state.put(
                self._db, vault, note_id, replace(state, merged_seq=seq, outbox=None, gone=True)
            )
        return False

    def _redirected(self, merged: Merged) -> Merged:
        """As it names categories and marks now (stickle.sync.local.redirect)."""
        note = local.redirect(merged.note, self._redirects)
        copy = merged.copy
        if copy is not None:
            copy = (copy[0], local.redirect(copy[1], self._redirects))
        return replace(merged, note=note, copy=copy)

    def _copy(self, merged: Merged, now: int) -> list[str]:
        """The conflict copy the merge made, here, to put: unless it is here already."""
        if merged.copy is None:
            return []
        copy_id, copy = merged.copy
        place = self._place
        at = local.here(self._db, self._notes, copy_id)
        if at.note is not None or at.emptied_at is not None:
            return []
        if sync_state.get(self._db, place.vault, copy_id) is not None:
            return []
        made = as_sent(copy, now)
        seq = local.take(self._db, self._notes, place.device, copy_id, made, at)
        sync_state.put(
            self._db,
            place.vault,
            copy_id,
            NoteState(outbox=local.as_kept(made), merged_seq=seq or 0),
        )
        self._report.changed.append(copy_id)
        return [copy_id]

    def _sent(self, note_id: str, put: NoteFile, tag: str | None) -> None:
        """The file put went through: seen, and let go of if that is enough to know."""
        vault = self._place.vault
        with writing(self._db):
            state = sync_state.get(self._db, vault, note_id)
            if state is None or state.outbox != local.as_kept(put):
                return
            outbox = None if self._trusted else state.outbox
            sync_state.put(
                self._db, vault, note_id, replace(state, seen=state.outbox, tag=tag, outbox=outbox)
            )
        self._report.put.append(note_id)

    def _empty_old_trash(self) -> None:
        """Notes in the trash a year since that reached the cloud (put in the
        trash again since: from then), emptied."""
        place = self._place
        now = self._now()
        known = sync_state.known(self._db, place.vault)
        for note in self._notes.deleted():
            state = known.get(note.id)
            if note.secret or state is None:
                continue
            at = local.here(self._db, self._notes, note.id)
            mine = local.mine(place.keys, place.device, at, state)
            if mine is None:
                continue
            if state.gone:  # kept here only: a year from when it went in the trash
                trash = mine.contents.fields.get(TRASH)
                if trash is None or trash.value is None or now - trash.hlc.millis < YEAR:
                    continue
                with writing(self._db):
                    sync_state.drop(self._db, place.vault, note.id)
            elif not trash_due(mine, now):
                continue
            self._notes.purge(note.id)
            self._report.changed.append(note.id)

    def _remove_old_deletions(self) -> None:
        """Deletions a year in the vault: their files removed, and forgotten here."""
        place = self._place
        now = self._now()
        for note_id, state in sorted(sync_state.known(self._db, place.vault).items()):
            if not state.deleted or state.outbox is not None:
                continue  # only deletions seen in the vault are read here
            seen = local.kept(state.seen)
            if seen is None or not deletion_due(seen, now):
                continue
            tag = state.tag
            if not self._trusted:
                try:
                    cloud = self._read(note_id)
                except _LaterError, _PassedError:
                    continue
                if cloud.note is not None and not _same(cloud.note, seen):
                    continue  # changed since: the next round merges it
                tag = cloud.tag
            try:
                place.target.delete(item_path(NOTES, note_id), AsRead(tag))
            except NotThereError:
                pass
            except RefusedError:
                continue
            with writing(self._db):
                sync_state.drop(self._db, place.vault, note_id)
                self._notes.forget_deletion(note_id)
