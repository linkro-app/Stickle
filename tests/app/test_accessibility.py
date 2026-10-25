"""Every control the keyboard can reach has a name a screen reader can say.

A name is the control's accessible name, the text on it (a button, a
checkbox) or a label naming it (a label's buddy). Parts of a composite
control (the line edit inside a key sequence edit) are named by the control.
"""

import secrets
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import apsw
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractScrollArea,
    QAbstractSpinBox,
    QComboBox,
    QKeySequenceEdit,
    QLabel,
    QWidget,
)
from pytestqt.qtbot import QtBot
from test_shortcuts import System

from stickle.app.backups import BackupAccess, RestoreDialog
from stickle.app.category_dialog import NewCategoryDialog
from stickle.app.data_menu import DataAccess
from stickle.app.first_run import FirstRunDialog
from stickle.app.i18n import Translations
from stickle.app.import_dialog import ImportAccess, ImportDialog
from stickle.app.label_manager import LabelManager
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.password_dialog import PasswordDialog
from stickle.app.recovery_dialog import KINDS, Problem, RecoveryDialog
from stickle.app.recovery_key_dialog import EnterRecoveryKeyDialog, RecoveryKeyDialog
from stickle.app.settings_window import RecoveryKeys, SettingsWindow
from stickle.app.shortcuts import GlobalShortcuts
from stickle.app.stickle_window import StickleWindow
from stickle.core.labels import Category, Mark
from stickle.data.backups import Backup, Contents
from stickle.data.export import ExportResult
from stickle.data.notes import NoteRepository
from stickle.data.schema import NotesDiff, open_store
from stickle.data.settings import Settings
from stickle.importers.base import Found, ImportedNote, ImportResult
from stickle.importers.bring_in import BroughtIn
from stickle.importers.sources import SOURCES
from stickle.platform.autostart import Autostart, Places
from stickle.platform.linux.appimage import AppMenuEntry

KEY = secrets.token_bytes(32)
RECOVERY_KEY = "ABCD-EFGH-JKMN-PQRS-TVWX-YZ01-2345-6789"


def recognise_nothing(_path: Path) -> None:
    return None


def no_imports() -> ImportAccess:
    return ImportAccess(
        lambda: [], recognise_nothing, lambda _s, _r: 0, lambda _s, _r, _l: BroughtIn()
    )


def import_dialog() -> ImportDialog:
    """With one app's notes found."""

    def read(_path: Path) -> ImportResult:
        return ImportResult([ImportedNote("a", "장보기", "yellow")], dropped=["fonts"])

    source = replace(SOURCES[0], read=read)
    dialog = ImportDialog(
        ImportAccess(
            lambda: [(source, Found(source.name, Path("x")))],
            recognise_nothing,
            lambda _s, _r: 1,
            lambda _s, _r, _l: BroughtIn(),
        )
    )
    dialog.refresh()
    return dialog


def restore_dialog(folder: Path) -> RestoreDialog:
    """With one backup to go back to."""
    backup = Backup(folder / "notes.db", datetime(2026, 10, 18, 12, tzinfo=UTC), False)
    return RestoreDialog(
        BackupAccess(lambda: [backup], lambda _: Contents(2, newer=False), lambda _: True)
    )


COMPOSITE = (QKeySequenceEdit, QComboBox, QAbstractSpinBox)


def part_of_another(widget: QWidget, top: QWidget) -> bool:
    """Inside a composite control, or a scroll area's own viewport."""
    parent = widget.parentWidget()
    while parent is not None and parent is not top.parentWidget():
        if isinstance(parent, COMPOSITE):
            return True
        if isinstance(parent, QAbstractScrollArea) and widget is parent.viewport():
            return True
        parent = parent.parentWidget()
    return False


def unnamed(top: QWidget) -> list[str]:
    """The controls under top that the keyboard reaches but no one names."""
    named_by_label = {id(label.buddy()) for label in top.findChildren(QLabel) if label.buddy()}
    missing: list[str] = []
    for widget in [top, *top.findChildren(QWidget)]:
        # Reached by Tab; a label whose text can be selected by mouse only is not.
        reached = widget.focusPolicy() & Qt.FocusPolicy.TabFocus
        if not reached or part_of_another(widget, top):
            continue
        # A button, a checkbox or a label (text that can be selected) says its own text.
        text = widget.text() if isinstance(widget, QAbstractButton | QLabel) else ""
        if widget.accessibleName() or text.strip() or id(widget) in named_by_label:
            continue
        missing.append(f"{type(widget).__name__} {widget.objectName()!r}")
    return missing


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


def close(*windows: QWidget) -> None:
    for window in windows:
        if isinstance(window, NoteWindow):
            window.release()
        else:
            window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_a_note_names_all_it_offers(qtbot: QtBot) -> None:
    labelled = NoteWindow(text="회의")
    labelled.set_labels(Category("id", "회사", "blue", 1), [Mark("urgent", None, "urgent", 2)])
    for window in (
        NoteWindow(text=""),
        NoteWindow(text="- [ ] 우유\n[문서](https://x.org)"),
        labelled,
    ):
        window.set_collapsed(True)  # the folded title bar takes the keyboard
        assert unnamed(window) == []
        window.set_collapsed(False)
        assert unnamed(window) == []
        close(window)


@pytest.mark.parametrize("hotkeys", [True, False])
def test_the_stickle_window_names_all_it_offers(
    qtbot: QtBot, connection: apsw.Connection, hotkeys: bool
) -> None:
    manager = NoteManager(NoteRepository(connection))
    shortcuts = GlobalShortcuts(Settings(connection), System(available=hotkeys))
    window = StickleWindow(
        manager, Translations(), lambda: None, settings=Settings(connection), shortcuts=shortcuts
    )

    try:
        assert unnamed(window) == []
    finally:  # left open, a later language switch would reach it after its database closed
        close(window)


def test_the_dialogs_name_all_they_offer(qtbot: QtBot, tmp_path: Path) -> None:
    diff = NotesDiff(missing=["회의록"], changed=[""], added=["새 메모"])
    dialogs: list[QWidget] = [
        PasswordDialog(True, lambda _: None),
        PasswordDialog(False, lambda _: None, can_recover=True),
        EnterRecoveryKeyDialog(lambda _: None),
        RecoveryKeyDialog(RECOVERY_KEY),
        FirstRunDialog(RECOVERY_KEY, True, True),
    ]
    dialogs += [
        RecoveryDialog(Problem(kind, diff=diff), tmp_path, export=lambda _: None)  # pyright: ignore[reportArgumentType]
        for kind in KINDS
    ]
    dialogs += [
        NewCategoryDialog(
            None, lambda name, color, icon: Category("id", name, color, 1, icon), "blue"
        ),
        LabelManager(NoteManager()),
        restore_dialog(tmp_path),
        import_dialog(),
    ]
    found = {type(dialog).__name__: unnamed(dialog) for dialog in dialogs}

    assert {name: missing for name, missing in found.items() if missing} == {}
    close(*dialogs)


def test_a_folded_note_is_read_by_its_title(qtbot: QtBot, translations: Translations) -> None:
    window = NoteWindow(text="# 장보기\n우유")
    window.set_collapsed(True)

    assert window.title_bar.accessibleName() == "Folded note: 장보기"
    translations.apply("ko")
    assert window.title_bar.accessibleName() == "접힌 메모: 장보기"
    assert window.title_bar.accessibleDescription() == "Enter를 누르면 펼칩니다."
    close(window)


# Tab goes as the eye reads: top to bottom, and left to right along a row.


def tab_chain(top: QWidget) -> list[QWidget]:
    """The controls Tab reaches under top, in the order it reaches them."""
    reachable = [
        widget
        for widget in top.findChildren(QWidget)
        if widget.focusPolicy() & Qt.FocusPolicy.TabFocus
        and widget.isVisibleTo(top)
        and widget.isEnabled()
        and not part_of_another(widget, top)
    ]
    # One that hands the keyboard to another (a tab widget to its tabs) is that other.
    reachable = [w for w in reachable if w.focusProxy() not in reachable]
    if not reachable:
        return []
    chain: list[QWidget] = []
    widget: QWidget | None = reachable[0]
    for _ in range(len(top.findChildren(QWidget)) + 1):
        if widget is None:
            break
        if widget in reachable and widget not in chain:
            chain.append(widget)
        widget = widget.nextInFocusChain()
        if widget is reachable[0]:
            break
    return chain


def reading_order(top: QWidget, widgets: list[QWidget]) -> list[QWidget]:
    """Top to bottom; on the same row (overlapping heights), left to right."""

    def place(widget: QWidget) -> tuple[int, int]:
        corner = widget.mapTo(top, widget.rect().topLeft())
        return corner.y(), corner.x()

    rows: list[list[QWidget]] = []
    for widget in sorted(widgets, key=place):
        y = place(widget)[0]
        if rows and y < place(rows[-1][0])[0] + rows[-1][0].height() // 2:
            rows[-1].append(widget)
        else:
            rows.append([widget])
    return [widget for row in rows for widget in sorted(row, key=lambda w: place(w)[1])]


def names(widgets: list[QWidget]) -> list[str]:
    return [
        w.accessibleName() or getattr(w, "text", lambda: "")() or type(w).__name__ for w in widgets
    ]


def rotated_to(chain: list[QWidget], first: QWidget) -> list[QWidget]:
    start = chain.index(first)
    return chain[start:] + chain[:start]


def assert_tab_follows_the_screen(qtbot: QtBot, top: QWidget) -> None:
    top.show()
    qtbot.waitExposed(top)
    chain = tab_chain(top)
    expected = reading_order(top, chain)
    assert names(rotated_to(chain, expected[0])) == names(expected)
    top.hide()


@pytest.mark.parametrize("hotkeys", [True, False])
def test_tab_goes_round_the_stickle_window_as_it_reads(
    qtbot: QtBot, connection: apsw.Connection, tmp_path: Path, hotkeys: bool
) -> None:
    """With everything it can show: login and app list switches, the recovery
    key, a hidden note, a deleted one and something on the clipboard."""
    notes = NoteRepository(connection)
    notes.set_hidden(notes.create("숨긴 메모").id, True)
    notes.delete(notes.create("지운 메모").id)
    QGuiApplication.clipboard().setText("붙여 넣을 글")
    manager = NoteManager(notes)
    shortcuts = GlobalShortcuts(Settings(connection), System(available=hotkeys))
    places = Places(home=tmp_path, config=tmp_path / "config", appdata=tmp_path)
    window = StickleWindow(
        manager,
        Translations(),
        lambda: None,
        autostart=Autostart(places, "linux", ["/x"]),
        app_list=AppMenuEntry(tmp_path / "Stickle.AppImage", b"png", tmp_path / "share"),
        recovery=RecoveryKeys(exists=lambda: True, make=lambda: RECOVERY_KEY, kept=lambda: None),
        settings=Settings(connection),
        shortcuts=shortcuts,
        data=DataAccess(
            BackupAccess(lambda: [], lambda _: None, lambda _: False),
            lambda folder: ExportResult(folder, 0, 0),
            no_imports(),
        ),
    )
    window.show_notice(True)  # the notice's two buttons, at the top

    try:
        assert_tab_follows_the_screen(qtbot, window)
    finally:
        close(window)


@pytest.mark.parametrize("hotkeys", [True, False])
def test_tab_goes_round_each_page_of_the_settings_as_it_reads(
    qtbot: QtBot, connection: apsw.Connection, tmp_path: Path, hotkeys: bool
) -> None:
    """With everything they can show: the tabs, then the page shown."""
    places = Places(home=tmp_path, config=tmp_path / "config", appdata=tmp_path)
    window = SettingsWindow(
        NoteManager(NoteRepository(connection)),
        Translations(),
        autostart=Autostart(places, "linux", ["/x"]),
        app_list=AppMenuEntry(tmp_path / "Stickle.AppImage", b"png", tmp_path / "share"),
        recovery=RecoveryKeys(exists=lambda: True, make=lambda: RECOVERY_KEY, kept=lambda: None),
        login_may_be_blocked=True,
        shortcuts=GlobalShortcuts(Settings(connection), System(available=hotkeys)),
    )
    try:
        assert unnamed(window) == []
        tabs = window.tabs
        for index in range(tabs.count()):
            tabs.setCurrentIndex(index)
            assert_tab_follows_the_screen(qtbot, window)  # the tabs first: they are on top
    finally:
        close(window)


def test_tab_goes_round_the_dialogs_as_they_read(qtbot: QtBot, tmp_path: Path) -> None:
    diff = NotesDiff(missing=["회의록"], changed=[""], added=["새 메모"])
    dialogs: list[QWidget] = [
        PasswordDialog(True, lambda _: None),
        PasswordDialog(False, lambda _: None, can_recover=True),
        EnterRecoveryKeyDialog(lambda _: None),
        RecoveryKeyDialog(RECOVERY_KEY),
        FirstRunDialog(RECOVERY_KEY, True, True),
    ]
    dialogs += [
        RecoveryDialog(Problem(kind, diff=diff), tmp_path, export=lambda _: None)  # pyright: ignore[reportArgumentType]
        for kind in KINDS
    ]
    dialogs += [
        NewCategoryDialog(
            None, lambda name, color, icon: Category("id", name, color, 1, icon), "blue"
        ),
        LabelManager(NoteManager()),
        restore_dialog(tmp_path),
        import_dialog(),
    ]
    wrong: dict[str, list[str]] = {}
    for dialog in dialogs:
        try:
            assert_tab_follows_the_screen(qtbot, dialog)
        except AssertionError:
            wrong[type(dialog).__name__] = names(tab_chain(dialog))
    close(*dialogs)

    assert wrong == {}
