"""The one line that says how sync stands: which of them shows, and what it says."""

from PySide6.QtCore import QDate, QDateTime, QLocale, QTime
from pytestqt.qtbot import QtBot

from stickle.app.sync_status import SyncStatus
from stickle.app.syncing import Ended
from stickle.app.times import moment_text
from stickle.sync.engine import GETTING, PUTTING, Report
from stickle.sync.outcome import Outcome, Stopped

CLOUD = "WebDAV"


class Line:
    """The line, and what it is told of."""

    def __init__(self, stopped: Stopped | None = None) -> None:
        self.to_put = 0
        self.synced: QDateTime | None = QDateTime(QDate.currentDate(), QTime(14, 5))
        self.status = SyncStatus(CLOUD, stopped, lambda: self.to_put, lambda: self.synced)
        self.changes = 0
        self.status.changed.connect(self._changed)

    def _changed(self) -> None:
        self.changes += 1

    def ended(self, outcome: Outcome = Outcome(), report: Report | None = None) -> str:  # noqa: B008
        self.status.round_ended(Ended(report or Report(), outcome))
        return self.status.text()


def at_two_past_five() -> str:
    return QLocale().toString(QTime(14, 5), QLocale.FormatType.ShortFormat)


def test_it_says_when_it_last_went_through(qtbot: QtBot) -> None:
    line = Line()
    assert line.status.text() == f"Synced with WebDAV · {at_two_past_five()}"


def test_a_day_that_was_not_today_is_said_too(qtbot: QtBot) -> None:
    line = Line()
    line.synced = QDateTime(QDate.currentDate().addDays(-1), QTime(14, 5))
    text = line.ended()
    assert text == f"Synced with WebDAV · {moment_text(line.synced)}"


def test_before_anything_is_known_it_says_nothing(qtbot: QtBot) -> None:
    line = Line()
    line.synced = None
    assert line.ended() == ""


def test_stopped_as_it_was_left_is_said_before_any_round(qtbot: QtBot) -> None:
    line = Line(Stopped.SIGN_IN)
    assert line.status.text() == "Sync stopped — check your server password"


def test_a_round_going_through_ends_that(qtbot: QtBot) -> None:
    line = Line(Stopped.SIGN_IN)
    assert line.ended().startswith("Synced with")


def test_a_full_cloud_is_said_until_there_is_room(qtbot: QtBot) -> None:
    line = Line()
    assert line.ended(Outcome(full=True)) == "Uploads stopped — your WebDAV is full"
    assert line.ended().startswith("Synced with")


def test_stopped_goes_before_full(qtbot: QtBot) -> None:
    line = Line()
    text = line.ended(Outcome(Stopped.BIG_CHANGE, full=True))
    assert text == "Sync stopped — check a big change from another computer"


def test_how_far_it_has_got_is_said_of_more_than_twenty_notes(qtbot: QtBot) -> None:
    line = Line()
    line.status.moved(PUTTING, 340, 1000)
    number = QLocale().toString(1000)
    assert line.status.text() == f"Uploading to WebDAV · 340 of {number} notes"
    line.status.moved(GETTING, 3, 21)
    assert line.status.text() == "Downloading from WebDAV · 3 of 21 notes"


def test_a_few_notes_on_the_move_are_not_said(qtbot: QtBot) -> None:
    line = Line()
    before, told = line.status.text(), line.changes
    line.status.moved(PUTTING, 1, 20)
    assert line.status.text() == before
    assert line.changes == told


def test_how_far_is_gone_once_the_round_is_over(qtbot: QtBot) -> None:
    line = Line()
    line.status.moved(PUTTING, 340, 1000)
    assert line.ended().startswith("Synced with")


def test_with_no_answer_it_says_offline(qtbot: QtBot) -> None:
    line = Line()
    assert line.ended(Outcome(wait=15.0, failures=1, offline=True)) == "Offline"


def test_offline_says_what_waits_to_go_up(qtbot: QtBot) -> None:
    line = Line()
    line.to_put = 3
    text = line.ended(Outcome(wait=15.0, failures=1, offline=True))
    assert text == "Offline · 3 note(s) to upload"


def test_an_answer_that_says_it_failed_is_not_offline(qtbot: QtBot) -> None:
    line = Line()
    line.to_put = 3
    assert (
        line.ended(Outcome(wait=15.0, failures=1)) == "3 note(s) to upload · trying again shortly"
    )


def test_failing_with_nothing_to_upload_stays_as_it_was(qtbot: QtBot) -> None:
    line = Line()
    assert line.ended(Outcome(wait=15.0, failures=1)).startswith("Synced with")


def test_notes_a_round_passed_over_are_said_though_it_went_through(qtbot: QtBot) -> None:
    line = Line()
    text = line.ended(report=Report(left=["a", "b", "a"]))
    assert text == "2 note(s) to upload · trying again shortly"


def test_how_far_is_told_at_most_twice_a_second(qtbot: QtBot) -> None:
    line = Line()
    line.status.moved(PUTTING, 21, 1000)
    for done in range(22, 60):
        line.status.moved(PUTTING, done, 1000)
    assert line.changes == 1
    assert line.status.text().startswith("Uploading to WebDAV · 59 of")  # the line is up to date
    qtbot.wait(520)
    line.status.moved(PUTTING, 60, 1000)
    assert line.changes == 2


def test_a_new_round_tells_how_far_at_once(qtbot: QtBot) -> None:
    line = Line()
    line.status.moved(PUTTING, 21, 1000)
    line.ended()
    told = line.changes
    line.status.moved(PUTTING, 22, 1000)
    assert line.changes == told + 1


def test_it_says_whether_there_was_no_answer_and_what_is_to_do(qtbot: QtBot) -> None:
    line = Line()
    assert not line.status.offline and not line.status.halted
    line.ended(Outcome(wait=15.0, failures=1, offline=True))
    assert line.status.offline and not line.status.halted
    line.ended(Outcome(full=True))
    assert line.status.halted and not line.status.offline
    line.ended(Outcome(Stopped.SIGN_IN))
    assert line.status.halted and not line.status.update_carries_on
    line.ended(Outcome(Stopped.NEWER))
    assert line.status.update_carries_on
    line.ended()
    assert not line.status.halted and not line.status.offline


def test_stopped_as_it_was_left_is_what_there_is_to_do(qtbot: QtBot) -> None:
    assert Line(Stopped.BIG_CHANGE).status.halted


def test_changing_what_a_round_changed_happens_once(qtbot: QtBot) -> None:
    line = Line()
    line.ended()
    assert line.changes == 1
    line.status.moved(PUTTING, 21, 22)
    assert line.changes == 2
