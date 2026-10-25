"""Shortcuts from anywhere: registered at start, changed or turned off in the
Stickle window, and said so when another app has one."""

import secrets
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import override

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from stickle.app.i18n import Translations
from stickle.app.notes import NoteManager
from stickle.app.shortcuts import DEFAULTS, GlobalShortcuts, Refused, State, readable_keys
from stickle.app.stickle_window import StickleWindow
from stickle.data.schema import open_store
from stickle.data.settings import SHORTCUTS, Settings
from stickle.platform.hotkeys import Combo, Hotkeys, Portal
from stickle.platform.instance import NEW_NOTE, SET_ASIDE, SHOW

KEY = secrets.token_bytes(32)


class FakeHotkeys:
    """The system's register, with some combinations taken by other apps."""

    def __init__(self, pressed: Callable[[int], object], taken: set[str]) -> None:
        self.pressed = pressed
        self.taken = taken
        self.registered: dict[int, str] = {}

    def register(self, number: int, combo: Combo) -> bool:
        if combo.text in self.taken or combo.text in self.registered.values():
            return False
        self.registered[number] = combo.text
        return True

    def unregister(self, number: int) -> None:
        self.registered.pop(number, None)


class System:
    def __init__(self, taken: set[str] | None = None, available: bool = True) -> None:
        self.taken = taken or set()
        self.available = available
        self.hotkeys: FakeHotkeys | None = None

    def __call__(self, pressed: Callable[[int], object]) -> Hotkeys | None:
        if not self.available:
            return None
        self.hotkeys = FakeHotkeys(pressed, self.taken)
        return self.hotkeys

    @property
    def registered(self) -> list[str]:
        assert self.hotkeys is not None
        return sorted(self.hotkeys.registered.values())


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield Settings(connection)
    connection.close()


def test_the_defaults_are_registered_at_start(settings: Settings) -> None:
    system = System()
    shortcuts = GlobalShortcuts(settings, system)

    assert system.registered == ["Ctrl+Alt+H", "Ctrl+Alt+N", "Ctrl+Alt+S"]
    on = [action for action in DEFAULTS if DEFAULTS[action]]
    assert all(shortcuts.state(action) == State.ON for action in on)
    assert shortcuts.state("next-category") == State.OFF  # no keys until chosen
    assert shortcuts.state("find") == State.OFF


def test_pressing_one_asks_for_what_the_command_line_would(settings: Settings) -> None:
    system = System()
    shortcuts = GlobalShortcuts(settings, system)
    heard: list[bytes] = []
    shortcuts.pressed.connect(heard.append)
    assert system.hotkeys is not None
    numbers = {text: number for number, text in system.hotkeys.registered.items()}

    for text in ("Ctrl+Alt+N", "Ctrl+Alt+S", "Ctrl+Alt+H"):
        system.hotkeys.pressed(numbers[text])

    assert heard == [NEW_NOTE, SHOW, SET_ASIDE]


def test_one_another_app_has_is_left_out_and_said_so(settings: Settings) -> None:
    shortcuts = GlobalShortcuts(settings, System(taken={"Ctrl+Alt+H"}))

    assert shortcuts.state("hide-all") == State.TAKEN
    assert shortcuts.state("new-note") == State.ON  # the others still work


def test_a_changed_shortcut_is_kept_on_this_computer_and_used_at_once(settings: Settings) -> None:
    system = System()
    shortcuts = GlobalShortcuts(settings, system)

    assert shortcuts.change("hide-all", "Ctrl+Shift+F9") is None

    assert "Ctrl+Shift+F9" in system.registered and "Ctrl+Alt+H" not in system.registered
    assert settings.get(SHORTCUTS) == {"hide-all": "Ctrl+Shift+F9"}
    again = GlobalShortcuts(settings, System())
    assert again.combo("hide-all") == "Ctrl+Shift+F9"


def test_one_turned_off_stays_off(settings: Settings) -> None:
    system = System()
    shortcuts = GlobalShortcuts(settings, system)

    shortcuts.change("show", "")

    assert "Ctrl+Alt+S" not in system.registered
    assert shortcuts.state("show") == State.OFF
    assert GlobalShortcuts(settings, System()).combo("show") == ""


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        ("N", Refused.NOT_A_SHORTCUT),
        ("Shift+N", Refused.NOT_A_SHORTCUT),
        ("Ctrl+Alt+Space", Refused.NOT_A_SHORTCUT),
        ("Ctrl+Alt+N", Refused.IN_USE_HERE),  # the new note's
    ],
)
def test_a_combination_that_cannot_work_is_refused_and_nothing_changes(
    settings: Settings, text: str, refused: Refused
) -> None:
    system = System()
    shortcuts = GlobalShortcuts(settings, system)

    assert shortcuts.change("hide-all", text) == refused

    assert shortcuts.combo("hide-all") == "Ctrl+Alt+H"
    assert settings.get(SHORTCUTS) == {}


def test_a_stored_combination_no_longer_understood_falls_back_to_the_default(
    settings: Settings,
) -> None:
    settings.set(SHORTCUTS, {"new-note": "Hyper+Q"})

    assert GlobalShortcuts(settings, System()).combo("new-note") == "Ctrl+Alt+N"


def test_where_shortcuts_cannot_be_set_nothing_is_registered(settings: Settings) -> None:
    shortcuts = GlobalShortcuts(settings, System(available=False))

    assert not shortcuts.available
    assert all(shortcuts.state(action) == State.UNAVAILABLE for action in DEFAULTS)


# In the Stickle window


class Window:
    def __init__(self, settings: Settings, system: System) -> None:
        self.shortcuts = GlobalShortcuts(settings, system)
        self.manager = NoteManager(None)
        self.window = StickleWindow(
            self.manager, Translations(), lambda: None, shortcuts=self.shortcuts
        )
        rows = self.window.settings.shortcut_rows
        assert rows is not None
        self.rows = rows

    def choose(self, action: str, text: str) -> None:
        edit = self.rows.edits[action]
        edit.setKeySequence(QKeySequence(text))
        edit.editingFinished.emit()

    def close(self) -> None:
        # Gone before the database closes: a later language switch reaches every window.
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def window(qtbot: QtBot, settings: Settings) -> Iterator[Window]:
    window = Window(settings, System(taken={"Ctrl+Alt+S"}))
    yield window
    window.close()


def test_each_shortcut_is_shown_with_what_holds_for_it(window: Window) -> None:
    rows = window.rows
    assert rows.edits["new-note"].keySequence() == QKeySequence("Ctrl+Alt+N")
    assert rows.edits["new-note"].accessibleName() == "New note"
    assert not rows.notes["new-note"].isVisibleTo(window.rows)
    assert "Another app is using Ctrl+Alt+S" in rows.notes["show"].text()
    assert rows.notes["show"].isVisibleTo(window.rows)
    assert not rows.unavailable.isVisibleTo(window.rows)


def test_a_new_combination_chosen_in_the_window_is_used(window: Window) -> None:
    window.choose("show", "Ctrl+Alt+J")

    assert window.shortcuts.combo("show") == "Ctrl+Alt+J"
    assert window.shortcuts.state("show") == State.ON
    assert not window.rows.notes["show"].isVisibleTo(window.rows)


def test_a_combination_that_cannot_work_is_explained_and_put_back(window: Window) -> None:
    window.choose("hide-all", "Shift+Q")

    assert window.rows.edits["hide-all"].keySequence() == QKeySequence("Ctrl+Alt+H")
    assert "cannot be used" in window.rows.notes["hide-all"].text()

    window.choose("hide-all", "Ctrl+Alt+N")
    assert "already uses" in window.rows.notes["hide-all"].text()


def test_the_clear_button_turns_a_shortcut_off(window: Window) -> None:
    window.rows.edits["new-note"].clear()

    assert window.shortcuts.combo("new-note") == ""
    assert window.shortcuts.state("new-note") == State.OFF


def test_where_shortcuts_cannot_be_set_the_window_says_how_to_have_them(
    qtbot: QtBot, settings: Settings
) -> None:
    window = Window(settings, System(available=False))

    assert window.rows.unavailable.isVisibleTo(window.rows)
    assert "--new-note" in window.rows.unavailable.text()
    assert not window.rows.edits["new-note"].isVisibleTo(window.rows)
    window.close()


def test_the_rows_are_translated(window: Window, translations: Translations) -> None:
    translations.apply("ko")

    assert window.rows.heading.text() == "어디서나 쓰는 단축키"
    assert window.rows.edits["new-note"].accessibleName() == "새 메모"
    assert "다른 앱이" in window.rows.notes["show"].text()


def test_nothing_unusable_is_stored_even_by_hand(settings: Settings) -> None:
    with pytest.raises(ValueError):
        settings.set(SHORTCUTS, {"quit": "Ctrl+Q"})


# Kept by the desktop (Wayland, through its portal)


class FakePortal(Portal):
    def __init__(self, has_page: bool = True) -> None:
        super().__init__()
        self.offered: list[tuple[str, str, str]] = []
        self.parent_window = "not offered"
        self.has_page = has_page
        self.pages_opened = 0

    @override
    def bind(self, shortcuts: list[tuple[str, str, str]], parent_window: str = "") -> None:
        self.offered = shortcuts
        self.parent_window = parent_window

    @override
    def configure(self) -> bool:
        self.pages_opened += 1
        return self.has_page


def kept_by_desktop(settings: Settings, portal: FakePortal) -> GlobalShortcuts:
    return GlobalShortcuts(settings, System(available=False), make_portal=lambda: portal)


def test_the_desktop_is_offered_the_shortcuts_once_stickle_is_up(settings: Settings) -> None:
    settings.set(SHORTCUTS, {"hide-all": ""})  # turned off here before
    portal = FakePortal()
    shortcuts = kept_by_desktop(settings, portal)
    assert shortcuts.available and shortcuts.by_desktop
    assert portal.offered == []  # not while Stickle starts
    assert all(shortcuts.state(action) == State.WAITING for action in DEFAULTS)

    shortcuts.start()

    offered = {action: keys for action, _, keys in portal.offered}
    assert offered == {
        "new-note": "CTRL+ALT+n",
        "show": "CTRL+ALT+s",
        "hide-all": "",
        "next-category": "",
        "find": "",
    }
    assert all(description for _, description, _ in portal.offered)


def test_the_desktops_question_belongs_to_the_stickle_window_when_it_shows(
    qtbot: QtBot, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(QGuiApplication, "platformName", lambda: "xcb")
    window = QWidget()
    qtbot.addWidget(window)

    hidden = FakePortal()
    kept_by_desktop(settings, hidden).start(window)
    window.show()
    showing = FakePortal()
    kept_by_desktop(settings, showing).start(window)

    assert hidden.parent_window == ""  # nothing to come up over
    assert showing.parent_window == f"x11:{int(window.winId()):x}"


def test_what_the_desktop_gave_is_shown_and_its_presses_heard(settings: Settings) -> None:
    portal = FakePortal()
    shortcuts = kept_by_desktop(settings, portal)
    heard: list[bytes] = []
    shortcuts.pressed.connect(heard.append)
    shortcuts.start()

    portal.bound.emit({"new-note": "Ctrl+Alt+N", "show": "", "hide-all": "Super+H"})
    portal.activated.emit("hide-all")

    assert shortcuts.combo("new-note") == "Ctrl+Alt+N"
    assert shortcuts.state("show") == State.OFF
    assert shortcuts.state("hide-all") == State.ON
    assert heard == [SET_ASIDE]


def test_a_desktop_that_cannot_keep_them_leaves_the_command_line(settings: Settings) -> None:
    portal = FakePortal()
    shortcuts = kept_by_desktop(settings, portal)
    shortcuts.start()

    portal.failed.emit()

    assert not shortcuts.available
    assert all(shortcuts.state(action) == State.UNAVAILABLE for action in DEFAULTS)


def test_the_window_shows_the_desktops_keys_and_leads_to_its_settings(
    qtbot: QtBot, settings: Settings
) -> None:
    portal = FakePortal()
    shortcuts = kept_by_desktop(settings, portal)
    window = StickleWindow(NoteManager(None), Translations(), lambda: None, shortcuts=shortcuts)
    rows = window.settings.shortcut_rows
    assert rows is not None

    assert "asked" in rows.desktop_note.text()
    assert not rows.configure_button.isEnabled()  # nothing to change yet

    shortcuts.start()
    portal.bound.emit({"new-note": "Ctrl+Alt+N", "show": "", "hide-all": "Super+H"})

    assert rows.given["new-note"].text() == "Ctrl+Alt+N"
    assert rows.given["show"].text() == "none"
    assert rows.given["new-note"].isVisibleTo(rows)
    assert not rows.edits["new-note"].isVisibleTo(rows)
    assert "keyboard settings" in rows.desktop_note.text()
    rows.configure_button.click()
    assert portal.pages_opened == 1
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize(
    ("described", "shown"),
    [
        ("Press <Control><Alt>n", "Ctrl+Alt+N"),
        ("<Control><Alt>n 키를 누르십시오", "Ctrl+Alt+N"),  # GNOME in Korean
        ("Drücken Sie <Control><Alt>n", "Ctrl+Alt+N"),
        ("<Super><Shift>F7", "Super+Shift+F7"),
        ("Ctrl+Alt+N", "Ctrl+Alt+N"),  # already readable: as it is
        ("", ""),
        ("Press <Hyper>q", "Press <Hyper>q"),  # not known: as the desktop wrote it
    ],
)
def test_the_desktops_description_is_written_as_stickle_writes_keys(
    described: str, shown: str
) -> None:
    assert readable_keys(described) == shown
