"""Two to four devices doing what users do to their notes, syncing now and then
(alone, or several at once with their requests falling between one another's),
with requests failing, answers lost, the app ended in the middle of a round,
clocks set wrong and a year passing: once every device has synced, they all
have the notes the model says, and nothing written is lost (sync_bed.Model).
They make categories and marks of the same few names apart, colours alike,
set reminders and deal with them, bring in notes: once synced, they have the
same records, one of each name (sync_bed.records_agree).

STICKLE_SYNC_EXAMPLES runs more of them (the default is what a commit can wait for).
"""

import os
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from fake_cloud import (
    DROPBOX,
    ONEDRIVE,
    WEBDAV_HALF,
    WEBDAV_IGNORING,
    WEBDAV_NO_TAGS,
    WEBDAV_WEAK,
    FakeCloud,
    Kind,
    Request,
    Trouble,
    Turns,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sync_bed import Clock, Device, Model, records_agree, settle
from test_vault_rules import plain_in

from stickle.core.reminders import Rule
from stickle.core.text_reminders import due
from stickle.data.colors import OwnColorsFullError
from stickle.data.labels import CategoryNameError, MarkNameError
from stickle.data.notes import NoteRepository
from stickle.sync.format import UnreadableError
from stickle.sync.merge import YEAR
from stickle.sync.targets.base import TargetError

EXAMPLES = int(os.environ.get("STICKLE_SYNC_EXAMPLES", "8"))
STEPS = 40
HOUR = 60 * 60 * 1000
# Clouds whose tags tell every write apart, or that have none (so every file is
# read): a WebDAV server whose tags are made of the second and the size can
# give two writes one tag, and a device that listed the first misses the
# second until the device files tell it what changed.
KINDS = (DROPBOX, ONEDRIVE, WEBDAV_NO_TAGS, WEBDAV_WEAK, WEBDAV_IGNORING, WEBDAV_HALF)
POOL = ("같은 글 표식-pool", "Same text marker-pool")  # written on two devices alike, now and then


# A round ended by these is tried again later: a cloud failing for now, vault.json half read.
LATER = (TargetError, UnreadableError)


class EndedError(Exception):
    """The app ended in the middle of a request."""


ENDED = "ended"
TROUBLES: tuple[Trouble | str | None, ...] = (
    None,
    None,
    None,
    None,
    None,
    None,
    Trouble.LOST,
    Trouble.UNAVAILABLE,
    Trouble.TOO_BUSY,
    Trouble.HALF,
    Trouble.LATE,
    ENDED,
)
COLORS = ("blue", "pink", "green", "yellow")
SWITCHES = ("always_on_top", "hidden", "collapsed", "locked")
# Few names, so that two devices make one of a name apart (case aside, too).
CATEGORIES = ("Work-표식", "work-표식", "Home-marker")
MARKS = ("Later-표식", "later-표식", "To do")
OWN_COLORS = ("#336699", "#993366")
SOURCES = (("sticky_notes", "s-1"), ("xpad", "x-1"))
NAMES: tuple[dict[str, str], ...] = ({"todo": "To do"}, {"todo": "할 일"}, {}, {})  # languages


class Bed:
    def __init__(self, folder: Path, kind: Kind, count: int) -> None:
        self.clock = Clock()
        self.cloud = FakeCloud(kind)
        self.devices = [
            Device(name, folder, self.cloud, self.clock, whole=True, names=NAMES[i])
            for i, name in enumerate("abcd"[:count])
        ]
        self.model = Model()
        self.records: dict[str, set[str]] = {
            "categories": set(),
            "marks": set(),
            "custom_colors": set(),
        }
        self.written = 0
        self.made = 0
        self.done: list[str] = []  # what was done, for a test that failed

    def did(self, device: Device, what: str) -> None:
        self.done.append(f"{self.cloud.requests:4d} {device.name} {what}")

    def text(self, data: st.DataObject) -> str:
        if data.draw(st.booleans()) and data.draw(st.booleans()):
            return POOL[self.written % 2]
        self.written += 1
        return f"메모 {self.written} 표식-{self.written:04d} note"

    def troubled(self, troubles: list[Trouble | str | None]) -> None:
        start = self.cloud.requests

        def chosen(request: Request) -> Trouble | None:
            at = request.number - start - 1
            trouble = troubles[at] if 0 <= at < len(troubles) else None
            if trouble == ENDED:
                raise EndedError(request.op)
            return trouble if isinstance(trouble, Trouble) else None

        self.cloud.trouble = chosen

    def sync(self, device: Device) -> None:
        try:
            device.sync()
        except EndedError:
            device.reopen()
        except LATER:
            pass  # tried again at a later round

    def together(self, devices: list[Device], order: list[int]) -> None:
        self.cloud.turns = Turns(order)
        try:
            failed = self.cloud.turns.run({device.name: device.round for device in devices})
        finally:
            self.cloud.turns = None
        for device in devices:
            error = failed.get(device.name)
            if isinstance(error, EndedError):
                device.reopen()
            elif error is not None and not isinstance(error, LATER):
                raise error


def _pick(data: st.DataObject, notes: list[str]) -> str | None:
    return data.draw(st.sampled_from(notes)) if notes else None


def _do(bed: Bed, data: st.DataObject) -> None:
    draw = data.draw
    device = draw(st.sampled_from(bed.devices))
    notes: NoteRepository = device.notes
    live = sorted(note.id for note in notes.live())
    trashed = sorted(note.id for note in notes.deleted())
    action = draw(
        st.sampled_from(
            ["new", "edit", "edit", "edit", "field", "field", "trash", "restore", "empty"]
            + ["sync"] * 5
            + ["together"] * 3
            + ["skew", "tick", "tick", "year"]
            + ["category", "rename", "uncategorise", "categorise", "categorise"]
            + ["mark", "marked", "colour", "uncolour", "remind", "remind", "import"]
        )
    )
    bed.cloud.trouble = lambda _request: None
    if action == "new" or (action == "edit" and not live):
        bed.made += 1  # ids of their own, the same in every run of an example
        note = notes.create(bed.text(data), note_id=f"{bed.made:08x}-1111-4222-8333-444455556666")
        bed.model.made(device, note.id)
        bed.did(device, f"new {note.id[:8]} {note.body!r}")
    elif action == "edit":
        note_id = _pick(data, live)
        assert note_id is not None
        before = notes.get(note_id)
        after = bed.text(data)
        if before is not None and before.body != after:
            notes.update_body(note_id, after, keep_earlier=True)
            bed.model.edited(device, note_id, before.body, after)
            bed.did(device, f"edit {note_id[:8]} {before.body!r} -> {after!r}")
    elif action == "field" and (note_id := _pick(data, live)) is not None:
        name = draw(st.sampled_from(("color", "opacity", *SWITCHES)))
        if name == "color":
            notes.set_color(note_id, draw(st.sampled_from(COLORS)))
        elif name == "opacity":
            notes.set_opacity(note_id, draw(st.sampled_from((0.3, 0.6, 1.0))))
        else:
            setter = getattr(notes, f"set_{name}")
            setter(note_id, draw(st.booleans()))
        bed.model.changed(device, note_id, name)
        bed.did(device, f"field {note_id[:8]} {name}")
    elif action == "trash" and (note_id := _pick(data, live)) is not None:
        notes.delete(note_id)
        bed.model.changed(device, note_id, "deleted_at")
        bed.did(device, f"trash {note_id[:8]}")
    elif action == "restore" and (note_id := _pick(data, trashed)) is not None:
        notes.restore(note_id)
        bed.model.taken_out(device, note_id)
        bed.model.changed(device, note_id, "category")  # back in its own, or one of its name
        bed.did(device, f"restore {note_id[:8]}")
    elif action == "empty" and (note_id := _pick(data, trashed)) is not None:
        note = notes.get(note_id)
        assert note is not None
        bed.model.emptying(device, note_id, note.body)
        notes.purge(note_id)
        bed.did(device, f"empty {note_id[:8]}")
    elif action == "sync":
        troubles = draw(st.lists(st.sampled_from(TROUBLES), max_size=8))
        bed.did(device, f"sync {[str(each) for each in troubles if each]}")
        bed.troubled(troubles)
        bed.sync(device)
    elif action == "together" and len(bed.devices) > 1:
        devices = draw(st.lists(st.sampled_from(bed.devices), min_size=2, unique=True))
        troubles = draw(st.lists(st.sampled_from(TROUBLES), max_size=8))
        bed.did(device, f"together {[d.name for d in devices]} {[str(t) for t in troubles if t]}")
        bed.troubled(troubles)
        bed.together(devices, draw(st.lists(st.integers(0, 3), max_size=40)))
    elif action == "skew":
        device.skew += draw(st.sampled_from((9 * HOUR, -9 * HOUR, 24 * HOUR, -device.skew)))
        bed.did(device, f"skew {device.skew}")
    elif action == "tick":
        bed.clock.tick(draw(st.sampled_from((1, 300, 1_000, 60_000, HOUR, 24 * HOUR))))
    elif action in RECORDS:
        RECORDS[action](bed, device, data, live)
    elif action == "year":
        # A year passes once all have synced (a device away a year is another matter).
        settle(bed.devices)
        for note in bed.devices[0].notes.deleted():
            bed.model.emptying(bed.devices[0], note.id, note.body)  # a year in the trash
        bed.clock.tick(YEAR + 1_000)
        bed.did(device, "a year on")


def _category(bed: Bed, device: Device, data: st.DataObject, live: list[str]) -> None:
    name = data.draw(st.sampled_from(CATEGORIES))
    try:
        made = device.labels.create_category(name, data.draw(st.sampled_from(COLORS)))
    except CategoryNameError:
        return
    bed.records["categories"].add(made.id)
    bed.did(device, f"category {made.id[:8]} {name!r}")


def _rename(bed: Bed, device: Device, data: st.DataObject, live: list[str]) -> None:
    categories = device.labels.categories()
    if not categories:
        return
    category = data.draw(st.sampled_from(categories))
    name = data.draw(st.sampled_from(CATEGORIES))
    try:
        device.labels.rename_category(category.id, name)
    except CategoryNameError:
        return
    bed.did(device, f"rename {category.id[:8]} {name!r}")


def _uncategorise(bed: Bed, device: Device, data: st.DataObject, live: list[str]) -> None:
    categories = device.labels.categories()
    if not categories:
        return
    category = data.draw(st.sampled_from(categories))
    for note_id in device.notes.remove_category(category.id, with_notes=False):
        bed.model.changed(device, note_id, "category")
    bed.did(device, f"remove category {category.id[:8]}")


def _categorise(bed: Bed, device: Device, data: st.DataObject, live: list[str]) -> None:
    note_id = _pick(data, live)
    if note_id is None:
        return
    chosen = data.draw(st.sampled_from([None, *(c.id for c in device.labels.categories())]))
    device.notes.set_category(note_id, chosen)
    bed.model.changed(device, note_id, "category")
    bed.did(device, f"categorise {note_id[:8]} {chosen and chosen[:8]}")


def _mark(bed: Bed, device: Device, data: st.DataObject, live: list[str]) -> None:
    name = data.draw(st.sampled_from(MARKS))
    try:
        made = device.labels.create_mark(name, "flag", lambda m: device.mark_name(m.id, m.name))
    except MarkNameError:
        return
    bed.records["marks"].add(made.id)
    bed.did(device, f"mark {made.id[:8]} {name!r}")


def _marked(bed: Bed, device: Device, data: st.DataObject, live: list[str]) -> None:
    note_id = _pick(data, live)
    marks = device.labels.marks()
    if note_id is None or not marks:
        return
    mark = data.draw(st.sampled_from(marks))
    on = data.draw(st.booleans())
    device.notes.set_mark(note_id, mark.id, on)
    bed.did(device, f"marked {note_id[:8]} {mark.id[:8]} {on}")


def _colour(bed: Bed, device: Device, data: st.DataObject, live: list[str]) -> None:
    try:
        made = device.colors.add(data.draw(st.sampled_from(OWN_COLORS)))
    except OwnColorsFullError:
        return
    bed.records["custom_colors"].add(made.id)
    bed.did(device, f"colour {made.hex}")


def _uncolour(bed: Bed, device: Device, data: st.DataObject, live: list[str]) -> None:
    colors = device.colors.colors()
    if colors:
        color = data.draw(st.sampled_from(colors))
        device.colors.remove(color.id)
        bed.did(device, f"uncolour {color.hex}")


def _remind(bed: Bed, device: Device, data: st.DataObject, live: list[str]) -> None:
    note_id = _pick(data, live)
    if note_id is None:
        return
    reminders = device.reminders
    own = reminders.of_note(note_id)
    what = data.draw(st.sampled_from(("set", "deal", "snooze", "off", "write")))
    now = datetime.fromtimestamp(device.millis() / 1000, UTC)
    if what == "set" or (own is None and what in ("deal", "snooze")):
        start = now.date().toordinal() + data.draw(st.integers(1, 3))
        repeat = data.draw(st.sampled_from(("none", "daily")))
        reminders.set(note_id, Rule(date.fromordinal(start), time(9), "Asia/Seoul", repeat))
    elif what == "deal" and own is not None:
        reminders.dealt_with(own.id)
    elif what == "snooze" and own is not None:
        reminders.snooze(own.id, now + timedelta(minutes=10))
    elif what == "off":
        reminders.turn_off(note_id)
    elif what == "write":
        note = device.notes.get(note_id)
        assert note is not None
        day = date.fromordinal(now.date().toordinal() + 2).isoformat()
        after = (
            f"{note.body}\n⏰{day} 15:00" if "⏰" not in note.body else note.body.split("\n⏰")[0]
        )
        before = note.body
        device.notes.update_body(note_id, after, keep_earlier=True)
        bed.model.edited(device, note_id, before, after)
        reminders.match_text(note_id, due(after, time(9)).keys())
    bed.did(device, f"remind {note_id[:8]} {what}")


def _import(bed: Bed, device: Device, data: st.DataObject, live: list[str]) -> None:
    note_id = _pick(data, live)
    if note_id is None:
        return
    note = device.notes.get(note_id)
    assert note is not None
    source, source_id = data.draw(st.sampled_from(SOURCES))
    device.imports.record(source, source_id, note.body, note_id)
    bed.did(device, f"import {note_id[:8]} {source}")


RECORDS = {
    "category": _category,
    "rename": _rename,
    "uncategorise": _uncategorise,
    "categorise": _categorise,
    "mark": _mark,
    "marked": _marked,
    "colour": _colour,
    "uncolour": _uncolour,
    "remind": _remind,
    "import": _import,
}


@pytest.mark.parametrize("kind", KINDS, ids=lambda kind: kind.name)
@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(data=st.data())
def test_devices_agree_with_the_model_and_lose_nothing(kind: Kind, data: st.DataObject) -> None:
    with tempfile.TemporaryDirectory() as folder:
        bed = Bed(Path(folder), kind, data.draw(st.integers(2, 4), label="devices"))
        try:
            for _ in range(data.draw(st.integers(4, STEPS), label="steps")):
                _do(bed, data)
            bed.cloud.trouble = lambda _request: None
            settle(bed.devices)
            bed.model.check(bed.devices, bed.cloud)
            records_agree(bed.devices, bed.records)
            texts = {f"text {i}": text for i, (_, text) in enumerate(bed.model.written)}
            said = (
                *CATEGORIES[:2],
                *MARKS[:2],
                *(f"{d.name.upper()}-PC 표식" for d in bed.devices),
            )
            texts |= {f"said {i}": text for i, text in enumerate(said)}
            assert plain_in(bed.cloud.files(), texts) == []
        except BaseException:
            print(*bed.done, sep=chr(10))
            print(bed.cloud.describe(last=80))
            raise
        finally:
            for device in bed.devices:
                device.close()
