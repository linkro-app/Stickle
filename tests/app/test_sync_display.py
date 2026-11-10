"""How sync stands, where it is seen: the tray's icon, tooltip and top line, the
line at the foot of Stickle Manager, and each note not in the cloud yet. A
computer that does not sync shows none of it."""

from collections.abc import Collection
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import QDate, QDateTime, QLocale, QObject, Qt, QTime, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from stickle.app.app_icon import (
    SIZES,
    TRAY_SIZES,
    SyncSign,
    halted_pixmap,
    icon_pixmap,
    make_icon,
)
from stickle.app.i18n import Translations
from stickle.app.label_icons import state_pixmap
from stickle.app.labels import CategoryTag
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.stickle_window import StickleWindow
from stickle.app.sync_display import SyncDisplay, SyncLine
from stickle.app.sync_status import SyncStatus
from stickle.app.syncing import Ended, Syncing
from stickle.app.tray import SyncShown, Tray
from stickle.app.update_check import UpdateCheck
from stickle.core.updates import Release
from stickle.sync.engine import PUTTING, Report
from stickle.sync.outcome import Outcome, Stopped

# The tray's icon


def counted(image: QImage, wanted: QColor, within: int = 15) -> int:
    """Pixels of about this colour."""
    return sum(
        1
        for x in range(image.width())
        for y in range(image.height())
        if abs(image.pixelColor(x, y).red() - wanted.red()) < within
        and abs(image.pixelColor(x, y).green() - wanted.green()) < within
        and abs(image.pixelColor(x, y).blue() - wanted.blue()) < within
        and image.pixelColor(x, y).alpha() > 180
    )


SKY = QColor("#3b82f6")
ORANGE = QColor("#f08c00")


def test_with_no_sign_the_icon_is_as_it_always_was(qtbot: QtBot) -> None:
    for size in (16, 64):
        assert icon_pixmap(size).toImage() == icon_pixmap(size, 1.0, None).toImage()
        assert counted(icon_pixmap(size).toImage(), SKY) == 0


def test_only_the_trays_sizes_are_drawn_with_a_sign(qtbot: QtBot) -> None:
    assert sorted(s.width() for s in make_icon().availableSizes()) == sorted(SIZES)
    made = make_icon(sign=SyncSign.ON)
    assert sorted(s.width() for s in made.availableSizes()) == sorted(TRAY_SIZES)


@pytest.mark.parametrize("size", [16, 20, 24, 32, 48])
def test_the_sky_of_the_lifted_corner_shows_at_every_tray_size(qtbot: QtBot, size: int) -> None:
    assert counted(icon_pixmap(size, 1.0, SyncSign.ON).toImage(), SKY) > 0


@pytest.mark.parametrize("size", [16, 24, 48])
def test_with_no_network_the_corner_fades(qtbot: QtBot, size: int) -> None:
    on = counted(icon_pixmap(size, 1.0, SyncSign.ON).toImage(), SKY)
    away = counted(icon_pixmap(size, 1.0, SyncSign.OFFLINE).toImage(), SKY)
    assert away < on
    assert icon_pixmap(size, 1.0, SyncSign.OFFLINE).toImage() != (
        icon_pixmap(size, 1.0, SyncSign.ON).toImage()
    )


@pytest.mark.parametrize("size", [16, 24, 48])
def test_stopped_has_the_orange_dot_and_nothing_else_does(qtbot: QtBot, size: int) -> None:
    assert counted(icon_pixmap(size, 1.0, SyncSign.HALTED).toImage(), ORANGE) > 0
    for other in (SyncSign.ON, SyncSign.OFFLINE, None):
        assert counted(icon_pixmap(size, 1.0, other).toImage(), ORANGE) == 0


def test_the_dot_in_a_menu_is_the_dot_on_the_icon(qtbot: QtBot) -> None:
    assert counted(halted_pixmap(16).toImage(), ORANGE) > 0


# The tray


def tray_of(translations: Translations, updates: object = None) -> Tray:
    return Tray(lambda: None, lambda: None, translations, updates=cast(UpdateCheck, updates))


class Updates(QObject):
    found = Signal(object)
    switched = Signal(object)
    release: Release | None = None


def test_not_syncing_the_tray_is_as_it_always_was(translations: Translations) -> None:
    tray = tray_of(translations)
    assert tray.toolTip() == "Stickle"
    assert not tray.sync_action.isVisible()


def test_syncing_the_tooltip_has_a_second_line(translations: Translations) -> None:
    tray = tray_of(translations)
    tray.set_sync(SyncShown("Synced with WebDAV · 2:05 PM", SyncSign.ON))
    assert tray.toolTip() == "Stickle\nSynced with WebDAV · 2:05 PM"


def test_before_anything_is_known_there_is_no_second_line(translations: Translations) -> None:
    tray = tray_of(translations)
    tray.set_sync(SyncShown("", SyncSign.ON))
    assert tray.toolTip() == "Stickle"


def test_the_second_line_goes_under_whatever_the_first_says(
    translations: Translations, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = NoteManager()
    tray = Tray(lambda: None, lambda: None, translations, manager)
    tray.set_sync(SyncShown("Offline", SyncSign.OFFLINE))
    monkeypatch.setattr(NoteManager, "set_aside", property(lambda _self: True))
    tray.retranslate()
    assert tray.toolTip() == "Stickle: notes hidden for now\nOffline"


def test_sync_ending_takes_it_all_away(translations: Translations) -> None:
    tray = tray_of(translations)
    tray.set_sync(SyncShown("Sync stopped — x", SyncSign.HALTED, True))
    tray.set_sync(None)
    assert tray.toolTip() == "Stickle"
    assert not tray.sync_action.isVisible()


def test_the_icon_is_set_only_when_it_would_change(
    translations: Translations, monkeypatch: pytest.MonkeyPatch
) -> None:
    tray = tray_of(translations)
    set_icons: list[object] = []
    real = tray.setIcon

    def noted(icon: object) -> None:
        set_icons.append(icon)
        real(icon)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(tray, "setIcon", noted)
    tray.set_sync(SyncShown("a", SyncSign.ON))
    assert len(set_icons) == 1
    for line in ("b", "c", "d"):
        tray.set_sync(SyncShown(line, SyncSign.ON))
    assert len(set_icons) == 1
    tray.set_sync(SyncShown("e", SyncSign.OFFLINE))
    assert len(set_icons) == 2
    tray.set_sync(None)
    assert len(set_icons) == 3


def test_the_tooltip_is_set_only_when_it_would_change(
    translations: Translations, monkeypatch: pytest.MonkeyPatch
) -> None:
    tray = tray_of(translations)
    set_tips: list[str] = []
    real = tray.setToolTip

    def noted(tip: str) -> None:
        set_tips.append(tip)
        real(tip)

    monkeypatch.setattr(tray, "setToolTip", noted)
    for _ in range(3):
        tray.set_sync(SyncShown("a", SyncSign.ON))
    assert set_tips == ["Stickle\na"]


def test_stopped_is_told_at_the_top_of_the_menu(translations: Translations) -> None:
    tray = tray_of(translations)
    line = "Sync stopped — check your server password"
    tray.set_sync(SyncShown(line, SyncSign.HALTED, True))
    assert tray.sync_action.isVisible()
    assert tray.sync_action.text() == line + "…"
    assert not tray.sync_action.icon().isNull()
    menu = tray.contextMenu()
    assert menu is not None
    actions = menu.actions()
    assert actions.index(tray.sync_action) == actions.index(tray.open_window_action) + 1


def test_as_usual_the_menu_has_no_line_for_it(translations: Translations) -> None:
    tray = tray_of(translations)
    for sign in (SyncSign.ON, SyncSign.OFFLINE):
        tray.set_sync(SyncShown("x", sign))
        assert not tray.sync_action.isVisible()


def test_a_mark_in_the_line_is_not_taken_for_a_key(translations: Translations) -> None:
    tray = tray_of(translations)
    tray.set_sync(SyncShown("a & b", SyncSign.HALTED, True))
    assert tray.sync_action.text() == "a && b…"


def test_needing_an_update_is_not_said_twice(translations: Translations) -> None:
    updates = Updates()
    tray = tray_of(translations, updates)
    shown = SyncShown("Sync stopped — update Stickle to carry on", SyncSign.HALTED, True, True)
    tray.set_sync(shown)
    assert tray.sync_action.isVisible()  # with nothing to get yet, this is all there is

    updates.release = Release("0.8.0", "https://example.com/")
    tray.refresh_new_version()
    assert not tray.sync_action.isVisible()
    assert tray.new_version_action.isVisible()


def test_another_stop_is_said_though_a_new_version_is_out(translations: Translations) -> None:
    updates = Updates()
    updates.release = Release("0.8.0", "https://example.com/")
    tray = tray_of(translations, updates)
    tray.set_sync(SyncShown("Sync stopped — x", SyncSign.HALTED, True))
    assert tray.sync_action.isVisible()


# The line at the foot of Stickle Manager


def test_with_nothing_to_say_the_line_is_out_of_sight(qtbot: QtBot) -> None:
    line = SyncLine()
    qtbot.addWidget(line)
    line.show_status("", False)
    assert line.isHidden()


def test_it_says_how_sync_stands_in_words_taken_as_they_are(qtbot: QtBot) -> None:
    line = SyncLine()
    qtbot.addWidget(line)
    line.show()
    line.show_status("a <b>x</b> & c", False)
    assert not line.isHidden()
    assert "a &lt;b&gt;x&lt;/b&gt; &amp; c" in line.link.text()
    assert line.link.accessibleDescription() == "a <b>x</b> & c"


def test_the_dot_goes_before_it_only_when_stopped(qtbot: QtBot) -> None:
    line = SyncLine()
    qtbot.addWidget(line)
    line.show_status("x", True)
    assert not line.dot.isHidden()
    line.show_status("x", False)
    assert line.dot.isHidden()


def test_it_says_what_pressing_it_does(qtbot: QtBot) -> None:
    line = SyncLine()
    qtbot.addWidget(line)
    assert line.link.toolTip() == "Open the connection settings"
    assert line.link.accessibleName() == "Open the connection settings"


def test_pressing_it_says_so(qtbot: QtBot) -> None:
    line = SyncLine()
    qtbot.addWidget(line)
    line.show_status("x", False)
    with qtbot.waitSignal(line.pressed, timeout=1000):
        line.link.linkActivated.emit("connections")


def test_a_new_theme_rewrites_it_in_the_new_colour(qtbot: QtBot) -> None:
    line = SyncLine()
    qtbot.addWidget(line)
    line.show_status("x", False)
    before = line.link.text()
    palette = line.palette()
    palette.setColor(palette.ColorRole.WindowText, QColor("#123456"))
    line.setPalette(palette)
    assert line.link.text() != before
    assert "#123456" in line.link.text()


def test_it_is_reached_by_the_keyboard_and_opens_the_settings(
    qtbot: QtBot, translations: Translations
) -> None:
    window = StickleWindow(NoteManager(), translations, lambda: None)
    qtbot.addWidget(window)
    window.show()
    assert window.sync_line.isHidden()
    window.sync_line.show_status("Synced with WebDAV · 2:05 PM", False)
    assert window.sync_line.isVisible()
    assert window.quit_button.nextInFocusChain() is window.sync_line.link
    window.sync_line.pressed.emit()
    assert window.settings.isVisible()
    assert window.settings.tabs.currentWidget() is window.settings.connections_page
    window.settings.close()


# Keeping them up with sync


class FakeSync(QObject):
    unsent_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.status = SyncStatus(
            "WebDAV", None, lambda: 3, lambda: QDateTime(QDate.currentDate(), QTime(14, 5))
        )


@pytest.fixture
def seen(qtbot: QtBot, translations: Translations) -> Seen:
    return Seen(qtbot, translations)


class Seen:
    def __init__(self, qtbot: QtBot, translations: Translations) -> None:
        self.manager = NoteManager()
        self.tray = Tray(lambda: None, lambda: None, translations, self.manager)
        self.window = StickleWindow(self.manager, translations, lambda: None)
        qtbot.addWidget(self.window)
        self.display = SyncDisplay(self.tray, self.window, self.manager, translations)
        self.translations = translations
        self.sync = FakeSync()

    def follow(self) -> None:
        self.display.follow(cast(Syncing, self.sync))

    def ended(self, outcome: Outcome = Outcome()) -> None:  # noqa: B008
        self.sync.status.round_ended(Ended(Report(), outcome))

    @property
    def sign(self) -> SyncSign | None:
        return self.tray._icon_is[1]  # pyright: ignore[reportPrivateUsage]


def test_not_syncing_nothing_is_shown(seen: Seen) -> None:
    seen.display.follow(None)
    assert seen.sign is None
    assert seen.tray.toolTip() == "Stickle"
    assert seen.window.sync_line.isHidden()


def test_turned_on_it_all_says_how_sync_stands(seen: Seen) -> None:
    seen.follow()
    assert seen.sign is SyncSign.ON
    assert seen.tray.toolTip().startswith("Stickle\nSynced with WebDAV")
    assert not seen.window.sync_line.isHidden()
    assert "Synced with WebDAV" in seen.window.sync_line.link.text()


def test_with_no_answer_the_corner_fades(seen: Seen) -> None:
    seen.follow()
    seen.ended(Outcome(wait=15.0, failures=1, offline=True))
    assert seen.sign is SyncSign.OFFLINE
    assert seen.tray.toolTip().endswith("Offline · 3 note(s) to upload")
    assert not seen.tray.sync_action.isVisible()
    assert seen.window.sync_line.dot.isHidden()


def test_stopped_everything_says_so_and_a_round_through_clears_it(seen: Seen) -> None:
    seen.follow()
    seen.ended(Outcome(Stopped.SIGN_IN))
    line = "Sync stopped — check your server password"
    assert seen.sign is SyncSign.HALTED
    assert seen.tray.sync_action.text() == line + "…"
    assert seen.tray.sync_action.isVisible()
    assert not seen.window.sync_line.dot.isHidden()
    seen.ended()
    assert seen.sign is SyncSign.ON
    assert not seen.tray.sync_action.isVisible()
    assert seen.window.sync_line.dot.isHidden()


def test_no_permission_names_the_stickle_folder(seen: Seen) -> None:
    seen.follow()
    seen.ended(Outcome(Stopped.NO_ACCESS))
    assert seen.tray.toolTip().endswith(
        "Sync stopped — check the Stickle folder's permissions on the server"
    )


def test_uploads_stopped_is_told_like_a_stop(seen: Seen) -> None:
    seen.follow()
    seen.ended(Outcome(full=True))
    assert seen.sign is SyncSign.HALTED
    assert seen.tray.sync_action.text() == "Uploads stopped — your WebDAV is full…"


def test_how_far_it_has_got_is_followed(seen: Seen) -> None:
    seen.follow()
    seen.sync.status.moved(PUTTING, 30, 50)
    assert seen.tray.toolTip().endswith("Uploading to WebDAV · 30 of 50 notes")


def test_needing_an_update_is_passed_on(seen: Seen) -> None:
    seen.follow()
    seen.ended(Outcome(Stopped.NEWER))
    assert seen.tray._sync is not None  # pyright: ignore[reportPrivateUsage]
    assert seen.tray._sync.update_carries_on  # pyright: ignore[reportPrivateUsage]


def test_turned_off_it_all_goes_and_the_old_sync_is_no_longer_heard(seen: Seen) -> None:
    seen.follow()
    seen.manager.show_unsent(frozenset({"x"}))
    seen.display.follow(None)
    assert seen.sign is None
    assert seen.tray.toolTip() == "Stickle"
    assert seen.window.sync_line.isHidden()
    assert seen.manager._unsent == frozenset()  # pyright: ignore[reportPrivateUsage]
    seen.ended(Outcome(Stopped.SIGN_IN))
    assert seen.sign is None
    assert seen.window.sync_line.isHidden()


def test_another_sync_takes_over_from_the_last(seen: Seen, monkeypatch: pytest.MonkeyPatch) -> None:
    seen.follow()
    old = seen.sync
    seen.sync = FakeSync()
    seen.follow()
    shown: list[object] = []
    told: list[object] = []
    monkeypatch.setattr(seen.tray, "set_sync", shown.append)
    monkeypatch.setattr(seen.manager, "show_unsent", told.append)
    old.status.round_ended(Ended(Report(), Outcome(Stopped.SIGN_IN)))
    old.unsent_changed.emit(frozenset({"a"}))
    assert shown == []
    assert told == []


def test_notes_not_in_the_cloud_are_passed_on(seen: Seen, monkeypatch: pytest.MonkeyPatch) -> None:
    seen.follow()
    told: list[Collection[str]] = []
    monkeypatch.setattr(seen.manager, "show_unsent", told.append)
    seen.sync.unsent_changed.emit(frozenset({"a"}))
    assert told == [frozenset({"a"})]


def test_a_change_of_language_says_it_in_the_new_one(qtbot: QtBot, translations_dir: Path) -> None:
    own = Translations(translations_dir)
    made = Seen(qtbot, own)
    made.follow()
    own.apply("ko")
    try:
        assert "동기화됨" in made.tray.toolTip()
        assert "동기화됨" in made.window.sync_line.link.text()
    finally:
        own.apply("en")
        QLocale.setDefault(QLocale.system())


# A note not in the cloud yet


def test_it_says_in_words_that_it_is_kept_here(qtbot: QtBot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    tag = CategoryTag(parent)
    tag.retranslate()
    plain = tag.toolTip()
    tag.set_unsent(True)
    assert tag.toolTip() == f"{plain}\nNot in the cloud yet. It's saved on this computer."
    assert tag.accessibleName() == tag.toolTip()
    tag.set_unsent(False)
    assert tag.toolTip() == plain


def test_the_cloud_is_drawn_on_the_title_bar(qtbot: QtBot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    tag = CategoryTag(parent)
    tag.set_color(QColor("#333333"))
    tag.resize(40, 40)
    parent.show()
    plain = tag.grab().toImage()
    tag.set_unsent(True)
    assert tag.grab().toImage() != plain


def test_the_cloud_is_at_the_top_right_and_faint(qtbot: QtBot) -> None:
    color = QColor("#000000")
    without = state_pixmap(None, color, None, 32).toImage()
    made = state_pixmap(None, color, None, 32, unsent=True).toImage()
    assert made != without
    changed = [
        (x, y)
        for x in range(32)
        for y in range(32)
        if made.pixelColor(x, y) != without.pixelColor(x, y)
    ]
    assert changed and all(x >= 10 and y < 16 for x, y in changed)
    assert 120 < made.pixelColor(23, 7).alpha() < 190  # the middle of it


def test_windows_show_it_and_stop_showing_it(qtbot: QtBot) -> None:
    manager = NoteManager()
    window = manager.new_note()
    window.note_id = "note-1"
    other = manager.new_note()
    other.note_id = "note-2"
    manager.show_unsent(frozenset({"note-1"}))
    assert window.title_bar.category_tag.unsent
    assert not other.title_bar.category_tag.unsent
    manager.show_unsent(frozenset())
    assert not window.title_bar.category_tag.unsent
    for each in manager.windows:
        each.release()


def test_a_window_opened_later_says_so_too(qtbot: QtBot) -> None:
    manager = NoteManager()
    manager.show_unsent(frozenset({None}))  # pyright: ignore[reportArgumentType]
    window = manager.new_note()
    assert isinstance(window, NoteWindow)
    assert window.note_id is None
    assert window.title_bar.category_tag.unsent
    window.release()


def test_tab_keys_do_not_trip_on_the_line(qtbot: QtBot) -> None:
    line = SyncLine()
    qtbot.addWidget(line)
    line.show_status("x", False)
    line.show()
    line.link.setFocus()
    QTest.keyClick(line.link, Qt.Key.Key_Tab)
