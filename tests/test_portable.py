"""Portable notes: a stickle-data folder next to the program holds everything,
always opens with a password, and nothing is left on the computer."""

import shutil
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot
from test_unlock import FAST, PASSWORD, Store

from stickle import __main__ as entry
from stickle.crypto.key_files import KeyFiles
from stickle.crypto.keyfile import WrongPasswordError
from stickle.crypto.recovery import generate, write_recovery
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.platform import paths
from stickle.platform.credentials import CredentialStoreUnavailableError
from stickle.platform.paths import PORTABLE_FOLDER, DataPlace, data_place, program_folder
from stickle.unlock import Blocked, NeedPassword, Unlock


def env_for(tmp_path: Path, **more: str) -> dict[str, str]:
    return {"APPDATA": str(tmp_path / "appdata"), "XDG_DATA_HOME": str(tmp_path / "xdg"), **more}


# Where the notes are


def test_without_the_folder_the_usual_place_is_used(tmp_path: Path) -> None:
    program = tmp_path / "Stickle"
    program.mkdir()

    place = data_place(env_for(tmp_path), "win32", tmp_path, program)

    assert place == DataPlace(tmp_path / "appdata" / "Stickle")


def test_with_the_folder_next_to_the_program_it_is_used(tmp_path: Path) -> None:
    program = tmp_path / "Stickle"
    (program / PORTABLE_FOLDER).mkdir(parents=True)

    place = data_place(env_for(tmp_path), "linux", tmp_path, program)

    assert place == DataPlace(program / PORTABLE_FOLDER, portable=True)


def test_the_development_folder_comes_before_everything(tmp_path: Path) -> None:
    program = tmp_path / "Stickle"
    (program / PORTABLE_FOLDER).mkdir(parents=True)
    env = env_for(tmp_path, STICKLE_DATA_DIR=str(tmp_path / "dev"))

    assert data_place(env, "linux", tmp_path, program) == DataPlace(tmp_path / "dev")


def test_an_appimage_looks_next_to_its_own_file_not_where_it_is_served(tmp_path: Path) -> None:
    env = {"APPIMAGE": str(tmp_path / "usb" / "Stickle.AppImage"), "APPDIR": "/tmp/.mount_x"}

    assert program_folder(env) == tmp_path / "usb"


def test_run_from_source_there_is_no_program_folder() -> None:
    assert program_folder({}) is None


# Opening portable notes


def test_portable_notes_start_with_a_password_and_never_ask_the_store(tmp_path: Path) -> None:
    store = Store("has_key")  # this computer has a key of its own: not used
    first = Unlock(tmp_path, store, FAST, portable=True)
    assert first.outcome() == NeedPassword(create=True)
    key = first.create_password(PASSWORD)
    connection = open_store(first.database, key)
    note = NoteRepository(connection).create("회의록")
    connection.close()

    later = Unlock(tmp_path, store, FAST, portable=True)
    assert later.outcome() == NeedPassword(create=False)
    with pytest.raises(WrongPasswordError):
        later.enter_password("wrong password")
    connection = open_store(later.database, later.enter_password(PASSWORD))
    assert NoteRepository(connection).get(note.id) == note
    connection.close()
    assert store.opened == 0
    assert store.backend.writes == 0


def test_carried_to_another_place_they_open_with_the_same_password(tmp_path: Path) -> None:
    here = tmp_path / "here"
    here.mkdir()
    unlock = Unlock(here, Store("absent"), FAST, portable=True)
    unlock.outcome()
    connection = open_store(unlock.database, unlock.create_password(PASSWORD))
    NoteRepository(connection).create("장보기")
    connection.close()

    there = tmp_path / "there"
    shutil.copytree(here, there)
    moved = Unlock(there, Store("has_key"), FAST, portable=True)

    connection = open_store(moved.database, moved.enter_password(PASSWORD))
    assert [n.body for n in NoteRepository(connection).all()] == ["장보기"]
    connection.close()


def test_notes_brought_from_a_computer_that_kept_the_key_open_with_the_recovery_key(
    tmp_path: Path,
) -> None:
    store = Store("has_key")
    key = store.key()
    assert key is not None
    connection = open_store(tmp_path / "notes.db", key)
    NoteRepository(connection).create("옮긴 메모")
    connection.close()
    recovery_key = generate()
    write_recovery(KeyFiles(tmp_path), key, recovery_key, "2026-10-26T00:00:00.000Z")

    portable = Unlock(tmp_path, Store("empty"), FAST, portable=True)
    assert portable.outcome() == Blocked("key_missing")
    opened = portable.open_with_recovery_key(recovery_key)
    assert portable.recovered_key_needs_password  # kept with a password, not the store
    with pytest.raises(CredentialStoreUnavailableError):
        portable.keep_recovered_key(opened)
    portable.set_password(opened, PASSWORD)

    again = Unlock(tmp_path, Store("empty"), FAST, portable=True)
    assert again.outcome() == NeedPassword(create=False)
    assert again.enter_password(PASSWORD) == key


# A folder that cannot be written


def never_writable(_folder: Path) -> bool:
    return False


def test_a_portable_folder_that_cannot_be_written_is_said_and_nothing_is_kept_elsewhere(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "usb" / PORTABLE_FOLDER
    folder.mkdir(parents=True)
    monkeypatch.delenv("STICKLE_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(paths, "data_place", lambda: DataPlace(folder, portable=True))
    monkeypatch.setattr(paths, "writable", never_writable)
    told: list[str] = []

    def show(_parent: object, _title: str, text: str) -> None:
        told.append(text)

    from stickle.app import portable

    monkeypatch.setattr(portable, "show", show)

    assert entry.main(["stickle"]) == 1

    assert len(told) == 1 and str(folder) in told[0]
    assert list(folder.iterdir()) == []
    assert not (tmp_path / "appdata").exists() and not (tmp_path / "xdg").exists()


def test_a_folder_can_be_told_writable_or_not(tmp_path: Path) -> None:
    assert paths.writable(tmp_path)
    assert not paths.writable(tmp_path / "missing")
    assert list(tmp_path.iterdir()) == []  # the probe leaves nothing


# Nothing left on the computer, and said in the Stickle window


def test_portable_notes_keep_nothing_on_the_computer(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stickle.app import application

    touched: list[str] = []
    monkeypatch.setattr(application, "point_launcher_here", lambda: touched.append("launcher"))
    monkeypatch.setattr(application, "app_list_entry", lambda: touched.append("app list"))

    autostart, app_list, make_portal = application.on_this_computer(False, portable=True)

    assert (autostart, app_list, make_portal()) == (None, None, None)
    assert touched == []


def test_the_stickle_window_says_where_portable_notes_are(qtbot: QtBot, tmp_path: Path) -> None:
    from stickle.app.i18n import Translations
    from stickle.app.notes import NoteManager
    from stickle.app.stickle_window import StickleWindow

    folder = tmp_path / PORTABLE_FOLDER
    window = StickleWindow(NoteManager(None), Translations(), lambda: None, portable_folder=folder)
    usual = StickleWindow(NoteManager(None), Translations(), lambda: None)

    assert window.portable_label.isVisibleTo(window)
    assert str(folder) in window.portable_label.text()
    assert not usual.portable_label.isVisibleTo(usual)
    window.deleteLater()
    usual.deleteLater()


def test_the_password_is_asked_for_as_portable_notes_need_it(
    qtbot: QtBot,
) -> None:
    from stickle.app.password_dialog import PasswordDialog

    portable = PasswordDialog(True, lambda _password: None, portable=True)
    usual = PasswordDialog(True, lambda _password: None)

    assert "stickle-data" in portable.intro.text()
    assert "keychain" not in portable.intro.text()
    assert "keychain" in usual.intro.text()
    portable.deleteLater()
    usual.deleteLater()


def test_where_only_the_desktop_keeps_shortcuts_portable_says_it_sets_none(
    qtbot: QtBot, tmp_path: Path
) -> None:

    from stickle.app.i18n import Translations
    from stickle.app.notes import NoteManager
    from stickle.app.shortcuts import GlobalShortcuts
    from stickle.app.stickle_window import StickleWindow

    shortcuts = GlobalShortcuts(None, lambda _pressed: None)  # nothing registers shortcuts
    window = StickleWindow(
        NoteManager(None),
        Translations(),
        lambda: None,
        shortcuts=shortcuts,
        portable_folder=tmp_path / PORTABLE_FOLDER,
    )
    rows = window.settings.shortcut_rows
    assert rows is not None

    assert "Portable" in rows.unavailable.text() and "--new-note" not in rows.unavailable.text()
    window.deleteLater()
