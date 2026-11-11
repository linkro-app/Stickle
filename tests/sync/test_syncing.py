"""Sync running alongside the app: rounds on their own thread, and what they
bring on screen. The other computer is a device of the test bed, through a
cloud in memory."""

import gc
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import apsw
import pytest
from fake_cloud import WEBDAV as WEBDAV_KIND
from fake_cloud import FakeCloud, Request, Trouble
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from pytestqt.qtbot import QtBot
from sync_bed import KEY, KEYS, VAULT, Clock, Device

from stickle.app import syncing as syncing_module
from stickle.app.about_dialog import AboutDialog
from stickle.app.application import login_here, replace_sync
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.reminders import ReminderKeeper
from stickle.app.syncing import BIG_TEXT, Ended, Syncing, cloud_time, look_first
from stickle.core.clock import Clocks
from stickle.core.hlc import wall_millis
from stickle.core.labels import Mark
from stickle.core.reminders import Rule
from stickle.data import sync_connection
from stickle.data.backups import backups
from stickle.data.colors import ColorRepository
from stickle.data.labels import LabelRepository
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.reminders import ReminderRepository
from stickle.data.schema import open_store
from stickle.data.settings import Settings
from stickle.data.sync_connection import WEBDAV, Login
from stickle.data.versions import VersionRepository
from stickle.sync import schedule as schedule_module
from stickle.sync.engine import GETTING, PUTTING, FirstLook
from stickle.sync.format import NOTES, item_path
from stickle.sync.outcome import Stopped
from stickle.sync.targets.base import SignInNeededError

ROUND_MS = 15_000
HOUR = 3600.0


@pytest.fixture(autouse=True)
def windows_deleted() -> Iterator[None]:
    """The windows a test added are deleted when it ends, as an event loop would."""
    yield
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.enable()
    gc.collect()  # here, and not on some thread of the next tests' servers


def said(told: Any) -> object:
    """What the signal waited for said first."""
    return told.args[0]


@dataclass
class Pc:
    """This computer: the app's windows and sync, and another one to sync with."""

    db: apsw.Connection
    path: Path
    notes: NoteRepository
    manager: NoteManager
    syncing: Syncing
    other: Device
    cloud: FakeCloud
    clock: Clock
    reminders: ReminderKeeper
    qtbot: QtBot

    def round(self) -> Ended:
        with self.qtbot.waitSignal(self.syncing.round_shown, timeout=ROUND_MS) as told:
            self.syncing.sync_now()
        ended = said(told)
        assert isinstance(ended, Ended)
        return ended

    def other_syncs(self) -> None:
        self.clock.now = wall_millis()
        self.other.sync()

    def window_of(self, note_id: str) -> NoteWindow | None:
        return self.manager.window_for(note_id)


def make_pc(
    qtbot: QtBot,
    tmp_path: Path,
    cloud: FakeCloud,
    stopped: str | None = None,
    name: str = "a",
) -> Pc:
    path = tmp_path / f"{name}.db"
    db = open_store(path, KEY)
    login = Login(VAULT, WEBDAV, "https://nas.example/dav", "u", "p", KEYS.keys, None, stopped)
    sync_connection.keep(db, login)
    clocks = Clocks()
    cloud_clock = cloud_time(db, clocks)
    notes = NoteRepository(db, clocks.stamp, clocks.millis)
    settings = Settings(db)
    manager = NoteManager(
        notes,
        idle_ms=10,
        max_ms=50,
        settings=settings,
        layouts=LayoutRepository(db),
        labels=LabelRepository(db, clocks.stamp, clocks.millis),
        colors=ColorRepository(db, clocks.stamp, clocks.millis),
        versions=VersionRepository(db),
    )
    reminders = ReminderKeeper(
        ReminderRepository(db, device=settings.device_id(), millis=clocks.millis),
        manager,
        settings,
        zone="UTC",
    )
    syncing = Syncing(
        db,
        path,
        KEY,
        sync_connection.current(db) or login,
        cloud_clock,
        manager,
        reminders,
        make_target=lambda _heard, _gone: cloud.target(name),
        # Its rounds are the ones a test asks for: making the other computer's
        # database below can take longer, on a slow machine, than the app waits
        # before a first round of its own.
        start_wait=HOUR,
    )
    clock = Clock(wall_millis())
    other = Device("b", tmp_path, cloud, clock, whole=True)
    return Pc(db, path, notes, manager, syncing, other, cloud, clock, reminders, qtbot)


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud(WEBDAV_KIND)


@pytest.fixture
def pc(qtbot: QtBot, tmp_path: Path, cloud: FakeCloud) -> Iterator[Pc]:
    made = make_pc(qtbot, tmp_path, cloud)
    made.manager.open_stored()
    made.syncing.start()
    made.round()
    yield made
    made.syncing.finish(0.0)
    for window in made.manager.windows:
        window.release()
    made.other.close()
    made.db.close()


def note_of_both(pc: Pc, text: str = "first") -> str:
    """A note on the two computers, shown here."""
    note = pc.other.notes.create(text)
    pc.other_syncs()
    pc.round()
    assert pc.window_of(note.id) is not None
    return note.id


def test_a_round_tells_how_far_it_has_got_on_the_apps_thread(pc: Pc) -> None:
    pc.notes.create("one")
    told: list[tuple[str, int, int, bool]] = []
    here = threading.current_thread()

    def moving(doing: str, done: int, of: int) -> None:
        told.append((doing, done, of, threading.current_thread() is here))

    pc.syncing.moving.connect(moving)
    pc.round()
    assert told == [(PUTTING, 1, 1, True)]


def test_a_note_changed_elsewhere_changes_in_its_window(pc: Pc) -> None:
    note_id = note_of_both(pc)
    pc.other.notes.update_body(note_id, "changed there")
    pc.other_syncs()
    pc.round()
    window = pc.window_of(note_id)
    assert window is not None and window.text == "changed there"


def test_a_note_changed_elsewhere_is_not_saved_again_from_here(pc: Pc) -> None:
    note_id = note_of_both(pc)
    pc.other.notes.update_body(note_id, "changed there")
    pc.other_syncs()
    pc.round()
    seq = pc.notes.get(note_id).change_seq  # pyright: ignore[reportOptionalMemberAccess]
    window = pc.window_of(note_id)
    assert window is not None and not pc.manager.waiting_to_save()
    pc.manager.save(window)
    assert pc.notes.get(note_id).change_seq == seq  # pyright: ignore[reportOptionalMemberAccess]


def test_writing_back_the_text_from_before_a_change_came_is_saved(pc: Pc) -> None:
    """What the window knows as stored is what came, not what it had before."""
    note_id = note_of_both(pc)
    pc.other.notes.update_body(note_id, "changed there")
    pc.other_syncs()
    pc.round()
    window = pc.window_of(note_id)
    assert window is not None
    window.editor.setPlainText("first")
    pc.manager.save(window)
    assert pc.notes.get(note_id).body == "first"  # pyright: ignore[reportOptionalMemberAccess]


def test_what_else_changed_elsewhere_shows_too(pc: Pc) -> None:
    note_id = note_of_both(pc)
    pc.other.notes.set_color(note_id, "green")
    pc.other.notes.set_opacity(note_id, 0.8)
    pc.other.notes.set_locked(note_id, True)
    pc.other.notes.set_always_on_top(note_id, False)
    pc.other_syncs()
    pc.round()
    window = pc.window_of(note_id)
    assert window is not None
    assert window.color == "green"
    assert window.opacity == pytest.approx(0.8)
    assert window.locked
    assert not window.always_on_top


def test_a_note_made_elsewhere_opens_without_taking_the_keyboard(
    pc: Pc, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown_quietly: list[bool] = []
    real_show = NoteWindow.show

    def show(window: NoteWindow) -> None:
        shown_quietly.append(window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        real_show(window)

    monkeypatch.setattr(NoteWindow, "show", show)
    note = pc.other.notes.create("from there")
    pc.other_syncs()
    pc.round()
    window = pc.window_of(note.id)
    assert window is not None and window.isVisible()
    assert shown_quietly == [True]
    # Later on, shown as any other: the keyboard may go to it.
    assert not window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


def test_a_note_made_elsewhere_hidden_stays_out_of_sight(pc: Pc) -> None:
    note = pc.other.notes.create("from there")
    pc.other.notes.set_hidden(note.id, True)
    pc.other_syncs()
    pc.round()
    assert pc.window_of(note.id) is None


def test_a_note_made_elsewhere_while_notes_are_set_aside_stays_aside(pc: Pc) -> None:
    note_of_both(pc)
    pc.manager.set_all_aside()
    note = pc.other.notes.create("from there")
    pc.other_syncs()
    pc.round()
    window = pc.window_of(note.id)
    assert window is not None and not window.isVisible()
    pc.manager.bring_back()
    assert window.isVisible()


def test_a_note_put_in_the_trash_elsewhere_closes(pc: Pc) -> None:
    note_id = note_of_both(pc)
    pc.other.notes.delete(note_id)
    pc.other_syncs()
    pc.round()
    assert pc.window_of(note_id) is None
    assert pc.notes.get(note_id).deleted  # pyright: ignore[reportOptionalMemberAccess]


def test_a_note_hidden_elsewhere_closes(pc: Pc) -> None:
    note_id = note_of_both(pc)
    pc.other.notes.set_hidden(note_id, True)
    pc.other_syncs()
    pc.round()
    assert pc.window_of(note_id) is None


def test_a_note_taken_out_of_the_trash_elsewhere_opens(pc: Pc) -> None:
    note_id = note_of_both(pc)
    pc.other.notes.delete(note_id)
    pc.other_syncs()
    pc.round()
    pc.other.notes.restore(note_id)
    pc.other_syncs()
    pc.round()
    assert pc.window_of(note_id) is not None


def test_a_window_being_edited_is_left_as_it_is(pc: Pc) -> None:
    note_id = note_of_both(pc)
    window = pc.window_of(note_id)
    assert window is not None
    window.edit()
    pc.other.notes.update_body(note_id, "changed there")
    pc.other_syncs()
    pc.round()
    assert window.editing
    assert window.text == "first"


def test_leaving_a_window_untouched_keeps_what_came(pc: Pc) -> None:
    """Being open for editing while a change came is no edit to save over it:
    what came waits for the edit to end, then shows."""
    note_id = note_of_both(pc)
    window = pc.window_of(note_id)
    assert window is not None
    window.edit()
    pc.other.notes.update_body(note_id, "changed there")
    pc.other_syncs()
    assert pc.round().report.held == [note_id]
    pc.manager.save(window)
    assert pc.notes.get(note_id).body == "first"  # pyright: ignore[reportOptionalMemberAccess]
    assert window.held

    with pc.qtbot.waitSignal(pc.syncing.round_shown, timeout=ROUND_MS):
        window.show_formatted()  # the edit is over: a round at once
    assert window.text == "changed there" and not window.held
    assert pc.notes.get(note_id).body == "changed there"  # pyright: ignore[reportOptionalMemberAccess]


def test_typed_while_a_change_waited_both_texts_are_kept(pc: Pc) -> None:
    note_id = note_of_both(pc)
    window = pc.window_of(note_id)
    assert window is not None
    pc.other.notes.update_body(note_id, "changed there")
    pc.other_syncs()
    window.edit()
    window.editor.setPlainText("typed here")
    pc.manager.save(window)
    assert pc.round().report.held == [note_id]
    assert window.text == "typed here"

    with pc.qtbot.waitSignal(pc.syncing.round_shown, timeout=ROUND_MS):
        window.show_formatted()

    texts = {note.body for note in pc.notes.all()}
    assert texts == {"typed here", "changed there"}  # the later in the note, a copy beside it
    assert pc.notes.get(note_id).body == "typed here"  # pyright: ignore[reportOptionalMemberAccess]
    copies = [each for each in pc.manager.windows if each.copy is not None]
    assert len(copies) == 1 and copies[0].text == "changed there"


def test_what_is_typed_goes_up_while_nothing_came(pc: Pc) -> None:
    note_id = note_of_both(pc)
    window = pc.window_of(note_id)
    assert window is not None
    window.edit()
    window.editor.setPlainText("typed here")
    pc.manager.save(window)
    ended = pc.round()
    assert ended.report.held == [] and ended.report.put == [note_id]
    assert window.editing


def test_a_window_with_something_to_save_is_left_as_it_is(pc: Pc) -> None:
    note_id = note_of_both(pc)
    window = pc.window_of(note_id)
    assert window is not None
    autosave = pc.manager._autosaves[window]  # pyright: ignore[reportPrivateUsage]
    autosave._idle.setInterval(60_000)  # pyright: ignore[reportPrivateUsage]
    autosave._max.setInterval(60_000)  # pyright: ignore[reportPrivateUsage]
    autosave.changed()
    pc.other.notes.update_body(note_id, "changed there")
    pc.other_syncs()
    pc.round()
    assert window.text == "first"


def test_a_note_being_edited_put_in_the_trash_elsewhere_stays_open(pc: Pc) -> None:
    note_id = note_of_both(pc)
    window = pc.window_of(note_id)
    assert window is not None
    window.edit()
    pc.other.notes.delete(note_id)
    pc.other_syncs()
    pc.round()
    assert pc.window_of(note_id) is window


def test_a_category_made_elsewhere_shows(pc: Pc) -> None:
    note_id = note_of_both(pc)
    category = pc.other.labels.create_category("From there", "green")
    pc.other.notes.set_category(note_id, category.id)
    pc.other_syncs()
    pc.round()
    window = pc.window_of(note_id)
    assert window is not None and window.category is not None
    assert window.category.name == "From there"


def test_a_category_both_know_given_elsewhere_shows(pc: Pc) -> None:
    """Nothing about the categories changed: only the note."""
    note_id = note_of_both(pc)
    category = pc.other.labels.create_category("From there", "green")
    pc.other_syncs()
    pc.round()
    pc.other.notes.set_category(note_id, category.id)
    pc.other_syncs()
    pc.round()
    window = pc.window_of(note_id)
    assert window is not None and window.category is not None
    assert window.category.id == category.id


def test_a_change_here_goes_a_moment_later(pc: Pc, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedule_module, "EDIT_WAIT", 0.05)
    note = pc.notes.create("written here")
    with pc.qtbot.waitSignal(pc.syncing.round_shown, timeout=ROUND_MS):
        pc.notes.set_color(note.id, "green")
    assert item_path(NOTES, note.id) in pc.cloud.files()


def test_what_sync_itself_writes_starts_no_round(pc: Pc) -> None:
    note_id = note_of_both(pc)
    assert note_id
    pc.qtbot.wait(300)  # any change noticed by now
    assert not pc.syncing.runner._schedule.waiting_to_put  # pyright: ignore[reportPrivateUsage]


def test_a_change_to_where_a_window_is_starts_no_round(pc: Pc) -> None:
    note_id = note_of_both(pc)
    window = pc.window_of(note_id)
    assert window is not None
    pc.manager.save_layout(window, force=True)
    pc.qtbot.wait(100)
    assert not pc.syncing.runner._schedule.waiting_to_put  # pyright: ignore[reportPrivateUsage]


def test_a_big_note_waits_until_it_is_left(pc: Pc, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedule_module, "BIG_EDIT_WAIT", 3600.0)
    monkeypatch.setattr(schedule_module, "EDIT_WAIT", 0.01)
    note = pc.notes.create("x" * (BIG_TEXT + 1))
    # Made at once with its text: this is the save of a change to it.
    pc.notes.update_body(note.id, "y" * (BIG_TEXT + 1))
    pc.qtbot.wait(200)
    assert item_path(NOTES, note.id) not in pc.cloud.files()
    with pc.qtbot.waitSignal(pc.syncing.round_shown, timeout=ROUND_MS):
        pc.syncing.runner.left()
    assert item_path(NOTES, note.id) in pc.cloud.files()


def test_quitting_sends_what_is_waiting(qtbot: QtBot, tmp_path: Path, cloud: FakeCloud) -> None:
    pc = make_pc(qtbot, tmp_path, cloud)
    pc.manager.open_stored()
    pc.syncing.start()
    pc.round()
    note = pc.notes.create("the last words")
    qtbot.wait(20)
    pc.syncing.finish(5.0)
    assert item_path(NOTES, note.id) in cloud.files()
    for window in pc.manager.windows:
        window.release()
    pc.other.close()
    pc.db.close()


def test_quitting_with_the_system_sends_nothing_more(
    qtbot: QtBot, tmp_path: Path, cloud: FakeCloud
) -> None:
    pc = make_pc(qtbot, tmp_path, cloud)
    pc.manager.open_stored()
    pc.syncing.start()
    pc.round()
    note = pc.notes.create("the last words")
    pc.syncing.finish(0.0)
    assert item_path(NOTES, note.id) not in cloud.files()
    for window in pc.manager.windows:
        window.release()
    pc.other.close()
    pc.db.close()


def test_a_note_changed_while_saving_at_quit_is_noticed(
    qtbot: QtBot, tmp_path: Path, cloud: FakeCloud
) -> None:
    """Saved as the app quits: the event loop will not come round again."""
    pc = make_pc(qtbot, tmp_path, cloud)
    pc.manager.open_stored()
    pc.syncing.start()
    pc.round()
    note = pc.notes.create("the last words")  # noticed only by the timer
    pc.syncing.finish(5.0)
    assert item_path(NOTES, note.id) in cloud.files()
    for window in pc.manager.windows:
        window.release()
    pc.other.close()
    pc.db.close()


def test_a_failing_cloud_is_tried_again_later(pc: Pc) -> None:
    pc.cloud.trouble = lambda _request: Trouble.UNAVAILABLE
    ended = pc.round()
    assert ended.outcome.wait == 15.0
    assert ended.outcome.failures == 1
    pc.cloud.trouble = lambda _request: None
    assert pc.round().outcome.failures == 0


def test_what_a_failing_round_took_in_is_shown(pc: Pc) -> None:
    """The round takes notes in order of their ids: the first in, the second not put."""
    first, last = "00000000-0000-4000-8000-000000000001", "ffffffff-ffff-4fff-8fff-ffffffffffff"
    pc.other.notes.create("from there", note_id=first)
    pc.other_syncs()
    pc.round()
    pc.other.notes.update_body(first, "changed there")
    pc.other_syncs()
    pc.notes.create("written here", note_id=last)
    failing = item_path(NOTES, last)

    def trouble(request: Request) -> Trouble | None:
        return Trouble.UNAVAILABLE if request.path == failing and request.op == "put" else None

    pc.cloud.trouble = trouble
    ended = pc.round()
    assert ended.outcome.wait is not None
    window = pc.window_of(first)
    assert window is not None and window.text == "changed there"


def test_when_it_stops_it_says_why_and_keeps_it(pc: Pc, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise SignInNeededError()

    monkeypatch.setattr(syncing_module, "sync_vault", refuse)
    ended = pc.round()
    assert ended.outcome.stopped is Stopped.SIGN_IN
    pc.qtbot.waitUntil(lambda: _stopped(pc) == "sign in", timeout=2000)
    assert pc.syncing.runner._schedule.held  # pyright: ignore[reportPrivateUsage]


def _stopped(pc: Pc) -> str | None:
    login = sync_connection.current(pc.db)
    return None if login is None else login.stopped


@pytest.mark.parametrize(
    "why", ["sign in", "no access", "vault gone", "vault changed", "certificate"]
)
def test_what_stopped_stays_stopped_at_the_next_start(
    qtbot: QtBot, tmp_path: Path, cloud: FakeCloud, why: str
) -> None:
    pc = make_pc(qtbot, tmp_path, cloud, stopped=why)
    assert pc.syncing.runner._schedule.next_at() is None  # pyright: ignore[reportPrivateUsage]
    pc.syncing.finish(0.0)
    pc.other.close()
    pc.db.close()


@pytest.mark.parametrize("why", ["big change", "newer"])
def test_a_big_change_or_a_newer_format_is_looked_at_again_at_the_next_start(
    qtbot: QtBot, tmp_path: Path, cloud: FakeCloud, why: str
) -> None:
    pc = make_pc(qtbot, tmp_path, cloud, stopped=why)
    assert pc.syncing.runner._schedule.next_at() is not None  # pyright: ignore[reportPrivateUsage]
    pc.syncing.finish(0.0)
    pc.other.close()
    pc.db.close()


def test_a_round_through_after_it_was_stopped_clears_why(
    qtbot: QtBot, tmp_path: Path, cloud: FakeCloud
) -> None:
    pc = make_pc(qtbot, tmp_path, cloud, stopped="big change")
    pc.manager.open_stored()
    pc.syncing.start()
    pc.round()
    pc.qtbot.waitUntil(lambda: _stopped(pc) is None, timeout=2000)
    pc.syncing.finish(0.0)
    for window in pc.manager.windows:
        window.release()
    pc.other.close()
    pc.db.close()


def test_the_cloud_clock_is_kept_for_the_next_run(pc: Pc, tmp_path: Path) -> None:
    """Told by the responses it gets, which here are none: told by hand."""
    cloud_clock = pc.syncing._rounds.cloud  # pyright: ignore[reportPrivateUsage]
    cloud_clock.heard(wall_millis() + 60_000, 0)
    pc.round()
    pc.qtbot.waitUntil(lambda: abs(sync_connection.cloud_offset(pc.db)) > 30_000, timeout=2000)


def test_stamps_go_by_the_cloud_clock(pc: Pc) -> None:
    pc.syncing._rounds.cloud.heard(wall_millis() + 3_600_000, 0)  # pyright: ignore[reportPrivateUsage]
    note = pc.notes.create("later")
    pc.notes.set_color(note.id, "green")
    updated = datetime.fromisoformat(
        pc.notes.get(note.id).updated_at.replace("Z", "+00:00")  # pyright: ignore[reportOptionalMemberAccess]
    )
    assert updated - datetime.now(UTC) > timedelta(minutes=50)


def test_what_it_says_in_a_report_of_a_problem(pc: Pc) -> None:
    lines = pc.syncing.feedback_lines()
    assert lines[0].startswith("Sync: format ")
    joined = " ".join(lines)
    assert "nas.example" not in joined and VAULT not in joined


def test_a_device_with_a_new_id_goes_on_as_that(pc: Pc) -> None:
    pc.reminders.use_device("another-id")
    assert pc.reminders._repository._device == "another-id"  # pyright: ignore[reportPrivateUsage]


def test_a_reminder_dealt_with_elsewhere_stops_ringing_here(pc: Pc) -> None:
    note_id = note_of_both(pc)
    at = datetime.now(UTC).replace(second=0, microsecond=0)
    pc.clock.now = wall_millis() - 120_000  # the other computer sets it for a moment ahead
    reminder = pc.other.reminders.set(note_id, Rule(at.date(), at.time(), "UTC"), False, UTC)
    pc.other_syncs()
    pc.round()
    pc.reminders.check()
    window = pc.window_of(note_id)
    assert window is not None and window.ringing
    pc.other.reminders.dealt_with(reminder.id, UTC)
    pc.other_syncs()
    pc.round()
    assert not window.ringing


def test_a_mark_shows_in_the_language_here() -> None:
    assert syncing_module._shown_name("todo", None)  # pyright: ignore[reportPrivateUsage]
    assert syncing_module._shown_name("mine", "My mark") == "My mark"  # pyright: ignore[reportPrivateUsage]
    assert Mark("todo", None, "", 0).name is None


def test_nothing_but_a_failure_is_logged(pc: Pc, caplog: pytest.LogCaptureFixture) -> None:
    """No address, sign-in or vault in what it logs."""
    pc.cloud.trouble = lambda _request: Trouble.UNAVAILABLE
    with caplog.at_level("INFO"):
        pc.round()
    assert "nas.example" not in caplog.text
    assert VAULT not in caplog.text


def ten_notes(pc: Pc) -> list[str]:
    made = [pc.other.notes.create(f"note {n}").id for n in range(10)]
    pc.other_syncs()
    pc.round()
    return made


def test_a_big_change_stops_and_changes_nothing(pc: Pc) -> None:
    made = ten_notes(pc)
    for note_id in made:
        pc.other.notes.delete(note_id)
    pc.other_syncs()
    ended = pc.round()
    assert ended.outcome.stopped is Stopped.BIG_CHANGE and not ended.taken_in
    assert all(pc.window_of(note_id) is not None for note_id in made)
    assert backups(pc.path) == []
    assert not any(pc.notes.get(note_id).deleted for note_id in made)  # pyright: ignore[reportOptionalMemberAccess]


def test_a_big_change_taken_in_goes_ahead_after_a_backup(pc: Pc) -> None:
    made = ten_notes(pc)
    for note_id in made:
        pc.other.notes.delete(note_id)
    pc.other_syncs()
    assert pc.round().outcome.stopped is Stopped.BIG_CHANGE
    with pc.qtbot.waitSignal(pc.syncing.round_shown, timeout=ROUND_MS) as told:
        pc.syncing.take_big_change()
    ended = said(told)
    assert isinstance(ended, Ended) and ended.outcome.stopped is None
    assert ended.taken_in  # what it took in the user saw: not told of again
    assert all(pc.window_of(note_id) is None for note_id in made)
    assert all(pc.notes.get(note_id).deleted for note_id in made)  # pyright: ignore[reportOptionalMemberAccess]
    assert len(backups(pc.path)) == 1
    assert not pc.round().taken_in  # that round only


def test_a_big_change_with_no_backup_takes_nothing_in_and_stays_stopped(
    pc: Pc, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = ten_notes(pc)
    for note_id in made:
        pc.other.notes.delete(note_id)
    pc.other_syncs()
    assert pc.round().outcome.stopped is Stopped.BIG_CHANGE

    def full(*_args: object, **_kwargs: object) -> Path:
        raise OSError("the disk is full")

    monkeypatch.setattr(syncing_module, "take", full)
    with pc.qtbot.waitSignal(pc.syncing.round_shown, timeout=ROUND_MS) as told:
        pc.syncing.take_big_change()
    ended = said(told)
    assert isinstance(ended, Ended) and ended.backup_failed and not ended.taken_in
    assert ended.outcome.stopped is Stopped.BIG_CHANGE and ended.wait is None
    assert not any(pc.notes.get(note_id).deleted for note_id in made)  # pyright: ignore[reportOptionalMemberAccess]
    assert all(pc.window_of(note_id) is not None for note_id in made)
    assert _stopped(pc) == "big change"
    # Not tried again by itself, nor taken in by the next round asked for.
    assert pc.syncing.runner._schedule.next_at() is None  # pyright: ignore[reportPrivateUsage]
    monkeypatch.undo()
    assert pc.round().outcome.stopped is Stopped.BIG_CHANGE
    assert backups(pc.path) == []


def test_what_a_big_change_stopped_for_is_listed_note_by_note(pc: Pc) -> None:
    made = ten_notes(pc)
    assert pc.syncing.big_change().trash == []  # nothing stopped for
    for note_id in made[:6]:
        pc.other.notes.delete(note_id)
    for note_id in made[6:]:
        pc.other.notes.delete(note_id)
        pc.other.notes.purge(note_id)
    pc.other_syncs()
    assert pc.round().outcome.stopped is Stopped.BIG_CHANGE
    change = pc.syncing.big_change()
    assert sorted(t or "" for t in change.trash) == [f"note {n}" for n in range(6)]
    assert sorted(t or "" for t in change.gone) == [f"note {n}" for n in range(6, 10)]
    assert change.copies == []


def test_sync_ended_for_another_connection_does_no_more(pc: Pc) -> None:
    note_id = note_of_both(pc)
    pc.syncing.end()
    assert not pc.syncing.runner.running
    pc.notes.update_body(note_id, "changed after it ended")
    pc.qtbot.wait(300)
    pc.other_syncs()
    assert pc.other.texts()[note_id] == "first"  # nothing went up


def test_another_connection_takes_the_place_of_the_one_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done: list[str] = []

    class Current:
        def end(self) -> None:
            done.append("ended")

    def connect() -> Syncing:
        done.append("connected")
        return cast(Syncing, object())

    def resume(login: Login) -> Syncing:
        raise AssertionError("not resumed")

    replace_sync(cast(Syncing, Current()), connect, resume, lambda: None)
    assert done == ["ended", "connected"]
    done.clear()
    replace_sync(None, connect, resume, lambda: None)
    assert done == ["connected"]


def test_a_connection_that_fails_leaves_the_one_before_going_on() -> None:
    kept = Login(VAULT, WEBDAV, "https://nas.example/dav", "u", "p", KEYS.keys)
    resumed: list[Login] = []

    class Current:
        def end(self) -> None:
            pass

    def connect() -> Syncing:
        raise OSError("the disk is full")

    def resume(login: Login) -> Syncing:
        resumed.append(login)
        return cast(Syncing, object())

    with pytest.raises(OSError, match="full"):
        replace_sync(cast(Syncing, Current()), connect, resume, lambda: kept)
    assert resumed == [kept]


def joining(qtbot: QtBot, tmp_path: Path, cloud: FakeCloud) -> Pc:
    """A computer with notes of its own, about to join a vault with notes in it."""
    pc = make_pc(qtbot, tmp_path, cloud)
    pc.other.notes.create("in the vault")
    pc.other.notes.create("alike on both")
    pc.other_syncs()
    pc.notes.create("alike on both")
    pc.notes.create("only here")
    pc.manager.open_stored()
    pc.syncing.start()
    return pc


def end(pc: Pc) -> None:
    pc.syncing.finish(0.0)
    for window in pc.manager.windows:
        window.release()
    pc.other.close()
    pc.db.close()


def look_of(pc: Pc, told: list[tuple[str, int, int]] | None = None) -> FirstLook | None:
    """What merging would make, looked at as the window turning sync on has it done."""
    login = sync_connection.current(pc.db)
    assert login is not None

    def moving(doing: str, done: int, of: int) -> None:
        if told is not None:
            told.append((doing, done, of))

    return look_first(pc.path, KEY, login, moving, lambda _heard, _gone: pc.cloud.target("a"))


def test_what_a_first_connection_would_make_is_told_as_the_notes_come(
    qtbot: QtBot, tmp_path: Path, cloud: FakeCloud
) -> None:
    pc = joining(qtbot, tmp_path, cloud)
    told: list[tuple[str, int, int]] = []
    look = look_of(pc, told)
    assert isinstance(look, FirstLook)
    assert (look.here, look.cloud, look.alike) == (2, 2, 1)
    assert sorted(look.recent) == ["alike on both", "in the vault"]  # pyright: ignore[reportArgumentType]
    assert told == [(GETTING, 1, 2), (GETTING, 2, 2)]
    assert sorted(note.body for note in pc.notes.live()) == ["alike on both", "only here"]
    end(pc)


def test_a_first_look_that_fails_says_so(qtbot: QtBot, tmp_path: Path, cloud: FakeCloud) -> None:
    pc = joining(qtbot, tmp_path, cloud)
    cloud.trouble = lambda _request: Trouble.UNAVAILABLE
    assert look_of(pc) is None
    end(pc)


def test_only_the_apps_thread_collects_garbage_while_sync_runs(pc: Pc) -> None:
    """Garbage may hold windows, which only the app's thread may destroy."""
    assert not gc.isenabled()


def test_the_apps_thread_collects_now_and_then(pc: Pc, monkeypatch: pytest.MonkeyPatch) -> None:
    collected: list[bool] = []

    def collect() -> int:
        collected.append(True)
        return 0

    monkeypatch.setattr(syncing_module.gc, "collect", collect)
    collector = pc.syncing._collector  # pyright: ignore[reportPrivateUsage]
    assert collector.isActive()
    assert collector.interval() == syncing_module.COLLECT_EVERY_MS
    collector.timeout.emit()
    assert collected == [True]


def test_collecting_goes_on_as_usual_when_sync_has_ended(
    qtbot: QtBot, tmp_path: Path, cloud: FakeCloud
) -> None:
    pc = make_pc(qtbot, tmp_path, cloud)
    pc.manager.open_stored()
    pc.syncing.start()
    pc.round()
    end(pc)
    assert gc.isenabled()


def test_collecting_stays_off_while_the_thread_is_still_at_work(
    qtbot: QtBot, tmp_path: Path, cloud: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    going = threading.Event()
    let_go = threading.Event()

    def round_of(*_args: object, **_kwargs: object) -> None:
        going.set()
        let_go.wait(10)

    monkeypatch.setattr(syncing_module, "sync_vault", round_of)
    pc = make_pc(qtbot, tmp_path, cloud)
    pc.manager.open_stored()
    pc.syncing.start()
    pc.syncing.sync_now()
    assert going.wait(10)
    pc.syncing.finish(0.0)
    assert not gc.isenabled()
    let_go.set()
    qtbot.waitUntil(lambda: not pc.syncing.runner.running, timeout=ROUND_MS)
    for window in pc.manager.windows:
        window.release()
    pc.other.close()
    pc.db.close()


def test_a_report_of_a_problem_says_how_sync_is_doing(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    told: list[tuple[bool, object]] = []

    def feedback(portable: bool, sync: object = ()) -> bool:
        told.append((portable, sync))
        return True

    monkeypatch.setattr("stickle.app.about_dialog.open_feedback", feedback)
    dialog = AboutDialog(None, sync_lines=lambda: ("Sync: format 1",))
    qtbot.addWidget(dialog)
    dialog.report()
    assert told == [(False, ("Sync: format 1",))]


def test_a_login_that_cannot_be_read_does_not_stop_the_notes(tmp_path: Path) -> None:
    db = open_store(tmp_path / "x.db", KEY)
    sync_connection.keep(db, Login(VAULT, WEBDAV, "https://n.example", "u", "p", KEYS.keys))
    db.execute("UPDATE sync_connection SET keys = 'not json'")
    assert login_here(db) is None
    db.close()


def test_no_login_no_sync(tmp_path: Path) -> None:
    db = open_store(tmp_path / "x.db", KEY)
    assert login_here(db) is None
    db.close()


def test_stored_times_are_this_computers_without_sync() -> None:
    clocks = Clocks()
    assert abs(clocks.millis() - wall_millis()) < 2_000
    assert clocks.stamp().endswith("Z")


def test_stored_times_are_the_clouds_with_it() -> None:
    clocks = Clocks()
    clocks.cloud = lambda: 1_767_225_600_000  # 2026-01-01
    assert clocks.millis() == 1_767_225_600_000
    assert clocks.stamp() == "2026-01-01T00:00:00.000Z"
