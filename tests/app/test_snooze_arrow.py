"""[Remind me again]'s arrow: wide enough to hit, set apart from the button, and
pressing it only opens the other waits; it does not snooze at once. The same
button rings in a note's strip (drawn by its style sheet) and in the missed
reminders' rows (drawn by the system's style)."""

from collections.abc import Callable

import pytest
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QStyle, QStyleOptionToolButton, QWidget
from pytestqt.qtbot import QtBot

from stickle.app.reminder_bar import ARROW_WIDTH, ReminderBar, SnoozeButton


def in_a_notes_strip(parent: QWidget) -> SnoozeButton:
    bar = ReminderBar(parent)
    bar.set_colors(QColor("#202020"), QColor("#806000"))
    bar.ring("15:00", 10)
    bar.adjustSize()
    return bar.again_button


def in_the_missed_reminders(parent: QWidget) -> SnoozeButton:
    button = SnoozeButton(parent)
    button.adjustSize()
    return button


PLACES = [in_a_notes_strip, in_the_missed_reminders]


def shown(qtbot: QtBot, place: Callable[[QWidget], SnoozeButton]) -> tuple[QWidget, SnoozeButton]:
    """The button on a window that is shown (kept by the caller: the button goes with it)."""
    parent = QWidget()
    qtbot.addWidget(parent)
    button = place(parent)
    parent.resize(400, 80)
    parent.show()
    qtbot.waitExposed(parent)
    return parent, button


def arrow_part(button: SnoozeButton) -> QRect:
    option = QStyleOptionToolButton()
    button.initStyleOption(option)
    return button.style().subControlRect(
        QStyle.ComplexControl.CC_ToolButton, option, QStyle.SubControl.SC_ToolButtonMenu, button
    )


@pytest.mark.parametrize("place", PLACES)
def test_its_arrow_is_wide_enough_to_hit(
    qtbot: QtBot, place: Callable[[QWidget], SnoozeButton]
) -> None:
    _window, button = shown(qtbot, place)
    arrow = arrow_part(button)
    assert arrow.width() >= ARROW_WIDTH  # the styles gave it 12-15 pixels
    assert arrow.right() >= button.width() - 2  # at the right end, where it is drawn


def near_its_line(arrow: QRect) -> int:
    return arrow.left() + 3


def in_its_middle(arrow: QRect) -> int:
    return arrow.center().x()


@pytest.mark.parametrize("place", PLACES)
@pytest.mark.parametrize("where", [near_its_line, in_its_middle])  # where a hand aiming at it lands
def test_pressing_its_arrow_opens_the_other_waits_without_snoozing(
    qtbot: QtBot, place: Callable[[QWidget], SnoozeButton], where: Callable[[QRect], int]
) -> None:
    _window, button = shown(qtbot, place)
    snoozed: list[int] = []
    button.snoozed.connect(snoozed.append)
    opened: list[bool] = []

    def seen() -> None:
        opened.append(True)
        # It runs until closed; tied to the menu, so not after the menu is gone.
        QTimer.singleShot(0, button.again_menu, button.again_menu.close)

    button.again_menu.aboutToShow.connect(seen)
    arrow = arrow_part(button)
    QTest.mouseClick(
        button,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        QPoint(where(arrow), arrow.center().y()),
    )
    assert opened == [True]
    assert snoozed == []


@pytest.mark.parametrize("place", PLACES)
def test_pressing_the_rest_reminds_again_after_the_usual_wait(
    qtbot: QtBot, place: Callable[[QWidget], SnoozeButton]
) -> None:
    _window, button = shown(qtbot, place)
    snoozed: list[int] = []
    button.snoozed.connect(snoozed.append)
    QTest.mouseClick(
        button,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        QPoint(arrow_part(button).left() // 2, button.height() // 2),
    )
    assert snoozed == [10]
