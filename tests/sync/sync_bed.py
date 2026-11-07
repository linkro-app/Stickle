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
  it was emptied: nothing deleted comes back by itself.
"""

import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from fake_cloud import FakeCloud

from stickle.core.clock import stamp_of
from stickle.core.hlc import Hlc
from stickle.crypto.vault_keys import DataKeys
from stickle.data import stamps, sync_state
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.sync import local
from stickle.sync.engine import Place, Report, sync_notes
from stickle.sync.format import NOTES, Json, item_at, json_bytes, open_envelope
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

    def __init__(self, name: str, folder: Path, cloud: FakeCloud, clock: Clock) -> None:
        self.name = name
        self.id = device_id(name)
        self.path = folder / f"{name}.db"
        self.clock = clock
        self.skew = 0
        self.place = Place(cloud.target(name), VAULT, KEYS, self.id)
        self.db = open_store(self.path, KEY)
        self.notes = NoteRepository(self.db, clock=self.stamp, millis=self.millis)

    def millis(self) -> int:
        return self.clock.now + self.skew

    def stamp(self) -> str:
        return stamp_of(datetime.fromtimestamp(self.millis() / 1000, UTC))

    def sync(self) -> Report:
        return sync_notes(self.db, self.place, self.clock, self.notes)

    def round(self) -> None:
        """A round, on a thread of its own (fake_cloud.Turns)."""
        self.sync()

    def reopen(self) -> None:
        """As after the app was ended in the middle of something, and started again."""
        self.db.close()
        self.db = open_store(self.path, KEY)
        self.notes = NoteRepository(self.db, clock=self.stamp, millis=self.millis)

    def close(self) -> None:
        self.db.close()

    def state(self, note_id: str) -> sync_state.NoteState | None:
        return sync_state.get(self.db, VAULT, note_id)

    def stamp_of(self, note_id: str, name: str) -> tuple[Hlc, str]:
        when, by = stamps.of(self.db, note_id).get(name, (Hlc(0), None))
        return when, by or self.id

    def texts(self) -> dict[str, str]:
        """Every note's text here, in the trash too."""
        return {note.id: note.body for note in self.notes.all()}

    def shown(self) -> dict[str, tuple[object, ...]]:
        """Every note as it is here, in the trash too: what devices must agree on."""
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
        }


def value_of(device: Device, note_id: str, name: str) -> object:
    note = device.notes.get(note_id)
    assert note is not None
    return getattr(note, name)


def settle(devices: Iterable[Device], passes: int = 12) -> None:
    """Each device syncs in turn until a pass where none has anything to do."""
    devices = list(devices)
    for _ in range(passes):
        busy = False
        for device in devices:
            report = device.sync()
            busy = busy or bool(report.put or report.changed or report.left)
        if not busy:
            return
    raise AssertionError("the devices never settled")


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
