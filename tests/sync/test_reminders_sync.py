"""Reminders, between devices: a note's own reminder as set, and how it and
those written in its text were dealt with, follow it to every device; one
dealt with for a setting since changed counts for nothing there."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest
from fake_cloud import DROPBOX, WEBDAV_IGNORING, FakeCloud, Kind
from test_records import KEYS, VAULT, Device, Times

from stickle.core.hlc import Hlc
from stickle.core.reminders import Rule
from stickle.core.text_reminders import due
from stickle.data import sync_state
from stickle.data.reminders import ReminderRepository
from stickle.sync.contents import Field
from stickle.sync.format import NOTES, Json, item_path, open_envelope, seal
from stickle.sync.note_file import NoteFile

SEOUL = "Asia/Seoul"
NINE = time(9)
DAY = 86_400_000


class Reminding(Device):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)  # pyright: ignore[reportArgumentType]
        self.reminders = ReminderRepository(
            self.db, clock=self.times.stamp, device=self.id, millis=self.times
        )

    def own(self, note_id: str) -> tuple[str, str, str | None, str | None] | None:
        """Its own reminder: the day it starts, its time, the next day it rings,
        and when it was put off to."""
        rows = self.db.execute(
            "SELECT start_day, at_time, next_day, snoozed_until FROM reminders"
            " WHERE note_id = ? AND kind = 'note' AND deleted_at IS NULL",
            (note_id,),
        ).fetchall()
        return None if not rows else tuple(rows[0])  # pyright: ignore[reportReturnType]

    def written(self, note_id: str) -> dict[tuple[str, str], str | None]:
        """Those written in its text: (day, time) -> the next day it rings."""
        rows = self.db.execute(
            "SELECT start_day, at_time, next_day FROM reminders"
            " WHERE note_id = ? AND kind = 'text' AND deleted_at IS NULL",
            (note_id,),
        )
        return {(str(day), str(at)): None if nxt is None else str(nxt) for day, at, nxt in rows}

    def save_text(self, note_id: str, text: str) -> None:
        """As the app does when a note's text is saved."""
        self.notes.update_body(note_id, text)
        self.reminders.match_text(note_id, due(text, NINE).keys())


@pytest.fixture
def times() -> Times:
    return Times()  # 2025-11-07


@pytest.fixture(params=[DROPBOX, WEBDAV_IGNORING], ids=lambda k: k.name)
def cloud(request: pytest.FixtureRequest) -> FakeCloud:
    kind: Kind = request.param
    return FakeCloud(kind)


@pytest.fixture
def devices(tmp_path: Path, cloud: FakeCloud, times: Times) -> Iterator[list[Reminding]]:
    made = [Reminding(name, tmp_path, cloud, times) for name in ("a", "b", "c")]
    yield made
    for device in made:
        device.db.close()


def settle(*devices: Device) -> None:
    for _ in range(4):
        for device in devices:
            device.sync()
    for device in devices:
        assert not sync_state.unconfirmed(device.db, VAULT)


def shared_note(devices: list[Reminding], text: str = "회의") -> str:
    note = devices[0].notes.create(text)
    settle(*devices)
    return note.id


def test_a_reminder_set_on_one_rings_on_the_others(devices: list[Reminding]) -> None:
    a, b, _ = devices
    note = shared_note(devices)
    a.reminders.set(note, Rule(date(2025, 11, 10), NINE, SEOUL, "daily"))

    settle(a, b)

    assert b.own(note) == ("2025-11-10", "09:00", "2025-11-10", None)


def test_dealt_with_or_snoozed_on_one_it_is_on_the_others(
    devices: list[Reminding], times: Times
) -> None:
    a, b, _ = devices
    note = shared_note(devices)
    a.reminders.set(note, Rule(date(2025, 11, 10), NINE, SEOUL, "daily"))
    settle(a, b)
    times.tick(3 * DAY)  # 11-10, after nine

    on_b = b.reminders.of_note(note)
    assert on_b is not None
    b.reminders.snooze(on_b.id, datetime(2025, 11, 10, 3, 30, tzinfo=UTC))
    settle(a, b)
    assert a.own(note) == ("2025-11-10", "09:00", "2025-11-10", "2025-11-10T03:30:00.000Z")

    times.tick()
    on_a = a.reminders.of_note(note)
    assert on_a is not None
    a.reminders.dealt_with(on_a.id)
    settle(a, b)

    for device in (a, b):
        assert device.own(note) == ("2025-11-10", "09:00", "2025-11-11", None)


def test_dealt_with_for_the_setting_before_it_changed_counts_for_nothing(
    devices: list[Reminding], times: Times
) -> None:
    a, b, _ = devices
    note = shared_note(devices)
    a.reminders.set(note, Rule(date(2025, 11, 8), NINE, SEOUL))
    settle(a, b)
    times.tick(DAY)  # 11-08, after nine: it rang on both

    a.reminders.set(note, Rule(date(2025, 11, 12), NINE, SEOUL))  # set again
    times.tick()
    on_b = b.reminders.of_note(note)
    assert on_b is not None
    b.reminders.dealt_with(on_b.id)  # b, not knowing: later, but for the one before
    settle(a, b)

    for device in (a, b):
        assert device.own(note) == ("2025-11-12", "09:00", "2025-11-12", None)


def test_dealt_with_for_the_setting_as_it_is_is_not_undone_by_one_for_the_one_before(
    devices: list[Reminding], times: Times
) -> None:
    a, b, c = devices
    note = shared_note(devices)
    a.reminders.set(note, Rule(date(2025, 11, 8), NINE, SEOUL, "daily"))
    settle(a, b, c)
    times.tick(DAY)
    b.reminders.set(note, Rule(date(2025, 11, 8), time(10), SEOUL, "daily"))
    settle(b, c)
    times.tick(DAY)
    on_c = c.reminders.of_note(note)
    assert on_c is not None
    c.reminders.dealt_with(on_c.id)  # for ten
    settle(b, c)
    times.tick()
    on_a = a.reminders.of_note(note)
    assert on_a is not None
    a.reminders.dealt_with(on_a.id)  # later, for nine: a has not seen it change

    settle(a, b, c)

    for device in devices:
        assert device.own(note) == ("2025-11-08", "10:00", "2025-11-10", None)


def test_set_on_two_the_later_stays_and_turned_off_it_is_off(
    devices: list[Reminding], times: Times
) -> None:
    a, b, _ = devices
    note = shared_note(devices)
    a.reminders.set(note, Rule(date(2025, 11, 10), NINE, SEOUL))
    times.tick()
    b.reminders.set(note, Rule(date(2025, 11, 11), NINE, SEOUL))
    settle(a, b)
    for device in (a, b):
        assert device.own(note) == ("2025-11-11", "09:00", "2025-11-11", None)

    times.tick()
    a.reminders.turn_off(note)
    settle(a, b)

    for device in (a, b):
        assert device.own(note) is None


def test_one_written_in_the_text_rings_on_the_others_and_is_dealt_with_there(
    devices: list[Reminding], times: Times
) -> None:
    a, b, _ = devices
    note = shared_note(devices)
    a.save_text(note, "회의\n⏰2025-11-10 15:00")
    settle(a, b)
    assert b.written(note) == {("2025-11-10", "15:00"): "2025-11-10"}

    times.tick(4 * DAY)
    on_b = next(r for r in b.reminders.pending() if r.note_id == note)
    b.reminders.dealt_with(on_b.id)
    settle(a, b)
    assert a.written(note) == {("2025-11-10", "15:00"): None}

    times.tick()
    a.save_text(note, "회의")  # taken out
    times.tick()
    a.save_text(note, "회의\n⏰2025-11-10 15:00")  # and written again: anew
    settle(a, b)

    for device in (a, b):
        assert device.written(note) == {("2025-11-10", "15:00"): "2025-11-10"}


def test_a_conflict_copy_rings_only_what_its_note_does_not(
    devices: list[Reminding], times: Times
) -> None:
    a, b, _ = devices
    note = shared_note(devices)
    a.reminders.set(note, Rule(date(2025, 11, 10), NINE, SEOUL))
    settle(a, b)
    a.save_text(note, "회의\n⏰2025-11-10 15:00")
    times.tick()
    b.save_text(note, "회의 장소\n⏰2025-11-10 15:00\n⏰2025-11-11 15:00")  # later: stays

    settle(a, b)

    copies = [n.id for n in a.notes.all() if n.id != note]
    assert len(copies) == 1  # a's text, the earlier
    for device in (a, b):
        assert device.own(copies[0]) is None  # the note's own stays with the note
        assert device.written(note) == {
            ("2025-11-10", "15:00"): "2025-11-10",
            ("2025-11-11", "15:00"): "2025-11-11",
        }
        assert device.written(copies[0]) == {("2025-11-10", "15:00"): None}  # the note rings it


def test_one_of_a_calendar_this_version_does_not_know_is_left_in_the_file(
    devices: list[Reminding], cloud: FakeCloud, times: Times
) -> None:
    a, b, _ = devices
    note = shared_note(devices)
    path = item_path(NOTES, note)
    read = a.place.target.get(path)
    value = open_envelope(KEYS, read.data, VAULT, NOTES)[1]
    file = NoteFile.from_json(KEYS, value)
    later: dict[str, Json] = {
        "calendar": "mars",
        "start_day": "0042-03-01",
        "at_time": "09:00",
        "time_zone": None,
        "repeat": "none",
        "weekdays": "",
        "hide_note": False,
        "first_day": None,
    }
    fields = {**file.contents.fields, "reminder": Field(later, Hlc(times.now), a.id)}
    written = NoteFile(type(file.contents)(fields, file.contents.others), file.body).to_json()
    cloud.put_by_hand(path, seal(KEYS, VAULT, NOTES, note, written))

    settle(b)

    assert b.own(note) is None
    kept = NoteFile.from_json(
        KEYS, open_envelope(KEYS, b.place.target.get(path).data, VAULT, NOTES)[1]
    )
    assert kept.contents.fields["reminder"].value == later
