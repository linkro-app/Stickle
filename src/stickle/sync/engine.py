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

A file that does not open whole is left for a later round; one written by a
newer version, or with a key this device does not have, stops the round
(stickle.sync.format). Secret notes are not synced yet. A note the vault had
and has no file for any more, it says nothing of yet.

By the cloud's time: notes a year in the trash since that reached the cloud
are emptied, and a deletion a year in the vault is removed from it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field, replace

import apsw

from stickle.data import sync_state
from stickle.data.notes import NoteRepository
from stickle.data.record_stamps import CATEGORY, MARK
from stickle.data.sync_state import NoteState
from stickle.sync import local, local_reminders
from stickle.sync.contents import ReadOnlyItemError
from stickle.sync.format import (
    NOTES,
    NotOursError,
    UnreadableError,
    item_at,
    item_path,
    json_bytes,
    open_envelope,
    seal,
)
from stickle.sync.local_records import Shown, own_name
from stickle.sync.merge import Merged, as_sent, deletion_due, merge, trash_due
from stickle.sync.note_file import NoteFile, Step
from stickle.sync.place import Place, writing
from stickle.sync.records import RecordReport, RecordRound
from stickle.sync.targets.base import Absent, AsRead, NotThereError, RefusedError

TRIES = 5  # merges of one note in a round, refused each time, before it is left
__all__ = ["Place", "Report", "sync_notes", "sync_vault"]


@dataclass
class Report:
    changed: list[str] = field(default_factory=list[str])  # notes changed here
    put: list[str] = field(default_factory=list[str])  # notes put in the vault
    left: list[str] = field(default_factory=list[str])  # notes left for a later round
    records: RecordReport = field(default_factory=RecordReport)


@dataclass(frozen=True)
class _Read:
    note: NoteFile | None  # None: no file
    tag: str | None
    kept: str | None = None  # the file as kept here, when it is not read but taken as last seen


class _LaterError(Exception):
    """The note is left for a later round."""


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
) -> Report:
    """One round of the whole vault: the records first, so that the categories
    and marks notes name are here when the notes come; then the notes, and
    again those that name a category or mark that came, a field of theirs
    perhaps not taken before for want of it."""
    records = RecordRound(connection, place, cloud_now, shown=shown).run()
    came = [item for kind, item in records.changed if kind in (CATEGORY, MARK)]
    also = sync_state.mentioning(connection, place.vault, came)
    report = _Round(connection, notes or NoteRepository(connection), place, cloud_now).run(also)
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
        self._report = Report()
        self._trusted = place.target.keeps_conditions and place.target.unique_tags
        self._redirects = local.Redirects({}, {})

    def run(self, also: list[str] | None = None) -> Report:
        """also: notes merged whether or not they changed."""
        vault, target = self._place.vault, self._place.target
        self._empty_old_trash()
        known = sync_state.known(self._db, vault)
        to_read: set[str] = set(sync_state.unconfirmed(self._db, vault))
        for entry in target.list(NOTES):
            found = item_at(entry.path)
            if found is None or found[0] != NOTES:
                continue  # a copy under another name, say
            note_id = found[1]
            state = known.get(note_id)
            if entry.tag is None or state is None or state.seen is None or state.tag != entry.tag:
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
        self._remove_old_deletions()
        return self._report

    def _read(self, note_id: str) -> _Read:
        place = self._place
        try:
            read = place.target.get(item_path(NOTES, note_id))
        except NotThereError:
            return _Read(None, None)
        try:
            head, value = open_envelope(place.keys, read.data, place.vault, NOTES)
            if head.item != note_id:
                raise _LaterError()  # another note's file in its place
            return _Read(NoteFile.from_json(place.keys, value), read.tag)
        except (UnreadableError, NotOursError) as error:
            raise _LaterError() from error

    def _sync(self, note_id: str, read: bool) -> list[str]:
        """Merged, taken in, put; the conflict copies it made, to sync after it."""
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
                if cloud.note is None and seen is not None:
                    raise _LaterError()  # gone from the vault: not taken for a deletion here
            else:
                tag, kept = (None, None) if state is None else (state.tag, state.seen)
                cloud = _Read(seen, tag, kept)
            outcome = self._merge(note_id, state, cloud)
            if outcome is None:
                return []
            put, copies = outcome
            if put is None:
                return copies
            try:
                tag = self._place.target.put(
                    item_path(NOTES, note_id),
                    seal(self._place.keys, self._place.vault, NOTES, note_id, put.to_json()),
                    Absent() if cloud.note is None else AsRead(cloud.tag),
                )
            except RefusedError:
                read = True
                continue
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
                    raise _LaterError() from error
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
            if mine is None or not trash_due(mine, now):
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
                except _LaterError:
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
