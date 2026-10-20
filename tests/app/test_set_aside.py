"""Every note out of sight for a while, a note to type in at once, a note from
the clipboard, and the command line that asks a running Stickle for them."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from PySide6.QtGui import QGuiApplication
from pytestqt.qtbot import QtBot
from test_autosave import compose
from test_stickle_window import App

from stickle import __main__ as entry
from stickle.app.application import answer_request
from stickle.app.i18n import Translations
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.platform import instance
from stickle.platform.instance import NEW_NOTE, SET_ASIDE, SHOW, InstanceLock

KEY = secrets.token_bytes(32)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def app(qtbot: QtBot, connection: apsw.Connection) -> Iterator[App]:
    app = App(connection, tray=True)
    yield app
    app.close()


@pytest.fixture
def trayless(qtbot: QtBot, connection: apsw.Connection) -> Iterator[App]:
    app = App(connection, tray=False)
    yield app
    app.close()


def note(app: App, text: str) -> NoteWindow:
    window = app.manager.new_note()
    window.editor.insertPlainText(text)
    app.manager.save(window)
    return window


def on_screen(app: App) -> list[str]:
    return sorted(window.text for window in app.manager.windows if window.isVisible())


def stay_put(_window: NoteWindow) -> None:
    """In place of bringing a note to the front, which tests need not see."""


# Every note out of sight for a while


def test_setting_aside_takes_notes_off_screen_and_changes_nothing_stored(app: App) -> None:
    note(app, "A")
    note(app, "B")
    app.manager.hide(note(app, "C"))

    app.manager.set_all_aside()

    assert on_screen(app) == []
    assert sorted(n.body for n in app.notes.visible()) == ["A", "B"]  # not hidden notes
    assert [n.body for n in app.notes.hidden()] == ["C"]


def test_they_come_back_where_they_were_and_hidden_ones_stay_hidden(app: App) -> None:
    first = note(app, "A")
    note(app, "B")
    app.manager.hide(note(app, "C"))
    first.move(first.pos().x() + 40, first.pos().y() + 30)
    where = first.geometry()

    app.manager.switch_set_aside()
    app.manager.switch_set_aside()

    assert on_screen(app) == ["A", "B"]
    assert first.geometry() == where
    assert [n.body for n in app.notes.hidden()] == ["C"]
    assert not app.manager.set_aside


def test_text_being_typed_is_saved_before_going_out_of_sight(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = note(app, "안녕")
    window.edit()
    compose(window, "하")

    def commit() -> None:  # as an input method asked to commit does
        compose(window, "", commit="하")

    monkeypatch.setattr(window, "_request_commit", commit)
    monkeypatch.setattr(window.editor, "hasFocus", lambda: True)

    app.manager.set_all_aside()

    assert [n.body for n in app.notes.visible()] == ["안녕하"]


def test_the_window_and_the_tray_say_so_while_it_holds(app: App) -> None:
    note(app, "A")
    button = app.window.set_aside_button
    assert button.text() == "Hide all notes for now"
    assert not app.window.set_aside_label.isVisibleTo(app.window)

    button.click()
    assert button.text() == "Show the notes again"
    assert app.window.set_aside_label.isVisibleTo(app.window)
    assert app.tray.set_aside_action.text() == "Sho&w the notes again"

    button.click()
    assert on_screen(app) == ["A"]
    assert app.tray.set_aside_action.text() == "Hide all notes for no&w"
    assert not app.window.set_aside_label.isVisibleTo(app.window)


def test_the_tray_icon_fades_while_the_notes_are_out_of_sight(app: App) -> None:
    note(app, "A")

    def ink() -> int:
        """How solid the tray icon is: the sum of its pixels' alpha."""
        image = app.tray.icon().pixmap(32, 32).toImage()
        return sum(
            image.pixelColor(x, y).alpha()
            for x in range(image.width())
            for y in range(image.height())
        )

    shown = ink()
    assert app.tray.toolTip() == "Stickle"
    app.window.set_aside_button.click()
    assert ink() < shown * 0.6
    assert app.tray.toolTip() == "Stickle: notes hidden for now"
    app.window.set_aside_button.click()
    assert ink() == shown
    assert app.tray.toolTip() == "Stickle"


def test_the_next_start_shows_them_again(app: App, connection: apsw.Connection) -> None:
    note(app, "A")
    app.manager.set_all_aside()
    app.manager.prepare_to_quit()  # quitting while they are out of sight

    again = NoteManager(NoteRepository(connection))
    again.open_stored()
    assert [w.text for w in again.windows if w.isVisible()] == ["A"]
    for window in again.windows:
        window.release()


def test_a_note_opened_meanwhile_shows_alone_until_the_rest_come_back(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(NoteWindow, "bring_to_front", stay_put)
    first = note(app, "A")
    note(app, "B")
    app.manager.set_all_aside()

    new = note(app, "새 메모")
    assert on_screen(app) == ["새 메모"]
    NoteWindow.show(first)  # as bring_to_front would
    app.manager.open_note(str(first.note_id))
    assert on_screen(app) == ["A", "새 메모"]
    assert app.manager.set_aside  # B still is

    app.manager.bring_back()
    assert on_screen(app) == ["A", "B", "새 메모"]
    assert new.isVisible()


def test_bringing_all_to_front_brings_them_back(app: App) -> None:
    note(app, "A")
    app.manager.set_all_aside()

    app.manager.raise_all()

    assert on_screen(app) == ["A"] and not app.manager.set_aside


def test_without_a_tray_closing_the_window_brings_them_back_rather_than_quitting(
    trayless: App,
) -> None:
    note(trayless, "A")
    trayless.manager.set_all_aside()
    trayless.window.open()

    trayless.window.close()

    assert on_screen(trayless) == ["A"]
    assert trayless.quits == []


# A note to type in at once


def test_the_new_note_request_opens_one_ready_to_type_and_does_not_pile_up(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(NoteWindow, "bring_to_front", stay_put)
    note(app, "A")

    first = app.manager.quick_note()
    again = app.manager.quick_note()

    assert again is first  # still untouched: used again
    assert first.editing
    assert len(app.manager.windows) == 2

    first.editor.insertPlainText("장보기")
    assert app.manager.quick_note() is not first


def test_a_new_note_while_notes_are_set_aside_shows_alone(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(NoteWindow, "bring_to_front", stay_put)
    note(app, "A")
    app.manager.set_all_aside()

    window = app.manager.quick_note()
    NoteWindow.show(window)

    assert on_screen(app) == [""]
    assert app.manager.set_aside


# A note from the clipboard


def test_a_note_is_made_from_the_clipboard_and_stored_at_once(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(NoteWindow, "bring_to_front", stay_put)
    QGuiApplication.clipboard().setText("장보기 목록\n- 우유")
    app.window.refresh_clipboard()
    assert app.window.clipboard_button.isEnabled()

    app.window.clipboard_button.click()

    assert [n.body for n in app.notes.visible()] == ["장보기 목록\n- 우유"]


def test_nul_is_left_out_of_a_note_from_the_clipboard(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(NoteWindow, "bring_to_front", stay_put)
    QGuiApplication.clipboard().setText("a\x00b")

    app.manager.note_from_clipboard()

    assert [n.body for n in app.notes.visible()] == ["ab"]


@pytest.mark.parametrize("held", ["", "   \n"])
def test_with_no_text_on_the_clipboard_there_is_nothing_to_make(app: App, held: str) -> None:
    QGuiApplication.clipboard().setText(held)
    app.window.refresh_clipboard()

    assert not app.window.clipboard_button.isEnabled()
    assert app.manager.note_from_clipboard() is None
    assert app.notes.all() == []


# Asked for from the command line


def test_each_request_does_what_it_says(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(NoteWindow, "bring_to_front", stay_put)
    note(app, "A")

    answer_request(SET_ASIDE, app.manager, app.window)
    assert app.manager.set_aside
    answer_request(SET_ASIDE, app.manager, app.window)
    assert not app.manager.set_aside
    answer_request(NEW_NOTE, app.manager, app.window)
    assert len(app.manager.windows) == 2 and app.manager.windows[-1].editing
    answer_request(SHOW, app.manager, app.window)
    assert app.window.isVisible()


@pytest.mark.parametrize(
    ("option", "expected"),
    [("--new-note", NEW_NOTE), ("--show", SHOW), ("--hide-all", SET_ASIDE), (None, SHOW)],
)
def test_a_second_start_passes_its_request_to_the_running_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: str | None, expected: bytes
) -> None:
    monkeypatch.setenv("STICKLE_DATA_DIR", str(tmp_path))
    sent: list[bytes] = []

    def ask(_folder: Path, asked: bytes = SHOW, timeout: float = 0) -> bool:
        sent.append(asked)
        return True

    monkeypatch.setattr(instance, "ask", ask)
    running = InstanceLock(tmp_path)
    assert running.acquire()
    try:
        assert entry.main(["stickle", *([option] if option else [])]) == 0
    finally:
        running.release()

    assert sent == [expected]


def test_the_requests_are_written_in_the_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        entry.main(["stickle", "--help"])

    shown = capsys.readouterr().out
    assert "--new-note" in shown and "--show" in shown and "--hide-all" in shown


def test_the_new_strings_are_translated(app: App, translations: Translations) -> None:
    translations.apply("ko")
    note(app, "A")
    assert app.window.set_aside_button.text() == "모든 메모 잠시 숨기기"
    app.manager.set_all_aside()
    assert app.window.set_aside_button.text() == "메모 다시 보이기"
    assert app.window.set_aside_label.text().startswith("모든 메모를 잠시")
    assert app.window.clipboard_button.text() == "클립보드로 새 메모"
