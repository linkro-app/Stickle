from collections.abc import Iterator
from typing import override

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt
from PySide6.QtGui import QContextMenuEvent, QIcon, QImage, QPalette, QRegion
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from stickle.app.application import NoteManager
from stickle.app.note_window import (
    BUTTON_SIZE,
    ICON_SCALES,
    ICON_SIZE,
    TITLE_BAR_HEIGHT,
    NoteWindow,
)
from stickle.app.palette import qcolor
from stickle.core.colors import DEFAULT_COLOR, note_colors

FOREGROUND = qcolor(note_colors(DEFAULT_COLOR).text)


@pytest.fixture
def manager(qtbot: QtBot) -> Iterator[NoteManager]:
    manager = NoteManager()
    yield manager
    for window in manager.windows:
        window.close()


def test_note_window_floats_above_other_windows_without_a_taskbar_entry(qtbot: QtBot) -> None:
    window = NoteWindow()
    qtbot.addWidget(window)

    flags = window.windowFlags()
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.FramelessWindowHint
    assert window.windowType() == Qt.WindowType.Tool
    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


def test_text_stays_dark_on_the_light_note(qtbot: QtBot) -> None:
    window = NoteWindow()
    qtbot.addWidget(window)
    window.show()

    assert window.editor.palette().color(QPalette.ColorRole.Text) == FOREGROUND
    title = window.title_bar.title
    assert title.palette().color(title.foregroundRole()) == FOREGROUND


def test_a_dark_system_theme_leaves_the_notes_text_dark(qtbot: QtBot) -> None:
    # Pinned by palettes, not a style sheet: one costs every widget under it 0.4 MB a note.
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    system = app.palette()
    dark = QPalette(system)
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText):
        dark.setColor(role, Qt.GlobalColor.white)
    app.setPalette(dark)
    try:
        window = NoteWindow(text="메모")
        qtbot.addWidget(window)
        window.show()

        assert window.styleSheet() == ""
        for part in (window.editor, window.view, window.locked_face):
            assert part.palette().color(part.foregroundRole()) == FOREGROUND
        for part in (window.editor, window.view):
            assert part.palette().color(QPalette.ColorRole.Text) == FOREGROUND
        title = window.title_bar.title
        assert title.palette().color(title.foregroundRole()) == FOREGROUND
        window.release()
    finally:
        app.setPalette(system)


def test_title_bar_buttons_are_bare_icons_until_pointed_at(qtbot: QtBot) -> None:
    window = NoteWindow()
    qtbot.addWidget(window)
    window.show()
    button = window.title_bar.close_button
    image = QImage(button.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    button.render(image, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)

    # No frame, no shade: the corners are left to the title bar.
    assert image.pixelColor(0, 0).alpha() == 0
    assert image.pixelColor(image.width() - 1, image.height() - 1).alpha() == 0
    window.release()


def test_title_bar_is_slim_and_its_buttons_small(qtbot: QtBot) -> None:
    window = NoteWindow()
    qtbot.addWidget(window)
    window.show()

    bar = window.title_bar
    assert bar.height() == TITLE_BAR_HEIGHT == 22
    for button in (bar.pin_button, bar.menu_button, bar.close_button):
        assert button.size() == QSize(BUTTON_SIZE, BUTTON_SIZE)
        assert button.iconSize() == QSize(ICON_SIZE, ICON_SIZE)


@pytest.mark.parametrize("scale", ICON_SCALES)
def test_icons_are_drawn_for_each_screen_scale(qtbot: QtBot, scale: float) -> None:
    window = NoteWindow()
    qtbot.addWidget(window)
    icon = window.title_bar.close_button.icon()

    pixmap = icon.pixmap(QSize(ICON_SIZE, ICON_SIZE), scale)

    # Drawn at that scale, not stretched from another one.
    assert pixmap.width() == round(ICON_SIZE * scale)
    assert pixmap.devicePixelRatio() == pytest.approx(scale)


def test_icons_are_quiet_until_pointed_at(qtbot: QtBot) -> None:
    window = NoteWindow()
    qtbot.addWidget(window)
    icon = window.title_bar.menu_button.icon()
    size = QSize(ICON_SIZE, ICON_SIZE)

    def darkest(mode: QIcon.Mode) -> int:
        image = icon.pixmap(size, 2.0, mode).toImage()
        return min(
            image.pixelColor(x, y).lightness()
            for x in range(image.width())
            for y in range(image.height())
            if image.pixelColor(x, y).alpha() == 255
        )

    assert darkest(QIcon.Mode.Normal) > darkest(QIcon.Mode.Active)


def test_notes_of_a_colour_share_their_title_bar_icons(qtbot: QtBot) -> None:
    # Each icon is fourteen pixmaps: drawn for every note, they cost 0.4 MB a note.
    first, second, blue = NoteWindow(), NoteWindow(), NoteWindow(color="sky")
    for window in (first, second, blue):
        qtbot.addWidget(window)

    def keys(window: NoteWindow) -> list[int]:
        bar = window.title_bar
        buttons = (bar.menu_button, bar.close_button, bar.lock_button, bar.pin_button)
        return [button.icon().cacheKey() for button in buttons]

    assert keys(first) == keys(second)
    assert set(keys(first)).isdisjoint(keys(blue))
    second.set_always_on_top(False)
    assert keys(second)[3] != keys(first)[3]  # the pin lying down is another drawing
    second.set_always_on_top(True)
    assert keys(second) == keys(first)
    for window in (first, second, blue):
        window.release()


def test_the_pin_stands_when_on_top_and_lies_when_not(qtbot: QtBot) -> None:
    window = NoteWindow()
    qtbot.addWidget(window)
    size = QSize(ICON_SIZE, ICON_SIZE)
    upright = window.title_bar.pin_button.icon().pixmap(size, 2.0).toImage()

    window.set_always_on_top(False)
    lying = window.title_bar.pin_button.icon().pixmap(size, 2.0).toImage()

    def column_of_point(image: QImage) -> set[int]:
        """Columns inked in the lowest inked row: the pin's point."""
        y = max(
            y
            for y in range(image.height())
            if any(image.pixelColor(x, y).alpha() > 60 for x in range(image.width()))
        )
        return {x for x in range(image.width()) if image.pixelColor(x, y).alpha() > 60}

    middle = upright.width() // 2
    assert any(abs(x - middle) <= 1 for x in column_of_point(upright))  # straight down
    assert not any(abs(x - middle) <= 1 for x in column_of_point(lying))  # off to a side
    window.release()


def test_the_menus_items_are_made_as_it_first_opens_and_show_the_note_as_it_is(
    qtbot: QtBot,
) -> None:
    # Fifty items kept by every note cost each 0.17 MB; most menus are never opened.
    window = NoteWindow(color="sky")
    qtbot.addWidget(window)
    window.set_locked(True)
    window.set_opacity(0.8)
    window.set_always_on_top(False)
    box = window.title_bar.menu_button.menu()
    assert box is not None
    assert box.actions() == []

    box.aboutToShow.emit()  # as it opens

    assert box is window.menu
    assert window.color_actions["sky"].isChecked()
    assert window.opacity_actions[0.8].isChecked()
    assert window.lock_action.isChecked()
    assert not window.on_top_action.isChecked()
    assert not window.move_action.isEnabled()
    assert window.reminder_action in box.actions()  # made already, for its key
    window.set_locked(False)
    assert not window.lock_action.isChecked()
    window.release()


def test_keys_of_the_menus_items_work_before_it_is_ever_opened(
    qtbot: QtBot, manager: NoteManager
) -> None:
    window = manager.new_note()
    qtbot.waitActive(window)
    asked: list[bool] = []
    window.reminder_requested.connect(lambda: asked.append(True))
    box = window.title_bar.menu_button.menu()
    assert box is not None

    QTest.keyClick(window.windowHandle(), Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier)

    assert asked == [True]
    assert box.actions() == []


def test_right_click_opens_the_menu_without_opening_colors(qtbot: QtBot) -> None:
    # A submenu opened by itself missed its first click.
    window = NoteWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    point = window.title_bar.rect().center()
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, point, window.title_bar.mapToGlobal(point)
    )
    QApplication.sendEvent(window.title_bar, event)
    qtbot.waitUntil(window.menu.isVisible)
    QTest.qWait(400)  # longer than a submenu's hover delay

    assert window.menu.activeAction() is None
    assert not window.color_menu.isVisible()
    window.menu.close()


def test_a_change_of_display_scale_redoes_the_notes_size(qtbot: QtBot) -> None:
    # Changing the scale while a note was open cut off its right end, icons and
    # all (Windows VM). The note sets its size again and is drawn whole.
    window = NoteWindow(text="배율")
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    geometry = window.geometry()
    sizes: list[int] = []
    window.installEventFilter(filter_resizes := ResizeCounter(sizes))

    window.screen().logicalDotsPerInchChanged.emit(144.0)
    qtbot.waitUntil(lambda: len(sizes) >= 2)

    assert window.geometry() == geometry
    window.removeEventFilter(filter_resizes)


class ResizeCounter(QObject):
    def __init__(self, sizes: list[int]) -> None:
        super().__init__()
        self._sizes = sizes

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Resize:
            self._sizes.append(1)
        return False


def test_every_control_has_an_accessible_name(qtbot: QtBot) -> None:
    window = NoteWindow()
    qtbot.addWidget(window)

    bar = window.title_bar
    controls: list[QWidget] = [window, window.editor, bar.close_button, bar.menu_button]
    assert all(control.accessibleName() for control in controls)


def test_new_notes_cascade_instead_of_stacking(manager: NoteManager) -> None:
    first = manager.new_note()
    second = manager.new_note()

    assert len(manager.windows) == 2
    assert second.pos() != first.pos()


def test_a_new_note_skips_a_spot_a_note_already_has(manager: NoteManager) -> None:
    # The cascade starts over with each run: a note made in an earlier run can
    # sit where the next new note would go, which then covered it exactly (Mint).
    earlier = manager.new_note()
    manager._created = 0  # pyright: ignore[reportPrivateUsage]  # as after a restart

    new = manager.new_note()

    assert new.pos() != earlier.pos()


def test_new_note_gets_keyboard_focus(qtbot: QtBot, manager: NoteManager) -> None:
    window = manager.new_note()
    qtbot.waitUntil(lambda: window.editor.hasFocus())


def test_ctrl_n_opens_another_note(qtbot: QtBot, manager: NoteManager) -> None:
    window = manager.new_note()
    qtbot.waitActive(window)

    QTest.keyClick(window.windowHandle(), Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)

    assert len(manager.windows) == 2


def test_ctrl_w_hides_the_note(qtbot: QtBot, manager: NoteManager) -> None:
    window = manager.new_note()
    qtbot.waitActive(window)

    with qtbot.waitSignal(window.closed):
        QTest.keyClick(window.windowHandle(), Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)

    assert manager.windows == ()


def test_hide_button_hides_the_note(qtbot: QtBot, manager: NoteManager) -> None:
    window = manager.new_note()

    with qtbot.waitSignal(window.closed):
        QTest.mouseClick(window.title_bar.close_button, Qt.MouseButton.LeftButton)

    assert manager.windows == ()


def test_typed_text_stays_in_the_note(qtbot: QtBot, manager: NoteManager) -> None:
    window = manager.new_note()

    QTest.keyClicks(window.editor, "Buy milk")

    assert window.editor.toPlainText() == "Buy milk"


def test_closing_the_last_note_is_announced(qtbot: QtBot, manager: NoteManager) -> None:
    first = manager.new_note()
    second = manager.new_note()

    with qtbot.assertNotEmitted(manager.last_note_closed):
        first.close()
    with qtbot.waitSignal(manager.last_note_closed):
        second.close()


def test_a_note_gone_while_waiting_for_the_window_system_is_left_alone(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shiboken6 import Shiboken

    import stickle.app.note_window as module

    window = NoteWindow(text="hello")
    window.show()
    qtbot.waitExposed(window)
    marked: list[int] = []
    # Under X11, waiting lets pending events run; one of them closed and deleted the note.
    monkeypatch.setattr(module.QGuiApplication, "sync", lambda: Shiboken.delete(window))

    def keep_off_taskbar(window_id: int, mapped: bool) -> None:
        marked.append(window_id)

    monkeypatch.setattr(module, "keep_off_taskbar", keep_off_taskbar)

    window._keep_off_taskbar_mapped()  # pyright: ignore[reportPrivateUsage]

    assert not Shiboken.isValid(window)
    assert marked == []
