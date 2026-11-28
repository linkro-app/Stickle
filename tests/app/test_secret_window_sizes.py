"""The secret notes' password windows keep their wrapped text whole when the
line saying what is wrong appears: the window stayed as tall as it was, and the
text above the field was cut off at its top and bottom (seen on Ubuntu, going
back to a backup with four buttons making the window wide)."""

from pathlib import Path

from PySide6.QtWidgets import QLabel
from pytestqt.qtbot import QtBot
from test_nothing_kept import secret_passwords, turned_on

from stickle.app.i18n import Translations
from stickle.app.secret_dialogs import OpenSecretDialog


def whole(label: QLabel) -> bool:
    return label.height() >= label.heightForWidth(label.width())


def test_a_wrong_password_leaves_the_text_above_it_whole(
    qtbot: QtBot, tmp_path: Path, translations: Translations
) -> None:
    translations.apply("ko")  # Korean wraps to more lines
    passwords = secret_passwords(tmp_path)
    turned_on(qtbot, passwords)
    dialog = OpenSecretDialog(passwords, purpose="restore", count=1)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    assert whole(dialog.intro)

    dialog.password.setText("not the secret notes' password")
    dialog.ok_button.click()

    assert dialog.isVisible() and dialog.message.isVisible()
    assert whole(dialog.intro) and whole(dialog.message)
    assert dialog.intro.geometry().bottom() < dialog.password.geometry().top()
    assert dialog.message.geometry().bottom() < dialog.buttons.geometry().top()


def test_checking_leaves_the_text_above_it_whole(
    qtbot: QtBot, tmp_path: Path, translations: Translations
) -> None:
    translations.apply("ko")
    passwords = secret_passwords(tmp_path)
    turned_on(qtbot, passwords)
    dialog = OpenSecretDialog(passwords, purpose="restore", count=1)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    dialog.working()  # said while the key is derived

    assert dialog.message.isVisible()
    assert whole(dialog.intro) and whole(dialog.message)


def test_a_long_reason_shown_at_once_is_whole_and_so_is_the_text_above(
    qtbot: QtBot, tmp_path: Path, translations: Translations
) -> None:
    # Some refusals come with nothing checked first (a password too short, say).
    translations.apply("ko")
    passwords = secret_passwords(tmp_path)
    turned_on(qtbot, passwords)
    dialog = OpenSecretDialog(passwords, purpose="restore", count=1)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    dialog.show_error("이유가 길게 이어지는 문장입니다. " * 6, dialog.password)

    assert whole(dialog.intro) and whole(dialog.message)
    assert dialog.message.geometry().bottom() < dialog.buttons.geometry().top()
