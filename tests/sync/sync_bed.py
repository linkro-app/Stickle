"""Devices syncing a vault through a cloud in memory, for sync's tests: each
with its own notes database and clock, and what the tests check them against
(Model) — not one another only, as devices running the same code would agree
on the same mistake.

What the model knows of the notes is what the users did: the texts they wrote
and which text each edit was made on, the fields they changed and the stamps
those changes were given, and what they emptied. What it expects of them once
every device has synced:

- every text written is the text of a note on every device (the note's own, a
  conflict copy's, in the trash or not), unless an edit was made on it or the
  note was emptied where it was the note's text: no note, and no text, is lost;
- each field of a note never emptied has the value of its latest change, by
  its stamp (the later time, then the device, then the value);
- a note emptied is not back out of the trash, unless a user took it out after
  it was emptied: nothing deleted comes back by itself;
- a note's category is its latest change's, or the one that became one with
  it (devices syncing the whole vault, records too: whole).

Of the records (whole devices), they expect every device to have them alike,
one of each name, nothing made gone (records_agree).
"""

import secrets
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from fake_cloud import FakeCloud

from stickle.core.clock import stamp_of
from stickle.core.hlc import Hlc
from stickle.crypto.vault_keys import DataKeys
from stickle.data import record_state, stamps, sync_state
from stickle.data.colors import ColorRepository
from stickle.data.imports import ImportRecords
from stickle.data.labels import LabelRepository, become
from stickle.data.notes import NoteRepository
from stickle.data.reminders import ReminderRepository
from stickle.data.schema import open_store
from stickle.data.settings import DEVICE_ID, Settings
from stickle.sync import local
from stickle.sync.devices import About
from stickle.sync.engine import BigChangeError, Place, Report, sync_notes, sync_vault
from stickle.sync.format import (
    DEVICES,
    NOTES,
    VAULT_FILE,
    Json,
    VaultInfo,
    item_at,
    json_bytes,
    open_envelope,
    vault_file,
)
from stickle.sync.merge import COPY_OF
from stickle.sync.note_file import NoteFile

KEY = secrets.token_bytes(32)
KEYS = DataKeys({1: secrets.token_bytes(32)})
VAULT = "vault-1"
START = 1_762_483_200_000  # 2025-11-07
FIELDS = ("color", "opacity", "always_on_top", "hidden", "collapsed", "locked", "deleted_at")


class Clock:
    """The cloud's time, moved by the test."""

    def __init__(self, now: int = START) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def tick(self, millis: int = 1_000) -> None:
        self.now += millis


def device_id(name: str) -> str:
    return f"{name}-0000-4000-8000-000000000000"


class Device:
    """One device: its database, its clock (the cloud's, and skew off it: the
    time its notes are stamped with), and its way into the cloud."""

    def __init__(
        self,
        name: str,
        folder: Path,
        cloud: FakeCloud,
        clock: Clock,
        whole: bool = False,
        names: dict[str, str] | None = None,
        aside: Path | None = None,
    ) -> None:
        """whole: syncs the whole vault, records and its own file too (else its
        notes only); names: what its built-in marks show as, in its language;
        aside: where it keeps files it sets aside (None: it sets none aside)."""
        self.name = name
        self.id = device_id(name)
        self.path = folder / f"{name}.db"
        self.clock = clock
        self.skew = 0
        self.whole = whole
        self.names = names or {}
        self.cloud = cloud
        self.place = Place(cloud.target(name), VAULT, KEYS, self.id, aside)
        # Its user lets a round stopped for a big change take it in (backing up
        # first: counted); else the round's BigChangeError is let through.
        self.accepts = True
        self.holds = 0
        self.backups = 0
        self.db = open_store(self.path, KEY)
        self._repositories()
        if whole and VAULT_FILE not in cloud.files():
            cloud.put_by_hand(VAULT_FILE, vault_file(VaultInfo(VAULT)), long_ago=True)

    def _repositories(self) -> None:
        stamp, millis = self.stamp, self.millis
        self.notes = NoteRepository(self.db, clock=stamp, millis=millis)
        self.labels = LabelRepository(self.db, clock=stamp, millis=millis)
        self.colors = ColorRepository(self.db, clock=stamp, millis=millis)
        self.reminders = ReminderRepository(self.db, clock=stamp, device=self.id, millis=millis)
        self.imports = ImportRecords(self.db, clock=stamp, millis=millis)

    def mark_name(self, mark_id: str, name: str | None) -> str:
        return name or self.names.get(mark_id, mark_id)

    def millis(self) -> int:
        return self.clock.now + self.skew

    def stamp(self) -> str:
        return stamp_of(datetime.fromtimestamp(self.millis() / 1000, UTC))

    def sync(self, accept: Callable[[], object] | None = None) -> Report:
        """A round; stopped for a big change, nothing changed here nor (when
        no other device is in the middle of a round) in the vault but this
        device's own file, and let go on if its user accepts."""
        before = self._before()
        try:
            return self._sync(accept)
        except BigChangeError:
            self.holds += 1
            assert self._before() == before, "a round stopped for a big change changed something"
            if accept is not None or not self.accepts:
                raise
            return self._sync(self.back_up)

    def back_up(self) -> None:
        self.backups += 1

    def _before(self) -> tuple[object, ...]:
        files = None
        if self.cloud.turns is None:
            files = {p: d for p, d in self.cloud.files().items() if not p.startswith(f"{DEVICES}/")}
        notes = {
            note.id: (note.body, note.deleted_at, note.color, note.hidden)
            for note in self.notes.all()
        }
        names = sorted(str(row[0]) for row in self.db.execute("SELECT name FROM categories"))
        return files, notes, self.notes.deletion_records(), names

    def _sync(self, accept: Callable[[], object] | None) -> Report:
        renewed = Settings(self.db).get(DEVICE_ID)
        if renewed is not None and renewed != self.id:  # a round that ended after it took one
            self._renewed(renewed)
        if self.whole:
            about = About(f"{self.name.upper()}-PC 표식", "Linux", "0.7.0")
            report = sync_vault(
                self.db, self.place, self.clock, self.notes, self.mark_name, about, accept=accept
            )
            if report.device != self.id:  # another device had its id: a copy of its disk
                self._renewed(report.device)
            return report
        return sync_notes(self.db, self.place, self.clock, self.notes, accept)

    def sync_as(self, device: str) -> Report:
        """A round of the whole vault with this place, as a caller holding the
        id device would run it (not following a new one)."""
        place = Place(self.place.target, VAULT, KEYS, device, self.place.aside)
        about = About(f"{self.name.upper()}-PC 표식", "Linux", "0.7.0")
        return sync_vault(self.db, place, self.clock, self.notes, self.mark_name, about)

    def _renewed(self, device: str) -> None:
        self.id = device
        self.place = Place(self.place.target, VAULT, KEYS, self.id, self.place.aside)
        self._repositories()

    def round(self) -> None:
        """A round, on a thread of its own (fake_cloud.Turns)."""
        self.sync()

    def reopen(self) -> None:
        """As after the app was ended in the middle of something, and started again."""
        self.db.close()
        self.db = open_store(self.path, KEY)
        self._repositories()

    def close(self) -> None:
        self.db.close()

    def copied(self, name: str) -> Device:
        """Another device made from a copy of this one's disk: its database, its id."""
        self.db.close()
        shutil.copyfile(self.path, self.path.with_name(f"{name}.db"))
        self.db = open_store(self.path, KEY)
        self._repositories()
        aside = None if self.place.aside is None else self.place.aside.with_name(f"{name}-aside")
        copy = Device(name, self.path.parent, self.cloud, self.clock, True, self.names, aside)
        copy.id = self.id
        copy.place = Place(self.cloud.target(name), VAULT, KEYS, self.id, aside)
        copy.reopen()
        return copy

    def state(self, note_id: str) -> sync_state.NoteState | None:
        return sync_state.get(self.db, VAULT, note_id)

    def stamp_of(self, note_id: str, name: str) -> tuple[Hlc, str]:
        when, by = stamps.of(self.db, note_id).get(name, (Hlc(0), None))
        return when, by or self.id

    def texts(self) -> dict[str, str]:
        """Every note's text here, in the trash too."""
        return {note.id: note.body for note in self.notes.all()}

    def shown(self) -> dict[str, tuple[object, ...]]:
        """Every note as it is here, in the trash too: what devices must agree on
        (but a note kept here only, its file gone from the vault while the device
        was away for more than a year)."""
        gone = {each for each, state in sync_state.known(self.db, VAULT).items() if state.gone}
        return {
            note.id: (
                note.body,
                note.color,
                note.opacity,
                note.always_on_top,
                note.hidden,
                note.collapsed,
                note.locked,
                note.deleted_at,
                tuple(sorted(note.marks)),
            )
            for note in self.notes.all()
            if note.id not in gone
        }


def value_of(device: Device, note_id: str, name: str) -> object:
    note = device.notes.get(note_id)
    assert note is not None
    return getattr(note, "label" if name == "category" else name)


def settle(devices: Iterable[Device], passes: int = 12, tick: int = 0) -> None:
    """Each device syncs in turn until a pass where none has anything to do, nor
    wrote its own file (the others to read it: a copied disk is seen so);
    tick: milliseconds passing between passes (for a file set aside only once
    it failed for minutes)."""
    devices = list(devices)
    for _ in range(passes):
        if tick:
            devices[0].clock.tick(tick)
        told = _devices_files(devices[0].cloud)
        busy = False
        for device in devices:
            report = device.sync()
            records = report.records
            busy = busy or bool(report.put or report.changed or report.left)
            busy = busy or bool(records.put or records.changed or records.left)
        if not busy and _devices_files(devices[0].cloud) == told:
            return
    raise AssertionError("the devices never settled")


def _devices_files(cloud: FakeCloud) -> dict[str, bytes]:
    return {path: data for path, data in cloud.files().items() if path.startswith(f"{DEVICES}/")}


def cloud_notes(cloud: FakeCloud) -> dict[str, NoteFile]:
    found: dict[str, NoteFile] = {}
    for path, data in cloud.files().items():
        at = item_at(path)
        if at is None or at[0] != NOTES:
            continue
        _, contents = open_envelope(KEYS, data, VAULT, NOTES)
        found[at[1]] = NoteFile.from_json(KEYS, contents)
    return found


def origins(device: Device, note_id: str) -> list[str]:
    """The note, and the note it is a conflict copy of, and so on, as the device
    has them: a text a copy holds was written on the note it copies."""
    found = [note_id]
    while (state := device.state(found[-1])) is not None:
        kept = local.kept(state.outbox if state.outbox is not None else state.seen)
        mark = None if kept is None else kept.contents.fields.get(COPY_OF)
        if mark is None or not isinstance(mark.value, dict):
            break
        original = mark.value.get("note")
        if not isinstance(original, str) or original in found:
            break
        found.append(original)
    return found


type Change = tuple[tuple[Hlc, str, bytes], object]


@dataclass
class Model:
    written: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    made_on: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    emptied_texts: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    created: set[str] = field(default_factory=set[str])
    emptied: set[str] = field(default_factory=set[str])
    out_after_emptied: set[str] = field(default_factory=set[str])
    changes: dict[tuple[str, str], list[Change]] = field(
        default_factory=dict[tuple[str, str], list[Change]]
    )

    def made(self, device: Device, note_id: str) -> None:
        self.created.add(note_id)
        note = device.notes.get(note_id)
        assert note is not None
        self.written.append((note_id, note.body))
        for name in FIELDS:  # as made, by the device that made it, at the earliest time
            self.changed(device, note_id, name)

    def edited(self, device: Device, note_id: str, before: str, after: str) -> None:
        self.made_on.update((each, before) for each in origins(device, note_id))
        self.written.append((note_id, after))

    def changed(self, device: Device, note_id: str, name: str) -> None:
        when, by = device.stamp_of(note_id, name)
        value = value_of(device, note_id, name)
        order = (when, by, json_bytes(cast(Json, value)))
        self.changes.setdefault((note_id, name), []).append((order, value))

    def taken_out(self, device: Device, note_id: str) -> None:
        self.changed(device, note_id, "deleted_at")
        if note_id in self.emptied:
            self.out_after_emptied.add(note_id)

    def emptying(self, device: Device, note_id: str, text: str) -> None:
        """Called before the device empties it."""
        self.emptied.add(note_id)
        self.emptied_texts.update((each, text) for each in origins(device, note_id))

    def check(self, devices: list[Device], cloud: FakeCloud) -> None:
        first = devices[0]
        shown = first.shown()
        for device in devices[1:]:
            assert device.shown() == shown, f"{device.name} differs from {first.name}"
        for device in devices:
            assert sync_state.unconfirmed(device.db, VAULT) == [], f"{device.name} still sending"
        in_cloud = {
            note_id: note.body.text
            for note_id, note in cloud_notes(cloud).items()
            if not note.body.deleted
        }
        assert in_cloud == {note_id: each[0] for note_id, each in shown.items()}
        texts = {each[0] for each in shown.values()}
        for note_id, text in self.written:
            if (note_id, text) in self.made_on or (note_id, text) in self.emptied_texts:
                continue
            assert text in texts, f"lost: {text!r} of {note_id}"
        for note_id in self.emptied - self.out_after_emptied:
            assert note_id not in shown or shown[note_id][7] is not None, f"{note_id} came back"
        for note_id in self.created - self.emptied:
            assert note_id in shown, f"{note_id} is gone"
            for name in FIELDS:
                _, latest = max(self.changes[(note_id, name)], key=lambda change: change[0])
                there = value_of(first, note_id, name)
                assert there == latest, f"{note_id} {name}: {there!r}, not {latest!r}"
            if (note_id, "category") in self.changes:
                _, latest = max(self.changes[(note_id, "category")], key=lambda c: c[0])
                if isinstance(latest, str):
                    latest = become(first.db, "categories", latest)
                there = value_of(first, note_id, "category")
                assert there == latest, f"{note_id} category: {there!r}, not {latest!r}"


# A note kept here only (its file gone from the vault while the device was away
# for more than a year) keeps its reminders here only too.
_REMINDERS = (
    "reminders WHERE deleted_at IS NULL"
    " AND note_id NOT IN (SELECT note_id FROM sync_notes WHERE gone)"
)
_TABLES = {
    "categories": "id, name, color, icon, position, created_at, deleted_at, merged_into",
    "marks": "id, name, icon, position, created_at, deleted_at, merged_into",
    "custom_colors": "id, hex, position, created_at, deleted_at",
    "import_records": "source, source_id, content_hash, note_id, imported_at",
    _REMINDERS: "note_id, kind, calendar, start_day, at_time,"
    " time_zone, repeat, weekdays, hide_note, first_day, next_day, snoozed_until, acked_at,"
    " acked_device",
}


def _rows(device: Device, table: str, columns: str) -> list[tuple[object, ...]]:
    return sorted(
        (tuple(row) for row in device.db.execute(f"SELECT {columns} FROM {table}")), key=repr
    )


def records_agree(devices: list[Device], made: dict[str, set[str]]) -> None:
    """Every device has the records alike: categories, marks, colours, notes
    brought in, reminders; of those not deleted nor become one with another, one
    of each name (a mark's, as it is stored; a colour's value); and every one
    made (made: table -> ids) is there."""
    first = devices[0]
    for table, columns in _TABLES.items():
        rows = _rows(first, table, columns)
        for device in devices[1:]:
            assert _rows(device, table, columns) == rows, f"{device.name}'s {table} differ"
    for device in devices:
        assert record_state.unconfirmed(device.db, VAULT) == [], f"{device.name} still sending"
    live = "deleted_at IS NULL AND merged_into IS NULL"
    for table, column in (("categories", "name"), ("marks", "coalesce(name, id)")):
        names = [
            str(n).casefold()
            for (n,) in first.db.execute(f"SELECT {column} FROM {table} WHERE {live}")
        ]
        assert len(names) == len(set(names)), f"{table} of one name: {sorted(names)}"
    hexes = [
        h for (h,) in first.db.execute("SELECT hex FROM custom_colors WHERE deleted_at IS NULL")
    ]
    assert len(hexes) == len(set(hexes)), f"colours alike: {sorted(hexes)}"  # pyright: ignore[reportUnknownArgumentType]
    for table, ids in made.items():
        there = {str(i) for (i,) in first.db.execute(f"SELECT id FROM {table}")}
        assert ids <= there, f"{table} gone: {sorted(ids - there)}"
