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

A round looks before it takes anything in: what the vault lists is compared
with what was seen, and the files to read are read then, one at a time, and
kept here (stickle.data.sync_staged); they are taken in from there. One kept by
a round that broke off, listed with the same tag since, is not fetched again
where tags tell writes apart.

Listing a folder takes more the more notes there are, so a round of the whole
vault lists them only when it has to: on a full look (at start, once a day),
never having listed them, or with a device so far ahead that its file no longer
names all it put. Otherwise it reads the files the other devices say they put
(stickle.sync.devices), and those are read on a full look too: a write that
left its file the tag it had shows only so.

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

A device that joined a vault which was there before it (joined) merges as a
first connection does until a round is through: its untouched welcome note,
and a what's-new note the vault does not have, untouched, are let go of, not
put (stickle.core.made_notes: they can be made again, and the vault stands for
them); a note of its own with the same text as one of the vault's, both out of
the trash, is folded into that one, as one note changed on two devices (its
places on screen, size and earlier texts handed over), and no big change is
stopped for. What that would make is told before (first_look); keeping only
the vault's notes instead (cloud_only) backs up first and removes this
device's without a trace.

A big change stops the round once it has looked, before anything is taken in
or put (BigChangeError): ten notes or more here, out of the trash, that would
go to it or be deleted for good, and conflict copies that would be made, all
told. What it stopped for is kept (stickle.data.sync_held); a round the user
lets take that in (accept) backs up first, and stops again only if ten or more
of what it would bring were not among it. Notes not here, texts changed, notes
taken out of the trash or emptied from it are not counted.
"""

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace

import apsw

from stickle.core import made_notes
from stickle.core.markdown import note_title
from stickle.data import record_state, sync_held, sync_staged, sync_state
from stickle.data.notes import NoteRepository
from stickle.data.record_stamps import CATEGORY, MARK
from stickle.data.settings import DEVICE_ID, Settings
from stickle.data.sync_held import Held
from stickle.data.sync_left import NOT_OURS, READ_ONLY
from stickle.data.sync_staged import Staged
from stickle.data.sync_state import NoteState
from stickle.sync import local, local_reminders
from stickle.sync.contents import ReadOnlyItemError
from stickle.sync.devices import About, Looked, sync_devices, tell
from stickle.sync.format import (
    NOTES,
    RECORDS,
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
from stickle.sync.merge import (
    TRASH,
    YEAR,
    Merged,
    as_sent,
    deletion_due,
    merge,
    merge_fields,
    trash_due,
)
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
BIG = 10  # changes a round stops for (to the trash, deleted for good, conflict copies)
GETTING = "getting"  # notes' files being read from the vault
PUTTING = "putting"  # notes being looked after to go to it
# Told as a round goes: what it is doing, how many notes so far, of how many.
type Progress = Callable[[str, int, int], None]
__all__ = [
    "GETTING",
    "PUTTING",
    "BigChangeError",
    "FirstLook",
    "Place",
    "Progress",
    "Report",
    "cloud_only",
    "first_look",
    "joined",
    "sync_notes",
    "sync_vault",
]


class BigChangeError(Exception):
    """The round would bring a big change: it stopped before taking any of it
    in. held: what it stopped for (stickle.data.sync_held, kept there too)."""

    def __init__(self, held: list[Held]) -> None:
        kinds = {k: [e.kind for e in held].count(k) for k in sorted({e.kind for e in held})}
        super().__init__(", ".join(f"{count} {kind}" for kind, count in kinds.items()))
        self.held = held


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
class FirstLook:
    """What merging would make of this device's notes and a vault's it joined."""

    here: int  # this device's notes out of the trash, but those let go of
    cloud: int  # the vault's notes out of the trash
    alike: int  # of this device's, those that become one with one of the vault's
    recent: list[str | None]  # the first lines of the vault's three edited last (None: secret)


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


def _items(told: frozenset[str], folder: str) -> set[str]:
    """The items of folder among these files."""
    found = (item_at(path) for path in told)
    return {at[1] for at in found if at is not None and at[0] == folder}


def _same(a: NoteFile, b: NoteFile) -> bool:
    return json_bytes(a.to_json()) == json_bytes(b.to_json())


def _made(note_id: str, text: str) -> bool:
    """A note the app made, untouched: the welcome note, or a what's-new note."""
    return made_notes.welcome(text) or made_notes.news(note_id, text)


def _shown(note: NoteFile) -> bool:
    """Out of the trash, and not deleted."""
    trash = note.contents.fields.get(TRASH)
    return not note.body.deleted and (trash is None or trash.value is None)


def _writable(note: NoteFile) -> bool:
    """Whether this version may take the note in: no field it may not change."""
    try:
        note.contents.check_writable(local.known(note.contents))
    except ReadOnlyItemError:
        return False
    return True


def joined(connection: apsw.Connection, vault: str) -> None:
    """This device has just joined the vault, which was there before it: until
    a round with it is through, its rounds merge as a first connection does."""
    with writing(connection):
        sync_state.set_joining(connection, vault, True)


def first_look(
    connection: apsw.Connection,
    place: Place,
    cloud_now: Callable[[], int],
    notes: NoteRepository | None = None,
) -> FirstLook:
    """What merging would make of this device's notes and those of the vault it
    joined, before its first round with it: the vault's notes read once, and
    kept for that round, which does not fetch them again."""
    confirm(place.target, place.vault)
    notes = notes or NoteRepository(connection)
    looking = _Round(connection, notes, place, cloud_now, joining=True)
    looking.look()
    return looking.preview()


def cloud_only(
    connection: apsw.Connection, notes: NoteRepository, vault: str, back_up: Callable[[], object]
) -> int:
    """Only the vault's notes kept, for a vault this device just joined: backed
    up first (back_up), then this device's notes removed without a trace, in
    the trash and secret ones too (the vault never had them), and the vault
    taken as joined. How many were removed."""
    back_up()
    gone = notes.all()
    with writing(connection):
        for note in gone:
            notes.drop(note.id)
        sync_state.set_joining(connection, vault, True)
    return len(gone)


def sync_notes(
    connection: apsw.Connection,
    place: Place,
    cloud_now: Callable[[], int],
    notes: NoteRepository | None = None,
    accept: Callable[[], object] | None = None,
) -> Report:
    """One round; cloud_now: the time by the cloud's clock (stickle.core.cloud_time);
    accept: what a round stopped for taken in after all, after this is called
    (to back up)."""
    notes = notes or NoteRepository(connection)
    return _Round(connection, notes, place, cloud_now, accept).run()


def sync_vault(
    connection: apsw.Connection,
    place: Place,
    cloud_now: Callable[[], int],
    notes: NoteRepository | None = None,
    shown: Shown = own_name,
    about: About | None = None,
    full: bool = True,
    accept: Callable[[], object] | None = None,
    report: Report | None = None,
    progress: Progress | None = None,
) -> Report:
    """One round of the whole vault: vault.json first, the vault still the one
    it was (stickle.sync.vault.confirm: VaultError, else nothing is done); this
    device's own file (about: what it says of itself; None: not now) and the
    others'; a look at the notes (what changed there, read); the records, so
    that the categories and marks notes name are here when the notes are taken
    in; then the notes, and again those that name a category
    or mark that came, a field of theirs perhaps not taken before for want of
    it. full: a full look (at start, once a day): every folder listed, and
    what else only that reads. Otherwise only what the devices said they put.
    The look stops the round for a big change (BigChangeError), records and
    notes alike; accept: what it stopped for taken in after all, after this is
    called (to back up). report: where what it does is noted as it goes, so
    that what a round did before it failed is known. progress: told as the
    notes go by, on the thread of the round.

    The device goes as the id its settings hold, if they hold one: a round that
    took a new id may have ended before its caller heard (Report.device)."""
    held = Settings(connection).get(DEVICE_ID)
    if held is not None and held != place.device:
        place = replace(place, device=held)
    confirm(place.target, place.vault)
    looked: Looked | None = None
    if about is not None:
        looked = sync_devices(connection, place, cloud_now, about, full)
        place = replace(place, device=looked.device)
    report = Report() if report is None else report
    report.device = place.device
    notes_round = _Round(
        connection,
        notes or NoteRepository(connection),
        place,
        cloud_now,
        accept,
        report=report,
        progress=progress,
    )
    told = frozenset[str]() if looked is None else looked.told
    listing = looked is None or full or not looked.enough or notes_round.first
    notes_round.look(_items(told, NOTES), listing)
    records = RecordRound(connection, place, cloud_now, shown=shown, report=report.records).run(
        told=_items(told, RECORDS), listing=listing
    )
    came = [item for kind, item in records.changed if kind in (CATEGORY, MARK)]
    also = sync_state.mentioning(connection, place.vault, came)
    notes_round.full = records.full
    notes_round.run(also)
    report.full = report.full or records.full
    if about is not None and looked is not None:
        if not report.left and not records.left:  # else read again next round
            with writing(connection):
                record_state.set_taken(connection, place.vault, looked.rounds)
        tell(connection, place, cloud_now, about)
    return report


class _Round:
    def __init__(
        self,
        connection: apsw.Connection,
        notes: NoteRepository,
        place: Place,
        cloud_now: Callable[[], int],
        accept: Callable[[], object] | None = None,
        joining: bool | None = None,
        report: Report | None = None,
        progress: Progress | None = None,
    ) -> None:
        """joining: None, as the vault was joined or not (stickle.sync.engine.joined)."""
        self._db = connection
        self._accept = accept
        self._progress = progress
        self._joining = sync_state.joining(connection, place.vault) if joining is None else joining
        self._drops: list[str] = []  # this device's made notes to let go of, joining
        self._pairs: dict[str, str] = {}  # the vault's note: this device's to fold into it
        self._notes = notes
        self._place = place
        self._now = cloud_now
        self._report = Report(device=place.device) if report is None else report
        self._trusted = place.target.keeps_conditions and place.target.unique_tags
        self._redirects = local.Redirects({}, {})
        self._leaving = Leaving(connection, place, cloud_now)
        self._copies: dict[str, list[str]] = {}  # by note: its copies under other names, kept
        self._last = sync_state.synced_at(connection, place.vault)
        self._started = 0
        self._to_read: set[str] | None = None  # None: not looked yet
        self._staged: set[str] = set()  # files kept by the look, not taken in yet
        self.full = False  # the cloud full: nothing put

    @property
    def first(self) -> bool:
        """Whether what the vault holds has to be listed all the same: never
        listed by this device, or a first connection's merge."""
        return self._last is None or self._joining

    def look(self, told: set[str] | None = None, listing: bool = True) -> None:
        """What changed in the vault, before anything is taken in: the notes other
        devices said they put (told) and, listing, the notes listed; the files
        to read read and kept here one at a time (stickle.data.sync_staged). One
        kept by an earlier round and listed with the same tag since is not
        fetched again, if tags tell writes apart."""
        vault, target = self._place.vault, self._place.target
        self._started = self._now()
        known = sync_state.known(self._db, vault)
        own = set(sync_state.unconfirmed(self._db, vault))  # put here: read back to be sure
        to_read: set[str] = own | (told or set())
        listed: dict[str, Entry] = {}
        if listing:
            for entry in target.list(NOTES):
                passed = self._leaving.listed(entry)
                found = item_at(entry.path)
                if found is None or found[0] != NOTES:
                    copy_of = None if passed else self._copy_of(entry)
                    if copy_of is not None:
                        to_read.add(copy_of)
                    continue
                note_id = found[1]
                listed[note_id] = entry
                if passed:
                    left = self._leaving.passed(entry.path)
                    mine = note_id in known or self._notes.get(note_id) is not None
                    if left is not None and left.why != READ_ONLY and mine:
                        to_read.add(note_id)  # this device's own to be put in its place
                    continue
                state = known.get(note_id)
                if (
                    entry.tag is None
                    or state is None
                    or state.seen is None
                    or state.tag != entry.tag
                ):
                    to_read.add(note_id)
            for note_id, state in known.items():  # not listed: looked for, a list may be late
                if note_id not in listed and state.seen is not None and not state.gone:
                    to_read.add(note_id)
        kept = sync_staged.tags(self._db, vault)
        fetching: list[str] = []
        for note_id in sorted(to_read):
            path = item_path(NOTES, note_id)
            if self._leaving.passed(path) is not None:
                continue  # left as it was: not read
            entry = listed.get(note_id)
            if (
                target.unique_tags
                and entry is not None
                and entry.tag is not None
                and path in kept
                and kept[path] == entry.tag
            ):
                self._staged.add(path)
                continue
            fetching.append(note_id)
        coming = sum(1 for note_id in fetching if note_id not in own)
        done = 0
        for note_id in fetching:
            self._stage(item_path(NOTES, note_id))
            if note_id not in own:
                done += 1
                self._tell(GETTING, done, coming)
        self._to_read = to_read
        if self._joining:
            self._alike(set(listed))
        else:
            self._weigh()

    def _tell(self, doing: str, done: int, of: int) -> None:
        if self._progress is not None:
            self._progress(doing, done, of)

    def _alike(self, listed: set[str]) -> None:
        """Joining: this device's made notes to let go of, and its notes with the
        same text as one of the vault's (out of the trash, each once)."""
        vault = self._place.vault
        mine: dict[str, list[str]] = {}
        for note in self._notes.all():
            if note.id in listed or sync_state.get(self._db, vault, note.id) is not None:
                continue  # merged as the note it is
            if _made(note.id, note.body):
                self._drops.append(note.id)
            elif note.deleted_at is None and not note.secret:
                mine.setdefault(made_notes.digest(note.body), []).append(note.id)
        theirs: dict[str, list[str]] = {}
        for note_id in sorted(self._to_read or ()):
            here = local.here(self._db, self._notes, note_id)
            if here.note is not None or here.emptied_at is not None:
                continue
            file, _ = self._opened(note_id)
            if file is not None and _shown(file) and file.body.text is not None:
                theirs.setdefault(made_notes.digest(file.body.text), []).append(note_id)
        for text, ids in mine.items():
            for own, there in zip(sorted(ids), theirs.get(text, []), strict=False):
                self._pairs[there] = own

    def preview(self) -> FirstLook:
        """What merging would make (joining): see first_look."""
        place = self._place
        dropped = set(self._drops)
        here = sum(1 for n in self._notes.all() if n.deleted_at is None and n.id not in dropped)
        cloud, alike = 0, len(self._pairs)
        recent: list[tuple[int, str, str | None]] = []
        for note_id in sorted(self._to_read or ()):
            file, _ = self._opened(note_id)
            if file is None or not _shown(file):
                continue
            cloud += 1
            at = local.here(self._db, self._notes, note_id)
            if at.note is not None and at.note.deleted_at is None and not at.note.secret:
                mine = local.mine(place.keys, place.device, at, None)
                if mine is not None and merge(place.keys, note_id, mine, file).copy is None:
                    alike += 1
            secret = file.contents.fields.get("secret_title")
            plain = secret is None or secret.value is None
            title = note_title(file.body.text or "") if plain else None
            recent = sorted([*recent, (file.body.edited, note_id, title)], reverse=True)[:3]
        return FirstLook(here, cloud, alike, [title for _, _, title in recent])

    def _weigh(self) -> None:
        """BigChangeError if what the round would bring is a big change, but for
        what the user lets it take in (called first, to back up)."""
        vault = self._place.vault
        known = sync_state.known(self._db, vault)
        changes = [
            each
            for note_id in sorted(self._to_read or ())
            for each in self._would(note_id, known.get(note_id))
        ]
        taken: set[Held] = set()
        if self._accept is not None:
            taken = set(sync_held.every(self._db, vault))
        if sum(1 for each in changes if each not in taken) >= BIG:
            with writing(self._db):
                sync_held.keep(self._db, vault, changes)
            raise BigChangeError(changes)
        if self._accept is not None and any(each in taken for each in changes):
            self._accept()

    def _would(self, note_id: str, state: NoteState | None) -> list[Held]:
        """What taking the note in would do here that counts toward a big change:
        the note, here out of the trash, going to it or deleted for good; conflict
        copies made. Nothing for a note not here."""
        place, path = self._place, item_path(NOTES, note_id)
        at = local.here(self._db, self._notes, note_id)
        if at.note is None or at.note.secret or self._leaving.passed(path) is not None:
            return []
        made: list[str] = []
        note = local.mine(place.keys, place.device, at, state)
        folded = False
        for copy_path in self._copies.get(note_id, []):
            copy = self._kept_copy(copy_path)
            if copy is not None and _writable(copy.note):
                note = self._joined(note_id, note, copy.note, made)
                folded = True
        cloud, none = self._opened(note_id)
        out: list[Held] = []
        if none and not folded:
            if state is None or state.seen is None or state.gone or state.deleted:
                return []
            away = self._last is not None and self._now() - self._last > YEAR
            if away and at.note.deleted_at is None:  # emptied elsewhere and cleared since
                out.append(Held(note_id, sync_held.TRASH))
            return out
        if cloud is not None:
            note = self._joined(note_id, note, cloud, made)
        if note is not None and at.note.deleted_at is None:
            trash = note.contents.fields.get(TRASH)
            if note.body.deleted:
                out.append(Held(note_id, sync_held.GONE))
            elif trash is not None and trash.value is not None:
                out.append(Held(note_id, sync_held.TRASH))
        for copy_id in made:
            there = local.here(self._db, self._notes, copy_id)
            new = there.note is None and there.emptied_at is None
            if new and sync_state.get(self._db, place.vault, copy_id) is None:
                out.append(Held(copy_id, sync_held.COPY, note_id))
        return out

    def _opened(self, note_id: str) -> tuple[NoteFile | None, bool]:
        """The note's file as the look read it, if it opens as the note's own and
        may be taken in; and whether it was read and not there."""
        place, path = self._place, item_path(NOTES, note_id)
        staged = sync_staged.get(self._db, place.vault, path) if path in self._staged else None
        if staged is None:
            return None, False
        if staged.data is None:
            return None, True
        try:
            head, value = open_envelope(place.keys, staged.data, place.vault, NOTES)
            note = NoteFile.from_json(place.keys, value)
        except FormatError:
            return None, False  # left as it is, or for later: nothing taken in
        if head.item != note_id or not _writable(note):
            return None, False
        return note, False

    def _joined(
        self, note_id: str, mine: NoteFile | None, cloud: NoteFile, made: list[str]
    ) -> NoteFile:
        """The two merged, as the round would merge them; a copy made, in made."""
        if mine is None:
            return cloud
        merged = merge(self._place.keys, note_id, mine, cloud)
        if merged.copy is not None:
            made.append(merged.copy[0])
        return merged.note

    def _stage(self, path: str) -> None:
        try:
            read = self._place.target.get(path)
            staged = Staged(read.tag, read.data)
        except NotThereError:
            staged = Staged(None, None)
        with writing(self._db):
            sync_staged.put(self._db, self._place.vault, path, staged)
        self._staged.add(path)

    def _get(self, path: str) -> Read:
        """The file as the look read it, the first time; else as it is now.
        NotThereError if there is none."""
        if path in self._staged:
            self._staged.discard(path)
            staged = sync_staged.get(self._db, self._place.vault, path)
            if staged is not None:
                if staged.data is None:
                    raise NotThereError(path)
                return Read(staged.data, staged.tag)
        return self._place.target.get(path)

    def run(self, also: list[str] | None = None) -> Report:
        """also: notes merged whether or not they changed."""
        if self._to_read is None:
            self.look()
        assert self._to_read is not None
        vault = self._place.vault
        self._empty_old_trash()
        self._let_go()
        changed = set(sync_state.changed(self._db, vault)) | set(
            sync_state.emptied(self._db, vault)
        )
        self._redirects = local.Redirects.here(self._db)
        to_read = self._to_read
        # This device's notes folded into the vault's go with those, not on their own.
        folded = set(self._pairs.values())
        queue = sorted((to_read | changed | set(also or [])) - folded)
        going = changed - folded
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
            if note_id in going:
                self._tell(PUTTING, len(done & going), len(going))
        self._remove_old_deletions()
        with writing(self._db):
            sync_state.set_synced_at(self._db, vault, self._started)
            sync_staged.clear(self._db, vault)
            sync_held.clear(self._db, vault)
            sync_state.set_joining(self._db, vault, False)
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
            NoteFile.from_json(place.keys, value)
        except FormatError:
            self._leaving.leave(entry.path, read, NOT_OURS)
            return None
        with writing(self._db):
            sync_staged.put(self._db, place.vault, entry.path, Staged(read.tag, read.data))
        self._copies.setdefault(head.item, []).append(entry.path)
        return head.item

    def _kept_copy(self, path: str) -> _Copy | None:
        """A copy under another name, as the look read it."""
        place = self._place
        staged = sync_staged.get(self._db, place.vault, path)
        if staged is None or staged.data is None:
            return None
        read = Read(staged.data, staged.tag)
        _, value = open_envelope(place.keys, read.data, place.vault, NOTES)
        return _Copy(path, read, NoteFile.from_json(place.keys, value))

    def _read(self, note_id: str, fresh: bool = False) -> _Read:
        """fresh: as it is now, not as the look read it."""
        place, path = self._place, item_path(NOTES, note_id)
        left = self._leaving.passed(path)
        if left is not None:  # listed as it was left: not read again
            if left.why == READ_ONLY:
                raise _PassedError()
            return _Read(None, left.tag, foreign=True)
        try:
            read = place.target.get(path) if fresh else self._get(path)
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
        paired = self._pairs.pop(note_id, None)
        own = paired is not None and self._fold_mine(note_id, paired)
        for path in self._copies.pop(note_id, []):
            copy = self._kept_copy(path)
            if copy is None:
                continue
            copies = self._fold(note_id, copy)
            if copies is not None:
                made += copies
                folded.append(copy)
        made += self._sync_note(note_id, read or bool(folded) or own, bool(folded) or own)
        for copy in folded:
            with suppress(NotThereError, RefusedError):  # gone, or changed since: next round
                self._place.target.delete(copy.path, AsRead(copy.read.tag))
        return made

    def _let_go(self) -> None:
        """Joining: this device's made notes, untouched, dropped (not put)."""
        for note_id in self._drops:
            with writing(self._db):
                note = self._notes.get(note_id)
                if note is None or not _made(note.id, note.body):
                    continue  # changed since it was looked at: a note as any other
                self._notes.drop(note_id)
            self._report.changed.append(note_id)

    def _fold_mine(self, note_id: str, own: str) -> bool:
        """This device's note with the vault's note's text taken in as that one,
        as if the vault had it, and handing over to it; True if it was. The
        vault's text at its version, and the fields this device changed, each
        if later: one it never changed goes as the vault has it (both never
        changed, the earliest time on each would leave it to the devices' ids)."""
        place = self._place
        at = local.here(self._db, self._notes, own)
        mine = local.mine(place.keys, place.device, at, None)
        cloud, _ = self._opened(note_id)
        if mine is None or cloud is None or at.note is None or at.note.deleted_at is not None:
            return False
        if local.here(self._db, self._notes, note_id).note is not None:
            return False
        changed = {
            name: each for name, each in mine.contents.fields.items() if each.hlc != local.EARLIEST
        }
        fields = merge_fields(replace(mine.contents, fields=changed), cloud.contents)
        try:
            self._merge(note_id, None, _Read(NoteFile(fields, cloud.body), None))
        except _PassedError:
            return False
        with writing(self._db):
            self._notes.hand_over(own, note_id)
        self._report.changed.append(own)
        return True

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
            with writing(self._db):
                sync_state.add_untold(self._db, self._place.vault, item_path(NOTES, note_id))
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
                    cloud = self._read(note_id, fresh=True)
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
