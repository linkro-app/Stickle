"""Two to four devices doing what users do to their notes, syncing now and then
(alone, or several at once with their requests falling between one another's),
with requests failing, answers lost, the app ended in the middle of a round,
clocks set wrong and a year passing: once every device has synced, they all
have the notes the model says, and nothing written is lost (sync_bed.Model).

STICKLE_SYNC_EXAMPLES runs more of them (the default is what a commit can wait for).
"""

import os
import tempfile
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
from sync_bed import Clock, Device, Model, settle
from test_vault_rules import plain_in

from stickle.data.notes import NoteRepository
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


class Bed:
    def __init__(self, folder: Path, kind: Kind, count: int) -> None:
        self.clock = Clock()
        self.cloud = FakeCloud(kind)
        self.devices = [Device(name, folder, self.cloud, self.clock) for name in "abcd"[:count]]
        self.model = Model()
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
        except TargetError:
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
            elif error is not None and not isinstance(error, TargetError):
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
    elif action == "year":
        # A year passes once all have synced (a device away a year is another matter).
        settle(bed.devices)
        for note in bed.devices[0].notes.deleted():
            bed.model.emptying(bed.devices[0], note.id, note.body)  # a year in the trash
        bed.clock.tick(YEAR + 1_000)
        bed.did(device, "a year on")


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
            texts = {f"text {i}": text for i, (_, text) in enumerate(bed.model.written)}
            assert plain_in(bed.cloud.files(), texts) == []
        except BaseException:
            print(*bed.done, sep=chr(10))
            print(bed.cloud.describe(last=80))
            raise
        finally:
            for device in bed.devices:
                device.close()
