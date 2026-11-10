"""Text saying something went wrong reads on a light window and on a dark one."""

from collections.abc import Callable, Iterator

import pytest
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QWidget
from pytestqt.qtbot import QtBot

from stickle.app.error_text import ON_DARK, ON_LIGHT, error_style
from stickle.app.master_password import (
    CheckMasterPasswordDialog,
    MasterPasswordDialog,
    MasterPasswords,
)
from stickle.app.password_dialog import PasswordDialog
from stickle.app.recovery_key_dialog import EnterRecoveryKeyDialog, RecoveryKeys
from stickle.app.secret_dialogs import SecretTitleDialog
from stickle.app.sync_setup import SyncSetup
from stickle.app.turn_on_sync import TurnOnSync

READABLE = 4.5  # as WCAG asks of text

type Said = tuple[QWidget, QLabel]  # a window, and the line in it that says what is wrong


def _part(value: float) -> float:
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def luminance(colour: QColor) -> float:
    return (
        0.2126 * _part(colour.redF())
        + 0.7152 * _part(colour.greenF())
        + 0.0722 * _part(colour.blueF())
    )


def contrast(one: QColor, other: QColor) -> float:
    high, low = sorted((luminance(one), luminance(other)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def colour_of(style: str) -> QColor:
    return QColor(style.removeprefix("color: ").removesuffix(";"))


@pytest.mark.parametrize(
    "window",
    ["#ffffff", "#f3f3f3", "#efefef", "#000000", "#1e1e1e", "#202020", "#2d2d2d", "#353535"],
)
def test_an_error_reads_on_the_window(qtbot: QtBot, window: str) -> None:
    widget = QWidget()
    qtbot.addWidget(widget)
    palette = QPalette(widget.palette())
    palette.setColor(QPalette.ColorRole.Window, QColor(window))
    widget.setPalette(palette)
    assert contrast(colour_of(error_style(widget)), QColor(window)) >= READABLE


@pytest.fixture
def dark(qapp: QApplication) -> Iterator[None]:
    before = QApplication.palette()
    palette = QPalette(before)
    palette.setColor(QPalette.ColorRole.Window, QColor("#202020"))
    QApplication.setPalette(palette)
    yield
    QApplication.setPalette(before)


def passwords() -> MasterPasswords:
    return MasterPasswords(
        exists=lambda: True,
        set=lambda _: None,
        change=lambda _current, _new: None,
        check=lambda _: False,
        opens_notes=lambda _: False,
        recovery_key_opens=lambda _: True,
    )


def _password() -> Said:
    dialog = PasswordDialog(True, lambda _: None)
    return dialog, dialog.error


def _recovery_key() -> Said:
    dialog = EnterRecoveryKeyDialog(lambda _: None)
    return dialog, dialog.error


def _setting_spare() -> Said:
    dialog = MasterPasswordDialog(passwords())
    dialog.show_error("wrong", dialog.new)
    return dialog, dialog.message


def _checking_spare() -> Said:
    dialog = CheckMasterPasswordDialog(passwords())
    dialog.check()
    return dialog, dialog.answer


def _secret_note() -> Said:
    dialog = SecretTitleDialog()
    dialog.show_error("wrong", dialog.title)
    return dialog, dialog.message


def _turning_on() -> Said:
    keys = RecoveryKeys(exists=lambda: True, make=lambda: "", kept=lambda: None)
    window = TurnOnSync(
        SyncSetup(
            lambda: None,
            keys,
            lambda: 0,
            lambda: {},
            lambda _login, _joining: None,  # pyright: ignore[reportArgumentType]
            lambda: None,
        )
    )
    window._fail("wrong")  # pyright: ignore[reportPrivateUsage]
    return window, window.message


@pytest.mark.parametrize(
    "said_by",
    [_password, _recovery_key, _setting_spare, _checking_spare, _secret_note, _turning_on],
    ids=lambda made: made.__name__.strip("_"),
)
@pytest.mark.usefixtures("dark")
def test_each_window_takes_its_colour_from_the_window(
    qtbot: QtBot, said_by: Callable[[], Said]
) -> None:
    window, label = said_by()
    qtbot.addWidget(window)
    assert label.styleSheet() == f"color: {ON_DARK};"


def test_on_a_light_window_it_is_the_dark_red(qtbot: QtBot) -> None:
    widget = QWidget()
    qtbot.addWidget(widget)
    palette = QPalette(widget.palette())
    palette.setColor(QPalette.ColorRole.Window, QColor("#f3f3f3"))
    widget.setPalette(palette)
    assert error_style(widget) == f"color: {ON_LIGHT};"
